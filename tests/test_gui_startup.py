import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("win32api")

import gui.main as gui
import memory


@pytest.fixture
def window():
    ctx = SimpleNamespace(get=lambda *keys, default=None: 123 if keys[-1] == "target_pid" else default,
                          save_config=Mock())
    return SimpleNamespace(engine=None, resolver=None, ctx=ctx, config_path="custom-config.json",
                           _log=Mock(), _on_status_update=Mock(), _on_worker_finished=Mock(),
                           btn_start=Mock(), btn_stop=Mock(), btn_pause=Mock(),
                           pokemon_healer_selector=SimpleNamespace(save_selection=Mock()))


def test_start_failure_shows_popup_without_starting_engine(monkeypatch, window):
    connect = Mock(side_effect=RuntimeError("ponteiro nulo"))
    engine = Mock()
    popup = Mock()
    monkeypatch.setattr(memory, "connect_live_resolver", connect)
    monkeypatch.setattr(gui, "create_engine", engine)
    monkeypatch.setattr(gui.QMessageBox, "critical", popup)
    gui.MainWindow._on_start(window)
    connect.assert_called_once_with(123, config_path="custom-config.json")
    engine.assert_not_called()
    assert "ponteiro nulo" in popup.call_args.args[2]
    assert window.resolver is None


def test_start_passes_same_checked_resolver_to_engine_and_worker(monkeypatch, window):
    resolver = SimpleNamespace(proc=SimpleNamespace(detach=Mock()))
    engine = Mock()
    thread = Mock()
    worker = Mock()
    factory = Mock(return_value=engine)
    worker_factory = Mock(return_value=worker)
    monkeypatch.setattr(memory, "connect_live_resolver", Mock(return_value=resolver))
    monkeypatch.setattr(gui, "create_engine", factory)
    monkeypatch.setattr(gui, "QThread", Mock(return_value=thread))
    monkeypatch.setattr(gui, "BotWorker", worker_factory)
    monkeypatch.setattr(gui, "QtLogBridge", lambda signal: logging.NullHandler())
    try:
        gui.MainWindow._on_start(window)
        factory.assert_called_once_with(config_path="custom-config.json", resolver=resolver)
        worker_factory.assert_called_once_with(engine, resolver)
        thread.start.assert_called_once_with()
        window.pokemon_healer_selector.save_selection.assert_called_once_with()
        window.ctx.save_config.assert_called_once_with()
        resolver.proc.detach.assert_not_called()
    finally:
        logging.getLogger("pokebot").removeHandler(window._log_handler)


def test_engine_init_failure_releases_checked_process(monkeypatch, window):
    resolver = SimpleNamespace(proc=SimpleNamespace(detach=Mock()))
    monkeypatch.setattr(memory, "connect_live_resolver", Mock(return_value=resolver))
    monkeypatch.setattr(gui, "create_engine", Mock(side_effect=RuntimeError("engine init failed")))
    popup = Mock()
    monkeypatch.setattr(gui.QMessageBox, "critical", popup)
    gui.MainWindow._on_start(window)
    resolver.proc.detach.assert_called_once_with()
    assert window.engine is None and window.resolver is None
    popup.assert_called_once()
