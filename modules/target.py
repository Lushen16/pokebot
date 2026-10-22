# pokebot/modules/target.py
"""Modulo Target (esqueleto operacional).

Loop tick() que le a lista de criaturas, calcula a distancia de Chebyshev
ate o player e loga o alvo mais proximo dentro do alcance de ataque.

A fonte de entidades e plugavel via `entity_reader` (callable sem argumentos
que retorna um iteravel de Entity). Enquanto os offsets da Entity List
(creature_list.* e world.entity_list_ptr/entity_count) nao estao calibrados,
o modulo nao dispara acao alguma: apenas avisa em log UMA vez.

Exemplo de uso ao vivo: python -m modules.target --process PokeAlliance_dx.exe
"""

from __future__ import annotations

import argparse
import logging
import math
import time
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

from core.entities import DEFAULT_RADIUS, Entity, EntityReader
from modules.base import AutomationModule
from core.hotkeys import resolve_key, split_hotkey

log = logging.getLogger("pokebot.modules.target")

DEFAULT_ATTACK_RANGE = 1
TARGET_LOG = "Alvo encontrado: {name} em ({x},{y}) | Distância: {distance} tiles"
MAX_DELAY_MS = 1000
PERFECT_LURE_POLL_S = 0.1

HOTKEY_VK = {
    "shift": 0x10, "ctrl": 0x11, "alt": 0x12,
    "space": 0x20, "tab": 0x09, "enter": 0x0D, "esc": 0x1B,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
}


def parse_hotkey(text: str) -> list[int]:
    """Converte 'Shift+F' / 'F5' em codigos VK. Retorna [] se invalido."""
    if not text or not isinstance(text, str):
        return []
    codes: list[int] = []
    for part in split_hotkey(text):
        if not part:
            continue
        try:
            codes.append(resolve_key(part))
        except ValueError:
            return []
    return codes


def is_shiny_name(name: str) -> bool:
    """Fallback: alguns nomes expoem "shiny" explicitamente (ex.: "Shiny Bala").

    A deteccao principal usa o flag de criatura (@ +0x150, bit 0x100), porque
    criaturas shiny do mapa aparecem com o nome base (ex.: "Rattata").
    """
    return bool(name) and "shiny" in str(name).lower()


class CombatState(str, Enum):
    LURING = "luring"
    COMBATING = "combating"
    REVIVING = "reviving"
    POST_COMBAT = "post_combat"


