import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import pytest

from memory import Resolver
from modules.healer import Healer, _resolve_vk, _resolve_hotkey

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def make_healer(monkeypatch):
    monkeypatch.setitem(sys.modules, "inputbus", SimpleNamespace(get_bus=Mock()))
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr("modules.healer.time.monotonic", lambda: clock.now)
    monkeypatch.setattr("modules.healer.time.sleep", lambda s: None)

    def create(hp=110, hp_max=200, settings=None):
        config = {"healer": {"hp_percent_threshold": 60, "panic_hp_percent": 25,
                             **(settings or {})}}

        def get(*keys, default=None):
            value = config
            for key in keys:
                if not isinstance(value, dict) or key not in value:
                    return default
                value = value[key]
            return default if value is None else value

        ctx = SimpleNamespace(config=config, get=get, inc_stat=Mock())
        bus = SimpleNamespace(press_key=Mock(return_value=True))
        values = {"hp": hp, "hp_max": hp_max}
        resolver = SimpleNamespace(read_field=Mock(side_effect=lambda block, field: values[field]))
        healer = Healer(ctx=ctx, bus=bus, resolver=resolver)
        return healer, ctx, bus, resolver, values, clock

    return create


def test_hp_heals_without_reading_mp(make_healer):
    healer, ctx, bus, resolver, _, _ = make_healer()
    healer.tick()
    assert resolver.read_field.call_args_list == [call("player", "hp"), call("player", "hp_max")]
    assert bus.press_key.call_args_list == [
        call(0x34, 50.0, 5, module="healer"),
        call(0x71, 50.0, 5, module="healer"),
        call(0x31, 50.0, 10, module="healer"),
    ]
    ctx.inc_stat.assert_called_once_with("heals")
    assert healer.stats["heals_sent"] == 1


def test_normal_cooldown_is_not_key_hold(make_healer):
    healer, _, bus, _, _, clock = make_healer(settings={"cooldown_ms": 800, "hold_ms": 30})
    healer.tick()
    clock.now = 0.799
    healer.tick()
    assert bus.press_key.call_count == 3
    assert healer.last_skip_reason == "cura em cooldown"
    clock.now = 0.801
    healer.tick()
    assert bus.press_key.call_count == 6
    assert bus.press_key.call_args_list[0] == call(0x34, 30.0, 5, module="healer")


def test_emergency_priority_cooldown_and_normal_reset(make_healer):
    healer, _, bus, _, values, clock = make_healer(hp=30)
    healer.tick()
    assert bus.press_key.call_args_list == [
        call(0x34, 50.0, 1, module="healer"),
        call(0x71, 50.0, 1, module="healer"),
        call(0x31, 50.0, 10, module="healer"),
    ]
    clock.now = 0.399
    healer.tick()
    assert bus.press_key.call_count == 3
    clock.now = 0.401
    healer.tick()
    assert bus.press_key.call_count == 6
    values["hp"] = 100
    clock.now = 0.9
    healer.tick()
    assert bus.press_key.call_count == 6
    clock.now = 1.202
    healer.tick()
    assert bus.press_key.call_count == 9
    assert healer.stats["emergency_heals_sent"] == 2
    assert healer.stats["heals_sent"] == 1


def test_emergency_can_interrupt_normal_cooldown(make_healer):
    healer, _, bus, _, values, clock = make_healer()
    healer.tick()
    assert bus.press_key.call_count == 3
    values["hp"] = 30
    clock.now = 0.1
    healer.tick()
    assert bus.press_key.call_count == 6
    assert bus.press_key.call_args_list[3] == call(0x34, 50.0, 1, module="healer")


@pytest.mark.parametrize("hp,hp_max", [(None, 200), (100, None), (100, 0), (100, -1), (0, 200), (-1, 200), (201, 200), (120, 200)])
def test_invalid_or_sufficient_hp_does_not_press(make_healer, hp, hp_max):
    healer, _, bus, _, _, _ = make_healer(hp=hp, hp_max=hp_max)
    healer.tick()
    bus.press_key.assert_not_called()
    assert healer.last_skip_reason


def test_missing_resolver_is_noop(make_healer):
    healer, _, bus, _, _, _ = make_healer()
    healer.resolver = None
    healer.tick()
    bus.press_key.assert_not_called()
    assert healer.last_skip_reason == "resolver indisponível"


def test_threshold_boundary_is_normal_not_emergency(make_healer):
    healer, _, bus, _, _, _ = make_healer(hp=50)
    healer.tick()
    assert bus.press_key.call_args_list[0] == call(0x34, 50.0, 5, module="healer")


