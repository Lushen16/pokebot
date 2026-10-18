"""
tests/test_scan_party_array.py - Testes unitários para core/tools/scan_party_array.py
"""
import argparse
import json
from unittest.mock import Mock, patch
import pytest

from core.tools import scan_party_array


def test_parse_integer():
    assert scan_party_array.parse_integer("1234") == 1234
    assert scan_party_array.parse_integer("0x1A2B") == 0x1A2B
    assert scan_party_array.parse_integer("0X1000") == 0x1000
    with pytest.raises(argparse.ArgumentTypeError):
        scan_party_array.parse_integer("invalid_hex")


def test_extract_pokemon_name():
    reader = Mock()

    # ASCII name
    data_ascii = b"\x00" * 16 + b"Togepi\x00" + b"\x00" * 32
    reader.read_bytes.return_value = data_ascii
    assert scan_party_array.extract_pokemon_name(reader, 0x200000) == "Togepi"

    # UTF-16LE name
    data_utf16 = b"\x00" * 16 + "Pikachu\x00\x00".encode("utf-16le") + b"\x00" * 32
    reader.read_bytes.return_value = data_utf16
    assert scan_party_array.extract_pokemon_name(reader, 0x200000) == "Pikachu"

    # Low address or unreadable
    assert scan_party_array.extract_pokemon_name(reader, 0x1000) is None
    reader.read_bytes.return_value = None
    assert scan_party_array.extract_pokemon_name(reader, 0x200000) is None


def test_is_readable_pointer():
    reader = Mock()
    # 0 (NULL) is valid for empty slot
    assert scan_party_array.is_readable_pointer(reader, 0) is True

    # Below 0x10000 or above user range
    assert scan_party_array.is_readable_pointer(reader, 0x500) is False
    assert scan_party_array.is_readable_pointer(reader, 0x8000000000000000) is False

    # Readable valid address
    reader.read_bytes.return_value = b"\x00" * 8
    assert scan_party_array.is_readable_pointer(reader, 0x200000) is True

    # Unreadable address
    reader.read_bytes.return_value = None
    assert scan_party_array.is_readable_pointer(reader, 0x200000) is False


def test_evaluate_party_array_with_target():
    reader = Mock()
    target_addr = 0x200100

    # Mock 6 pointers: Togepi (target), Pikachu, Charizard, empty (0), empty (0), empty (0)
    pointers = [target_addr, 0x200200, 0x200300, 0, 0, 0]
    raw_array = b"".join(p.to_bytes(8, "little") for p in pointers)

    def mock_read(addr, size):
        if addr == 0x100000:
            return raw_array[:size]
        if addr == target_addr:
            return (b"Togepi\x00" + b"\x00" * 32)[:size]
        if addr == 0x200200:
            return (b"Pikachu\x00" + b"\x00" * 32)[:size]
        if addr == 0x200300:
            return (b"Charizard\x00" + b"\x00" * 32)[:size]
        return b"\x00" * size

    reader.read_bytes.side_effect = mock_read

    eval_result = scan_party_array.evaluate_party_array(reader, 0x100000, target_addr)
    assert eval_result is not None
    assert eval_result["array_base"] == 0x100000
    assert eval_result["target_slot"] == 1
    assert eval_result["valid_count"] == 3
    assert eval_result["named_count"] == 3
    assert len(eval_result["slots"]) == 6
    assert eval_result["slots"][0]["name"] == "Togepi"
    assert eval_result["slots"][1]["name"] == "Pikachu"
    assert eval_result["slots"][2]["name"] == "Charizard"
    assert eval_result["slots"][3]["is_empty"] is True


def test_find_pointer_references_across_regions():
    reader = Mock()
    target = 0x140F0365270
    needle = target.to_bytes(8, "little")

    # Region 1 contains needle at offset 16
    region1_data = b"\x00" * 16 + needle + b"\x00" * 40
    # Region 2 does not contain needle
    region2_data = b"\x00" * 64

    def mock_read(addr, size):
        if addr == 0x100000:
            return region1_data[:size]
        if addr == 0x200000:
            return region2_data[:size]
        return None

    reader.read_bytes.side_effect = mock_read
    regions = [(0x100000, len(region1_data)), (0x200000, len(region2_data))]

    hits = scan_party_array.find_pointer_references(reader, regions, target, chunk_size=64)
    assert hits == [0x100010]


