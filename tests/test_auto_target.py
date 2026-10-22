"""Testes do AutoTarget: selecao por alcance/filtros (sem tocar no jogo)."""
import sys
import time
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.auto_target import ActionDecision, ActionKind, AutoTarget


class Ctx:
    def __init__(self, data):
        self.data = data
        self.config = {"modules": {"auto_target": {"enabled": False, "tick_ms": 300}}}

    def get(self, *keys, default=None):
        node = self.data
        for key in keys:
            if isinstance(node, dict) and key in node:
                node = node[key]
            else:
                return default
        return node

    def inc_stat(self, key):
        pass

    def set(self, value, *keys):
        node = self.data
        for key in keys[:-1]:
            node = node.setdefault(key, {})
        node[keys[-1]] = value


def make_module(**overrides):
    cfg = {
        "attack_range_tiles": 7,
        "ignore_players": True,
        "ignore_npcs": True,
        "ignore_dead": True,
        "prioritize": "lowest_hp_percent",
        "name_whitelist": [],
        "name_blacklist": [],
    }
    cfg.update(overrides)
    return AutoTarget(ctx=Ctx({"auto_target": cfg}), resolver=None)


def creature(name, x=0, y=0, hp=100, **flags):
    return {"name": name, "x": x, "y": y, "hp_percent": hp, **flags}


class TestAutoTargetSelection:
    def test_range_filter(self):
        module = make_module(attack_range_tiles=7)
        near = creature("Rattata", 3, 4)
        far = creature("Pidgey", 10, 0)
        chosen = module._select_target([near, far], player=(0, 0))
        assert chosen["name"] == "Rattata"
        assert chosen["distance"] == 4

    def test_blacklist(self):
        module = make_module(name_blacklist=["Gary"])
        chosen = module._select_target([creature("Gary", 0, 0), creature("Rattata", 1, 0)],
                                       player=(0, 0))
        assert chosen["name"] == "Rattata"

    def test_whitelist(self):
        module = make_module(name_whitelist=["Pikachu"])
        chosen = module._select_target([creature("Rattata", 0, 0), creature("Pikachu", 2, 0)],
                                       player=(0, 0))
        assert chosen["name"] == "Pikachu"

    def test_ignore_dead_and_zero_hp(self):
        module = make_module()
        chosen = module._select_target([creature("Dead", 0, 0, is_dead=True),
                                        creature("Ghost", 0, 0, hp=0),
                                        creature("Alive", 1, 0)], player=(0, 0))
        assert chosen["name"] == "Alive"

    def test_prioritize_nearest(self):
        module = make_module(prioritize="nearest")
        chosen = module._select_target([creature("Far", 6, 0, hp=50),
                                        creature("Near", 2, 0, hp=90)], player=(0, 0))
        assert chosen["name"] == "Near"

    def test_own_roster_is_never_targeted(self):
        module = make_module()
        module.ctx.data["pokemon_roster"] = {"4": "Togepi"}
        chosen = module._select_target([creature("Togepi", 0, 0), creature("Rattata", 1, 0)],
                                       player=(0, 0))
        assert chosen["name"] == "Rattata"

    def test_returns_none_when_out_of_range(self):
        module = make_module(attack_range_tiles=2)
        assert module._select_target([creature("Far", 9, 9)], player=(0, 0)) is None

    def test_focus_shiny_ignores_normal_monsters(self):
        module = make_module(focus_shiny=True)
        chosen = module._select_target([
            creature("Rattata", 1, 0, shiny=False),
            creature("Rattata", 2, 0, shiny=True),
        ], player=(0, 0))
        assert chosen["shiny"] is True

    def test_focus_shiny_falls_back_to_normal_monsters(self):
        module = make_module(focus_shiny=True)
        chosen = module._select_target([creature("Rattata", 1, 0, shiny=False)],
                                       player=(0, 0))
        assert chosen is not None
        assert chosen["shiny"] is False

    def test_focus_shiny_off_accepts_normal_monsters(self):
        module = make_module(focus_shiny=False)
        chosen = module._select_target([creature("Rattata", 1, 0, shiny=False)],
                                       player=(0, 0))
        assert chosen["name"] == "Rattata"

    def test_target_ignores_creatures_on_another_floor(self):
        module = make_module()
        chosen = module._select_target([
            creature("Rattata", 1, 0, creature_id=1, z=8),
            creature("Rattata", 2, 0, creature_id=2, z=7),
        ], player=(0, 0, 7))
        assert chosen is not None
        assert chosen["creature_id"] == 2

    def test_focus_shiny_does_not_remove_normal_monsters_from_lure(self):
        module = make_module(focus_shiny=True)
        monsters = module._eligible_lure_monsters([
            creature("Rattata", 1, 0, shiny=False),
            creature("Rattata", 2, 0, shiny=True),
        ], player=(0, 0), floor_z=7)
        assert len(monsters) == 2
        assert any(monster["shiny"] for monster in monsters)

    def test_no_reader_means_no_creatures(self):
        module = make_module()
        assert module._reader is None
        assert module._get_creatures((0, 0)) == []


