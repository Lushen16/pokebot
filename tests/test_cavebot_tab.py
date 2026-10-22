import os
from unittest.mock import MagicMock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from modules.walker import Walker, WalkerState
from ui.cavebot_tab import CavebotTab


@pytest.fixture
def cavebot():
    app = QApplication.instance() or QApplication([])
    walker = Walker(input_bus=MagicMock(), loop=False)
    walker.read_player_position = MagicMock(return_value=(100, 200, 7))
    tab = CavebotTab(walker)
    yield app, tab, walker
    walker.stop(timeout=0.5)
    if isValid(tab):
        tab.close()
        tab.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def test_route_editor_defaults_to_compact_view_without_changing_route(cavebot):
    _, tab, walker = cavebot
    original = walker.route_snapshot()
    assert not tab.advanced_section.toggle.isChecked()
    assert tab.advanced_section.content.isHidden()
    assert tab.table_waypoints.isColumnHidden(3)
    tab.chk_show_floor.setChecked(True)
    assert not tab.table_waypoints.isColumnHidden(3)
    tab.advanced_section.toggle.setChecked(True)
    assert not tab.advanced_section.content.isHidden()
    assert walker.route_snapshot() == original


def test_controls_have_all_actions_and_default_interval(cavebot):
    _, tab, _ = cavebot
    assert [tab.combo_action.itemText(index) for index in range(tab.combo_action.count())] == [
        "Walk", "Wait", "Say", "Safe Spot", "BOX", "Stair"]
    assert tab.spin_interval.value() == 3


def test_distance_guard_controls_enforce_hysteresis_and_rebind(cavebot):
    _, tab, walker = cavebot
    tab.chk_distance_guard.setChecked(True)
    tab.spin_distance_max.setValue(6)
    tab.spin_distance_resume.setValue(4)
    assert walker.route_snapshot()["distance_guard"] == {
        "enabled": True, "max_distance": 6, "resume_distance": 4}
    tab.spin_distance_max.setValue(3)
    assert tab.spin_distance_resume.value() == 2
    assert walker.distance_guard_resume == 2
    walker.configure_distance_guard(enabled=False, max_distance=8, resume_distance=1)
    tab.set_walker(walker)
    assert not tab.chk_distance_guard.isChecked()
    assert tab.spin_distance_max.value() == 8
    assert tab.spin_distance_resume.value() == 1

def test_distance_guard_profile_load_and_pause_status(cavebot, tmp_path):
    from core.profiles import ProfileManager
    _, tab, walker = cavebot
    walker.add_waypoint(120, 200, 7)
    walker.configure_distance_guard(enabled=True, max_distance=7, resume_distance=2)
    tab.profile_manager = ProfileManager(tmp_path / "hunts.json")
    tab.profile_manager.save_profile("Distância", walker.route_snapshot())
    walker.configure_distance_guard(enabled=False)
    tab._refresh_profiles("Distância")
    tab._on_load_profile()
    assert tab.chk_distance_guard.isChecked()
    assert tab.spin_distance_max.value() == 7
    assert tab.spin_distance_resume.value() == 2
    walker.pokemon_position_provider = lambda: None
    walker.tick()
    tab._refresh_state()
    assert "POSIÇÃO DO POKÉMON INDISPONÍVEL" in tab.lbl_state.text()
    assert "distance_unavailable" in walker.stats["pause_reasons"]

