# pokebot/modules/perfect_lure.py
"""Perfect Lure: agrupamento de monstros com combo emergencial por HP.

Maquina de estados puramente baseada em tick (nunca bloqueia a thread: sem
sleep). Cada chamada a `tick()`/`step()` recebe o instante atual (monotonic) e
um `LureSnapshot` com o estado de mundo ja filtrado pelas regras de targeting.

Estados:
    IDLE               - nada a fazer (menos de `min_monsters_to_lure`) -> combate normal
    WAITING_FOR_GROUP  - reuniu o minimo; espera ficarem proximos/estaveis
    COMBO_PENDING      - condicao atingida, mas o executor estava ocupado
    EXECUTING_COMBO    - combo enviado, aguardando `combo_wait_ms`
    COOLDOWN           - combo/revive concluido, aguardando `attack_cooldown_ms`

Prioridades por tick: (3) emergencia de HP, (4) grupo reunido e estavel,
(5) tempo maximo de espera (lure). Se nenhuma valer, o controle devolve
`handled=False` e o AutoTarget segue com o combate normal.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Iterable, Optional, Sequence

log = logging.getLogger("pokebot.modules.perfect_lure")

MAX_DELAY_MS = 1000
POSITION_UNAVAILABLE = "Perfect Lure indisponível: posição do Pokémon não confirmada"


class LureState(str, Enum):
    IDLE = "idle"
    LURING = "luring"
    MOVING_TO_TILE = "moving_to_tile"
    WAITING_FOR_GROUP = "waiting_for_group"
    COMBO_PENDING = "combo_pending"
    EXECUTING_COMBO = "executing_combo"
    FORCE_KILLING = "force_killing"
    HEALING = "healing"
    COOLDOWN = "cooldown"


@dataclass
class LureSnapshot:
    """Leitura de mundo ja filtrada: monstros elegiveis + estado do Pokemon ativo."""

    valid: bool = True
    # Posicao so pode ser usada quando a identidade E a posicao do Pokemon
    # ativo foram confirmadas por dados validos e recentes.
    position_confirmed: bool = False
    # HP so pode disparar emergencia quando comprovadamente pertence ao
    # Pokemon ativo (identidade conhecida) e a leitura e valida.
    hp_confirmed: bool = False
    monsters: Sequence[dict] = field(default_factory=tuple)
    pokemon_x: Optional[int] = None
    pokemon_y: Optional[int] = None
    pokemon_z: Optional[int] = None
    pokemon_hp_percent: Optional[int] = None
    pokemon_hp_current: Optional[int] = None
    pokemon_hp_max: Optional[int] = None
    pokemon_key: Optional[str] = None
    unavailable_reason: str = ""


@dataclass
class LureStatus:
    """Resultado de um tick, para logs/GUI."""

    state: str
    handled: bool
    detected: int = 0
    near: int = 0
    hp_percent: Optional[int] = None
    hp_limit: int = 0
    remaining_ms: int = 0
    pending_reason: Optional[str] = None
    last_reason: Optional[str] = None
    unavailable_reason: Optional[str] = None


def _clamp_ms(value, default: int, maximum: int = MAX_DELAY_MS) -> int:
    try:
        ms = int(value)
    except (TypeError, ValueError):
        ms = int(default)
    return min(maximum, max(0, ms))


class PerfectLureController:
    """Controlador de Perfect Lure orientado a tick (sem sleep, sem thread)."""

    def __init__(
        self,
        *,
        snapshot_provider: Callable[[], LureSnapshot],
        press_combo: Callable[[Sequence[int], str], bool],
        send_text: Optional[Callable[[str], bool]] = None,
        walker=None,
        stop_event=None,
        is_connected: Optional[Callable[[], bool]] = None,
        combo_codes: Optional[Sequence[int]] = None,
        revive_codes: Optional[Sequence[int]] = None,
        combo_command: str = "",
        enabled: bool = True,
        min_monsters_to_lure: int = 8,
        lure_radius_sqm: int = 1,
        emergency_hp_percent: int = 40,
        max_lure_wait_ms: int = 2500,
        group_stable_ms: int = 300,
        combo_wait_ms: int = 1000,
        revive_delay_ms: int = 800,
        cooldown_ms: int = 900,
        post_combat_revive: bool = True,
        best_tile_provider: Optional[Callable[["LureSnapshot"], Optional[dict]]] = None,
        move_to: Optional[Callable[[int, int, int], bool]] = None,
        heal_callback: Optional[Callable[[], bool]] = None,
        kill_all_on_damage: bool = False,
        damage_threshold_pct: int = 1,
        finish_remaining_count: int = 0,
        force_kill_max_ms: int = 8000,
        best_tile_max_wait_ms: int = 2500,
        heal_retry_count: int = 2,
        heal_retry_ms: int = 2000,
        combo_active: Optional[Callable[[], bool]] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.snapshot_provider = snapshot_provider
        self.press_combo = press_combo
        self.send_text = send_text
        self.walker = walker
        self.stop_event = stop_event
        self.is_connected = is_connected
        self.clock = clock
        self.combo_codes = list(combo_codes or [])
        self.revive_codes = list(revive_codes or [])
        self.combo_command = (combo_command or "").strip()
        self.post_combat_revive = bool(post_combat_revive)
        self.best_tile_provider = best_tile_provider
        self.move_to = move_to
        self.heal_callback = heal_callback
        self.kill_all_on_damage = bool(kill_all_on_damage)
        self.damage_threshold_pct = max(0, int(damage_threshold_pct))
        self.finish_remaining_count = max(0, int(finish_remaining_count))
        self.force_kill_max = _clamp_ms(force_kill_max_ms, 8000, 60000) / 1000.0
        self.best_tile_max_wait = _clamp_ms(best_tile_max_wait_ms, 2500, 60000) / 1000.0
        self.heal_retry_count = max(1, min(5, int(heal_retry_count)))
        self.heal_retry_delay = _clamp_ms(heal_retry_ms, 2000, 10000) / 1000.0
        self.combo_active = combo_active

        self.enabled = bool(enabled)
        self.min_monsters_to_lure = max(1, int(min_monsters_to_lure))
        self.lure_radius_sqm = max(0, int(lure_radius_sqm))
        self.emergency_hp_percent = min(100, max(1, int(emergency_hp_percent)))
        self.max_lure_wait = _clamp_ms(max_lure_wait_ms, 2500, 5000) / 1000.0
        self.group_stable = _clamp_ms(group_stable_ms, 300) / 1000.0
        self.combo_wait = _clamp_ms(combo_wait_ms, 1000) / 1000.0
        self.revive_delay = _clamp_ms(revive_delay_ms, 800) / 1000.0
        self.cooldown = _clamp_ms(cooldown_ms, 900) / 1000.0

        self.state = LureState.IDLE
        self.last_reason: Optional[str] = None

        self._pokemon_key: Optional[str] = None
        self._monsters: dict[object, dict] = {}
        self._pokemon_pos: Optional[tuple[Optional[int], Optional[int]]] = None
        self._hp_percent: Optional[int] = None
        self._pending: Optional[str] = None
        self._busy_kind: Optional[str] = None
        self._busy_until = 0.0
        self._deadline: Optional[float] = None
        self._stable_since: Optional[float] = None
        self._walker_paused = False
        self._last_wait_key = None
        self._last_wait_log = 0.0
        self._unavailable_reason = ""
        self._last_unavailable = ""
        self._best_tile = None
        self._best_tile_started = 0.0
        self._best_tile_attempted = False
        self._hp_at_group = None
        self._damaged = False
        self._force_kill_until = 0.0
        self._next_force_combo = 0.0
        self._heal_attempts = 0
        self._next_heal_at = 0.0

    # -- configuracao em runtime ----------------------------------------
    def configure(self, **kwargs) -> None:
        """Atualiza limites a quente (GUI/config) sem perder o estado atual."""
        if "enabled" in kwargs:
            self.enabled = bool(kwargs["enabled"])
        if "min_monsters_to_lure" in kwargs:
            self.min_monsters_to_lure = max(1, int(kwargs["min_monsters_to_lure"]))
        if "lure_radius_sqm" in kwargs:
            self.lure_radius_sqm = max(0, int(kwargs["lure_radius_sqm"]))
        if "emergency_hp_percent" in kwargs:
            self.emergency_hp_percent = min(100, max(1, int(kwargs["emergency_hp_percent"])))
        if "max_lure_wait_ms" in kwargs:
            self.max_lure_wait = _clamp_ms(kwargs["max_lure_wait_ms"], 2500, 5000) / 1000.0
        if "group_stable_ms" in kwargs:
            self.group_stable = _clamp_ms(kwargs["group_stable_ms"], 300) / 1000.0
        if "combo_wait_ms" in kwargs:
            self.combo_wait = _clamp_ms(kwargs["combo_wait_ms"], 1000) / 1000.0
        if "revive_delay_ms" in kwargs:
            self.revive_delay = _clamp_ms(kwargs["revive_delay_ms"], 800) / 1000.0
        if "cooldown_ms" in kwargs:
            self.cooldown = _clamp_ms(kwargs["cooldown_ms"], 900) / 1000.0
        if "post_combat_revive" in kwargs:
            self.post_combat_revive = bool(kwargs["post_combat_revive"])
        if "combo_codes" in kwargs:
            self.combo_codes = list(kwargs["combo_codes"] or [])
        if "revive_codes" in kwargs:
            self.revive_codes = list(kwargs["revive_codes"] or [])
        if "combo_command" in kwargs:
            self.combo_command = (kwargs["combo_command"] or "").strip()
        if "best_tile_provider" in kwargs:
            self.best_tile_provider = kwargs["best_tile_provider"]
        if "move_to" in kwargs:
            self.move_to = kwargs["move_to"]
        if "heal_callback" in kwargs:
            self.heal_callback = kwargs["heal_callback"]
        if "kill_all_on_damage" in kwargs:
            self.kill_all_on_damage = bool(kwargs["kill_all_on_damage"])
        if "damage_threshold_pct" in kwargs:
            self.damage_threshold_pct = max(0, int(kwargs["damage_threshold_pct"]))
        if "finish_remaining_count" in kwargs:
            self.finish_remaining_count = max(0, int(kwargs["finish_remaining_count"]))
        if "force_kill_max_ms" in kwargs:
            self.force_kill_max = _clamp_ms(kwargs["force_kill_max_ms"], 8000, 60000) / 1000.0
        if "best_tile_max_wait_ms" in kwargs:
            self.best_tile_max_wait = _clamp_ms(
                kwargs["best_tile_max_wait_ms"], 2500, 60000) / 1000.0

    # -- API publica -----------------------------------------------------
    def tick(self, now: Optional[float] = None) -> LureStatus:
        """Le um snapshot novo e avanca a maquina de estados."""
        now = self.clock() if now is None else float(now)
        try:
            snapshot = self.snapshot_provider()
        except Exception as exc:  # noqa: BLE001 - leitura ao vivo pode falhar
            log.debug("Perfect Lure: snapshot falhou: %s", exc)
            snapshot = LureSnapshot(valid=False)
        return self.step(now, snapshot)

    def step(self, now: float, snapshot: LureSnapshot) -> LureStatus:
        self._advance(now)

        if not self.enabled:
            self._cancel("modulo desabilitado")
            return self._status(now, handled=False)

        if self._stopped() or (self.is_connected is not None and not self.is_connected()):
            self._cancel("desconectado")
            return self._status(now, handled=False)

        # Troca do Pokemon ativo: descarta dados antigos e aguarda snapshot novo.
        if snapshot.pokemon_key != self._pokemon_key:
            self._pokemon_key = snapshot.pokemon_key
            self._monsters = {}
            self._pokemon_pos = None
            self._hp_percent = None
            self._reset_group()
            if self._pending is not None:
                log.info("Perfect Lure: combo pendente cancelado (pokemon ativo mudou)")
            self._pending = None
            return self._status(now, handled=self._is_busy())

        # Snapshot invalido NAO significa que todos os monstros sumiram.
        if not snapshot.valid:
            if self._pending is not None:
                log.info("Perfect Lure: combo pendente cancelado (snapshot invalido)")
            self._pending = None
            return self._status(now, handled=self._is_busy())

        self._apply(snapshot)
        detected = len(self._monsters)
        position_ok = bool(snapshot.position_confirmed)
        hp_ok = bool(snapshot.hp_confirmed) and self._hp_percent is not None
        near = self._count_near() if position_ok else 0
        hp = self._hp_percent if hp_ok else None
        self._unavailable_reason = (
            "" if position_ok else (snapshot.unavailable_reason or POSITION_UNAVAILABLE))
        self._log_unavailable()

        if (self._hp_at_group is None and hp is not None and position_ok
                and detected >= self.min_monsters_to_lure):
            self._hp_at_group = hp
        if hp is not None:
            if self._hp_at_group is not None and hp < self._hp_at_group - self.damage_threshold_pct:
                self._damaged = True
            if hp < self.emergency_hp_percent:
                self._damaged = True

        if self._busy_kind == "post_combo":
            return self._resolve_post_combo(now, hp, detected)

        # Sem posicao confirmada nao existe agrupamento (nem por tempo maximo).
        emergency = hp is not None and detected >= 1 and hp < self.emergency_hp_percent
        group_ready = (position_ok and detected >= self.min_monsters_to_lure
                       and detected > 0 and near == detected)
        if position_ok:
            self._update_group_timers(now, detected, group_ready)
        else:
            self._reset_group()
        stable_ok = (group_ready and self._stable_since is not None
                     and (now - self._stable_since) >= self.group_stable)
        maxwait = (position_ok and detected >= self.min_monsters_to_lure
                   and self._deadline is not None and now >= self._deadline)

        # O timeout duro de lure vence o posicionamento: parou de esperar, comba.
        if emergency or maxwait:
            self._cancel_best_tile_move()
        elif not self._is_busy():
            moved = self._tick_best_tile(now, snapshot, detected)
            if moved:
                return self._status(now, handled=True)

        reason: Optional[str] = None
        if emergency:
            reason = "HP %d%% abaixo do limite de %d%%" % (hp, self.emergency_hp_percent)
        elif stable_ok:
            reason = "grupo de %d monstros pronto" % detected
        elif maxwait:
            reason = "tempo maximo de lure atingido (%d monstros)" % detected

        if self._is_busy():
            if reason and reason != self._pending:
                self._pending = reason
                log.info("Perfect Lure: combo pendente — %s (executor ocupado)", reason)
            elif not reason:
                self._pending = None
            return self._status(now, handled=True)

        if reason:
            self._start_combo(reason, now)
            return self._status(now, handled=True)

        self._pending = None
        if position_ok and detected >= self.min_monsters_to_lure:
            self._set_state(LureState.WAITING_FOR_GROUP)
            self._log_wait(detected, near, hp, now)
            return self._status(now, handled=True)

        if position_ok and detected < self.min_monsters_to_lure:
            # Lure ativo: continua andando e contando. NAO deixa o Target
            # normal atacar antes de alcancar o Min Count.
            self._reset_group()
            self._set_state(LureState.LURING)
            return self._status(now, handled=True)

        # Sem posicao confirmada: Perfect Lure indisponivel; combate normal age.
        self._reset_cycle()
        self._set_state(LureState.IDLE)
        return self._status(now, handled=False)

    # -- maquina de estados ---------------------------------------------
    def _advance(self, now: float) -> None:
        if self._busy_kind == "executing" and now >= self._busy_until:
            if self._combo_still_active():
                self._busy_kind = "waiting_combo"
                self._busy_until = now + 0.2
                self._set_state(LureState.EXECUTING_COMBO)
            else:
                self._begin_post_combo(now)
        elif self._busy_kind == "waiting_combo" and now >= self._busy_until:
            if self._combo_still_active():
                self._busy_until = now + 0.2
                self._set_state(LureState.EXECUTING_COMBO)
            else:
                self._begin_post_combo(now)
        elif self._busy_kind == "revive" and now >= self._busy_until:
            if self.kill_all_on_damage:
                self._busy_kind = "post_combo"
                self._busy_until = 0.0
                self._set_state(LureState.COMBO_PENDING)
            else:
                self._busy_until = now + self.cooldown
                self._busy_kind = "cooldown"
                self._set_state(LureState.COOLDOWN)
        elif self._busy_kind == "cooldown" and now >= self._busy_until:
            self._busy_kind = None
            self._busy_until = 0.0
            self._resume_walker()
            self._reset_cycle()
            self._set_state(LureState.IDLE)

    def _combo_still_active(self) -> bool:
        if self.combo_active is None:
            return False
        try:
            return bool(self.combo_active())
        except Exception:  # noqa: BLE001
            return False

    def _begin_post_combo(self, now: float) -> None:
        if self.post_combat_revive and self._monsters and self.revive_codes:
            self._send_revive()
            self._busy_kind = "revive"
            self._busy_until = now + self.revive_delay
            self._set_state(LureState.COOLDOWN)
            return
        if self.kill_all_on_damage:
            self._busy_kind = "post_combo"
            self._busy_until = 0.0
            self._set_state(LureState.COMBO_PENDING)
            return
        self._busy_until = now + self.cooldown
        self._busy_kind = "cooldown"
        self._set_state(LureState.COOLDOWN)

    def _start_combo(self, reason: str, now: float) -> bool:
        self._pending = None
        if not self._send_combo():
            self._pending = reason
            self._set_state(LureState.COMBO_PENDING)
            log.info("Perfect Lure: combo nao enviado (fila cheia) — %s", reason)
            return False
        self.last_reason = reason
        cancel_dynamic = getattr(self.walker, "cancel_dynamic_lure", None)
        if callable(cancel_dynamic):
            try:
                cancel_dynamic()
            except Exception as exc:  # noqa: BLE001
                log.debug("cancel_dynamic_lure falhou: %s", exc)
        if self.walker is not None and not self._walker_paused:
            try:
                self.walker.pause_for_combat()
                self._walker_paused = True
            except Exception as exc:  # noqa: BLE001
                log.debug("walker.pause_for_combat falhou: %s", exc)
        log.info("Perfect Lure: combo disparado — %s", reason)
        if self._hp_at_group is None and self._hp_percent is not None:
            self._hp_at_group = self._hp_percent
        self._busy_kind = "executing"
        self._busy_until = now + self.combo_wait
        self._force_kill_until = 0.0
        self._next_force_combo = now
        self._heal_attempts = 0
        self._next_heal_at = 0.0
        self._reset_group()
        self._set_state(LureState.EXECUTING_COMBO)
        return True

    def _cancel(self, why: str) -> None:
        had = self._pending is not None or self._busy_kind is not None
        self._pending = None
        self._busy_kind = None
        self._busy_until = 0.0
        self._reset_group()
        self._cancel_best_tile_move()
        self._reset_cycle()
        self._resume_walker()
        if had:
            log.info("Perfect Lure: cancelado (%s)", why)
        self._set_state(LureState.IDLE)

    # -- best tile / pos-combo -------------------------------------------
    @staticmethod
    def _tile_xyz(tile):
        if isinstance(tile, dict):
            return int(tile["x"]), int(tile["y"]), int(tile.get("z", 0) or 0)
        return int(tile.x), int(tile.y), int(getattr(tile, "z", 0) or 0)

    def _tick_best_tile(self, now: float, snapshot: LureSnapshot, detected: int) -> bool:
        if self.best_tile_provider is None or self._pokemon_pos is None:
            return False
        if detected < self.min_monsters_to_lure:
            self._cancel_best_tile_move()
            return False
        if self._best_tile_attempted and self._best_tile is None:
            return False
        px, py = self._pokemon_pos
        if self._best_tile is None:
            try:
                tile = self.best_tile_provider(snapshot)
            except Exception as exc:  # noqa: BLE001 - mapa pode estar indisponivel
                log.debug("Perfect Lure: best tile falhou: %s", exc)
                return False
            if not tile:
                return False
            try:
                tx, ty, tz = self._tile_xyz(tile)
            except (KeyError, TypeError, ValueError):
                return False
            if (tx, ty) == (px, py):
                self._best_tile = tile
                self._best_tile_attempted = True
                return False
            cancel_dynamic = getattr(self.walker, "cancel_dynamic_lure", None)
            if callable(cancel_dynamic):
                cancel_dynamic()
            self._best_tile_attempted = True
            if not self._start_move(tx, ty, tz):
                return False
            self._best_tile = tile
            self._best_tile_started = now
            self._set_state(LureState.MOVING_TO_TILE)
            log.info("Perfect Lure: indo para best tile (%d,%d,%d)", tx, ty, tz)
            return True

        try:
            tx, ty, _tz = self._tile_xyz(self._best_tile)
        except (KeyError, TypeError, ValueError):
            self._cancel_best_tile_move()
            return False
        if (tx, ty) == (px, py):
            return False
        if now - self._best_tile_started >= self.best_tile_max_wait:
            log.info("Perfect Lure: best tile (%d,%d) nao alcancado em %dms; seguindo no local",
                     tx, ty, int(self.best_tile_max_wait * 1000))
            self._cancel_best_tile_move(give_up=True)
            return False
        self._set_state(LureState.MOVING_TO_TILE)
        return True

    def _start_move(self, x: int, y: int, z: int) -> bool:
        if self.move_to is not None:
            try:
                return bool(self.move_to(x, y, z))
            except Exception as exc:  # noqa: BLE001
                log.debug("Perfect Lure: move_to falhou: %s", exc)
                return False
        detour = getattr(self.walker, "detour_to", None)
        if callable(detour):
            try:
                return bool(detour(x, y, z))
            except Exception as exc:  # noqa: BLE001
                log.debug("Perfect Lure: detour_to falhou: %s", exc)
        return False

    def _cancel_best_tile_move(self, *, give_up: bool = False) -> None:
        if self._best_tile is None:
            if give_up:
                self._best_tile_attempted = True
            return
        restore = getattr(self.walker, "restore_route", None)
        if callable(restore):
            try:
                restore()
            except Exception as exc:  # noqa: BLE001
                log.debug("Perfect Lure: restore_route falhou: %s", exc)
        self._best_tile = None
        self._best_tile_started = 0.0
        if give_up:
            self._best_tile_attempted = True

    def _resolve_post_combo(self, now: float, hp: Optional[int], detected: int) -> LureStatus:
        if (hp is not None and self._hp_at_group is not None
                and hp < self._hp_at_group - self.damage_threshold_pct):
            self._damaged = True
        if not self._damaged:
            log.info("Perfect Lure: sem dano apos combo; retomando rota (%d monstros restantes)",
                     detected)
            self._finish_cycle(now)
            return self._status(now, handled=True)

        if self._force_kill_until <= 0.0:
            self._force_kill_until = now + self.force_kill_max
        if detected > 0 and now < self._force_kill_until:
            if now >= self._next_force_combo:
                if self._send_combo():
                    self._next_force_combo = now + self.combo_wait
                    log.info("Perfect Lure: HP caiu; forcando limpeza (%d monstros)", detected)
                else:
                    self._next_force_combo = now + 0.2
            self._set_state(LureState.FORCE_KILLING)
            return self._status(now, handled=True)

        if detected > 0:
            log.info("Perfect Lure: tempo de limpeza esgotado com %d monstros; curando", detected)
        self._heal(now)
        return self._status(now, handled=True)

    def _heal(self, now: float) -> None:
        if now < self._next_heal_at:
            self._set_state(LureState.HEALING)
            return
        if self._heal_attempts >= self.heal_retry_count:
            log.warning("Perfect Lure: cura falhou apos %d tentativas; seguindo",
                        self._heal_attempts)
            self._finish_cycle(now)
            return
        self._heal_attempts += 1
        if self.heal_callback is None:
            log.info("Perfect Lure: dano detectado, mas Healer nao configurado")
            self._finish_cycle(now)
            return
        try:
            ok = bool(self.heal_callback())
        except Exception as exc:  # noqa: BLE001
            log.warning("Perfect Lure: heal_callback falhou: %s", exc)
            ok = False
        if ok:
            log.info("Perfect Lure: cura pos-combate concluida (tentativa %d)",
                     self._heal_attempts)
            self._finish_cycle(now)
            return
        if self._heal_attempts < self.heal_retry_count:
            self._next_heal_at = now + self.heal_retry_delay
            self._set_state(LureState.HEALING)
            log.warning("Perfect Lure: cura falhou (tentativa %d/%d); retry em %dms",
                        self._heal_attempts, self.heal_retry_count,
                        int(self.heal_retry_delay * 1000))
            return
        log.warning("Perfect Lure: cura falhou apos %d tentativas; seguindo",
                    self._heal_attempts)
        self._finish_cycle(now)

    def _finish_cycle(self, now: float) -> None:
        self._busy_kind = "cooldown"
        self._busy_until = now + self.cooldown
        self._set_state(LureState.COOLDOWN)

    def _reset_cycle(self) -> None:
        self._best_tile = None
        self._best_tile_started = 0.0
        self._best_tile_attempted = False
        self._hp_at_group = None
        self._damaged = False
        self._force_kill_until = 0.0
        self._next_force_combo = 0.0
        self._heal_attempts = 0
        self._next_heal_at = 0.0
        self._reset_group()

    # -- helpers ---------------------------------------------------------
    def _set_state(self, state: LureState) -> None:
        if self.state != state:
            self.state = state
            log.debug("Perfect Lure state -> %s", state.value)

    def _is_busy(self) -> bool:
        return self._busy_kind is not None

    def _stopped(self) -> bool:
        stop_event = getattr(self, "stop_event", None)
        return stop_event is not None and stop_event.is_set()

    def _apply(self, snapshot: LureSnapshot) -> None:
        if (snapshot.position_confirmed and snapshot.pokemon_x is not None
                and snapshot.pokemon_y is not None):
            self._pokemon_pos = (int(snapshot.pokemon_x), int(snapshot.pokemon_y))
        else:
            self._pokemon_pos = None

        hp_percent = snapshot.pokemon_hp_percent if snapshot.hp_confirmed else None
        if (hp_percent is None and snapshot.hp_confirmed
                and snapshot.pokemon_hp_current is not None and snapshot.pokemon_hp_max):
            try:
                mx = int(snapshot.pokemon_hp_max)
                if mx > 0:
                    hp_percent = round(100.0 * int(snapshot.pokemon_hp_current) / mx)
            except (TypeError, ValueError):
                hp_percent = None
        if hp_percent is not None:
            try:
                hp_percent = int(hp_percent)
            except (TypeError, ValueError):
                hp_percent = None
            if hp_percent is not None and not (0 <= hp_percent <= 100):
                hp_percent = None
        self._hp_percent = hp_percent

        deduped: dict[object, dict] = {}
        for m in snapshot.monsters or ():
            key = m.get("creature_id")
            if key is None:
                key = m.get("anchor")
            if key is None:
                key = (m.get("name"), m.get("x"), m.get("y"))
            deduped[key] = m
        self._monsters = deduped

    def _count_near(self) -> int:
        if self._pokemon_pos is None:
            return 0
        px, py = self._pokemon_pos
        if px is None or py is None:
            return 0
        count = 0
        for m in self._monsters.values():
            try:
                distance = max(abs(int(m.get("x", px)) - px), abs(int(m.get("y", py)) - py))
            except (TypeError, ValueError):
                continue
            if distance <= self.lure_radius_sqm:
                count += 1
        return count

    def _update_group_timers(self, now: float, detected: int, group_ready: bool) -> None:
        if detected >= self.min_monsters_to_lure:
            if self._deadline is None:
                self._deadline = now + self.max_lure_wait
            if group_ready:
                if self._stable_since is None:
                    self._stable_since = now
            else:
                self._stable_since = None
        else:
            self._deadline = None
            self._stable_since = None

    def _reset_group(self) -> None:
        self._deadline = None
        self._stable_since = None

    def _remaining_ms(self, now: float) -> int:
        target = None
        if self._busy_kind is not None:
            target = self._busy_until
        elif self._deadline is not None:
            target = self._deadline
        if target is None:
            return 0
        return max(0, int(round((target - now) * 1000.0)))

    def _log_unavailable(self) -> None:
        reason = self._unavailable_reason
        if reason and reason != self._last_unavailable:
            log.info(reason)
        self._last_unavailable = reason

    def _log_wait(self, detected: int, near: int, hp: Optional[int], now: float) -> None:
        key = (detected, near, hp)
        if key == self._last_wait_key and (now - self._last_wait_log) < 1.0:
            return
        self._last_wait_key = key
        self._last_wait_log = now
        log.info("Perfect Lure: aguardando agrupamento — %d/%d proximos "
                 "(hp=%s limite=%d%%, resta %dms)", near, detected, hp,
                 self.emergency_hp_percent, self._remaining_ms(now))

    def _status(self, now: float, handled: bool) -> LureStatus:
        return LureStatus(
            state=self.state.value,
            handled=handled,
            detected=len(self._monsters),
            near=self._count_near(),
            hp_percent=self._hp_percent,
            hp_limit=self.emergency_hp_percent,
            remaining_ms=self._remaining_ms(now),
            pending_reason=self._pending,
            last_reason=self.last_reason,
            unavailable_reason=self._unavailable_reason or None,
        )

    # -- envio de input --------------------------------------------------
    def _send_combo(self) -> bool:
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
            return False
        return bool(self.press_combo(self.revive_codes, "revive"))

    def _resume_walker(self) -> None:
        if self.walker is None:
            return
        restore = getattr(self.walker, "restore_route", None)
        if callable(restore):
            try:
                restore()
            except Exception as exc:  # noqa: BLE001
                log.debug("walker.restore_route falhou: %s", exc)
        if self._walker_paused:
            try:
                self.walker.resume_route()
            except Exception as exc:  # noqa: BLE001
                log.debug("walker.resume_route falhou: %s", exc)
            self._walker_paused = False

    # -- introspecao -----------------------------------------------------
    @property
    def status(self) -> LureStatus:
        now = self.clock()
        return self._status(now, handled=self.state != LureState.IDLE or self._pending is not None)
