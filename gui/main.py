# pokebot/gui/main.py
"""Main PySide6 GUI application."""

from __future__ import annotations
import sys
import logging
import copy
import html
import threading
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal, QObject, QThread, QSignalBlocker, QSize
from PySide6.QtGui import QFont, QColor, QPalette, QIcon, QAction
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QTabWidget, QLabel, QPushButton, QGroupBox, QFormLayout,
    QLineEdit, QSpinBox, QDoubleSpinBox, QCheckBox, QComboBox,
    QTextEdit, QListWidget, QListWidgetItem, QTableWidget,
    QTableWidgetItem, QHeaderView, QSplitter, QScrollArea,
    QFileDialog, QMessageBox, QProgressBar, QSlider,
    QTreeWidget, QTreeWidgetItem, QMenu, QSystemTrayIcon, QFrame, QLayout, QGraphicsDropShadowEffect,
)

from context import get_context, BotContext
from engine import create_engine
from navigator import Navigator, Waypoint
from gui.pokemon_selector import PokemonHealerSelector
from gui.hotkey_edit import HotkeyEdit
from gui.global_hotkeys import GlobalHotkeys, WM_HOTKEY
from gui.settings_tab import SettingsTab
from gui.icons import icon, set_button_icon

log = logging.getLogger("pokebot.gui")


# ─── Theme ────────────────────────────────────────────────────────────────
DARK_PALETTE = {
    "bg": "#070d16",
    "bg_secondary": "#0c1927",
    "bg_tertiary": "#0a1420",
    "fg": "#d9e8f5",
    "fg_muted": "#7c95ad",
    "accent": "#22d3ee",
    "accent_hover": "#67e8f9",
    "green": "#34d399",
    "red": "#fb7185",
    "yellow": "#fbbf24",
    "border": "#1b3145",
    "input_bg": "#09131e",
    "input_fg": "#ecf7ff",
}

def apply_dark_theme(app: QApplication):
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(DARK_PALETTE["bg"]))
    palette.setColor(QPalette.WindowText, QColor(DARK_PALETTE["fg"]))
    palette.setColor(QPalette.Base, QColor(DARK_PALETTE["input_bg"]))
    palette.setColor(QPalette.AlternateBase, QColor(DARK_PALETTE["bg_secondary"]))
    palette.setColor(QPalette.ToolTipBase, QColor(DARK_PALETTE["bg_tertiary"]))
    palette.setColor(QPalette.ToolTipText, QColor(DARK_PALETTE["fg"]))
    palette.setColor(QPalette.Text, QColor(DARK_PALETTE["input_fg"]))
    palette.setColor(QPalette.Button, QColor(DARK_PALETTE["bg_secondary"]))
    palette.setColor(QPalette.ButtonText, QColor(DARK_PALETTE["fg"]))
    palette.setColor(QPalette.BrightText, QColor(DARK_PALETTE["red"]))
    palette.setColor(QPalette.Highlight, QColor(DARK_PALETTE["accent"]))
    palette.setColor(QPalette.HighlightedText, QColor(DARK_PALETTE["bg"]))
    app.setPalette(palette)

    assets = Path(__file__).with_name("assets")
    stylesheet = Path(__file__).with_name("style.qss").read_text(encoding="utf-8")
    for token, filename in (("@CHECK_ICON@", "check.svg"),
                            ("@TOGGLE_OFF@", "toggle_off.svg"),
                            ("@TOGGLE_ON@", "toggle_on.svg"),
                            ("@POKEBALL@", "pokeball.svg")):
        stylesheet = stylesheet.replace(token, (assets / filename).as_posix())
    app.setStyleSheet(stylesheet)


# ─── Worker Thread ────────────────────────────────────────────────────────

