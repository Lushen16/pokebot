from unittest.mock import MagicMock, patch, call
import threading
from types import SimpleNamespace

import pytest

from modules.walker import VK_A, VK_D, VK_S, VK_W, Walker, WalkerState


def make_walker(positions, loop: bool = False):
    state = {"step": 0, "positions": list(positions)}

    walker = Walker(input_bus=MagicMock(), loop=loop, record_sqm_interval=1, record_stable_ms=0,
                    arrival_tolerance=0)

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
    assert walker.waypoints[0] == {"x": 105, "y": 200, "z": 7, "action": "Walk", "param": ""}

    walker.goto(300, 400, 7)
    assert len(walker.waypoints) == 1
    assert walker.waypoints[0] == {"x": 300, "y": 400, "z": 7, "action": "Walk", "param": ""}

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
    walker.bus.key_down.assert_called_once_with(VK_D)
    assert len(walker.waypoints) == 1

    walker.bus.reset_mock()
    state["step"] = 1
    walker.tick()
    walker.bus.key_up.assert_not_called()
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
    walker.bus.key_down.assert_called_once_with(VK_A)


def test_memory_read_failure_keeps_keys_during_grace_period():
    timeline = [(100, 200, 7), None]
    walker, state = make_walker(timeline)
    walker.add_waypoint(105, 200, 7)

    state["step"] = 0
    walker.tick()
    walker.bus.key_down.assert_called_once_with(VK_D)

    walker.bus.reset_mock()
    state["step"] = 1
    walker.tick()
    walker.bus.key_up.assert_not_called()
    assert walker._pressed_keys == {VK_D}


def test_teardown_releases_all_keys():
    walker, _ = make_walker([(100, 200, 7)])
    walker.add_waypoint(110, 210, 7)

    walker.tick()
    assert VK_D in walker._pressed_keys
    assert VK_S not in walker._pressed_keys

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
    assert walker.waypoints == [{"x": 100, "y": 200, "z": 7, "action": "Walk", "param": ""}]

    # Continua parado no mesmo tile: não adiciona duplicata
    state["step"] = 1
    walker.tick()
    assert len(walker.waypoints) == 1

    # Moveu para (101, 200, 7)
    state["step"] = 2
    walker.tick()
    assert len(walker.waypoints) == 2
    assert walker.waypoints[-1] == {"x": 101, "y": 200, "z": 7, "action": "Walk", "param": ""}

    # Moveu para (101, 201, 7)
    state["step"] = 3
    walker.tick()
    assert len(walker.waypoints) == 3
    assert walker.waypoints[-1] == {"x": 101, "y": 201, "z": 7, "action": "Walk", "param": ""}

    walker.stop_recording()
    assert walker.recording is False


@patch("modules.walker.time.monotonic", side_effect=[10.0, 10.0, 11.0, 11.0])
def test_walker_loop_rotates_waypoints_continuously(clock):
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
    assert walker.waypoints[0] == {"x": 101, "y": 200, "z": 7, "action": "Walk", "param": ""}
    assert walker.waypoints[1] == {"x": 100, "y": 200, "z": 7, "action": "Walk", "param": ""}

    # Tick 1: Alcança WP2 (101, 200, 7), que também vai para o fim da fila
    state["step"] = 1
    walker.tick()
    assert len(walker.waypoints) == 2
    assert walker.waypoints[0] == {"x": 100, "y": 200, "z": 7, "action": "Walk", "param": ""}
    assert walker.waypoints[1] == {"x": 101, "y": 200, "z": 7, "action": "Walk", "param": ""}


def test_wait_uses_monotonic_without_sleep():
    walker, _ = make_walker([(100, 200, 7)])
    walker.add_waypoint(100, 200, 7, "Wait", "3000")
    walker.add_waypoint(101, 200, 7)
    with patch("modules.walker.time.monotonic", return_value=10.0) as clock, \
            patch("time.sleep", side_effect=AssertionError("blocking sleep")):
        walker.tick()
        assert walker.walker_state == WalkerState.WAITING
        assert walker.wait_until == 13.0
        clock.return_value = 12.999
        walker.tick()
        assert len(walker.waypoints) == 2
        clock.return_value = 13.0
        walker.tick()
        assert walker.walker_state == WalkerState.MOVING
        assert len(walker.waypoints) == 1
    walker.bus.key_down.assert_not_called()