class CombatController:
    """Maquina de estados do lure: para o walker, comba, revive e retoma."""

    def __init__(
        self,
        *,
        entities_provider: Callable[[], Sequence[dict]],
        player_provider: Callable[[], Optional[tuple[int, int]]],
        press_combo: Callable[[Sequence[int], str], bool],
        send_text: Optional[Callable[[str], bool]] = None,
        player_hp_provider: Optional[Callable[[], Optional[int]]] = None,
        walker=None,
        stop_event=None,
        lure_target_count: int = 8,
        lure_radius: int = 7,
        perfect_lure: bool = True,
        perfect_lure_distance: int = 3,
        perfect_lure_wait_ms: int = 1000,
        attack_range: int = 7,
        combo_hotkey: str = "F5",
        combo_command: str = "",
        focus_shiny: bool = True,
        revive_hotkey: str = "Shift+F",
        revive_delay_ms: int = 800,
        combo_wait_ms: int = 1000,
        max_combat_loops: int = 5,
        post_combat_revive: bool = True,
        prioritize: str = "nearest",
        blacklist: Iterable[str] = (),
        own_names: Iterable[str] = (),
    ) -> None:
        self.entities_provider = entities_provider
        self.player_provider = player_provider
        self.press_combo = press_combo
        self.send_text = send_text
        self.player_hp_provider = player_hp_provider
        self.walker = walker
        self.stop_event = stop_event
        self.lure_target_count = max(0, int(lure_target_count))
        self.lure_radius = max(0, int(lure_radius))
        self.perfect_lure = bool(perfect_lure)
        self.perfect_lure_distance = max(0, int(perfect_lure_distance))
        self.perfect_lure_wait = min(MAX_DELAY_MS, max(0, int(perfect_lure_wait_ms))) / 1000.0
        self.attack_range = max(0, int(attack_range))
        self.combo_codes = parse_hotkey(combo_hotkey)
        self.combo_command = (combo_command or "").strip()
        self.focus_shiny = bool(focus_shiny)
        self.revive_codes = parse_hotkey(revive_hotkey)
        self.revive_delay = min(MAX_DELAY_MS, max(0, int(revive_delay_ms))) / 1000.0
        self.combo_wait = min(MAX_DELAY_MS, max(0, int(combo_wait_ms))) / 1000.0
        self.max_combat_loops = max(1, int(max_combat_loops))
        self.post_combat_revive = bool(post_combat_revive)
        self.prioritize = prioritize
        self.blacklist = {str(n).lower() for n in blacklist if n}
        self.own_names = {str(n).lower() for n in own_names if n}
        self.state = CombatState.LURING
        self.last_target: Optional[dict] = None
        self._interrupted = False
        self._last_lure_enemies: list[dict] = []
        self._last_close_count = 0
        self._last_lure_log = 0.0

    # -- utilidades ------------------------------------------------------
    def _wait(self, seconds: float) -> bool:
        """Espera interrompivel; retorna False se o bot foi parado."""
        if seconds <= 0:
            return not self._stopped()
        if self.stop_event is not None:
            return not self.stop_event.wait(seconds)
        time.sleep(seconds)
        return not self._stopped()

    def _stopped(self) -> bool:
        return self.stop_event is not None and self.stop_event.is_set()

    def _set_state(self, state: CombatState) -> None:
        if self.state != state:
            self.state = state
            log.debug("CombatState -> %s", state.value)

    def _send_combo(self, target: dict) -> bool:
        if self.combo_command and self.send_text is not None:
            try:
                return bool(self.send_text(self.combo_command))
            except Exception as exc:  # noqa: BLE001
                log.warning("Falha ao enviar combo '%s': %s", self.combo_command, exc)
                return False
        if not self.combo_codes:
            log.warning("Combo hotkey invalida; nada enviado")
            return False
        return bool(self.press_combo(self.combo_codes, "combo"))

    def _send_revive(self) -> bool:
        if not self.revive_codes:
            log.warning("Revive hotkey invalida; nada enviado")
            return False
        return bool(self.press_combo(self.revive_codes, "revive"))

    def enemies(self, entities: Sequence[dict], player: Optional[tuple[int, int]],
                radius: Optional[int] = None) -> list[dict]:
        limit = self.lure_radius if radius is None else int(radius)
        px, py = player if player else (None, None)
        result = []
        for creature in entities or []:
            if creature.get("is_player") or creature.get("is_npc") or creature.get("is_dead"):
                continue
            if int(creature.get("hp_percent", 100) or 0) <= 0:
                continue
            name = str(creature.get("name") or "").lower()
            if name in self.blacklist or name in self.own_names:
                continue
            shiny = bool(creature.get("shiny")) or is_shiny_name(name)
            if px is not None and py is not None:
                distance = max(abs(int(creature.get("x", px)) - px),
                               abs(int(creature.get("y", py)) - py))
                if distance > limit:
                    continue
            else:
                distance = int(creature.get("distance", 0) or 0)
            result.append({**creature, "distance": distance, "shiny": shiny})
        return result

    def pick_target(self, enemies: Sequence[dict]) -> Optional[dict]:
        if not enemies:
            return None
        if self.focus_shiny:
            shinies = [e for e in enemies if e.get("shiny")]
            if shinies:
                return min(shinies, key=lambda e: (e["distance"], -int(e.get("hp_percent", 100))))
        if self.prioritize == "lowest_hp_percent":
            return min(enemies, key=lambda e: (int(e.get("hp_percent", 100)), e["distance"]))
        return min(enemies, key=lambda e: (e["distance"], int(e.get("hp_percent", 100))))

    def should_lure(self, entities: Sequence[dict], player: Optional[tuple[int, int]]) -> bool:
        """Perfect Lure: exige `lure_target_count` bichos dentro do raio normal
        E `lure_target_count` deles a `perfect_lure_distance` SQM do player."""
        self._last_lure_enemies = []
        self._last_close_count = 0
        if self.lure_target_count <= 0:
            return False
        enemies = self.enemies(entities, player)
        self._last_lure_enemies = enemies
        if self.perfect_lure:
            self._last_close_count = sum(
                1 for e in enemies if e["distance"] <= self.perfect_lure_distance)
        else:
            self._last_close_count = len(enemies)
        if len(enemies) < self.lure_target_count:
            return False
        return self._last_close_count >= self.lure_target_count

    def _log_lure_wait(self) -> None:
        """Loga (no maximo 1x/s) o motivo de ainda nao combar."""
        if not self.perfect_lure or self.lure_target_count <= 0:
            return
        now = time.monotonic()
        if now - self._last_lure_log < 1.0:
            return
        self._last_lure_log = now
        log.info("PERFECT LURE: %d/%d bichos a <= %d SQM (detectados no raio %d: %d)",
                 self._last_close_count, self.lure_target_count,
                 self.perfect_lure_distance, self.lure_radius,
                 len(self._last_lure_enemies))

    def _wait_for_gather(self, player: Optional[tuple[int, int]]) -> bool:
        """Juntou a quantidade no raio? Freia o walker e espera aproximar.

        Retorna True quando os bichos chegaram a `perfect_lure_distance`; se
        nao aglomerar dentro de `perfect_lure_wait`, solta o walker pra voltar
        a andar (evita ficar parado pra sempre).
        """
        if not self.perfect_lure or self.walker is None:
            return False
        if len(self._last_lure_enemies) < self.lure_target_count:
            return False
        self._set_state(CombatState.LURING)
        self.walker.pause_for_combat()
        deadline = time.monotonic() + self.perfect_lure_wait
        while time.monotonic() < deadline:
            if not self._wait(PERFECT_LURE_POLL_S):
                break
            entities = list(self.entities_provider() or [])
            if self.should_lure(entities, self.player_provider() or player):
                log.info("PERFECT LURE pronto: %d bichos a <= %d SQM",
                         self._last_close_count, self.perfect_lure_distance)
                return True
        self.walker.resume_route()
        return False

    # -- loop principal --------------------------------------------------
    def run(self, entities: Optional[Sequence[dict]] = None,
            player: Optional[tuple[int, int]] = None) -> bool:
        if (self.lure_target_count <= 0 and not self.focus_shiny) or self._stopped():
            return False
        player = player or self.player_provider()
        if entities is None:
            entities = self.entities_provider()
        entities = list(entities or [])
        radius = self.attack_range if self.focus_shiny else None
        if self.focus_shiny:
            # Foco em shiny e exclusivo: nao espera agrupamento nem cai para
            # monstros comuns. Um unico shiny ja inicia o combate.
            if not self.enemies(entities, player, radius=radius):
                return False
        elif not self.should_lure(entities, player):
            if not self._wait_for_gather(player):
                self._log_lure_wait()
                return False
            player = self.player_provider() or player
            entities = list(self.entities_provider() or [])
        if self._stopped():
            return False

        if self.walker is not None:
            self.walker.pause_for_combat()
        self._interrupted = False
        try:
            for _ in range(self.max_combat_loops):
                if self._stopped():
                    self._interrupted = True
                    break
                target = self.pick_target(self.enemies(entities, player, radius=radius))
                if target is None:
                    break
                self.last_target = target
                self._set_state(CombatState.COMBATING)
                if target.get("shiny"):
                    log.info("SHINY DETECTADO: focando %s em (%s,%s)",
                             target.get("name"), target.get("x"), target.get("y"))
                self._send_combo(target)
                if not self._wait(self.combo_wait):
                    self._interrupted = True
                    break

                player = self.player_provider() or player
                entities = list(self.entities_provider() or [])
                remaining = self.enemies(entities, player, radius=radius)
                if not remaining:
                    break

                hp = self.player_hp_provider() if self.player_hp_provider else None
                self._set_state(CombatState.REVIVING)
                log.info("Combate continua (%d inimigos, hp=%s); usando revive",
                         len(remaining), hp)
                self._send_revive()
                if not self._wait(self.revive_delay):
                    self._interrupted = True
                    break
        finally:
            if self.post_combat_revive and not self._interrupted and not self._stopped():
                self._set_state(CombatState.POST_COMBAT)
                self._send_revive()
                self._wait(self.revive_delay)
            self._set_state(CombatState.LURING)
            if self.walker is not None:
                self.walker.resume_route()
        return True


