from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLineEdit
from core.hotkeys import resolve_key

_MODIFIER_KEYS = {
    Qt.Key_Shift, Qt.Key_Control, Qt.Key_Alt, Qt.Key_Meta, Qt.Key_AltGr,
}

_SPECIAL_KEYS = {
    Qt.Key_Space: "Space",
    Qt.Key_Tab: "Tab",
    Qt.Key_Return: "Enter",
    Qt.Key_Enter: "Enter",
    Qt.Key_Escape: "Esc",
    Qt.Key_Backspace: "Backspace",
    Qt.Key_Delete: "Delete",
    Qt.Key_Equal: "=", Qt.Key_Minus: "-", Qt.Key_Plus: "Plus",
    Qt.Key_Semicolon: ";", Qt.Key_Comma: ",", Qt.Key_Period: ".",
    Qt.Key_Slash: "/", Qt.Key_QuoteLeft: "`", Qt.Key_BracketLeft: "[",
    Qt.Key_Backslash: chr(92), Qt.Key_BracketRight: "]", Qt.Key_Apostrophe: "'",
    Qt.Key_Left: "Left", Qt.Key_Right: "Right", Qt.Key_Up: "Up", Qt.Key_Down: "Down",
    Qt.Key_Home: "Home", Qt.Key_End: "End", Qt.Key_Insert: "Insert",
    Qt.Key_PageUp: "PageUp", Qt.Key_PageDown: "PageDown", Qt.Key_Pause: "Pause",
    Qt.Key_Print: "PrintScreen", Qt.Key_CapsLock: "CapsLock",
    Qt.Key_NumLock: "NumLock", Qt.Key_ScrollLock: "ScrollLock",
}


def key_label(key: int) -> str:
    """Nome legivel de uma tecla, no vocabulario aceito por parse_hotkey."""
    if Qt.Key_A <= key <= Qt.Key_Z:
        return chr(key)
    if Qt.Key_0 <= key <= Qt.Key_9:
        return chr(key)
    if Qt.Key_F1 <= key <= Qt.Key_F24:
        return f"F{key - Qt.Key_F1 + 1}"
    return _SPECIAL_KEYS.get(key, "")


def format_hotkey(modifiers, key: int, native_virtual_key: int = 0) -> str:
    """Monta 'Ctrl+Shift+F' a partir dos modificadores e da tecla pressionada."""
    base = key_label(key)
    if modifiers & Qt.KeypadModifier:
        if Qt.Key_0 <= key <= Qt.Key_9:
            base = f"Num{chr(key)}"
        else:
            base = {Qt.Key_Plus: "NumPlus", Qt.Key_Minus: "NumMinus",
                    Qt.Key_Asterisk: "NumMultiply", Qt.Key_Slash: "NumDivide",
                    Qt.Key_Period: "NumDecimal", Qt.Key_Comma: "NumDecimal",
                    Qt.Key_Enter: "NumEnter"}.get(key, base)
    if 1 <= native_virtual_key <= 0xFE:
        try:
            expected = resolve_key(base)
        except ValueError:
            expected = None
        if expected != native_virtual_key:
            base = f"VK_0x{native_virtual_key:02X}"
    if not base:
        return ""
    parts = []
    if modifiers & Qt.ControlModifier:
        parts.append("Ctrl")
    if modifiers & Qt.AltModifier:
        parts.append("Alt")
    if modifiers & Qt.ShiftModifier:
        parts.append("Shift")
    parts.append(base)
    return "+".join(parts)


class HotkeyEdit(QLineEdit):
    """QLineEdit somente leitura que captura a combinacao de teclas ao clicar.

    Emite editingFinished a cada captura para manter os binds atuais
    (editingFinished/textChanged) sem alterar o codigo das abas.
    """

    captured = Signal(str)

    def __init__(self, value: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("hotkeyInput")
        self.setReadOnly(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setPlaceholderText("Pressione a tecla")
        self.setToolTip("Clique e pressione a combinacao desejada "
                        "(Esc cancela, Delete limpa).")
        self.setAccessibleName("Atalho de teclado")
        self._capturing = False
        self._before = str(value or "")
        self.setText(self._before)
        self._sync_property()

    def value(self) -> str:
        return self.text().strip()

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self._start_capture()

    def focusOutEvent(self, event):
        if self._capturing:
            self._stop_capture(restore=True)
        super().focusOutEvent(event)

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        if not self._capturing:
            self._start_capture()

    def _start_capture(self):
        self._capturing = True
        self._before = self.text()
        self._sync_property()

    def _stop_capture(self, restore: bool = False):
        self._capturing = False
        if restore:
            self.setText(self._before)
        self._sync_property()

    def _commit(self, text: str):
        self._capturing = False
        self.setText(text)
        self._sync_property()
        self.captured.emit(text)
        self.editingFinished.emit()

    def _sync_property(self):
        if bool(self.property("capturing")) != self._capturing:
            self.setProperty("capturing", self._capturing)
            self.style().unpolish(self)
            self.style().polish(self)

    def keyPressEvent(self, event):
        if not self._capturing:
            super().keyPressEvent(event)
            return
        key = event.key()
        if key in _MODIFIER_KEYS:
            event.accept()
            return
        if key == Qt.Key_Escape and event.modifiers() == Qt.NoModifier:
            self._stop_capture(restore=True)
            event.accept()
            return
        if key in (Qt.Key_Backspace, Qt.Key_Delete) and event.modifiers() == Qt.NoModifier:
            self._commit("")
            event.accept()
            return
        text = format_hotkey(event.modifiers(), key, event.nativeVirtualKey())
        if not text:
            event.accept()
            return
        self._commit(text)
        event.accept()
