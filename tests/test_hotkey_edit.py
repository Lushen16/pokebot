import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QFocusEvent, QKeyEvent
from PySide6.QtWidgets import QApplication

from gui.hotkey_edit import HotkeyEdit, format_hotkey, key_label

app = QApplication.instance() or QApplication([])


def press(widget, key, modifiers=Qt.NoModifier):
    widget.keyPressEvent(QKeyEvent(QEvent.KeyPress, key, modifiers))


class TestFormatting:
    def test_function_keys(self):
        assert key_label(Qt.Key_F1) == "F1"
        assert key_label(Qt.Key_F12) == "F12"

    def test_letters_and_digits(self):
        assert key_label(Qt.Key_A) == "A"
        assert key_label(Qt.Key_7) == "7"

    def test_special_keys_use_parse_hotkey_vocabulary(self):
        assert key_label(Qt.Key_Space) == "Space"
        assert key_label(Qt.Key_Return) == "Enter"
        assert key_label(Qt.Key_Escape) == "Esc"

    def test_combination_order_is_modifiers_first(self):
        mods = Qt.ControlModifier | Qt.AltModifier | Qt.ShiftModifier
        assert format_hotkey(mods, Qt.Key_F) == "Ctrl+Alt+Shift+F"
        assert format_hotkey(Qt.ShiftModifier, Qt.Key_F) == "Shift+F"

    def test_navigation_is_supported_but_unknown_key_returns_empty(self):
        assert format_hotkey(Qt.NoModifier, Qt.Key_Left) == "Left"
        assert key_label(Qt.Key_unknown) == ""


class TestHotkeyEdit:
    @pytest.mark.parametrize("key,label", [(Qt.Key_Equal, "="), (Qt.Key_Minus, "-"),
        (Qt.Key_Plus, "Plus"), (Qt.Key_Slash, "/"), (Qt.Key_BracketLeft, "["),
        (Qt.Key_Comma, ","), (Qt.Key_Left, "Left"), (Qt.Key_Home, "Home")])
    def test_capture_punctuation_and_navigation(self, key, label):
        widget = HotkeyEdit("")
        widget._start_capture()
        press(widget, key)
        assert widget.value() == label
        assert not widget.property("capturing")

    def test_native_layout_key_is_preserved_when_qt_label_differs(self):
        widget = HotkeyEdit("")
        widget._start_capture()
        event = QKeyEvent(QEvent.KeyPress, Qt.Key_Semicolon, Qt.NoModifier,
                          0, 0xBF, 0, ";")
        widget.keyPressEvent(event)
        assert widget.value() == "VK_0xBF"

    def test_numpad_plus_is_distinct_from_main_keyboard(self):
        widget = HotkeyEdit("")
        widget._start_capture()
        press(widget, Qt.Key_Plus, Qt.KeypadModifier)
        assert widget.value() == "NumPlus"

    def test_equal_reaches_all_hotkey_consumers(self):
        from gui.global_hotkeys import parse_binding
        from modules.target import parse_hotkey
        from modules.healer import _resolve_hotkey
        widget = HotkeyEdit("")
        widget._start_capture()
        event = QKeyEvent(QEvent.KeyPress, Qt.Key_Equal, Qt.ControlModifier,
                          0, 0xBB, 0, "=")
        widget.keyPressEvent(event)
        assert widget.value() == "Ctrl+="
        assert parse_binding(widget.value()) == (2, 0xBB)
        assert parse_hotkey(widget.value()) == [0x11, 0xBB]
        assert _resolve_hotkey(widget.value()) == [0x11, 0xBB]

    def test_click_starts_capture_and_emits_property(self):
        widget = HotkeyEdit("F5")
        assert widget.value() == "F5"
        assert not widget.property("capturing")
        widget._start_capture()
        assert widget.property("capturing") is True
        assert widget.isReadOnly()

    def test_key_capture_commits_and_emits_editing_finished(self):
        widget = HotkeyEdit("F5")
        emitted = []
        widget.editingFinished.connect(lambda: emitted.append(widget.value()))
        widget._start_capture()
        press(widget, Qt.Key_F6)
        assert widget.value() == "F6"
        assert widget.property("capturing") is False
        assert emitted == ["F6"]

    def test_modifier_only_presses_do_not_commit(self):
        widget = HotkeyEdit("F5")
        widget._start_capture()
        press(widget, Qt.Key_Shift, Qt.ShiftModifier)
        press(widget, Qt.Key_Control, Qt.ControlModifier)
        assert widget.value() == "F5"
        assert widget.property("capturing") is True

    def test_combination_capture(self):
        widget = HotkeyEdit("")
        widget._start_capture()
        press(widget, Qt.Key_F, Qt.ShiftModifier)
        assert widget.value() == "Shift+F"

    def test_escape_restores_previous_value(self):
        widget = HotkeyEdit("Shift+F")
        widget._start_capture()
        press(widget, Qt.Key_Escape)
        assert widget.value() == "Shift+F"
        assert widget.property("capturing") is False

    def test_delete_clears_hotkey(self):
        widget = HotkeyEdit("F8")
        widget._start_capture()
        press(widget, Qt.Key_Delete)
        assert widget.value() == ""

    def test_leaving_without_pressing_restores_value(self):
        widget = HotkeyEdit("Ctrl+F")
        widget._start_capture()
        widget.focusOutEvent(QFocusEvent(QEvent.FocusOut))
        assert widget.value() == "Ctrl+F"
        assert widget.property("capturing") is False

    def test_configured_value_is_kept_on_construction(self):
        widget = HotkeyEdit("Shift+F")
        assert widget.text() == "Shift+F"
        assert widget.placeholderText() == "Pressione a tecla"