def resolve(module, creatures, hp=100, player=(0, 0), now=0.0):
    return module._resolve_lure_identity(creatures, player, hp, now=now)


def confirm(module, creatures, hp=100, player=(0, 0)):
    resolve(module, creatures, hp=hp, player=player, now=0.0)
    return resolve(module, creatures, hp=hp, player=player, now=0.1)


class TestBestTileWiring:
    def test_provider_uses_player_and_creatures(self):
        module = make_module(best_tile_enabled=True, best_tile_radius=5,
                             best_tile_wall_margin=3)
        calls = []

        class Selector:
            def select(self, player, creatures, *, radius, wall_margin):
                calls.append((player, list(creatures), radius, wall_margin))
                return type("Tile", (), {"x": 12, "y": 13, "z": 8})()

        class Mini:
            def refresh_if_changed(self):
                return False

        module._best_tile_selector = Selector()
        module._minimap = Mini()
        module._last_map_refresh = time.monotonic()
        module._scratch["player"] = (10, 10, 8)
        module._scratch["creatures"] = [{"name": "Rattata", "x": 11, "y": 10, "z": 8}]
        assert module._best_tile_provider(None) == {"x": 12, "y": 13, "z": 8}
        assert calls[0][0] == (10, 10, 8)
        assert calls[0][2:] == (5, 3)

    def test_post_combat_heal_calls_healer(self):
        class Healer:
            def __init__(self):
                self.calls = 0

            def force_heal_sequence(self):
                self.calls += 1

        healer = Healer()
        module = make_module(post_combat_heal=True)
        module.healer = healer
        assert module._heal_after_damage() is True
        assert healer.calls == 1

    def test_post_combat_heal_can_be_disabled(self):
        class Healer:
            def force_heal_sequence(self):
                raise AssertionError("nao deveria curar")

        module = make_module(post_combat_heal=False)
        module.healer = Healer()
        assert module._heal_after_damage() is False


class TestComboAttack:
    def test_empty_attack_uses_configured_combo_hotkey_without_f5(self):
        module = make_module(attack_hotkey="", combo_command="", combo_spellwords="", combo_hotkey="C")
        module.bus = Mock()
        module.bus.press_combo.return_value = True
        assert module._should_combo_attack() is True
        assert module._send_combo_once() is True
        module.bus.press_combo.assert_called_once_with([0x43], hold_ms=50, priority=10, module=module.name)
        module.bus.send_text.assert_not_called()

    def test_invalid_combo_hotkey_does_not_send_text_or_other_skill(self):
        module = make_module(attack_hotkey="", combo_command="", combo_hotkey="invalid-key")
        module.bus = Mock()
        assert module._send_combo_once() is False
        module.bus.press_combo.assert_not_called()
        module.bus.send_text.assert_not_called()

    def test_failed_combo_does_not_fall_back_to_unconfigured_attack(self):
        module = make_module(attack_hotkey="", combo_command="!autocombo")
        target = creature("Rattata", 1, 1, hp=100, creature_id=9)
        module._pause_route_for_combat = Mock()
        module._ensure_target_selected = Mock(return_value=target)
        module._trigger_combo_sequence = Mock(return_value=False)
        module.press = Mock()
        module._execute_attack(100.0, target)
        module._trigger_combo_sequence.assert_called_once()
        module.press.assert_not_called()

    def test_explicit_combo_command_takes_precedence_over_hotkey(self):
        module = make_module(attack_hotkey="", combo_command="!autocombo", combo_hotkey="C")
        module.bus = Mock()
        module.bus.send_text.return_value = True
        assert module._send_combo_once() is True
        module.bus.send_text.assert_called_once_with("!autocombo", priority=10, module=module.name)
        module.bus.press_combo.assert_not_called()

    def test_plain_autocombo_is_sent_without_sequence(self):
        module = make_module(combo_command="!autocombo")
        assert module._effective_combo_command() == "!autocombo"

    def test_empty_attack_hotkey_falls_back_to_combo_text(self):
        module = make_module(attack_hotkey="", combo_command="!autocombo")
        assert module._should_combo_attack() is True
        sent = []

        class Bus:
            def send_text(self, text, priority=10, module="unknown"):
                sent.append(text)
                return True

        module.bus = Bus()
        assert module._send_combo_once() is True
        assert sent == ["!autocombo"]

    def test_spellwords_are_sent_separately_without_commas(self):
        module = make_module(attack_hotkey="", combo_command="",
                             combo_spellwords="m1,m2 m3")
        assert module._should_combo_attack() is True
        sent = []

        class Bus:
            def send_text(self, text, priority=10, module="unknown"):
                sent.append(text)
                return True

        module.bus = Bus()
        assert module._send_combo_once() is True
        assert sent == ["m1", "m2", "m3"]

    def test_configured_attack_hotkey_keeps_normal_attack(self):
        module = make_module(attack_hotkey="F5", combo_command="!autocombo")
        assert module._should_combo_attack() is False

    def test_target_mode_combo_uses_combo_text(self):
        module = make_module(attack_hotkey="F5", target_mode="combo",
                             combo_command="!autocombo m1")
        assert module._should_combo_attack() is True


