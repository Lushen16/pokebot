import json
from unittest.mock import patch

import pytest

from core.profiles import ProfileManager


@pytest.fixture
def profile():
    return {"waypoints": [{"x": 100, "y": 200, "z": 7, "action": "Wait", "param": "1000"}],
            "loop": True, "arrival_tolerance": 1, "config_healer": {"hp_percent_threshold": 60}}


def test_loading_legacy_profile_filters_origin_without_rewriting_file(tmp_path, profile, caplog):
    path = tmp_path / "hunts.json"
    profile["waypoints"].append(dict(x=0, y=0, z=0, action="Walk", param=""))
    original = json.dumps({"legacy": profile})
    path.write_text(original, encoding="utf-8")
    loaded = ProfileManager(path).load_profile("legacy")
    assert len(loaded["waypoints"]) == 1
    assert path.read_text(encoding="utf-8") == original
    assert "descartado" in caplog.text


def test_distance_guard_profile_roundtrip_and_validation(tmp_path, profile):
    manager = ProfileManager(tmp_path / "hunts.json")
    profile["distance_guard"] = {"enabled": True, "max_distance": 6, "resume_distance": 2}
    manager.save_profile("distancia", profile)
    assert manager.load_profile("distancia") == profile
    profile["distance_guard"]["resume_distance"] = 6
    with pytest.raises(ValueError):
        manager.save_profile("invalido", profile)
    assert manager.list_profiles() == ["distancia"]

def test_save_load_list_and_restart(tmp_path, profile):
    path = tmp_path / "hunts.json"
    manager = ProfileManager(path)
    assert manager.list_profiles() == []
    manager.save_profile("Zubat", profile)
    manager.save_profile("Água", profile)
    manager.save_profile("Abra", profile)
    restarted = ProfileManager(path)
    assert restarted.list_profiles() == ["Abra", "Zubat", "Água"]
    assert restarted.load_profile("Zubat") == profile


def test_overwrite_preserves_other_hunts_and_copies_data(tmp_path, profile):
    manager = ProfileManager(tmp_path / "hunts.json")
    manager.save_profile("Primeira", profile)
    manager.save_profile("Segunda", profile)
    profile["loop"] = False
    manager.save_profile("Primeira", profile)
    loaded = manager.load_profile("Primeira")
    loaded["waypoints"].clear()
    assert not manager.load_profile("Primeira")["loop"]
    assert manager.load_profile("Primeira")["waypoints"]
    assert manager.load_profile("Segunda")["loop"]


@pytest.mark.parametrize("name", ["", "   ", None, "a" * 121])
def test_invalid_name_leaves_file_untouched(tmp_path, profile, name):
    manager = ProfileManager(tmp_path / "hunts.json")
    with pytest.raises(ValueError):
        manager.save_profile(name, profile)
    assert not manager.path.exists()


@pytest.mark.parametrize("changes", [{"loop": "yes"}, {"waypoints": {}}, {"config_healer": []}])
def test_invalid_profile_rejected(tmp_path, profile, changes):
    profile.update(changes)
    with pytest.raises(ValueError):
        ProfileManager(tmp_path / "hunts.json").save_profile("Teste", profile)


def test_invalid_action_and_coordinate_rejected(tmp_path, profile):
    manager = ProfileManager(tmp_path / "hunts.json")
    profile["waypoints"][0]["action"] = "Unknown"
    with pytest.raises(ValueError):
        manager.save_profile("Teste", profile)
    profile["waypoints"][0].update(action="Walk", x=True)
    with pytest.raises(ValueError):
        manager.save_profile("Teste", profile)


def test_failed_atomic_replace_preserves_file_and_removes_temporary(tmp_path, profile):
    manager = ProfileManager(tmp_path / "hunts.json")
    manager.save_profile("Original", profile)
    original = manager.path.read_bytes()
    with patch("core.profiles.os.replace", side_effect=OSError("disk error")), pytest.raises(OSError):
        manager.save_profile("Nova", profile)
    assert manager.path.read_bytes() == original
    assert list(tmp_path.glob(".hunts-*.tmp")) == []


