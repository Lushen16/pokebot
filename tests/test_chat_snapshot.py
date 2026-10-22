import json

from core import chat_snapshot


def _snapshot(tick, events):
    return json.dumps({
        "schema": 2,
        "tick": tick,
        "alertEventRevision": tick // 10,
        "alertEventsDropped": 0,
        "alertEvents": events,
    }, separators=(",", ":")).encode("utf-8") + b"\x00"


def _talk(event_id, name="Player", text="hi", mode=1, self_=False):
    return {"id": event_id, "kind": "talk", "mode": mode, "value": 0,
            "name": name, "text": text, "self": self_, "pos": None}


class FakeProc:
    def __init__(self):
        self.memory = {}

    def read_bytes(self, address, size):
        chunk = bytearray(size)
        for offset, raw in self.memory.items():
            start = offset - address
            if start < 0:
                continue
            if start >= size:
                continue
            end = min(size, start + len(raw))
            chunk[start:end] = raw[:end - start]
        return bytes(chunk)


class ChunkLimitedProc(FakeProc):
    def __init__(self, max_read=4096):
        super().__init__()
        self.max_read = max_read

    def read_bytes(self, address, size):
        if size > self.max_read:
            return None
        for offset, raw in self.memory.items():
            start = address - offset
            if 0 <= start < len(raw):
                return raw[start:start + size]
        return b""


def test_read_cstring_survives_large_single_read_failure():
    raw = b'{"schema":2,"tick":7,"alertEvents":[]}' + b" " * 9000
    proc = ChunkLimitedProc()
    proc.memory[0x1000] = raw + b"\x00"
    assert chat_snapshot._read_cstring(proc, 0x1000) == raw


def test_reader_parses_snapshot_larger_than_a_single_chunk():
    proc = ChunkLimitedProc()
    body = _snapshot(10, [_talk(1)]).rstrip(b"\x00") + b" " * 9000 + b"\x00"
    proc.memory[0x5000] = body
    reader = chat_snapshot.ChatSnapshotReader(proc, preferred_address=0x5000)
    reader._candidates = [0x5000]
    reader._address = 0x5000
    assert reader.poll() == []
    assert reader.status["connected"] is True
    assert reader.status["last_error"] is None


def test_parse_chat_events_filters_non_talk_and_empty_fields():
    events = chat_snapshot.parse_chat_events({
        "alertEvents": [
            _talk(1, text="hello"),
            _talk(2, text=""),
            _talk(3, name=""),
            {"id": 4, "kind": "gm_message", "name": "GM", "text": "stop"},
            _talk(5, name="Self", text="mine", self_=True),
        ]
    })
    assert [event.id for event in events] == [1, 5]
    assert events[1].self is True


def test_parse_snapshot_stops_at_nul_and_rejects_garbage():
    assert chat_snapshot.parse_snapshot(b'{"schema":2,"tick":7}\x00xxxx')["tick"] == 7
    assert chat_snapshot.parse_snapshot(b"not-json\x00") is None
    assert chat_snapshot.parse_snapshot(b"") is None


def test_find_snapshot_candidates_sorts_by_latest_tick(monkeypatch):
    proc = FakeProc()
    proc.memory[0x1000] = _snapshot(10, [_talk(1)])
    proc.memory[0x2000] = _snapshot(20, [_talk(2)])
    monkeypatch.setattr(chat_snapshot, "_iter_private_rw_regions",
                        lambda _proc: [(0x1000, 0x3000)])
    found = chat_snapshot.find_snapshot_candidates(proc)
    assert found == [(20, 0x2000), (10, 0x1000)]


def test_reader_ignores_backlog_and_returns_only_new_events(monkeypatch):
    proc = FakeProc()
    proc.memory[0x1000] = _snapshot(100, [_talk(1, text="old")])
    monkeypatch.setattr(chat_snapshot, "_iter_private_rw_regions",
                        lambda _proc: [(0x1000, 0x4000)])
    reader = chat_snapshot.ChatSnapshotReader(proc, full_scan_interval=60)
    assert reader.poll() == []
    assert reader.poll() == []
    assert reader.status["connected"] is True

    proc.memory[0x1000] = _snapshot(110, [_talk(1, text="old"), _talk(2, text="new")])
    fresh = reader.poll()
    assert [event.id for event in fresh] == [2]
    assert fresh[0].text == "new"
    assert reader.poll() == []


def test_reader_returns_multiple_new_events_in_order(monkeypatch):
    proc = FakeProc()
    proc.memory[0x1000] = _snapshot(100, [_talk(1)])
    monkeypatch.setattr(chat_snapshot, "_iter_private_rw_regions",
                        lambda _proc: [(0x1000, 0x4000)])
    reader = chat_snapshot.ChatSnapshotReader(proc, full_scan_interval=60)
    reader.poll()
    reader.poll()
    proc.memory[0x1000] = _snapshot(120, [_talk(1), _talk(2, text="a"), _talk(3, text="b")])
    assert [event.id for event in reader.poll()] == [2, 3]


def test_reader_uses_preferred_address_when_scan_misses(monkeypatch):
    proc = FakeProc()
    proc.memory[0x5000] = _snapshot(50, [_talk(1)])
    monkeypatch.setattr(chat_snapshot, "_iter_private_rw_regions",
                        lambda _proc: [(0x1000, 0x1000)])
    reader = chat_snapshot.ChatSnapshotReader(proc, preferred_address=0x5000)
    assert reader.poll() == []
    assert reader.status["connected"] is True
    assert reader.status["address"] == 0x5000


def test_reader_reports_missing_snapshot(monkeypatch):
    proc = FakeProc()
    monkeypatch.setattr(chat_snapshot, "_iter_private_rw_regions",
                        lambda _proc: [(0x1000, 0x1000)])
    reader = chat_snapshot.ChatSnapshotReader(proc)
    assert reader.poll() == []
    assert "nenhum snapshot" in reader.status["last_error"]
