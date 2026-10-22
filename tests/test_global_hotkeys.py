from unittest.mock import Mock

import pytest

from gui.global_hotkeys import ACTIONS, GlobalHotkeys, WindowsHotkeyBackend, MOD_NOREPEAT, parse_binding


class FakeBackend:
    def __init__(self):
        self.active = {}
        self.blocked = set()

    def register(self, hwnd, identifier, modifiers, key):
        if (modifiers, key) in self.blocked:
            return False
        self.active[identifier] = (modifiers, key)
        return True

    def unregister(self, hwnd, identifier):
        self.active.pop(identifier, None)


@pytest.mark.parametrize("text,expected", [("", None), ("Ctrl+Alt+P", (3, ord("P"))),
    ("shift+f8", (4, 0x77)), ("F24", (0, 0x87)), ("Ctrl+Space", (2, 0x20))])
def test_parse_global_binding(text, expected):
    assert parse_binding(text) == expected


@pytest.mark.parametrize("text", ["Ctrl", "Ctrl+Ctrl+A", "Meta+A", "A+B", "F25", "Ctrl+"])
def test_invalid_binding_is_rejected(text):
    with pytest.raises(ValueError):
        parse_binding(text)


@pytest.mark.parametrize("text,expected", [("=", (0, 0xBB)), ("Ctrl+=", (2, 0xBB)),
    ("-", (0, 0xBD)), ("/", (0, 0xBF)), ("NumPlus", (0, 0x6B)),
    ("Ctrl++", (2, 0xBB)), ("Left", (0, 0x25)), ("VK_0xBF", (0, 0xBF))])
def test_punctuation_global_bindings(text, expected):
    assert parse_binding(text) == expected


@pytest.mark.parametrize("text,codes", [("=", [0xBB]), ("Ctrl+=", [0x11, 0xBB]),
    ("-", [0xBD]), ("Shift+Plus", [0x10, 0xBB]), ("NumPlus", [0x6B]),
    ("VK_0xBF", [0xBF]), ("PageDown", [0x22]), ("F24", [0x87])])
def test_combat_and_healer_share_extended_key_mapping(text, codes):
    from modules.target import parse_hotkey
    from modules.healer import _resolve_hotkey
    assert parse_hotkey(text) == codes
    assert _resolve_hotkey(text) == codes


def test_apply_dispatch_replace_and_close():
    backend = FakeBackend()
    callbacks = {action: Mock() for action in ACTIONS}
    manager = GlobalHotkeys(123, callbacks, backend)
    manager.apply(dict(zip(ACTIONS, ("Ctrl+Alt+P", "Ctrl+Alt+M", "Ctrl+Alt+B"))))
    for identifier, action in enumerate(ACTIONS, start=0x5040):
        assert manager.dispatch(identifier)
        callbacks[action].assert_called_once_with()
    assert not manager.dispatch(42)
    manager.apply({"pause": "F8"})
    assert len(backend.active) == 1
    assert manager.binding_for(0x5040) == "F8"
    manager.close()
    manager.close()
    assert backend.active == {}
    assert not manager.dispatch(0x5040)


def test_duplicates_do_not_remove_active_bindings():
    backend = FakeBackend()
    manager = GlobalHotkeys(123, {}, backend)
    manager.apply({"pause": "F8"})
    with pytest.raises(ValueError, match="diferente"):
        manager.apply({"pause": "Ctrl+Alt+P", "restore": "alt+ctrl+p"})
    assert manager.bindings == {"pause": "F8"}
    assert backend.active == {0x5040: (0, 0x77)}


def test_conflict_rolls_back_previous_bindings_and_cleans_partial_changes():
    backend = FakeBackend()
    manager = GlobalHotkeys(123, {}, backend)
    manager.apply({"pause": "F8"})
    backend.blocked.add(parse_binding("F10"))
    with pytest.raises(ValueError, match="indisponível"):
        manager.apply({"pause": "F9", "restore": "F10"})
    assert manager.bindings == {"pause": "F8"}
    assert backend.active == {0x5040: (0, 0x77)}


def test_native_registration_disables_auto_repeat_without_sending_input():
    backend = WindowsHotkeyBackend.__new__(WindowsHotkeyBackend)
    backend.api = Mock()
    assert backend.register(123, 7, 3, ord("P"))
    backend.api.RegisterHotKey.assert_called_once_with(123, 7, 3 | MOD_NOREPEAT, ord("P"))