def test_dynamic_lure_controls_sync_and_persist(cavebot):
    _, tab, walker = cavebot
    assert tab.chk_dynamic_lure.isChecked() == walker.dynamic_lure_enabled
    assert tab.spin_dynamic_max.value() == walker.dynamic_lure_max_distance
    assert tab.spin_dynamic_resume.value() == walker.dynamic_lure_resume_distance
    assert tab.chk_dynamic_move.isChecked() == walker.dynamic_lure_use_move_command
    assert tab.edit_dynamic_move.text() == walker.dynamic_lure_move_command

    tab.chk_dynamic_lure.setChecked(True)
    tab.spin_dynamic_max.setValue(6)
    tab.spin_dynamic_resume.setValue(4)
    tab.edit_dynamic_move.setText("{name}, move {x} {y} {z}!")
    tab.edit_dynamic_move.editingFinished.emit()
    assert walker.dynamic_lure_enabled is True
    assert walker.dynamic_lure_max_distance == 6
    assert walker.dynamic_lure_resume_distance == 4
    assert walker.dynamic_lure_move_command == "{name}, move {x} {y} {z}!"
    assert walker.ctx.get("walker", "dynamic_lure_enabled") is True
    assert walker.ctx.get("walker", "dynamic_lure_max_distance") == 6
    assert walker.ctx.get("walker", "dynamic_lure_move_command") == "{name}, move {x} {y} {z}!"

    tab.spin_dynamic_max.setValue(3)
    assert tab.spin_dynamic_resume.value() == 2
    assert walker.dynamic_lure_resume_distance == 2


def test_replacing_walker_syncs_dynamic_lure_controls(cavebot):
    _, tab, _ = cavebot
    other = Walker(input_bus=MagicMock(), loop=False)
    other.dynamic_lure_enabled = True
    other.dynamic_lure_max_distance = 7
    other.dynamic_lure_resume_distance = 2
    other.dynamic_lure_use_move_command = False
    other.dynamic_lure_move_command = "{name}, venha!"
    tab.set_walker(other)
    assert tab.chk_dynamic_lure.isChecked() is True
    assert tab.spin_dynamic_max.value() == 7
    assert tab.spin_dynamic_resume.value() == 2
    assert tab.chk_dynamic_move.isChecked() is False
    assert tab.edit_dynamic_move.text() == "{name}, venha!"
    other.stop(timeout=0.5)


def test_adding_waypoint_preserves_action_and_parameter(cavebot):
    _, tab, walker = cavebot
    tab.combo_action.setCurrentText("Say")
    QTest.keyClicks(tab.edit_param, "!pokestop")
    tab.btn_add_current.click()
    assert walker.waypoint_snapshot() == [
        {"x": 100, "y": 200, "z": 7, "action": "Say", "param": "!pokestop"}]
    assert tab.table_waypoints.item(0, 5).text() == "!pokestop"


def test_refresh_shows_waiting_and_stop_never_joins_on_gui_thread(cavebot):
    _, tab, walker = cavebot
    walker.add_waypoint(100, 200, 7, "Wait", "3600000")
    walker.tick()
    tab._refresh_state()
    assert "AGUARDANDO" in tab.lbl_state.text()
    walker.stop = MagicMock(side_effect=AssertionError("GUI must not join worker"))
    tab.btn_stop_route.setEnabled(True)
    tab.btn_stop_route.click()
    assert walker.state == WalkerState.IDLE
    walker.stop.assert_not_called()
    del walker.stop


def test_recording_uses_live_options_and_interval(cavebot):
    _, tab, walker = cavebot
    tab.spin_interval.setValue(4)
    tab.btn_record.click()
    assert walker.recording
    assert walker.record_sqm_interval == 4
    tab.combo_action.setCurrentText("Say")
    tab.edit_param.setText("!pokestop")
    walker.read_player_position.return_value = (104, 200, 7)
    tab._refresh_state()
    assert walker.waypoints[-1]["action"] == "Say"
    assert walker.waypoints[-1]["param"] == "!pokestop"
    tab.btn_record.click()
    assert not walker.recording


def test_recording_continues_through_gui_refresh_without_stopping(cavebot, monkeypatch):
    _, tab, walker = cavebot
    clock = [0.0]
    monkeypatch.setattr("modules.walker.time.monotonic", lambda: clock[0])
    tab.btn_record.click()
    for step in range(1, 7):
        clock[0] += 0.1
        walker.read_player_position.return_value = (100 + step, 200, 7)
        tab._refresh_state()
    assert [point["x"] for point in walker.waypoint_snapshot()] == [100, 103, 106]
    assert tab.table_waypoints.rowCount() == 3
    tab.btn_record.click()


