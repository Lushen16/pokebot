from unittest.mock import MagicMock, patch
from modules.walker import VK_A, VK_D, VK_S, VK_W, Walker


def make_walker(positions):
    """
    Cria uma instância de Walker com `self.read` e `self.bus` mockados,
    simulando a evolução das coordenadas a cada passo.
    """
    state = {"step": 0, "positions": list(positions)}

    with patch.object(Walker, "__init__", lambda self, *a, **kw: None):
        walker = Walker()

    walker.waypoints = []
    walker._pressed_keys = set()
    walker.bus = MagicMock()
    walker.running = False

    def fake_read(key: str):
        pos = state["positions"][state["step"]]
        if pos is None:
            return None
        x, y, z = pos
        if key == "player.x":
            return x
        if key == "player.y":
            return y
        if key == "player.z":
            return z
        return None

    walker.read = MagicMock(side_effect=fake_read)
    return walker, state


def test_waypoint_queue_methods():
    walker, _ = make_walker([(100, 200, 7)])

    walker.add_waypoint(105, 200, 7)
    walker.add_waypoint(110, 205, 7)
    assert len(walker.waypoints) == 2
    assert walker.waypoints[0] == {"x": 105, "y": 200, "z": 7}

    walker.goto(300, 400, 7)
    assert len(walker.waypoints) == 1
    assert walker.waypoints[0] == {"x": 300, "y": 400, "z": 7}

    walker.clear_waypoints()
    assert len(walker.waypoints) == 0


def test_walker_only_arrives_when_dx_and_dy_are_zero():
    timeline = [
        (100, 200, 7),  # Passo 0: Alvo (102, 199, 7) -> dx=+2 (D), dy=-1 (W)
        (101, 199, 7),  # Passo 1: dx=+1, dy=0 -> Solta W, mas MANTÉM D (não pode parar em dx=1!)
        (102, 199, 7),  # Passo 2: dx=0, dy=0 -> Chegou exatamente no tile! Solta tudo e remove WP.
    ]
    walker, state = make_walker(timeline)
    walker.add_waypoint(102, 199, 7)

    # Passo 0: Segura D e W
    state["step"] = 0
    walker.tick()
    walker.bus.key_down.assert_any_call(VK_D)
    walker.bus.key_down.assert_any_call(VK_W)
    assert len(walker.waypoints) == 1

    # Passo 1: Está a 1 tile (dx=1, dy=0). Deve soltar W, continuar segurando D e NÃO remover o waypoint
    walker.bus.reset_mock()
    state["step"] = 1
    walker.tick()
    walker.bus.key_up.assert_called_once_with(VK_W)
    assert VK_D in walker._pressed_keys
    assert len(walker.waypoints) == 1

    # Passo 2: Chegou exatamente em (102, 199, 7) -> dx == 0 e dy == 0
    walker.bus.reset_mock()
    state["step"] = 2
    walker.tick()
    walker.bus.key_up.assert_any_call(VK_W)
    walker.bus.key_up.assert_any_call(VK_A)
    walker.bus.key_up.assert_any_call(VK_S)
    walker.bus.key_up.assert_any_call(VK_D)
    assert len(walker._pressed_keys) == 0
    assert len(walker.waypoints) == 0


def test_walker_west_and_south_movement():
    timeline = [
        (100, 200, 7),  # Alvo (98, 202, 7) -> dx=-2 (A), dy=+2 (S)
    ]
    walker, _ = make_walker(timeline)
    walker.add_waypoint(98, 202, 7)

    walker.tick()
    walker.bus.key_down.assert_any_call(VK_A)
    walker.bus.key_down.assert_any_call(VK_S)


def test_memory_read_failure_releases_all_keys():
    timeline = [
        (100, 200, 7),
        None,
    ]
    walker, state = make_walker(timeline)
    walker.add_waypoint(105, 200, 7)

    state["step"] = 0
    walker.tick()
    walker.bus.key_down.assert_called_once_with(VK_D)

    walker.bus.reset_mock()
    state["step"] = 1
    walker.tick()
    walker.bus.key_up.assert_any_call(VK_D)
    assert len(walker._pressed_keys) == 0


def test_teardown_releases_all_keys():
    walker, _ = make_walker([(100, 200, 7)])
    walker.add_waypoint(110, 210, 7)

    walker.tick()
    assert VK_D in walker._pressed_keys
    assert VK_S in walker._pressed_keys

    walker.bus.reset_mock()
    walker.teardown()

    walker.bus.key_up.assert_any_call(VK_W)
    walker.bus.key_up.assert_any_call(VK_A)
    walker.bus.key_up.assert_any_call(VK_S)
    walker.bus.key_up.assert_any_call(VK_D)
    assert len(walker._pressed_keys) == 0
