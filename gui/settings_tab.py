from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QGroupBox, QFormLayout, QPushButton

from gui.global_hotkeys import ACTIONS
from gui.hotkey_edit import HotkeyEdit
from gui.icons import set_button_icon


class SettingsTab(QWidget):
    apply_requested = Signal(dict)

    def __init__(self, bindings, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        title = QLabel("Settings", self)
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        description = QLabel("Atalhos globais para controlar o PokeBot com o jogo em foco.", self)
        description.setObjectName("subtitle")
        description.setWordWrap(True)
        layout.addWidget(description)
        group = QGroupBox("Atalhos de teclado", self)
        form = QFormLayout(group)
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self.editors = {}
        labels = {"pause": "Pausar / retomar o bot", "minimize": "Minimizar a janela",
                  "restore": "Abrir / restaurar a janela"}
        for action in ACTIONS:
            editor = HotkeyEdit(bindings.get(action, ""), group)
            editor.setAccessibleName(labels[action])
            editor.setPlaceholderText("Desativado — clique para definir")
            self.editors[action] = editor
            form.addRow(labels[action], editor)
        layout.addWidget(group)
        help_text = QLabel("Clique no campo e pressione a combinação. Esc cancela; Delete limpa. "
                           "Sugestões: Ctrl+Alt+P, Ctrl+Alt+M e Ctrl+Alt+B. "
                           "Campos vazios desativam o atalho. Evite teclas usadas no jogo. "
                           "Minimizar não pausa o bot; restaurar não inicia a automação.", self)
        help_text.setObjectName("hint")
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        self.btn_apply = QPushButton("Salvar e aplicar atalhos", self)
        self.btn_apply.setObjectName("primary")
        set_button_icon(self.btn_apply, "save")
        self.btn_apply.clicked.connect(lambda: self.apply_requested.emit(
            {action: editor.value() for action, editor in self.editors.items()}))
        layout.addWidget(self.btn_apply)
        self.status = QLabel("Nenhum atalho é ativado até ser salvo.", self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        layout.addStretch()

    def show_status(self, text, error=False):
        self.status.setText(text)
        self.status.setStyleSheet("color: #fb7185;" if error else "color: #34d399;")
