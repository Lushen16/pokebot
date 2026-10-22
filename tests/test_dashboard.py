import os
import time
from pathlib import Path
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


@pytest.mark.parametrize("width", [1200, 1440, 1920])
def test_targeting_panels_fit_viewport_with_theme(dashboard, width):
    app, window = dashboard
    previous_style = app.styleSheet()
    previous_palette = app.palette()
    try:
        gui.apply_dark_theme(app)
        window.tabs.setCurrentWidget(window.tab_targeting.parentWidget().parentWidget())
        window.show()
        window.resize(1920, 800)
        for _ in range(5):
            app.processEvents()
        window.resize(width, 800)
        for _ in range(5):
            app.processEvents()
        page = window.tab_targeting
        scroll = page.parentWidget().parentWidget()
        assert page.width() <= scroll.viewport().width()
        panels = page.findChildren(gui.QFrame, "panelCard")
        assert len(panels) == 3
        assert page._column_count == (3 if width == 1920 else 2)
        for panel in panels:
            assert panel.geometry().left() >= 0
            assert panel.mapTo(page, panel.rect().bottomRight()).x() < page.width()
            assert panel.width() >= panel.minimumSizeHint().width()
    finally:
        app.setStyleSheet(previous_style)
        app.setPalette(previous_palette)


def fake_resolver():
    values = {"player.hp": 205, "player.hp_max": 205, "player.x": 3312, "player.y": 2051, "player.z": 7}
    return SimpleNamespace(read_entry=Mock(side_effect=values.get), last_error=None,
                           proc=SimpleNamespace(pid=123, detach=Mock()))


def test_settings_applies_and_persists_hotkeys_without_starting_bot(dashboard):
    import json
    _, window = dashboard
    window.global_hotkeys.backend = Mock()
    bindings = {"pause": "Ctrl+Alt+P", "minimize": "Ctrl+Alt+M", "restore": "Ctrl+Alt+B"}
    for action, text in bindings.items():
        window.tab_settings.editors[action].setText(text)
    window.tab_settings.btn_apply.click()
    assert window.ctx.get("settings", "hotkeys") == bindings
    assert json.loads(Path(window.config_path).read_text())["settings"]["hotkeys"] == bindings
    assert window.worker is None
    assert window.engine is None
    assert "salvos" in window.tab_settings.status.text()
    window.tab_settings.editors["restore"].setText("Ctrl+Alt+P")
    window.tab_settings.btn_apply.click()
    assert "diferente" in window.tab_settings.status.text()
    assert window.ctx.get("settings", "hotkeys") == bindings


def test_settings_restore_minimized_window_without_starting_bot(dashboard):
    app, window = dashboard
    window.global_hotkeys.backend = Mock()
    window._apply_global_hotkeys({"minimize": "Ctrl+Alt+M", "restore": "Ctrl+Alt+B"})
    window.show()
    assert window.global_hotkeys.dispatch(0x5041)
    app.processEvents()
    assert window.isMinimized()
    assert window.global_hotkeys.dispatch(0x5042)
    app.processEvents()
    assert window.isVisible() and not window.isMinimized()
    assert window.worker is None and window.engine is None


def test_pause_hotkey_controls_standalone_walker_and_does_nothing_when_idle(dashboard):
    _, window = dashboard
    window._on_pause()
    assert not window.ctx.is_paused()
    original = window.tab_cavebot.walker
    walker = Mock(is_running=True, recording=False)
    window.tab_cavebot.walker = walker
    try:
        window._on_pause()
        assert window.ctx.is_paused()
        walker.set_user_paused.assert_called_with(True)
        assert window.btn_pause.isEnabled()
        window._on_pause()
        assert not window.ctx.is_paused()
        walker.set_user_paused.assert_called_with(False)
        assert window.engine is None and window.worker is None
    finally:
        window.tab_cavebot.walker = original


