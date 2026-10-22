# pokebot/core/best_tile.py
"""Selecao de best tile para o Perfect Lure.

Escolhe um tile walkable perto do player com espaco aberto ao redor e com
distancia minima de paredes/obstaculos. A fonte de walkability e plugavel:
normalmente ``core.minimap_map.OtmmMinimap``. Tiles desconhecidos contam como
bloqueados para nao posicionar o personagem em um canto nao explorado.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence


@dataclass(frozen=True)
class BestTile:
    x: int
    y: int
    z: int
    wall_distance: int
    open_3x3: int
    open_5x5: int
    monster_count: int
    distance: int
    reason: str = ""


class BestTileSelector:
    """Escolhe o melhor tile dentro de um raio, priorizando espaco aberto."""

    def __init__(self, map_reader) -> None:
        self.map_reader = map_reader

    def _walkable(self, x: int, y: int, z: int) -> Optional[bool]:
        try:
            return self.map_reader.walkable(x, y, z)
        except Exception:  # noqa: BLE001 - leitura de arquivo pode falhar
            return None

    def select(
        self,
        player: Optional[Sequence[int]],
        monsters: Iterable[dict] = (),
        *,
        radius: int = 4,
        wall_margin: int = 3,
    ) -> Optional[BestTile]:
        if player is None or len(player) < 2:
            return None
        px, py = int(player[0]), int(player[1])
        pz = int(player[2]) if len(player) > 2 and player[2] is not None else 0
        radius = max(1, int(radius))
        wall_margin = max(1, int(wall_margin))
        scan = radius + wall_margin + 2
        mobs = [m for m in (monsters or ())
                if m.get("x") is not None and m.get("y") is not None
                and int(m.get("z", pz) or pz) == pz]

        # Mapa local em cache para nao repetir leituras de bloco.
        local = {}

        def walkable(x: int, y: int) -> Optional[bool]:
            key = (x, y)
            if key not in local:
                local[key] = self._walkable(x, y, pz)
            return local[key]

        def wall_distance(cx: int, cy: int) -> int:
            best = scan
            for dy in range(-scan, scan + 1):
                for dx in range(-scan, scan + 1):
                    distance = max(abs(dx), abs(dy))
                    if distance >= best:
                        continue
                    if walkable(cx + dx, cy + dy) is not True:
                        best = distance
            return best

        def open_count(cx: int, cy: int, size: int) -> int:
            count = 0
            for dy in range(-size, size + 1):
                for dx in range(-size, size + 1):
                    if walkable(cx + dx, cy + dy) is True:
                        count += 1
            return count

        def monsters_near(cx: int, cy: int) -> int:
            count = 0
            for mob in mobs:
                try:
                    if max(abs(int(mob["x"]) - cx), abs(int(mob["y"]) - cy)) <= 5:
                        count += 1
                except (TypeError, ValueError, KeyError):
                    continue
            return count

        candidates = []
        fallback = []
        for dy in range(-radius, radius + 1):
            for dx in range(-radius, radius + 1):
                cx, cy = px + dx, py + dy
                if walkable(cx, cy) is not True:
                    continue
                distance = max(abs(dx), abs(dy))
                wall = wall_distance(cx, cy)
                open3 = open_count(cx, cy, 1)
                open5 = open_count(cx, cy, 2)
                mobs_near = monsters_near(cx, cy)
                tile = BestTile(
                    x=cx, y=cy, z=pz, wall_distance=wall,
                    open_3x3=open3, open_5x5=open5,
                    monster_count=mobs_near, distance=distance,
                )
                fallback.append(tile)
                if wall >= wall_margin and open3 >= 6:
                    candidates.append(tile)

        if not candidates:
            # Sem tile seguro: devolve o menos ruim para o controlador decidir.
            if not fallback:
                return None
            best = max(fallback, key=lambda t: (
                t.wall_distance, t.open_5x5, t.open_3x3, t.monster_count, -t.distance))
            return BestTile(**{**best.__dict__, "reason": "sem margem minima"})

        best = max(candidates, key=lambda t: (
            min(t.wall_distance, wall_margin + 2),
            t.open_5x5,
            t.open_3x3,
            t.monster_count,
            -t.distance,
        ))
        return BestTile(**{**best.__dict__, "reason": "ok"})


__all__ = ["BestTile", "BestTileSelector"]
