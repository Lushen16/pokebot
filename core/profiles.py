from __future__ import annotations

import copy
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
from core.position import is_valid_position


class ProfileManager:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else Path(__file__).resolve().parents[1] / "hunts.json"
        self._lock = threading.RLock()

    @staticmethod
    def validate_profile(data, *, discard_origin=False):
        if not isinstance(data, dict) or not isinstance(data.get("waypoints"), list):
            raise ValueError("Perfil exige uma lista de waypoints")
        if type(data.get("loop")) is not bool:
            raise ValueError("Loop deve ser booleano")
        if not isinstance(data.get("config_healer", {}), dict):
            raise ValueError("Configuração do Healer deve ser um objeto")
        healer = data.get("config_healer", {})
        for field, minimum, maximum in (("hp_percent_threshold", 1, 99),
                                         ("pokemon_swap_delay_ms", 200, 3000),
                                         ("pokemon_reset_delay_ms", 0, 10000)):
            if field in healer and (type(healer[field]) is not int or not minimum <= healer[field] <= maximum):
                raise ValueError(f"Configuração inválida do Healer: {field}")
        for field in ("heal_move", "revive_hotkey"):
            if field in healer and not isinstance(healer[field], str):
                raise ValueError(f"Configuração inválida do Healer: {field}")
        from modules.walker import Walker
        arrival_tolerance = Walker.validate_arrival_tolerance(data.get("arrival_tolerance", 1))
        distance_guard = data.get("distance_guard")
        if distance_guard is not None:
            if not isinstance(distance_guard, dict) or set(distance_guard) != {"enabled", "max_distance", "resume_distance"}:
                raise ValueError("Configuração inválida de distância do Pokémon")
            Walker.validate_distance_guard(**distance_guard)
        waypoints = []
        for waypoint in data["waypoints"]:
            if not isinstance(waypoint, dict) or any(type(waypoint.get(axis)) is not int for axis in ("x", "y", "z")):
                raise ValueError("Waypoint exige X, Y e Z inteiros")
            if not is_valid_position(waypoint["x"], waypoint["y"], waypoint["z"]):
                raise ValueError("Waypoint fora dos limites do mapa")
            if discard_origin and waypoint["x"] == 0 and waypoint["y"] == 0:
                logging.getLogger(__name__).warning("Waypoint (0, 0) descartado ao carregar perfil")
                continue
            waypoints.append(Walker._waypoint(waypoint["x"], waypoint["y"], waypoint["z"],
                                              waypoint.get("action", "Walk"), waypoint.get("param", "")))
        result = {"waypoints": waypoints, "loop": data["loop"],
                "arrival_tolerance": arrival_tolerance,
                "config_healer": copy.deepcopy(data.get("config_healer", {}))}
        if distance_guard is not None:
            result["distance_guard"] = copy.deepcopy(distance_guard)
        return result

    def _read(self):
        if not self.path.exists():
            return {}
        with self.path.open(encoding="utf-8") as stream:
            profiles = json.load(stream)
        if not isinstance(profiles, dict):
            raise ValueError("hunts.json deve conter um objeto de perfis")
        return profiles

    def list_profiles(self):
        with self._lock:
            return sorted(self._read(), key=str.casefold)

    def load_profile(self, name):
        with self._lock:
            return self.validate_profile(self._read()[name], discard_origin=True)

    def save_profile(self, name, data):
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 120:
            raise ValueError("Nome da hunt deve ter de 1 a 120 caracteres")
        validated = self.validate_profile(data)
        with self._lock:
            profiles = self._read()
            profiles[name.strip()] = validated
            self._write(profiles)

    def delete_profile(self, name):
        with self._lock:
            profiles = self._read()
            del profiles[name]
            self._write(profiles)

    def _write(self, profiles):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix=".hunts-", suffix=".tmp", delete=False) as stream:
                temporary_path = Path(stream.name)
                json.dump(profiles, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
