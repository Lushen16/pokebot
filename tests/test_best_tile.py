from core.best_tile import BestTileSelector


class FakeMap:
    def __init__(self, blocked=(), unknown=()):
        self.blocked = set(blocked)
        self.unknown = set(unknown)
        self.calls = 0

    def walkable(self, x, y, z):
        self.calls += 1
        if (x, y) in self.blocked:
            return False
        if (x, y) in self.unknown:
            return None
        return True


def _open_map(size=21):
    return FakeMap(blocked={(x, y) for x in range(size) for y in range(size)
                            if x in (0, size - 1) or y in (0, size - 1)})


def test_selects_tile_with_wall_margin():
    map_reader = _open_map()
    selector = BestTileSelector(map_reader)
    tile = selector.select((10, 10, 8), radius=4, wall_margin=3)
    assert tile is not None
    assert tile.reason == "ok"
    assert tile.wall_distance >= 3
    assert max(abs(tile.x - 10), abs(tile.y - 10)) <= 4


def test_stays_near_player_when_area_is_open():
    tile = BestTileSelector(_open_map()).select((10, 10, 8), radius=4, wall_margin=3)
    assert (tile.x, tile.y) == (10, 10)


def test_wall_next_to_player_moves_away():
    blocked = {(10, y) for y in range(21)}
    map_reader = FakeMap(blocked=blocked)
    tile = BestTileSelector(map_reader).select((8, 10, 8), radius=4, wall_margin=3)
    assert tile is not None
    assert abs(tile.x - 10) >= 3
    assert tile.wall_distance >= 3


def test_falls_back_when_no_tile_has_margin():
    blocked = {(x, y) for x in range(8, 13) for y in range(8, 13)
               if x in (8, 12) or y in (8, 12)}
    map_reader = FakeMap(blocked=blocked)
    tile = BestTileSelector(map_reader).select((10, 10, 8), radius=4, wall_margin=3)
    assert tile is not None
    assert tile.reason == "sem margem minima"


def test_returns_none_without_map_or_player():
    assert BestTileSelector(FakeMap(unknown={(x, y) for x in range(20) for y in range(20)})) \
        .select((10, 10, 8), radius=4, wall_margin=3) is None
    assert BestTileSelector(_open_map()).select(None) is None


def test_counts_monsters_as_tie_breaker():
    monsters = [{"x": 11, "y": 10, "z": 8}, {"x": 12, "y": 10, "z": 8}]
    tile = BestTileSelector(_open_map()).select((10, 10, 8), monsters,
                                                radius=4, wall_margin=3)
    assert tile.monster_count >= 1
