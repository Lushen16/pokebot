# pokebot/core/chat_snapshot.py
"""Leitura do snapshot JSON de alertas que o cliente expõe ao PKAengine.

O cliente mantém cópias do JSON em buffers privados. A cópia canônica avança
o campo ``tick`` a cada ~500 ms e contém ``alertEvents`` com o chat recente.
A varredura completa é feita uma vez; depois só os candidatos conhecidos são
relidos. Se o tick parar de avançar, uma nova varredura completa é agendada.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Iterable

SNAPSHOT_MARKER = b'{"schema":2,"tick":'
_TICK_RE = re.compile(rb'"tick":(\d+)')
_MAX_SNAPSHOT_BYTES = 262144
_MAX_CANDIDATES = 32
_SCAN_CHUNK = 1024 * 1024
_TRADE_MODE = 7
_MONSTER_MODE = 44


@dataclass(frozen=True)
class ChatEvent:
    id: int
    mode: int
    name: str
    text: str
    self: bool
    pos: Any = None

    @property
    def is_trade(self) -> bool:
        return self.mode == _TRADE_MODE

    @property
    def is_monster(self) -> bool:
        return self.mode == _MONSTER_MODE

    @property
    def is_creature(self) -> bool:
        return 36 <= self.mode <= 44


def parse_chat_events(snapshot: dict) -> list[ChatEvent]:
    """Extrai somente eventos ``kind == "talk"`` válidos do snapshot."""
    events: list[ChatEvent] = []
    raw_events = snapshot.get("alertEvents")
    if not isinstance(raw_events, list):
        return events
    for raw in raw_events:
        if not isinstance(raw, dict) or raw.get("kind") != "talk":
            continue
        try:
            event_id = int(raw.get("id"))
            mode = int(raw.get("mode", 1))
        except (TypeError, ValueError):
            continue
        name = str(raw.get("name") or "").strip()
        text = str(raw.get("text") or "").strip()
        if not name or not text:
            continue
        events.append(ChatEvent(
            id=event_id,
            mode=mode,
            name=name,
            text=text,
            self=bool(raw.get("self", False)),
            pos=raw.get("pos"),
        ))
    return events


def parse_snapshot(raw: bytes) -> dict | None:
    """Converte um buffer NUL-terminado em dict; None se inválido."""
    if not raw:
        return None
    end = raw.find(b"\x00")
    if end >= 0:
        raw = raw[:end]
    try:
        obj = json.loads(raw.decode("utf-8", errors="replace"))
    except (TypeError, ValueError, UnicodeError):
        return None
    return obj if isinstance(obj, dict) else None


def _tick_from_raw(raw: bytes) -> int | None:
    match = _TICK_RE.search(raw)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


class _MemoryBasicInformation(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_ulonglong),
        ("AllocationBase", ctypes.c_ulonglong),
        ("AllocationProtect", wt.DWORD),
        ("__alignment1", wt.DWORD),
        ("RegionSize", ctypes.c_ulonglong),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
        ("__alignment2", wt.DWORD),
    ]


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.VirtualQueryEx.argtypes = [wt.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
_kernel32.VirtualQueryEx.restype = ctypes.c_size_t


def _process_handle(proc) -> int | None:
    pm = getattr(proc, "pm", None)
    handle = getattr(pm, "process_handle", None)
    if handle:
        return int(handle)
    handle = getattr(proc, "handle", None)
    return int(handle) if handle else None


def _iter_private_rw_regions(proc) -> Iterable[tuple[int, int]]:
    handle = _process_handle(proc)
    if not handle:
        return ()
    regions = []
    address = 0
    max_address = 0x7FFFFFFFFFFF
    while address < max_address:
        info = _MemoryBasicInformation()
        result = _kernel32.VirtualQueryEx(
            handle, ctypes.c_void_p(address), ctypes.byref(info), ctypes.sizeof(info)
        )
        if result == 0:
            break
        base, size = int(info.BaseAddress), int(info.RegionSize)
        if not size:
            break
        if (info.State == 0x1000 and info.Type == 0x20000
                and not (info.Protect & (0x100 | 0x01))
                and (info.Protect & (0x04 | 0x08 | 0x40 | 0x80))):
            regions.append((base, size))
        address = base + size
    return regions


def _read_bytes(proc, address: int, size: int) -> bytes:
    try:
        data = proc.read_bytes(address, size)
    except Exception:
        return b""
    return data or b""


def _read_cstring(proc, address: int, limit: int = _MAX_SNAPSHOT_BYTES,
                  chunk_size: int = 4096) -> bytes:
    """Le um buffer NUL-terminado em blocos pequenos.

    Uma leitura unica de ``limit`` bytes pode atravessar o fim da regiao e
    falhar inteira (ReadProcessMemory exige o range completo). O snapshot vive
    em uma regiao privada cujo tamanho costuma ser menor que 256 KiB, por isso
    lemos em chunks ate o NUL ou ate a leitura falhar.
    """
    out = bytearray()
    step = max(1, int(chunk_size))
    while len(out) < limit:
        chunk = _read_bytes(proc, address + len(out), min(step, limit - len(out)))
        if not chunk:
            break
        end = chunk.find(b"\x00")
        if end >= 0:
            out.extend(chunk[:end])
            break
        out.extend(chunk)
    return bytes(out)


def find_snapshot_candidates(proc, limit: int = _MAX_CANDIDATES) -> list[tuple[int, int]]:
    """Varre a memória privada e devolve ``(tick, endereço)`` dos candidatos."""
    hits: list[tuple[int, int]] = []
    for base, size in _iter_private_rw_regions(proc):
        offset = 0
        while offset < size:
            length = min(_SCAN_CHUNK, size - offset)
            chunk = _read_bytes(proc, base + offset, length)
            if chunk:
                start = 0
                while True:
                    index = chunk.find(SNAPSHOT_MARKER, start)
                    if index < 0:
                        break
                    address = base + offset + index
                    tick = _tick_from_raw(chunk[index:index + 256])
                    if tick is not None:
                        hits.append((tick, address))
                    start = index + 1
            offset += length
    hits.sort(reverse=True)
    return hits[:limit]


class ChatSnapshotReader:
    """Mantém o último snapshot conhecido e devolve eventos de chat novos."""

    def __init__(self, proc, *, full_scan_interval: float = 10.0,
                 max_candidates: int = _MAX_CANDIDATES,
                 preferred_address: int | None = None):
        self.proc = proc
        self.full_scan_interval = max(1.0, float(full_scan_interval))
        self.max_candidates = max(1, int(max_candidates))
        self._candidates: list[int] = []
        self._address = int(preferred_address or 0)
        self._preferred = int(preferred_address or 0)
        self._last_tick = -1
        self._last_event_id = 0
        self._last_progress = 0.0
        self._last_full_scan = 0.0
        self._connected = False
        self._last_error: str | None = None
        self._event_count = 0
        self._dropped = 0
        self._revision = 0
        self._stall_scans = 0
        self._scan_interval = self.full_scan_interval

    def poll(self) -> list[ChatEvent]:
        """Retorna eventos com ``id`` maior que o último já entregue."""
        if self.proc is None:
            return []
        now = time.monotonic()
        if not self._candidates:
            self._full_scan(now)
        else:
            tick, _address = self._best_candidate()
            if tick is None:
                self._full_scan(now)
            elif (tick <= self._last_tick
                  and now - self._last_progress > self._scan_interval):
                scanned_tick = self._full_scan(now)
                if scanned_tick is not None and scanned_tick <= self._last_tick:
                    self._stall_scans += 1
                    self._scan_interval = min(self.full_scan_interval * (2 ** self._stall_scans), 300.0)

        tick, address = self._best_candidate()
        if tick is None:
            self._last_error = ("snapshot indisponível" if self._candidates
                                else "nenhum snapshot de alertas encontrado")
            return []
        if tick <= self._last_tick:
            self._last_error = None
            return []
        snapshot = self._read_snapshot(address)
        if snapshot is None:
            self._last_error = "snapshot inválido"
            return []
        self._address = address
        self._preferred = address
        self._last_tick = tick
        self._last_progress = now
        self._last_error = None
        self._stall_scans = 0
        self._scan_interval = self.full_scan_interval
        self._revision = int(snapshot.get("alertEventRevision") or 0)
        self._dropped = int(snapshot.get("alertEventsDropped") or 0)
        events = parse_chat_events(snapshot)
        self._event_count = len(events)
        if not self._connected:
            self._connected = True
            self._last_event_id = max((event.id for event in events), default=0)
            return []
        fresh = [event for event in events if event.id > self._last_event_id]
        if fresh:
            self._last_event_id = max(event.id for event in fresh)
        return fresh

    def _full_scan(self, now: float) -> int | None:
        found = find_snapshot_candidates(self.proc, self.max_candidates)
        self._last_full_scan = now
        self._candidates = [address for _tick, address in found]
        if found:
            tick, address = found[0]
            self._address = address
            self._last_progress = now
            self._last_error = None
            return tick
        self._candidates = []
        self._last_error = "nenhum snapshot de alertas encontrado"
        return None

    def _best_candidate(self) -> tuple[int | None, int]:
        best_tick: int | None = None
        best_address = 0
        addresses: list[int] = []
        for candidate in (self._preferred, self._address, *self._candidates):
            if candidate and candidate not in addresses:
                addresses.append(candidate)
        for address in addresses:
            raw = _read_bytes(self.proc, address, 256)
            tick = _tick_from_raw(raw)
            if tick is None:
                continue
            if best_tick is None or tick > best_tick:
                best_tick, best_address = tick, address
        return best_tick, best_address

    def _read_snapshot(self, address: int) -> dict | None:
        return parse_snapshot(_read_cstring(self.proc, address))

    @property
    def status(self) -> dict:
        return {
            "connected": self._connected,
            "tick": self._last_tick,
            "address": self._address,
            "events": self._event_count,
            "revision": self._revision,
            "dropped": self._dropped,
            "last_error": self._last_error,
            "candidates": len(self._candidates),
            "last_full_scan": self._last_full_scan,
        }
