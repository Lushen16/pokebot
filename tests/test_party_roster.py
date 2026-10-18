from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core.party_roster import MemoryRosterProvider, RosterMember


def resolver(rows=None):
    return SimpleNamespace(
        proc=SimpleNamespace(pid=123, is_attached=True),
        entries={"pokemon.roster": {"base_rva": 100, "verified": True}},
        get_party_roster=Mock(return_value=rows or [RosterMember(2, "Togepi")]),
    )


def test_unconfigured_provider_returns_six_unnamed_slots():
    snapshot = MemoryRosterProvider().get_roster(123)
    assert not snapshot.detected
    assert [member.slot for member in snapshot.members] == list(range(1, 7))
    assert all(member.name is None for member in snapshot.members)


@pytest.mark.parametrize("pid", [None, 0, -1, "123", True])
def test_invalid_pid_never_accesses_memory(pid):
    factory = Mock()
    snapshot = MemoryRosterProvider(factory).get_roster(pid)
    assert not snapshot.detected
    factory.assert_not_called()


@pytest.mark.parametrize("change", ["wrong_pid", "detached", "missing", "unverified", "null_rva"])
def test_invalid_connection_or_offset_never_calls_decoder(change):
    source = resolver()
    if change == "wrong_pid": source.proc.pid = 456
    if change == "detached": source.proc.is_attached = False
    if change == "missing": source.entries = {}
    if change == "unverified": source.entries["pokemon.roster"]["verified"] = False
    if change == "null_rva": source.entries["pokemon.roster"]["base_rva"] = None
    snapshot = MemoryRosterProvider(lambda: source).get_roster(123)
    assert not snapshot.detected
    source.get_party_roster.assert_not_called()


def test_unknown_layout_is_not_guessed():
    source = resolver()
    del source.get_party_roster
    snapshot = MemoryRosterProvider(lambda: source).get_roster(123)
    assert not snapshot.detected
    assert "Layout" in snapshot.reason


def test_decoder_result_is_associated_with_correct_slots():
    source = resolver([RosterMember(5, "Chansey"), RosterMember(1, "Pikachu")])
    snapshot = MemoryRosterProvider(lambda: source).get_roster(123)
    assert snapshot.detected
    assert snapshot.members[0].name == "Pikachu"
    assert snapshot.members[4].name == "Chansey"
    assert snapshot.members[1].name is None


@pytest.mark.parametrize("rows", [[], ["Togepi"], [RosterMember(2)], [RosterMember(2, "A"), RosterMember(2, "B")], [RosterMember(1, "A")] * 7])
def test_invalid_or_duplicate_decoder_rows_fail_closed(rows):
    source = resolver()
    source.get_party_roster.return_value = rows
    assert not MemoryRosterProvider(lambda: source).get_roster(123).detected


def test_decoder_failure_discards_names():
    source = resolver()
    source.get_party_roster.side_effect = RuntimeError("read failed")
    snapshot = MemoryRosterProvider(lambda: source).get_roster(123)
    assert not snapshot.detected
    assert all(member.name is None for member in snapshot.members)


@pytest.mark.parametrize("slot,name", [(0, "A"), (7, "A"), (True, "A"), (1, ""), (1, "x\ny"), (1, "A" * 129)])
def test_member_validation(slot, name):
    with pytest.raises(ValueError):
        RosterMember(slot, name)