@pytest.mark.parametrize("hp", [110, 30])
def test_rejected_queue_does_not_count_or_start_cooldown(make_healer, hp):
    healer, ctx, bus, _, _, _ = make_healer(hp=hp)
    bus.press_key.return_value = False
    healer.tick()
    ctx.inc_stat.assert_not_called()
    assert healer.heals_sent == healer.emergency_heals_sent == 0
    assert healer._last_heal_ts is None
    assert healer._last_emergency_ts is None
    bus.press_key.return_value = True
    healer.tick()
    ctx.inc_stat.assert_called_once_with("heals")


def test_existing_config_preserves_hotkeys_and_uses_delay_as_cooldown(make_healer):
    settings = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))["healer"]
    healer, _, bus, _, _, clock = make_healer(settings=settings)
    assert healer.cooldown_ms == 800
    assert healer.hold_ms == 50
    assert healer.hp_threshold_pct == 90
    assert healer.emergency_threshold_pct == 20
    healer.tick()
    clock.now = 0.799
    healer.tick()
    # Puxar (1), skill (1), Revive Shift+F (2), retornar (1).
    assert bus.press_key.call_count == 5


def test_gui_config_updates_are_observed_next_tick(make_healer):
    healer, ctx, bus, _, _, _ = make_healer(hp=130)
    healer.tick()
    bus.press_key.assert_not_called()
    ctx.config["healer"].update(hp_percent_threshold=70, pokemon_heal_hotkey="5")
    healer.tick()
    assert bus.press_key.call_args_list[0].args[0] == 0x35


def test_alias_config_and_explicit_cooldown(make_healer):
    healer, _, bus, _, _, _ = make_healer(settings={"pokemon_heal_hotkey": "5", "cooldown_ms": 900, "delay_between_heals_ms": 650})
    healer.tick()
    assert healer.cooldown_ms == 900
    assert bus.press_key.call_args_list[0].args[0] == 0x35


def test_pokemon_swap_heal_sequence_presses_three_hotkeys_in_order(make_healer):
    with patch("modules.healer.time.sleep") as mock_sleep:
        healer, ctx, bus, _, _, _ = make_healer(
            hp=100,
            hp_max=200,
            settings={
                "pokemon_heal_hotkey": "4",
                "pokemon_heal_skill_hotkey": "F2",
                "pokemon_return_hotkey": "1",
                "pokemon_swap_delay_ms": 500,
                "pokemon_skill_delay_ms": 500,
            },
        )
        mock_press = Mock(return_value=True)
        healer.press = mock_press

        healer.tick()

        assert mock_press.call_count == 3
        assert mock_press.call_args_list == [
            call(0x34, hold_ms=50, priority=5),
            call(0x71, hold_ms=50, priority=5),
            call(0x31, hold_ms=50, priority=10),
        ]
        assert mock_sleep.call_args_list == [
            call(0.5),
            call(0.5),
        ]
        assert healer.stats["heals_sent"] == 1
        assert healer.stats["pokemon_heal_hotkey"] == "4"
        assert healer.stats["pokemon_heal_skill_hotkey"] == "F2"
        assert healer.stats["pokemon_return_hotkey"] == "1"


def test_togepi_m4_heal_sequence_resolves_slot_and_move(make_healer):
    with patch("modules.healer.time.sleep"):
        healer, ctx, bus, _, _, _ = make_healer(
            hp=100,
            hp_max=200,
            settings={
                "heal_pokemon_name": "Togepi",
                "heal_pokemon_slot": 4,
                "heal_move": "M4",
                "pokemon_return_hotkey": "1",
            },
        )
        mock_press = Mock(return_value=True)
        healer.press = mock_press

        healer.tick()

        assert mock_press.call_count == 3
        # Slot 4 -> 4, M4 -> F4, slot 1 -> 1
        assert mock_press.call_args_list == [
            call(0x34, hold_ms=50, priority=5),
            call(0x73, hold_ms=50, priority=5),
            call(0x31, hold_ms=50, priority=10),
        ]
        assert healer.stats["heal_pokemon_name"] == "Togepi"
        assert healer.stats["heal_move"] == "M4"
        assert healer.stats["pokemon_heal_hotkey"] == "4"


@pytest.mark.parametrize("settings", [{"hp_percent_threshold": 101}, {"panic_hp_percent": 70}, {"hold_ms": -1}, {"cooldown_ms": float("nan")}, {"emergency_cooldown_ms": -1}])
def test_invalid_settings_are_rejected(make_healer, settings):
    with pytest.raises(ValueError):
        make_healer(settings=settings)


