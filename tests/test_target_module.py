import logging

import pytest

from modules.target import Entity, TargetModule, chebyshev_distance


class FakeOffsets:
    def __init__(self, values):
        self.values = values

    def read(self, memory, key):
        return self.values.get(key)

    def find(self, key):
        return None


def make_module(**kwargs):
    values = kwargs.pop("values", {})
    return TargetModule(memory=object(), offsets=FakeOffsets(values), **kwargs)


def test_chebyshev_distance():
    assert chebyshev_distance(0, 0, 0, 0) == 0
    assert chebyshev_distance(0, 0, 3, 2) == 3
    assert chebyshev_distance(10, 10, 7, 4) == 6


def test_select_target_nearest_within_range():
    module = make_module(attack_range=3)
    entities = [Entity("Pidgey", 5, 0), Entity("Rattata", 2, 0), Entity("Zubat", 9, 9)]
    entity, distance = module.select_target(entities, 0, 0)
    assert (entity.name, distance) == ("Rattata", 2)


def test_select_target_returns_none_out_of_range():
    module = make_module(attack_range=1)
    assert module.select_target([Entity("Pidgey", 5, 5)], 0, 0) is None


def test_select_target_skips_npc_player_and_dead():
    module = make_module(attack_range=10)
    entities = [
        Entity("Gary", 1, 0, is_player=True),
        Entity("Nurse", 1, 0, is_npc=True),
        Entity("Corpse", 1, 0, is_dead=True),
        Entity("Pidgey", 3, 0),
    ]
    entity, _ = module.select_target(entities, 0, 0)
    assert entity.name == "Pidgey"


def test_select_target_tie_break_is_deterministic():
    module = make_module(attack_range=5)
    entities = [Entity("Zubat", 2, 0), Entity("Abra", 0, 2)]
    entity, distance = module.select_target(entities, 0, 0)
    assert (entity.name, distance) == ("Abra", 2)


def test_tick_logs_found_target(caplog):
    module = make_module(values={"player.x": 0, "player.y": 0, "player.z": 0},
                         entity_reader=lambda: [Entity("Pidgey", 2, 1)], attack_range=5)
    with caplog.at_level(logging.INFO, logger="pokebot.modules.target"):
        module.tick()
    assert "Alvo encontrado: Pidgey em (2,1) | Distância: 2 tiles" in caplog.text
    assert module.last_target[0].name == "Pidgey"


def test_tick_warns_once_when_list_missing(caplog):
    module = make_module(values={"player.x": 1, "player.y": 1}, entity_reader=lambda: [])
    with caplog.at_level(logging.INFO, logger="pokebot.modules.target"):
        module.tick()
        module.tick()
    assert caplog.text.count("nenhuma entidade disponivel") == 1


def test_tick_does_nothing_without_player_position(caplog):
    module = make_module(values={}, entity_reader=lambda: [Entity("Pidgey", 1, 0)])
    with caplog.at_level(logging.INFO, logger="pokebot.modules.target"):
        module.tick()
    assert caplog.text == ""


def test_on_target_callback_receives_selection():
    seen = {}
    module = make_module(values={"player.x": 0, "player.y": 0}, attack_range=5,
                         entity_reader=lambda: [Entity("Pidgey", 1, 0)],
                         on_target=lambda entity, distance: seen.update(name=entity.name))
    module.tick()
    assert seen == {"name": "Pidgey"}


def test_negative_attack_range_is_rejected():
    with pytest.raises(ValueError):
        make_module(attack_range=-1)
