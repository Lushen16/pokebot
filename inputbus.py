# pokebot/inputbus.py
"""Serialized input bus with safety limits and per-module queue."""

from __future__ import annotations
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

import win32api
import win32con
import win32gui
import win32process

from context import get_context
from wininput import send_scancode, vk_to_scancode, send_mouse_button, sendinput_selftest
from wininput import text_key_codes, send_virtual_keys

log = logging.getLogger("pokebot.inputbus")


class InputType(Enum):
    KEY_DOWN = "key_down"
    KEY_UP = "key_up"
    KEY_PRESS = "key_press"  # down + up with delay
    KEY_COMBO = "key_combo"  # modifier(s) + key with hold time
    MOUSE_CLICK = "mouse_click"
    MOUSE_MOVE = "mouse_move"
    TEXT = "text"


@dataclass
class InputRequest:
    type: InputType
    # key press: vk or scancode
    code: int = 0
    # key combo: multiple vks or scancodes in sequence
    codes: tuple[int, ...] = ()
    # mouse: x, y (client-relative)
    x: int = 0
    y: int = 0
    # key_press hold time
    hold_ms: int = 50
    # priority: lower = higher priority
    priority: int = 10
    # callback on completion
    callback: Optional[Callable[[], None]] = None
    # module name for debugging
    module: str = "unknown"
    button: str = "left"
    text: str = ""
    cancel_event: Optional[threading.Event] = None


