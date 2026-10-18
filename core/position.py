MAX_MAP_COORDINATE = 10000


def is_valid_position(x, y, z) -> bool:
    return (all(type(value) is int for value in (x, y, z))
            and 0 <= x <= MAX_MAP_COORDINATE and 0 <= y <= MAX_MAP_COORDINATE
            and 0 <= z <= 15)
