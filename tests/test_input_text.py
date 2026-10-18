import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

import wininput


@pytest.fixture
def backend(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "inputbus.py"
    spec = importlib.util.spec_from_file_location("_inputbus_text_test", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    context = SimpleNamespace(get=lambda *keys, default=None: default,
                              is_stopped=Mock(return_value=False))
    monkeypatch.setattr(module, "get_context", lambda: context)
    monkeypatch.setattr(module, "win32gui", SimpleNamespace(GetForegroundWindow=Mock(return_value=123)))
    monkeypatch.setattr(module, "win32process", SimpleNamespace(
        GetWindowThreadProcessId=Mock(return_value=(42, 99))))
    monkeypatch.setattr(module, "send_virtual_keys", Mock())
    monkeypatch.setattr(module, "text_key_codes", Mock(return_value=[(0x10, 0x31), (0x41,)]))
    bus = module.InputBus()
    monkeypatch.setattr(bus, "_focus_client", Mock(return_value=True))
    monkeypatch.setattr(bus, "_get_hwnd", lambda: 123)
    monkeypatch.setattr(bus, "_send_scancode", Mock())
    return module, bus


def queued_text(module, bus, monkeypatch, text="!a"):
    assert bus.send_text(text, module="walker")
    request = bus._queue.get_nowait()[2]
    monkeypatch.setattr(request.cancel_event, "wait", Mock(return_value=False))
    return request


def test_text_is_one_atomic_queue_request_and_native_calls_run_only_on_worker(backend, monkeypatch):
    module, bus = backend
    request = queued_text(module, bus, monkeypatch)
    assert request.type == module.InputType.TEXT
    module.send_virtual_keys.assert_not_called()
    assert bus._execute(request) is True
    assert module.send_virtual_keys.call_args_list == [
        call((0x0D,)), call((0x10, 0x31)), call((0x41,)), call((0x0D,))]
    module.text_key_codes.assert_called_once_with("!a", 42)


def test_canceling_chat_closes_it_without_sending_message(backend, monkeypatch):
    module, bus = backend
    request = queued_text(module, bus, monkeypatch)
    request.cancel_event.wait.side_effect = [False, True]
    assert bus._execute(request) is False
    assert module.send_virtual_keys.call_args_list == [call((0x0D,)), call((0x10, 0x31)), call((0x1B,))]


def test_canceled_queued_chat_never_opens_or_refocuses_window(backend, monkeypatch):
    module, bus = backend
    request = queued_text(module, bus, monkeypatch)
    bus.cancel_text("walker")
    assert bus._execute(request) is False
    module.send_virtual_keys.assert_not_called()
    bus._focus_client.assert_not_called()


def test_focus_loss_interrupts_typing_without_sending_to_another_window(backend, monkeypatch):
    module, bus = backend
    request = queued_text(module, bus, monkeypatch)
    module.win32gui.GetForegroundWindow.return_value = 456
    assert bus._execute(request) is False
    module.send_virtual_keys.assert_not_called()


def test_unmappable_character_is_rejected_before_opening_chat(backend, monkeypatch):
    module, bus = backend
    request = queued_text(module, bus, monkeypatch)
    module.text_key_codes.side_effect = ValueError("unmapped")
    assert bus._execute(request) is False
    module.send_virtual_keys.assert_not_called()


def test_queue_full_returns_false_but_never_drops_key_release(backend):
    module, bus = backend
    bus._ctx.get = lambda *keys, default=None: 1 if keys[-1] == "input_bus_queue_max" else default
    assert bus.press_key(0x41)
    assert bus.send_text("hello") is False
    assert bus.key_up(0x41)
    assert bus._queue.qsize() == 2


@pytest.mark.parametrize("text", ["", "a\nb", "a\rb", "\t", "a" * 1025, None])
def test_invalid_chat_is_rejected_before_enqueueing(backend, text):
    _, bus = backend
    with pytest.raises(ValueError):
        bus.send_text(text)
    assert bus._queue.empty()


def test_stop_releases_held_keys_even_after_global_context_stops(backend):
    module, bus = backend
    bus._held_scancodes.add(30)
    bus._ctx.is_stopped.return_value = True
    bus._running = True
    assert bus.press_key(0x42)
    bus._run()
    bus._send_scancode.assert_called_once_with(30, False)
    assert not bus._held_scancodes


@pytest.mark.parametrize("caps_lock,expected", [(False, [(0x10, 0x31), (0x41,), (0x10, 0x41)]),
                                               (True, [(0x10, 0x31), (0x10, 0x41), (0x41,)])])
def test_native_character_mapping_includes_shift_and_capslock(monkeypatch, caps_lock, expected):
    user32 = Mock()
    user32.GetKeyboardLayout.return_value = 123
    user32.GetKeyState.return_value = int(caps_lock)
    user32.VkKeyScanExW.side_effect = [0x0131, 0x0041, 0x0141]
    monkeypatch.setattr(wininput, "_user32", lambda: user32)
    assert wininput.text_key_codes("!aA", thread_id=42) == expected


def test_unmapped_native_character_fails_before_injection(monkeypatch):
    user32 = Mock()
    user32.GetKeyState.return_value = 0
    user32.VkKeyScanExW.return_value = -1
    monkeypatch.setattr(wininput, "_user32", lambda: user32)
    with pytest.raises(ValueError):
        wininput.text_key_codes("\U0001f600", thread_id=42)


def test_native_default_layout_uses_vk_key_scan_w(monkeypatch):
    user32 = Mock()
    user32.GetKeyState.return_value = 0
    user32.VkKeyScanW.return_value = 0x41
    monkeypatch.setattr(wininput, "_user32", lambda: user32)
    assert wininput.text_key_codes("a") == [(0x41,)]
    user32.VkKeyScanW.assert_called_once_with("a")
    user32.VkKeyScanExW.assert_not_called()


def test_native_vk_events_release_modifiers_in_reverse_order(monkeypatch):
    send = Mock()
    monkeypatch.setattr(wininput, "send_inputs", send)
    wininput.send_virtual_keys((0x10, 0x31))
    events = send.call_args.args[0]
    assert [(event.ki.wVk, event.ki.dwFlags) for event in events] == [
        (0x10, 0), (0x31, 0), (0x31, wininput.KEYEVENTF_KEYUP), (0x10, wininput.KEYEVENTF_KEYUP)]
