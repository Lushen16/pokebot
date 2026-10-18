"""Diálogo para seleção de processos do Windows com filtro dinâmico."""

from __future__ import annotations

import ctypes
from ctypes import wintypes as wt
from typing import Optional, List, Tuple

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QPushButton, QWidget
)

TH32CS_SNAPPROCESS = 0x00000002


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(wt.ULONG)),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", wt.LONG),
        ("dwFlags", wt.DWORD),
        ("szExeFile", wt.WCHAR * 260)
    ]


def enumerate_windows_processes() -> List[Tuple[str, int]]:
    """Enumera todos os processos do Windows ativos usando Toolhelp32Snapshot."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap in (None, 0, -1, ctypes.c_void_p(-1).value):
        return []

    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    procs: List[Tuple[str, int]] = []

    try:
        if kernel32.Process32FirstW(snap, ctypes.byref(entry)):
            while True:
                name = str(entry.szExeFile)
                pid = int(entry.th32ProcessID)
                procs.append((name, pid))
                if not kernel32.Process32NextW(snap, ctypes.byref(entry)):
                    break
    finally:
        kernel32.CloseHandle(snap)

    return procs


class ProcessSelectorDialog(QDialog):
    """Janela modal para selecionar qualquer processo do Windows em execução."""

    process_selected = Signal(str, int)  # (nome, pid)

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        initial_filter: str = "",
        current_pid: Optional[int] = None,
        current_name: Optional[str] = None,
        process_provider=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Selecionar Processo")
        self.resize(460, 520)
        self.setMinimumSize(380, 420)

        self.process_provider = process_provider or enumerate_windows_processes
        self.selected_name: Optional[str] = None
        self.selected_pid: Optional[int] = None
        self._all_processes: List[Tuple[str, int]] = []

        self._setup_style()
        self._setup_ui(initial_filter)
        self.refresh_processes()

        if current_pid:
            self._select_by_pid(current_pid)
        elif current_name:
            self._select_by_name(current_name)

    def _setup_style(self):
        self.setStyleSheet("""
            QDialog {
                background-color: #21242d;
                color: #ffffff;
            }
            QLabel#heading {
                color: #ffffff;
                font-size: 16px;
                font-weight: bold;
                padding-top: 4px;
                padding-bottom: 8px;
            }
            QLabel#subtitle {
                color: #9ca3af;
                font-size: 12px;
                padding-bottom: 2px;
            }
            QLineEdit#filterInput {
                background-color: #121316;
                border: 1px solid #333640;
                border-radius: 4px;
                color: #ffffff;
                font-size: 13px;
                padding: 6px 8px;
            }
            QLineEdit#filterInput:focus {
                border: 1px solid #be185d;
            }
            QListWidget#processList {
                background-color: #121316;
                border: 1px solid #ffffff;
                border-radius: 2px;
                color: #ffffff;
                font-family: 'Segoe UI', Consolas, monospace;
                font-size: 13px;
                padding: 2px;
            }
            QListWidget#processList::item {
                padding: 4px 6px;
                border-radius: 2px;
            }
            QListWidget#processList::item:hover {
                background-color: #282c37;
            }
            QListWidget#processList::item:selected {
                background-color: #be185d;
                color: #ffffff;
            }
            QPushButton#btnRefresh {
                background-color: #2d333f;
                border: none;
                border-radius: 4px;
                color: #ffffff;
                font-size: 13px;
                padding: 8px 24px;
                min-width: 100px;
            }
            QPushButton#btnRefresh:hover {
                background-color: #3b4352;
            }
            QPushButton#btnSelect {
                background-color: #be185d;
                border: none;
                border-radius: 4px;
                color: #ffffff;
                font-size: 13px;
                font-weight: bold;
                padding: 8px 28px;
                min-width: 120px;
            }
            QPushButton#btnSelect:hover {
                background-color: #db2777;
            }
        """)

    def _setup_ui(self, initial_filter: str):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        # Cabeçalho centralizado
        self.lbl_title = QLabel("Selecionar Processo", self)
        self.lbl_title.setObjectName("heading")
        self.lbl_title.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.lbl_title)

        # Label de filtro
        self.lbl_filter = QLabel("Filtrar por nome:", self)
        self.lbl_filter.setObjectName("subtitle")
        layout.addWidget(self.lbl_filter)

        # Campo de busca
        self.filter_edit = QLineEdit(self)
        self.filter_edit.setObjectName("filterInput")
        self.filter_edit.setText(initial_filter)
        self.filter_edit.textChanged.connect(self._apply_filter)
        layout.addWidget(self.filter_edit)

        # Lista de processos com borda branca destacada
        self.list_widget = QListWidget(self)
        self.list_widget.setObjectName("processList")
        self.list_widget.itemDoubleClicked.connect(self._on_item_double_clicked)
        layout.addWidget(self.list_widget)

        # Barra de botões: Atualizar à esquerda, Selecionar à direita
        btn_bar = QHBoxLayout()
        self.btn_refresh = QPushButton("Atualizar", self)
        self.btn_refresh.setObjectName("btnRefresh")
        self.btn_refresh.clicked.connect(self.refresh_processes)

        self.btn_select = QPushButton("Selecionar", self)
        self.btn_select.setObjectName("btnSelect")
        self.btn_select.clicked.connect(self._on_select)

        btn_bar.addWidget(self.btn_refresh)
        btn_bar.addStretch()
        btn_bar.addWidget(self.btn_select)
        layout.addLayout(btn_bar)

    def refresh_processes(self):
        """Carrega todos os processos do Windows e reaplica o filtro."""
        raw = self.process_provider()
        self._all_processes = list(raw)
        self._apply_filter()

    def _apply_filter(self):
        """Filtra os processos pelo texto digitado e popula a lista."""
        query = self.filter_edit.text().strip().lower()
        selected_before = self.list_widget.currentItem()
        prev_data = selected_before.data(Qt.UserRole) if selected_before else None

        self.list_widget.clear()

        # Prioriza PokeAlliance no topo se não houver filtro digitado
        procs = list(self._all_processes)
        if not query:
            # Preserva a ordem natural de enumeração do Windows (como no print do usuário)
            pass
        else:
            procs = [p for p in procs if query in p[0].lower() or query in str(p[1])]

        target_item = None
        for name, pid in procs:
            label = f"{name} [PID {pid}]"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, (name, pid))
            self.list_widget.addItem(item)

            if prev_data and (name, pid) == prev_data:
                target_item = item
            elif target_item is None and "pokealliance" in name.lower():
                target_item = item

        if target_item:
            self.list_widget.setCurrentItem(target_item)
        elif self.list_widget.count() > 0:
            self.list_widget.setCurrentRow(0)

    def _select_by_pid(self, pid: int):
        for idx in range(self.list_widget.count()):
            item = self.list_widget.item(idx)
            data = item.data(Qt.UserRole)
            if data and data[1] == pid:
                self.list_widget.setCurrentItem(item)
                break

    def _select_by_name(self, name: str):
        target = name.strip().lower()
        for idx in range(self.list_widget.count()):
            item = self.list_widget.item(idx)
            data = item.data(Qt.UserRole)
            if data and data[0].strip().lower() == target:
                self.list_widget.setCurrentItem(item)
                break

    def _on_item_double_clicked(self, item: QListWidgetItem):
        self._on_select()

    def _on_select(self):
        item = self.list_widget.currentItem()
        if not item:
            return
        name, pid = item.data(Qt.UserRole)
        self.selected_name = name
        self.selected_pid = pid
        self.process_selected.emit(name, pid)
        self.accept()