def test_find_party_arrays_slot_backtracking():
    reader = Mock()
    target = 0x200100

    # The party array is at 0x100000.
    # Target is placed in slot 2 (offset +8 -> address 0x100008).
    pointers = [0x200050, target, 0, 0, 0, 0]
    raw_array = b"".join(p.to_bytes(8, "little") for p in pointers)

    def mock_read(addr, size):
        if addr == 0x100000:
            return raw_array[:size]
        if addr in (0x200050, target):
            return (b"Bulbasaur\x00" + b"\x00" * 32)[:size]
        return b"\x00" * size

    reader.read_bytes.side_effect = mock_read
    regions = [(0x100000, len(raw_array))]

    candidates = scan_party_array.find_party_arrays(reader, regions, target, check_all_slots=True)
    assert len(candidates) >= 1
    best = candidates[0]
    assert best["array_base"] == 0x100000
    assert best["target_slot"] == 2


def test_generate_offsets_entries():
    chain = {
        "module": "PokeAlliance_dx.exe",
        "rva": 0x12345,
        "steps": [0x10, 0x20],
    }
    party_array = {
        "slots": [
            {"slot": 1, "offset": 0x00, "name": "Togepi"},
            {"slot": 2, "offset": 0x08, "name": "Pikachu"},
            {"slot": 3, "offset": 0x10, "name": None},
            {"slot": 4, "offset": 0x18, "name": None},
            {"slot": 5, "offset": 0x20, "name": None},
            {"slot": 6, "offset": 0x28, "name": None},
        ]
    }
    entries = scan_party_array.generate_offsets_entries(chain, party_array)

    assert "pokemon.roster_ptr" in entries
    assert entries["pokemon.roster_ptr"]["base_rva"] == 0x12345
    assert entries["pokemon.roster_ptr"]["steps"] == [0x10, 0x20]
    assert entries["pokemon.roster_ptr"]["deref"] is False

    assert "pokemon.roster.slot_1" in entries
    assert entries["pokemon.roster.slot_1"]["steps"] == [0x10, 0x20, 0x00]
    assert entries["pokemon.roster.slot_1"]["deref"] is True
    assert "Togepi" in entries["pokemon.roster.slot_1"]["note"]

    assert "pokemon.roster.slot_2" in entries
    assert entries["pokemon.roster.slot_2"]["steps"] == [0x10, 0x20, 0x08]
    assert entries["pokemon.roster.slot_2"]["deref"] is True


def test_main_cli_not_admin(capsys):
    with patch("core.memory.MemoryReader.require_admin", return_value=False):
        res = scan_party_array.main(["--target", "0x123456"])
        assert res == 1
        err = capsys.readouterr().err
        assert "Administrador" in err


def test_main_cli_flow(capsys, tmp_path):
    reader = Mock()
    reader.pid = 9999
    reader.is_64bit = True
    reader.attach.return_value = True
    reader.detach = Mock()
    reader.pointer_size = 8

    target = 0x200100
    pointers = [target, 0x200200, 0, 0, 0, 0]
    raw_array = b"".join(p.to_bytes(8, "little") for p in pointers)

    def mock_read(addr, size):
        if addr == 0x100000:
            return raw_array[:size]
        if addr in (target, 0x200200):
            return (b"Togepi\x00" + b"\x00" * 32)[:size]
        return b"\x00" * size

    reader.read_bytes.side_effect = mock_read
    regions = [(0x100000, len(raw_array))]
    save_file = tmp_path / "party_report.json"

    with patch("core.memory.MemoryReader.require_admin", return_value=True), \
         patch("core.tools.scan_party_array.MemoryReader", return_value=reader), \
         patch("core.tools.scan_party_array.iter_regions", return_value=regions), \
         patch("core.tools.scan_party_array.build_chains", return_value=[[(0x7FF7AC840010, 0x0)]]), \
         patch("core.tools.scan_party_array.format_static_chain", return_value={
             "module": "PokeAlliance_dx.exe", "rva": 0x10, "steps": [0]
         }):
        res = scan_party_array.main(["--target", hex(target), "--save", str(save_file)])
        assert res == 0
        reader.detach.assert_called_once()
        out = capsys.readouterr().out
        assert "PARTY ARRAY LOCALIZADO COM SUCESSO" in out
        assert "Slot 1" in out
        assert save_file.exists()
        saved_data = json.loads(save_file.read_text(encoding="utf-8"))
        assert saved_data["target_pokemon_address"] == target