def test_stop_cancels_long_wait_and_worker_promptly():
    walker, _ = make_walker([(100, 200, 7)])
    walker.add_waypoint(100, 200, 7, "Wait", "3600000")
    walker.tick()
    assert walker.walker_state == WalkerState.WAITING
    assert walker.start()
    assert walker.stop(timeout=0.5)
    assert not walker.is_running
    assert walker.walker_state == WalkerState.IDLE
    assert walker.wait_until == 0
    assert len(walker.waypoints) == 1
    walker.bus.cancel_text.assert_called_once_with(module="walker")
    walker.tick()
    assert walker.walker_state == WalkerState.IDLE


def test_clear_and_goto_reset_pending_wait():
    walker, _ = make_walker([(100, 200, 7)])
    walker.add_waypoint(100, 200, 7, "Wait", "1000")
    walker.tick()
    walker.goto(110, 200, 7)
    assert walker.walker_state == WalkerState.IDLE
    assert walker.wait_until == 0
    walker.tick()
    assert walker.walker_state == WalkerState.MOVING
    walker.clear_waypoints()
    assert walker.walker_state == WalkerState.IDLE
    assert not walker._pressed_keys


def test_say_is_queued_once_and_retries_only_when_queue_rejects():
    walker, _ = make_walker([(100, 200, 7)])
    walker.add_waypoint(100, 200, 7, "Say", "!pokestop")
    walker.bus.send_text.return_value = False
    walker.tick()
    assert len(walker.waypoints) == 1
    walker.bus.send_text.return_value = True
    walker.tick()
    walker.tick()
    assert len(walker.waypoints) == 0
    assert walker.bus.send_text.call_count == 2
    walker.bus.send_text.assert_called_with("!pokestop", module="walker")


def test_start_nearest_rotates_without_losing_order_or_actions():
    walker, _ = make_walker([(109, 200, 7)], loop=True)
    walker.add_waypoint(100, 200, 7)
    walker.add_waypoint(110, 200, 7, "Wait", "1000")
    walker.add_waypoint(120, 200, 7, "Say", "hello")
    original = walker.waypoint_snapshot()
    assert walker.start_nearest()
    assert walker.waypoint_snapshot() == original[1:] + original[:1]
    assert not walker.is_running


def test_nearest_prefers_current_floor_and_missing_position_preserves_route():
    walker, state = make_walker([(100, 200, 7), None, (100, 200, 9)])
    walker.add_waypoint(100, 200, 8)
    walker.add_waypoint(120, 200, 7)
    assert walker.start_nearest()
    original = walker.waypoint_snapshot()
    for step in (1, 2):
        state["step"] = step
        assert not walker.start_nearest()
        assert walker.waypoint_snapshot() == original


def test_record_interval_three_records_only_initial_and_third_tile():
    walker, state = make_walker([(100 + step, 200, 7) for step in range(5)])
    walker.configure_recording(interval=3)
    walker.start_recording()
    for step in range(1, 5):
        state["step"] = step
        walker.tick()
    assert [waypoint["x"] for waypoint in walker.waypoints] == [100, 103]
    walker.stop_recording()
    assert walker.waypoints[-1]["x"] == 104


def test_recording_preserves_corner_before_turn_and_floor_changes():
    walker, state = make_walker([(100, 200, 7), (101, 200, 7), (102, 200, 7),
                                 (102, 201, 7), (102, 201, 8)])
    walker.configure_recording("Say", "!pokestop", interval=10)
    walker.start_recording()
    for step in range(1, 5):
        state["step"] = step
        walker.tick()
    assert [(waypoint["x"], waypoint["y"], waypoint["z"]) for waypoint in walker.waypoints] == [
        (100, 200, 7), (102, 200, 7), (102, 201, 7), (102, 201, 8)]
    assert all(waypoint["action"] == "Say" and waypoint["param"] == "!pokestop"
               for waypoint in walker.waypoints)


@pytest.mark.parametrize("param", ["", "abc", "-1", "1.5"])
def test_invalid_wait_is_rejected_before_mutating_route(param):
    walker, _ = make_walker([(100, 200, 7)])
    with pytest.raises(ValueError):
        walker.add_waypoint(100, 200, 7, "Wait", param)
    assert not walker.waypoints


def test_snapshot_is_independent_and_parallel_recording_has_no_duplicates():
    walker, _ = make_walker([(100, 200, 7)])
    walker.start_recording()
    workers = [threading.Thread(target=walker.record_step_if_moved) for _ in range(20)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=1)
        assert not worker.is_alive()
    assert len(walker.waypoints) == 1
    snapshot = walker.waypoint_snapshot()
    snapshot[0]["x"] = 999
    assert walker.waypoints[0]["x"] == 100