class TestSingleDecision:
    def test_tick_executes_exactly_one_decision(self):
        module = make_module()
        module.resolver = object()
        module._decide_action = Mock(return_value=ActionDecision(ActionKind.NONE, "teste"))
        module._execute_decision = Mock()
        module.tick()
        module._decide_action.assert_called_once()
        module._execute_decision.assert_called_once()

    def test_manual_pause_skips_decision_and_action(self):
        module = make_module()
        module.resolver = object()
        module.ctx.is_paused = lambda: True
        module.ctx.is_stopped = lambda: False
        module._decide_action = Mock()
        module._execute_decision = Mock()
        module.tick()
        module._decide_action.assert_not_called()
        module._execute_decision.assert_not_called()


class TestUnavailableLureFallsBack:
    def test_lure_handled_false_still_selects_normal_target(self):
        module = make_module(perfect_lure_enabled=True)
        class Ctrl:
            def tick(self, now):
                return type("Status", (), {"handled": False, "state": "idle"})()
        module._ensure_perfect = lambda: Ctrl()
        module._sync_perfect_settings = lambda: None
        module._player_pos = lambda: (0, 0)
        module._get_creatures = lambda player=None: [
            creature("Rattata", 1, 0, creature_id=9)]
        decision = module._decide_action(100.0)
        assert decision.kind in (ActionKind.SELECT_TARGET, ActionKind.ATTACK)
        assert decision.target["creature_id"] == 9


class TestTargetSelection:
    def test_selects_target_with_tab_and_does_not_cycle_repeatedly(self):
        module = make_module()
        module.press = Mock()
        module._scratch["player"] = (0, 0)
        module._scratch["creatures"] = [creature("Rattata", 1, 0, creature_id=7)]
        target = module._ensure_target_selected(1.0)
        assert target["creature_id"] == 7
        module.press.assert_called_once_with(0x09)

        module.press.reset_mock()
        module._ensure_target_selected(1.5)
        module.press.assert_not_called()


class TestPokemonPositionCache:
    def test_position_cache_expires_and_revalidates(self, monkeypatch):
        module = make_module()
        module._last_pokemon_pos = (1, 2, 7)
        module._last_pokemon_pos_at = 100.0
        module._player_pos = lambda: (0, 0)
        module._get_creatures = lambda player=None: []
        module._lure_hp_percent = lambda: 100
        module._player_z = lambda: 7
        module._resolve_lure_identity = lambda *args, **kwargs: {
            "position_confirmed": True, "x": 3, "y": 4, "z": 7}

        monkeypatch.setattr("modules.auto_target.time.monotonic", lambda: 100.5)
        assert module.active_pokemon_position() == (1, 2, 7)

        monkeypatch.setattr("modules.auto_target.time.monotonic", lambda: 101.0)
        assert module.active_pokemon_position() == (3, 4, 7)

    def test_identity_reset_invalidates_position_cache(self):
        module = make_module()
        module._last_pokemon_pos = (1, 2, 7)
        module._last_pokemon_pos_at = time.monotonic()
        module._reset_lure_identity()
        assert module._last_pokemon_pos is None