def chebyshev_distance(ax: int, ay: int, bx: int, by: int) -> int:
    """Distancia em tiles do grid (movimento 8-direcoes)."""
    return max(abs(ax - bx), abs(ay - by))


class TargetModule(AutomationModule):
    """Seleciona o alvo mais proximo; nao envia input sozinho (esqueleto)."""

    def __init__(self, memory=None, offsets=None, *, entity_reader: Optional[Callable[[], Iterable[Entity]]] = None,
                 attack_range: int = DEFAULT_ATTACK_RANGE, interval: float = 0.3, bus=None,
                 on_target: Optional[Callable[[Entity, int], None]] = None,
                 ignore_npcs: bool = True, ignore_players: bool = True, ignore_dead: bool = True,
                 entity_radius: int = DEFAULT_RADIUS, deep_scan: bool = False) -> None:
        if int(attack_range) < 0:
            raise ValueError("attack_range deve ser >= 0")
        if int(entity_radius) < 0:
            raise ValueError("entity_radius deve ser >= 0")
        self.attack_range = int(attack_range)
        self.ignore_npcs = bool(ignore_npcs)
        self.ignore_players = bool(ignore_players)
        self.ignore_dead = bool(ignore_dead)
        self.deep_scan = bool(deep_scan)
        self._entity_reader = entity_reader
        self._reader: Optional[EntityReader] = None
        if self._entity_reader is None and memory is not None and offsets is not None:
            self._reader = EntityReader.from_offsets(memory, offsets, radius=entity_radius)
        self._on_target = on_target
        self._warned_missing = False
        self.last_target: Optional[tuple[Entity, int]] = None
        super().__init__(memory, offsets, name="target", interval=interval, bus=bus)

    def read_player(self) -> Optional[tuple[int, int, int]]:
        x = self.read("player.x")
        y = self.read("player.y")
        if x is None or y is None:
            return None
        z = self.read("player.z")
        return int(x), int(y), int(z) if z is not None else 0

    def iter_entities(self, px: Optional[int] = None, py: Optional[int] = None) -> list[Entity]:
        if self._entity_reader is not None:
            return list(self._entity_reader())
        if self._reader is None:
            return []
        if px is None or py is None:
            player = self.read_player()
            if player is None:
                return []
            px, py = player[0], player[1]
        if self.deep_scan:
            return self._reader.deep_scan(px, py)
        return self._reader.scan(px, py)

    def select_target(self, entities: Iterable[Entity], px: int, py: int, *,
                      attack_range: Optional[int] = None) -> Optional[tuple[Entity, int]]:
        limit = self.attack_range if attack_range is None else int(attack_range)
        best: Optional[tuple[Entity, int]] = None
        for entity in entities:
            if self.ignore_npcs and entity.is_npc:
                continue
            if self.ignore_players and entity.is_player:
                continue
            if self.ignore_dead and entity.is_dead:
                continue
            distance = chebyshev_distance(px, py, entity.x, entity.y)
            if distance > limit:
                continue
            if best is None or (distance, entity.name) < (best[1], best[0].name):
                best = (entity, distance)
        return best

    def tick(self) -> None:
        player = self.read_player()
        if player is None:
            return
        px, py, _ = player
        entities = self.iter_entities(px, py)
        if not entities:
            if not self._warned_missing:
                log.info("Target: nenhuma entidade disponivel "
                         "(offsets da Entity List ainda nao calibrados?)")
                self._warned_missing = True
            return
        self._warned_missing = False
        found = self.select_target(entities, px, py)
        if found is None:
            self.last_target = None
            return
        entity, distance = found
        self.last_target = found
        log.info(TARGET_LOG.format(name=entity.name, x=entity.x, y=entity.y, distance=distance))
        if self._on_target is not None:
            self._on_target(entity, distance)

    @staticmethod
    def _entity_dict(entity: Entity) -> dict:
        return {"name": entity.name, "x": entity.x, "y": entity.y, "z": entity.z,
                "creature_id": entity.creature_id, "hp_percent": entity.hp_percent,
                "is_npc": entity.is_npc, "is_player": entity.is_player,
                "is_dead": entity.is_dead, "shiny": entity.shiny, "anchor": entity.anchor}

    def execute_combat_loop(self, *, walker=None, entities=None, player=None,
                            config: Optional[dict] = None,
                            player_hp_provider: Optional[Callable[[], Optional[int]]] = None) -> bool:
        """Loop lure -> combo -> revive ate limpar a tela (ou estourar max loops)."""
        cfg = config or {}
        player = player or self.read_player()
        if player is None:
            return False
        px, py = int(player[0]), int(player[1])

        def press(codes, kind="combo") -> bool:
            bus = self.bus
            if bus is None or not hasattr(bus, "press_combo"):
                return False
            return bool(bus.press_combo(list(codes), hold_ms=50, priority=10, module=self.name))
        controller = CombatController(
            entities_provider=lambda: [self._entity_dict(e) for e in self.iter_entities(px, py)],
            player_provider=lambda: (px, py),
            press_combo=press,
            send_text=getattr(self.bus, "send_text", None),
            player_hp_provider=player_hp_provider,
            walker=walker,
            stop_event=self.stop_event,
            lure_target_count=cfg.get("lure_target_count", 8),
            lure_radius=cfg.get("lure_radius", 7),
            perfect_lure=cfg.get("perfect_lure", True),
            perfect_lure_distance=cfg.get("perfect_lure_distance", 3),
            perfect_lure_wait_ms=cfg.get("perfect_lure_wait_ms", 1000),
            attack_range=cfg.get("attack_range_tiles", 7),
            combo_hotkey=cfg.get("combo_hotkey", "F5"),
            combo_command=cfg.get("combo_command", ""),
            focus_shiny=cfg.get("focus_shiny", True),
            revive_hotkey=cfg.get("revive_hotkey", "Shift+F"),
            revive_delay_ms=cfg.get("revive_delay_ms", 800),
            combo_wait_ms=cfg.get("combo_wait_ms", 1000),
            max_combat_loops=cfg.get("max_combat_loops", 5),
            post_combat_revive=cfg.get("post_combat_revive", True),
            prioritize=cfg.get("prioritize", "nearest"),
            blacklist=cfg.get("name_blacklist", ()),
            own_names=cfg.get("own_names", ()),
        )
        if entities is not None:
            entities = [self._entity_dict(e) if isinstance(e, Entity) else e for e in entities]
        return controller.run(entities=entities, player=(px, py))