def test_constructor_passes_offset_registry_to_base():
    registry = SimpleNamespace(read_entry=MagicMock(return_value=123))
    walker = Walker(offset_registry=registry, input_bus=MagicMock())
    assert walker.read("player.x") == 123
    registry.read_entry.assert_called_once_with("player.x")


def test_public_motion_state_and_default_recording_interval():
    walker = Walker(input_bus=MagicMock())
    assert walker.state == WalkerState.IDLE
    assert walker.record_sqm_interval == 3
    assert walker.stats["state"] == "stopped"
    assert walker.stats["walker_state"] == "idle"


def test_nearest_accepts_position_without_reading_memory():
    walker, _ = make_walker([None])
    walker.add_waypoint(100, 200, 7)
    walker.add_waypoint(110, 200, 7, "Stair")
    assert walker.start_nearest((109, 200, 7))
    assert walker.waypoints[0]["action"] == "Stair"
    walker.read.assert_not_called()


@pytest.mark.parametrize("action", ["Walk", "Safe Spot", "BOX", "Stair"])
def test_passive_actions_advance_without_sending_chat(action):
    walker, _ = make_walker([(100, 200, 7)])
    walker.add_waypoint(100, 200, 7, action)
    walker.tick()
    assert walker.state == WalkerState.IDLE
    assert not walker.waypoints
    walker.bus.send_text.assert_not_called()


def test_rejected_key_down_is_retried_on_next_tick():
    walker, _ = make_walker([(100, 200, 7)])
    walker.add_waypoint(101, 200, 7)
    walker.bus.key_down.return_value = False
    walker.tick()
    assert VK_D not in walker._pressed_keys
    walker.bus.key_down.return_value = True
    walker.tick()
    assert VK_D in walker._pressed_keys
    assert walker.bus.key_down.call_count == 2



def test_axis_switch_releases_horizontal_before_pressing_vertical():
    walker, state = make_walker([(100, 200, 7), (102, 200, 7), (102, 201, 7)], loop=False)
    walker.add_waypoint(102, 201, 7)
    walker.tick()
    assert walker._pressed_keys == {VK_D}
    walker.bus.reset_mock()
    state["step"] = 1
    walker.tick()
    assert walker.bus.method_calls == [call.key_up(VK_D), call.key_down(VK_S)]
    assert walker._pressed_keys == {VK_S}
    state["step"] = 2
    walker.tick()
    assert not walker._pressed_keys and not walker.waypoints


def test_recording_requires_stable_tile_for_300ms(monkeypatch):
    walker, state = make_walker([(100, 200, 7), (101, 201, 7), (102, 202, 7)])
    walker.record_stable_ms = 300
    clock = [10.0]
    monkeypatch.setattr("modules.walker.time.monotonic", lambda: clock[0])
    walker.start_recording()
    assert walker.waypoints == []
    clock[0] = 10.299
    walker.record_step_if_moved()
    assert walker.waypoints == []
    clock[0] = 10.301
    walker.record_step_if_moved()
    assert len(walker.waypoints) == 1
    state["step"] = 1
    clock[0] = 10.4
    walker.record_step_if_moved()
    state["step"] = 2
    clock[0] = 10.6
    walker.record_step_if_moved()
    clock[0] = 10.8
    walker.record_step_if_moved()
    assert len(walker.waypoints) == 1
    clock[0] = 10.901
    walker.record_step_if_moved()
    assert [(point["x"], point["y"]) for point in walker.waypoints] == [(100, 200), (102, 202)]


def test_stopping_recording_does_not_commit_unstable_position(monkeypatch):
    walker, state = make_walker([(100, 200, 7), (101, 201, 7)])
    walker.record_stable_ms = 300
    clock = [0.0]
    monkeypatch.setattr("modules.walker.time.monotonic", lambda: clock[0])
    walker.start_recording()
    clock[0] = 0.301
    walker.record_step_if_moved()
    state["step"] = 1
    walker.stop_recording()
    assert [(point["x"], point["y"]) for point in walker.waypoints] == [(100, 200)]