@pytest.mark.parametrize("as_void_pointer", [False, True])
def test_native_hotkey_message_dispatch_and_capture_do_not_trigger_bot(dashboard, as_void_pointer):
    import ctypes
    from ctypes import wintypes
    from shiboken6 import VoidPtr
    _, window = dashboard
    window.global_hotkeys.backend = Mock()
    callback = Mock()
    window.global_hotkeys.callbacks["pause"] = callback
    window._apply_global_hotkeys({"pause": "Ctrl+Alt+P"})
    message = wintypes.MSG()
    message.message = gui.WM_HOTKEY
    message.wParam = 0x5040
    pointer = VoidPtr(ctypes.addressof(message), 0) if as_void_pointer else ctypes.addressof(message)
    assert window.nativeEvent(b"windows_generic_MSG", pointer) == (True, 0)
    callback.assert_called_once_with()
    editor = window.tab_settings.editors["restore"]
    editor._start_capture()
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(window, "focusWidget", lambda: editor)
        window.nativeEvent(b"windows_generic_MSG", pointer)
    assert editor.value() == "Ctrl+Alt+P"
    callback.assert_called_once_with()


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


def test_route_controls_live_only_in_route_tab_and_recent_log_limit(dashboard):
    _, window = dashboard
    assert window.tab_cavebot.control_group.isAncestorOf(window.tab_cavebot.btn_start_route)
    assert not window.tab_dashboard.isAncestorOf(window.tab_cavebot.btn_start_route)
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



def test_memory_connection_passes_configured_movement_keys_to_walker(dashboard, monkeypatch):
    import inputbus
    _, window = dashboard
    window.ctx.set({"north": "Up", "south": "Down", "east": "Right", "west": "Left"},
                   "cavebot", "walk_keys")
    monkeypatch.setattr(inputbus, "get_bus", lambda: Mock())
    window._memory_connected(fake_resolver())
    assert window.tab_cavebot.walker.movement_keys == {
        "north": 0x26, "south": 0x28, "east": 0x27, "west": 0x25}

def test_final_tabs_and_alert_controls(dashboard):
    _, window = dashboard
    labels = [window.tabs.tabText(index) for index in range(window.tabs.count())]
    assert labels == ["DASHBOARD", "TARGETING", "HEALER", "WALKER", "ALERTAS", "SETTINGS"]
    assert window.tab_healer.isAncestorOf(window.pokemon_healer_selector)
    assert window.tab_targeting.isAncestorOf(window.chk_focus_shiny)
    assert window.chk_focus_shiny.text() == "Só atacar shiny (estrela/boss)"
    assert window.chk_focus_shiny.isChecked()
    assert window.tab_alert.isAncestorOf(window.chk_chat_player)
    assert window.tab_alert.isAncestorOf(window.chk_chat_gm)
    assert window.tab_alert.isAncestorOf(window.txt_chat_log)


def test_redesign_keeps_all_interface_controls(dashboard):
    _, window = dashboard
    expected = (
        (window.tab_dashboard, window,
         ("live_panel", "log_view", "lbl_dash_status", "lbl_route_state",
          "chk_healer_enabled")),
        (window.tab_cavebot, window.tab_cavebot,
         ("table_waypoints", "btn_record", "chk_loop", "combo_profiles",
          "btn_add_wp", "btn_add_current", "spin_x", "spin_y", "spin_z",
          "spin_arrival_tolerance", "btn_start_route", "btn_start_nearest",
          "btn_stop_route")),
        (window.tab_targeting, window,
         ("chk_focus_shiny", "spin_attack_range", "edit_attack_hotkey",
          "spin_attack_cd", "combo_prioritize", "chk_chase", "chk_auto_target",
          "spin_combat_resume", "chk_perfect_lure", "spin_min_lure",
          "spin_lure_radius_sqm", "spin_emergency_hp", "spin_max_lure_wait",
          "spin_group_stable", "edit_combo_hotkey", "edit_combo_command",
          "spin_combo_wait", "edit_revive_hotkey", "spin_revive_delay",
          "spin_combat_loops", "chk_post_revive", "list_whitelist",
          "list_blacklist", "edit_new_target", "btn_add_target",
          "edit_target_filter", "slider_emergency_hp")),
        (window.tab_healer, window,
         ("pokemon_healer_selector",)),
        (window.tab_alert, window,
         ("chk_chat_player", "chk_chat_gm", "chk_chat_ignore_self",
          "chk_chat_include_trade", "spin_chat_poll", "edit_gm_patterns",
          "chk_chat_beep", "chk_chat_webhook", "chk_alarm_death",
          "chk_alarm_disconnect", "chk_alarm_gm", "lbl_chat_status",
          "txt_chat_log")),
    )
    for tab, owner, names in expected:
        for name in names:
            widget = getattr(owner, name)
            assert tab.isAncestorOf(widget), f"{name} saiu da aba {tab.objectName() or tab}"
    assert not hasattr(window, "stat_labels")
    assert window.tab_dashboard.isAncestorOf(window.live_panel)
    assert window.tab_dashboard.isAncestorOf(window.log_view)
    assert window.header.isAncestorOf(window.status_dot)
    assert window.header.isAncestorOf(window.btn_pause)
    assert window.header.isAncestorOf(window.combo_header_profile)
    assert not window.tab_dashboard.isAncestorOf(window.tab_cavebot.btn_start_route)
    assert window.tab_cavebot.control_group.isAncestorOf(window.tab_cavebot.btn_start_route)


