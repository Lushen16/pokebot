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


def test_native_focus_restores_and_verifies_game_window(backend, monkeypatch):
    module, bus = backend
    gui = SimpleNamespace(GetForegroundWindow=Mock(side_effect=[999, 123]),
                          IsIconic=Mock(return_value=False), ShowWindow=Mock(),
                          BringWindowToTop=Mock(), SetForegroundWindow=Mock())
    monkeypatch.setattr(module, "win32gui", gui)
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    assert module.InputBus._focus_client(bus) is True
    gui.SetForegroundWindow.assert_called_once_with(123)


def test_key_release_does_not_steal_focus_even_when_game_is_not_foreground(backend):
    module, bus = backend
    bus._focus_client.return_value = False
    request = module.InputRequest(type=module.InputType.KEY_UP, code=0x26, module="walker")
    bus._vk_to_scancode = Mock(return_value=0xE048)
    bus._execute(request)
    bus._focus_client.assert_not_called()
    bus._send_scancode.assert_called_once_with(0xE048, False)


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
    events = []
    send.side_effect = lambda batch: events.extend(batch)
    monkeypatch.setattr(wininput, "send_inputs", send)
    monkeypatch.setattr(wininput.time, "sleep", lambda seconds: None)
    wininput.send_virtual_keys((0x10, 0x31))
    assert [(event.ki.wVk, event.ki.dwFlags) for event in events] == [
        (0x10, 0), (0x31, 0), (0x31, wininput.KEYEVENTF_KEYUP), (0x10, wininput.KEYEVENTF_KEYUP)]
    assert send.call_count == 4


class TestBackgroundInput:
    @pytest.fixture
    def posting(self, backend, monkeypatch):
        module, bus = backend
        bus._ctx.get = lambda *keys, default=None: (
            "background" if keys[-1] == "input_mode" else default)
        posted = []
        module.win32gui.PostMessage = Mock(
            side_effect=lambda hwnd, msg, wp, lp: posted.append((msg, wp, lp)) or 1)
        monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
        bus._vk_to_scancode = Mock(return_value=0x20)
        return module, bus, posted

    def test_key_press_posts_messages_without_stealing_focus(self, posting):
        module, bus, posted = posting
        request = module.InputRequest(type=module.InputType.KEY_PRESS, code=0x44,
                                      hold_ms=10, module="walker")
        assert bus._execute(request) is True
        bus._focus_client.assert_not_called()
        assert posted == [(0x0100, 0x44, 0x200001), (0x0101, 0x44, 0xC0200001)]

    def test_combo_posts_down_and_up_in_reverse(self, posting):
        module, bus, posted = posting
        request = module.InputRequest(type=module.InputType.KEY_COMBO, codes=(0x10, 0x46),
                                      hold_ms=50, module="target")
        assert bus._execute(request) is True
        bus._focus_client.assert_not_called()
        assert [(wp, lp >> 31) for _, wp, lp in posted] == [
            (0x10, 0), (0x46, 0), (0x46, 1), (0x10, 1)]

    def test_text_posts_without_foreground_check(self, posting, monkeypatch):
        module, bus, posted = posting
        module.text_key_codes.return_value = [(0x41,)]
        request = queued_text(module, bus, monkeypatch, "a")
        assert bus._execute(request) is True
        bus._focus_client.assert_not_called()
        assert [(msg, wp) for msg, wp, _ in posted] == [
            (0x0100, 0x0D), (0x0101, 0x0D), (0x0102, ord("a")),
            (0x0100, 0x0D), (0x0101, 0x0D)]

    def test_background_autocombo_posts_literal_exclamation_without_digit_key(self, posting, monkeypatch):
        module, bus, posted = posting
        text = "!autocombo"
        module.text_key_codes.return_value = [(0x10, 0x31)] + [(ord(character.upper()),) for character in text[1:]]
        request = queued_text(module, bus, monkeypatch, text)
        assert bus._execute(request) is True
        assert "".join(chr(wp) for msg, wp, _ in posted if msg == 0x0102) == text
        assert not any(wp in (0x10, 0x31) for msg, wp, _ in posted if msg in (0x0100, 0x0101))
        module.text_key_codes.assert_not_called()

    def test_background_text_failure_does_not_submit_partial_command(self, posting, monkeypatch):
        module, bus, posted = posting
        original = module.win32gui.PostMessage.side_effect
        def post(hwnd, msg, wp, lp):
            if msg == 0x0102:
                raise OSError("janela indisponível")
            return original(hwnd, msg, wp, lp)
        module.win32gui.PostMessage.side_effect = post
        request = queued_text(module, bus, monkeypatch, "!autocombo")
        assert bus._execute(request) is False
        assert [(msg, wp) for msg, wp, _ in posted] == [
            (0x0100, 0x0D), (0x0101, 0x0D), (0x0100, 0x1B), (0x0101, 0x1B)]

    def test_background_text_accepts_pywin32_none_success_and_utf16(self, posting, monkeypatch):
        module, bus, posted = posting
        module.win32gui.PostMessage.side_effect = lambda hwnd, msg, wp, lp: posted.append((msg, wp, lp))
        request = queued_text(module, bus, monkeypatch, "!é😀")
        assert bus._execute(request) is True
        units = [wp for msg, wp, _ in posted if msg == 0x0102]
        assert units == [0x21, 0xE9, 0xD83D, 0xDE00]

    def test_background_cancel_closes_chat_without_submitting(self, posting, monkeypatch):
        module, bus, posted = posting
        request = queued_text(module, bus, monkeypatch, "!autocombo")
        request.cancel_event.wait.side_effect = [False, True]
        assert bus._execute(request) is False
        assert [wp for msg, wp, _ in posted if msg == 0x0102] == [ord("!")]
        assert [(msg, wp) for msg, wp, _ in posted][-2:] == [(0x0100, 0x1B), (0x0101, 0x1B)]