def test_route_replacement_is_atomic_and_preserves_original_on_validation_error():
    walker, _ = make_walker([(100, 200, 7)])
    walker.add_waypoint(100, 200, 7)
    original = walker.route_snapshot()
    with pytest.raises(ValueError):
        walker.replace_route([{"x": 1, "y": 2, "z": 7, "action": "Wait", "param": "invalid"}], False)
    assert walker.route_snapshot() == original
    walker.replace_route([{"x": 3, "y": 4, "z": 7}], False)
    assert not walker.loop and walker.waypoints[0]["x"] == 3



@pytest.mark.parametrize("position", [(65535, 65535, 255), (-1, 200, 7), (100, 200, 16)])
def test_invalid_live_coordinates_are_tolerated_and_not_recorded(position):
    walker, state = make_walker([(100, 200, 7), position])
    walker.add_waypoint(105, 200, 7)
    walker.tick()
    state["step"] = 1
    assert walker.read_player_position() is None
    walker.tick()
    assert walker._pressed_keys == {VK_D}
    walker.start_recording()
    assert not walker.waypoints



@pytest.mark.parametrize("position, expected", [((0, 0, 0), True), ((10000, 10000, 15), True),
    ((65535, 200, 7), False), ((100, 65535, 7), False), ((10001, 200, 7), False),
    ((100, 10001, 7), False), ((-1, 200, 7), False), ((100, 200, 255), False),
    ((None, 200, 7), False), ((True, 200, 7), False), ((100.0, 200, 7), False)])
def test_position_sanitizer(position, expected):
    assert Walker.is_valid_position(*position) is expected


