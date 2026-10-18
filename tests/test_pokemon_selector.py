import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication

from core.party_roster import RosterMember, RosterSnapshot, unavailable_roster
from gui.pokemon_selector import PokemonHealerSelector


def detected(pid=123, names=None):
    names = names or {2: "Blissey", 3: "Meu atacante"}
    return RosterSnapshot(pid, tuple(RosterMember(slot, names.get(slot)) for slot in range(1, 7)), True)


@pytest.fixture
def selector(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    config = {
        "client": {"target_pid": 123},
        "pokemon_roster": {"2": "Nome manual antigo"},
        "healer": {"heal_pokemon_slot": 2, "pokemon_heal_hotkey": "2",
                   "heal_move": "m4", "pokemon_return_hotkey": "3"},
    }
    def get(*keys, default=None):
        return lookup(config, keys, default)
    def set_value(value, *keys):
        cur = config
        for k in keys[:-1]:
            cur = cur.setdefault(k, {})
        cur[keys[-1]] = value
    ctx = SimpleNamespace(get=get, set=set_value)
    provider = SimpleNamespace(get_roster=Mock(return_value=unavailable_roster(123, "Offsets nao validados.")))
    widget = PokemonHealerSelector(ctx, roster_provider=provider, process_provider=lambda: [123])
    widget.timer.stop()
    yield widget, config, provider
    widget.close()
    widget.deleteLater()
    app.processEvents()


def test_missing_offsets_show_slots_not_cached_manual_names(selector):
    widget, config, provider = selector
    assert widget.pokemon_combo.currentData() == "2"
    assert widget.return_combo.currentData() == "3"
    assert widget.skill_combo.currentData() == "m4"
    assert widget.pokemon_combo.currentText() == "Slot 2 (F2)"
    assert "Nomes nao detectados" in widget.status.text()
    assert len(widget.slot_combos) == 6
    assert all(not combo.isEditable() for combo in widget.slot_combos.values())
    provider.get_roster.assert_called_with(123)
    assert config["pokemon_roster"]["2"] == "Nome manual antigo"


def test_detect_button_updates_all_dropdowns_without_changing_slots(selector):
    widget, config, provider = selector
    provider.get_roster.return_value = detected()
    widget.detect_button.click()
    assert "Blissey" in widget.slot_combos[2].currentText()
    assert "Blissey" in widget.pokemon_combo.currentText()
    assert "Meu atacante" in widget.return_combo.currentText()
    assert "Blissey" in widget.reset_pokemon_combo.itemText(2)
    assert widget.pokemon_combo.currentData() == "2"
    assert widget.return_combo.currentData() == "3"
    assert config["healer"]["pokemon_heal_hotkey"] == "2"
    assert "Nomes recebidos" in widget.status.text()


def test_selection_saves_slot_and_skill_hotkeys(selector):
    widget, config, provider = selector
    provider.get_roster.return_value = detected(names={5: "Chansey"})
    widget.detect_button.click()
    widget.pokemon_combo.setCurrentIndex(widget.pokemon_combo.findData("5"))
    widget.skill_combo.setCurrentIndex(widget.skill_combo.findData("m2"))
    assert config["healer"]["heal_pokemon_name"] == "Chansey"
    assert config["healer"]["heal_pokemon_slot"] == 5
    assert config["healer"]["pokemon_heal_hotkey"] == "F5"
    assert config["healer"]["pokemon_heal_skill_hotkey"] == "F2"


def test_later_failure_clears_detected_names_but_preserves_choices(selector):
    widget, config, provider = selector
    provider.get_roster.return_value = detected()
    widget.detect_roster()
    provider.get_roster.side_effect = RuntimeError("Falha de leitura")
    widget.detect_roster()
    assert widget.pokemon_combo.currentText() == "Slot 2 (F2)"
    assert widget.return_combo.currentData() == "3"
    assert "Falha de leitura" in widget.status.text()


def test_wrong_pid_snapshot_is_rejected(selector):
    widget, _, provider = selector
    provider.get_roster.return_value = detected(pid=999)
    widget.detect_roster()
    assert "outro cliente" in widget.status.text()
    assert "Blissey" not in widget.pokemon_combo.currentText()


def test_pid_change_requests_new_roster(selector):
    widget, _, provider = selector
    widget.process_provider = lambda: [123, 456]
    widget.refresh()
    provider.get_roster.return_value = unavailable_roster(456, "Sem nomes")
    widget.client_combo.setCurrentIndex(widget.client_combo.findData(456))
    provider.get_roster.assert_called_with(456)
    assert widget._roster_snapshot.pid == 456


def test_return_reset_and_config_round_trip(selector, tmp_path):
    widget, config, _ = selector
    widget.return_combo.setCurrentIndex(widget.return_combo.findData("6"))
    widget.reset_pokemon_combo.setCurrentIndex(widget.reset_pokemon_combo.findData("1"))
    widget.reset_delay.setValue(700)
    assert config["healer"]["pokemon_return_hotkey"] == "F6"
    assert config["healer"]["pokemon_reset_hotkey"] == "F1"
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    loaded = json.loads(path.read_text(encoding="utf-8"))
    restored_ctx = SimpleNamespace(get=lambda *keys, default=None: lookup(loaded, keys, default), set=Mock())
    restored = PokemonHealerSelector(restored_ctx, process_provider=lambda: [123])
    restored.timer.stop()
    try:
        assert restored.reset_pokemon_combo.currentData() == "1"
        assert restored.return_combo.currentData() == "6"
        assert restored.pokemon_combo.currentData() == "2"
    finally:
        restored.close()
        restored.deleteLater()


def lookup(config, keys, default):
    value = config
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            return default
        value = value[key]
    return value


def test_save_selection_persists_unchanged_slots_as_function_keys(selector):
    widget, config, _ = selector
    widget.save_selection()
    assert config["healer"]["pokemon_heal_hotkey"] == "F2"
    assert config["healer"]["pokemon_return_hotkey"] == "F3"
    assert config["healer"]["pokemon_reset_hotkey"] == ""
    assert config["healer"]["pokemon_reset_delay_ms"] == 300


def test_selector_has_no_manual_text_fields(selector):
    from PySide6.QtWidgets import QAbstractSpinBox, QLineEdit
    widget, _, _ = selector
    assert all(isinstance(field.parent(), QAbstractSpinBox)
               for field in widget.findChildren(QLineEdit))


def test_default_provider_uses_detector_and_releases_reader(selector, monkeypatch):
    import gui.pokemon_selector as selector_module
    widget, _, _ = selector
    reader = Mock(pid=123, handle=42)
    offsets = Mock()
    offsets.find.return_value.is_resolved = True
    offsets.read.return_value = [{"slot": 2, "name": "Blissey"}]
    monkeypatch.setattr(selector_module, "MemoryReader", Mock(return_value=reader))
    monkeypatch.setattr(selector_module, "load_offsets", Mock(return_value=offsets))
    snapshot = widget.get_roster(123)
    assert snapshot.detected
    assert snapshot.members[1].name == "Blissey"
    offsets.read.assert_called_once_with(reader, "pokemon.roster")
    reader.attach.assert_called_once_with()
    reader.detach.assert_called_once_with()


def test_save_and_activate_healer_persists_settings_and_enables_module(selector):
    widget, config, provider = selector
    provider.get_roster.return_value = detected(names={1: "Togepi", 3: "Nina"})
    widget.detect_button.click()
    widget.pokemon_combo.setCurrentIndex(widget.pokemon_combo.findData("1"))
    widget.skill_combo.setCurrentIndex(widget.skill_combo.findData("m2"))
    widget.return_combo.setCurrentIndex(widget.return_combo.findData("3"))
    widget.spin_hp_thresh.setValue(60)

    widget.btn_save_activate.click()

    assert config["modules"]["healer"]["enabled"] is True
    assert config["healer"]["heal_pokemon_name"] == "Togepi"
    assert config["healer"]["pokemon_heal_hotkey"] == "F1"
    assert config["healer"]["heal_move"] == "m2"
    assert config["healer"]["pokemon_heal_skill_hotkey"] == "F2"
    assert config["healer"]["return_pokemon_name"] == "Nina"
    assert config["healer"]["pokemon_return_hotkey"] == "F3"
    assert config["healer"]["hp_percent_threshold"] == 60
    assert "ATIVADO" in widget.status.text()


def test_test_healer_button_invokes_callback(selector):
    widget, config, provider = selector
    called = []
    widget.test_healer_callback = lambda: called.append(True)
    widget.btn_test_healer.click()
    assert called == [True]