def test_recording_waits_for_valid_memory_then_recovers(cavebot):
    _, tab, walker = cavebot
    walker.read_player_position.return_value = None
    tab.btn_record.click()
    tab._refresh_state()
    assert "AGUARDANDO POSIÇÃO" in tab.lbl_state.text()
    assert walker.waypoint_snapshot() == []
    walker.read_player_position.return_value = (100, 200, 7)
    tab._refresh_state()
    assert tab.lbl_state.text() == "GRAVANDO"
    assert len(walker.waypoint_snapshot()) == 1
    tab.btn_record.click()


def test_nearest_button_rotates_and_starts(cavebot):
    _, tab, walker = cavebot
    walker.add_waypoint(120, 200, 7)
    walker.add_waypoint(101, 200, 7)
    walker.start = MagicMock(return_value=True)
    tab.btn_start_nearest.click()
    assert walker.waypoints[0]["x"] == 101
    walker.start.assert_called_once_with()


def test_invalid_wait_does_not_mutate_route(cavebot, monkeypatch):
    _, tab, walker = cavebot
    warning = MagicMock()
    monkeypatch.setattr("ui.cavebot_tab.QMessageBox.warning", warning)
    tab.combo_action.setCurrentText("Wait")
    tab.edit_param.setText("invalid")
    tab.btn_add_wp.click()
    warning.assert_called_once()
    assert not walker.waypoints


def test_parent_destruction_deletes_owned_timer_and_stops_route(cavebot):
    _, tab, walker = cavebot
    timer = tab.debug_timer
    tab.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    assert not isValid(timer)
    assert not isValid(tab)
    assert walker.stop_event.is_set()



def test_loading_profile_updates_walker_loop_and_emits_healer_settings(cavebot, tmp_path):
    from core.profiles import ProfileManager
    _, tab, walker = cavebot
    manager = ProfileManager(tmp_path / "hunts.json")
    data = {"waypoints": [{"x": 123, "y": 456, "z": 7, "action": "Say", "param": "!pokestop"}],
            "loop": True, "config_healer": {"hp_percent_threshold": 61}}
    manager.save_profile("Minha Hunt", data)
    tab.profile_manager = manager
    tab._refresh_profiles("Minha Hunt")
    emitted = []
    tab.healer_profile_loaded.connect(emitted.append)
    tab.btn_load_profile.click()
    assert walker.waypoint_snapshot() == data["waypoints"]
    assert walker.loop and tab.chk_loop.isChecked()
    assert emitted == [data["config_healer"]]


def test_save_profile_dialog_cancel_has_no_side_effects(cavebot, tmp_path, monkeypatch):
    from core.profiles import ProfileManager
    _, tab, _ = cavebot
    tab.profile_manager = ProfileManager(tmp_path / "hunts.json")
    monkeypatch.setattr("ui.cavebot_tab.QInputDialog.getText", lambda *args, **kwargs: ("ignored", False))
    tab.btn_save_profile.click()
    assert not tab.profile_manager.path.exists()


def test_save_profile_includes_current_healer_and_survives_reopen(cavebot, tmp_path, monkeypatch):
    from core.profiles import ProfileManager
    _, tab, walker = cavebot
    manager = ProfileManager(tmp_path / "hunts.json")
    tab.profile_manager = manager
    tab.healer_provider = lambda: {"hp_percent_threshold": 65}
    walker.add_waypoint(100, 200, 7)
    monkeypatch.setattr("ui.cavebot_tab.QInputDialog.getText", lambda *args, **kwargs: ("Rota", True))
    tab.btn_save_profile.click()
    assert tab.combo_profiles.currentText() == "Rota"
    assert ProfileManager(manager.path).load_profile("Rota")["config_healer"] == {"hp_percent_threshold": 65}