def test_eleven_invalid_ticks_release_keys_without_consuming_route(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr("modules.walker.time.monotonic", lambda: clock[0])
    walker, state = make_walker([(100, 200, 7), (65535, 65535, 255), (105, 200, 7)])
    walker.add_waypoint(105, 200, 7)
    walker.tick()
    route = walker.route_snapshot()
    walker.bus.reset_mock()
    state["step"] = 1
    for failure in range(10):
        clock[0] = 10.0 + failure * 0.049
        walker.tick()
    walker.bus.key_up.assert_not_called()
    assert walker._pressed_keys == {VK_D}
    walker.tick()
    walker.bus.key_up.assert_called_once_with(VK_D)
    assert not walker._pressed_keys
    assert walker.route_snapshot() == route
    state["step"] = 2
    walker.tick()
    assert not walker.waypoints


def test_blindness_timeout_uses_elapsed_time_when_ticks_are_slow(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("modules.walker.time.monotonic", lambda: clock[0])
    walker, state = make_walker([(100, 200, 7), None, (102, 200, 7)])
    walker.add_waypoint(105, 200, 7)
    walker.tick()
    state["step"] = 1
    walker.tick()
    assert walker._pressed_keys == {VK_D}
    clock[0] = 0.501
    walker.tick()
    assert not walker._pressed_keys
    state["step"] = 2
    walker.tick()
    assert walker._pressed_keys == {VK_D}
    state["step"] = 1
    walker.tick()
    assert walker._pressed_keys == {VK_D}


def test_read_exception_is_treated_as_temporary_failure_and_stop_is_immediate():
    walker, _ = make_walker([(100, 200, 7)])
    walker.add_waypoint(105, 200, 7)
    walker.tick()
    walker.read.side_effect = OSError("read failed")
    assert walker.read_player_position() is None
    walker.tick()
    assert walker._pressed_keys == {VK_D}
    walker.request_stop()
    assert not walker._pressed_keys


def test_valid_read_resets_failure_count_and_aligns_y_after_grace(monkeypatch):
    monkeypatch.setattr("modules.walker.time.monotonic", lambda: 0.0)
    walker, state = make_walker([(100, 200, 7), None, (102, 200, 7)])
    walker.add_waypoint(102, 202, 7)
    walker.tick()
    for cycle in range(2):
        state["step"] = 1
        for failure in range(10):
            walker.tick()
        assert walker._pressed_keys
        state["step"] = 2
        walker.tick()
        assert walker._pressed_keys == {VK_S}
    assert len(walker.waypoints) == 1


@pytest.mark.parametrize("action,param,reached", [
    ("Walk", "", True), ("BOX", "", False), ("Stair", "", False),
    ("Safe Spot", "", False), ("Wait", "1000", False), ("Say", "hello", False),
])
def test_default_arrival_tolerance_is_only_for_walk(action, param, reached):
    walker = Walker(input_bus=MagicMock(), loop=False)
    walker.read_player_position = MagicMock(return_value=(100, 200, 7))
    walker.add_waypoint(101, 201, 7, action, param)
    walker.tick()
    assert walker.arrival_tolerance == 1
    assert bool(walker.waypoints) is not reached
    if not reached:
        walker.bus.key_down.assert_called_once_with(VK_D)
        walker.bus.send_text.assert_not_called()
        assert walker.state == WalkerState.MOVING


def test_inter_waypoint_pause_is_nonblocking_and_resumes_at_deadline():
    walker, _ = make_walker([(100, 200, 7)])
    walker.add_waypoint(100, 200, 7)
    walker.add_waypoint(105, 200, 7)
    with patch("modules.walker.time.monotonic", return_value=10.0) as clock, patch(
        "modules.walker.random.uniform", return_value=0.125
    ) as jitter, patch("modules.walker.time.sleep") as sleep:
        walker.tick()
        jitter.assert_called_once_with(0.05, 0.20)
        assert walker._next_move_delay_until == 10.125
        walker.read.reset_mock()
        clock.return_value = 10.124
        walker.tick()
        walker.read.assert_not_called()
        walker.bus.key_down.assert_not_called()
        assert len(walker.waypoints) == 1
        clock.return_value = 10.125
        walker.tick()
        walker.bus.key_down.assert_called_once_with(VK_D)
        sleep.assert_not_called()


@pytest.mark.parametrize("value", [-1, 3, True, 1.5, "1", None])
def test_invalid_arrival_tolerance(value):
    with pytest.raises(ValueError):
        Walker(arrival_tolerance=value)


@pytest.mark.parametrize("operation", ["clear_waypoints", "request_stop", "teardown"])
def test_route_reset_clears_pending_pause(operation):
    walker, _ = make_walker([(100, 200, 7)], loop=True)
    walker.add_waypoint(100, 200, 7)
    walker.tick()
    assert walker._next_move_delay_until > 0
    getattr(walker, operation)()
    assert walker._next_move_delay_until == 0.0


@pytest.mark.parametrize("tolerance,position,reached", [
    (0, (101, 200, 7), False), (1, (102, 200, 7), False),
    (2, (102, 202, 7), True), (2, (101, 200, 8), False),
])
def test_walk_tolerance_boundaries_and_floor(tolerance, position, reached):
    walker = Walker(input_bus=MagicMock(), loop=False, arrival_tolerance=tolerance)
    walker.read_player_position = MagicMock(return_value=position)
    walker.add_waypoint(100, 200, 7)
    walker.tick()
    assert bool(walker.waypoints) is not reached


def test_wait_completion_schedules_pause_without_repeating_action():
    walker, _ = make_walker([(100, 200, 7)])
    walker.add_waypoint(100, 200, 7, "Wait", "1000")
    walker.add_waypoint(105, 200, 7)
    with patch("modules.walker.time.monotonic", return_value=10.0) as clock, patch(
        "modules.walker.random.uniform", return_value=0.1
    ) as jitter:
        walker.tick()
        assert walker.state == WalkerState.WAITING
        jitter.assert_not_called()
        clock.return_value = 11.0
        walker.tick()
        assert walker.state == WalkerState.MOVING
        assert len(walker.waypoints) == 1
        assert walker._next_move_delay_until == 11.1
        clock.return_value = 11.05
        walker.tick()
        walker.bus.key_down.assert_not_called()
        clock.return_value = 11.1
        walker.tick()
        walker.bus.key_down.assert_called_once_with(VK_D)
        jitter.assert_called_once()


def test_failed_say_does_not_schedule_pause():
    walker, _ = make_walker([(100, 200, 7)])
    walker.add_waypoint(100, 200, 7, "Say", "hello")
    walker.bus.send_text.return_value = False
    with patch("modules.walker.random.uniform") as jitter:
        walker.tick()
        jitter.assert_not_called()
        assert walker._next_move_delay_until == 0.0
        assert len(walker.waypoints) == 1


def test_replace_route_validation_is_atomic_and_resets_pause():
    walker, _ = make_walker([(100, 200, 7)], loop=True)
    walker.add_waypoint(100, 200, 7)
    walker.tick()
    previous = walker.route_snapshot()
    with pytest.raises(ValueError):
        walker.replace_route([], False, arrival_tolerance=3)
    assert walker.route_snapshot() == previous
    assert walker._next_move_delay_until > 0
    walker.replace_route(previous["waypoints"], True, arrival_tolerance=2)
    assert walker.arrival_tolerance == 2
    assert walker._next_move_delay_until == 0.0
