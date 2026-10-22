import struct
import zlib
from pathlib import Path

from core.minimap_map import BLOCK_BYTES, BLOCK_SIZE, OtmmMinimap, find_latest_minimap


def _block(x, y, z, pixels=None):
    raw = bytearray((0x01, 0x81, 0x0A) * (BLOCK_SIZE * BLOCK_SIZE))
    for (dx, dy), rgb in (pixels or {}).items():
        offset = ((dy * BLOCK_SIZE) + dx) * 3
        raw[offset:offset + 3] = bytes(rgb)
    header = struct.pack("<HHBH", x, y, z, 0x0051)
    return header + zlib.compress(bytes(raw), 9)


def _write_minimap(path, blocks):
    header = (b"OTMM" + b"\x16\x00\x01\x00\x00\x00\x00\x00\x08\x00"
              + b"OTMM 1.0")
    path.write_bytes(header + b"".join(blocks))


def test_otmm_reads_walkable_and_blocked_tiles(tmp_path):
    path = tmp_path / "minimap854.otmm"
    _write_minimap(path, [
        _block(0, 0, 8, {(0, 0): (0x05, 0x56, 0x0F),
                         (1, 0): (0x05, 0xBA, 0x0A),
                         (32, 32): (0x05, 0x56, 0x0F)}),
        _block(64, 0, 8, {(0, 0): (0x05, 0x56, 0x0F)}),
    ])
    minimap = OtmmMinimap(path)
    assert minimap.block_count == 2
    assert minimap.walkable(1, 1, 8) is True
    assert minimap.walkable(0, 0, 8) is False
    assert minimap.walkable(32, 32, 8) is False
    assert minimap.walkable(64, 0, 8) is False
    assert minimap.walkable(65, 0, 8) is True
    assert minimap.walkable(1, 1, 7) is None


def test_otmm_unknown_color_is_none(tmp_path):
    path = tmp_path / "minimap.otmm"
    _write_minimap(path, [_block(0, 0, 0, {(5, 5): (0x02, 0x00, 0x00)})])
    minimap = OtmmMinimap(path)
    assert minimap.walkable(5, 5, 0) is None


def test_find_latest_minimap_prefers_newest(tmp_path):
    old = tmp_path / "minimap1.otmm"
    new = tmp_path / "minimap2.otmm"
    _write_minimap(old, [_block(0, 0, 0)])
    _write_minimap(new, [_block(0, 0, 0)])
    old.touch()
    new.touch()
    import os
    os.utime(old, (1_000, 1_000))
    assert find_latest_minimap(tmp_path) == new
