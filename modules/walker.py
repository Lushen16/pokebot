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
        record_stable_ms: int = 300,
        arrival_tolerance: int = 1,
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
        self.record_action = "Walk"
        self.record_param = ""
        self._record_position = None
        self._record_direction = None
        self.record_stable_ms = max(0, int(record_stable_ms))
        self._record_candidate = None
        self._record_candidate_since = 0.0
        self._invalid_position_ticks = 0
        self._position_lost_since = None
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
        self._waypoint(0, 0, 0, action, param)
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

    def clear_waypoints(self) -> None:
        with self._route_lock:
            self.waypoints.clear()
            self._reset_motion()

    def replace_route(self, waypoints: List[Dict[str, Any]], loop: bool,
                      arrival_tolerance: Optional[int] = None) -> None:
        validated = [self._waypoint(**waypoint) for waypoint in waypoints]
        if arrival_tolerance is not None:
            self.validate_arrival_tolerance(arrival_tolerance)
        with self._route_lock:
            self._reset_motion()
            self.recording = False
            self.waypoints[:] = validated
            self.loop = bool(loop)
            if arrival_tolerance is not None:
                self.arrival_tolerance = arrival_tolerance

    def route_snapshot(self) -> Dict[str, Any]:
        with self._route_lock:
            return {"waypoints": self.waypoint_snapshot(), "loop": self.loop,
                    "arrival_tolerance": self.arrival_tolerance}

    def goto(self, x: int, y: int, z: int) -> None:
        with self._route_lock:
            self._reset_motion()
            self.waypoints = [self._waypoint(x, y, z)]

    def start_nearest(self, position: Optional[Tuple[int, int, int]] = None) -> bool:
        with self._route_lock:
            if position is None:
                position = self.read_player_position()
            if position is None or not self.waypoints:
                return False
            curr_x, curr_y, curr_z = position
            candidates = [index for index, waypoint in enumerate(self.waypoints)
                          if waypoint["z"] == curr_z]
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
            log.info("Gravação de Hunt finalizada com %d waypoints.", len(self.waypoints))

    def _append_recorded(self, position: Tuple[int, int, int]) -> Optional[Dict[str, Any]]:
        if self.waypoints and position == tuple(self.waypoints[-1][axis] for axis in ("x", "y", "z")):
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
            now = time.monotonic()
            if position != self._record_candidate:
                self._record_candidate = position
                self._record_candidate_since = now
            if now - self._record_candidate_since < self.record_stable_ms / 1000.0:
                return None
            previous = self._record_position
            self._record_position = position
            if previous is None or not self.waypoints:
                return self._append_recorded(position)
            if position == previous:
                return None
            delta_x, delta_y = position[0] - previous[0], position[1] - previous[1]
            direction = ((delta_x > 0) - (delta_x < 0), (delta_y > 0) - (delta_y < 0))
            floor_changed = position[2] != previous[2]
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
        try:
            position = tuple(self.read("player." + axis) for axis in "xyz")
        except Exception:
            log.debug("Falha temporária ao ler a posição", exc_info=True)
            return None
        return position if self.is_valid_position(*position) else None

    def tick(self) -> None:
        with self._route_lock:
            if self._stop_event.is_set():
                self._reset_motion()
                return
            self._tick_route()

    def _tick_route(self) -> None:
        if self.recording:
            self._release_all_keys()
            self.record_step_if_moved()
            return

        if not self.waypoints:
            self._reset_motion()
            return

        if time.monotonic() < self._next_move_delay_until:
            self._release_all_keys()
            return

        if self.walker_state == WalkerState.WAITING:
            if time.monotonic() >= self.wait_until:
                self.wait_until = 0.0
                self._advance_waypoint()
            return

        pos = self.read_player_position()
        if pos is None:
            now = time.monotonic()
            self._invalid_position_ticks += 1
            if self._position_lost_since is None:
                self._position_lost_since = now
            if self._invalid_position_ticks > 10 or now - self._position_lost_since >= 0.5:
                self._release_all_keys()
            return

        self._invalid_position_ticks = 0
        self._position_lost_since = None

        curr_x, curr_y, curr_z = pos
        target = self.waypoints[0]
        tx, ty, tz = target["x"], target["y"], target["z"]

        if curr_z != tz:
            self.walker_state = WalkerState.IDLE
            self._release_all_keys()
            return

        dx = tx - curr_x
        dy = ty - curr_y

        action = target.get("action", "Walk")
        tolerance = self.arrival_tolerance if action == "Walk" else 0
        if abs(dx) <= tolerance and abs(dy) <= tolerance:
            self._release_all_keys()
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

    def _advance_waypoint(self) -> None:
        reached = self.waypoints.pop(0)
        self._next_move_delay_until = time.monotonic() + random.uniform(0.05, 0.20)
        if self.loop:
            self.waypoints.append(reached)
        self.walker_state = WalkerState.MOVING if self.waypoints else WalkerState.IDLE

    def _reset_motion(self) -> None:
        self._next_move_delay_until = 0.0
        self._invalid_position_ticks = 0
        self._position_lost_since = None
        self.walker_state = WalkerState.IDLE
        self.wait_until = 0.0
        self._release_all_keys()

    def _apply_axis_movement(self, dx: int, dy: int) -> None:
        desired = None
        if dx != 0:
            desired = VK_D if dx > 0 else VK_A
        elif dy != 0:
            desired = VK_S if dy > 0 else VK_W
        for key in tuple(self._pressed_keys):
            if key != desired:
                self.bus.key_up(key)
                self._pressed_keys.discard(key)
        if desired is not None and desired not in self._pressed_keys:
            if self.bus.key_down(desired):
                self._pressed_keys.add(desired)

    def _release_all_keys(self) -> None:
        for vk in ALL_MOVEMENT_KEYS:
            if vk in self._pressed_keys:
                self.bus.key_up(vk)
                self._pressed_keys.discard(vk)

    def teardown(self) -> None:
        with self._route_lock:
            self._reset_motion()

    def request_stop(self) -> None:
        self._stop_event.set()
        with self._route_lock:
            if hasattr(self.bus, "cancel_text"):
                self.bus.cancel_text(module=self.name)
            self.recording = False
            self._reset_motion()

    def stop(self, *args: Any, **kwargs: Any) -> Any:
        self.request_stop()
        return super().stop(*args, **kwargs)
