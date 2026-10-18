import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

import wininput
from modules.base import Module


@pytest.mark.parametrize("button,down,flag", [
    ("left", True, 0x0002), ("left", False, 0x0004),
    ("right", True, 0x0008), ("right", False, 0x0010),
])
def test_native_mouse_flags_without_sending_input(monkeypatch, button, down, flag):
    send = Mock()
    monkeypatch.setattr(wininput, "send_inputs", send)
    wininput.send_mouse_button(down, button=button)
    assert send.call_args.args[0][0].mi.dwFlags == flag


def test_native_default_remains_left_and_invalid_button_is_rejected(monkeypatch):
    send = Mock()
    monkeypatch.setattr(wininput, "send_inputs", send)
    wininput.send_mouse_button(True)
    assert send.call_args.args[0][0].mi.dwFlags == wininput.MOUSEEVENTF_LEFTDOWN
    send.reset_mock()
    with pytest.raises(ValueError):
        wininput.send_mouse_button(True, button="middle")
    send.assert_not_called()


@pytest.fixture
def backend(monkeypatch):
    for name in ("win32api", "win32con", "win32gui"):
        monkeypatch.setitem(sys.modules, name, Mock())
    path = Path(__file__).resolve().parents[1] / "inputbus.py"
    spec = importlib.util.spec_from_file_location("_inputbus_mouse_test", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    ctx = SimpleNamespace(get=lambda *keys, default=None: default)
    monkeypatch.setattr(module, "get_context", lambda: ctx)
    monkeypatch.setattr(module.time, "sleep", Mock())
    monkeypatch.setattr(module, "send_mouse_button", Mock())
    bus = module.InputBus()
    monkeypatch.setattr(bus, "_focus_client", Mock())
    monkeypatch.setattr(bus, "_get_hwnd", lambda: 123)
    module.win32gui.GetClientRect.return_value = (0, 0, 800, 600)
    module.win32gui.ClientToScreen.return_value = (110, 220)
    return module, bus


def test_right_click_survives_queue_and_execution(backend):
    module, bus = backend
    assert bus.click(10, 20, priority=1, module="healer", button="right")
    priority, _, request = bus._queue.get_nowait()
    assert priority == 1
    assert request.module == "healer"
    bus._execute(request)
    module.win32gui.ClientToScreen.assert_called_once_with(123, (10, 20))
    module.win32api.SetCursorPos.assert_called_once_with((110, 220))
    assert module.send_mouse_button.call_args_list == [
        call(True, button="right"), call(False, button="right")]


def test_legacy_click_defaults_to_left(backend):
    _, bus = backend
    assert bus.click(10, 20)
    assert bus._queue.get_nowait()[2].button == "left"
    with pytest.raises(ValueError):
        bus.click(10, 20, button="middle")
    assert bus._queue.empty()


@pytest.mark.parametrize("x,y", [(-1, 20), (800, 20), (10, -1), (10, 600)])
def test_out_of_client_click_does_not_move_or_send(backend, x, y):
    module, bus = backend
    bus._execute(module.InputRequest(module.InputType.MOUSE_CLICK, x=x, y=y, button="right"))
    module.win32api.SetCursorPos.assert_not_called()
    module.send_mouse_button.assert_not_called()


def test_module_right_click_delegates_to_existing_bus():
    bus = SimpleNamespace(click=Mock(return_value=True))
    owner = SimpleNamespace(bus=bus, name="healer")
    assert Module.right_click(owner, 10, 20, priority=1)
    bus.click.assert_called_once_with(10, 20, 1, module="healer", button="right")


def test_failed_focus_never_sends_key_or_mouse(backend, monkeypatch):
    module, bus = backend
    monkeypatch.setattr(bus, "_focus_client", Mock(return_value=False))
    monkeypatch.setattr(bus, "_send_scancode", Mock())
    for kind in (module.InputType.KEY_PRESS, module.InputType.MOUSE_CLICK):
        assert bus._execute(module.InputRequest(kind, code=0x73)) is False
    bus._send_scancode.assert_not_called()
    module.send_mouse_button.assert_not_called()
    module.win32api.SetCursorPos.assert_not_called()


def test_window_selection_uses_pid_not_matching_title(backend, monkeypatch):
    module, bus = backend
    bus._ctx = SimpleNamespace(get=lambda *keys, default=None: 99 if keys[-1] == "target_pid" else default)
    monkeypatch.setattr(module, "win32process", SimpleNamespace(
        GetWindowThreadProcessId=lambda hwnd: (1, {111: 88, 222: 99}[hwnd])))
    module.win32gui.IsWindow.return_value = True
    module.win32gui.IsWindowVisible.return_value = True
    module.win32gui.GetWindowText.return_value = "PokeAlliance"
    module.win32gui.EnumWindows.side_effect = lambda callback, data: [callback(hwnd, data) for hwnd in (111, 222)]
    assert module.InputBus._get_hwnd(bus) == 222
    bus._ctx = SimpleNamespace(get=lambda *keys, default=None: 88 if keys[-1] == "target_pid" else default)
    assert module.InputBus._get_hwnd(bus) == 111


def test_already_focused_client_does_not_refocus(backend):
    module, bus = backend
    module.win32gui.GetForegroundWindow.return_value = 123
    assert module.InputBus._focus_client(bus) is True
    module.win32gui.SetForegroundWindow.assert_not_called()


def test_manual_focus_mode_never_steals_focus(backend):
    module, bus = backend
    bus._ctx = SimpleNamespace(get=lambda *keys, default=None: False if keys[-1] == "focus_before_input" else default)
    module.win32gui.GetForegroundWindow.return_value = 456
    assert module.InputBus._focus_client(bus) is False
    module.win32gui.SetForegroundWindow.assert_not_called()


@pytest.mark.parametrize("foreground,expected", [(123, True), (456, False)])
def test_automatic_focus_requires_foreground_confirmation(backend, foreground, expected):
    module, bus = backend
    module.win32gui.GetForegroundWindow.side_effect = [456, foreground]
    assert module.InputBus._focus_client(bus) is expected
    module.win32gui.SetForegroundWindow.assert_called_once_with(123)
