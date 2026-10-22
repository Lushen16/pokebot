"""Testes do Perfect Lure (maquina de estados orientada a tick, sem sleep)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.perfect_lure import (
    POSITION_UNAVAILABLE,
    LureSnapshot,
    LureState,
    PerfectLureController,
)


class Recorder:
    def __init__(self, ok=True):
        self.combos = []
        self.ok = ok

    def press_combo(self, codes, kind="combo"):
        self.combos.append((list(codes), kind))
        return self.ok

    def kinds(self):
        return [kind for _, kind in self.combos]

    def combos_only(self):
        return [c for c in self.combos if c[1] == "combo"]


class FakeWalker:
    def __init__(self):
        self.paused = 0
        self.resumed = 0

    def pause_for_combat(self):
        self.paused += 1

    def resume_route(self):
        self.resumed += 1


class Feed:
    """Snapshot provider que pode ser trocado a cada tick."""

    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.snapshot


def mobs(count, *, x=0, y=0, start=0, name="Rattata", **extra):
    return [{"name": name, "x": x, "y": y, "hp_percent": 100,
             "creature_id": start + i, **extra} for i in range(count)]


def snap(monsters, *, hp=100, px=0, py=0, key=None, valid=True,
         hp_cur=None, hp_max=None, position_confirmed=True,
         hp_confirmed=True, reason=""):
    return LureSnapshot(valid=valid, monsters=tuple(monsters), pokemon_x=px,
                        pokemon_y=py, pokemon_hp_percent=hp, pokemon_key=key,
                        pokemon_hp_current=hp_cur, pokemon_hp_max=hp_max,
                        position_confirmed=position_confirmed,
                        hp_confirmed=hp_confirmed,
                        unavailable_reason=reason)


def make(feed, *, recorder=None, walker=None, connected=None, **overrides):
    recorder = recorder or Recorder()
    walker = walker if walker is not None else FakeWalker()
    kwargs = dict(
        snapshot_provider=feed,
        press_combo=recorder.press_combo,
        walker=walker,
        is_connected=connected,
        combo_codes=[0x74],
        revive_codes=[0x10, 0x46],
        min_monsters_to_lure=8,
        lure_radius_sqm=1,
        emergency_hp_percent=40,
        max_lure_wait_ms=2500,
        group_stable_ms=300,
        combo_wait_ms=500,
        revive_delay_ms=400,
        cooldown_ms=900,
        post_combat_revive=False,
    )
    kwargs.update(overrides)
    return PerfectLureController(**kwargs), recorder, walker


class TestGrouping:
    def test_fewer_than_minimum_keeps_luring_without_attacking(self):
        feed = Feed(snap(mobs(5)))
        controller, rec, walker = make(feed)
        status = controller.step(0.0, feed.snapshot)
        assert status.handled is True
        assert rec.combos == []
        assert controller.state == LureState.LURING
        assert walker.paused == 0

    def test_exact_minimum_grouped_and_stable_combos(self):
        feed = Feed(snap(mobs(8)))
        controller, rec, walker = make(feed)
        first = controller.step(0.0, feed.snapshot)
        assert first.handled is True
        assert rec.combos == []
        assert controller.state == LureState.WAITING_FOR_GROUP
        controller.step(0.4, feed.snapshot)
        assert len(rec.combos_only()) == 1
        assert controller.state == LureState.EXECUTING_COMBO
        assert walker.paused == 1

    def test_more_than_minimum_has_no_upper_limit(self):
        feed = Feed(snap(mobs(14)))
        controller, rec, _ = make(feed)
        controller.step(0.0, feed.snapshot)
        controller.step(0.4, feed.snapshot)
        assert len(rec.combos_only()) == 1

    def test_not_all_near_waits(self):
        near = mobs(4, start=0)
        far = mobs(4, x=10, y=10, start=100)
        feed = Feed(snap(near + far))
        controller, rec, _ = make(feed)
        status = controller.step(0.0, feed.snapshot)
        assert controller.state == LureState.WAITING_FOR_GROUP
        assert status.near == 4 and status.detected == 8
        assert rec.combos == []

    def test_dedup_by_creature_id(self):
        dupes = [{"name": "Rattata", "x": 0, "y": 0, "hp_percent": 100,
                  "creature_id": 7} for _ in range(8)]
        feed = Feed(snap(dupes))
        controller, _, _ = make(feed)
        status = controller.step(0.0, feed.snapshot)
        assert status.detected == 1

    def test_monster_leaving_drops_below_minimum(self):
        feed = Feed(snap(mobs(8)))
        controller, rec, _ = make(feed)
        controller.step(0.0, feed.snapshot)
        feed.snapshot = snap(mobs(7))
        status = controller.step(0.1, feed.snapshot)
        assert status.handled is True
        assert status.state == "luring"
        assert rec.combos == []


class TestEmergencyHP:
    def test_low_hp_with_one_target_combos(self):
        feed = Feed(snap(mobs(1), hp=10))
        controller, rec, _ = make(feed)
        status = controller.step(0.0, feed.snapshot)
        assert status.handled is True
        assert len(rec.combos_only()) == 1
        assert "HP" in controller.last_reason

    def test_hp_equal_to_limit_does_not_trigger(self):
        feed = Feed(snap(mobs(1), hp=40))
        controller, rec, _ = make(feed)
        status = controller.step(0.0, feed.snapshot)
        assert status.state == "luring"
        assert rec.combos == []

    def test_hp_just_below_limit_triggers(self):
        feed = Feed(snap(mobs(1), hp=39))
        controller, rec, _ = make(feed)
        controller.step(0.0, feed.snapshot)
        assert len(rec.combos_only()) == 1

    def test_emergency_requires_a_target(self):
        feed = Feed(snap([], hp=5))
        controller, rec, _ = make(feed)
        status = controller.step(0.0, feed.snapshot)
        assert status.state == "luring"
        assert rec.combos == []

    def test_missing_hp_does_not_trigger(self):
        feed = Feed(snap(mobs(1), hp=None))
        controller, rec, _ = make(feed)
        controller.step(0.0, feed.snapshot)
        assert rec.combos == []

    def test_zero_max_hp_does_not_trigger(self):
        feed = Feed(snap(mobs(1), hp=None, hp_cur=5, hp_max=0))
        controller, rec, _ = make(feed)
        controller.step(0.0, feed.snapshot)
        assert rec.combos == []

    def test_out_of_range_percent_does_not_trigger(self):
        feed = Feed(snap(mobs(1), hp=150))
        controller, rec, _ = make(feed)
        controller.step(0.0, feed.snapshot)
        assert rec.combos == []

    def test_hp_from_current_and_max(self):
        feed = Feed(snap(mobs(1), hp=None, hp_cur=10, hp_max=100))
        controller, rec, _ = make(feed)
        controller.step(0.0, feed.snapshot)
        assert len(rec.combos_only()) == 1


class DetourWalker(FakeWalker):
    def __init__(self):
        super().__init__()
        self.detours = []
        self.restored = 0

    def detour_to(self, x, y, z):
        self.detours.append((x, y, z))
        return True

    def restore_route(self):
        self.restored += 1
        return True


class TestBestTile:
    def test_moves_to_best_tile_before_combat(self):
        walker = DetourWalker()
        moved = []

        def provider(_snapshot):
            return {"x": 2, "y": 0, "z": 0}

        feed = Feed(snap(mobs(8)))
        controller, rec, _ = make(feed, walker=walker,
                                  best_tile_provider=provider,
                                  lure_radius_sqm=5,
                                  move_to=lambda x, y, z: moved.append((x, y, z)) or True)
        first = controller.step(0.0, feed.snapshot)
        assert first.handled is True
        assert controller.state == LureState.MOVING_TO_TILE
        assert rec.combos == []
        assert moved == [(2, 0, 0)]

        feed.snapshot = snap(mobs(8), px=2, py=0)
        controller.step(0.1, feed.snapshot)
        feed.snapshot = snap(mobs(8), px=2, py=0)
        controller.step(0.5, feed.snapshot)
        assert len(rec.combos_only()) == 1


    def test_lure_timeout_forces_combo_even_while_moving(self):
        walker = DetourWalker()
        feed = Feed(snap(mobs(8)))
        controller, rec, _ = make(
            feed, walker=walker, best_tile_provider=lambda _s: {"x": 2, "y": 0, "z": 0},
            lure_radius_sqm=5, max_lure_wait_ms=2500, best_tile_max_wait_ms=5000)
        controller.step(0.0, feed.snapshot)
        assert controller.state == LureState.MOVING_TO_TILE
        assert rec.combos == []

        controller.step(2.55, feed.snapshot)
        assert len(rec.combos_only()) == 1
        assert controller._best_tile is None
        assert controller._best_tile_attempted is True


class TestDamageCycle:
    def test_damage_forces_kill_all_then_heals(self):
        heal_calls = []
        feed = Feed(snap(mobs(8), hp=100))
        walker = DetourWalker()
        controller, rec, walker = make(
            feed, walker=walker, kill_all_on_damage=True,
            heal_callback=lambda: heal_calls.append(1) or True,
            combo_wait_ms=100, cooldown_ms=100, force_kill_max_ms=1000)
        controller.step(0.0, feed.snapshot)
        controller.step(0.4, feed.snapshot)
        assert len(rec.combos_only()) == 1

        feed.snapshot = snap(mobs(4), hp=80)
        controller.step(0.5, feed.snapshot)
        assert controller.state == LureState.FORCE_KILLING
        assert len(rec.combos_only()) == 2

        feed.snapshot = snap([], hp=80)
        controller.step(0.65, feed.snapshot)
        assert heal_calls == [1]
        assert controller.state == LureState.COOLDOWN

        controller.step(0.8, feed.snapshot)
        assert controller.state in (LureState.IDLE, LureState.LURING)
        assert walker.restored >= 1

    def test_revive_waits_for_combo_repeater_to_finish(self):
        flag = {"active": True}
        feed = Feed(snap(mobs(8), hp=100))
        controller, rec, _ = make(
            feed, kill_all_on_damage=True, post_combat_revive=True,
            combo_wait_ms=100, revive_delay_ms=100,
            combo_active=lambda: flag["active"])
        controller.step(0.0, feed.snapshot)
        controller.step(0.4, feed.snapshot)
        controller.step(0.5, feed.snapshot)
        assert "revive" not in rec.kinds()
        assert controller._busy_kind == "waiting_combo"

        controller.step(0.75, feed.snapshot)
        assert "revive" not in rec.kinds()

        flag["active"] = False
        controller.step(1.0, feed.snapshot)
        assert "revive" in rec.kinds()

    def test_heal_failure_retries_once_without_spam(self):
        calls = []
        feed = Feed(snap(mobs(8), hp=100))
        controller, _, _ = make(
            feed, kill_all_on_damage=True,
            heal_callback=lambda: calls.append(1) or False,
            combo_wait_ms=100, cooldown_ms=100, heal_retry_ms=2000)
        controller.step(0.0, feed.snapshot)
        controller.step(0.4, feed.snapshot)
        feed.snapshot = snap([], hp=80)
        controller.step(0.5, feed.snapshot)
        assert calls == [1]
        assert controller.state == LureState.HEALING

        controller.step(0.6, feed.snapshot)
        assert calls == [1]

        controller.step(2.6, feed.snapshot)
        assert calls == [1, 1]
        assert controller.state == LureState.COOLDOWN

        controller.step(2.7, feed.snapshot)
        assert calls == [1, 1]

    def test_revive_is_sent_before_post_combat_force_kill(self):
        feed = Feed(snap(mobs(8), hp=100))
        controller, rec, _ = make(
            feed, kill_all_on_damage=True, post_combat_revive=True,
            combo_wait_ms=100, revive_delay_ms=100, cooldown_ms=100)
        controller.step(0.0, feed.snapshot)
        controller.step(0.4, feed.snapshot)
        feed.snapshot = snap(mobs(4), hp=80)
        controller.step(0.5, feed.snapshot)
        assert "revive" in rec.kinds()
        assert controller._busy_kind == "revive"

    def test_no_damage_resumes_without_heal(self):
        heal_calls = []
        feed = Feed(snap(mobs(8), hp=100))
        controller, rec, _ = make(
            feed, kill_all_on_damage=True, heal_callback=lambda: heal_calls.append(1) or True,
            combo_wait_ms=100, cooldown_ms=100)
        controller.step(0.0, feed.snapshot)
        controller.step(0.4, feed.snapshot)
        assert len(rec.combos_only()) == 1

        feed.snapshot = snap(mobs(3), hp=100)
        controller.step(0.5, feed.snapshot)
        assert controller.state == LureState.COOLDOWN
        assert heal_calls == []
        assert len(rec.combos_only()) == 1


class TestProximity:
    def test_distance_is_relative_to_pokemon(self):
        monsters = mobs(8, x=50, y=50)
        feed = Feed(snap(monsters, px=50, py=50))
        controller, rec, _ = make(feed)
        controller.step(0.0, feed.snapshot)
        controller.step(0.4, feed.snapshot)
        assert len(rec.combos_only()) == 1

    def test_monsters_far_from_pokemon_do_not_group(self):
        monsters = mobs(8, x=0, y=0)
        feed = Feed(snap(monsters, px=50, py=50))
        controller, rec, _ = make(feed)
        controller.step(0.0, feed.snapshot)
        assert rec.combos == []
        assert controller.state == LureState.WAITING_FOR_GROUP


class TestPositionAndHPConfirmation:
    def test_unconfirmed_position_blocks_grouping(self):
        feed = Feed(snap(mobs(8), position_confirmed=False))
        controller, rec, _ = make(feed)
        status = controller.step(0.0, feed.snapshot)
        assert status.handled is False
        assert status.near == 0
        assert rec.combos == []
        assert controller.state == LureState.IDLE

    def test_unconfirmed_position_blocks_max_wait_combo(self):
        feed = Feed(snap(mobs(8), position_confirmed=False))
        controller, rec, _ = make(feed)
        controller.step(0.0, feed.snapshot)
        status = controller.step(2.55, feed.snapshot)
        assert status.handled is False
        assert rec.combos == []

    def test_unconfirmed_position_reports_exact_message(self):
        feed = Feed(snap(mobs(8), position_confirmed=False, reason=""))
        controller, _, _ = make(feed)
        status = controller.step(0.0, feed.snapshot)
        assert status.unavailable_reason == POSITION_UNAVAILABLE
        assert status.unavailable_reason == (
            "Perfect Lure indisponível: posição do Pokémon não confirmada")

    def test_unconfirmed_position_with_confirmed_hp_allows_emergency(self):
        feed = Feed(snap(mobs(1), hp=10, position_confirmed=False,
                         hp_confirmed=True))
        controller, rec, _ = make(feed)
        status = controller.step(0.0, feed.snapshot)
        assert status.handled is True
        assert len(rec.combos_only()) == 1

    def test_unconfirmed_hp_blocks_emergency(self):
        feed = Feed(snap(mobs(1), hp=10, hp_confirmed=False))
        controller, rec, _ = make(feed)
        status = controller.step(0.0, feed.snapshot)
        assert status.state == "luring"
        assert rec.combos == []

    def test_unconfirmed_hp_ignores_stale_percent(self):
        feed = Feed(snap(mobs(1), hp=1, hp_confirmed=False))
        controller, rec, _ = make(feed)
        controller.step(0.0, feed.snapshot)
        controller.step(0.4, feed.snapshot)
        assert rec.combos == []

    def test_switch_during_cooldown_discards_pending(self):
        feed = Feed(snap(mobs(1), hp=10, key="A"))
        controller, rec, _ = make(feed, combo_wait_ms=500, cooldown_ms=900)
        controller.step(0.0, feed.snapshot)  # troca inicial: limpa dados
        controller.step(0.1, feed.snapshot)
        assert len(rec.combos_only()) == 1
        feed.snapshot = snap(mobs(1), hp=10, key="B")
        controller.step(0.2, feed.snapshot)
        assert controller._pending is None
        assert controller._pokemon_pos is None
        assert controller._hp_percent is None
        assert controller._pokemon_key == "B"

    def test_missing_data_does_not_halt_normal_combat(self):
        feed = Feed(snap([], valid=False, position_confirmed=False,
                         hp_confirmed=False))
        controller, rec, _ = make(feed)
        status = controller.step(0.0, feed.snapshot)
        assert status.handled is False
        assert rec.combos == []
        assert controller.state == LureState.IDLE


class TestMaxWait:
    def test_combo_fires_when_max_wait_elapses(self):
        far = mobs(8, x=10, y=10)
        feed = Feed(snap(far))
        controller, rec, _ = make(feed)
        controller.step(0.0, feed.snapshot)
        assert rec.combos == []
        controller.step(2.55, feed.snapshot)
        assert len(rec.combos_only()) == 1

    def test_new_monsters_do_not_reset_deadline(self):
        far = mobs(8, x=10, y=10)
        feed = Feed(snap(far))
        controller, rec, _ = make(feed)
        controller.step(0.0, feed.snapshot)
        feed.snapshot = snap(mobs(11, x=10, y=10))
        controller.step(0.5, feed.snapshot)
        assert rec.combos == []
        controller.step(2.55, feed.snapshot)
        assert len(rec.combos_only()) == 1

    def test_group_stability_reset_by_new_arrival(self):
        feed = Feed(snap(mobs(8)))
        controller, rec, _ = make(feed)
        controller.step(0.0, feed.snapshot)
        feed.snapshot = snap(mobs(8) + mobs(1, x=10, y=10, start=100))
        controller.step(0.2, feed.snapshot)
        feed.snapshot = snap(mobs(9))
        controller.step(0.3, feed.snapshot)
        controller.step(0.5, feed.snapshot)
        assert rec.combos == []
        controller.step(0.65, feed.snapshot)
        assert len(rec.combos_only()) == 1


class TestPendingAndCooldown:
    def test_emergency_during_cooldown_becomes_pending(self):
        feed = Feed(snap(mobs(1), hp=10))
        controller, rec, _ = make(feed, combo_wait_ms=900, cooldown_ms=900)
        controller.step(0.0, feed.snapshot)
        assert len(rec.combos_only()) == 1
        controller.step(0.3, feed.snapshot)
        assert len(rec.combos_only()) == 1
        assert controller._pending is not None
        controller.step(1.0, feed.snapshot)
        assert len(rec.combos_only()) == 1
        controller.step(2.0, feed.snapshot)
        assert len(rec.combos_only()) == 2

    def test_pending_cleared_when_condition_vanishes(self):
        feed = Feed(snap(mobs(1), hp=10))
        controller, rec, _ = make(feed, combo_wait_ms=900, cooldown_ms=900)
        controller.step(0.0, feed.snapshot)
        controller.step(0.3, feed.snapshot)
        assert controller._pending is not None
        feed.snapshot = snap(mobs(1), hp=100)
        controller.step(0.4, feed.snapshot)
        assert controller._pending is None

    def test_no_second_combo_during_cooldown(self):
        feed = Feed(snap(mobs(8)))
        controller, rec, _ = make(feed, combo_wait_ms=500, cooldown_ms=900)
        controller.step(0.0, feed.snapshot)
        controller.step(0.4, feed.snapshot)
        assert len(rec.combos_only()) == 1
        controller.step(1.0, feed.snapshot)
        assert len(rec.combos_only()) == 1

    def test_failed_send_keeps_request_pending(self):
        feed = Feed(snap(mobs(1), hp=10))
        rec = Recorder(ok=False)
        controller, _, _ = make(feed, recorder=rec)
        status = controller.step(0.0, feed.snapshot)
        assert status.handled is True
        assert controller.state == LureState.COMBO_PENDING
        assert controller._pending is not None


class TestLifecycle:
    def test_disable_cancels_pending_and_resumes_walker(self):
        feed = Feed(snap(mobs(1), hp=10))
        controller, rec, walker = make(feed, combo_wait_ms=900, cooldown_ms=900)
        controller.step(0.0, feed.snapshot)
        controller.step(0.3, feed.snapshot)
        assert controller._pending is not None
        controller.configure(enabled=False)
        status = controller.step(0.4, feed.snapshot)
        assert status.handled is False
        assert controller.state == LureState.IDLE
        assert controller._pending is None
        assert walker.resumed >= 1

    def test_disconnect_cancels_pending(self):
        state = {"connected": True}
        feed = Feed(snap(mobs(1), hp=10))
        controller, rec, _ = make(feed, connected=lambda: state["connected"],
                                  combo_wait_ms=900, cooldown_ms=900)
        controller.step(0.0, feed.snapshot)
        controller.step(0.3, feed.snapshot)
        assert controller._pending is not None
        state["connected"] = False
        controller.step(0.4, feed.snapshot)
        assert controller._pending is None
        assert controller.state == LureState.IDLE

    def test_pokemon_switch_discards_previous_hp(self):
        feed = Feed(snap(mobs(1), hp=90, key="A"))
        controller, rec, _ = make(feed)
        controller.step(0.0, feed.snapshot)
        assert rec.combos == []
        feed.snapshot = snap(mobs(1), hp=10, key="B")
        controller.step(0.3, feed.snapshot)
        assert rec.combos == []
        controller.step(0.6, feed.snapshot)
        assert len(rec.combos_only()) == 1

    def test_invalid_snapshot_keeps_group(self):
        feed = Feed(snap(mobs(8)))
        controller, rec, _ = make(feed)
        controller.step(0.0, feed.snapshot)
        feed.snapshot = snap([], valid=False)
        status = controller.step(0.1, feed.snapshot)
        assert status.detected == 8
        assert rec.combos == []


class TestClampingAndRevive:
    def test_delays_are_clamped_to_one_second(self):
        feed = Feed(snap(mobs(1), hp=10))
        controller, _, _ = make(feed, combo_wait_ms=5000, revive_delay_ms=5000,
                                max_lure_wait_ms=5000, group_stable_ms=5000,
                                cooldown_ms=5000)
        assert controller.combo_wait == 1.0
        assert controller.revive_delay == 1.0
        assert controller.max_lure_wait == 5.0
        assert controller.group_stable == 1.0
        assert controller.cooldown == 1.0

    def test_revive_after_combo_when_monsters_remain(self):
        feed = Feed(snap(mobs(1), hp=10))
        controller, rec, _ = make(feed, combo_wait_ms=500, revive_delay_ms=400,
                                  post_combat_revive=True)
        controller.step(0.0, feed.snapshot)
        controller.step(0.6, feed.snapshot)
        assert "revive" in rec.kinds()