@pytest.mark.parametrize("hotkey,expected", [("F1", 0x70), (" f12 ", 0x7B), ("1", 0x31), ("0x70", 0x70), ("112", 0x70)])
def test_hotkey_resolution(hotkey, expected):
    assert _resolve_vk(hotkey) == expected


@pytest.mark.parametrize("hotkey", ["invalid", "0x100", "-1"])
def test_invalid_hotkey(hotkey):
    with pytest.raises(ValueError):
        _resolve_vk(hotkey)


def test_legacy_resolver_returns_none_for_unknown_and_unverified_mp():
    resolver = Resolver(None, str(ROOT / "offsets.json"), str(ROOT / "config.json"))
    assert resolver.read_field("player", "mp") is None
    assert resolver.read_field("player", "mp_max") is None
    resolver.entries.pop("player.mp", None)
    assert resolver.read_field("player", "mp") is None


@pytest.mark.parametrize("pokemon_pct", [None, 0, 45, 100])
@pytest.mark.parametrize("hp,expected_inputs", [(100, 3), (200, 0), (None, 0)])
def test_healer_decision_uses_only_player_hp(make_healer, pokemon_pct, hp, expected_inputs):
    healer, _, bus, resolver, _, _ = make_healer(hp=hp, hp_max=200)
    resolver.read_entry = Mock(return_value=pokemon_pct)
    healer.tick()
    resolver.read_entry.assert_not_called()
    assert bus.press_key.call_count == expected_inputs
    assert healer.last_hp == hp
    assert healer.last_hp_max == 200
    assert healer.last_pct == (hp * 100.0 / 200 if hp is not None else None)


def test_healer_does_not_access_broken_pokemon_offset(make_healer):
    healer, _, bus, resolver, _, _ = make_healer(hp=79, hp_max=200)
    resolver.read_entry = Mock(side_effect=RuntimeError("pokemon offset unavailable"))
    healer.tick()
    resolver.read_entry.assert_not_called()
    assert bus.press_key.call_count == 3
    assert healer.last_pct == 39.5


@pytest.mark.parametrize("slot", range(1, 7))
@pytest.mark.parametrize("move", range(1, 6))
def test_slot_numbers_and_move_function_keys(make_healer, slot, move):
    healer, _, bus, _, _, _ = make_healer(settings={
        "heal_pokemon_slot": slot, "heal_move": f"m{move}", "hold_ms": 30,
    })
    healer.tick()
    assert bus.press_key.call_args_list == [
        call(ord(str(slot)), 30.0, 5, module="healer"),
        call(0x6F + move, 30.0, 5, module="healer"),
        call(ord("1"), 30.0, 10, module="healer"),
    ]


def test_rejected_return_is_not_counted_as_completed_sequence(make_healer):
    healer, ctx, bus, _, _, _ = make_healer()
    bus.press_key.side_effect = [True, True, False]
    healer.tick()
    assert healer.heals_sent == 0
    assert healer._last_heal_ts is None
    assert healer.last_skip_reason == "fila de input recusou a cura"
    ctx.inc_stat.assert_not_called()


def test_cooldown_starts_after_sequence_delays(make_healer):
    healer, _, bus, _, _, clock = make_healer()
    def advance_clock(duration):
        clock.now += duration
    with patch("modules.healer.time.sleep", side_effect=advance_clock):
        healer.tick()
    assert healer._last_heal_ts == 1.0
    clock.now = 1.799
    healer.tick()
    assert bus.press_key.call_count == 3


@pytest.mark.parametrize("reset_pokemon,expected_codes,expected_delays", [
    ("", [0x35, 0x71, 0x77, 0x33], [0.5, 0.5, 0.3]),
    ("6", [0x35, 0x71, 0x77, 0x33], [0.5, 0.5, 0.3]),
])
def test_configurable_reset_sequence(make_healer, reset_pokemon, expected_codes, expected_delays):
    healer, _, bus, _, _, _ = make_healer(settings={
        "heal_pokemon_name": "Blissey", "pokemon_heal_hotkey": "5",
        "heal_move": "M2", "pokemon_return_hotkey": "3",
        "pokemon_reset_hotkey": "F8", "pokemon_reset_pokemon_hotkey": reset_pokemon,
        "pokemon_reset_delay_ms": 300,
    })
    with patch("modules.healer.time.sleep") as sleep:
        healer.tick()
    assert [request.args[0] for request in bus.press_key.call_args_list] == expected_codes
    assert sleep.call_args_list == [call(delay) for delay in expected_delays]
    assert healer.heals_sent == 1
    assert healer.stats["pokemon_reset_hotkey"] == "F8"


