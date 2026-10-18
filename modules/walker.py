import logging
from typing import Any, Dict, List, Optional, Set, Tuple
from modules.base import AutomationModule

logger = logging.getLogger("PokeBot.Walker")

VK_W = 0x57
VK_A = 0x41
VK_S = 0x53
VK_D = 0x44
ALL_MOVEMENT_KEYS: Tuple[int, ...] = (VK_W, VK_A, VK_S, VK_D)


class Walker(AutomationModule):
    """
    Módulo Cavebot / Walker baseado em coordenadas (X, Y, Z) e input contínuo WASD.
    Utiliza `self.read()` e `self.bus` providos por `AutomationModule`.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        memory = kwargs.pop("memory_reader", None)
        offsets = kwargs.pop("offset_registry", None)
        bus = kwargs.pop("input_bus", None)
        if "bus" not in kwargs and bus is not None:
            kwargs["bus"] = bus

        if len(args) == 0:
            super().__init__(memory=memory, offsets=offsets, name="walker", **kwargs)
        else:
            super().__init__(*args, **kwargs)

        self.waypoints: List[Dict[str, int]] = []
        self._pressed_keys: Set[int] = set()

    def add_waypoint(self, x: int, y: int, z: int) -> None:
        """Adiciona um waypoint ao final da fila."""
        waypoint = {"x": int(x), "y": int(y), "z": int(z)}
        self.waypoints.append(waypoint)
        logger.info("Waypoint adicionado: %s (Total na fila: %d)", waypoint, len(self.waypoints))

    def clear_waypoints(self) -> None:
        """Limpa a fila de waypoints e solta todas as teclas de movimento."""
        self.waypoints.clear()
        self._release_all_keys()
        logger.info("Fila de waypoints limpa.")

    def goto(self, x: int, y: int, z: int) -> None:
        """Limpa a fila atual e define um único waypoint como destino imediato."""
        self.clear_waypoints()
        self.add_waypoint(x, y, z)
        logger.info("Goto definido para (%d, %d, %d).", x, y, z)

    def read_player_position(self) -> Optional[Tuple[int, int, int]]:
        """
        Lê (player.x, player.y, player.z) usando `self.read()` da classe base.
        Retorna None e loga o erro se qualquer leitura falhar.
        """
        try:
            x = self.read("player.x")
            y = self.read("player.y")
            z = self.read("player.z")

            if x is None or y is None or z is None:
                logger.error(
                    "Falha ao ler coordenadas do player (x=%s, y=%s, z=%s).",
                    x,
                    y,
                    z,
                )
                return None

            return (int(x), int(y), int(z))
        except Exception as exc:
            logger.exception("Exceção ao ler coordenadas do player: %s", exc)
            return None

    def _hold_key(self, vk_code: int) -> None:
        """Segura a tecla via self.bus.key_down caso ainda não esteja pressionada."""
        if vk_code not in self._pressed_keys:
            try:
                self.bus.key_down(vk_code)
                self._pressed_keys.add(vk_code)
            except Exception as exc:
                logger.error("Erro ao executar key_down(0x%X): %s", vk_code, exc)

    def _release_key(self, vk_code: int) -> None:
        """Solta a tecla via self.bus.key_up caso esteja pressionada."""
        if vk_code in self._pressed_keys:
            try:
                self.bus.key_up(vk_code)
            except Exception as exc:
                logger.error("Erro ao executar key_up(0x%X): %s", vk_code, exc)
            finally:
                self._pressed_keys.discard(vk_code)

    def _release_all_keys(self) -> None:
        """Garante que W, A, S e D sejam todas soltas no InputBus."""
        for vk_code in ALL_MOVEMENT_KEYS:
            try:
                self.bus.key_up(vk_code)
            except Exception as exc:
                logger.error("Erro ao liberar tecla 0x%X: %s", vk_code, exc)
        self._pressed_keys.clear()

    def _apply_axis_movement(self, dx: int, dy: int) -> None:
        """
        Controle de Teclas (WASD) por eixo:
        - Se dx > 0: solta A e segura D. Se dx < 0: solta D e segura A. Se dx == 0: solta A e D.
        - Se dy > 0: solta W e segura S. Se dy < 0: solta S e segura W. Se dy == 0: solta W e S.
        """
        if dx > 0:
            self._release_key(VK_A)
            self._hold_key(VK_D)
        elif dx < 0:
            self._release_key(VK_D)
            self._hold_key(VK_A)
        else:
            self._release_key(VK_A)
            self._release_key(VK_D)

        if dy > 0:
            self._release_key(VK_W)
            self._hold_key(VK_S)
        elif dy < 0:
            self._release_key(VK_S)
            self._hold_key(VK_W)
        else:
            self._release_key(VK_W)
            self._release_key(VK_S)

    def tick(self) -> None:
        """Loop principal de locomoção executado a cada ciclo da thread."""
        pos = self.read_player_position()
        if pos is None:
            self._release_all_keys()
            return

        if not self.waypoints:
            if self._pressed_keys:
                self._release_all_keys()
            return

        player_x, player_y, player_z = pos
        target = self.waypoints[0]

        if player_z != target["z"]:
            logger.warning(
                "Andar diferente do waypoint alvo (player.z=%d, target.z=%d). Soltando teclas.",
                player_z,
                target["z"],
            )
            self._release_all_keys()
            return

        dx = target["x"] - player_x
        dy = target["y"] - player_y

        # Chegada exata no tile (dx == 0 e dy == 0)
        if dx == 0 and dy == 0:
            self._release_all_keys()
            reached = self.waypoints.pop(0)
            logger.info(
                "Waypoint alcançado: %s. Restantes na fila: %d",
                reached,
                len(self.waypoints),
            )
            return

        self._apply_axis_movement(dx, dy)

    def teardown(self) -> None:
        """Garante que todas as teclas (W, A, S, D) sejam soltas ao parar o módulo."""
        self._release_all_keys()
        logger.info("Teardown do Walker concluído: teclas W, A, S, D liberadas.")

    def stop(self, timeout: float = 5.0) -> bool:
        """Interrompe o módulo e garante a liberação imediata das teclas."""
        res = super().stop(timeout=timeout)
        self.teardown()
        return res
