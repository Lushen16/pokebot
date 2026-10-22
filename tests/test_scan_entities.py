import json
import struct
from unittest.mock import Mock, patch

import pytest

from core.memory import MemoryReader
from core.tools import scan_entities


def make_image():
    reader = MemoryReader(123)
    reader.pid = 123
    reader.pointer_size = 8
    reader.ptr_format = "<Q"
    regions = [(0x200000, 0x1000), (0x300000, 0x1000)]
    data = {}

    def write(address, raw):
        data.update({address + offset: byte for offset, byte in enumerate(raw)})

    def read(address, size):
        if not any(base <= address and address + size <= base + length for base, length in regions):
            return None
        return bytes(data.get(address + offset, 0) for offset in range(size))

    reader.read_bytes = Mock(side_effect=read)
    reader.attach = Mock(return_value=True)
    reader.detach = Mock()
    return reader, regions, write


def write_entity(write, anchor, x, y, z, name):
    write(anchor, struct.pack("<iiB", x, y, z))
    write(anchor + scan_entities.NAME_OFFSET, name.encode("ascii") + b"\x00")


def test_find_entities_filters_by_radius_and_name():
    reader, regions, write = make_image()
    write_entity(write, 0x200100, 105, 100, 1, "Pidgey")
    write_entity(write, 0x200200, 100, 103, 1, "Rattata")
    write_entity(write, 0x200300, 1000, 1000, 1, "Zubat")
    hits = scan_entities.find_entities(reader, regions, 100, 100, radius=10)
    assert [h["name"] for h in hits] == ["Rattata", "Pidgey"]
    assert [h["distance"] for h in hits] == [3, 5]
    filtered = scan_entities.find_entities(reader, regions, 100, 100, radius=10, name_filter="pidgey")
    assert [h["name"] for h in filtered] == ["Pidgey"]


def test_find_entities_rejects_non_printable_name_and_bad_z():
    reader, regions, write = make_image()
    write(0x200100, struct.pack("<iiB", 100, 100, 1))
    write(0x200100 + scan_entities.NAME_OFFSET, b"\x01\x02\x03\x00")
    write_entity(write, 0x200200, 100, 101, 99, "Oddish")
    assert scan_entities.find_entities(reader, regions, 100, 100, radius=5) == []


def test_find_entities_respects_max_hits():
    reader, regions, write = make_image()
    for index in range(6):
        write_entity(write, 0x200100 + index * 0x20, 100 + index, 100, 1, f"Mon{index}")
    hits = scan_entities.find_entities(reader, regions, 100, 100, radius=5, max_hits=2)
    assert len(hits) == 2


def test_decode_name_handles_termination():
    reader, _, write = make_image()
    write(0x200100, b"Pidgey\x00garbage")
    assert scan_entities.decode_name(reader, 0x200100) == "Pidgey"
    write(0x200200, b"")
    assert scan_entities.decode_name(reader, 0x200200) is None


def test_find_entity_list_groups_contiguous_pointers():
    reader, regions, write = make_image()
    write_entity(write, 0x200100, 105, 100, 1, "Pidgey")
    write_entity(write, 0x200200, 100, 103, 1, "Rattata")
    write(0x300000, (0x2000E0).to_bytes(8, "little"))
    write(0x300008, (0x2001E0).to_bytes(8, "little"))
    arrays = scan_entities.find_entity_list(reader, regions, [0x200100, 0x200200])
    assert len(arrays) == 1
    assert arrays[0]["base"] == 0x300000
    assert arrays[0]["slots"] == 2
    assert arrays[0]["distinct_entities"] == 2


def test_find_entity_list_ignores_single_pointer_runs():
    reader, regions, write = make_image()
    write_entity(write, 0x200100, 105, 100, 1, "Pidgey")
    write(0x300000, (0x2000E0).to_bytes(8, "little"))
    assert scan_entities.find_entity_list(reader, regions, [0x200100]) == []


def test_find_entity_list_supports_strided_slots():
    reader, regions, write = make_image()
    write_entity(write, 0x200100, 105, 100, 1, "Pidgey")
    write_entity(write, 0x200200, 100, 103, 1, "Rattata")
    write(0x300000, (0x2000E0).to_bytes(8, "little"))
    write(0x300010, (0x2001E0).to_bytes(8, "little"))
    arrays = scan_entities.find_entity_list(reader, regions, [0x200100, 0x200200])
    assert len(arrays) == 1
    assert arrays[0]["base"] == 0x300000
    assert arrays[0]["slots"] == 2
    assert arrays[0]["stride"] == 0x10


def test_cli_saves_report_and_detaches(tmp_path, capsys):
    reader, regions, write = make_image()
    write_entity(write, 0x200100, 105, 100, 1, "Pidgey")
    factory = Mock(return_value=reader)
    factory.require_admin.return_value = True
    output = tmp_path / "entities.json"
    with patch.object(scan_entities, "MemoryReader", factory), \
         patch.object(scan_entities, "read_offsets", return_value=None), \
         patch.object(scan_entities, "iter_regions", return_value=regions):
        assert scan_entities.main(["123", "--player", "100", "100", "--radius", "10",
                                   "--save", str(output)]) == 0
    factory.assert_called_once_with(123)
    reader.detach.assert_called_once_with()
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["entities"][0]["name"] == "Pidgey"
    assert "RESULTADO DO SCAN DE ENTIDADES" in capsys.readouterr().out


def test_cli_without_player_position_fails(capsys):
    reader, regions, _ = make_image()
    factory = Mock(return_value=reader)
    factory.require_admin.return_value = True
    with patch.object(scan_entities, "MemoryReader", factory), \
         patch.object(scan_entities, "read_offsets", return_value=None), \
         patch.object(scan_entities, "iter_regions", return_value=regions):
        assert scan_entities.main(["123"]) == 2
    assert "posicao do player indisponivel" in capsys.readouterr().err


def test_non_admin_does_not_attach():
    factory = Mock()
    factory.require_admin.return_value = False
    with patch.object(scan_entities, "MemoryReader", factory):
        assert scan_entities.main(["123", "--player", "1", "1"]) == 1
    factory.assert_not_called()


@pytest.mark.parametrize("arguments", [["--radius", "0"], ["--save", "offsets.json"]])
def test_cli_rejects_invalid_arguments(arguments):
    with pytest.raises(SystemExit) as error:
        scan_entities.main(["--player", "1", "1", *arguments])
    assert error.value.code == 2