class TestPokemonMoveCommand:
    def _module(self, template):
        module = make_module()
        module.ctx.data["walker"] = {"dynamic_lure_move_command": template}
        module._last_pokemon_name = "Ditto"
        return module

    def test_formats_name_and_target_tile(self):
        module = self._module("{name}, move {x} {y} {z}!")
        sent = []

        class Bus:
            def send_text(self, text, priority=10, module="unknown"):
                sent.append((text, priority, module))
                return True

        module.bus = Bus()
        assert module.send_pokemon_move_command((10, 20, 7)) is True
        assert sent[0][0] == "Ditto, move 10 20 7!"

    def test_default_command_and_missing_name(self):
        module = make_module()
        module.ctx.data["walker"] = {"dynamic_lure_move_command": "{name}, move!"}
        module._last_pokemon_name = "Ditto"
        sent = []

        class Bus:
            def send_text(self, text, priority=10, module="unknown"):
                sent.append(text)
                return True

        module.bus = Bus()
        assert module.send_pokemon_move_command((1, 2, 3)) is True
        assert sent == ["Ditto, move!"]

        module._last_pokemon_name = None
        module.ctx.data["pokemon_roster"] = {"1": "", "2": ""}
        assert module.send_pokemon_move_command((1, 2, 3)) is False


class TestLureIdentity:
    def test_requires_two_stable_ticks(self):
        module = make_module()
        module.ctx.data["pokemon_roster"] = {"4": "Togepi"}
        first = resolve(module, [creature("Togepi", 1, 0, creature_id=7)])
        assert first["position_confirmed"] is False
        second = resolve(module, [creature("Togepi", 1, 0, creature_id=7)], now=0.1)
        assert second["position_confirmed"] is True
        assert second["key"] == 7
        assert (second["x"], second["y"]) == (1, 0)

    def test_explicit_name_wins_over_roster(self):
        module = make_module(active_pokemon="Togepi")
        module.ctx.data["pokemon_roster"] = {"1": "Spearow", "4": "Togepi"}
        result = confirm(module, [creature("Spearow", 1, 0, creature_id=1),
                                  creature("Togepi", 2, 0, creature_id=2)])
        assert result["position_confirmed"] is True
        assert result["key"] == 2

    def test_auto_detects_by_roster_name(self):
        module = make_module()
        module.ctx.data["pokemon_roster"] = {"1": "Spearow", "4": "Togepi"}
        result = confirm(module, [creature("Rattata", 1, 1, creature_id=1),
                                  creature("Togepi", 2, 0, creature_id=2)])
        assert result["position_confirmed"] is True
        assert result["key"] == 2

    def test_hp_mismatch_is_not_confirmed(self):
        module = make_module()
        module.ctx.data["pokemon_roster"] = {"4": "Togepi"}
        result = confirm(module, [creature("Togepi", 1, 0, hp=35, creature_id=7)])
        assert result["position_confirmed"] is False
        assert result["hp_confirmed"] is False

    def test_zero_global_hp_falls_back_to_unique_creature_hp(self):
        module = make_module()
        module.ctx.data["pokemon_roster"] = {"4": "Togepi"}
        first = resolve(module, [creature("Togepi", 1, 0, hp=58, creature_id=7)], hp=0)
        assert first["position_confirmed"] is False
        second = resolve(module, [creature("Togepi", 1, 0, hp=58, creature_id=7)],
                         hp=0, now=0.1)
        assert second["position_confirmed"] is True
        assert second["hp_confirmed"] is True
        assert second["hp"] == 58
        recent = resolve(module, [], hp=0, now=1.0)
        assert recent["hp_confirmed"] is True
        assert recent["hp"] == 58

    def test_far_creature_is_not_confirmed(self):
        module = make_module()
        module.ctx.data["pokemon_roster"] = {"4": "Togepi"}
        result = confirm(module, [creature("Togepi", 10, 0, creature_id=7)])
        assert result["position_confirmed"] is False

    def test_ambiguous_creatures_are_not_confirmed(self):
        module = make_module()
        module.ctx.data["pokemon_roster"] = {"4": "Togepi"}
        result = confirm(module, [creature("Togepi", 1, 0, creature_id=7),
                                  creature("Togepi", 0, 1, creature_id=8)])
        assert result["position_confirmed"] is False

    def test_missing_creature_keeps_hp_only_within_ttl(self):
        module = make_module()
        module.ctx.data["pokemon_roster"] = {"4": "Togepi"}
        ok = confirm(module, [creature("Togepi", 1, 0, creature_id=7)])
        assert ok["position_confirmed"] is True
        recent = resolve(module, [], now=1.0)
        assert recent["position_confirmed"] is False
        assert recent["hp_confirmed"] is True
        assert recent["key"] == 7
        stale = resolve(module, [], now=5.0)
        assert stale["hp_confirmed"] is False
        assert stale["key"] is None

    def test_roster_change_invalidates_identity(self):
        module = make_module()
        module.ctx.data["pokemon_roster"] = {"4": "Togepi"}
        confirm(module, [creature("Togepi", 1, 0, creature_id=7)])
        module.ctx.data["pokemon_roster"] = {"4": "Spearow"}
        result = resolve(module, [creature("Spearow", 1, 0, creature_id=9)], now=0.2)
        assert result["position_confirmed"] is False
        assert result["key"] is None

    def test_creature_switch_invalidates_immediately(self):
        module = make_module()
        module.ctx.data["pokemon_roster"] = {"4": "Togepi"}
        confirm(module, [creature("Togepi", 1, 0, creature_id=7)])
        switched = resolve(module, [creature("Togepi", 0, 1, creature_id=8)], now=0.2)
        assert switched["position_confirmed"] is False
        assert switched["key"] is None
        again = resolve(module, [creature("Togepi", 0, 1, creature_id=8)], now=0.3)
        assert again["position_confirmed"] is True
        assert again["key"] == 8

    def test_no_roster_names_means_no_identity(self):
        module = make_module()
        result = confirm(module, [creature("Togepi", 1, 0, creature_id=7)])
        assert result["position_confirmed"] is False
        assert result["key"] is None

    def test_position_never_falls_back_to_player(self):
        module = make_module()
        module.ctx.data["pokemon_roster"] = {"4": "Togepi"}
        result = confirm(module, [creature("Rattata", 1, 0, creature_id=1)])
        assert result["position_confirmed"] is False
        assert result["x"] is None and result["y"] is None


