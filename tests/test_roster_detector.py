from pathlib import Path
from unittest.mock import Mock

import pytest

from core.offsets import OffsetRegistry, load_offsets
from core.roster_detector import RosterDetector


def fallback():
    return [{"slot": slot, "name": f"Slot {slot}"} for slot in range(1, 7)]


def test_missing_offset_returns_fallback_without_memory_read():
    reader = Mock()
    offsets = OffsetRegistry({}, version=2, raw={}, source=Path("offsets.json"))
    assert RosterDetector(reader, offsets).get_roster() == fallback()
    reader.read_entry.assert_not_called()


def test_null_offset_returns_fallback(tmp_path):
    path = tmp_path / "offsets.json"
    path.write_text('{"entries":{"pokemon.roster":{"base_rva":null}}}', encoding="utf-8")
    reader = Mock()
    offsets = load_offsets(path, schema_path=None, validate=False)
    assert RosterDetector(reader, offsets).get_roster() == fallback()
    reader.read_entry.assert_not_called()


def test_none_memory_result_returns_fallback():
    reader = Mock()
    offsets = Mock(read=Mock(return_value=None))
    assert RosterDetector(reader, offsets).get_roster() == fallback()
    offsets.read.assert_called_once_with(reader, "pokemon.roster")
    reader.decode_roster.assert_not_called()


def test_real_names_and_identity_are_preserved():
    offsets = Mock(read=Mock(return_value=[{"slot": 4, "name": "Chansey", "uuid": "real-id"}]))
    rows = RosterDetector(Mock(), offsets).get_roster()
    assert rows[3] == {"slot": 4, "name": "Chansey", "uuid": "real-id"}
    assert rows[0] == fallback()[0]


def test_pointer_is_only_decoded_by_explicit_reader_decoder():
    reader = Mock(decode_roster=Mock(return_value=[{"slot": 2, "name": "Blissey"}]))
    offsets = Mock(read=Mock(return_value=4096))
    assert RosterDetector(reader, offsets).get_roster()[1]["name"] == "Blissey"
    reader.decode_roster.assert_called_once_with(4096)


@pytest.mark.parametrize("rows", [[], 4096, [None], [{"slot": 0, "name": "X"}],
    [{"slot": 1, "name": ""}], [{"slot": 1, "name": "A"}, {"slot": 1, "name": "B"}]])
def test_invalid_data_never_becomes_a_detected_name(rows):
    assert RosterDetector(object(), Mock(read=Mock(return_value=rows))).get_roster() == fallback()


def test_memory_exception_is_logged_and_returns_fallback(caplog):
    offsets = Mock(read=Mock(side_effect=OSError("ReadProcessMemory failed")))
    assert RosterDetector(Mock(), offsets).get_roster() == fallback()
    assert "ReadProcessMemory failed" in caplog.text