def test_target_panel_adds_and_filters_whitelist(dashboard):
    _, window = dashboard
    window.edit_new_target.setText("Pikachu")
    window._add_target_from_field()
    assert window.ctx.get("auto_target", "name_whitelist") == ["Pikachu"]
    assert window.list_whitelist.count() == 1
    window.edit_target_filter.setText("pika")
    assert not window.list_whitelist.item(0).isHidden()
    window.edit_target_filter.setText("zzz")
    assert window.list_whitelist.item(0).isHidden()


def test_emergency_hp_slider_mirrors_spin_and_config(dashboard):
    _, window = dashboard
    window.spin_emergency_hp.setValue(55)
    assert window.slider_emergency_hp.value() == 55
    window.slider_emergency_hp.setValue(25)
    assert window.spin_emergency_hp.value() == 25
    assert window.ctx.get("auto_target", "emergency_hp_percent") == 25


def test_header_profile_combo_mirrors_saved_hunts(dashboard):
    _, window = dashboard
    names = window.tab_cavebot.profile_manager.list_profiles()
    assert window.combo_header_profile.count() == len(names) + 1


def test_revive_delay_field_does_not_clamp_configured_value(dashboard):
    _, window = dashboard
    window.spin_revive_delay.setValue(1100)
    assert window.spin_revive_delay.value() == 1100
    assert window.ctx.get("auto_target", "revive_delay_ms") == 1100


def test_alert_config_save_updates_patterns_and_beeps(dashboard):
    _, window = dashboard
    window.chk_chat_player.setChecked(False)
    window.chk_chat_gm.setChecked(True)
    window.chk_chat_beep.setChecked(False)
    window.chk_alarm_death.setChecked(True)
    window.chk_alarm_disconnect.setChecked(False)
    window.chk_alarm_gm.setChecked(True)
    window.edit_gm_patterns.setText("GM, Gamemaster")
    window._save_alert_config()
    assert window.ctx.get("alarms", "chat_player_enabled") is False
    assert window.ctx.get("alarms", "chat_gm_enabled") is True
    assert window.ctx.get("alarms", "chat_beep") is False
    assert window.ctx.get("alarms", "gm_name_patterns") == ["GM", "Gamemaster"]
    assert window.ctx.get("alarms", "beep_on") == ["death", "gm_detected"]


def test_chat_panel_shows_status_and_recent_messages(dashboard):
    _, window = dashboard
    window.engine = SimpleNamespace(modules={"alarms": SimpleNamespace(
        chat_status={"connected": True, "tick": 123, "events": 2, "dropped": 1},
        recent_chat=[{"time": "12:00:00", "gm": False, "name": "Player", "text": "oi"}],
    )})
    window._refresh_chat_panel()
    assert "tick 123" in window.lbl_chat_status.text()
    assert "JOGADOR" in window.txt_chat_log.toPlainText()
    assert "oi" in window.txt_chat_log.toPlainText()
    window.engine = None

