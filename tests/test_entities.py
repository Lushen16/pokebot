"""Testes do EntityReader (core.entities)."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.entities import DEFAULT_RADIUS, Entity, EntityReader


class RecordingScanner:
    def __init__(self, hits=None):
        self.calls = []
        self.hits = hits or []

    def __call__(self, memory, regions, px, py, radius):
        self.calls.append({"regions": list(regions), "px": px, "py": py, "radius": radius})
        return list(self.hits)


class TestEntityReaderRegions:
    def test_uses_seeded_arena_regions(self):
        first, second = (0x1000, 0x1000), (0x5000, 0x2000)
        scanner = RecordingScanner([{"name": "Rattata", "x": 1, "y": 2, "z": 3,
                                     "anchor": 0x1500, "distance": 1}])
        reader = EntityReader(object(),
                              registry_provider=lambda: [0x1500],
                              regions_provider=lambda: [first, second],
                              scanner=scanner, radius=12)
        out = reader.scan(0, 0)
        assert scanner.calls[0]["regions"] == [first]
        assert scanner.calls[0]["radius"] == 12
        assert out == [Entity(name="Rattata", x=1, y=2, z=3, anchor=0x1500, distance=1)]

    def test_deep_scan_uses_all_regions_and_learns(self):
        first, second = (0x1000, 0x1000), (0x5000, 0x2000)
        scanner = RecordingScanner([{"name": "Zubat", "x": 9, "y": 9, "z": 0,
                                     "anchor": 0x6000, "distance": 9}])
        reader = EntityReader(object(),
                              registry_provider=lambda: [0x1500],
                              regions_provider=lambda: [first, second],
                              scanner=scanner)
        out = reader.deep_scan(0, 0)
        assert sorted(scanner.calls[0]["regions"]) == sorted([first, second])
        assert out[0].name == "Zubat"
        # arena da entidade (second) agora e memorizada para o fast path
        scanner.calls.clear()
        reader.scan(0, 0)
        assert sorted(scanner.calls[0]["regions"]) == sorted([first, second])

    def test_falls_back_to_all_regions_without_seeds(self):
        region = (0x1000, 0x1000)
        scanner = RecordingScanner([])
        reader = EntityReader(object(),
                              registry_provider=lambda: [],
                              regions_provider=lambda: [region],
                              scanner=scanner)
        reader.scan(0, 0)
        assert scanner.calls[0]["regions"] == [region]

    def test_explicit_radius_overrides_default(self):
        scanner = RecordingScanner([])
        reader = EntityReader(object(), regions_provider=lambda: [(0, 0x1000)],
                              scanner=scanner, radius=DEFAULT_RADIUS)
        reader.scan(0, 0, radius=5)
        assert scanner.calls[0]["radius"] == 5

    def test_negative_radius_rejected(self):
        with pytest.raises(ValueError):
            EntityReader(object(), radius=-1)


class _FlagMem:
    def __init__(self, size=0x4000):
        self.data = bytearray(size)

    def read_bytes(self, address, size):
        return bytes(self.data[address:address + size])


class TestShinyFlag:
    def test_reads_flag_from_creature(self):
        memory = _FlagMem()
        shiny_anchor, normal_anchor = 0x1000, 0x2000
        memory.data[shiny_anchor + 0x150:shiny_anchor + 0x154] = (0x100).to_bytes(4, "little")
        scanner = RecordingScanner([
            {"name": "Rattata", "x": 1, "y": 2, "z": 3, "anchor": shiny_anchor, "distance": 1},
            {"name": "Rattata", "x": 2, "y": 2, "z": 3, "anchor": normal_anchor, "distance": 2},
        ])
        reader = EntityReader(memory, scanner=scanner)
        out = reader.scan(0, 0)
        assert [e.shiny for e in out] == [True, False]
        assert out[0].name == "Rattata"

    def test_missing_memory_does_not_crash(self):
        scanner = RecordingScanner([{"name": "Rattata", "x": 1, "y": 2, "z": 3,
                                     "anchor": 0x1500, "distance": 1}])
        out = EntityReader(object(), scanner=scanner).scan(0, 0)
        assert out[0].shiny is False


class TestCreatureStats:
    def test_reads_hp_percent_and_creature_id(self):
        memory = _FlagMem()
        anchor = 0x1000
        memory.data[anchor + 0xE78:anchor + 0xE7C] = (58).to_bytes(4, "little")
        memory.data[anchor + 0x2C:anchor + 0x30] = (1080578289).to_bytes(4, "little")
        scanner = RecordingScanner([
            {"name": "Togepi", "x": 1, "y": 2, "z": 3, "anchor": anchor, "distance": 1},
        ])
        out = EntityReader(memory, scanner=scanner).scan(0, 0)
        assert out[0].hp_percent == 58
        assert out[0].creature_id == 1080578289

    def test_missing_memory_keeps_defaults(self):
        scanner = RecordingScanner([
            {"name": "Togepi", "x": 1, "y": 2, "z": 3, "anchor": 0x1500, "distance": 1},
        ])
        out = EntityReader(object(), scanner=scanner).scan(0, 0)
        assert out[0].hp_percent == 100
        assert out[0].creature_id is None


class _Entry:
    def __init__(self, resolved=True):
        self.is_resolved = resolved

    def to_reader_dict(self):
        return {}


class _Offsets:
    def __init__(self, entries):
        self._entries = entries

    def find(self, name):
        return self._entries.get(name)


class _Mem:
    def __init__(self, addresses, pointers):
        self._addresses = addresses
        self._pointers = pointers

    def resolve_entry_address(self, entry):
        return self._addresses.get(entry.get("which")) if entry else None

    def read_ptr(self, slot):
        return self._pointers.get(slot)


class TestEntityReaderFromOffsets:
    def _reader(self):
        offsets = _Offsets({
            "world.entity_list_ptr": _Entry(),
            "world.entity_list_end": _Entry(),
        })

        class _EntryWithKey(_Entry):
            def __init__(self, key):
                super().__init__()
                self.key = key

            def to_reader_dict(self):
                return {"which": self.key}

        offsets._entries["world.entity_list_ptr"] = _EntryWithKey("begin")
        offsets._entries["world.entity_list_end"] = _EntryWithKey("end")
        memory = _Mem({"begin": 0x2000, "end": 0x2030},
                      {0x2000: 0x40000, 0x2010: 0x40040, 0x2020: 0xA0000})
        return EntityReader.from_offsets(memory, offsets)

    def test_registry_anchors_from_handles(self):
        reader = self._reader()
        assert reader.registry_anchors() == [0x40020, 0x40060, 0xA0020]

    def test_unresolved_entries_yield_no_anchors(self):
        offsets = _Offsets({"world.entity_list_ptr": _Entry(resolved=False),
                            "world.entity_list_end": _Entry(resolved=False)})
        reader = EntityReader.from_offsets(_Mem({}, {}), offsets)
        assert reader.registry_anchors() == []