class ResponsiveColumns(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._panels = []
        self._column_count = 0
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(2, 2, 2, 2)
        self.grid.setSpacing(16)
        self.grid.setSizeConstraint(QLayout.SetNoConstraint)

    def set_panels(self, panels):
        self._panels = list(panels)
        self._reflow()

    def minimumSizeHint(self):
        margins = self.grid.contentsMargins()
        width = max((panel.minimumSizeHint().width() for panel in self._panels), default=0)
        return QSize(width + margins.left() + margins.right(), self.grid.minimumSize().height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reflow()

    def _reflow(self):
        if not self._panels:
            return
        margins = self.grid.contentsMargins()
        available = self.width() - margins.left() - margins.right()
        spacing = self.grid.horizontalSpacing()
        columns = 1
        for candidate in range(len(self._panels), 1, -1):
            widths = [max(panel.minimumSizeHint().width()
                          for panel in self._panels[column::candidate])
                      for column in range(candidate)]
            if sum(widths) + spacing * (candidate - 1) <= available:
                columns = candidate
                break
        if columns == self._column_count:
            return
        self._column_count = columns
        for panel in self._panels:
            self.grid.removeWidget(panel)
        for column in range(len(self._panels)):
            self.grid.setColumnStretch(column, 1 if column < columns else 0)
        for index, panel in enumerate(self._panels):
            self.grid.addWidget(panel, index // columns, index % columns, Qt.AlignTop)
        self.updateGeometry()


class QtLogBridge(logging.Handler):
    """Forwards Python logging records to a Qt Signal."""
    def __init__(self, signal):
        super().__init__()
        self.signal = signal

    def emit(self, record):
        try:
            msg = self.format(record)
            self.signal.emit(record.levelname, msg)
        except Exception:
            pass


class BotWorker(QObject):
    """Runs the engine in a background thread."""
    status_update = Signal(dict)
    log_message = Signal(str, str)  # level, message
    finished = Signal()

    def __init__(self, engine, resolver):
        super().__init__()
        self.engine = engine
        self.resolver = resolver
        self._running = False
        self._stop_event = threading.Event()

    def run(self):
        try:
            if self._stop_event.is_set():
                return
            self._running = True
            self.engine.start()
            self.log_message.emit("INFO", "Bot started")

            # Periodic status updates and keep thread alive
            while not self._stop_event.is_set() and not self.engine.ctx.is_stopped():
                self._emit_status()
                self._stop_event.wait(0.5)
        except Exception as exc:
            log.exception("BotWorker run error: %s", exc)
            self.log_message.emit("ERROR", f"BotWorker error: {exc}")
        finally:
            if self.engine:
                try:
                    self.engine.stop()
                except Exception:
                    pass
            self.log_message.emit("INFO", "Bot stopped")
            self.finished.emit()

    def stop(self):
        self._stop_event.set()
        self._running = False
        if self.engine:
            self.engine.ctx.stop()
            self.engine.walker.request_stop()

    def _emit_status(self):
        if self.engine:
            self.status_update.emit(self.engine.get_status())


# ─── Main Window ──────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    ui_log = Signal(str, str)

    def __init__(self, config_path: str = None):
        super().__init__()
        self.ui_log.connect(self._log)
        self.config_path = config_path
        self.ctx = get_context(config_path)
        self.engine = None
        self.worker = None
        self.worker_thread = None
        self.resolver = None
        self.wp_table = None
        self.memory_thread = None
        self._memory_valid = False
        self._position_updating = False
        self._pending_target = None
        self._closing = False
        self._allow_close = False
        self._input_shutdown = None

        self.setWindowTitle("PokeBot — Central de Automação")
        self.setWindowIcon(icon("power"))
        self.setMinimumSize(1200, 800)
        self._setup_ui()
        self._setup_tray()
        self._load_config_to_ui()

        # Status timer
        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self._update_status_bar)
        self.status_timer.start(100)

        self._last_chat_key = None
        self.chat_timer = QTimer(self)
        self.chat_timer.timeout.connect(self._refresh_chat_panel)
        self.chat_timer.start(1000)
        self.global_hotkeys = GlobalHotkeys(int(self.winId()), {
            "pause": self._on_pause, "minimize": self.showMinimized,
            "restore": self._restore_window})
        self.tab_settings.apply_requested.connect(self._apply_global_hotkeys)
        try:
            self.global_hotkeys.apply(self.ctx.get("settings", "hotkeys", default={}))
            if self.global_hotkeys.bindings:
                self.tab_settings.show_status("Atalhos salvos carregados e ativos.")
        except ValueError as error:
            self.tab_settings.show_status(str(error), error=True)

    def _setup_ui(self):
        # Central widget with tabs
        shell = QWidget(self)
        shell_layout = QVBoxLayout(shell)
        shell_layout.setContentsMargins(20, 12, 20, 12)
        shell_layout.setSpacing(14)
        self.header = self._create_header(shell)
        shell_layout.addWidget(self.header)
        self.tabs = QTabWidget(shell)
        shell_layout.addWidget(self.tabs, 1)
        self.setCentralWidget(shell)

        # Create tabs
        self.tab_dashboard = self._create_dashboard_tab()
        self.tab_cavebot = self._create_cavebot_tab()
        self.tab_targeting = self._create_targeting_tab()
        self.tab_healer = self._create_healer_tab()
        self.tab_alert = self._create_alert_tab()
        self.tab_settings = SettingsTab(self.ctx.get("settings", "hotkeys", default={}), self)

        self.page_dashboard = self._scroll_page(self.tab_dashboard)
        self.page_cavebot = self._scroll_page(self.tab_cavebot)
        self.page_targeting = self._scroll_page(self.tab_targeting)
        self.page_healer = self._scroll_page(self.tab_healer)
        self.page_alert = self._scroll_page(self.tab_alert)
        self.page_settings = self._scroll_page(self.tab_settings)
        self._decorate_groups()
        self.tabs.setDocumentMode(True)
        self.tabs.setIconSize(QSize(21, 21))
        self.tabs.tabBar().setExpanding(True)
        self.tabs.tabBar().setUsesScrollButtons(False)
        self.tabs.tabBar().setElideMode(Qt.ElideNone)
        self.tabs.addTab(self.page_dashboard, icon("dashboard"), "DASHBOARD")
        self.tabs.addTab(self.page_targeting, icon("target"), "TARGETING")
        self.tabs.addTab(self.page_healer, icon("heart"), "HEALER")
        self.tabs.addTab(self.page_cavebot, icon("walk"), "WALKER")
        self.tabs.addTab(self.page_alert, icon("bolt"), "ALERTAS")
        self.tabs.addTab(self.page_settings, icon("settings"), "SETTINGS")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.tabs.setCurrentIndex(1)

        self.lbl_connection = self.lbl_header_connection
        self.btn_start = self.btn_toggle_bot
        self.btn_stop = QPushButton(self)
        self.btn_stop.hide()
        self._refresh_header_processes()
        self._populate_header_profiles()
        self.tab_cavebot.combo_profiles.currentTextChanged.connect(self._sync_header_profile)
        self._on_tab_changed(self.tabs.currentIndex())

    def _scroll_page(self, widget):
        if isinstance(widget, QScrollArea):
            inner = widget.widget()
            if inner is not None and inner.layout() is not None:
                inner.layout().setSizeConstraint(QLayout.SetMinimumSize)
            widget.setParent(self)
            return widget
        layout = widget.layout()
        if layout is not None and not isinstance(widget, ResponsiveColumns):
            layout.setSizeConstraint(QLayout.SetMinimumSize)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(widget)
        return scroll

    # ── Helpers de UI (ícones + acessibilidade) ─────────────────────────
    def _glyph(self, parent, name, size=18, color=None):
        label = QLabel(parent)
        label.setPixmap(icon(name, color).pixmap(size, size))
        label.setFixedSize(size, size)
        label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        return label

    def _icon_label(self, name, text, *, object_name="fieldLabel", size=18):
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(self._glyph(holder, name, size))
        label = QLabel(text, holder)
        label.setObjectName(object_name)
        row.addWidget(label)
        row.addStretch()
        return holder

    def _field_row(self, form, name, text, widget, hint=None):
        holder = self._icon_label(name, text)
        if hint:
            holder.setToolTip(hint)
            widget.setToolTip(hint)
        form.addRow(holder, widget)
        return widget

    def _check_row(self, form, name, text, checkbox, hint=None):
        checkbox.setText(text)
        checkbox.setIcon(icon(name))
        checkbox.setIconSize(QSize(18, 18))
        if hint:
            checkbox.setToolTip(hint)
        form.addRow("", checkbox)
        return checkbox

    def _section(self, title, name, hint=None, parent=None):
        box = QGroupBox(parent)
        box.setObjectName("sectionCard")
        box.setProperty("accent", True)
        outer = QVBoxLayout(box)
        outer.setContentsMargins(14, 12, 14, 13)
        outer.setSpacing(7)
        header = QWidget(box)
        header.setObjectName("groupHeader")
        head_row = QHBoxLayout(header)
        head_row.setContentsMargins(0, 0, 0, 2)
        head_row.setSpacing(10)
        head_row.addWidget(self._glyph(header, name, 22))
        title_label = QLabel(title, header)
        title_label.setObjectName("sectionTitle")
        head_row.addWidget(title_label)
        head_row.addStretch()
        outer.addWidget(header)
        if hint:
            subtitle = QLabel(hint, box)
            subtitle.setObjectName("hint")
            subtitle.setWordWrap(True)
            outer.addWidget(subtitle)
        form = QFormLayout()
        form.setContentsMargins(0, 3, 0, 0)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(7)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        outer.addLayout(form)
        box.form = form
        return box

    def _section_grid(self, title, name, hint=None, parent=None):
        box = self._section(title, name, hint, parent)
        grid = QGridLayout()
        grid.setContentsMargins(0, 3, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(7)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        box.layout().addLayout(grid)
        box.grid = grid
        return box

    def _grid_field(self, grid, row, column, name, text, widget, hint=None, span=1):
        holder = QWidget()
        layout = QVBoxLayout(holder)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        label = self._icon_label(name, text, size=15)
        if hint:
            holder.setToolTip(hint)
            widget.setToolTip(hint)
        layout.addWidget(label)
        layout.addWidget(widget)
        grid.addWidget(holder, row, column, 1, span)
        return widget

    def _grid_check(self, grid, row, column, name, text, checkbox, hint=None, span=2):
        checkbox.setText(text)
        checkbox.setIcon(icon(name))
        checkbox.setIconSize(QSize(16, 16))
        if hint:
            checkbox.setToolTip(hint)
        grid.addWidget(checkbox, row, column, 1, span)
        return checkbox

    def _panel(self, title, name, hint=None, parent=None):
        panel = QFrame(parent)
        panel.setObjectName("panelCard")
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(16, 15, 16, 16)
        outer.setSpacing(11)
        header = QHBoxLayout()
        header.setSpacing(11)
        badge = QLabel(panel)
        badge.setObjectName("panelIcon")
        badge.setFixedSize(40, 40)
        badge.setAlignment(Qt.AlignCenter)
        badge.setPixmap(icon(name, DARK_PALETTE["accent"]).pixmap(23, 23))
        header.addWidget(badge)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        title_label = QLabel(title, panel)
        title_label.setObjectName("panelTitle")
        title_label.setWordWrap(True)
        titles.addWidget(title_label)
        if hint:
            subtitle = QLabel(hint, panel)
            subtitle.setObjectName("panelSubtitle")
            subtitle.setWordWrap(True)
            titles.addWidget(subtitle)
        header.addLayout(titles, 1)
        outer.addLayout(header)
        body = QVBoxLayout()
        body.setSpacing(10)
        outer.addLayout(body)
        panel.body = body
        return panel

    def _inner_card(self, title, name, parent=None):
        card = QFrame(parent)
        card.setObjectName("innerCard")
        body = QVBoxLayout(card)
        body.setContentsMargins(15, 14, 15, 15)
        body.setSpacing(10)
        head = QHBoxLayout()
        head.setSpacing(8)
        head.addWidget(self._glyph(card, name, 18, DARK_PALETTE["accent"]))
        label = QLabel(title, card)
        label.setObjectName("innerTitle")
        head.addWidget(label)
        head.addStretch()
        body.addLayout(head)
        card.body = body
        return card

    def _form(self):
        form = QFormLayout()
        form.setContentsMargins(0, 2, 0, 0)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(9)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        form.setRowWrapPolicy(QFormLayout.DontWrapRows)
        return form

    GROUP_ICONS = (
        ("automa", "dashboard"), ("session", "dashboard"), ("statistic", "dashboard"),
        ("indicador", "dashboard"), ("personagem", "players"), ("jogador", "players"),
        ("rota", "map"), ("waypoint", "map"), ("perfil", "map"), ("gerenciar", "map"),
        ("posi", "range"),
        ("atividade", "say"), ("gravacao", "record"), ("gravação", "record"),
        ("target", "target"), ("alvo", "target"), ("combate", "combo"), ("combo", "combo"),
        ("name filter", "filter"), ("filtro", "filter"), ("fila", "loop"),
        ("cura", "heart"), ("food", "food"), ("captura", "catch"), ("catch", "catch"),
        ("loot", "loot"), ("general", "sliders"), ("hotkey", "bolt"),
        ("safety", "safe"), ("runtime", "clock"), ("scanner", "debug"), ("output", "eye"),
    )

    def _icon_for_title(self, title):
        low = title.casefold()
        for needle, name in self.GROUP_ICONS:
            if needle in low:
                return name
        return None

    def _decorate_groups(self):
        for box in self.findChildren(QGroupBox):
            title = box.title()
            if not title or box.findChild(QWidget, "groupHeader") is not None:
                continue
            name = self._icon_for_title(title)
            if not name:
                continue
            layout = box.layout()
            if layout is None or isinstance(layout, QHBoxLayout):
                continue
            header = QWidget(box)
            header.setObjectName("groupHeader")
            row = QHBoxLayout(header)
            row.setContentsMargins(0, 0, 0, 4)
            row.setSpacing(10)
            row.addWidget(self._glyph(header, name, 20))
            label = QLabel(title, header)
            label.setObjectName("sectionTitle")
            row.addWidget(label)
            row.addStretch()
            if isinstance(layout, QFormLayout):
                layout.insertRow(0, header)
            else:
                layout.insertWidget(0, header)
            box.setTitle("")

    def _create_header(self, parent):
        header = QFrame(parent)
        header.setObjectName("header")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(20, 14, 18, 14)
        layout.setSpacing(13)

        mark = QFrame(header)
        mark.setObjectName("brandMark")
        mark.setFixedSize(62, 62)
        mark_layout = QVBoxLayout(mark)
        mark_layout.setContentsMargins(7, 7, 7, 7)
        emblem = QLabel(mark)
        emblem.setAlignment(Qt.AlignCenter)
        emblem.setPixmap(icon("pokeball", "#eafcff").pixmap(46, 46))
        mark_layout.addWidget(emblem)
        logo_glow = QGraphicsDropShadowEffect(mark)
        logo_glow.setBlurRadius(28)
        logo_glow.setOffset(0, 0)
        logo_glow.setColor(QColor(34, 211, 238, 120))
        mark.setGraphicsEffect(logo_glow)
        layout.addWidget(mark)

        brand = QVBoxLayout()
        brand.setSpacing(2)
        title_row = QHBoxLayout()
        title_row.setSpacing(9)
        title = QLabel("PokeBot", header)
        title.setObjectName("brand")
        version = QLabel("v2.0", header)
        version.setObjectName("versionBadge")
        title_row.addWidget(title)
        title_row.addWidget(version)
        title_row.addStretch()
        brand.addLayout(title_row)

        status_row = QHBoxLayout()
        status_row.setSpacing(7)
        self.status_dot = QLabel(header)
        self.status_dot.setObjectName("statusDot")
        self.status_dot.setFixedSize(8, 8)
        self.status_dot.setStyleSheet(
            f"background: {DARK_PALETTE['fg_muted']}; border-radius: 4px;")
        self.lbl_header_connection = QLabel("OFFLINE", header)
        self.lbl_header_connection.setObjectName("headerStatus")
        meta_dot = QLabel("•", header)
        meta_dot.setObjectName("headerMetaDot")
        self.lbl_header_process = QLabel("Nenhum cliente conectado", header)
        self.lbl_header_process.setObjectName("headerProcess")
        status_row.addWidget(self.status_dot)
        status_row.addWidget(self.lbl_header_connection)
        status_row.addWidget(meta_dot)
        status_row.addWidget(self.lbl_header_process)
        status_row.addStretch()
        brand.addLayout(status_row)
        kicker = QLabel("AUTOMAÇÃO PARA POKEALLIANCE", header)
        kicker.setObjectName("brandKicker")
        brand.addWidget(kicker)
        layout.addLayout(brand, 1)

        self.combo_processes = QComboBox(header)
        self.combo_processes.setObjectName("headerCombo")
        self.combo_processes.setMinimumWidth(195)
        self.combo_processes.setAccessibleName("Processo do jogo")
        self.combo_processes.setToolTip("Selecionar conecta a leitura; não inicia a automação.")
        self.combo_processes.activated.connect(self._select_header_process)
        layout.addWidget(self.combo_processes)

        self.btn_select_process = QPushButton(header)
        self.btn_select_process.setObjectName("iconButton")
        self.btn_select_process.setFixedSize(44, 44)
        self.btn_select_process.setAccessibleName("Atualizar processos")
        self.btn_select_process.setToolTip("Atualizar a lista de processos do cliente.")
        set_button_icon(self.btn_select_process, "refresh", 18)
        self.btn_select_process.clicked.connect(self._refresh_header_processes)
        layout.addWidget(self.btn_select_process)

        self.combo_header_profile = QComboBox(header)
        self.combo_header_profile.setObjectName("headerCombo")
        self.combo_header_profile.setMinimumWidth(165)
        self.combo_header_profile.setAccessibleName("Perfil de hunt")
        self.combo_header_profile.setToolTip("Carregar um perfil de hunt salvo.")
        self.combo_header_profile.addItem("Perfil: Default", "")
        self.combo_header_profile.activated.connect(self._select_header_profile)
        layout.addWidget(self.combo_header_profile)

        self.btn_pause = QPushButton(header)
        self.btn_pause.setObjectName("iconButton")
        self.btn_pause.setFixedSize(46, 46)
        self.btn_pause.setAccessibleName("Pausar automação")
        self.btn_pause.setToolTip("Pausar automação")
        set_button_icon(self.btn_pause, "pause", 18)
        self.btn_pause.clicked.connect(self._on_pause)
        self.btn_pause.setEnabled(False)
        layout.addWidget(self.btn_pause)

        self.btn_toggle_bot = QPushButton("INICIAR BOT", header)
        self.btn_toggle_bot.setObjectName("startBtn")
        set_button_icon(self.btn_toggle_bot, "play", size=22, color="#ffffff")
        self.master_glow = QGraphicsDropShadowEffect(self.btn_toggle_bot)
        self.master_glow.setBlurRadius(34)
        self.master_glow.setOffset(0, 0)
        self.master_glow.setColor(QColor(14, 165, 233, 150))
        self.master_glow.setEnabled(False)
        self.btn_toggle_bot.setGraphicsEffect(self.master_glow)
        self.btn_toggle_bot.setMinimumSize(176, 50)
        self.btn_toggle_bot.setEnabled(False)
        self.btn_toggle_bot.clicked.connect(self._toggle_bot)
        layout.addWidget(self.btn_toggle_bot)
        return header

    def _refresh_header_processes(self):
        from gui.process_selector import enumerate_windows_processes
        selected = self.combo_processes.currentData()
        blocker = QSignalBlocker(self.combo_processes)
        self.combo_processes.clear()
        self.combo_processes.addItem("Selecione o processo…", None)
        try:
            processes = enumerate_windows_processes()
            preferred = str(self.ctx.get("client", "process_name", default="PokeAlliance_dx.exe")).casefold()
            for name, pid in sorted(processes, key=lambda item: (item[0].casefold() != preferred, item[0].casefold(), item[1])):
                self.combo_processes.addItem(f"{name} · PID {pid}", (name, pid))
            index = self.combo_processes.findData(selected)
            self.combo_processes.setCurrentIndex(max(0, index))
            self.combo_processes.setToolTip("Selecionar conecta a leitura; não inicia a automação.")
        except OSError as error:
            self.combo_processes.setToolTip(str(error))
        del blocker

    def _populate_header_profiles(self):
        combo = getattr(self, "combo_header_profile", None)
        if combo is None or not hasattr(self, "tab_cavebot"):
            return
        current = self.tab_cavebot.active_profile or combo.currentData() or ""
        try:
            names = self.tab_cavebot.profile_manager.list_profiles()
        except Exception:
            names = []
        blocker = QSignalBlocker(combo)
        combo.clear()
        combo.addItem("Perfil: Default", "")
        for name in names:
            combo.addItem(f"Perfil: {name}", name)
        index = combo.findData(current)
        combo.setCurrentIndex(index if index >= 0 else 0)
        del blocker

    def _sync_header_profile(self, name):
        combo = getattr(self, "combo_header_profile", None)
        if combo is None:
            return
        index = combo.findData(name)
        blocker = QSignalBlocker(combo)
        combo.setCurrentIndex(index if index >= 0 else 0)
        del blocker

    def _select_header_profile(self, index):
        name = self.combo_header_profile.itemData(index)
        if not name or not hasattr(self, "tab_cavebot"):
            return
        walker = getattr(self.tab_cavebot, "walker", None)
        if self.worker is not None or (walker and (walker.is_running or walker.recording)):
            self._sync_header_profile(self.tab_cavebot.combo_profiles.currentText())
            return
        profile_index = self.tab_cavebot.combo_profiles.findText(name)
        if profile_index < 0:
            self._populate_header_profiles()
            return
        self.tab_cavebot.combo_profiles.setCurrentIndex(profile_index)
        self.tab_cavebot._on_load_profile()

    def _select_header_process(self, index):
        target = self.combo_processes.itemData(index)
        if target is None:
            return
        walker = self.tab_cavebot.walker
        if self._closing or self.worker is not None or (walker and (walker.is_running or walker.recording)):
            return
        self._pending_target = target
        self.btn_select_process.setEnabled(False)
        self.combo_processes.setEnabled(False)
        self.btn_start.setEnabled(False)
        if self.memory_thread is not None:
            self.memory_thread.stop()
        else:
            self._begin_memory_connection()

    def _open_process_selector(self):
        walker = getattr(self.tab_cavebot, "walker", None)
        if self.worker is not None or (walker and (walker.is_running or walker.recording)):
            QMessageBox.warning(self, "Bot em uso", "Desligue o bot e pare a gravação antes de trocar o processo.")
            return
        from gui.process_selector import ProcessSelectorDialog
        dlg = ProcessSelectorDialog(self, current_pid=self.ctx.get("client", "target_pid", default=None),
                                    current_name=self.ctx.get("client", "process_name", default="PokeAlliance_dx.exe"))
        if dlg.exec() and dlg.selected_name:
            self._pending_target = (dlg.selected_name, dlg.selected_pid)
            self.btn_select_process.setEnabled(False)
            self.btn_start.setEnabled(False)
            if self.memory_thread is not None:
                self.memory_thread.stop()
            else:
                self._begin_memory_connection()
        dlg.deleteLater()

    def _begin_memory_connection(self):
        from gui.dashboard_tab import MemoryMonitor
        name, pid = self._pending_target
        self._pending_target = None
        self.ctx.set(name, "client", "process_name")
        self.ctx.set(pid, "client", "target_pid")
        self.ctx.save_config()
        self.lbl_dash_process.setText(f"{name}" + (f" [PID {pid}]" if pid else ""))
        self._memory_valid = False
        self.live_panel.show_snapshot({"valid": False})
        self._position_updating = False
        self.live_panel.connection.setText("CONECTANDO…")
        self.memory_thread = MemoryMonitor(pid or name, self.config_path, self)
        self.memory_thread.connected.connect(self._memory_connected)
        self.memory_thread.snapshot_ready.connect(self._receive_memory_snapshot)
        self.memory_thread.failed.connect(self._memory_failed)
        self.memory_thread.finished.connect(self._memory_finished)
        self.memory_thread.start()

    def _memory_connected(self, resolver):
        from inputbus import get_bus
        from modules.walker import Walker
        self.resolver = resolver
        if not self._closing:
            walker = Walker(memory_reader=resolver.proc, offset_registry=resolver, input_bus=get_bus(),
                            movement_keys=self.ctx.get("cavebot", "walk_keys", default=None))
            previous = self.tab_cavebot.walker
            if previous is not None:
                route = previous.route_snapshot()
                walker.replace_route(route["waypoints"], route["loop"], route["arrival_tolerance"])
            self.tab_cavebot.set_walker(walker)
            self.btn_select_process.setEnabled(True)
            self.btn_start.setEnabled(True)
            self._log("INFO", "Memória conectada; automação permanece desligada.")

    def _memory_failed(self, message):
        self._memory_valid = False
        self._position_updating = False
        self.live_panel.show_snapshot({"valid": False, "error": message})
        self._log("ERROR", message)
        if not self._closing:
            QMessageBox.warning(self, "Falha de conexão", message)

    def _memory_finished(self):
        monitor = self.memory_thread
        self.memory_thread = None
        self.resolver = None
        self._memory_valid = False
        self._position_updating = False
        if monitor is not None:
            if monitor.resolver is not None:
                monitor.resolver.proc.detach()
            monitor.deleteLater()
        if self.tab_cavebot.walker is not None:
            self.tab_cavebot.walker.request_stop()
            self.tab_cavebot.walker.offsets = None
            self.tab_cavebot.walker.memory = None
        if self._pending_target is not None and not self._closing:
            self._begin_memory_connection()
        else:
            self.btn_select_process.setEnabled(True)
            self.btn_start.setEnabled(False)

    def _start_route_inputs(self):
        if self.worker is not None:
            return
        self.ctx.stop_event.clear()
        self.ctx.resume()
        self.tab_cavebot.walker.set_user_paused(False)
        self.tab_cavebot.walker.bus.start()

    def _receive_memory_snapshot(self, snapshot):
        self._memory_valid = snapshot.get("valid", False)
        self._position_updating = snapshot.get("hp_valid") is True and snapshot.get("position_valid") is False
        self.live_panel.show_snapshot(snapshot)
        if self._position_updating:
            return
        if not self._memory_valid:
            walker = self.tab_cavebot.walker
            if self.worker is not None or (walker is not None and walker.is_running):
                self._on_stop()

    def _healer_snapshot(self):
        if hasattr(self, "pokemon_healer_selector"):
            self.pokemon_healer_selector.save_selection()
        return copy.deepcopy(self.ctx.get("healer", default={}))

    def _apply_healer_profile(self, settings):
        from PySide6.QtCore import QSignalBlocker
        self.ctx.set(copy.deepcopy(settings), "healer")
        selector = self.pokemon_healer_selector
        controls = (selector.skill_combo, selector.revive_combo, selector.pokemon_combo,
                    selector.return_combo, selector.spin_hp_thresh, selector.spin_action_delay,
                    selector.reset_pokemon_combo, selector.reset_delay)
        blockers = [QSignalBlocker(control) for control in controls]
        selector.skill_combo.setCurrentIndex(max(0, selector.skill_combo.findData(settings.get("heal_move", "m4"))))
        revive = settings.get("revive_hotkey", "Shift+F")
        index = selector.revive_combo.findData(revive)
        if index < 0:
            selector.revive_combo.addItem(revive or "Nenhum", revive)
            index = selector.revive_combo.count() - 1
        selector.revive_combo.setCurrentIndex(index)
        selector.spin_hp_thresh.setValue(int(settings.get("hp_percent_threshold", 60)))
        selector.spin_action_delay.setValue(int(settings.get("pokemon_swap_delay_ms", 1000)))
        selector.reset_delay.setValue(int(settings.get("pokemon_reset_delay_ms", 300)))
        selector.refresh()
        selector._populate_roster()
        del blockers
        self.ctx.save_config()

    def _toggle_bot(self):
        walker = self.tab_cavebot.walker
        if self.worker is not None or (walker is not None and walker.is_running):
            self._on_stop()
        else:
            self._on_start()

    def _on_test_healer(self):
        self.pokemon_healer_selector.save_selection()
        self.ctx.save_config()

        def _worker():
            if self.engine and self.engine.modules.get('healer'):
                healer = self.engine.modules['healer']
                healer.on_config_change()
                healer.force_heal_sequence()
                self.ui_log.emit('INFO', '[TESTE] Sequência de cura forçada executada no bot ativo!')
            else:
                try:
                    from inputbus import get_bus
                    from modules.healer import Healer
                    bus = get_bus()
                    bus.start()
                    healer = Healer(ctx=self.ctx, bus=bus, resolver=self.resolver)
                    healer.force_heal_sequence()
                    self._log('INFO', '[TESTE] Sequência forçada de cura executada com sucesso!')
                except Exception as exc:
                    self.ui_log.emit('ERROR', f'[TESTE] Falha ao executar cura de teste: {exc}')

        import threading
        threading.Thread(target=_worker, daemon=True).start()

    def _on_tab_changed(self, index: int):
        if hasattr(self, "tab_healer") and self.tabs.widget(index) is self.page_healer:
            if hasattr(self, "pokemon_healer_selector"):
                self.pokemon_healer_selector.detect_roster()

    def _apply_global_hotkeys(self, bindings):
        try:
            self.global_hotkeys.apply(bindings)
        except ValueError as error:
            self.tab_settings.show_status(str(error), error=True)
            return
        self.ctx.set(dict(bindings), "settings", "hotkeys")
        self.ctx.save_config()
        self.tab_settings.show_status("Atalhos salvos e aplicados. Campos vazios estão desativados.")

    def _restore_window(self):
        self.setWindowState(self.windowState() & ~Qt.WindowMinimized)
        self.show()
        self.raise_()
        self.activateWindow()

    def nativeEvent(self, event_type, message):
        if event_type in (b"windows_generic_MSG", b"windows_dispatcher_MSG") and message is not None:
            import ctypes
            from ctypes import wintypes
            native_message = wintypes.MSG.from_address(int(message))
            manager = getattr(self, "global_hotkeys", None)
            if native_message.message == WM_HOTKEY and manager is not None and not self._closing:
                focused = self.focusWidget()
                if isinstance(focused, HotkeyEdit) and focused.property("capturing"):
                    binding = manager.binding_for(native_message.wParam)
                    if binding:
                        focused._commit(binding)
                        return True, 0
                if manager.dispatch(native_message.wParam):
                    return True, 0
        return super().nativeEvent(event_type, message)

    def _on_force_focus(self):
        def _worker():
            try:
                from inputbus import get_bus
                ok = get_bus().focus_client(force=True)
            except Exception as exc:
                self.ui_log.emit("ERROR", f"[FOCO] Falha ao forçar foco: {exc}")
                return
            if ok:
                self.ui_log.emit("INFO", "[FOCO] Janela do jogo trazida para frente.")
            else:
                self.ui_log.emit("WARNING", "[FOCO] Não foi possível focar a janela do jogo.")

        import threading
        threading.Thread(target=_worker, daemon=True).start()

    def _create_dashboard_tab(self) -> QWidget:
        from gui.dashboard_tab import LiveStatusPanel
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setSpacing(12)
        heading_row = QHBoxLayout()
        heading_col = QVBoxLayout()
        heading_col.setSpacing(3)
        eyebrow = QLabel("OPERAÇÃO", w)
        eyebrow.setObjectName("eyebrow")
        heading = QLabel("Visão geral", w)
        heading.setObjectName("pageTitle")
        caption = QLabel("Cliente, execução e últimas mensagens em um só lugar.", w)
        caption.setObjectName("subtitle")
        heading_col.addWidget(eyebrow)
        heading_col.addWidget(heading)
        heading_col.addWidget(caption)
        heading_row.addLayout(heading_col)
        heading_row.addStretch()
        layout.addLayout(heading_row)

        cards = QHBoxLayout()
        cards.setSpacing(16)
        self.live_panel = LiveStatusPanel(w)
        self.lbl_player_name = self.live_panel.name
        self.lbl_player_hp = self.live_panel.hp
        self.lbl_player_pos = self.live_panel.position
        cards.addWidget(self.live_panel, 5)

        summary = QGroupBox("EXECUÇÃO", w)
        summary.setObjectName("summaryCard")
        summary.setProperty("accent", True)
        summary_layout = QVBoxLayout(summary)
        summary_layout.setContentsMargins(18, 18, 18, 16)
        summary_layout.setSpacing(12)
        status_row = QHBoxLayout()
        status_row.setSpacing(8)
        self.lbl_dash_status = QLabel("SEM CONEXÃO", summary)
        self.lbl_dash_status.setObjectName("statusHeadline")
        status_row.addWidget(self.lbl_dash_status)
        status_row.addStretch()
        uptime_caption = QLabel("SESSÃO", summary)
        uptime_caption.setObjectName("metricLabel")
        self.lbl_dash_uptime = QLabel("00:00:00", summary)
        self.lbl_dash_uptime.setObjectName("metricValue")
        self.lbl_dash_uptime.setToolTip("Tempo desde que o bot foi ligado.")
        status_row.addWidget(uptime_caption)
        status_row.addWidget(self.lbl_dash_uptime)
        summary_layout.addLayout(status_row)

        divider = QFrame(summary)
        divider.setObjectName("divider")
        divider.setFrameShape(QFrame.HLine)
        summary_layout.addWidget(divider)

        grid = QGridLayout()
        grid.setHorizontalSpacing(22)
        grid.setVerticalSpacing(10)
        init_name = self.ctx.get("client", "process_name", default="PokeAlliance_dx.exe")
        init_pid = self.ctx.get("client", "target_pid", default=None)
        pid_text = f" [PID {init_pid}]" if init_pid else ""
        self.lbl_dash_process = QLabel(f"{init_name}{pid_text}", summary)
        self.lbl_dash_process.setObjectName("metricValue")
        self.lbl_dash_process.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.lbl_dash_modules = QLabel("0/0", summary)
        self.lbl_dash_modules.setObjectName("metricValue")
        self.lbl_active_profile = QLabel("Hunt sem salvar", summary)
        self.lbl_active_profile.setObjectName("metricValue")
        self.lbl_active_profile.setWordWrap(True)
        self.lbl_route_state = QLabel("Parada", summary)
        self.lbl_route_state.setObjectName("metricValue")
        self.lbl_healer_state = QLabel("Desligado", summary)
        self.lbl_healer_state.setObjectName("metricValue")
        self.lbl_loop_state = QLabel("Ativado", summary)
        self.lbl_loop_state.setObjectName("metricValue")

        def add_metric(row, column, title, value):
            label = QLabel(title.upper(), summary)
            label.setObjectName("metricLabel")
            grid.addWidget(label, row * 2, column)
            grid.addWidget(value, row * 2 + 1, column)

        add_metric(0, 0, "Processo", self.lbl_dash_process)
        add_metric(1, 0, "Hunt", self.lbl_active_profile)
        add_metric(2, 0, "Rota", self.lbl_route_state)
        self.lbl_dash_modules.hide()
        add_metric(1, 1, "Healer", self.lbl_healer_state)
        add_metric(2, 1, "Loop", self.lbl_loop_state)
        grid.setColumnStretch(0, 3)
        grid.setColumnStretch(1, 2)
        summary_layout.addLayout(grid)
        summary_layout.addStretch()

        controls = QHBoxLayout()
        controls.setSpacing(10)
        self.chk_healer_enabled = QCheckBox("Usar Healer ao ligar o bot", summary)
        self.chk_healer_enabled.setChecked(self.ctx.get("modules", "healer", "enabled", default=False))
        self.chk_healer_enabled.toggled.connect(lambda checked: self.ctx.set(checked, "modules", "healer", "enabled"))
        controls.addWidget(self.chk_healer_enabled)
        controls.addStretch()
        summary_layout.addLayout(controls)
        cards.addWidget(summary, 7)
        layout.addLayout(cards)

        log_group = QGroupBox("ÚLTIMAS MENSAGENS", w)
        log_group.setObjectName("logCard")
        lg_layout = QVBoxLayout(log_group)
        lg_layout.setContentsMargins(14, 12, 14, 12)
        self.log_view = QTextEdit(log_group)
        self.log_view.setReadOnly(True)
        self.log_view.document().setMaximumBlockCount(5)
        self.log_view.setMinimumHeight(100)
        self.log_view.setMaximumHeight(130)
        self.log_view.setFont(QFont("Cascadia Mono", 9))
        self.log_view.setPlaceholderText("As mensagens da execução aparecem aqui.")
        lg_layout.addWidget(self.log_view)
        layout.addWidget(log_group)
        layout.addStretch()
        return w

    def _create_cavebot_tab(self) -> QWidget:
        from ui.cavebot_tab import CavebotTab
        from core.profiles import ProfileManager
        walker = getattr(self.engine, "walker", None) if self.engine else None
        path = Path(self.config_path).resolve().parent if self.config_path else Path(__file__).resolve().parents[1]
        tab = CavebotTab(walker=walker, profile_manager=ProfileManager(path / "hunts.json"),
                         healer_provider=self._healer_snapshot)
        tab.route_start_requested.connect(self._start_route_inputs)
        tab.healer_profile_loaded.connect(self._apply_healer_profile)
        return tab

    def _create_targeting_tab(self) -> QWidget:
        w = ResponsiveColumns()

        panel_targets = self._panel(
            "Configuração de Alvos", "target",
            "Defina quais criaturas serão atacadas e suas preferências.", w)
        panel_combat = self._panel(
            "Mecânicas de Combate & Ataque", "combo",
            "Ajuste ataque, combo e o comportamento do lure.", w)
        panel_recovery = self._panel(
            "Posicionamento & Recuperação", "position",
            "Controle a aproximação, o revive e o fim de combate.", w)

        position_box = self._section(
            "Posicionamento", "position",
            "Aproximação, perseguição e retomada da rota.")
        pform = position_box.form
        combo_box = self._section(
            "Combo", "combo",
            "Tecla ou comando usado para disparar o combo.")
        cmform = combo_box.form

        # ── Alvo ─────────────────────────────────────────────────
        focus_box = self._section(
            "Alvos & Criaturas", "shiny",
            "Por enquanto o Target ataca somente o shiny da hunt; monstros comuns são ignorados.")
        fform = focus_box.form

        self.chk_focus_shiny = QCheckBox()
        self.chk_focus_shiny.setChecked(self.ctx.get("auto_target", "focus_shiny", default=True))
        self.chk_focus_shiny.toggled.connect(lambda v: self.ctx.set(v, "auto_target", "focus_shiny"))
        self._check_row(fform, "shiny", "Só atacar shiny (estrela/boss)", self.chk_focus_shiny,
                        "O shiny tem estrela e é mais forte que os monstros comuns. "
                        "Não depende do nome azul; os normais ficam de fora.")

        add_row = QHBoxLayout()
        add_row.setSpacing(8)
        self.edit_new_target = QLineEdit()
        self.edit_new_target.setPlaceholderText("Adicionar criatura à whitelist...")
        self.edit_new_target.setAccessibleName("Nome da criatura para adicionar")
        self.edit_new_target.returnPressed.connect(self._add_target_from_field)
        self.btn_add_target = QPushButton("Adicionar")
        set_button_icon(self.btn_add_target, "plus", 16)
        self.btn_add_target.clicked.connect(self._add_target_from_field)
        add_row.addWidget(self.edit_new_target, 1)
        add_row.addWidget(self.btn_add_target)
        fform.addRow("", add_row)
        panel_targets.body.addWidget(focus_box)

        # ── Combate ──────────────────────────────────────────────
        combat_box = self._section_grid("Ataque & Combo", "bolt",
                                        "Regras de ataque usadas quando o bot encontra um alvo.")
        cgrid = combat_box.grid

        self.spin_attack_range = QSpinBox()
        self.spin_attack_range.setRange(1, 20)
        self.spin_attack_range.setSuffix(" tiles")
        self.spin_attack_range.setValue(self.ctx.get("auto_target", "attack_range_tiles", default=7))
        self.spin_attack_range.valueChanged.connect(lambda v: self.ctx.set(v, "auto_target", "attack_range_tiles"))
        self._grid_field(cgrid, 0, 0, "range", "Alcance", self.spin_attack_range,
                         "Distância máxima (em tiles) para considerar um inimigo.")

        self.edit_attack_hotkey = HotkeyEdit(
            self.ctx.get("auto_target", "attack_hotkey", default="F5"))
        self.edit_attack_hotkey.editingFinished.connect(
            lambda: self.ctx.set(self.edit_attack_hotkey.text().strip(), "auto_target", "attack_hotkey"))
        self._grid_field(cgrid, 0, 1, "bolt", "Tecla de ataque", self.edit_attack_hotkey,
                         "Tecla que ataca no cliente (a mesma usada para lutar).")

        self.spin_attack_cd = QSpinBox()
        self.spin_attack_cd.setRange(100, 5000)
        self.spin_attack_cd.setSingleStep(50)
        self.spin_attack_cd.setSuffix(" ms")
        self.spin_attack_cd.setValue(self.ctx.get("auto_target", "attack_cooldown_ms", default=900))
        self.spin_attack_cd.valueChanged.connect(lambda v: self.ctx.set(v, "auto_target", "attack_cooldown_ms"))
        self._grid_field(cgrid, 1, 0, "stopwatch", "Espera", self.spin_attack_cd,
                         "Tempo mínimo entre dois ataques, para não spammar a hotkey.")

        self.combo_prioritize = QComboBox()
        for label, token in (("Mais próximo", "nearest"),
                             ("Com menos vida", "lowest_hp_percent"),
                             ("Maior nível", "highest_level")):
            self.combo_prioritize.addItem(label, token)
        current = self.ctx.get("auto_target", "prioritize", default="nearest")
        self.combo_prioritize.setCurrentIndex(max(0, self.combo_prioritize.findData(current)))
        self.combo_prioritize.currentIndexChanged.connect(
            lambda i: self.ctx.set(self.combo_prioritize.itemData(i), "auto_target", "prioritize"))
        self._grid_field(cgrid, 1, 1, "sort", "Priorizar", self.combo_prioritize,
                         "Qual inimigo atacar primeiro quando há vários.")

        self.chk_chase = QCheckBox()
        self.chk_chase.setChecked(self.ctx.get("auto_target", "chase_enabled", default=False))
        self.chk_chase.toggled.connect(lambda v: self.ctx.set(v, "auto_target", "chase_enabled"))
        self._check_row(pform, "chase", "Perseguir o alvo", self.chk_chase,
                        "Anda atrás do inimigo se ele sair do alcance.")

        self.chk_auto_target = QCheckBox()
        self.chk_auto_target.setChecked(self.ctx.get("modules", "auto_target", "enabled", default=False))
        self.chk_auto_target.toggled.connect(
            lambda v: self.ctx.set(v, "modules", "auto_target", "enabled"))
        self._grid_check(cgrid, 2, 0, "bolt", "Combate automático", self.chk_auto_target,
                         "Ataca e freia a rota quando aparece um inimigo ao alcance. "
                         "Religue o bot para aplicar.")

        self.spin_combat_resume = QSpinBox()
        self.spin_combat_resume.setRange(0, 10000)
        self.spin_combat_resume.setSingleStep(100)
        self.spin_combat_resume.setSuffix(" ms")
        self.spin_combat_resume.setValue(
            self.ctx.get("auto_target", "combat_resume_delay_ms", default=1500))
        self.spin_combat_resume.valueChanged.connect(
            lambda v: self.ctx.set(v, "auto_target", "combat_resume_delay_ms"))
        self._field_row(pform, "stopwatch", "Retomar rota", self.spin_combat_resume,
                        "Tempo parado após o último alvo antes de voltar a andar.")
        panel_recovery.body.addWidget(position_box)
        panel_combat.body.addWidget(combat_box)

        # ── Lure e Combo ─────────────────────────────────────────
        lure_box = self._section_grid("Perfect Lure", "lure",
                                      "Perfect Lure: o walker junta bichos até atingir a quantidade e "
                                      "espera eles chegarem perto do seu Pokémon antes de combar.")
        lgrid = lure_box.grid

        self.chk_perfect_lure = QCheckBox()
        self.chk_perfect_lure.setChecked(self.ctx.get("auto_target", "perfect_lure_enabled", default=True))
        self.chk_perfect_lure.toggled.connect(lambda v: self.ctx.set(v, "auto_target", "perfect_lure_enabled"))
        self._grid_check(lgrid, 0, 0, "target", "Perfect Lure", self.chk_perfect_lure,
                         "Junta os bichos e só comba quando o grupo estiver reunido "
                         "(ou quando o HP do Pokémon cair).")

        self.spin_min_lure = QSpinBox()
        self.spin_min_lure.setRange(1, 50)
        self.spin_min_lure.setSuffix(" bichos")
        self.spin_min_lure.setValue(self.ctx.get("auto_target", "min_monsters_to_lure", default=8))
        self.spin_min_lure.valueChanged.connect(
            lambda v: self.ctx.set(v, "auto_target", "min_monsters_to_lure"))
        self._grid_field(lgrid, 1, 0, "target", "Mínimo de bichos", self.spin_min_lure,
                         "Quantos bichos reunir antes de parar e combar.")

        self.spin_lure_radius_sqm = QSpinBox()
        self.spin_lure_radius_sqm.setRange(1, 20)
        self.spin_lure_radius_sqm.setSuffix(" SQM")
        self.spin_lure_radius_sqm.setValue(self.ctx.get("auto_target", "lure_radius_sqm", default=1))
        self.spin_lure_radius_sqm.valueChanged.connect(
            lambda v: self.ctx.set(v, "auto_target", "lure_radius_sqm"))
        self._grid_field(lgrid, 1, 1, "range", "Raio do grupo", self.spin_lure_radius_sqm,
                         "Distância do Pokémon que conta como “perto” para o grupo.")

        self.spin_emergency_hp = QSpinBox()
        self.spin_emergency_hp.setRange(1, 100)
        self.spin_emergency_hp.setSuffix(" %")
        self.spin_emergency_hp.setValue(self.ctx.get("auto_target", "emergency_hp_percent", default=40))
        self.spin_emergency_hp.valueChanged.connect(
            lambda v: self.ctx.set(v, "auto_target", "emergency_hp_percent"))
        hp_holder = QWidget()
        hp_row = QHBoxLayout(hp_holder)
        hp_row.setContentsMargins(0, 0, 0, 0)
        hp_row.setSpacing(9)
        self.slider_emergency_hp = QSlider(Qt.Horizontal)
        self.slider_emergency_hp.setRange(1, 100)
        self.slider_emergency_hp.setValue(self.spin_emergency_hp.value())
        self.slider_emergency_hp.setAccessibleName("HP de emergência")
        self.slider_emergency_hp.valueChanged.connect(self.spin_emergency_hp.setValue)
        self.spin_emergency_hp.valueChanged.connect(self.slider_emergency_hp.setValue)
        hp_row.addWidget(self.spin_emergency_hp, 1)
        hp_row.addWidget(self.slider_emergency_hp, 2)
        self._grid_field(lgrid, 2, 0, "heart", "HP de emergência", hp_holder,
                         "Se o HP do Pokémon cair abaixo disso, comba na hora, mesmo sem o grupo.")

        self.spin_max_lure_wait = QSpinBox()
        self.spin_max_lure_wait.setRange(2500, 5000)
        self.spin_max_lure_wait.setSingleStep(250)
        self.spin_max_lure_wait.setSuffix(" ms")
        self.spin_max_lure_wait.setValue(self.ctx.get("auto_target", "max_lure_wait_ms", default=2500))
        self.spin_max_lure_wait.setToolTip(
            "Espera maxima para o grupo fechar. Ao atingir, comba mesmo sem todos os monstros.")
        self.spin_max_lure_wait.valueChanged.connect(
            lambda v: self.ctx.set(v, "auto_target", "max_lure_wait_ms"))
        self._grid_field(lgrid, 2, 1, "clock", "Espera máxima", self.spin_max_lure_wait,
                         "Tempo máximo reunindo antes de combar de qualquer forma.")

        self.spin_group_stable = QSpinBox()
        self.spin_group_stable.setRange(0, 1000)
        self.spin_group_stable.setSingleStep(50)
        self.spin_group_stable.setSuffix(" ms")
        self.spin_group_stable.setValue(self.ctx.get("auto_target", "group_stable_ms", default=300))
        self.spin_group_stable.valueChanged.connect(
            lambda v: self.ctx.set(v, "auto_target", "group_stable_ms"))
        self._grid_field(lgrid, 3, 0, "clock", "Grupo estável", self.spin_group_stable,
                         "Tempo que o grupo precisa ficar estável antes de combar.")

        self.edit_combo_hotkey = HotkeyEdit(self.ctx.get("auto_target", "combo_hotkey", default="F5"))
        self.edit_combo_hotkey.setPlaceholderText("ex: F5")
        self.edit_combo_hotkey.editingFinished.connect(
            lambda: self.ctx.set(self.edit_combo_hotkey.text().strip(), "auto_target", "combo_hotkey"))
        self._field_row(cmform, "combo", "Tecla do combo", self.edit_combo_hotkey,
                        "Tecla que dispara o combo no jogo (ex: F5).")

        self.edit_combo_command = QLineEdit(self.ctx.get("auto_target", "combo_command", default=""))
        self.edit_combo_command.setPlaceholderText("ex: !autocombo (vazio = usa a tecla)")
        self.edit_combo_command.editingFinished.connect(
            lambda: self.ctx.set(self.edit_combo_command.text().strip(), "auto_target", "combo_command"))
        self._field_row(cmform, "say", "Comando do combo", self.edit_combo_command,
                        "Se preenchido, digita no chat em vez de apertar a tecla (ex: !autocombo).")

        self.spin_combo_repeat = QSpinBox()
        self.spin_combo_repeat.setRange(1, 10)
        self.spin_combo_repeat.setSuffix("x")
        self.spin_combo_repeat.setValue(self.ctx.get("auto_target", "combo_repeat_count", default=3))
        self.spin_combo_repeat.valueChanged.connect(
            lambda v: self.ctx.set(v, "auto_target", "combo_repeat_count"))
        self._field_row(cmform, "refresh", "Repetir comando", self.spin_combo_repeat,
                        "Quantas vezes mandar o AutoCombo por combate (padr?o 3).")

        self.spin_combo_repeat_interval = QSpinBox()
        self.spin_combo_repeat_interval.setRange(1000, 5000)
        self.spin_combo_repeat_interval.setSingleStep(250)
        self.spin_combo_repeat_interval.setSuffix(" ms")
        self.spin_combo_repeat_interval.setValue(
            self.ctx.get("auto_target", "combo_repeat_interval_ms", default=2000))
        self.spin_combo_repeat_interval.valueChanged.connect(
            lambda v: self.ctx.set(v, "auto_target", "combo_repeat_interval_ms"))
        self._field_row(cmform, "clock", "Intervalo do combo", self.spin_combo_repeat_interval,
                        "Intervalo entre os envios. 2000ms = 3 envios em ~4 segundos.")

        self.edit_combo_spellwords = QLineEdit(
            self.ctx.get("auto_target", "combo_spellwords", default=""))
        self.edit_combo_spellwords.setPlaceholderText("ex: m1 m2 m3 (sem v?rgula)")
        self.edit_combo_spellwords.editingFinished.connect(
            lambda: self.ctx.set(self.edit_combo_spellwords.text().strip(),
                                 "auto_target", "combo_spellwords"))
        self._field_row(cmform, "bolt", "Golpes separados", self.edit_combo_spellwords,
                        "Se preenchido, envia cada golpe em uma mensagem separada; "
                        "o AutoCombo n?o ? usado. N?o use v?rgula.")

        self.spin_combo_wait = QSpinBox()
        self.spin_combo_wait.setRange(50, 1000)
        self.spin_combo_wait.setSingleStep(50)
        self.spin_combo_wait.setSuffix(" ms")
        self.spin_combo_wait.setValue(self.ctx.get("auto_target", "combo_wait_ms", default=1000))
        self.spin_combo_wait.valueChanged.connect(lambda v: self.ctx.set(v, "auto_target", "combo_wait_ms"))
        self._grid_field(lgrid, 3, 1, "clock", "Após o combo", self.spin_combo_wait,
                         "Tempo para o combo fazer efeito antes de checar se os bichos morreram.")

        panel_combat.body.addWidget(lure_box)
        panel_recovery.body.addWidget(combo_box)

        # ── Revive ───────────────────────────────────────────────
        revive_box = self._section("Revive & Fim de Combate", "revive",
                                   "Usado quando ainda sobram inimigos ou o seu Pokémon morre.")
        rform = revive_box.form

        self.edit_revive_hotkey = HotkeyEdit(self.ctx.get("auto_target", "revive_hotkey", default="Shift+F"))
        self.edit_revive_hotkey.setPlaceholderText("ex: Shift+F")
        self.edit_revive_hotkey.editingFinished.connect(
            lambda: self.ctx.set(self.edit_revive_hotkey.text().strip(), "auto_target", "revive_hotkey"))
        self._field_row(rform, "revive", "Tecla do revive", self.edit_revive_hotkey,
                        "Combinação de teclas que revive o Pokémon (ex: Shift+F).")

        self.spin_revive_delay = QSpinBox()
        self.spin_revive_delay.setRange(50, 5000)
        self.spin_revive_delay.setSingleStep(50)
        self.spin_revive_delay.setSuffix(" ms")
        self.spin_revive_delay.setValue(self.ctx.get("auto_target", "revive_delay_ms", default=800))
        self.spin_revive_delay.valueChanged.connect(lambda v: self.ctx.set(v, "auto_target", "revive_delay_ms"))
        self._field_row(rform, "stopwatch", "Esperar após o revive", self.spin_revive_delay,
                        "Tempo de espera antes de combar de novo depois do revive.")

        self.spin_combat_loops = QSpinBox()
        self.spin_combat_loops.setRange(1, 20)
        self.spin_combat_loops.setValue(self.ctx.get("auto_target", "max_combat_loops", default=5))
        self.spin_combat_loops.valueChanged.connect(lambda v: self.ctx.set(v, "auto_target", "max_combat_loops"))
        self._field_row(rform, "loop", "Tentativas por combate", self.spin_combat_loops,
                        "Limite de ciclos combo+revive antes de voltar a andar (evita travar).")

        self.chk_post_revive = QCheckBox()
        self.chk_post_revive.setChecked(self.ctx.get("auto_target", "post_combat_revive", default=True))
        self.chk_post_revive.toggled.connect(lambda v: self.ctx.set(v, "auto_target", "post_combat_revive"))
        self._check_row(rform, "revive", "Revive ao fim do combate", self.chk_post_revive,
                        "Garante o Pokémon vivo antes de voltar a andar.")
        panel_recovery.body.addWidget(revive_box)

        # ── Filtros de nome ──────────────────────────────────────
        filter_box = self._section("Criaturas Configuradas", "filter",
                                   "Whitelist vazia = ataca tudo. Blacklist é sempre ignorada.")
        self.edit_target_filter = QLineEdit()
        self.edit_target_filter.setPlaceholderText("Buscar criatura...")
        self.edit_target_filter.setAccessibleName("Buscar criatura")
        self.edit_target_filter.textChanged.connect(self._filter_target_lists)
        filter_box.layout().insertWidget(filter_box.layout().count() - 1, self.edit_target_filter)
        frow = QVBoxLayout()
        frow.setSpacing(8)

        wl_col = QVBoxLayout()
        self.list_whitelist = QListWidget()
        self.list_whitelist.setMaximumHeight(150)
        self.list_whitelist.setToolTip("Deixe vazio para atacar qualquer criatura.")
        for name in self.ctx.get("auto_target", "name_whitelist", default=[]):
            self.list_whitelist.addItem(name)
        wl_col.addWidget(self.list_whitelist)
        wl_btns = QHBoxLayout()
        self.btn_wl_add = QPushButton("Adicionar")
        set_button_icon(self.btn_wl_add, "plus")
        self.btn_wl_add.clicked.connect(lambda: self._add_to_list(self.list_whitelist, "auto_target", "name_whitelist"))
        self.btn_wl_rem = QPushButton("Remover")
        set_button_icon(self.btn_wl_rem, "trash")
        self.btn_wl_rem.clicked.connect(lambda: self._remove_from_list(self.list_whitelist, "auto_target", "name_whitelist"))
        wl_btns.addWidget(self.btn_wl_add)
        wl_btns.addWidget(self.btn_wl_rem)
        wl_col.addLayout(wl_btns)

        bl_col = QVBoxLayout()
        self.list_blacklist = QListWidget()
        self.list_blacklist.setMaximumHeight(150)
        self.list_blacklist.setToolTip("Estes nomes (ou o seu Pokémon) nunca serão alvo.")
        for name in self.ctx.get("auto_target", "name_blacklist", default=[]):
            self.list_blacklist.addItem(name)
        bl_col.addWidget(self.list_blacklist)
        bl_btns = QHBoxLayout()
        self.btn_bl_add = QPushButton("Adicionar")
        set_button_icon(self.btn_bl_add, "plus")
        self.btn_bl_add.clicked.connect(lambda: self._add_to_list(self.list_blacklist, "auto_target", "name_blacklist"))
        self.btn_bl_rem = QPushButton("Remover")
        set_button_icon(self.btn_bl_rem, "trash")
        self.btn_bl_rem.clicked.connect(lambda: self._remove_from_list(self.list_blacklist, "auto_target", "name_blacklist"))
        bl_btns.addWidget(self.btn_bl_add)
        bl_btns.addWidget(self.btn_bl_rem)
        bl_col.addLayout(bl_btns)

        wl_page = QWidget()
        wl_page.setLayout(wl_col)
        bl_page = QWidget()
        bl_page.setLayout(bl_col)
        list_tabs = QTabWidget()
        list_tabs.setObjectName("listTabs")
        list_tabs.setMaximumHeight(252)
        list_tabs.addTab(wl_page, icon("safe", "#3fd0ff"), "Whitelist")
        list_tabs.addTab(bl_page, icon("players", "#ff6b81"), "Blacklist")
        frow.addWidget(list_tabs)
        filter_box.layout().insertLayout(filter_box.layout().count() - 1, frow)
        panel_targets.body.addWidget(filter_box)
        w.set_panels([panel_targets, panel_combat, panel_recovery])
        return w

    def _create_healer_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        heading = QLabel("Healer", w)
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)
        subtitle = QLabel("Configure os Pokémon e o limite de vida. O botão mestre controla a automação.", w)
        subtitle.setObjectName("subtitle")
        layout.addWidget(subtitle)
        healer_card = QGroupBox("CURA AUTOMÁTICA", w)
        healer_layout = QVBoxLayout(healer_card)
        self.pokemon_healer_selector = PokemonHealerSelector(
            self.ctx, w, pid_provider=lambda: self.resolver.proc.pid if self.resolver else None,
            resolver_provider=lambda: self.resolver,
            test_healer_callback=self._on_test_healer)
        self.pokemon_healer_selector.simplify_for_dashboard()
        self.pokemon_healer_selector.client_combo.hide()
        self.pokemon_healer_selector.btn_select_client.hide()
        healer_layout.addWidget(self.pokemon_healer_selector)
        layout.addWidget(healer_card)
        layout.addStretch()
        return w

    def _create_alert_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        heading = QLabel("Alertar", w)
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)
        subtitle = QLabel("Avisa quando um GM ou jogador falar no chat. O som e o webhook são opcionais.", w)
        subtitle.setObjectName("subtitle")
        layout.addWidget(subtitle)

        chat_group = QGroupBox("CHAT", w)
        chat_layout = QFormLayout(chat_group)
        self.chk_chat_player = QCheckBox("Avisar quando um jogador falar")
        self.chk_chat_player.setChecked(self.ctx.get("alarms", "chat_player_enabled", default=True))
        chat_layout.addRow(self.chk_chat_player)
        self.chk_chat_gm = QCheckBox("Avisar quando um GM falar")
        self.chk_chat_gm.setChecked(self.ctx.get("alarms", "chat_gm_enabled", default=True))
        chat_layout.addRow(self.chk_chat_gm)
        self.chk_chat_ignore_self = QCheckBox("Ignorar minhas próprias mensagens")
        self.chk_chat_ignore_self.setChecked(self.ctx.get("alarms", "chat_ignore_self", default=True))
        chat_layout.addRow(self.chk_chat_ignore_self)
        self.chk_chat_include_trade = QCheckBox("Incluir mensagens de trade")
        self.chk_chat_include_trade.setChecked(self.ctx.get("alarms", "chat_include_trade", default=False))
        chat_layout.addRow(self.chk_chat_include_trade)
        self.spin_chat_poll = QSpinBox()
        self.spin_chat_poll.setRange(200, 5000)
        self.spin_chat_poll.setSingleStep(100)
        self.spin_chat_poll.setSuffix(" ms")
        self.spin_chat_poll.setValue(int(self.ctx.get("alarms", "chat_poll_ms", default=500)))
        chat_layout.addRow("Intervalo de leitura:", self.spin_chat_poll)
        self.edit_gm_patterns = QLineEdit(", ".join(self.ctx.get("alarms", "gm_name_patterns", default=[])))
        self.edit_gm_patterns.setToolTip("Separe por vírgula. Ex.: GM, Gamemaster, GodAccess")
        chat_layout.addRow("Nomes/padrões de GM:", self.edit_gm_patterns)
        self.chk_chat_beep = QCheckBox("Tocar som ao receber mensagem")
        self.chk_chat_beep.setChecked(self.ctx.get("alarms", "chat_beep", default=True))
        chat_layout.addRow(self.chk_chat_beep)
        self.chk_chat_webhook = QCheckBox("Enviar webhook (precisa estar habilitado no config)")
        self.chk_chat_webhook.setChecked(self.ctx.get("alarms", "chat_webhook", default=False))
        chat_layout.addRow(self.chk_chat_webhook)
        layout.addWidget(chat_group)

        other_group = QGroupBox("OUTROS ALERTAS", w)
        other_layout = QFormLayout(other_group)
        beep_on = self.ctx.get("alarms", "beep_on", default=[])
        self.chk_alarm_death = QCheckBox("Tocar som ao morrer")
        self.chk_alarm_death.setChecked("death" in beep_on)
        other_layout.addRow(self.chk_alarm_death)
        self.chk_alarm_disconnect = QCheckBox("Tocar som ao desconectar")
        self.chk_alarm_disconnect.setChecked("disconnect" in beep_on)
        other_layout.addRow(self.chk_alarm_disconnect)
        self.chk_alarm_gm = QCheckBox("Tocar som quando um GM aparecer perto")
        self.chk_alarm_gm.setChecked("gm_detected" in beep_on)
        other_layout.addRow(self.chk_alarm_gm)
        layout.addWidget(other_group)

        status_group = QGroupBox("STATUS DO CHAT", w)
        status_layout = QVBoxLayout(status_group)
        self.lbl_chat_status = QLabel("Bot desligado.")
        self.lbl_chat_status.setWordWrap(True)
        status_layout.addWidget(self.lbl_chat_status)
        self.txt_chat_log = QTextEdit()
        self.txt_chat_log.setReadOnly(True)
        self.txt_chat_log.setMinimumHeight(140)
        self.txt_chat_log.setMaximumHeight(240)
        self.txt_chat_log.setPlaceholderText("As mensagens novas aparecem aqui.")
        status_layout.addWidget(self.txt_chat_log)
        layout.addWidget(status_group)

        btn_apply = QPushButton("Aplicar alertas")
        set_button_icon(btn_apply, "save")
        btn_apply.clicked.connect(self._save_alert_config)
        layout.addWidget(btn_apply)
        layout.addStretch()
        return w

    def _setup_tray(self):
        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(icon("power"))
        self.tray.setToolTip("PokeBot")
        menu = QMenu()
        act_show = QAction("Show", self)
        act_show.triggered.connect(self._restore_window)
        act_quit = QAction("Quit", self)
        act_quit.triggered.connect(self.close)
        menu.addAction(act_show)
        menu.addAction(act_quit)
        self.tray.setContextMenu(menu)
        self.tray.show()

    def _load_config_to_ui(self):
        """Sync config values to UI widgets."""
        pass  # Values loaded in widget creation

    def _save_config(self):
        self.pokemon_healer_selector.save_selection()
        self.ctx.save_config()
        self._log("INFO", "Configuration saved")

    def _save_alert_config(self):
        self.ctx.set(self.chk_chat_player.isChecked(), "alarms", "chat_player_enabled")
        self.ctx.set(self.chk_chat_gm.isChecked(), "alarms", "chat_gm_enabled")
        self.ctx.set(self.chk_chat_ignore_self.isChecked(), "alarms", "chat_ignore_self")
        self.ctx.set(self.chk_chat_include_trade.isChecked(), "alarms", "chat_include_trade")
        self.ctx.set(self.chk_chat_beep.isChecked(), "alarms", "chat_beep")
        self.ctx.set(self.chk_chat_webhook.isChecked(), "alarms", "chat_webhook")
        self.ctx.set(int(self.spin_chat_poll.value()), "alarms", "chat_poll_ms")
        patterns = [part.strip() for part in self.edit_gm_patterns.text().split(",") if part.strip()]
        self.ctx.set(patterns, "alarms", "gm_name_patterns")
        beep_on = []
        if self.chk_alarm_death.isChecked():
            beep_on.append("death")
        if self.chk_alarm_disconnect.isChecked():
            beep_on.append("disconnect")
        if self.chk_alarm_gm.isChecked():
            beep_on.append("gm_detected")
        self.ctx.set(beep_on, "alarms", "beep_on")
        self.ctx.set(True, "alarms", "chat_enabled")
        self.ctx.save_config()
        self._log("INFO", "Alertas salvos")

    def _refresh_chat_panel(self):
        if not hasattr(self, "lbl_chat_status"):
            return
        modules = getattr(self.engine, "modules", None) if self.engine is not None else None
        alarms = modules.get("alarms") if isinstance(modules, dict) else None
        if alarms is None:
            self.lbl_chat_status.setText("Bot desligado. Ligue o bot para ler o chat.")
            return
        status = alarms.chat_status
        if status.get("connected"):
            self.lbl_chat_status.setText(
                f"Chat conectado · tick {status.get('tick', 0)} · "
                f"eventos {status.get('events', 0)} · descartados {status.get('dropped', 0)}"
            )
        elif status.get("last_error"):
            self.lbl_chat_status.setText(f"Chat: {status['last_error']}")
        else:
            self.lbl_chat_status.setText("Chat: procurando snapshot (varredura inicial)...")
        recent = alarms.recent_chat
        self.txt_chat_log.setPlainText("\n".join(
            f"{item['time']} [{'GM' if item['gm'] else 'JOGADOR'}] {item['name']}: {item['text']}"
            for item in recent
        ))
        if recent:
            latest = recent[-1]
            key = (latest.get("time"), latest.get("name"), latest.get("text"))
            if key != self._last_chat_key:
                self._last_chat_key = key
                if hasattr(self, "tray"):
                    title = "GM" if latest.get("gm") else "Chat"
                    self.tray.showMessage(title, f"{latest.get('name')}: {latest.get('text')}",
                                          QSystemTrayIcon.Information, 5000)

    def _add_target_from_field(self):
        if not hasattr(self, "edit_new_target"):
            return
        text = self.edit_new_target.text().strip()
        if not text:
            return
        current = list(self.ctx.get("auto_target", "name_whitelist", default=[]))
        if text not in current:
            current.append(text)
            self.ctx.set(current, "auto_target", "name_whitelist")
            self.list_whitelist.addItem(text)
        self.edit_new_target.clear()
        self._filter_target_lists()

    def _filter_target_lists(self, _text=None):
        if not hasattr(self, "edit_target_filter"):
            return
        needle = self.edit_target_filter.text().strip().casefold()
        for list_widget in (self.list_whitelist, self.list_blacklist):
            for index in range(list_widget.count()):
                item = list_widget.item(index)
                item.setHidden(bool(needle) and needle not in item.text().casefold())

    def _add_to_list(self, list_widget: QListWidget, section: str, key: str):
        from PySide6.QtWidgets import QInputDialog
        text, ok = QInputDialog.getText(self, "Add Entry", "Enter name:")
        if ok and text:
            list_widget.addItem(text)
            current = self.ctx.get(section, key, default=[])
            current.append(text)
            self.ctx.set(current, section, key)

    def _remove_from_list(self, list_widget: QListWidget, section: str, key: str):
        for item in list_widget.selectedItems():
            row = list_widget.row(item)
            list_widget.takeItem(row)
            current = self.ctx.get(section, key, default=[])
            if 0 <= row < len(current):
                current.pop(row)
                self.ctx.set(current, section, key)

    def _add_waypoint(self):
        if not self.engine or not self.engine.navigator:
            self._log("WARNING", "Engine not running")
            return
        # Would get current position from resolver
        self._log("INFO", "Add waypoint at current position (requires resolver)")

    def _remove_waypoint(self):
        if not hasattr(self, "wp_table") or self.wp_table is None:
            return
        row = self.wp_table.currentRow()
        if row >= 0 and self.engine and self.engine.navigator:
            self.engine.navigator.remove_waypoint(row)
            self._refresh_waypoint_table()

    def _clear_waypoints(self):
        if self.engine and self.engine.navigator:
            self.engine.navigator.clear_waypoints()
            self._refresh_waypoint_table()

    def _load_waypoints(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load Waypoints", "", "JSON Files (*.json)")
        if path and self.engine and self.engine.navigator:
            self.engine.navigator.load_waypoints(path)
            self._refresh_waypoint_table()

    def _save_waypoints(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save Waypoints", "waypoints.json", "JSON Files (*.json)")
        if path and self.engine and self.engine.navigator:
            self.engine.navigator.save_waypoints(path)

    def _refresh_waypoint_table(self):
        if not self.engine or not self.engine.navigator:
            return
        if not hasattr(self, "wp_table") or self.wp_table is None:
            return
        wps = self.engine.navigator.waypoints
        self.wp_table.setRowCount(len(wps))
        for i, wp in enumerate(wps):
            self.wp_table.setItem(i, 0, QTableWidgetItem(str(i + 1)))
            self.wp_table.setItem(i, 1, QTableWidgetItem(wp.name))
            self.wp_table.setItem(i, 2, QTableWidgetItem(str(wp.x)))
            self.wp_table.setItem(i, 3, QTableWidgetItem(str(wp.y)))
            self.wp_table.setItem(i, 4, QTableWidgetItem(str(wp.z)))
            self.wp_table.setItem(i, 5, QTableWidgetItem(wp.action))

    def _on_start(self):
        if self.engine:
            return
        self._log("INFO", "Starting bot...")

        try:
            from memory import connect_live_resolver
            self.pokemon_healer_selector.save_selection()
            self.ctx.save_config()
            target_pid = self.ctx.get("client", "target_pid", default=None)
            proc_name = self.ctx.get("client", "process_name", default="PokeAlliance_dx.exe")
            target = target_pid if target_pid else proc_name
            if self.resolver is not None and not getattr(self, "_memory_valid", True):
                raise RuntimeError("Leitura de memória indisponível; reconecte o processo no cabeçalho.")
            if self.resolver is None:
                self.resolver = connect_live_resolver(target, config_path=self.config_path)
            previous_walker = getattr(getattr(self, "tab_cavebot", None), "walker", None)
            route = previous_walker.route_snapshot() if previous_walker is not None else None
            if previous_walker is not None:
                previous_walker.request_stop()
            self.engine = create_engine(config_path=self.config_path, resolver=self.resolver)
            if route is not None:
                self.engine.walker.replace_route(route["waypoints"], route["loop"], route["arrival_tolerance"])
            if hasattr(self, "tab_cavebot") and hasattr(self.tab_cavebot, "set_walker") and hasattr(self.engine, "walker"):
                self.tab_cavebot.set_walker(self.engine.walker)
        except Exception as exc:
            if self.resolver and getattr(self, "memory_thread", None) is None:
                self.resolver.proc.detach()
                self.resolver = None
            self.engine = None
            self._log("ERROR", f"Inicialização recusada: {exc}")
            QMessageBox.critical(self, "Falha ao iniciar", str(exc))
            return

        self.worker_thread = QThread()
        self.worker = BotWorker(self.engine, self.resolver)
        self.worker.moveToThread(self.worker_thread)
        self.worker_thread.started.connect(self.worker.run)
        self.worker.status_update.connect(self._on_status_update)
        self.worker.log_message.connect(self._log)
        self.worker_thread.finished.connect(self._on_worker_finished)
        self.worker.finished.connect(self.worker_thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.worker_thread.finished.connect(self.worker_thread.deleteLater)

        self._log_handler = QtLogBridge(self.worker.log_message)
        self._log_handler.setFormatter(logging.Formatter("%(message)s"))
        logging.getLogger("pokebot").addHandler(self._log_handler)

        self.worker_thread.start()

        self.btn_start.setEnabled(True)
        self.btn_start.setText("DESLIGAR BOT")
        self.btn_stop.setEnabled(True)
        self.btn_pause.setEnabled(True)
        if hasattr(self, "tab_cavebot"):
            self.tab_cavebot.btn_load_profile.setEnabled(False)
            self.btn_select_process.setEnabled(False)

    def _on_stop(self):
        walker = getattr(getattr(self, "tab_cavebot", None), "walker", None)
        if walker is not None:
            walker.request_stop()
        if self.worker:
            self.tab_cavebot.setEnabled(False)
            self.worker.stop()
        else:
            self.ctx.stop()
        self._log("INFO", "Stop requested")

    def _on_pause(self):
        walker = self.tab_cavebot.walker
        if self._closing or not (self.worker is not None or (walker and walker.is_running)):
            return
        paused = not self.ctx.is_paused()
        if self.engine is not None:
            self.engine.pause() if paused else self.engine.resume()
        else:
            self.ctx.pause() if paused else self.ctx.resume()
        if walker is not None:
            walker.set_user_paused(paused)
        label = "Retomar automação" if paused else "Pausar automação"
        self.btn_pause.setToolTip(label)
        self.btn_pause.setAccessibleName(label)
        set_button_icon(self.btn_pause, "play" if paused else "pause")
        self._update_status_bar()

    def _on_worker_finished(self):
        if hasattr(self, "_log_handler") and self._log_handler:
            try:
                logging.getLogger("pokebot").removeHandler(self._log_handler)
            except Exception:
                pass
            self._log_handler = None

        self.btn_start.setEnabled(self.resolver is not None)
        self.btn_start.setText("INICIAR BOT")
        self.btn_stop.setEnabled(False)
        self.btn_pause.setEnabled(False)
        self.btn_pause.setToolTip("Pausar automação")
        self.worker = None
        self.worker_thread = None
        self.engine = None
        self.tab_cavebot.btn_load_profile.setEnabled(True)
        self.tab_cavebot.setEnabled(not self._closing)
        self.btn_select_process.setEnabled(True)
        if self.resolver is not None and self.memory_thread is None:
            self.resolver.proc.detach()
            self.resolver = None
        if self.resolver is not None and not self._closing:
            self._memory_connected(self.resolver)

    def _on_status_update(self, status: dict):
        # Modules
        mods = status.get("modules", {})
        active = sum(1 for v in mods.values() if v)
        total = len(mods)
        self.lbl_dash_modules.setText(f"{active}/{total}")

        # Navigator
        nav = status.get("navigator", {})
        self._refresh_waypoint_table()

    def _update_status_bar(self):
        connected = self.resolver is not None and self._memory_valid
        updating = self.resolver is not None and self._position_updating
        walker = self.tab_cavebot.walker
        running = self.worker is not None or (walker is not None and walker.is_running)
        paused = self.ctx.is_paused() if running else False
        self.btn_pause.setEnabled(running and not self._closing)
        color = DARK_PALETTE['green'] if connected else DARK_PALETTE['yellow'] if updating else DARK_PALETTE['red']
        if connected:
            header_state = "ONLINE"
        elif updating:
            header_state = "SINCRONIZANDO"
        else:
            header_state = "OFFLINE"
        self.lbl_connection.setText(header_state)
        self.lbl_connection.setStyleSheet(f"color: {color};")
        proc_name = self.ctx.get("client", "process_name", default="PokeAlliance_dx.exe")
        proc_pid = self.ctx.get("client", "target_pid", default=None)
        if self.resolver is not None and getattr(getattr(self.resolver, "proc", None), "pid", None):
            proc_pid = self.resolver.proc.pid
        self.lbl_header_process.setText(
            f"{proc_name} (PID {proc_pid})" if proc_pid else "Nenhum cliente conectado")
        self.btn_toggle_bot.setText("DESLIGAR BOT" if running else "INICIAR BOT")
        object_name = "stopBtn" if running else "startBtn"
        if self.btn_toggle_bot.objectName() != object_name:
            self.btn_toggle_bot.setObjectName(object_name)
            set_button_icon(self.btn_toggle_bot, "stop" if running else "play", size=22,
                            color="#ffffff")
            self.btn_toggle_bot.style().unpolish(self.btn_toggle_bot)
            self.btn_toggle_bot.style().polish(self.btn_toggle_bot)
        self.lbl_active_profile.setText(self.tab_cavebot.active_profile or "Hunt sem salvar")
        healer_enabled = self.ctx.get("modules", "healer", "enabled", default=False)
        if self.chk_healer_enabled.isChecked() != healer_enabled:
            blocker = QSignalBlocker(self.chk_healer_enabled)
            self.chk_healer_enabled.setChecked(healer_enabled)
            del blocker
        self.lbl_healer_state.setText("Habilitado" if healer_enabled else "Desligado")
        self.lbl_loop_state.setText("Ativado" if self.tab_cavebot.chk_loop.isChecked() else "Desativado")
        if walker is None:
            route_state = "Sem leitura"
        elif walker.recording:
            route_state = "Gravando"
        elif walker.is_running and paused:
            route_state = "Pausada"
        elif walker.is_running:
            route_state = "Em execução"
        else:
            route_state = "Parada"
        self.lbl_route_state.setText(route_state)
        self.btn_toggle_bot.setEnabled((connected or running) and not self._closing and self._pending_target is None)
        self.master_glow.setEnabled(self.btn_toggle_bot.isEnabled() and not running)
        if running and paused:
            status_text, status_color = "BOT PAUSADO", DARK_PALETTE['yellow']
        elif running:
            status_text, status_color = "BOT LIGADO", DARK_PALETTE['green']
        elif connected:
            status_text, status_color = "PRONTO PARA INICIAR", DARK_PALETTE['accent']
        elif updating:
            status_text, status_color = "AGUARDANDO POSIÇÃO", DARK_PALETTE['yellow']
        else:
            status_text, status_color = "SEM CONEXÃO", DARK_PALETTE['fg_muted']
        self.lbl_dash_status.setText(status_text)
        self.lbl_dash_status.setStyleSheet(f"color: {status_color};")
        self.status_dot.setStyleSheet(f"background: {status_color}; border-radius: 4px;")
        self.btn_select_process.setEnabled(not self._closing and not running and not (walker and walker.recording)
                                           and (self.memory_thread is None or self.resolver is not None))
        self.combo_processes.setEnabled(self.btn_select_process.isEnabled())
        self.pokemon_healer_selector.btn_test_healer.setEnabled(connected and not self._closing)
        start_ts = self.ctx.stats.get("uptime_start")
        elapsed = max(0, int(time.time() - start_ts)) if running and start_ts else 0
        hours, remainder = divmod(elapsed, 3600)
        minutes, seconds = divmod(remainder, 60)
        text = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        self.lbl_dash_uptime.setText(text)

    def _log(self, level: str, msg: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        color = {
            "DEBUG": DARK_PALETTE["fg_muted"],
            "INFO": DARK_PALETTE["fg"],
            "WARNING": DARK_PALETTE["yellow"],
            "ERROR": DARK_PALETTE["red"],
        }.get(level, DARK_PALETTE["fg"])
        self.log_view.append(f'<span style="color:{color}">[{timestamp}] [{level}] {html.escape(msg)}</span>')

    def closeEvent(self, event):
        if self._allow_close:
            self.status_timer.stop()
            self.tab_cavebot.debug_timer.stop()
            self.ctx.save_config()
            event.accept()
            return
        event.ignore()
        if not self._closing:
            self._closing = True
            self.global_hotkeys.close()
            self._pending_target = None
            self._on_stop()
            self.tab_cavebot.setEnabled(False)
            self._complete_close()

    def _complete_close(self):
        walker = self.tab_cavebot.walker
        if self.worker is not None or (walker is not None and walker.is_running):
            QTimer.singleShot(50, self._complete_close)
            return
        if self.memory_thread is not None:
            self.memory_thread.stop()
            QTimer.singleShot(50, self._complete_close)
            return
        if self.resolver is not None:
            self.resolver.proc.detach()
            self.resolver = None
        if walker is not None:
            if self._input_shutdown is None:
                self._input_shutdown = threading.Thread(target=walker.bus.stop, daemon=True)
                self._input_shutdown.start()
            if self._input_shutdown.is_alive():
                QTimer.singleShot(50, self._complete_close)
                return
        self._allow_close = True
        self.close()


def main(config_path: str = None):
    app = QApplication(sys.argv)
    apply_dark_theme(app)
    app.setFont(QFont("Segoe UI", 9))

    window = MainWindow(config_path)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
