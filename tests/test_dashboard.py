import os
import time
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from context import BotContext
import gui.main as gui
from gui.dashboard_tab import MemoryMonitor, LiveStatusPanel


@pytest.fixture
def dashboard(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    path = tmp_path / "config.json"
    path.write_text('{}', encoding="utf-8")
    ctx = BotContext(config_path=str(path))
    monkeypatch.setattr(gui, "get_context", lambda *args: ctx)
    monkeypatch.setattr(gui.MainWindow, "_setup_tray", lambda self: None)
    monkeypatch.setattr(gui.PokemonHealerSelector, "detect_roster", lambda self: None)
    window = gui.MainWindow(str(path))
    yield app, window
    window.close()
    deadline = time.monotonic() + 3
    while window._closing and not window._allow_close and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    assert window._allow_close
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def wait_for(app, predicate):
    deadline = time.monotonic() + 3
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    assert predicate()


def fake_resolver():
    values = {"player.hp": 205, "player.hp_max": 205, "player.x": 3312, "player.y": 2051, "player.z": 7}
    return SimpleNamespace(read_entry=Mock(side_effect=values.get), last_error=None,
                           proc=SimpleNamespace(pid=123, detach=Mock()))


def test_dashboard_is_only_visible_process_selector_and_starts_off(dashboard):
    _, window = dashboard
    assert window.header.isAncestorOf(window.btn_select_process)
    assert not window.btn_start.isEnabled()
    assert window.pokemon_healer_selector.client_combo.isHidden()
    assert window.pokemon_healer_selector.btn_select_client.isHidden()
    assert "SEM CONEXÃO" in window.live_panel.connection.text()


def test_selection_starts_reading_without_automation(dashboard, monkeypatch):
    import inputbus
    import memory
    app, window = dashboard
    resolver = fake_resolver()
    bus = Mock()
    monkeypatch.setattr(inputbus, "get_bus", lambda: bus)
    monkeypatch.setattr(memory, "connect_live_resolver", Mock(return_value=resolver))
    factory = Mock()
    monkeypatch.setattr(gui, "create_engine", factory)
    window._pending_target = ("PokeAlliance_dx.exe", 123)
    window._begin_memory_connection()
    wait_for(app, lambda: "3312" in window.lbl_player_pos.text())
    assert window.resolver is resolver
    assert window.tab_cavebot.walker.read_player_position() == (3312, 2051, 7)
    assert window.lbl_player_hp.text() == "205 / 205"
    assert window.engine is None
    factory.assert_not_called()
    bus.start.assert_not_called()
    assert window.btn_start.isEnabled()


def test_snapshot_failure_clears_stale_values(dashboard):
    _, window = dashboard
    window.live_panel.show_snapshot({"valid": True, "hp": 205, "hp_max": 205, "position": (1, 2, 7)})
    window.live_panel.show_snapshot({"valid": False, "error": "cliente fechado"})
    assert "1" not in window.lbl_player_pos.text()
    assert window.lbl_player_hp.text() == "—"
    assert window.live_panel.connection.toolTip() == "cliente fechado"


def test_connection_failure_is_reported_and_thread_is_cleaned_up(dashboard, monkeypatch):
    import memory
    app, window = dashboard
    popup = Mock()
    monkeypatch.setattr(gui.QMessageBox, "warning", popup)
    monkeypatch.setattr(memory, "connect_live_resolver", Mock(side_effect=RuntimeError("processo fechado")))
    window._pending_target = ("PokeAlliance_dx.exe", 123)
    window._begin_memory_connection()
    wait_for(app, lambda: window.memory_thread is None)
    assert window.resolver is None and not window.btn_start.isEnabled()
    assert "processo fechado" in popup.call_args.args[2]


def test_monitor_snapshot_uses_current_values_and_invalid_z_is_not_green():
    monitor = MemoryMonitor(123, "config.json")
    monitor.resolver = fake_resolver()
    assert monitor.read_snapshot()["position"] == (3312, 2051, 7)
    monitor.resolver.read_entry.side_effect = lambda field: 255 if field == "player.z" else 205
    assert not monitor.read_snapshot()["valid"]
    assert monitor.read_snapshot()["position"] is None


def test_loading_healer_updates_controls_without_starting_modules(dashboard):
    _, window = dashboard
    settings = {"heal_move": "m2", "revive_hotkey": "Ctrl+F", "hp_percent_threshold": 55,
                "pokemon_swap_delay_ms": 800, "pokemon_reset_delay_ms": 350}
    window._apply_healer_profile(settings)
    selector = window.pokemon_healer_selector
    assert selector.skill_combo.currentData() == "m2"
    assert selector.revive_combo.currentData() == "Ctrl+F"
    assert selector.spin_hp_thresh.value() == 55
    assert selector.spin_action_delay.value() == 800
    assert window.ctx.get("healer") == settings
    assert window.engine is None



def test_global_start_stop_preserves_memory_and_loaded_route(dashboard, monkeypatch):
    import inputbus
    import memory
    from modules.walker import Walker
    app, window = dashboard
    resolver = fake_resolver()
    bus = Mock()
    monkeypatch.setattr(inputbus, "get_bus", lambda: bus)
    monkeypatch.setattr(memory, "connect_live_resolver", Mock(return_value=resolver))
    window._pending_target = ("PokeAlliance_dx.exe", 123)
    window._begin_memory_connection()
    wait_for(app, lambda: window._memory_valid)
    route = [{"x": 3312, "y": 2051, "z": 7, "action": "Walk", "param": ""}]
    window.tab_cavebot.walker.replace_route(route, True, arrival_tolerance=2)
    started = []

    def make_engine(**kwargs):
        engine = Mock()
        engine.ctx = window.ctx
        engine.walker = Walker(offset_registry=resolver, input_bus=bus)
        engine.start.side_effect = lambda: (window.ctx.stop_event.clear(), started.append(True))
        engine.stop.side_effect = lambda: (engine.walker.stop(), window.ctx.stop())
        engine.get_status.return_value = {"running": True, "stats": {}}
        return engine

    monkeypatch.setattr(gui, "create_engine", make_engine)
    for cycle in range(2):
        window._on_start()
        wait_for(app, lambda: len(started) == cycle + 1)
        assert window.engine.walker.waypoint_snapshot() == route
        assert window.engine.walker.arrival_tolerance == 2
        window._on_stop()
        wait_for(app, lambda: window.worker is None)
        assert window.resolver is resolver
        assert window.memory_thread.isRunning()
        assert window.tab_cavebot.walker.waypoint_snapshot() == route
        assert window.tab_cavebot.walker.arrival_tolerance == 2
        assert window.tab_cavebot.spin_arrival_tolerance.value() == 2
        resolver.proc.detach.assert_not_called()


def test_process_reconnect_detaches_old_handle_and_preserves_profile(dashboard, monkeypatch):
    import inputbus
    import memory
    app, window = dashboard
    first, second = fake_resolver(), fake_resolver()
    second.proc.pid = 456
    monkeypatch.setattr(inputbus, "get_bus", lambda: Mock())
    monkeypatch.setattr(memory, "connect_live_resolver", Mock(side_effect=[first, second]))
    window._pending_target = ("PokeAlliance_dx.exe", 123)
    window._begin_memory_connection()
    wait_for(app, lambda: window._memory_valid)
    window.tab_cavebot.walker.add_waypoint(3333, 2051, 7)
    window.tab_cavebot.spin_arrival_tolerance.setValue(0)
    window._pending_target = ("PokeAlliance_dx.exe", 456)
    window.memory_thread.stop()
    wait_for(app, lambda: window.resolver is second and window._memory_valid)
    first.proc.detach.assert_called_once()
    assert window.tab_cavebot.walker.waypoints[0]["x"] == 3333
    assert window.tab_cavebot.walker.arrival_tolerance == 0
    assert window.tab_cavebot.spin_arrival_tolerance.value() == 0
    second.proc.detach.assert_not_called()



def test_stop_before_worker_run_never_starts_engine():
    engine = Mock()
    worker = gui.BotWorker(engine, fake_resolver())
    worker.stop()
    worker.run()
    engine.start.assert_not_called()
    engine.walker.request_stop.assert_called_once()


def test_invalid_live_snapshot_requests_stop_without_retaining_coordinates(dashboard, monkeypatch):
    _, window = dashboard
    stop = Mock()
    monkeypatch.setattr(window, "_on_stop", stop)
    window.worker = Mock()
    window._receive_memory_snapshot({"valid": False, "error": "ponteiro nulo"})
    assert not window._memory_valid
    assert window.lbl_player_hp.text() == "—"
    stop.assert_called_once()
    window.worker = None


def test_close_during_connection_waits_for_monitor_and_releases_process(dashboard, monkeypatch):
    import memory
    app, window = dashboard
    resolver = fake_resolver()
    monkeypatch.setattr(memory, "connect_live_resolver", Mock(return_value=resolver))
    window._pending_target = ("PokeAlliance_dx.exe", 123)
    window._begin_memory_connection()
    window.close()
    wait_for(app, lambda: window._allow_close)
    assert window.memory_thread is None
    resolver.proc.detach.assert_called_once()



def test_coordinate_only_failure_does_not_stop_engine_and_stop_button_remains_enabled(dashboard, monkeypatch):
    _, window = dashboard
    stop = Mock()
    monkeypatch.setattr(window, "_on_stop", stop)
    window.resolver = fake_resolver()
    window.worker = Mock()
    window._receive_memory_snapshot({"valid": False, "hp_valid": True, "position_valid": False,
                                     "hp": 205, "hp_max": 205, "position": None})
    window._update_status_bar()
    stop.assert_not_called()
    assert window.btn_toggle_bot.isEnabled()
    assert "Atualizando" in window.lbl_player_pos.text()
    window.worker = None
    window.resolver = None


def test_monitor_rejects_xy_sentinel_even_with_valid_z():
    monitor = MemoryMonitor(123, "config.json")
    monitor.resolver = fake_resolver()
    monitor.resolver.read_entry.side_effect = lambda field: 65535 if field == "player.x" else 7 if field == "player.z" else 205
    snapshot = monitor.read_snapshot()
    assert snapshot["position"] is None
    assert snapshot["position_valid"] is False and snapshot["hp_valid"] is True


def test_panel_sanitizes_raw_sentinels_and_recovers_green(dashboard):
    _, window = dashboard
    panel = window.live_panel
    panel.show_snapshot({"valid": True, "position": (65535, 65535, 255), "hp": 205, "hp_max": 205})
    assert "65535" not in panel.position.text()
    assert "Atualizando" in panel.position.text()
    panel.show_snapshot({"valid": True, "position": (3312, 2051, 7), "hp": 205, "hp_max": 205})
    assert "3312" in panel.position.text()
    assert "#00c853" in panel.styleSheet()



@pytest.mark.parametrize("failed_field, hp_valid", [("player.x", True), ("player.hp", False)])
def test_monitor_read_exception_preserves_position_vs_hp_failure_classification(failed_field, hp_valid):
    monitor = MemoryMonitor(123, "config.json")
    resolver = fake_resolver()
    original_read = resolver.read_entry.side_effect
    def read(field):
        if field == failed_field:
            raise OSError("temporary read failure")
        return original_read(field)
    resolver.read_entry.side_effect = read
    monitor.resolver = resolver
    snapshot = monitor.read_snapshot()
    assert not snapshot["valid"]
    assert snapshot["hp_valid"] is hp_valid
    assert "temporary read failure" in snapshot["error"]


def test_header_refresh_does_not_attach_and_selection_connects(dashboard, monkeypatch):
    _, window = dashboard
    monkeypatch.setattr("gui.process_selector.enumerate_windows_processes",
                        lambda: [("PokeAlliance_dx.exe", 4321)])
    begin = Mock()
    monkeypatch.setattr(window, "_begin_memory_connection", begin)
    window._refresh_header_processes()
    begin.assert_not_called()
    assert window.combo_processes.count() == 2
    window._select_header_process(1)
    assert window._pending_target == ("PokeAlliance_dx.exe", 4321)
    begin.assert_called_once_with()
    window._pending_target = None


def test_combat_advanced_is_collapsed_and_preserves_controls(dashboard):
    _, window = dashboard
    selector = window.pokemon_healer_selector
    assert selector.advanced.content.isHidden()
    assert selector.layout().labelForField(selector.pokemon_combo).text() == "Pokémon de Cura:"
    assert selector.layout().labelForField(selector.return_combo).text() == "Pokémon de Caça (Retorno):"
    assert selector.advanced.isAncestorOf(selector.spin_action_delay)
    assert selector.advanced.isAncestorOf(selector.reset_delay)
    assert selector.advanced.isAncestorOf(selector.revive_combo)
    selector.advanced.toggle.click()
    assert not selector.advanced.content.isHidden()
    selector.advanced.toggle.click()
    assert selector.advanced.content.isHidden()


def test_dashboard_route_controls_and_recent_log_limit(dashboard):
    _, window = dashboard
    assert window.tab_dashboard.isAncestorOf(window.tab_cavebot.btn_start_route)
    assert window.header.isAncestorOf(window.btn_toggle_bot)
    for index in range(8):
        window._log("INFO", f"Evento {index} <teste>")
    messages = window.log_view.toPlainText()
    assert window.log_view.document().blockCount() == 5
    assert "Evento 0" not in messages
    assert "Evento 7 <teste>" in messages


def test_health_bar_clears_invalid_snapshot(dashboard):
    _, window = dashboard
    panel = window.live_panel
    panel.show_snapshot({"valid": True, "hp": 50, "hp_max": 200, "position": (1, 2, 7)})
    assert panel.hp_bar.value() == 25
    panel.show_snapshot({"valid": False})
    assert panel.hp_bar.value() == 0
    assert not panel.hp_bar.isEnabled()


def test_master_button_stop_style_survives_position_failure(dashboard):
    _, window = dashboard
    window.worker = Mock()
    window._position_updating = True
    window.resolver = Mock()
    window._update_status_bar()
    assert window.btn_toggle_bot.objectName() == "stopBtn"
    assert "DESLIGAR" in window.btn_toggle_bot.text()
    assert window.btn_toggle_bot.isEnabled()
    window.worker = None
    window.resolver = None


def test_primary_pages_scroll_without_clipping_controls(dashboard):
    _, window = dashboard
    assert window.page_dashboard.widget() is window.tab_dashboard
    assert window.page_cavebot.widget() is window.tab_cavebot
    assert window.page_dashboard.widgetResizable()
    assert window.page_cavebot.widgetResizable()
    assert window.tab_cavebot.control_group.isAncestorOf(window.tab_cavebot.btn_record)
    assert window.tab_cavebot.control_group.isAncestorOf(window.tab_cavebot.chk_loop)


@pytest.mark.parametrize("hp, maximum, health", [(0, 100, "critical"), (19, 100, "critical"),
                                                (20, 100, "warning"), (60, 100, "warning"),
                                                (61, 100, "good"), (100, 100, "good"),
                                                (1, 0, "unknown"), (None, None, "unknown")])
def test_health_color_thresholds(dashboard, hp, maximum, health):
    _, window = dashboard
    window.live_panel.show_snapshot({"valid": True, "hp": hp, "hp_max": maximum, "position": (1, 2, 7)})
    assert window.live_panel.hp_bar.property("health") == health
    if health == "unknown":
        assert window.live_panel.hp_bar.value() == 0
        assert window.live_panel.hp_bar.format() == "Sem leitura"
    else:
        assert window.live_panel.hp_bar.format() == "%p%"


def test_tabs_actions_and_glow_follow_master_state(dashboard):
    _, window = dashboard
    assert all(not window.tabs.tabIcon(index).isNull() for index in range(window.tabs.count()))
    assert window.btn_toggle_bot.property("iconName") == "play"
    assert window.btn_pause.property("iconName") == "pause"
    effect = window.master_glow
    window._update_status_bar()
    assert not effect.isEnabled()
    window.resolver = Mock()
    window._memory_valid = True
    window._update_status_bar()
    assert effect.isEnabled()
    window.worker = Mock()
    window._update_status_bar()
    assert not effect.isEnabled()
    assert window.btn_toggle_bot.property("iconName") == "stop"
    assert window.btn_toggle_bot.objectName() == "stopBtn"
    assert window.btn_toggle_bot.graphicsEffect() is effect
    window.worker = None
    window.resolver = None
