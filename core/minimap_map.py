# pokebot/core/minimap_map.py
"""Leitura do minimapa OTMM do PokeAlliance para consultar walkability.

O cliente grava ``minimap<mapid>.otmm`` em
``%APPDATA%\\PokeAlliance\\PokeAllianceV3``. O formato observado:

    OTMM | versao(2) | ... | "OTMM 1.0" (offset 14)
    blocos: x(uint16) y(uint16) z(uint8) extra(uint16) + zlib

Cada bloco descomprime para 64x64 pixels RGB (12288 bytes). No mapa deste
cliente o canal R classifica o tile:

    0x01 -> walkable
    0x05 -> bloqueado (parede/agua)

Valores desconhecidos sao tratados como ``None`` para o chamador decidir.
"""

from __future__ import annotations

import logging
import os
import struct
import zlib
from collections import OrderedDict
from pathlib import Path
from typing import Dict, Optional, Tuple

log = logging.getLogger("pokebot.core.minimap_map")

OTMM_SIGNATURE = b"OTMM"
OTMM_VERSION_HEADER = b"OTMM 1.0"
HEADER_SIZE = 22
BLOCK_SIZE = 64
BLOCK_BYTES = BLOCK_SIZE * BLOCK_SIZE * 3
BLOCK_HEADER_SIZE = 7
WALKABLE_RED = 0x01
BLOCKED_RED = 0x05
MAX_CACHED_BLOCKS = 12
ZLIB_HEADERS = (b"\x78\x5e", b"\x78\x9c", b"\x78\x01", b"\x78\xda")
IndexKey = Tuple[int, int, int]


def minimap_directory() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return Path.home() / "AppData" / "Roaming" / "PokeAlliance" / "PokeAllianceV3"
    return Path(appdata) / "PokeAlliance" / "PokeAllianceV3"


def find_latest_minimap(folder: Optional[Path] = None) -> Optional[Path]:
    """Retorna o ``minimap*.otmm`` mais recente da pasta do cliente."""
    folder = Path(folder) if folder is not None else minimap_directory()
    try:
        files = [p for p in folder.glob("minimap*.otmm") if p.is_file()]
    except OSError:
        return None
    if not files:
        return None
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0]


class OtmmMinimap:
    """Indice de blocos OTMM com descompressao sob demanda e cache LRU."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._index: Dict[IndexKey, Tuple[int, int]] = {}
        self._cache: "OrderedDict[IndexKey, bytes]" = OrderedDict()
        self._mtime = 0.0
        self._size = -1
        self.load()

    @classmethod
    def load_latest(cls, folder: Optional[Path] = None) -> Optional["OtmmMinimap"]:
        path = find_latest_minimap(folder)
        if path is None:
            return None
        try:
            return cls(path)
        except Exception as exc:  # noqa: BLE001 - arquivo pode estar sendo escrito
            log.warning("Falha ao ler minimapa %s: %s", path, exc)
            return None

    # -- carregamento ----------------------------------------------------
    def load(self) -> None:
        stat = self.path.stat()
        self._mtime = stat.st_mtime
        self._size = stat.st_size
        data = self.path.read_bytes()
        if not data.startswith(OTMM_SIGNATURE):
            raise ValueError("assinatura OTMM invalida")
        if data[14:22] != OTMM_VERSION_HEADER:
            raise ValueError("cabecalho OTMM 1.0 ausente")

        index: Dict[IndexKey, Tuple[int, int]] = {}
        position = HEADER_SIZE
        total = len(data)
        while position + BLOCK_HEADER_SIZE <= total:
            x, y = struct.unpack_from("<HH", data, position)
            z = data[position + 4]
            start = position + BLOCK_HEADER_SIZE
            if data[start:start + 2] not in ZLIB_HEADERS:
                break
            chunk = data[start:start + 65536]
            decompressor = zlib.decompressobj()
            try:
                raw = decompressor.decompress(chunk)
            except zlib.error as exc:
                log.warning("Bloco OTMM invalido em %d: %s", start, exc)
                break
            if len(raw) != BLOCK_BYTES:
                log.warning("Bloco OTMM com %d bytes (esperado %d) em %s",
                            len(raw), BLOCK_BYTES, self.path)
                break
            consumed = len(chunk) - len(decompressor.unused_data)
            if consumed <= 0:
                break
            index[(x, y, z)] = (start, consumed)
            position = start + consumed

        if not index:
            raise ValueError("nenhum bloco OTMM encontrado")
        self._index = index
        self._cache.clear()
        log.info("Minimapa OTMM carregado: %s (%d blocos)", self.path.name, len(index))

    def refresh_if_changed(self) -> bool:
        try:
            stat = self.path.stat()
        except OSError:
            return False
        if stat.st_mtime == self._mtime and stat.st_size == self._size:
            return False
        try:
            self.load()
        except Exception as exc:  # noqa: BLE001 - cliente pode estar gravando
            log.debug("Minimapa ainda sendo atualizado: %s", exc)
            return False
        return True

    # -- consulta --------------------------------------------------------
    def _block(self, origin: IndexKey) -> Optional[bytes]:
        cached = self._cache.get(origin)
        if cached is not None:
            self._cache.move_to_end(origin)
            return cached
        entry = self._index.get(origin)
        if entry is None:
            return None
        start, consumed = entry
        with self.path.open("rb") as stream:
            stream.seek(start)
            compressed = stream.read(consumed)
        try:
            block = zlib.decompress(compressed)
        except zlib.error:
            return None
        if len(block) != BLOCK_BYTES:
            return None
        self._cache[origin] = block
        if len(self._cache) > MAX_CACHED_BLOCKS:
            self._cache.popitem(last=False)
        return block

    def tile_rgb(self, x: int, y: int, z: int) -> Optional[Tuple[int, int, int]]:
        ox, oy = (int(x) // BLOCK_SIZE) * BLOCK_SIZE, (int(y) // BLOCK_SIZE) * BLOCK_SIZE
        block = self._block((ox, oy, int(z)))
        if block is None:
            return None
        offset = (((int(y) - oy) * BLOCK_SIZE) + (int(x) - ox)) * 3
        if offset < 0 or offset + 3 > len(block):
            return None
        return block[offset], block[offset + 1], block[offset + 2]

    def walkable(self, x: int, y: int, z: int) -> Optional[bool]:
        rgb = self.tile_rgb(x, y, z)
        if rgb is None:
            return None
        if rgb[0] == WALKABLE_RED:
            return True
        if rgb[0] == BLOCKED_RED:
            return False
        return None

    @property
    def block_count(self) -> int:
        return len(self._index)


__all__ = [
    "OtmmMinimap",
    "find_latest_minimap",
    "minimap_directory",
    "BLOCK_SIZE",
    "WALKABLE_RED",
    "BLOCKED_RED",
]