class InputBus:
    """
    Single-threaded input serializer. All modules submit requests;
    a worker thread executes them with enforced minimum interval.
    """

    def __init__(self):
        self._ctx = get_context()
        self._queue: queue.PriorityQueue[tuple[int, int, InputRequest]] = queue.PriorityQueue()
        self._worker: threading.Thread | None = None
        self._running = False
        self._seq = 0
        self._lock = threading.Lock()
        self._last_input_time = 0.0
        self._hwnd = None
        self._hwnd_lock = threading.Lock()
        self._focus_blocked = False
        self._text_cancellations: dict[str, threading.Event] = {}
        self._held_scancodes: set[int] = set()

    def start(self):
        if self._running:
            return
        self._running = True
        self._worker = threading.Thread(target=self._run, name="InputBus", daemon=True)
        self._worker.start()
        log.info("InputBus started")

    def stop(self):
        self._running = False
        with self._lock:
            for event in self._text_cancellations.values():
                event.set()
        if self._worker:
            self._worker.join(timeout=2.0)
            self._worker = None
        log.info("InputBus stopped")

    def submit(self, req: InputRequest) -> bool:
        """Enqueue an input request. Returns False if queue full."""
        max_q = self._ctx.get("safety_limits", "input_bus_queue_max", default=32)
        if req.type != InputType.KEY_UP and self._queue.qsize() >= max_q:
            log.warning("InputBus queue full (%d), dropping request from %s", max_q, req.module)
            return False
        with self._lock:
            self._seq += 1
            seq = self._seq
        self._queue.put((req.priority, seq, req))
        return True

    def press_key(self, code: int, hold_ms: int = 50, priority: int = 10, module: str = "unknown") -> bool:
        return self.submit(InputRequest(
            type=InputType.KEY_PRESS,
            code=code,
            hold_ms=hold_ms,
            priority=priority,
            module=module,
        ))

    def press_combo(self, codes: list[int] | tuple[int, ...], hold_ms: int = 50, priority: int = 10, module: str = "unknown") -> bool:
        if not codes:
            return True
        if len(codes) == 1:
            return self.press_key(codes[0], hold_ms=hold_ms, priority=priority, module=module)
        return self.submit(InputRequest(
            type=InputType.KEY_COMBO,
            codes=tuple(codes),
            hold_ms=hold_ms,
            priority=priority,
            module=module,
        ))

    def key_down(self, code: int, priority: int = 10, module: str = "unknown") -> bool:
        return self.submit(InputRequest(
            type=InputType.KEY_DOWN,
            code=code,
            priority=priority,
            module=module,
        ))

    def send_text(self, text: str, priority: int = 10, module: str = "unknown") -> bool:
        if not isinstance(text, str) or not text or len(text) > 1024 or not text.isprintable():
            raise ValueError("Texto deve ter de 1 a 1024 caracteres imprimíveis")
        with self._lock:
            event = self._text_cancellations.get(module)
            if event is None or event.is_set():
                event = threading.Event()
                self._text_cancellations[module] = event
        return self.submit(InputRequest(type=InputType.TEXT, text=text, priority=priority,
                                        module=module, cancel_event=event))

    def cancel_text(self, module: str) -> None:
        with self._lock:
            event = self._text_cancellations.get(module)
            if event is not None:
                event.set()

    def _execute_text(self, req: InputRequest) -> bool:
        event = req.cancel_event or threading.Event()
        hwnd = self._get_hwnd()
        thread_id, _ = win32process.GetWindowThreadProcessId(hwnd)
        strokes = text_key_codes(req.text, thread_id)
        if event.is_set() or self._ctx.is_stopped() or win32gui.GetForegroundWindow() != hwnd:
            return False
        send_virtual_keys((0x0D,))
        chat_open = True
        try:
            for keys in strokes:
                if event.wait(0.02) or self._ctx.is_stopped():
                    return False
                if win32gui.GetForegroundWindow() != hwnd:
                    return False
                send_virtual_keys(keys)
            if event.wait(0.02) or self._ctx.is_stopped() or win32gui.GetForegroundWindow() != hwnd:
                return False
            send_virtual_keys((0x0D,))
            chat_open = False
            return True
        finally:
            if chat_open and win32gui.GetForegroundWindow() == hwnd:
                send_virtual_keys((0x1B,))

    def key_up(self, code: int, priority: int = 10, module: str = "unknown") -> bool:
        return self.submit(InputRequest(
            type=InputType.KEY_UP,
            code=code,
            priority=priority,
            module=module,
        ))

    def click(self, x: int, y: int, priority: int = 10, module: str = "unknown",
              *, button: str = "left") -> bool:
        if button not in ("left", "right"):
            raise ValueError("Mouse button must be left or right")
        return self.submit(InputRequest(
            type=InputType.MOUSE_CLICK,
            x=x,
            y=y,
            button=button,
            priority=priority,
            module=module,
        ))

    def _matches_client(self, hwnd):
        if not hwnd or not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd):
            return False
        _, owner_pid = win32process.GetWindowThreadProcessId(hwnd)
        if owner_pid == os.getpid():
            return False
        target_pid = self._ctx.get("client", "target_pid", default=None)
        if target_pid is not None and owner_pid != int(target_pid):
            return False

        # Ignorar janelas filhas
        try:
            parent = win32gui.GetParent(hwnd)
            if isinstance(parent, int) and parent != 0:
                return False
        except Exception:
            pass

        # Ignorar tooltips e tool windows
        try:
            ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            if isinstance(ex_style, int) and (ex_style & win32con.WS_EX_TOOLWINDOW):
                return False
        except Exception:
            pass

        # Filtrar janelas técnicas com dimensoes invalidas (ex: IME, helper)
        try:
            rect = win32gui.GetWindowRect(hwnd)
            if isinstance(rect, (tuple, list)) and len(rect) >= 4:
                w = rect[2] - rect[0]
                h = rect[3] - rect[1]
                if w < 160 or h < 120:
                    return False
        except Exception:
            pass

        if target_pid is None:
            pattern = self._ctx.get("client", "window_title_pattern", default="PokeAlliance")
            return pattern.lower() in win32gui.GetWindowText(hwnd).lower()
        return True

    def _get_hwnd(self):
        with self._hwnd_lock:
            if self._matches_client(self._hwnd):
                return self._hwnd
            candidates = []
            def collect(hwnd, _):
                if self._matches_client(hwnd):
                    try:
                        rect = win32gui.GetWindowRect(hwnd)
                        if isinstance(rect, (tuple, list)) and len(rect) >= 4:
                            area = (rect[2] - rect[0]) * (rect[3] - rect[1])
                        else:
                            area = 1
                    except Exception:
                        area = 1
                    candidates.append((area, hwnd))
                return True
            win32gui.EnumWindows(collect, None)
            if candidates:
                # Escolhe a janela de maior area visivel (viewport principal do jogo)
                candidates.sort(key=lambda x: x[0], reverse=True)
                self._hwnd = candidates[0][1]
            else:
                self._hwnd = None
            return self._hwnd

    def _focus_client(self):
        hwnd = self._get_hwnd()
        if not hwnd:
            return False
        fg_hwnd = win32gui.GetForegroundWindow()
        if fg_hwnd == hwnd:
            return True
        if not self._ctx.get("client", "focus_before_input", default=True):
            return False
        try:
            # Se estiver minimizada na barra de tarefas (Iconic), restaura.
            # Se ja estiver visivel/maximizada, usa SW_SHOW para nao desfazer
            # a tela cheia ou maximizacao do DirectX (que causava minimizacao).
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            else:
                win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
            win32gui.BringWindowToTop(hwnd)
            win32gui.SetForegroundWindow(hwnd)
            time.sleep(self._ctx.get("client", "refocus_delay_ms", default=120) / 1000.0)
        except Exception as exc:
            log.debug("focus failed: %s", exc)
            return False
        return win32gui.GetForegroundWindow() == hwnd

    def _send_scancode(self, scancode: int, down: bool):
        """SendInput with KEYEVENTF_SCANCODE."""
        send_scancode(scancode, down)

    def _vk_to_scancode(self, vk: int) -> int:
        return vk_to_scancode(vk)

    def _execute(self, req: InputRequest):
        min_interval = self._ctx.get("safety_limits", "min_interval_between_inputs_ms", default=25) / 1000.0
        now = time.monotonic()
        wait = self._last_input_time + min_interval - now
        if wait > 0:
            time.sleep(wait)

        try:
            if req.cancel_event is not None and req.cancel_event.is_set():
                return False
            if req.type != InputType.KEY_UP and not self._focus_client():
                if not self._focus_blocked:
                    log.warning("Input bloqueado: janela do PID selecionado nao esta em foco. Clique no jogo para continuar.")
                self._focus_blocked = True
                return False
            self._focus_blocked = False
            if req.type == InputType.KEY_PRESS:
                sc = req.code if req.code > 255 else self._vk_to_scancode(req.code)
                self._send_scancode(sc, True)
                try:
                    time.sleep(max(req.hold_ms, 80.0) / 1000.0)
                finally:
                    self._send_scancode(sc, False)
            elif req.type == InputType.KEY_COMBO:
                scancodes = [c if c > 255 else self._vk_to_scancode(c) for c in req.codes]
                for sc in scancodes:
                    self._send_scancode(sc, True)
                    time.sleep(0.035)
                try:
                    time.sleep(max(req.hold_ms, 90.0) / 1000.0)
                finally:
                    for sc in reversed(scancodes):
                        self._send_scancode(sc, False)
                        time.sleep(0.025)
            elif req.type == InputType.KEY_DOWN:
                sc = req.code if req.code > 255 else self._vk_to_scancode(req.code)
                self._send_scancode(sc, True)
                self._held_scancodes.add(sc)
            elif req.type == InputType.KEY_UP:
                sc = req.code if req.code > 255 else self._vk_to_scancode(req.code)
                self._send_scancode(sc, False)
                self._held_scancodes.discard(sc)
            elif req.type == InputType.TEXT:
                return self._execute_text(req)
            elif req.type == InputType.MOUSE_CLICK:
                hwnd = self._get_hwnd()
                if hwnd:
                    # Convert client-relative to screen
                    rect = win32gui.GetClientRect(hwnd)
                    if not rect[0] <= req.x < rect[2] or not rect[1] <= req.y < rect[3]:
                        raise ValueError("Click outside client area")
                    client_x, client_y = win32gui.ClientToScreen(hwnd, (req.x, req.y))
                    win32api.SetCursorPos((client_x, client_y))
                    time.sleep(0.01)
                    send_mouse_button(True, button=req.button)
                    try:
                        time.sleep(0.02)
                    finally:
                        send_mouse_button(False, button=req.button)
            else:
                log.warning("unknown input type: %s", req.type)
        except Exception as exc:
            log.error("input execution failed: %s", exc)
            return False
        finally:
            self._last_input_time = time.monotonic()
            if req.callback:
                try:
                    req.callback()
                except Exception:
                    pass

    def _run(self):
        try:
            while self._running:
                try:
                    _, _, req = self._queue.get(timeout=0.1)
                    if self._ctx.is_stopped():
                        break
                    self._execute(req)
                except queue.Empty:
                    continue
                except Exception as exc:
                    log.exception("InputBus worker error: %s", exc)
        finally:
            for scancode in tuple(self._held_scancodes):
                try:
                    self._send_scancode(scancode, False)
                except Exception:
                    log.exception("Falha ao soltar tecla no encerramento")
                self._held_scancodes.discard(scancode)


# Singleton
_BUS: InputBus | None = None


def get_bus() -> InputBus:
    global _BUS
    if _BUS is None:
        _BUS = InputBus()
    return _BUS


def shutdown_bus():
    global _BUS
    if _BUS:
        _BUS.stop()
        _BUS = None
