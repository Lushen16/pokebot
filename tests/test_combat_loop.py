"""Testes do loop de combate (lure -> combo -> revive -> retoma o walker)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.target import CombatController, CombatState, is_shiny_name, parse_hotkey
from modules.walker import Walker


class FakeEvent:
    def __init__(self):
        self.flag = False
        self.waits = []

    def wait(self, timeout):
        self.waits.append(timeout)
        return self.flag

    def is_set(self):
        return self.flag

    def set(self):
        self.flag = True


class Recorder:
    def __init__(self):
        self.combos = []
        self.texts = []

    def press_combo(self, codes, kind="combo"):
        self.combos.append((list(codes), kind))
        return True

    def send_text(self, text):
        self.texts.append(text)
        return True


class FakeWalker:
    def __init__(self):
        self.paused = 0
        self.resumed = 0

    def pause_for_combat(self):
        self.paused += 1

    def resume_route(self):
        self.resumed += 1


class SequenceProvider:
    def __init__(self, sequence):
        self.sequence = [list(item) for item in sequence]
        self.calls = 0

    def __call__(self):
        value = self.sequence[min(self.calls, len(self.sequence) - 1)]
        self.calls += 1
        return value


def mobs(count, name="Rattata", x=1, y=1, **extra):
    """Bichos agrupados num bloco 3x3 (dentro do Perfect Lure por padrao)."""
    return [{"name": name, "x": x + (i % 3), "y": y + (i // 3), "hp_percent": 100, **extra}
            for i in range(count)]


def build(entities_provider, *, recorder=None, walker=None, stop_event=None, **overrides):
    recorder = recorder or Recorder()
    walker = walker if walker is not None else FakeWalker()
    kwargs = dict(
        entities_provider=entities_provider,
        player_provider=lambda: (0, 0),
        press_combo=recorder.press_combo,
        send_text=recorder.send_text,
        walker=walker,
        stop_event=stop_event or FakeEvent(),
        lure_target_count=8,
        lure_radius=20,
        perfect_lure=True,
        perfect_lure_distance=3,
        combo_hotkey="F5",
        revive_hotkey="Shift+F",
        combo_wait_ms=100,
        revive_delay_ms=100,
        max_combat_loops=5,
        focus_shiny=False,
    )
    kwargs.update(overrides)
    return CombatController(**kwargs), recorder, walker


class TestParseHotkey:
    def test_single_key(self):
        assert parse_hotkey("F5") == [0x74]

    def test_modifier_combo(self):
        assert parse_hotkey("Shift+F") == [0x10, ord("F")]

    def test_invalid(self):
        assert parse_hotkey("banana") == []
        assert parse_hotkey("") == []


class TestShinyDetection:
    def test_shiny_name(self):
        assert is_shiny_name("Shiny Rattata")
        assert is_shiny_name("Shiny Bala")
        assert is_shiny_name("rattata shiny")
        assert not is_shiny_name("Rattata")


class TestEnemyFiltering:
    def test_excludes_own_roster_blacklist_dead_and_far(self):
        controller, _, _ = build(lambda: [], own_names=["Togepi"], blacklist=["Gary"])
        entities = [
            {"name": "Togepi", "x": 1, "y": 0, "hp_percent": 100},
            {"name": "Gary", "x": 1, "y": 0, "hp_percent": 100},
            {"name": "Dead", "x": 1, "y": 0, "hp_percent": 100, "is_dead": True},
            {"name": "Ghost", "x": 1, "y": 0, "hp_percent": 0},
            {"name": "Player", "x": 1, "y": 0, "hp_percent": 100, "is_player": True},
            {"name": "Far", "x": 99, "y": 0, "hp_percent": 100},
            {"name": "Rattata", "x": 2, "y": 0, "hp_percent": 100},
        ]
        names = [e["name"] for e in controller.enemies(entities, (0, 0))]
        assert names == ["Rattata"]

    def test_should_lure_uses_lure_radius(self):
        controller, _, _ = build(lambda: [], lure_target_count=3, lure_radius=2)
        assert controller.should_lure(mobs(3, x=0, y=0), (0, 0))
        assert not controller.should_lure(mobs(3, x=50), (0, 0))


class TestPerfectLure:
    @staticmethod
    def _split(count_close, count_far, far_x=12):
        close = [{"name": "Rattata", "x": i % 3, "y": i // 3, "hp_percent": 100}
                 for i in range(count_close)]
        far = [{"name": "Rattata", "x": far_x + i, "y": 0, "hp_percent": 100}
               for i in range(count_far)]
        return close + far

    def test_waits_while_detected_monsters_are_not_close_enough(self):
        entities = self._split(4, 6)
        controller, recorder, walker = build(lambda: entities, lure_target_count=8,
                                             lure_radius=20, perfect_lure_distance=3,
                                             perfect_lure_wait_ms=100)
        assert controller.should_lure(entities, (0, 0)) is False
        assert controller.run(entities=entities, player=(0, 0)) is False
        assert recorder.combos == []
        assert controller._last_close_count == 4
        assert walker.paused >= 1 and walker.resumed >= 1

    def test_combos_once_enough_monsters_reach_perfect_distance(self):
        entities = self._split(8, 2)
        controller, recorder, walker = build(SequenceProvider([entities, []]),
                                             lure_target_count=8, lure_radius=20,
                                             perfect_lure_distance=3)
        assert controller.should_lure(entities, (0, 0)) is True
        assert controller.run() is True
        assert [kind for _, kind in recorder.combos].count("combo") == 1
        assert walker.paused == 1

    def test_disabled_uses_lure_radius_only(self):
        entities = [{"name": "Rattata", "x": 5, "y": 0, "hp_percent": 100} for _ in range(8)]
        controller, _, _ = build(lambda: entities, lure_target_count=8, lure_radius=20,
                                 perfect_lure=False, perfect_lure_distance=3)
        assert controller.should_lure(entities, (0, 0)) is True

    def test_pauses_walker_and_waits_for_proximity(self):
        far = self._split(4, 6)
        close = self._split(8, 2)
        provider = SequenceProvider([far, close, close, []])
        controller, recorder, walker = build(provider, lure_target_count=8, lure_radius=20,
                                             perfect_lure_distance=3, perfect_lure_wait_ms=300)
        assert controller.run() is True
        assert walker.paused >= 1
        assert [kind for _, kind in recorder.combos].count("combo") == 1
        assert walker.resumed == 1

    def test_resumes_walking_when_lure_never_closes(self):
        provider = SequenceProvider([self._split(4, 6)])
        controller, recorder, walker = build(provider, lure_target_count=8, lure_radius=20,
                                             perfect_lure_distance=3, perfect_lure_wait_ms=100)
        assert controller.run() is False
        assert recorder.combos == []
        assert walker.paused >= 1 and walker.resumed >= 1


class TestDelayClamp:
    def test_delays_are_capped_at_one_second(self):
        controller, _, _ = build(lambda: [], combo_wait_ms=5000, revive_delay_ms=5000)
        assert controller.combo_wait == 1.0
        assert controller.revive_delay == 1.0


class TestShinyFocus:
    def test_focus_on_picks_shiny(self):
        controller, _, _ = build(lambda: [], focus_shiny=True)
        enemies = controller.enemies(
            [{"name": "Rattata", "x": 1, "y": 0, "hp_percent": 100},
             {"name": "Shiny Pidgey", "x": 6, "y": 0, "hp_percent": 100}], (0, 0))
        assert controller.pick_target(enemies)["name"] == "Shiny Pidgey"

    def test_focus_on_falls_back_to_commons_when_no_shiny(self):
        controller, _, _ = build(lambda: [], focus_shiny=True)
        enemies = controller.enemies(
            [{"name": "Rattata", "x": 1, "y": 0, "hp_percent": 100},
             {"name": "Pidgey", "x": 2, "y": 0, "hp_percent": 100}], (0, 0))
        assert controller.pick_target(enemies)["name"] == "Rattata"

    def test_focus_on_run_falls_back_to_commons(self):
        controller, recorder, walker = build(SequenceProvider([mobs(8)]), focus_shiny=True)
        assert controller.run() is True
        assert [kind for _, kind in recorder.combos].count("combo") >= 1

    def test_focus_on_attacks_single_shiny_without_lure_count(self):
        controller, recorder, _ = build(
            SequenceProvider([mobs(1, shiny=True), []]),
            focus_shiny=True, lure_target_count=0)
        assert controller.run() is True
        assert [kind for _, kind in recorder.combos].count("combo") == 1

    def test_focus_off_picks_nearest(self):
        controller, _, _ = build(lambda: [], focus_shiny=False)
        enemies = controller.enemies(
            [{"name": "Rattata", "x": 1, "y": 0, "hp_percent": 100},
             {"name": "Shiny Pidgey", "x": 6, "y": 0, "hp_percent": 100}], (0, 0))
        assert controller.pick_target(enemies)["name"] == "Rattata"

    def test_flag_shiny_with_base_name_is_prioritized(self):
        controller, _, _ = build(lambda: [], focus_shiny=True)
        enemies = controller.enemies(
            [{"name": "Rattata", "x": 1, "y": 0, "hp_percent": 100},
             {"name": "Rattata", "x": 6, "y": 0, "hp_percent": 100, "shiny": True}],
            (0, 0))
        assert controller.pick_target(enemies)["shiny"] is True


class TestCombatLoop:
    def test_combo_revive_until_clear(self):
        provider = SequenceProvider([mobs(8), mobs(8), mobs(4), mobs(0)])
        controller, recorder, walker = build(provider)
        assert controller.run() is True
        assert [kind for _, kind in recorder.combos].count("combo") == 3
        assert [kind for _, kind in recorder.combos].count("revive") == 3
        assert ([codes for codes, kind in recorder.combos if kind == "combo"][0]) == [0x74]
        assert ([codes for codes, kind in recorder.combos if kind == "revive"][0]) == [0x10, ord("F")]
        assert walker.paused == 1 and walker.resumed == 1
        assert controller.state == CombatState.LURING

    def test_max_loops_releases_walker(self):
        controller, recorder, walker = build(SequenceProvider([mobs(8)]))
        assert controller.run() is True
        assert [kind for _, kind in recorder.combos].count("combo") == 5
        assert walker.resumed == 1

    def test_combo_command_uses_chat(self):
        controller, recorder, _ = build(SequenceProvider([mobs(8), mobs(0)]), combo_command="!autocombo")
        assert controller.run() is True
        assert recorder.texts == ["!autocombo"]

    def test_no_lure_when_count_zero(self):
        controller, recorder, walker = build(SequenceProvider([mobs(8)]), lure_target_count=0)
        assert controller.run() is False
        assert recorder.combos == [] and walker.paused == 0

    def test_skips_when_below_lure_count(self):
        controller, recorder, walker = build(SequenceProvider([mobs(2)]))
        assert controller.run() is False
        assert recorder.combos == [] and walker.paused == 0

    def test_stop_event_interrupts(self):
        event = FakeEvent()
        event.set()
        controller, recorder, walker = build(SequenceProvider([mobs(8)]), stop_event=event)
        assert controller.run() is False
        assert walker.paused == 0

    def test_invalid_revive_hotkey_does_not_crash(self):
        controller, recorder, _ = build(SequenceProvider([mobs(8), mobs(8)]), revive_hotkey="???")
        assert controller.run() is True
        assert all(kind != "revive" for _, kind in recorder.combos)


class FakeBus:
    def key_up(self, code, *a, **k):
        return True

    def key_down(self, code, *a, **k):
        return True


class TestWalkerCombatPause:
    def test_pause_and_resume(self):
        walker = Walker(memory_reader=None, offset_registry=None, input_bus=FakeBus())
        assert walker.combat_paused is False
        walker.pause_for_combat()
        assert walker.combat_paused is True
        walker.tick()
        walker.resume_route()
        assert walker.combat_paused is False
