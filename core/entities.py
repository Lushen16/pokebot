"""Leitura de entidades (criaturas) do cliente.

Duas estrategias combinadas:

  * **Fast path**: usa os objetos validos do registry de criaturas
    (`world.entity_list_ptr`/`world.entity_list_end`, resolvidos por
    assinatura) como sementes para descobrir as arenas heap que contem
    entidades vivas; escaneia apenas essas arenas (~0.06s).
  * **Deep scan**: varre todas as regioes legiveis (~130s), usado sob
    demanda; as arenas descobertas sao memorizadas e reaproveitadas pelo
    fast path nas proximas leituras.

O layout da entidade (valido no player e nas criaturas):

    anchor        = endereco do campo X
    x @ anchor+0x00 (int32)
    y @ anchor+0x04 (int32)
    z @ anchor+0x08 (uint8)
    nome ASCII NUL-terminated @ anchor+0x50
    byte discriminador NUL @ anchor+0x4F
    flags de criatura @ anchor+0x150 (uint32; bit 0x100 = shiny/estrela/boss)

O flag em `anchor+0x150` e a fonte confiavel de deteccao do shiny; nao usar
cor do nome (azul/rosa) como criterio.

No registry cada slot tem 0x10 bytes `{objeto, objeto-0x10}` e o anchor fica em
`objeto + 0x20`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

log = logging.getLogger("pokebot.core.entities")

HANDLE_STRIDE = 0x10
OBJECT_TO_ANCHOR = 0x20
ANCHOR_MIN = 0x10000
DEFAULT_RADIUS = 40
SHINY_FLAG_OFFSET = 0x150
SHINY_FLAG_MASK = 0x100
CREATURE_ID_OFFSET = 0x2C
HP_PERCENT_OFFSET = 0xE78


@dataclass(frozen=True)
class Entity:
    """Criatura vista na memoria (campos preenchidos conforme offsets)."""

    name: str
    x: int
    y: int
    z: int = 0
    creature_id: Optional[int] = None
    hp_percent: int = 100
    is_npc: bool = False
    is_player: bool = False
    is_dead: bool = False
    shiny: bool = False
    anchor: Optional[int] = None
    distance: int = 0


def _default_scanner(memory, regions, px, py, radius):
    from core.tools.scan_entities import find_entities

    return find_entities(memory, regions, px, py, radius)


class EntityReader:
    """Le entidades combinando sementes do registry e scan de arenas."""

    def __init__(
        self,
        memory,
        *,
        registry_provider: Optional[Callable[[], Sequence[int]]] = None,
        regions_provider: Optional[Callable[[], Sequence[Tuple[int, int]]]] = None,
        scanner: Optional[Callable[..., List[dict]]] = None,
        radius: int = DEFAULT_RADIUS,
    ) -> None:
        if int(radius) < 0:
            raise ValueError("radius deve ser >= 0")
        self.memory = memory
        self.radius = int(radius)
        self._registry_provider = registry_provider or (lambda: [])
        self._regions_provider = regions_provider
        self._scanner = scanner or _default_scanner
        self._arena_regions = set()
        self._all_regions: Optional[List[Tuple[int, int]]] = None
        self._seeded = False

    # -- sementes ---------------------------------------------------------
    def registry_anchors(self) -> List[int]:
        try:
            return [int(a) for a in self._registry_provider() if a]
        except Exception as exc:  # noqa: BLE001 - provider pode falhar
            log.warning("registry_anchors falhou: %s", exc)
            return []

    def all_regions(self) -> List[Tuple[int, int]]:
        if self._all_regions is None:
            if self._regions_provider is None:
                self._all_regions = []
            else:
                self._all_regions = list(self._regions_provider())
        return self._all_regions

    def seed_arenas(self) -> set:
        """Memoriza as regioes que contem os anchors do registry."""
        regions = self.all_regions()
        for anchor in self.registry_anchors():
            for base, size in regions:
                if base <= anchor < base + size:
                    self._arena_regions.add((base, size))
                    break
        return set(self._arena_regions)

    def active_regions(self, deep: bool = False) -> List[Tuple[int, int]]:
        if deep:
            return list(self.all_regions())
        if not self._seeded:
            self.seed_arenas()
            self._seeded = True
        return list(self._arena_regions) if self._arena_regions else list(self.all_regions())

    # -- leitura ----------------------------------------------------------
    def _shiny_flag(self, anchor) -> bool:
        if anchor is None:
            return False
        reader = getattr(self.memory, "read_bytes", None)
        if reader is None:
            return False
        try:
            raw = reader(int(anchor) + SHINY_FLAG_OFFSET, 4)
        except Exception:  # noqa: BLE001 - leitura ao vivo pode falhar
            return False
        if not raw or len(raw) < 4:
            return False
        return bool(int.from_bytes(raw, "little") & SHINY_FLAG_MASK)

    def _u32_at(self, anchor, offset: int) -> Optional[int]:
        if anchor is None:
            return None
        reader = getattr(self.memory, "read_bytes", None)
        if reader is None:
            return None
        try:
            raw = reader(int(anchor) + offset, 4)
        except Exception:  # noqa: BLE001 - leitura ao vivo pode falhar
            return None
        if not raw or len(raw) < 4:
            return None
        return int.from_bytes(raw, "little", signed=False)

    def _hp_percent(self, anchor) -> int:
        raw = self._u32_at(anchor, HP_PERCENT_OFFSET)
        if raw is None or raw > 100:
            return 100
        return raw

    def _creature_id(self, anchor) -> Optional[int]:
        return self._u32_at(anchor, CREATURE_ID_OFFSET)

    def _to_entities(self, hits: Iterable[dict]) -> List[Entity]:
        result = []
        for hit in hits:
            anchor = hit.get("anchor")
            result.append(Entity(
                name=hit.get("name", ""),
                x=int(hit["x"]),
                y=int(hit["y"]),
                z=int(hit.get("z", 0)),
                creature_id=self._creature_id(anchor),
                hp_percent=self._hp_percent(anchor),
                shiny=self._shiny_flag(anchor),
                anchor=anchor,
                distance=int(hit.get("distance", 0)),
            ))
        return result

    def scan(self, px: int, py: int, radius: Optional[int] = None, *,
             deep: bool = False) -> List[Entity]:
        limit = self.radius if radius is None else int(radius)
        hits = self._scanner(self.memory, self.active_regions(deep=deep), px, py, limit)
        return self._to_entities(hits)

    def deep_scan(self, px: int, py: int, radius: Optional[int] = None) -> List[Entity]:
        """Varre tudo e aprende as arenas onde entidades foram encontradas."""
        limit = self.radius if radius is None else int(radius)
        regions = self.all_regions()
        hits = self._scanner(self.memory, regions, px, py, limit)
        for hit in hits:
            anchor = hit.get("anchor")
            if anchor is None:
                continue
            for base, size in regions:
                if base <= anchor < base + size:
                    self._arena_regions.add((base, size))
                    break
        return self._to_entities(hits)

    # -- fatorias ---------------------------------------------------------
    @classmethod
    def from_offsets(cls, memory, offsets, *, radius: int = DEFAULT_RADIUS,
                     scanner: Optional[Callable[..., List[dict]]] = None) -> "EntityReader":
        """Constroi um reader ligado a offsets.json (stack core)."""

        def registry_provider() -> List[int]:
            find = getattr(offsets, "find", None)
            if find is None:
                return []
            begin_entry = find("world.entity_list_ptr")
            end_entry = find("world.entity_list_end")
            if not begin_entry or not end_entry:
                return []
            if not begin_entry.is_resolved or not end_entry.is_resolved:
                return []
            begin = memory.resolve_entry_address(begin_entry.to_reader_dict())
            end = memory.resolve_entry_address(end_entry.to_reader_dict())
            if not begin or not end or end <= begin:
                return []
            anchors = []
            for slot in range(begin, end, HANDLE_STRIDE):
                base = memory.read_ptr(slot)
                if base and base > ANCHOR_MIN:
                    anchors.append(base + OBJECT_TO_ANCHOR)
            return anchors

        def regions_provider() -> List[Tuple[int, int]]:
            from core.tools.scan_value import iter_regions

            return list(iter_regions(memory))

        return cls(memory, registry_provider=registry_provider,
                   regions_provider=regions_provider, scanner=scanner, radius=radius)


__all__ = ["Entity", "EntityReader", "HANDLE_STRIDE", "OBJECT_TO_ANCHOR",
           "DEFAULT_RADIUS"]
