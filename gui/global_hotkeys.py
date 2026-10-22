from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from core.hotkeys import resolve_key, split_hotkey


ACTIONS = ("pause", "minimize", "restore")
WM_HOTKEY = 0x0312
MOD_NOREPEAT = 0x4000


def parse_binding(text):
    text = str(text or "").strip()
    if not text:
        return None
    parts = [part.upper() for part in split_hotkey(text)]
    modifiers = {"CTRL": 0x0002, "ALT": 0x0001, "SHIFT": 0x0004}
    mask = 0
    for part in parts[:-1]:
        if part not in modifiers or mask & modifiers[part]:
            raise ValueError(f"Atalho inválido: {text}")
        mask |= modifiers[part]
    key = parts[-1]
    if key in modifiers:
        raise ValueError(f"Atalho inválido: {text}")
    code = resolve_key(key)
    return mask, code


class WindowsHotkeyBackend:
    def __init__(self):
        self.api = None
        if sys.platform == "win32":
            self.api = ctypes.WinDLL("user32", use_last_error=True)
            self.api.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
            self.api.RegisterHotKey.restype = wintypes.BOOL
            self.api.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
            self.api.UnregisterHotKey.restype = wintypes.BOOL

    def register(self, hwnd, identifier, modifiers, key):
        return self.api is not None and bool(
            self.api.RegisterHotKey(hwnd, identifier, modifiers | MOD_NOREPEAT, key))

    def unregister(self, hwnd, identifier):
        if self.api is not None:
            self.api.UnregisterHotKey(hwnd, identifier)


class GlobalHotkeys:
    def __init__(self, hwnd, callbacks, backend=None):
        self.hwnd = hwnd
        self.callbacks = callbacks
        self.backend = backend if backend is not None else WindowsHotkeyBackend()
        self.bindings = {}
        self._registered = {}

    def apply(self, bindings):
        requested = {action: str(bindings.get(action, "") or "").strip() for action in ACTIONS}
        parsed = {action: parse_binding(text) for action, text in requested.items()}
        used = [binding for binding in parsed.values() if binding is not None]
        if len(used) != len(set(used)):
            raise ValueError("Cada ação precisa de um atalho diferente.")
        previous = dict(self.bindings)
        self.close()
        try:
            self._register(requested, parsed)
        except ValueError as error:
            self.close()
            try:
                self._register(previous, {action: parse_binding(text) for action, text in previous.items()})
            except ValueError:
                raise ValueError(f"{error} Não foi possível restaurar todos os atalhos anteriores.") from error
            raise

    def _register(self, requested, parsed):
        for identifier, action in enumerate(ACTIONS, start=0x5040):
            binding = parsed.get(action)
            if binding is not None:
                if not self.backend.register(self.hwnd, identifier, *binding):
                    raise ValueError(f"Atalho {requested[action]} indisponível ou reservado pelo Windows. "
                                     "Escolha outra combinação.")
                self._registered[identifier] = action
                self.bindings[action] = requested[action]

    def dispatch(self, identifier):
        action = self._registered.get(identifier)
        if action is None:
            return False
        self.callbacks[action]()
        return True

    def binding_for(self, identifier):
        return self.bindings.get(self._registered.get(identifier), "")

    def close(self):
        for identifier in tuple(self._registered):
            self.backend.unregister(self.hwnd, identifier)
        self._registered.clear()
        self.bindings.clear()