def test_corrupt_file_is_reported_not_overwritten(tmp_path, profile):
    path = tmp_path / "hunts.json"
    path.write_text("{broken", encoding="utf-8")
    manager = ProfileManager(path)
    with pytest.raises(json.JSONDecodeError):
        manager.save_profile("Teste", profile)
    assert path.read_text(encoding="utf-8") == "{broken"



def test_concurrent_saves_keep_all_hunts(tmp_path, profile):
    from concurrent.futures import ThreadPoolExecutor
    manager = ProfileManager(tmp_path / "hunts.json")
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda index: manager.save_profile(f"Hunt {index}", profile), range(10)))
    assert len(manager.list_profiles()) == 10
    assert all(manager.load_profile(name) == profile for name in manager.list_profiles())


@pytest.mark.parametrize("healer", [{"hp_percent_threshold": "bad"}, {"revive_hotkey": 5},
                                    {"pokemon_swap_delay_ms": -1}])
def test_invalid_healer_controls_are_rejected_before_loading(tmp_path, profile, healer):
    profile["config_healer"] = healer
    manager = ProfileManager(tmp_path / "hunts.json")
    with pytest.raises(ValueError):
        manager.save_profile("Teste", profile)
    assert not manager.path.exists()



def test_profile_rejects_coordinates_outside_shared_walker_limits(tmp_path, profile):
    profile["waypoints"][0]["x"] = 10001
    with pytest.raises(ValueError):
        ProfileManager(tmp_path / "hunts.json").save_profile("Fora do mapa", profile)


def test_delete_profile_preserves_other_hunts_and_restart(tmp_path, profile):
    manager = ProfileManager(tmp_path / "hunts.json")
    manager.save_profile("Primeira", profile)
    manager.save_profile("Segunda", profile)
    manager.delete_profile("Primeira")
    restarted = ProfileManager(manager.path)
    assert restarted.list_profiles() == ["Segunda"]
    assert restarted.load_profile("Segunda") == profile


def test_delete_missing_profile_does_not_rewrite_file(tmp_path, profile):
    manager = ProfileManager(tmp_path / "hunts.json")
    manager.save_profile("Existente", profile)
    previous = manager.path.read_bytes()
    with pytest.raises(KeyError):
        manager.delete_profile("Ausente")
    assert manager.path.read_bytes() == previous


def test_delete_write_failure_keeps_original_file(tmp_path, profile):
    manager = ProfileManager(tmp_path / "hunts.json")
    manager.save_profile("Existente", profile)
    previous = manager.path.read_bytes()
    with patch("core.profiles.os.replace", side_effect=OSError("disk error")):
        with pytest.raises(OSError):
            manager.delete_profile("Existente")
    assert manager.path.read_bytes() == previous
    assert not list(tmp_path.glob(".hunts-*.tmp"))


@pytest.mark.parametrize("tolerance", [0, 1, 2])
def test_arrival_tolerance_persists_after_restart(tmp_path, profile, tolerance):
    profile["arrival_tolerance"] = tolerance
    manager = ProfileManager(tmp_path / "hunts.json")
    manager.save_profile("Rota", profile)
    assert ProfileManager(manager.path).load_profile("Rota")["arrival_tolerance"] == tolerance


def test_legacy_profile_defaults_to_one_tile(tmp_path, profile):
    del profile["arrival_tolerance"]
    path = tmp_path / "hunts.json"
    path.write_text(json.dumps({"Antiga": profile}), encoding="utf-8")
    assert ProfileManager(path).load_profile("Antiga")["arrival_tolerance"] == 1


@pytest.mark.parametrize("value", [-1, 3, True, 1.5, "1", None])
def test_invalid_tolerance_does_not_overwrite_profile(tmp_path, profile, value):
    manager = ProfileManager(tmp_path / "hunts.json")
    manager.save_profile("Rota", profile)
    previous = manager.path.read_bytes()
    profile["arrival_tolerance"] = value
    with pytest.raises(ValueError):
        manager.save_profile("Rota", profile)
    assert manager.path.read_bytes() == previous