def _cli() -> int:
    parser = argparse.ArgumentParser(prog="modules.target", description=__doc__)
    parser.add_argument("--process", default="PokeAlliance_dx.exe")
    parser.add_argument("--offsets", default=str(Path(__file__).resolve().parents[1] / "offsets.json"))
    parser.add_argument("--interval", type=float, default=0.3)
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--attack-range", type=int, default=DEFAULT_ATTACK_RANGE)
    parser.add_argument("--radius", type=int, default=DEFAULT_RADIUS)
    parser.add_argument("--deep", action="store_true",
                        help="Usa varredura completa (lenta) em vez das arenas do registry")
    args = parser.parse_args()
    if not math.isfinite(args.interval) or args.interval <= 0:
        parser.error("--interval deve ser finito e > 0")
    logging.basicConfig(level=logging.INFO, stream=__import__("sys").stdout,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    from core import MemoryReader
    from core.offsets import load_offsets

    schema = Path(args.offsets).with_name("offsets.schema.json")
    offsets = load_offsets(args.offsets, schema if schema.exists() else None)
    with MemoryReader(args.process) as memory:
        if not memory.is_attached:
            print(f"attach failed: {memory.last_error}")
            return 2
        module = TargetModule(memory, offsets, interval=args.interval,
                              attack_range=args.attack_range, entity_radius=args.radius,
                              deep_scan=args.deep)
        if not module.start():
            return 1
        try:
            module.stop_event.wait(args.seconds)
        except KeyboardInterrupt:
            pass
        finally:
            module.stop(timeout=2.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
