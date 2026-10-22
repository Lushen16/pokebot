import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest


def _load_bus(monkeypatch, settings, name="_inputbus_focus_test"):
    for module_name in ("win32api", "win32con", "win32gui"):
        monkeypatch.setitem(sys.modules, module_name, Mock())
    path = Path(__file__).resolve().parents[1] / "inputbus.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)

    def _get(*keys, default=None):
        return settings.get(keys[-1], default)

    ctx = SimpleNamespace(get=_get, is_stopped=Mock(return_value=False))
    monkeypatch.setattr(module, "get_context", lambda: ctx)
    monkeypatch.setattr(module.time, "sleep", Mock())

    gui = Mock()
    monkeypatch.setattr(module, "win32gui", gui)

    bus = module.InputBus()
    monkeypatch.setattr(bus, "_get_hwnd", lambda: 123)
    monkeypatch.setattr(bus, "_vk_to_scancode", lambda vk: vk)
    return module, bus, gui


def test_manual_policy_never_steals_even_with_legacy_flag_true(monkeypatch):
    module, bus, gui = _load_bus(
        monkeypatch, {"focus_policy": "manual", "focus_before_input": True}, "fp_manual")
    gui.GetForegroundWindow.return_value = 456
    assert bus.focus_client() is False
    assert module.InputBus._focus_client(bus) is False
    gui.SetForegroundWindow.assert_not_called()


def test_always_policy_steals_focus(monkeypatch):
    module, bus, gui = _load_bus(monkeypatch, {"focus_policy": "always"}, "fp_always")
    gui.GetForegroundWindow.side_effect = [456, 123]
    assert module.InputBus._focus_client(bus) is True
    gui.SetForegroundWindow.assert_called_once_with(123)


def test_force_focus_bypasses_manual_policy(monkeypatch):
    module, bus, gui = _load_bus(monkeypatch, {"focus_policy": "manual"}, "fp_force")
    gui.GetForegroundWindow.side_effect = [456, 123]
    assert bus.focus_client(force=True) is True
    gui.SetForegroundWindow.assert_called_once_with(123)


def test_missing_policy_falls_back_to_legacy_flag(monkeypatch):
    module, bus, gui = _load_bus(monkeypatch, {"focus_before_input": True}, "fp_legacy")
    gui.GetForegroundWindow.side_effect = [456, 123]
    assert module.InputBus._focus_client(bus) is True
    gui.SetForegroundWindow.assert_called_once_with(123)


def test_execute_drops_input_without_stealing_focus(monkeypatch):
    module, bus, gui = _load_bus(monkeypatch, {"focus_policy": "manual"}, "fp_drop")
    monkeypatch.setattr(bus, "_send_scancode", Mock())
    gui.GetForegroundWindow.return_value = 456
    request = module.InputRequest(type=module.InputType.KEY_PRESS, code=0x41)
    assert bus._execute(request) is False
    bus._send_scancode.assert_not_called()
    gui.SetForegroundWindow.assert_not_called()


def test_execute_sends_when_already_focused(monkeypatch):
    module, bus, gui = _load_bus(monkeypatch, {"focus_policy": "manual"}, "fp_send")
    monkeypatch.setattr(bus, "_send_scancode", Mock())
    gui.GetForegroundWindow.return_value = 123
    request = module.InputRequest(type=module.InputType.KEY_PRESS, code=0x41)
    bus._execute(request)
    assert bus._send_scancode.call_args_list == [call(0x41, True), call(0x41, False)]
    gui.SetForegroundWindow.assert_not_called()
