import ctypes
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from core.memory import MemoryReader
from core.tools import scan_roster
from core.tools.scan_value import iter_regions


@pytest.fixture(params=[4, 8])
def image(request):
    reader = MemoryReader(123)
    reader.pid = 123
    reader.pointer_size = request.param
    reader.ptr_format = "<I" if request.param == 4 else "<Q"
    reader.modules = {"pokealliance_dx.exe": {"base": 0x100000, "size": 0x100}}
    regions = [(0x100000, 0x100), (0x200000, 0x600), (0x300000, 0x100)]
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
    write(0x200140, b"Togepi")
    write(0x200280, "Togepi".encode("utf-16le"))
    return reader, regions, write


def stats():
    return {"bytes_read": 0, "unreadable_blocks": 0}


def test_ascii_and_utf16_across_chunk_boundaries(image):
    reader, regions, _ = image
    hits, truncated = scan_roster.find_names(reader, regions, "Togepi", 7, 100, stats())
    assert [(hit["address"], hit["encoding"]) for hit in hits] == [
        (0x200140, "ASCII"), (0x200280, "UTF-16LE")]
    assert not truncated


def test_context_reads_back_0x100_without_inventing_struct(image):
    reader, regions, _ = image
    hits, _ = scan_roster.find_names(reader, regions, "Togepi", 64, 100, stats())
    context = scan_roster.read_context(reader, hits[0], 0x100)
    assert context["address"] == 0x200040
    assert bytes.fromhex(context["hex"])[0x100:0x106] == b"Togepi"
    assert "struct" not in context


def test_static_two_level_chain_revalidates_struct_and_string(image):
    reader, regions, write = image
    write(0x100010, (0x300000).to_bytes(reader.pointer_size, "little"))
    write(0x300020, (0x200100).to_bytes(reader.pointer_size, "little"))
    hits, _ = scan_roster.find_names(reader, regions, "Togepi", 64, 100, stats())
    chains, truncated = scan_roster.find_static_chains(reader, regions, hits, max_depth=2, chunk_size=13)
    assert not truncated
    assert len(chains) == 1
    result = chains[0]
    assert result["candidate_struct_address"] == 0x200100
    assert result["name_offset"] == 0x40
    assert result["struct_entry"]["base_rva"] == 0x10
    assert result["struct_entry"]["steps"] == [0x20, 0]
    assert result["string_entry"]["steps"] == [0x20, 0x40]
    assert not result["struct_entry"]["verified"]
    assert reader.resolve_entry_address(result["struct_entry"]) == 0x200100
    assert reader.resolve_entry_address(result["string_entry"]) == 0x200140


def test_all_children_are_considered_not_just_nearest(image):
    reader, regions, write = image
    write(0x100010, (0x200100).to_bytes(reader.pointer_size, "little"))
    write(0x200160, b"Togepi")
    hits, _ = scan_roster.find_names(reader, regions, "Togepi", 64, 100, stats())
    chains, _ = scan_roster.find_static_chains(reader, regions, hits, max_depth=1)
    assert {row["string_address"] for row in chains} == {0x200140, 0x200160}


def test_unaligned_pointer_option(image):
    reader, regions, write = image
    write(0x100011, (0x200100).to_bytes(reader.pointer_size, "little"))
    hits, _ = scan_roster.find_names(reader, regions, "Togepi", 64, 100, stats())
    aligned, _ = scan_roster.find_static_chains(reader, regions, hits, max_depth=1)
    unaligned, _ = scan_roster.find_static_chains(reader, regions, hits, max_depth=1, alignment=1)
    assert not aligned
    assert any(row["struct_entry"]["base_rva"] == 0x11 for row in unaligned)


def test_limits_are_explicit(image):
    reader, regions, write = image
    write(0x100010, (0x200100).to_bytes(reader.pointer_size, "little"))
    hits, truncated = scan_roster.find_names(reader, regions, "Togepi", 64, 1, stats())
    assert truncated and len(hits) == 1
    chains, truncated = scan_roster.find_static_chains(reader, regions, hits, max_chains=1)
    assert truncated and len(chains) == 1


def test_failed_large_read_retries_pages():
    reader = Mock()
    reader.read_bytes.side_effect = lambda address, size: b"A" * size if size <= 4096 else None
    counters = stats()
    blocks = list(scan_roster.iter_blocks(reader, 0x100000, 8192, 8192, counters))
    assert [(address, len(raw)) for address, raw in blocks] == [(0x100000, 4096), (0x101000, 4096)]
    assert counters == {"bytes_read": 8192, "unreadable_blocks": 0}


def test_unreadable_page_breaks_string_continuity():
    reader = Mock()
    reader.read_bytes.side_effect = [b"Tog", None, b"epi"]
    counters = stats()
    hits, _ = scan_roster.find_names(reader, [(0x100000, 9)], "Togepi", 3, 100, counters)
    assert hits == []
    assert counters["unreadable_blocks"] == 1


def test_virtual_query_failure_terminates_instead_of_scanning_entire_address_space():
    query = Mock(return_value=0)
    with patch("ctypes.WinDLL", return_value=SimpleNamespace(VirtualQueryEx=query)):
        assert list(iter_regions(SimpleNamespace(handle=1, pointer_size=8))) == []
    query.assert_called_once()


