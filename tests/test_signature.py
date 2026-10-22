"""Testes do scanner de assinaturas (core.signature) e integracao no resolver."""
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import signature
from core.memory import MemoryReader

BASE = 0x140000000
MODULE = "PokeAlliance_dx.exe"
PAT = "48 8B 0D ?? ?? ?? ?? 48 85 C9"


def build_image(instructions=(), size=0x4000):
    """Imagem .text sintetica com instrucoes `mov reg,[rip+disp]`."""
    image = bytearray(b"\x90" * size)
    for off, target in instructions:
        disp = target - (off + 7)
        image[off:off + 3] = b"\x48\x8B\x0D"
        image[off + 3:off + 7] = struct.pack("<i", disp)
        image[off + 7:off + 10] = b"\x48\x85\xC9"
    return image


class FakeMemory:
    def __init__(self, image, base=BASE, module=MODULE):
        self.image = bytes(image)
        self.main_module_base = base
        self.main_module_size = len(self.image)
        self.modules = {module.lower(): {"base": base, "size": len(self.image)}}

    def read_bytes(self, address, size):
        offset = address - self.main_module_base
        if offset < 0 or offset >= len(self.image):
            return None
        return self.image[offset:offset + size]


def make_reader(image, base=BASE):
    reader = MemoryReader(123)
    reader.main_module_base = base
    reader.main_module_size = len(image)
    reader.modules = {MODULE.lower(): {"base": base, "size": len(image)}}
    reader.read_bytes = lambda address, size: FakeMemory(image, base).read_bytes(address, size)
    return reader


class TestCompilePattern:
    def test_wildcards_and_hex(self):
        values, mask = signature.compile_pattern("48 8B ?? 0D")
        assert values == bytes([0x48, 0x8B, 0x00, 0x0D])
        assert mask == bytes([1, 1, 0, 1])

    def test_compact_hex(self):
        values, mask = signature.compile_pattern("488B0D")
        assert values == bytes([0x48, 0x8B, 0x0D])
        assert mask == bytes([1, 1, 1])

    def test_alt_wildcards(self):
        for wildcard in ("?", "**", ".."):
            _, mask = signature.compile_pattern(f"48 {wildcard}")
            assert mask == bytes([1, 0])

    def test_rejects_bad_tokens(self):
        for bad in ("", "   ", "48 ZZ", "4", "48 8B 0D X"):
            with pytest.raises(signature.SignatureError):
                signature.compile_pattern(bad)


class TestFindPattern:
    def test_single_hit(self):
        mem = FakeMemory(build_image([(0x100, 0x2000)]))
        hits = signature.find_pattern(mem, PAT, MODULE)
        assert hits == [BASE + 0x100]

    def test_multiple_hits(self):
        mem = FakeMemory(build_image([(0x100, 0x2000), (0x800, 0x2100)]))
        hits = signature.find_pattern(mem, PAT, MODULE)
        assert hits == [BASE + 0x100, BASE + 0x800]
        assert signature.count_matches(mem, PAT, MODULE) == 2

    def test_max_hits_stops_early(self):
        mem = FakeMemory(build_image([(0x100, 0x2000), (0x800, 0x2100)]))
        assert len(signature.find_pattern(mem, PAT, MODULE, max_hits=1)) == 1

    def test_chunk_boundary_is_handled(self):
        mem = FakeMemory(build_image([(0x100, 0x2000)]))
        hits = signature.find_pattern(mem, PAT, MODULE, chunk_size=8)
        assert hits == [BASE + 0x100]

    def test_missing_returns_empty(self):
        mem = FakeMemory(build_image([]))
        assert signature.find_pattern(mem, PAT, MODULE) == []

    def test_unknown_module_raises(self):
        mem = FakeMemory(build_image([]))
        with pytest.raises(signature.SignatureError):
            signature.find_pattern(mem, PAT, "nope.exe")


class TestResolve:
    def test_rip_resolution(self):
        mem = FakeMemory(build_image([(0x100, 0x2000)]))
        target = signature.resolve_signature(mem, PAT, MODULE, disp_offset=3, instr_length=7)
        assert target == BASE + 0x2000

    def test_resolve_signature_without_disp(self):
        mem = FakeMemory(build_image([(0x100, 0x2000)]))
        assert signature.resolve_signature(mem, PAT, MODULE) == BASE + 0x100

    def test_ambiguous_raises(self):
        mem = FakeMemory(build_image([(0x100, 0x2000), (0x800, 0x2100)]))
        with pytest.raises(signature.SignatureError):
            signature.resolve_signature(mem, PAT, MODULE, disp_offset=3, instr_length=7)

    def test_missing_raises(self):
        mem = FakeMemory(build_image([]))
        with pytest.raises(signature.SignatureError):
            signature.resolve_signature(mem, PAT, MODULE)

    def test_module_view_adapter(self):
        image = build_image([(0x100, 0x2000)])
        view = signature.ModuleView(
            FakeMemory(image).read_bytes, BASE, len(image), MODULE)
        assert signature.resolve_signature(
            view, PAT, MODULE, disp_offset=3, instr_length=7) == BASE + 0x2000


class TestReaderIntegration:
    def test_signature_entry_resolves(self):
        reader = make_reader(build_image([(0x100, 0x2000)]))
        entry = {
            "module": MODULE, "base_rva": None, "steps": [], "deref": False,
            "terminal": 0, "type": "int32", "verified": True,
            "source": "signature_scan", "signature": PAT,
            "signature_disp": 3, "signature_length": 7,
        }
        assert reader.resolve_entry_address(entry) == BASE + 0x2000

    def test_rva_fallback_when_signature_missing(self):
        reader = make_reader(build_image([]))
        entry = {
            "module": MODULE, "base_rva": 0x2000, "steps": [], "deref": False,
            "terminal": 0, "type": "int32", "verified": True,
            "source": "signature_scan", "signature": PAT,
            "signature_disp": 3, "signature_length": 7,
        }
        assert reader.resolve_entry_address(entry) == BASE + 0x2000

    def test_signature_only_unresolved_returns_none(self):
        reader = make_reader(build_image([]))
        entry = {
            "module": MODULE, "base_rva": None, "steps": [], "deref": False,
            "terminal": 0, "type": "int32", "verified": True,
            "source": "signature_scan", "signature": PAT,
            "signature_disp": 3, "signature_length": 7,
        }
        assert reader.resolve_entry_address(entry) is None

    def test_signature_cache_avoids_rescan(self):
        reader = make_reader(build_image([(0x100, 0x2000)]))
        calls = {"n": 0}
        original = signature.resolve_signature

        def counting(*args, **kwargs):
            calls["n"] += 1
            return original(*args, **kwargs)

        signature.resolve_signature = counting
        try:
            entry = {
                "module": MODULE, "base_rva": None, "steps": [], "deref": False,
                "terminal": 0, "type": "int32", "verified": True,
                "source": "signature_scan", "signature": PAT,
                "signature_disp": 3, "signature_length": 7,
            }
            reader.resolve_entry_address(entry)
            reader.resolve_entry_address(entry)
        finally:
            signature.resolve_signature = original
        assert calls["n"] == 1