def test_walker_retries_key_discarded_before_focus_is_available(backend):
    from modules.walker import Walker, VK_D
    module, bus = backend
    walker = Walker(input_bus=bus, loop=False)
    walker.read_player_position = Mock(return_value=(100, 200, 7))
    walker.add_waypoint(105, 200, 7)
    bus._focus_client.return_value = False
    walker.tick()
    request = bus._queue.get_nowait()[2]
    assert bus._execute(request) is False
    bus._focus_client.return_value = True
    walker.tick()
    assert not bus._queue.empty()
    retry = bus._queue.get_nowait()[2]
    bus._execute(retry)
    bus._send_scancode.assert_called_once_with(bus._vk_to_scancode(VK_D), True)


def test_pending_movement_does_not_duplicate_queue_requests(backend):
    from modules.walker import Walker
    _, bus = backend
    walker = Walker(input_bus=bus, loop=False)
    walker.read_player_position = Mock(return_value=(100, 200, 7))
    walker.add_waypoint(105, 200, 7)
    for _ in range(10):
        walker.tick()
    assert bus._queue.qsize() == 1


def test_inputbus_context_stop_resets_running_and_discards_stale_commands(backend):
    _, bus = backend
    bus._running = True
    bus._ctx.is_stopped.return_value = True
    bus.key_down(0x44)
    bus.key_down(0x57)
    bus._run()
    assert bus._running is False
    assert bus._queue.empty()
    assert bus.movement_key_active(0x44) is False
    assert bus.movement_key_active(0x57) is False
    bus._send_scancode.assert_not_called()



def test_delivered_key_is_not_requeued_and_release_clears_status(backend):
    from modules.walker import Walker, VK_D
    _, bus = backend
    walker = Walker(input_bus=bus, loop=False)
    walker.read_player_position = Mock(return_value=(100, 200, 7))
    walker.add_waypoint(105, 200, 7)
    walker.tick()
    bus._execute(bus._queue.get_nowait()[2])
    assert bus.movement_key_active(VK_D) is True
    walker.tick()
    assert bus._queue.empty()
    walker.request_stop()
    bus._execute(bus._queue.get_nowait()[2])
    assert bus.movement_key_active(VK_D) is False


def test_failed_sendinput_is_retried_without_claiming_key_is_held(backend):
    from modules.walker import Walker, VK_D
    _, bus = backend
    walker = Walker(input_bus=bus, loop=False)
    walker.read_player_position = Mock(return_value=(100, 200, 7))
    walker.add_waypoint(105, 200, 7)
    bus._send_scancode.side_effect = OSError("SendInput failed")
    walker.tick()
    assert bus._execute(bus._queue.get_nowait()[2]) is False
    assert bus.movement_key_active(VK_D) is False
    bus._send_scancode.side_effect = None
    walker.tick()
    assert bus._queue.qsize() == 1