def test_position_label_shows_updating_and_recovers_green(cavebot):
    _, tab, walker = cavebot
    walker.read_player_position.return_value = None
    tab._refresh_state()
    assert "Atualizando" in tab.lbl_position.text()
    walker.read_player_position.return_value = (100, 200, 7)
    tab._refresh_state()
    assert "X: 100" in tab.lbl_position.text()
    assert "#00E676" in tab.lbl_position.styleSheet()


def test_waypoint_table_is_read_only_and_keeps_action_parameter(cavebot):
    from PySide6.QtWidgets import QAbstractItemView
    _, tab, walker = cavebot
    walker.add_waypoint(100, 200, 7, "Wait", "1000")
    tab._sync_waypoint_list()
    assert tab.table_waypoints.rowCount() == 1
    assert [tab.table_waypoints.item(0, column).text() for column in range(6)] == [
        "1", "100", "200", "7", "Wait", "1000"]
    assert tab.table_waypoints.editTriggers() == QAbstractItemView.NoEditTriggers


def test_delete_hunt_requires_confirmation_and_keeps_route(cavebot, tmp_path, monkeypatch):
    from core.profiles import ProfileManager
    from PySide6.QtWidgets import QMessageBox
    _, tab, walker = cavebot
    walker.add_waypoint(100, 200, 7)
    tab.profile_manager = ProfileManager(tmp_path / "hunts.json")
    tab.profile_manager.save_profile("Teste", walker.route_snapshot())
    tab._refresh_profiles("Teste")
    tab.active_profile = "Teste"
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.No)
    tab.btn_delete_profile.click()
    assert tab.profile_manager.list_profiles() == ["Teste"]
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Yes)
    tab.btn_delete_profile.click()
    assert tab.profile_manager.list_profiles() == []
    assert tab.active_profile == ""
    assert walker.waypoint_snapshot() == [{"x": 100, "y": 200, "z": 7, "action": "Walk", "param": ""}]


def test_waypoint_action_icons_and_record_button_state(cavebot):
    _, tab, walker = cavebot
    walker.add_waypoint(100, 200, 7, "Say", "!pokestop")
    tab._sync_waypoint_list()
    assert not tab.table_waypoints.item(0, 0).icon().isNull()
    assert tab.table_waypoints.item(0, 0).toolTip() == "Say"
    assert tab.btn_record.property("iconName") == "record"
    tab.btn_record.click()
    assert tab.btn_record.property("iconName") == "stop"
    tab.btn_record.click()
    assert tab.btn_record.property("iconName") == "record"



def test_arrival_tolerance_controls_and_profile_roundtrip(cavebot, tmp_path, monkeypatch):
    from core.profiles import ProfileManager
    from PySide6.QtWidgets import QInputDialog
    _, tab, walker = cavebot
    control = tab.spin_arrival_tolerance
    assert (control.minimum(), control.maximum(), control.value()) == (0, 2, 1)
    control.setValue(2)
    assert walker.arrival_tolerance == 2
    tab.profile_manager = ProfileManager(tmp_path / "hunts.json")
    monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: ("Margem", True))
    tab.btn_save_profile.click()
    assert tab.profile_manager.load_profile("Margem")["arrival_tolerance"] == 2
    control.setValue(0)
    tab.btn_load_profile.click()
    assert control.value() == walker.arrival_tolerance == 2


def test_replacing_walker_synchronizes_tolerance_without_overwriting(cavebot):
    _, tab, _ = cavebot
    replacement = Walker(input_bus=MagicMock(), arrival_tolerance=0)
    tab.set_walker(replacement)
    assert tab.spin_arrival_tolerance.value() == replacement.arrival_tolerance == 0
    tab.spin_arrival_tolerance.setValue(2)
    assert replacement.arrival_tolerance == 2