@pytest.mark.parametrize("failure_index", [2, 3])
def test_reset_failure_does_not_report_completed_sequence(make_healer, failure_index):
    healer, ctx, bus, _, _, _ = make_healer(settings={
        "pokemon_reset_hotkey": "F8", "pokemon_reset_pokemon_hotkey": "6",
    })
    bus.press_key.side_effect = [True] * failure_index + [False]
    healer.tick()
    assert bus.press_key.call_count == failure_index + 1
    assert healer.heals_sent == 0
    assert healer._last_heal_ts is None
    ctx.inc_stat.assert_not_called()


@pytest.mark.parametrize("settings", [
    {"pokemon_reset_hotkey": "not-a-key"},
    {"pokemon_reset_hotkey": "F8", "pokemon_reset_delay_ms": -1},
    {"pokemon_reset_hotkey": "F8", "pokemon_reset_delay_ms": float("nan")},
])
def test_invalid_reset_configuration_rejected(make_healer, settings):
    with pytest.raises(ValueError):
        make_healer(settings=settings)


def test_reset_and_return_changes_are_observed_on_next_heal(make_healer):
    healer, ctx, bus, _, _, clock = make_healer()
    healer.tick()
    bus.press_key.reset_mock()
    ctx.config["healer"].update(
        heal_pokemon_name="Outro Pokemon", pokemon_heal_hotkey="6",
        pokemon_heal_skill_hotkey="F5", pokemon_return_hotkey="2",
        pokemon_reset_hotkey="F8", pokemon_reset_delay_ms=400,
    )
    clock.now = 2.0
    healer.tick()
    assert [request.args[0] for request in bus.press_key.call_args_list] == [0x36, 0x74, 0x77, 0x32]
    assert healer.heal_pokemon_name == "Outro Pokemon"


@pytest.mark.parametrize("emergency,priority", [(False, 5), (True, 1)])
def test_four_step_sequence_interleaves_keys_and_delays(make_healer, emergency, priority):
    healer, _, _, _, _, _ = make_healer(hp=10 if emergency else 100, settings={
        "pokemon_heal_hotkey": "F4", "pokemon_heal_skill_hotkey": "F2",
        "pokemon_reset_hotkey": "F1", "pokemon_return_hotkey": "F6",
        "pokemon_swap_delay_ms": 500, "pokemon_skill_delay_ms": 500,
        "pokemon_reset_delay_ms": 300,
    })
    timeline = Mock()
    healer.press = timeline.press
    healer.press.return_value = True
    with patch("modules.healer.time.sleep", timeline.sleep):
        healer.tick()
    assert timeline.mock_calls == [
        call.press(0x73, hold_ms=50.0, priority=priority), call.sleep(0.5),
        call.press(0x71, hold_ms=50.0, priority=priority), call.sleep(0.5),
        call.press(0x70, hold_ms=50.0, priority=priority), call.sleep(0.3),
        call.press(0x75, hold_ms=50.0, priority=10),
    ]


def test_tick_logs_unexpected_memory_failure_and_recovers(make_healer, caplog):
    healer, _, bus, resolver, _, _ = make_healer()
    resolver.read_field.side_effect = RuntimeError("memory disconnected")
    healer.tick()
    bus.press_key.assert_not_called()
    assert healer.last_skip_reason == "erro no ciclo de cura"
    assert "memory disconnected" in caplog.text
    resolver.read_field.side_effect = [100, 200]
    healer.tick()
    assert healer.heals_sent == 1


def test_force_heal_sequence_triggers_heal(make_healer):
    healer, _, bus, _, _, clock = make_healer()
    healer.force_heal_sequence()
    # At least the swap, skill, and return keys should be pressed
    assert bus.press_key.call_count >= 3


def test_resolve_hotkey_supports_combinations():
    assert _resolve_hotkey("Shift+F") == [0x10, 0x46]
    assert _resolve_hotkey("shift + f") == [0x10, 0x46]
    assert _resolve_hotkey("Ctrl+1") == [0x11, 0x31]
    assert _resolve_hotkey("F12") == [0x7B]
    assert _resolve_hotkey("Nenhum") == []
    assert _resolve_hotkey("") == []


def test_revive_combo_execution(make_healer):
    healer, ctx, bus, _, _, clock = make_healer(settings={
        "revive_hotkey": "Shift+F",
        "revive_delay_ms": 300,
    })
    bus.press_combo = Mock(return_value=True)
    healer.force_heal_sequence()
    bus.press_combo.assert_called_once_with([0x10, 0x46], hold_ms=50, priority=5, module="healer")
