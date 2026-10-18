from unittest.mock import MagicMock, patch
from modules.walker import VK_A, VK_D, VK_S, VK_W, Walker


def make_walker(positions, loop: bool = False):
    state = {"step": 0, "positions": list(positions)}

    with patch.object(Walker, "__init__", lambda self, *a, **kw: None):
        walker = Walker()

    walker.waypoints = []
    walker._pressed_keys = set()
    walker.bus = MagicMock()
    walker.running = False
    walker.loop = loop
    walker.recording = False
    walker.record_min_distance = 1

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
        (100, 200, 7),
        (101, 199, 7),
        (102, 199, 7),
    ]
    walker, state = make_walker(timeline, loop=False)
    walker.add_waypoint(102, 199, 7)

    state["step"] = 0
    walker.tick()
    walker.bus.key_down.assert_any_call(VK_D)
    walker.bus.key_down.assert_any_call(VK_W)
    assert len(walker.waypoints) == 1

    walker.bus.reset_mock()
    state["step"] = 1
    walker.tick()
    walker.bus.key_up.assert_called_once_with(VK_W)
    assert VK_D in walker._pressed_keys
    assert len(walker.waypoints) == 1

    walker.bus.reset_mock()
    state["step"] = 2
    walker.tick()
    assert len(walker._pressed_keys) == 0
    assert len(walker.waypoints) == 0


def test_walker_west_and_south_movement():
    walker, _ = make_walker([(100, 200, 7)])
    walker.add_waypoint(98, 202, 7)

    walker.tick()
    walker.bus.key_down.assert_any_call(VK_A)
    walker.bus.key_down.assert_any_call(VK_S)


def test_memory_read_failure_releases_all_keys():
    timeline = [(100, 200, 7), None]
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
    assert len(walker._pressed_keys) == 0


def test_walker_record_hunt_captures_movement_only():
    timeline = [
        (100, 200, 7),  # Ponto inicial ao clicar em Gravar
        (100, 200, 7),  # Parado no mesmo lugar (não deve duplicar)
        (101, 200, 7),  # Andou 1 tile para Leste
        (101, 201, 7),  # Andou 1 tile para Sul
    ]
    walker, state = make_walker(timeline, loop=True)

    state["step"] = 0
    walker.start_recording(clear_existing=True)
    assert walker.recording is True
    assert walker.waypoints == [{"x": 100, "y": 200, "z": 7}]

    # Continua parado no mesmo tile: não adiciona duplicata
    state["step"] = 1
    walker.tick()
    assert len(walker.waypoints) == 1

    # Moveu para (101, 200, 7)
    state["step"] = 2
    walker.tick()
    assert len(walker.waypoints) == 2
    assert walker.waypoints[-1] == {"x": 101, "y": 200, "z": 7}

    # Moveu para (101, 201, 7)
    state["step"] = 3
    walker.tick()
    assert len(walker.waypoints) == 3
    assert walker.waypoints[-1] == {"x": 101, "y": 201, "z": 7}

    walker.stop_recording()
    assert walker.recording is False


def test_walker_loop_rotates_waypoints_continuously():
    timeline = [
        (100, 200, 7),  # Já está no WP1 -> rotaciona WP1 pro fim, alvo vira WP2
        (101, 200, 7),  # Chegou no WP2 -> rotaciona WP2 pro fim, alvo volta a ser WP1
    ]
    walker, state = make_walker(timeline, loop=True)
    walker.add_waypoint(100, 200, 7)
    walker.add_waypoint(101, 200, 7)

    # Tick 0: Alcança WP1 (100, 200, 7), que vai para o fim da fila
    state["step"] = 0
    walker.tick()
    assert len(walker.waypoints) == 2
    assert walker.waypoints[0] == {"x": 101, "y": 200, "z": 7}
    assert walker.waypoints[1] == {"x": 100, "y": 200, "z": 7}

    # Tick 1: Alcança WP2 (101, 200, 7), que também vai para o fim da fila
    state["step"] = 1
    walker.tick()
    assert len(walker.waypoints) == 2
    assert walker.waypoints[0] == {"x": 100, "y": 200, "z": 7}
    assert walker.waypoints[1] == {"x": 101, "y": 200, "z": 7}