def test_table_initializes_empty_without_ghost_waypoint(cavebot):
    _, tab, walker = cavebot
    tab._refresh_state()
    assert tab.table_waypoints.rowCount() == 0
    assert not walker.waypoint_snapshot()
    assert tab.spin_z.value() == 7


@pytest.mark.parametrize("position,expected_z", [((100, 200, 8), 8), (None, 7), ((65535, 65535, 255), 7)])
def test_manual_waypoint_uses_live_floor_or_seven(cavebot, position, expected_z):
    _, tab, walker = cavebot
    tab.spin_x.setValue(123)
    tab.spin_y.setValue(456)
    tab.spin_z.setValue(15)
    walker.read_player_position.return_value = position
    tab.btn_add_wp.click()
    assert walker.waypoint_snapshot()[0] == {
        "x": 123, "y": 456, "z": expected_z, "action": "Walk", "param": ""}
    assert tab.table_waypoints.item(0, 3).text() == str(expected_z)
    assert tab.spin_z.value() == expected_z


def test_current_position_uses_live_floor_without_clamping_sentinel(cavebot):
    _, tab, walker = cavebot
    walker.read_player_position.return_value = (100, 200, 8)
    tab.btn_add_current.click()
    assert walker.waypoints[0]["z"] == 8
    walker.read_player_position.return_value = (65535, 65535, 255)
    tab.btn_add_current.click()
    assert len(walker.waypoints) == 1
    assert tab.spin_z.value() == 7


def test_delete_selected_waypoint_updates_route_and_numbering(cavebot):
    _, tab, walker = cavebot
    for coordinate in (100, 101, 102):
        walker.add_waypoint(coordinate, 200, 7)
    tab._sync_waypoint_list()
    tab.table_waypoints.selectRow(1)
    tab.btn_delete_wp.click()
    assert [point["x"] for point in walker.waypoint_snapshot()] == [100, 102]
    assert tab.table_waypoints.rowCount() == 2
    assert tab.table_waypoints.item(1, 0).text() == "2"
    assert tab.table_waypoints.item(1, 1).text() == "102"
    tab.table_waypoints.clearSelection()
    tab.btn_delete_wp.click()
    assert len(walker.waypoints) == 2



def test_gui_warns_once_on_persistent_memory_failure_and_recovers(cavebot, monkeypatch, caplog):
    _, tab, walker = cavebot
    clock = [10.0]
    monkeypatch.setattr("ui.cavebot_tab.time.monotonic", lambda: clock[0])
    walker.add_waypoint(105, 200, 7)
    walker._running = True
    walker.state = WalkerState.MOVING
    walker.read_player_position.return_value = None
    tab._refresh_state()
    clock[0] = 15.1
    tab._refresh_state()
    assert "MEMÓRIA" in tab.lbl_state.text()
    assert "#FFAB00" in tab.lbl_state.styleSheet()
    tab._refresh_state()
    assert sum("Walker parado, aguardando leitura de memória" in message for message in caplog.messages) == 1
    walker.read_player_position.return_value = (100, 200, 7)
    tab._refresh_state()
    assert tab.lbl_state.text() == "MOVENDO"
    assert not tab.lbl_state.styleSheet()
    walker._running = False


def test_gui_does_not_warn_during_programmed_long_wait(cavebot, monkeypatch, caplog):
    _, tab, walker = cavebot
    clock = [10.0]
    monkeypatch.setattr("ui.cavebot_tab.time.monotonic", lambda: clock[0])
    walker.add_waypoint(100, 200, 7, "Wait", "10000")
    walker.tick()
    walker._running = True
    tab._refresh_state()
    clock[0] = 16.0
    tab._refresh_state()
    assert tab.lbl_state.text() == "AGUARDANDO"
    assert not tab.lbl_state.styleSheet()
    assert "Walker parado, aguardando leitura de memória" not in caplog.text
    walker._running = False