class TestUnrelatedConfig:
    def test_revive_delay_stays_out_of_scope(self):
        import json
        cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
        assert cfg["auto_target"]["revive_delay_ms"] == 1100


class FakeWalker:
    def __init__(self):
        self.paused = 0
        self.resumed = 0
        self.combat_paused = False

    def pause_for_combat(self):
        self.paused += 1
        self.combat_paused = True

    def resume_route(self):
        self.resumed += 1
        self.combat_paused = False


class TestCombatPause:
    def make(self, **overrides):
        module = make_module(**overrides)
        module.walker = FakeWalker()
        return module

    def test_pauses_only_once_while_target_is_alive(self):
        module = self.make()
        module._pause_route_for_combat(creature("Rattata", 1, 0))
        module._pause_route_for_combat(creature("Rattata", 1, 0))
        assert module.walker.paused == 1
        assert module.walker.combat_paused is True

    def test_waits_resume_delay_before_walking_again(self):
        module = self.make(combat_resume_delay_ms=1500)
        module._pause_route_for_combat(creature("Rattata", 1, 0))
        module._resume_route_after_combat(module._last_target_seen + 1.0)
        assert module.walker.resumed == 0
        assert module.walker.combat_paused is True
        module._resume_route_after_combat(module._last_target_seen + 1.6)
        assert module.walker.resumed == 1
        assert module.walker.combat_paused is False

    def test_resume_without_pause_is_noop(self):
        module = self.make()
        module._resume_route_after_combat(now=time.monotonic() + 10)
        assert module.walker.resumed == 0

    def test_stop_releases_pause(self):
        module = self.make()
        module.start = lambda: None
        module._pause_route_for_combat(creature("Rattata", 1, 0))
        module.stop()
        assert module.walker.resumed == 1

    def test_tick_pauses_route_for_normal_target(self):
        module = self.make(perfect_lure_enabled=False)
        module.resolver = object()
        module._player_pos = lambda: (0, 0)
        module._get_creatures = lambda player=None: [
            creature("Rattata", 1, 0, creature_id=5)]
        module.press = lambda vk, **kwargs: True
        module.tick()
        assert module.walker.paused == 1
        assert module.walker.combat_paused is True
        assert module.ctx.get("combat", "target_active") is True

    def test_tick_resumes_route_when_no_target_remains(self):
        module = self.make(perfect_lure_enabled=False, combat_resume_delay_ms=1500)
        module.resolver = object()
        module._player_pos = lambda: (0, 0)
        module._get_creatures = lambda player=None: [
            creature("Rattata", 1, 0, creature_id=5)]
        module.press = lambda vk, **kwargs: True
        module.tick()
        module._get_creatures = lambda player=None: []
        module._last_target_seen = time.monotonic() - 2.0
        module.tick()
        assert module.walker.resumed == 1
        assert module.walker.combat_paused is False