def test_virtual_query_32_bit_bound_stops_at_4gb():
    def query_region(handle, address, output, length):
        region = output._obj
        region.BaseAddress = 0
        region.RegionSize = 0x100000000
        region.State = 0x1000
        region.Protect = 0x04
        return ctypes.sizeof(region)

    query = Mock(side_effect=query_region)
    with patch("ctypes.WinDLL", return_value=SimpleNamespace(VirtualQueryEx=query)):
        assert list(iter_regions(SimpleNamespace(handle=1, pointer_size=4))) == [(0, 0x100000000)]
    query.assert_called_once()


def test_cli_prompts_saves_report_and_detaches(image, tmp_path, capsys):
    reader, regions, write = image
    write(0x100010, (0x200100).to_bytes(reader.pointer_size, "little"))
    factory = Mock(return_value=reader)
    factory.require_admin.return_value = True
    output = tmp_path / "roster.json"
    with patch.object(scan_roster, "MemoryReader", factory), \
         patch.object(scan_roster, "iter_regions", return_value=regions), \
         patch("builtins.input", return_value="Togepi") as prompt:
        assert scan_roster.main(["123", "--max-depth", "1", "--save", str(output)]) == 0
    prompt.assert_called_once()
    factory.assert_called_once_with(123)
    reader.detach.assert_called_once_with()
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["chains"][0]["struct_entry"]["base_rva"] == 0x10
    assert "RESULTADO DO SCAN" in capsys.readouterr().out


def test_attach_failure_aborts_scan_and_detaches(capsys):
    factory = Mock()
    factory.require_admin.return_value = True
    factory.return_value.attach.return_value = False
    factory.return_value.last_error = "Access Denied"
    with patch.object(scan_roster, "MemoryReader", factory), \
         patch.object(scan_roster, "iter_regions") as regions:
        assert scan_roster.main(["--name", "Togepi"]) == 1
    regions.assert_not_called()
    factory.return_value.detach.assert_called_once_with()
    assert "Access Denied" in capsys.readouterr().err


def test_non_admin_does_not_attach():
    factory = Mock()
    factory.require_admin.return_value = False
    with patch.object(scan_roster, "MemoryReader", factory):
        assert scan_roster.main(["--name", "Togepi"]) == 1
    factory.assert_not_called()


def test_changed_string_is_not_reported_as_revalidated_chain(image):
    reader, regions, write = image
    write(0x100010, (0x200100).to_bytes(reader.pointer_size, "little"))
    hits, _ = scan_roster.find_names(reader, regions, "Togepi", 64, 100, stats())
    write(0x200140, b"Raichu")
    chains, _ = scan_roster.find_static_chains(reader, regions, hits, max_depth=1)
    assert chains == []


def test_frontier_budget_returns_explicit_partial_result(image):
    reader, regions, write = image
    write(0x300010, (0x200100).to_bytes(reader.pointer_size, "little"))
    write(0x300020, (0x200100).to_bytes(reader.pointer_size, "little"))
    hits, _ = scan_roster.find_names(reader, regions, "Togepi", 64, 100, stats())
    chains, truncated = scan_roster.find_static_chains(reader, regions, hits, max_frontier=1)
    assert chains == []
    assert truncated


def test_cli_cancellation_releases_process(image):
    reader, _, _ = image
    factory = Mock(return_value=reader)
    factory.require_admin.return_value = True
    with patch.object(scan_roster, "MemoryReader", factory), \
         patch.object(scan_roster, "iter_regions", side_effect=KeyboardInterrupt):
        assert scan_roster.main(["--name", "Togepi"]) == 130
    reader.detach.assert_called_once_with()


def test_cli_scan_exception_is_logged_and_releases_process(image, caplog):
    reader, _, _ = image
    factory = Mock(return_value=reader)
    factory.require_admin.return_value = True
    with patch.object(scan_roster, "MemoryReader", factory), \
         patch.object(scan_roster, "iter_regions", side_effect=OSError("process exited")):
        assert scan_roster.main(["--name", "Togepi"]) == 1
    assert "process exited" in caplog.text
    reader.detach.assert_called_once_with()


def test_no_hits_reports_no_chain_instead_of_inventing_offset(image, capsys):
    reader, regions, _ = image
    factory = Mock(return_value=reader)
    factory.require_admin.return_value = True
    with patch.object(scan_roster, "MemoryReader", factory), \
         patch.object(scan_roster, "iter_regions", return_value=regions):
        assert scan_roster.main(["--name", "Chansey"]) == 0
    assert "Strings: 0 | Chains: 0" in capsys.readouterr().out
    reader.detach.assert_called_once_with()


@pytest.mark.parametrize("arguments", [["--max-depth", "0"], ["--lookbehind", "-1"],
    ["--max-offset", "-1"], ["--max-hits", "0"], ["--save", "offsets.json"]])
def test_cli_rejects_invalid_limits_or_offsets_overwrite(arguments):
    with pytest.raises(SystemExit) as error:
        scan_roster.main(["--name", "Togepi", *arguments])
    assert error.value.code == 2
