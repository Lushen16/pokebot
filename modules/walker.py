from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from modules.base import AutomationModule

log = logging.getLogger("pokebot.modules.walker")

VK_W = 0x57
VK_A = 0x41
VK_S = 0x53
VK_D = 0x44
ALL_MOVEMENT_KEYS: Tuple[int, ...] = (VK_W, VK_A, VK_S, VK_D)


class Walker(AutomationModule):
    """Módulo de movimentação do personagem tile a tile usando WASD, com gravação e loop de Hunt."""

    def __init__(
        self,
        memory_reader: Any = None,
        offset_registry: Any = None,
        input_bus: Any = None,
        poll_interval_sec: float = 0.05,
        loop: bool = True,
        record_min_distance: int = 1,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        memory = memory_reader if memory_reader is not None else kwargs.pop("memory", None)
        offsets = offset_registry if offset_registry is not None else kwargs.pop("offsets", None)
        bus = input_bus if input_bus is not None else kwargs.pop("bus", None)

        super().__init__(
            memory=memory,
            resolver=offsets,
            bus=bus,
            interval=poll_interval_sec,
            name=kwargs.pop("name", "walker"),
            **kwargs,
        )
        self.waypoints: List[Dict[str, int]] = []
        self._pressed_keys: Set[int] = set()
        self.loop: bool = loop
        self.recording: bool = False
        self.record_min_distance: int = max(1, int(record_min_distance))

    def set_loop(self, enabled: bool) -> None:
        """Ativa ou desativa a repetição contínua da rota."""
        self.loop = bool(enabled)

    def add_waypoint(self, x: int, y: int, z: int) -> None:
        self.waypoints.append({"x": int(x), "y": int(y), "z": int(z)})

    def clear_waypoints(self) -> None:
        self.waypoints.clear()
        self._release_all_keys()

    def goto(self, x: int, y: int, z: int) -> None:
        self._release_all_keys()
        self.waypoints = [{"x": int(x), "y": int(y), "z": int(z)}]

    def start_recording(self, clear_existing: bool = True) -> None:
        """Inicia a gravação da Hunt, soltando teclas e capturando o tile inicial."""
        self._release_all_keys()
        if clear_existing:
            self.waypoints.clear()
        self.recording = True
        self.record_step_if_moved()
        log.info("Gravação de Hunt iniciada.")

    def stop_recording(self) -> None:
        """Finaliza a gravação da Hunt."""
        self.recording = False
        log.info("Gravação de Hunt finalizada com %d waypoints.", len(self.waypoints))

    def record_step_if_moved(self) -> Optional[Dict[str, int]]:
        """
        Lê a posição atual do jogador e adiciona à fila caso seja o primeiro ponto,
        tenha mudado de andar (Z) ou tenha andado pelo menos `record_min_distance` tiles.
        """
        if not self.recording:
            return None

        pos = self.read_player_position()
        if pos is None:
            return None

        curr_x, curr_y, curr_z = pos
        if not self.waypoints:
            wp = {"x": curr_x, "y": curr_y, "z": curr_z}
            self.waypoints.append(wp)
            return wp

        last = self.waypoints[-1]
        dist_x = abs(curr_x - last["x"])
        dist_y = abs(curr_y - last["y"])
        manhattan_dist = dist_x + dist_y

        if curr_z != last["z"] or manhattan_dist >= self.record_min_distance:
            wp = {"x": curr_x, "y": curr_y, "z": curr_z}
            self.waypoints.append(wp)
            return wp

        return None

    def read_player_position(self) -> Optional[Tuple[int, int, int]]:
        x = self.read("player.x")
        y = self.read("player.y")
        z = self.read("player.z")
        if x is None or y is None or z is None:
            return None
        return (int(x), int(y), int(z))

    def tick(self) -> None:
        if self.recording:
            self._release_all_keys()
            self.record_step_if_moved()
            return

        if not self.waypoints:
            self._release_all_keys()
            return

        pos = self.read_player_position()
        if pos is None:
            self._release_all_keys()
            return

        curr_x, curr_y, curr_z = pos
        target = self.waypoints[0]
        tx, ty, tz = target["x"], target["y"], target["z"]

        if curr_z != tz:
            skipped = self.waypoints.pop(0)
            if self.loop:
                self.waypoints.append(skipped)
            self._release_all_keys()
            return

        dx = tx - curr_x
        dy = ty - curr_y

        # REGRA CRÍTICA: Chegada EXATA no tile (dx == 0 e dy == 0)
        if dx == 0 and dy == 0:
            self._release_all_keys()
            reached = self.waypoints.pop(0)
            if self.loop:
                self.waypoints.append(reached)
            return

        self._apply_axis_movement(dx, dy)

    def _apply_axis_movement(self, dx: int, dy: int) -> None:
        desired_x: Optional[int] = None
        if dx > 0:
            desired_x = VK_D
        elif dx < 0:
            desired_x = VK_A

        for vk in (VK_A, VK_D):
            if vk != desired_x and vk in self._pressed_keys:
                self.bus.key_up(vk)
                self._pressed_keys.discard(vk)

        if desired_x is not None and desired_x not in self._pressed_keys:
            self.bus.key_down(desired_x)
            self._pressed_keys.add(desired_x)

        desired_y: Optional[int] = None
        if dy > 0:
            desired_y = VK_S
        elif dy < 0:
            desired_y = VK_W

        for vk in (VK_W, VK_S):
            if vk != desired_y and vk in self._pressed_keys:
                self.bus.key_up(vk)
                self._pressed_keys.discard(vk)

        if desired_y is not None and desired_y not in self._pressed_keys:
            self.bus.key_down(desired_y)
            self._pressed_keys.add(desired_y)

    def _release_all_keys(self) -> None:
        for vk in ALL_MOVEMENT_KEYS:
            if vk in self._pressed_keys:
                self.bus.key_up(vk)
                self._pressed_keys.discard(vk)

    def teardown(self) -> None:
        self._release_all_keys()

    def stop(self, *args: Any, **kwargs: Any) -> Any:
        self.recording = False
        self._release_all_keys()
        return super().stop(*args, **kwargs)
