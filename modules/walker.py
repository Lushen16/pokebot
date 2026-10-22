from __future__ import annotations

import logging
import random
import time
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple

from modules.base import AutomationModule
from core.position import is_valid_position

log = logging.getLogger("pokebot.modules.walker")

VK_W = 0x57
VK_A = 0x41
VK_S = 0x53
VK_D = 0x44
ALL_MOVEMENT_KEYS: Tuple[int, ...] = (VK_W, VK_A, VK_S, VK_D)
FLOOR_SENSITIVE_ACTIONS = frozenset(("Stair",))


class WalkerState(str, Enum):
    IDLE = "idle"
    MOVING = "moving"
    WAITING = "waiting"


class Walker(AutomationModule):
    """Módulo de movimentação do personagem tile a tile usando WASD, com gravação e loop de Hunt."""

    def __init__(
        self,
        memory_reader: Any = None,
        offset_registry: Any = None,
        input_bus: Any = None,
        poll_interval_sec: float = 0.05,
        loop: bool = True,
        record_min_distance: Optional[int] = None,
        record_sqm_interval: int = 3,
        record_stable_ms: int = 0,
        arrival_tolerance: int = 1,
        movement_keys: Optional[Dict[str, str]] = None,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        memory = memory_reader if memory_reader is not None else kwargs.pop("memory", None)
        offsets = offset_registry if offset_registry is not None else kwargs.pop("offsets", None)
        bus = input_bus if input_bus is not None else kwargs.pop("bus", None)

        autostart = kwargs.pop("autostart", False)
        super().__init__(
            memory=memory,
            offsets=offsets,
            bus=bus,
            interval=poll_interval_sec,
            name=kwargs.pop("name", "walker"),
            **kwargs,
        )
        self._route_lock = self._lock
        self.movement_keys = self._validate_movement_keys(movement_keys)
        self.arrival_tolerance = arrival_tolerance
        self._next_move_delay_until = 0.0
        self.waypoints: List[Dict[str, Any]] = []
        self._pressed_keys: Set[int] = set()
        self.loop: bool = loop
        self.recording: bool = False
        self.record_sqm_interval = max(1, int(
            record_sqm_interval if record_min_distance is None else record_min_distance))
        self.state = WalkerState.IDLE
        self.wait_until = 0.0
        self._pause_reasons: Set[str] = set()
        self.distance_guard_enabled = False
        self.distance_guard_max = 5
        self.distance_guard_resume = 3
        self.record_action = "Walk"
        self.record_param = ""
        self._record_position = None
        self._record_direction = None
        self.record_stable_ms = max(0, int(record_stable_ms))
        self._record_candidate = None
        self._record_candidate_since = 0.0
        self._invalid_position_ticks = 0
        self._position_lost_since = None
        self._watchdog_attempts = 0
        self._watchdog_recover_at = 0.0
        self._detour_saved = None
        self._detour_target = None
        self.dynamic_lure_enabled = False
        self.dynamic_lure_max_distance = 5
        self.dynamic_lure_resume_distance = 3
        self.dynamic_lure_chase = True
        self.dynamic_lure_use_move_command = True
        self.dynamic_lure_move_command = "{name}, move!"
        self.dynamic_lure_move_command_verified = False
        self.dynamic_lure_move_cooldown = 1.2
        self._dynamic_move_unverified_warned = False
        self.pokemon_position_provider = None
        self.pokemon_move_command_provider = None
        self._dynamic_waiting = False
        self._dynamic_started = 0.0
        self._next_dynamic_command_at = 0.0
        self._load_dynamic_lure_settings()
        self._reset_position_retries()
        self._reset_progress()
        if autostart:
            self.start()

    @property
    def state(self) -> WalkerState:
        with self._lock:
            return self._motion_state

    @state.setter
    def state(self, value: WalkerState) -> None:
        with self._lock:
            self._motion_state = WalkerState(value)

    @property
    def walker_state(self) -> WalkerState:
        return self.state

    @walker_state.setter
    def walker_state(self, value: WalkerState) -> None:
        self.state = value

    @property
    def stats(self) -> dict:
        with self._lock:
            result = super().stats
            result["state"] = super().state.value
            result["walker_state"] = self.state.value
            result["position_retry_count"] = self._position_retry_count
            result["next_position_read_at"] = self._next_position_read_at
            result["last_position_error"] = self._last_position_error
            result["last_move_time"] = self._last_move_time
            result["pause_reasons"] = sorted(self._pause_reasons)
            return result

    @staticmethod
    def _validate_movement_keys(keys: Optional[Dict[str, str]]) -> Dict[str, int]:
        if keys is None:
            return {"north": VK_W, "south": VK_S, "east": VK_D, "west": VK_A}
        codes = {"w": VK_W, "s": VK_S, "d": VK_D, "a": VK_A,
                 "up": 0x26, "down": 0x28, "right": 0x27, "left": 0x25}
        directions = ("north", "south", "east", "west")
        if not isinstance(keys, dict) or any(
            not isinstance(keys.get(direction), str) or keys[direction].lower() not in codes
            for direction in directions
        ):
            raise ValueError("walk_keys exige north/south/east/west com WASD ou Up/Down/Right/Left")
        result = {direction: codes[keys[direction].lower()] for direction in directions}
        if len(set(result.values())) != 4:
            raise ValueError("Cada direção da rota deve usar uma tecla diferente")
        return result

    @staticmethod
    def validate_arrival_tolerance(value: int) -> int:
        if type(value) is not int or not 0 <= value <= 2:
            raise ValueError("Tolerância de chegada deve ser um inteiro entre 0 e 2")
        return value

    @property
    def arrival_tolerance(self) -> int:
        with self._route_lock:
            return self._arrival_tolerance

    @arrival_tolerance.setter
    def arrival_tolerance(self, value: int) -> None:
        validated = self.validate_arrival_tolerance(value)
        with self._route_lock:
            self._arrival_tolerance = validated

    @property
    def record_min_distance(self) -> int:
        return self.record_sqm_interval

    @record_min_distance.setter
    def record_min_distance(self, value: int) -> None:
        with self._route_lock:
            self.record_sqm_interval = max(1, int(value))

    @staticmethod
    def _waypoint(x: int, y: int, z: int, action: str = "Walk", param: str = "") -> Dict[str, Any]:
        if not is_valid_position(x, y, z) or (x == 0 and y == 0):
            raise ValueError("Waypoint exige coordenadas válidas e X/Y diferentes de (0, 0)")
        action = str(action)
        param = str(param)
        if action not in ("Walk", "Wait", "Say", "Safe Spot", "BOX", "Stair"):
            raise ValueError(f"Ação desconhecida: {action}")
        if action == "Wait" and int(param) < 0:
            raise ValueError("Wait deve ser um inteiro de milissegundos >= 0")
        if action == "Say" and (not param or len(param) > 1024 or not param.isprintable()):
            raise ValueError("Say exige de 1 a 1024 caracteres imprimíveis")
        return {"x": int(x), "y": int(y), "z": int(z), "action": action, "param": param}

    def waypoint_snapshot(self) -> List[Dict[str, Any]]:
        with self._route_lock:
            return [dict(waypoint) for waypoint in self.waypoints]

    def configure_recording(self, action: str = "Walk", param: str = "", interval: int = 3) -> None:
        self._waypoint(1, 1, 7, action, param)
        with self._route_lock:
            self.record_action = action
            self.record_param = str(param)
            self.record_sqm_interval = max(1, int(interval))

    def set_loop(self, enabled: bool) -> None:
        """Ativa ou desativa a repetição contínua da rota."""
        with self._route_lock:
            self.loop = bool(enabled)

    def add_waypoint(self, x: int, y: int, z: int, action: str = "Walk", param: str = "") -> None:
        waypoint = self._waypoint(x, y, z, action, param)
        with self._route_lock:
            self.waypoints.append(waypoint)

    def remove_waypoint(self, index: int) -> None:
        with self._route_lock:
            if self.is_running or self.recording:
                raise RuntimeError("Pare a rota e a gravação antes de excluir um waypoint")
            if type(index) is not int or not 0 <= index < len(self.waypoints):
                raise IndexError("Waypoint selecionado fora da rota")
            self._reset_motion()
            del self.waypoints[index]

    def clear_waypoints(self) -> None:
        with self._route_lock:
            self._clear_detour()
            self.waypoints.clear()
            self._reset_motion()

    def replace_route(self, waypoints: List[Dict[str, Any]], loop: bool,
                      arrival_tolerance: Optional[int] = None,
                      distance_guard: Optional[Dict[str, Any]] = None) -> None:
        validated = []
        for waypoint in waypoints:
            if waypoint.get("x") == 0 and waypoint.get("y") == 0:
                log.warning("Waypoint (0, 0) descartado ao carregar a rota: %s", waypoint)
                continue
            validated.append(self._waypoint(**waypoint))
        if arrival_tolerance is not None:
            self.validate_arrival_tolerance(arrival_tolerance)
        if distance_guard is not None:
            self.validate_distance_guard(**distance_guard)
        with self._route_lock:
            self._clear_detour()
            self._reset_motion()
            self.recording = False
            self.waypoints[:] = validated
            self.loop = bool(loop)
            if arrival_tolerance is not None:
                self.arrival_tolerance = arrival_tolerance
            self.configure_distance_guard(**(distance_guard or {}))
            self._watchdog_attempts = 0
            self._watchdog_recover_at = 0.0
            self._set_pause_reason("watchdog", False)

    def route_snapshot(self) -> Dict[str, Any]:
        with self._route_lock:
            return {"waypoints": self.waypoint_snapshot(), "loop": self.loop,
                    "arrival_tolerance": self.arrival_tolerance,
                    "distance_guard": {"enabled": self.distance_guard_enabled,
                                       "max_distance": self.distance_guard_max,
                                       "resume_distance": self.distance_guard_resume}}

    @staticmethod
    def validate_distance_guard(enabled: bool, max_distance: int, resume_distance: int) -> None:
        if (type(enabled) is not bool or type(max_distance) is not int
                or type(resume_distance) is not int
                or not 0 <= resume_distance < max_distance <= 30):
            raise ValueError("Distâncias exigem 0 <= retomada < aguardar <= 30 SQM")

    def configure_distance_guard(self, *, enabled: bool = False,
                                 max_distance: int = 5, resume_distance: int = 3) -> None:
        self.validate_distance_guard(enabled, max_distance, resume_distance)
        with self._route_lock:
            self.distance_guard_enabled = enabled
            self.distance_guard_max = max_distance
            self.distance_guard_resume = resume_distance
            if not enabled:
                self._set_pause_reason("distance", False)
                self._set_pause_reason("distance_unavailable", False)

    @property
    def pause_reasons(self) -> frozenset[str]:
        with self._route_lock:
            return frozenset(self._pause_reasons)

    def _set_pause_reason(self, reason: str, paused: bool) -> None:
        previous = frozenset(self._pause_reasons)
        if paused:
            self._pause_reasons.add(reason)
            self._release_all_keys()
        else:
            self._pause_reasons.discard(reason)
        if previous != self._pause_reasons:
            self._reset_progress()
            log.info("Walker pausas: %s -> %s; motivo=%s",
                     sorted(previous), sorted(self._pause_reasons), reason)

    def _distance_guard_tick(self) -> None:
        if not self.distance_guard_enabled:
            return
        try:
            player = self.read_player_position()
            pokemon = self.pokemon_position_provider() if callable(self.pokemon_position_provider) else None
            valid = (player is not None and pokemon is not None
                     and self.is_valid_position(*player) and self.is_valid_position(*pokemon)
                     and player[:2] != (0, 0) and pokemon[:2] != (0, 0)
                     and player[2] == pokemon[2])
        except Exception:
            valid = False
        was_unavailable = "distance_unavailable" in self._pause_reasons
        self._set_pause_reason("distance_unavailable", not valid)
        if not valid:
            if not was_unavailable:
                log.info("Walker distancia: posicao do Pokemon indisponivel; aguardando sem mover")
            return
        distance = max(abs(player[0] - pokemon[0]), abs(player[1] - pokemon[1]))
        was_paused = "distance" in self._pause_reasons
        if distance >= self.distance_guard_max:
            self._set_pause_reason("distance", True)
            if not was_paused:
                log.info("Walker distancia: aguardando (dist=%d limite=%d retomar=%d player=%s pokemon=%s)",
                         distance, self.distance_guard_max, self.distance_guard_resume,
                         player, pokemon)
        elif distance <= self.distance_guard_resume:
            self._set_pause_reason("distance", False)
            if was_paused:
                log.info("Walker distancia: retomando (dist=%d retomar=%d player=%s pokemon=%s)",
                         distance, self.distance_guard_resume, player, pokemon)

    def goto(self, x: int, y: int, z: int) -> None:
        with self._route_lock:
            self._clear_detour()
            self._reset_motion()
            self.waypoints = [self._waypoint(x, y, z)]

    def detour_to(self, x: int, y: int, z: int) -> bool:
        """Vai a um tile temporario preservando a rota atual para depois."""
        waypoint = self._waypoint(x, y, z)
        with self._route_lock:
            if self._detour_saved is None:
                self._detour_saved = (
                    [dict(w) for w in self.waypoints], self.loop, self._arrival_tolerance)
            self._reset_motion()
            self.waypoints = [waypoint]
            self.loop = False
            self._arrival_tolerance = 0
            self._detour_target = (int(x), int(y), int(z))
            log.info("Walker desvio: iniciado target=(%d,%d,%d) waypoint=%s pausas=%s",
                     x, y, z, self.waypoints[0] if self.waypoints else None,
                     sorted(self._pause_reasons))
        return True

    def restore_route(self) -> bool:
        """Restaura a rota salva por ``detour_to``."""
        with self._route_lock:
            saved = self._detour_saved
            if saved is None:
                return False
            waypoints, loop, tolerance = saved
            self._detour_saved = None
            self._detour_target = None
            self._reset_motion()
            self.waypoints = [dict(w) for w in waypoints]
            self.loop = bool(loop)
            self._arrival_tolerance = int(tolerance)
            log.info("Walker desvio: rota restaurada waypoint=%s pausas=%s",
                     self.waypoints[0] if self.waypoints else None,
                     sorted(self._pause_reasons))
        return True

    @property
    def detour_active(self) -> bool:
        with self._route_lock:
            return self._detour_saved is not None

    @property
    def detour_target(self) -> Optional[Tuple[int, int, int]]:
        with self._route_lock:
            return self._detour_target

    def _clear_detour(self) -> None:
        self._detour_saved = None
        self._detour_target = None

    # -- lure dinamico ---------------------------------------------------
    def _load_dynamic_lure_settings(self) -> None:
        ctx = getattr(self, "ctx", None)
        if ctx is None:
            try:
                from context import get_context
                ctx = get_context()
                self.ctx = ctx
            except Exception:  # noqa: BLE001
                ctx = None
        if ctx is None:
            return
        try:
            self.dynamic_lure_enabled = bool(
                ctx.get("walker", "dynamic_lure_enabled", default=False))
            self.dynamic_lure_max_distance = max(1, int(
                ctx.get("walker", "dynamic_lure_max_distance", default=5) or 5))
            self.dynamic_lure_resume_distance = max(0, int(
                ctx.get("walker", "dynamic_lure_resume_distance", default=3) or 0))
            self.dynamic_lure_chase = bool(
                ctx.get("walker", "dynamic_lure_chase", default=True))
            self.dynamic_lure_use_move_command = bool(
                ctx.get("walker", "dynamic_lure_use_move_command", default=True))
            self.dynamic_lure_move_command = str(
                ctx.get("walker", "dynamic_lure_move_command", default="{name}, move!") or "")
            self.dynamic_lure_move_command_verified = bool(
                ctx.get("walker", "dynamic_lure_move_command_verified", default=False))
            self.dynamic_lure_move_cooldown = max(0.2, float(
                ctx.get("walker", "dynamic_lure_move_cooldown_ms", default=1200) or 1200) / 1000.0)
        except (TypeError, ValueError):
            pass
        self.dynamic_lure_resume_distance = min(
            self.dynamic_lure_resume_distance, self.dynamic_lure_max_distance - 1)
        if not self.dynamic_lure_enabled and self._dynamic_waiting:
            self._end_dynamic_lure()

    def configure_dynamic_lure(self, *, enabled=None, max_distance=None,
                               resume_distance=None, chase=None,
                               use_move_command=None, move_command=None) -> None:
        """Atualiza o lure dinamico e persiste no contexto."""
        if max_distance is not None:
            self.dynamic_lure_max_distance = max(1, min(30, int(max_distance)))
        if resume_distance is not None:
            self.dynamic_lure_resume_distance = max(0, int(resume_distance))
        self.dynamic_lure_resume_distance = min(
            self.dynamic_lure_resume_distance, self.dynamic_lure_max_distance - 1)
        if chase is not None:
            self.dynamic_lure_chase = bool(chase)
        if use_move_command is not None:
            self.dynamic_lure_use_move_command = bool(use_move_command)
        if move_command is not None:
            self.dynamic_lure_move_command = str(move_command)
        if enabled is not None:
            self.dynamic_lure_enabled = bool(enabled)
        if not self.dynamic_lure_enabled and self._dynamic_waiting:
            self._end_dynamic_lure()
        ctx = getattr(self, "ctx", None)
        if ctx is not None and hasattr(ctx, "set"):
            ctx.set(self.dynamic_lure_enabled, "walker", "dynamic_lure_enabled")
            ctx.set(self.dynamic_lure_max_distance, "walker", "dynamic_lure_max_distance")
            ctx.set(self.dynamic_lure_resume_distance, "walker", "dynamic_lure_resume_distance")
            ctx.set(self.dynamic_lure_chase, "walker", "dynamic_lure_chase")
            ctx.set(self.dynamic_lure_use_move_command, "walker", "dynamic_lure_use_move_command")
            ctx.set(self.dynamic_lure_move_command, "walker", "dynamic_lure_move_command")
            ctx.set(int(self.dynamic_lure_move_cooldown * 1000), "walker",
                    "dynamic_lure_move_cooldown_ms")

    def on_config_change(self) -> None:
        self._load_dynamic_lure_settings()

    def cancel_dynamic_lure(self) -> None:
        """Encerra o lure dinamico e devolve o controle a rota normal."""
        with self._route_lock:
            if self._dynamic_waiting:
                self._end_dynamic_lure()

    def _dynamic_lure_tick(self, now: float) -> bool:
        """Pausa/retoma a rota conforme a distancia do Pokemon ativo.

        Retorna True quando o tick normal da rota deve ser ignorado (espera).
        No modo chase o treinador anda em direcao ao Pokemon e o tick normal
        processa o waypoint temporario.
        """
        if not self.dynamic_lure_enabled or self.pokemon_position_provider is None:
            if self._dynamic_waiting:
                self._end_dynamic_lure()
            return False
        if self.recording or not self.waypoints:
            return False
        if self.walker_state == WalkerState.WAITING:
            return False
        if self.detour_active and not self._dynamic_waiting:
            return False
        try:
            player = self.read_player_position()
            pokemon = self.pokemon_position_provider()
        except Exception:  # noqa: BLE001 - leitura ao vivo pode falhar
            return False
        if player is None or pokemon is None:
            return False
        try:
            if isinstance(pokemon, dict):
                px, py, pz = int(pokemon["x"]), int(pokemon["y"]), int(pokemon.get("z", player[2]))
            else:
                px, py, pz = int(pokemon[0]), int(pokemon[1]), int(pokemon[2])
        except (KeyError, TypeError, ValueError, IndexError):
            return False
        if pz != player[2]:
            return False
        distance = max(abs(player[0] - px), abs(player[1] - py))
        if self._dynamic_waiting:
            if distance <= self.dynamic_lure_resume_distance:
                log.info("Lure dinamico: Pokemon a %d SQM; retomando rota", distance)
                self._end_dynamic_lure()
                return False
            if self._dynamic_move_command(now, player):
                return True
            if self.dynamic_lure_chase:
                self._set_detour_target(px, py, pz)
                return False
            self._release_all_keys()
            return True
        if distance >= self.dynamic_lure_max_distance:
            self._dynamic_waiting = True
            self._dynamic_started = now
            self._next_dynamic_command_at = 0.0
            log.info("Lure dinamico: Pokemon a %d SQM (limite %d); %s", distance,
                     self.dynamic_lure_max_distance,
                     "chamando com move" if self.dynamic_lure_use_move_command
                     else "indo buscar" if self.dynamic_lure_chase else "aguardando")
            if self._dynamic_move_command(now, player):
                return True
            if self.dynamic_lure_chase:
                self.detour_to(px, py, pz)
                return False
            self._release_all_keys()
            return True
        return False

    def _dynamic_move_command(self, now: float, player: Tuple[int, int, int]) -> bool:
        if not self.dynamic_lure_use_move_command or self.pokemon_move_command_provider is None:
            return False
        if not self.dynamic_lure_move_command_verified:
            if not self._dynamic_move_unverified_warned:
                log.info("Lure dinamico: comando move nao validado neste cliente; "
                         "usando apenas espera/chase")
                self._dynamic_move_unverified_warned = True
            return False
        if now < self._next_dynamic_command_at:
            self._release_all_keys()
            return True
        try:
            sent = bool(self.pokemon_move_command_provider(player))
        except Exception:  # noqa: BLE001 - envio de chat pode falhar
            sent = False
        if not sent:
            self._next_dynamic_command_at = now + 0.5
            return False
        jitter = random.uniform(0.85, 1.15)
        self._next_dynamic_command_at = now + self.dynamic_lure_move_cooldown * jitter
        self._release_all_keys()
        return True

    def _set_detour_target(self, x: int, y: int, z: int) -> None:
        waypoint = self._waypoint(x, y, z)
        if self._detour_saved is None:
            self.detour_to(x, y, z)
            return
        self.waypoints = [waypoint]
        self._detour_target = (int(x), int(y), int(z))

    def _end_dynamic_lure(self) -> None:
        if not self._dynamic_waiting and self._detour_saved is None:
            return
        self._dynamic_waiting = False
        self._dynamic_started = 0.0
        self._next_dynamic_command_at = 0.0
        self.restore_route()
        self._release_all_keys()
        self._reset_progress()

    def start_nearest(self, position: Optional[Tuple[int, int, int]] = None) -> bool:
        with self._route_lock:
            if position is None:
                position = self.read_player_position()
            if position is None or not self.waypoints:
                return False
            curr_x, curr_y, curr_z = position
            candidates = [index for index, waypoint in enumerate(self.waypoints)
                          if waypoint.get("action", "Walk") not in FLOOR_SENSITIVE_ACTIONS
                          or waypoint["z"] == curr_z]
            if not candidates:
                return False
            nearest = min(candidates, key=lambda index: (
                abs(self.waypoints[index]["x"] - curr_x)
                + abs(self.waypoints[index]["y"] - curr_y)))
            self._reset_motion()
            self.waypoints[:] = self.waypoints[nearest:] + self.waypoints[:nearest]
            return True

    def start_recording(self, clear_existing: bool = True) -> None:
        """Inicia a gravação da Hunt, soltando teclas e capturando o tile inicial."""
        with self._route_lock:
            self._reset_motion()
            if clear_existing:
                self.waypoints.clear()
            self._record_position = None
            self._record_direction = None
            self._record_candidate = None
            self._record_start_count = len(self.waypoints)
            self.recording = True
            self.record_step_if_moved()
        log.info("Gravação de Hunt iniciada.")

    def stop_recording(self) -> None:
        """Finaliza a gravação da Hunt."""
        with self._route_lock:
            if self.recording:
                self.record_step_if_moved()
                if self._record_position is not None:
                    self._append_recorded(self._record_position)
            self.recording = False
            added = len(self.waypoints) - getattr(self, "_record_start_count", len(self.waypoints))
            if self._record_position is None:
                log.warning("Gravação finalizada sem posição confirmada; verifique a leitura de X/Y/Z.")
            log.info("Gravação de Hunt finalizada: %d novos waypoints; %d no total.",
                     added, len(self.waypoints))

    def _append_recorded(self, position: Tuple[int, int, int]) -> Optional[Dict[str, Any]]:
        axes = ("x", "y", "z") if self.record_action in FLOOR_SENSITIVE_ACTIONS else ("x", "y")
        if self.waypoints and position[:len(axes)] == tuple(self.waypoints[-1][axis] for axis in axes):
            return None
        waypoint = self._waypoint(*position, self.record_action, self.record_param)
        self.waypoints.append(waypoint)
        return dict(waypoint)

    def record_step_if_moved(self) -> Optional[Dict[str, Any]]:
        with self._route_lock:
            if not self.recording:
                return None
            position = self.read_player_position()
            if position is None:
                self._record_candidate = None
                return None
            if position[0] == 0 and position[1] == 0:
                return None
            previous = self._record_position
            if previous is not None and max(abs(position[0] - previous[0]),
                                            abs(position[1] - previous[1])) > 20:
                log.debug("Amostra descartada: salto maior que 20 SQM, %s -> %s", previous, position)
                return None
            now = time.monotonic()
            dimensions = 3 if self.record_action in FLOOR_SENSITIVE_ACTIONS else 2
            if self._record_candidate is None or position[:dimensions] != self._record_candidate[:dimensions]:
                self._record_candidate = position
                self._record_candidate_since = now
            if now - self._record_candidate_since < self.record_stable_ms / 1000.0:
                return None
            previous = self._record_position
            self._record_position = position
            if previous is None or not self.waypoints:
                return self._append_recorded(position)
            if position[:dimensions] == previous[:dimensions]:
                return None
            delta_x, delta_y = position[0] - previous[0], position[1] - previous[1]
            direction = ((delta_x > 0) - (delta_x < 0), (delta_y > 0) - (delta_y < 0))
            floor_changed = dimensions == 3 and position[2] != previous[2]
            turned = (not floor_changed and self._record_direction is not None
                      and direction != self._record_direction)
            if turned or floor_changed:
                self._append_recorded(previous)
            self._record_direction = None if floor_changed else direction
            last = self.waypoints[-1]
            distance = abs(position[0] - last["x"]) + abs(position[1] - last["y"])
            if floor_changed or turned or distance >= self.record_sqm_interval:
                return self._append_recorded(position)
            return None

    @staticmethod
    def is_valid_position(x, y, z) -> bool:
        return is_valid_position(x, y, z)

    def read_player_position(self) -> Optional[Tuple[int, int, int]]:
        with self._route_lock:
            try:
                values = []
                for axis in "xyz":
                    value = self.read("player." + axis)
                    if value is None:
                        reason = getattr(self.offsets, "last_error", None)
                        self._last_position_error = reason if isinstance(reason, str) else f"player.{axis} sem leitura"
                        return None
                    values.append(value)
                position = tuple(values)
            except Exception as error:
                self._last_position_error = str(error)
                log.debug("Falha temporária ao ler a posição: %s", error)
                return None
            if not self.is_valid_position(*position):
                self._last_position_error = f"Coordenadas inválidas: {position}"
                return None
            self._last_position_error = ""
            return position

    def _reset_position_retries(self) -> None:
        self._position_retry_count = 0
        self._next_position_read_at = 0.0
        self._position_retry_warned = False
        self._blindness_warned = False
        self._last_position_error = ""

    def _read_route_position(self, now: float) -> Optional[Tuple[int, int, int]]:
        if now < self._next_position_read_at:
            return None
        position = self.read_player_position()
        if position is not None and position[:2] == (0, 0):
            self._last_position_error = "Posição (0, 0) recusada durante a execução da rota"
            position = None
        if position is not None:
            self._reset_position_retries()
            return position
        self._position_retry_count += 1
        if self._position_retry_count >= 5:
            if not self._position_retry_warned:
                log.warning("Posição indisponível após 5 tentativas: %s", self._last_position_error)
                self._position_retry_warned = True
            self._position_retry_count = 0
            self._next_position_read_at = now + 0.05
        else:
            self._next_position_read_at = now + 0.05 * 2 ** (self._position_retry_count - 1)
        return None

    def _reset_progress(self) -> None:
        self._last_motion_position = None
        self._last_move_time = 0.0

    def _check_movement_progress(self, position: Tuple[int, int], now: float) -> None:
        if not self._pressed_keys:
            self._reset_progress()
            return
        if position != self._last_motion_position:
            self._last_motion_position = position
            self._last_move_time = now
        elif now - self._last_move_time >= 2.0:
            self._watchdog_attempts += 1
            self._watchdog_recover_at = now + 3.0
            log.warning(
                "WATCHDOG: Personagem preso, soltando teclas (tentativa %d/2); "
                "waypoint preservado: %s", self._watchdog_attempts,
                self.waypoints[0] if self.waypoints else None)
            self._set_pause_reason("watchdog", True)
            self.walker_state = WalkerState.IDLE

    def _maybe_recover_watchdog(self, now: float) -> bool:
        """Retorna True se o watchdog ainda bloqueia a rota."""
        if self._watchdog_attempts >= 2:
            return True
        if now < self._watchdog_recover_at:
            return True
        log.info("WATCHDOG: retomando o mesmo waypoint apos pausa (%s)",
                 self.waypoints[0] if self.waypoints else None)
        self._set_pause_reason("watchdog", False)
        self._watchdog_recover_at = 0.0
        self._reset_progress()
        return False

    def tick(self) -> None:
        with self._route_lock:
            if self._stop_event.is_set():
                self._reset_motion()
                return
            self._tick_route()

    @property
    def combat_paused(self) -> bool:
        with self._route_lock:
            return "combat" in self._pause_reasons

    def pause_for_combat(self) -> None:
        """Freia a rota e solta as teclas enquanto o Target combate."""
        with self._route_lock:
            self._set_pause_reason("combat", True)

    def set_user_paused(self, paused: bool) -> None:
        with self._route_lock:
            self._set_pause_reason("user", bool(paused))
            if not paused:
                self._watchdog_attempts = 0
                self._watchdog_recover_at = 0.0
                self._set_pause_reason("watchdog", False)

    def resume_route(self) -> None:
        """Libera a rota apos o combate."""
        with self._route_lock:
            self._set_pause_reason("combat", False)

    def _tick_route(self) -> None:
        now = time.monotonic()
        if "user" in self._pause_reasons:
            self._release_all_keys()
            return
        if "watchdog" in self._pause_reasons:
            if self._maybe_recover_watchdog(now):
                self._release_all_keys()
                return
        if not self.recording and self.waypoints:
            self._distance_guard_tick()
        blocked = self._pause_reasons - ({"combat"} if self.detour_active else set())
        if blocked:
            self._release_all_keys()
            return

        if self.recording:
            self._release_all_keys()
            self.record_step_if_moved()
            return

        if not self.waypoints:
            self._reset_motion()
            return

        if now < self._next_move_delay_until:
            # Fallback se jitter travar por mais de 1 segundo
            if self._next_move_delay_until - now > 1.0:
                log.warning("[WALKER] Jitter excessivo detectado (%.2fs restante). Resetando para MOVING.", self._next_move_delay_until - now)
                self._next_move_delay_until = 0.0
            else:
                self._release_all_keys()
                return

        if self._dynamic_lure_tick(now):
            return

        if self.walker_state == WalkerState.WAITING:
            if time.monotonic() >= self.wait_until:
                self.wait_until = 0.0
                self._advance_waypoint()
            return

        pos = self._read_route_position(now)
        if pos is None:
            self._reset_progress()
            self._invalid_position_ticks += 1
            if self._position_lost_since is None:
                self._position_lost_since = now
            if self._invalid_position_ticks > 10 or now - self._position_lost_since >= 0.5:
                if not self._blindness_warned:
                    log.warning("Posição indisponível há %.2fs (%d ticks); soltando teclas: %s",
                                now - self._position_lost_since, self._invalid_position_ticks, self._last_position_error)
                    self._blindness_warned = True
                self._release_all_keys()
            return

        self._invalid_position_ticks = 0
        self._position_lost_since = None

        curr_x, curr_y, curr_z = pos
        target = self.waypoints[0]
        tx, ty, tz = target["x"], target["y"], target["z"]

        dx = tx - curr_x
        dy = ty - curr_y

        action = target.get("action", "Walk")
        tolerance = self.arrival_tolerance if action == "Walk" else 0

        log.debug(
            "[WALKER TICK] State: %s | Pos: (%s, %s, %s) | Target: (%s, %s, %s) | dx: %s, dy: %s | Tol: %s",
            self.walker_state.value,
            curr_x,
            curr_y,
            curr_z,
            tx,
            ty,
            tz,
            dx,
            dy,
            tolerance,
        )

        if abs(dx) <= tolerance and abs(dy) <= tolerance:
            self._release_all_keys()
            if action in FLOOR_SENSITIVE_ACTIONS and curr_z != tz:
                self.walker_state = WalkerState.IDLE
                return
            log.info("Chegou no Waypoint (%d, %d, %d). Tolerância usada: %d", tx, ty, tz, tolerance)
            if action == "Wait":
                self.wait_until = time.monotonic() + max(0, int(target.get("param", "0"))) / 1000.0
                self.walker_state = WalkerState.WAITING
            elif action == "Say":
                if self.bus.send_text(target.get("param", ""), module=self.name):
                    self._advance_waypoint()
            else:
                self._advance_waypoint()
            return

        self.walker_state = WalkerState.MOVING
        self._apply_axis_movement(dx, dy)
        self._check_movement_progress((curr_x, curr_y), now)

    def _advance_waypoint(self) -> None:
        self._reset_progress()
        reached = self.waypoints.pop(0)
        delay = random.uniform(0.05, 0.20)
        self._next_move_delay_until = time.monotonic() + delay
        log.debug("Jitter ativo: aguardando %.3fs antes do próximo movimento.", delay)
        if self.loop:
            self.waypoints.append(reached)
        self.walker_state = WalkerState.MOVING if self.waypoints else WalkerState.IDLE

    def _reset_motion(self) -> None:
        self._reset_position_retries()
        self._reset_progress()
        self._next_move_delay_until = 0.0
        self._invalid_position_ticks = 0
        self._position_lost_since = None
        self.walker_state = WalkerState.IDLE
        self.wait_until = 0.0
        self._release_all_keys()

    def _apply_axis_movement(self, dx: int, dy: int) -> None:
        desired = None
        if dx != 0:
            desired = self.movement_keys["east" if dx > 0 else "west"]
        elif dy != 0:
            desired = self.movement_keys["south" if dy > 0 else "north"]
        for key in tuple(self._pressed_keys):
            if key != desired:
                self.bus.key_up(key)
                self._pressed_keys.discard(key)
        delivery_status = getattr(self.bus, "movement_key_active", None)
        if desired in self._pressed_keys and callable(delivery_status):
            if delivery_status(desired) is False:
                self._pressed_keys.discard(desired)
        if desired is not None and desired not in self._pressed_keys:
            if self.bus.key_down(desired):
                self._pressed_keys.add(desired)

    def _release_all_keys(self) -> None:
        for vk in tuple(self._pressed_keys):
            if vk in self._pressed_keys:
                self.bus.key_up(vk)
                self._pressed_keys.discard(vk)

    def teardown(self) -> None:
        with self._route_lock:
            self._pause_reasons.clear()
            self._watchdog_attempts = 0
            self._watchdog_recover_at = 0.0
            self._clear_detour()
            self._reset_motion()

    def request_stop(self) -> None:
        self._stop_event.set()
        with self._route_lock:
            self._clear_detour()
            if hasattr(self.bus, "cancel_text"):
                self.bus.cancel_text(module=self.name)
            self.recording = False
            self._reset_motion()

    def stop(self, *args: Any, **kwargs: Any) -> Any:
        self.request_stop()
        return super().stop(*args, **kwargs)
