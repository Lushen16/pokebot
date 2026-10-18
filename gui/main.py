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
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTabWidget, QLabel, QPushButton, QGroupBox, QFormLayout,
    QLineEdit, QSpinBox, QDoubleSpinBox, QCheckBox, QComboBox,
    QTextEdit, QListWidget, QListWidgetItem, QTableWidget,
    QTableWidgetItem, QHeaderView, QSplitter, QScrollArea,
    QFileDialog, QMessageBox, QStatusBar, QProgressBar,
    QTreeWidget, QTreeWidgetItem, QMenu, QSystemTrayIcon, QFrame, QLayout, QGraphicsDropShadowEffect,
)

from context import get_context, BotContext
from engine import create_engine
from navigator import Navigator, Waypoint
from gui.pokemon_selector import PokemonHealerSelector
from gui.icons import icon, set_button_icon

log = logging.getLogger("pokebot.gui")


# ─── Theme ────────────────────────────────────────────────────────────────
DARK_PALETTE = {
    "bg": "#0e1117",
    "bg_secondary": "#161b22",
    "bg_tertiary": "#0f0f23",
    "fg": "#c9d1d9",
    "fg_muted": "#8b949e",
    "accent": "#00a8ff",
    "accent_hover": "#00eaff",
    "green": "#00ff88",
    "red": "#ff4444",
    "yellow": "#ffcc00",
    "border": "#2a2a4a",
    "input_bg": "#1e1e3a",
    "input_fg": "#ffffff",
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

    app.setStyleSheet(Path(__file__).with_name("style.qss").read_text(encoding="utf-8"))


# ─── Worker Thread ────────────────────────────────────────────────────────

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
        self.route_controls.addWidget(self.tab_cavebot.btn_start_route)
        self.route_controls.addWidget(self.tab_cavebot.btn_start_nearest)
        self.route_controls.addWidget(self.tab_cavebot.btn_stop_route)
        self.tab_targeting = self._create_targeting_tab()
        self.tab_combat = self._create_combat_tab()
        self.tab_cavebot.control_group.setTitle("GRAVAÇÃO E REPETIÇÃO")
        self.tab_cavebot.control_group.layout().addWidget(QLabel("Inicie ou pare a rota pelo Dashboard."))
        self.tab_loot = self._create_loot_tab()
        self.tab_settings = self._create_settings_tab()
        self.tab_debug = self._create_debug_tab()

        self.page_dashboard = self._scroll_page(self.tab_dashboard)
        self.page_cavebot = self._scroll_page(self.tab_cavebot)
        self.tabs.addTab(self.page_dashboard, icon("dashboard"), "Dashboard")
        self.tabs.addTab(self.page_cavebot, icon("map"), "Cavebot")
        self.tabs.addTab(self.tab_targeting, icon("target"), "Alvos")
        self.tabs.addTab(self.tab_combat, icon("target"), "Combate")
        self.tabs.addTab(self.tab_loot, icon("loot"), "Loot")
        self.tabs.addTab(self.tab_settings, icon("settings"), "Configurações")
        self.tabs.addTab(self.tab_debug, icon("debug"), "Diagnóstico")
        self.tabs.setIconSize(QSize(22, 22))
        self.tabs.currentChanged.connect(self._on_tab_changed)

        # Status bar
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.lbl_connection = QLabel("● Disconnected")
        self.lbl_connection.setStyleSheet(f"color: {DARK_PALETTE['red']};")
        self.lbl_uptime = QLabel("Uptime: 00:00:00")
        self.status_bar.addPermanentWidget(self.lbl_connection)
        self.status_bar.addPermanentWidget(self.lbl_uptime)

        self.btn_start = self.btn_toggle_bot
        self.btn_stop = QPushButton(self)
        self.btn_stop.hide()
        self.btn_pause = QPushButton("PAUSAR", self.tab_dashboard)
        set_button_icon(self.btn_pause, "pause")
        self.btn_pause.clicked.connect(self._on_pause)
        self.btn_pause.setEnabled(False)
        self.route_controls.addWidget(self.btn_pause)
        self._refresh_header_processes()

    def _scroll_page(self, widget):
        widget.layout().setSizeConstraint(QLayout.SetMinimumSize)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setWidget(widget)
        return scroll

    def _create_header(self, parent):
        header = QFrame(parent)
        header.setObjectName("header")
        layout = QHBoxLayout(header)
        brand = QVBoxLayout()
        title = QLabel("PokeBot", header)
        title.setObjectName("brand")
        self.lbl_header_connection = QLabel("● Desconectado", header)
        self.lbl_header_connection.setWordWrap(True)
        brand_title = QHBoxLayout()
        emblem = QLabel(header)
        emblem.setPixmap(icon("power").pixmap(30, 30))
        brand_title.addWidget(emblem)
        brand_title.addWidget(title)
        brand_title.addStretch()
        brand.addLayout(brand_title)
        brand.addWidget(self.lbl_header_connection)
        layout.addLayout(brand, 1)
        self.combo_processes = QComboBox(header)
        self.combo_processes.setMinimumWidth(240)
        self.combo_processes.setAccessibleName("Processo do jogo")
        self.combo_processes.activated.connect(self._select_header_process)
        layout.addWidget(self.combo_processes)
        self.btn_select_process = QPushButton("Atualizar Processos", header)
        set_button_icon(self.btn_select_process, "refresh")
        self.btn_select_process.clicked.connect(self._refresh_header_processes)
        layout.addWidget(self.btn_select_process)
        self.btn_toggle_bot = QPushButton("LIGAR BOT", header)
        self.btn_toggle_bot.setObjectName("startBtn")
        set_button_icon(self.btn_toggle_bot, "play", size=26, color="#052b1a")
        self.master_glow = QGraphicsDropShadowEffect(self.btn_toggle_bot)
        self.master_glow.setBlurRadius(26)
        self.master_glow.setOffset(0, 0)
        self.master_glow.setColor(QColor(0, 230, 118, 100))
        self.master_glow.setEnabled(False)
        self.btn_toggle_bot.setGraphicsEffect(self.master_glow)
        self.btn_toggle_bot.setMinimumSize(205, 50)
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
            walker = Walker(memory_reader=resolver.proc, offset_registry=resolver, input_bus=get_bus())
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
        if hasattr(self, "tab_combat") and self.tabs.widget(index) == self.tab_combat:
            if hasattr(self, "pokemon_healer_selector"):
                self.pokemon_healer_selector.detect_roster()

    def _create_dashboard_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        heading = QLabel("Sua central de operações", w)
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)
        from gui.dashboard_tab import SetupStep
        steps = QHBoxLayout()
        for number, title, caption, icon_name in (
                ("01", "Conecte o cliente", "Selecione o processo no cabeçalho.", "position"),
                ("02", "Prepare sua hunt", "Grave ou carregue uma rota no Cavebot.", "map"),
                ("03", "Inicie a automação", "Use LIGAR BOT e acompanhe os estados.", "power")):
            steps.addWidget(SetupStep(number, title, caption, icon_name, w), 1)
        layout.addLayout(steps)
        cards = QHBoxLayout()
        layout.addLayout(cards)

        # Status group
        status_group = QGroupBox("AUTOMAÇÃO")
        s_layout = QFormLayout(status_group)
        self.lbl_dash_status = QLabel("STOPPED")
        self.lbl_dash_status.setStyleSheet(f"color: {DARK_PALETTE['red']}; font-size: 18px; font-weight: bold;")
        s_layout.addRow("Estado:", self.lbl_dash_status)
        self.lbl_dash_uptime = QLabel("00:00:00")
        s_layout.addRow("Uptime:", self.lbl_dash_uptime)
        self.lbl_dash_modules = QLabel("0/6 active")
        s_layout.addRow("Módulos:", self.lbl_dash_modules)
        init_name = self.ctx.get("client", "process_name", default="PokeAlliance_dx.exe")
        init_pid = self.ctx.get("client", "target_pid", default=None)
        pid_text = f" [PID {init_pid}]" if init_pid else ""
        self.lbl_dash_process = QLabel(f"{init_name}{pid_text}")
        self.lbl_dash_process.setStyleSheet(f"color: {DARK_PALETTE['accent']}; font-weight: bold;")
        s_layout.addRow("Processo:", self.lbl_dash_process)
        self.lbl_active_profile = QLabel("Nenhuma hunt carregada")
        self.lbl_healer_state = QLabel("Desativado")
        self.lbl_loop_state = QLabel("Ativado")
        s_layout.addRow("Hunt:", self.lbl_active_profile)
        s_layout.addRow("Healer:", self.lbl_healer_state)
        self.chk_healer_enabled = QCheckBox("Usar Healer ao ligar o bot", w)
        self.chk_healer_enabled.setChecked(self.ctx.get("modules", "healer", "enabled", default=False))
        self.chk_healer_enabled.toggled.connect(lambda checked: self.ctx.set(checked, "modules", "healer", "enabled"))
        s_layout.addRow(self.chk_healer_enabled)
        s_layout.addRow("Loop:", self.lbl_loop_state)
        cards.addWidget(status_group, 1)

        # Stats group
        stats_group = QGroupBox("Session Statistics")
        st_layout = QFormLayout(stats_group)
        self.stat_labels = {}
        for key in ["heals", "mana_heals", "food_eaten", "creatures_killed", "items_looted", "waypoints_reached", "stuck_recoveries", "deaths", "disconnects"]:
            label = QLabel("0")
            label.setStyleSheet(f"color: {DARK_PALETTE['accent']}; font-weight: bold;")
            self.stat_labels[key] = label
            st_layout.addRow(key.replace("_", " ").title() + ":", label)
        from ui.combat_tab import CollapsibleSection
        statistics = CollapsibleSection("Estatísticas da sessão", w)
        stats_group.setTitle("INDICADORES")
        statistics.content_layout.addWidget(stats_group)
        layout.addWidget(statistics)

        from gui.dashboard_tab import LiveStatusPanel
        self.live_panel = LiveStatusPanel(w)
        self.lbl_player_name = self.live_panel.name
        self.lbl_player_hp = self.live_panel.hp
        self.lbl_player_pos = self.live_panel.position
        cards.insertWidget(0, self.live_panel, 1)
        route_group = QGroupBox("CONTROLE DA ROTA", w)
        self.route_controls = QHBoxLayout(route_group)
        layout.addWidget(route_group)

        # Recent events log
        log_group = QGroupBox("ATIVIDADE RECENTE · ÚLTIMAS 5 MENSAGENS")
        lg_layout = QVBoxLayout(log_group)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.document().setMaximumBlockCount(5)
        self.log_view.setMaximumHeight(150)
        self.log_view.setFont(QFont("Consolas", 9))
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
        w = QWidget()
        layout = QVBoxLayout(w)

        # Targeting settings
        t_group = QGroupBox("Targeting Settings")
        t_layout = QFormLayout(t_group)

        self.spin_attack_range = QSpinBox()
        self.spin_attack_range.setRange(1, 20)
        self.spin_attack_range.setValue(self.ctx.get("auto_target", "attack_range_tiles", default=7))
        self.spin_attack_range.valueChanged.connect(lambda v: self.ctx.set(v, "auto_target", "attack_range_tiles"))
        t_layout.addRow("Attack Range (tiles):", self.spin_attack_range)

        self.spin_attack_cd = QSpinBox()
        self.spin_attack_cd.setRange(100, 5000)
        self.spin_attack_cd.setSingleStep(50)
        self.spin_attack_cd.setValue(self.ctx.get("auto_target", "attack_cooldown_ms", default=900))
        self.spin_attack_cd.valueChanged.connect(lambda v: self.ctx.set(v, "auto_target", "attack_cooldown_ms"))
        t_layout.addRow("Attack Cooldown (ms):", self.spin_attack_cd)

        self.combo_prioritize = QComboBox()
        self.combo_prioritize.addItems(["lowest_hp_percent", "nearest", "highest_level"])
        self.combo_prioritize.setCurrentText(self.ctx.get("auto_target", "prioritize", default="lowest_hp_percent"))
        self.combo_prioritize.currentTextChanged.connect(lambda v: self.ctx.set(v, "auto_target", "prioritize"))
        t_layout.addRow("Prioritize:", self.combo_prioritize)

        self.chk_ignore_players = QCheckBox()
        self.chk_ignore_players.setChecked(self.ctx.get("auto_target", "ignore_players", default=True))
        self.chk_ignore_players.toggled.connect(lambda v: self.ctx.set(v, "auto_target", "ignore_players"))
        t_layout.addRow("Ignore Players:", self.chk_ignore_players)

        self.chk_ignore_npcs = QCheckBox()
        self.chk_ignore_npcs.setChecked(self.ctx.get("auto_target", "ignore_npcs", default=True))
        self.chk_ignore_npcs.toggled.connect(lambda v: self.ctx.set(v, "auto_target", "ignore_npcs"))
        t_layout.addRow("Ignore NPCs:", self.chk_ignore_npcs)

        self.chk_ignore_dead = QCheckBox()
        self.chk_ignore_dead.setChecked(self.ctx.get("auto_target", "ignore_dead", default=True))
        self.chk_ignore_dead.toggled.connect(lambda v: self.ctx.set(v, "auto_target", "ignore_dead"))
        t_layout.addRow("Ignore Dead:", self.chk_ignore_dead)

        self.chk_chase = QCheckBox()
        self.chk_chase.setChecked(self.ctx.get("auto_target", "chase_enabled", default=False))
        self.chk_chase.toggled.connect(lambda v: self.ctx.set(v, "auto_target", "chase_enabled"))
        t_layout.addRow("Chase Enabled:", self.chk_chase)

        layout.addWidget(t_group)

        # Whitelist/Blacklist
        wl_group = QGroupBox("Name Filters")
        wl_layout = QHBoxLayout(wl_group)

        wl_left = QVBoxLayout()
        wl_left.addWidget(QLabel("Whitelist (empty = all)"))
        self.list_whitelist = QListWidget()
        for name in self.ctx.get("auto_target", "name_whitelist", default=[]):
            self.list_whitelist.addItem(name)
        wl_left.addWidget(self.list_whitelist)
        wl_btns = QHBoxLayout()
        self.btn_wl_add = QPushButton("Add")
        set_button_icon(self.btn_wl_add, "plus")
        self.btn_wl_add.clicked.connect(lambda: self._add_to_list(self.list_whitelist, "auto_target", "name_whitelist"))
        self.btn_wl_rem = QPushButton("Remove")
        set_button_icon(self.btn_wl_rem, "trash")
        self.btn_wl_rem.clicked.connect(lambda: self._remove_from_list(self.list_whitelist, "auto_target", "name_whitelist"))
        wl_btns.addWidget(self.btn_wl_add)
        wl_btns.addWidget(self.btn_wl_rem)
        wl_left.addLayout(wl_btns)

        wl_right = QVBoxLayout()
        wl_right.addWidget(QLabel("Blacklist"))
        self.list_blacklist = QListWidget()
        for name in self.ctx.get("auto_target", "name_blacklist", default=[]):
            self.list_blacklist.addItem(name)
        wl_right.addWidget(self.list_blacklist)
        bl_btns = QHBoxLayout()
        self.btn_bl_add = QPushButton("Add")
        set_button_icon(self.btn_bl_add, "plus")
        self.btn_bl_add.clicked.connect(lambda: self._add_to_list(self.list_blacklist, "auto_target", "name_blacklist"))
        self.btn_bl_rem = QPushButton("Remove")
        set_button_icon(self.btn_bl_rem, "trash")
        self.btn_bl_rem.clicked.connect(lambda: self._remove_from_list(self.list_blacklist, "auto_target", "name_blacklist"))
        bl_btns.addWidget(self.btn_bl_add)
        bl_btns.addWidget(self.btn_bl_rem)
        wl_right.addLayout(bl_btns)

        wl_layout.addLayout(wl_left)
        wl_layout.addLayout(wl_right)
        layout.addWidget(wl_group)
        return w

    def _create_combat_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        heading = QLabel("Combate e cura", w)
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
        from ui.combat_tab import CollapsibleSection
        extras = CollapsibleSection("Outras configurações · comida e captura", w)
        layout.addWidget(extras)

        # Auto-food
        f_group = QGroupBox("Auto-Food")
        f_layout = QFormLayout(f_group)

        self.spin_food_interval = QSpinBox()
        self.spin_food_interval.setRange(10, 600)
        self.spin_food_interval.setValue(self.ctx.get("auto_food", "interval_seconds", default=60))
        self.spin_food_interval.valueChanged.connect(lambda v: self.ctx.set(v, "auto_food", "interval_seconds"))
        f_layout.addRow("Interval (seconds):", self.spin_food_interval)

        self.spin_food_jitter = QSpinBox()
        self.spin_food_jitter.setRange(0, 60)
        self.spin_food_jitter.setValue(self.ctx.get("auto_food", "jitter_seconds", default=8))
        self.spin_food_jitter.valueChanged.connect(lambda v: self.ctx.set(v, "auto_food", "jitter_seconds"))
        f_layout.addRow("Jitter (seconds):", self.spin_food_jitter)

        self.spin_food_pause_hp = QSpinBox()
        self.spin_food_pause_hp.setRange(0, 99)
        self.spin_food_pause_hp.setValue(self.ctx.get("auto_food", "pause_when_hp_below_percent", default=30))
        self.spin_food_pause_hp.valueChanged.connect(lambda v: self.ctx.set(v, "auto_food", "pause_when_hp_below_percent"))
        f_layout.addRow("Pause if HP Below (%):", self.spin_food_pause_hp)

        extras.content_layout.addWidget(f_group)

        # Auto-catch
        c_group = QGroupBox("Auto-Catch")
        c_layout = QFormLayout(c_group)

        self.spin_catch_throws = QSpinBox()
        self.spin_catch_throws.setRange(1, 10)
        self.spin_catch_throws.setValue(self.ctx.get("auto_catch", "throw_count", default=3))
        self.spin_catch_throws.valueChanged.connect(lambda v: self.ctx.set(v, "auto_catch", "throw_count"))
        c_layout.addRow("Throw Count:", self.spin_catch_throws)

        self.spin_catch_delay = QSpinBox()
        self.spin_catch_delay.setRange(100, 5000)
        self.spin_catch_delay.setSingleStep(50)
        self.spin_catch_delay.setValue(self.ctx.get("auto_catch", "throw_delay_ms", default=750))
        self.spin_catch_delay.valueChanged.connect(lambda v: self.ctx.set(v, "auto_catch", "throw_delay_ms"))
        c_layout.addRow("Throw Delay (ms):", self.spin_catch_delay)

        self.chk_only_after_kill = QCheckBox()
        self.chk_only_after_kill.setChecked(self.ctx.get("auto_catch", "only_after_kill", default=True))
        self.chk_only_after_kill.toggled.connect(lambda v: self.ctx.set(v, "auto_catch", "only_after_kill"))
        c_layout.addRow("Only After Kill:", self.chk_only_after_kill)

        self.spin_catch_stop_hp = QSpinBox()
        self.spin_catch_stop_hp.setRange(1, 100)
        self.spin_catch_stop_hp.setValue(self.ctx.get("auto_catch", "stop_when_hp_above_percent", default=60))
        self.spin_catch_stop_hp.valueChanged.connect(lambda v: self.ctx.set(v, "auto_catch", "stop_when_hp_above_percent"))
        c_layout.addRow("Stop If HP Above (%):", self.spin_catch_stop_hp)

        extras.content_layout.addWidget(c_group)
        layout.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(w)
        return scroll

    def _create_loot_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        group = QGroupBox("Loot Settings")
        g_layout = QFormLayout(group)

        self.list_loot = QListWidget()
        # Would load from config - placeholder
        self.list_loot.addItem("Gold Coin")
        self.list_loot.addItem("Platinum Coin")
        self.list_loot.addItem("Health Potion")
        g_layout.addRow("Desired Items:", self.list_loot)

        loot_btns = QHBoxLayout()
        self.btn_loot_add = QPushButton("Add")
        self.btn_loot_rem = QPushButton("Remove")
        loot_btns.addWidget(self.btn_loot_add)
        loot_btns.addWidget(self.btn_loot_rem)
        g_layout.addRow("", loot_btns)

        layout.addWidget(group)
        layout.addStretch()
        return w

    def _create_settings_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        # General
        gen_group = QGroupBox("General")
        gen_layout = QFormLayout(gen_group)

        gen_layout.addRow("Cliente:", QLabel("Seleção centralizada no cabeçalho"))

        self.edit_window_title = QLineEdit(self.ctx.get("client", "window_title_pattern", default="PokeAlliance"))
        self.edit_window_title.textChanged.connect(lambda v: self.ctx.set(v, "client", "window_title_pattern"))
        gen_layout.addRow("Window Title Pattern:", self.edit_window_title)

        self.chk_focus = QCheckBox()
        self.chk_focus.setChecked(self.ctx.get("client", "focus_before_input", default=True))
        self.chk_focus.toggled.connect(lambda v: self.ctx.set(v, "client", "focus_before_input"))
        gen_layout.addRow("Focus Before Input:", self.chk_focus)

        self.spin_refocus = QSpinBox()
        self.spin_refocus.setRange(0, 1000)
        self.spin_refocus.setValue(self.ctx.get("client", "refocus_delay_ms", default=120))
        self.spin_refocus.valueChanged.connect(lambda v: self.ctx.set(v, "client", "refocus_delay_ms"))
        gen_layout.addRow("Refocus Delay (ms):", self.spin_refocus)

        self.spin_click_delay = QSpinBox()
        self.spin_click_delay.setRange(0, 500)
        self.spin_click_delay.setValue(self.ctx.get("client", "click_delay_ms", default=45))
        self.spin_click_delay.valueChanged.connect(lambda v: self.ctx.set(v, "client", "click_delay_ms"))
        gen_layout.addRow("Click Delay (ms):", self.spin_click_delay)

        self.spin_key_delay = QSpinBox()
        self.spin_key_delay.setRange(0, 500)
        self.spin_key_delay.setValue(self.ctx.get("client", "key_delay_ms", default=30))
        self.spin_key_delay.valueChanged.connect(lambda v: self.ctx.set(v, "client", "key_delay_ms"))
        gen_layout.addRow("Key Delay (ms):", self.spin_key_delay)

        layout.addWidget(gen_group)

        # Hotkeys
        hk_group = QGroupBox("Hotkeys")
        hk_layout = QFormLayout(hk_group)

        hk_map = {
            "panic_stop": "F12",
            "heal_hp": "F1",
            "heal_mp": "F2",
            "heal_panic": "F3",
            "food": "F4",
            "attack": "F5",
            "catch": "F6",
        }
        self.hk_edits = {}
        for key, default in hk_map.items():
            edit = QLineEdit(self.ctx.get("hotkeys", key, default=default))
            edit.textChanged.connect(lambda v, k=key: self.ctx.set(v, "hotkeys", k))
            hk_layout.addRow(key.replace("_", " ").title() + ":", edit)
            self.hk_edits[key] = edit

        self.chk_global_hotkeys = QCheckBox()
        self.chk_global_hotkeys.setChecked(self.ctx.get("hotkeys", "global_hotkeys", default=False))
        self.chk_global_hotkeys.toggled.connect(lambda v: self.ctx.set(v, "hotkeys", "global_hotkeys"))
        hk_layout.addRow("Global Hotkeys:", self.chk_global_hotkeys)

        layout.addWidget(hk_group)

        # Safety limits
        safe_group = QGroupBox("Safety Limits")
        safe_layout = QFormLayout(safe_group)

        self.spin_max_threads = QSpinBox()
        self.spin_max_threads.setRange(1, 16)
        self.spin_max_threads.setValue(self.ctx.get("safety_limits", "max_threads", default=8))
        self.spin_max_threads.valueChanged.connect(lambda v: self.ctx.set(v, "safety_limits", "max_threads"))
        safe_layout.addRow("Max Threads:", self.spin_max_threads)

        self.spin_queue_max = QSpinBox()
        self.spin_queue_max.setRange(8, 256)
        self.spin_queue_max.setValue(self.ctx.get("safety_limits", "input_bus_queue_max", default=32))
        self.spin_queue_max.valueChanged.connect(lambda v: self.ctx.set(v, "safety_limits", "input_bus_queue_max"))
        safe_layout.addRow("Input Queue Max:", self.spin_queue_max)

        self.spin_mem_interval = QSpinBox()
        self.spin_mem_interval.setRange(50, 1000)
        self.spin_mem_interval.setValue(self.ctx.get("safety_limits", "memory_read_interval_ms", default=150))
        self.spin_mem_interval.valueChanged.connect(lambda v: self.ctx.set(v, "safety_limits", "memory_read_interval_ms"))
        safe_layout.addRow("Memory Read Interval (ms):", self.spin_mem_interval)

        self.spin_min_input_interval = QSpinBox()
        self.spin_min_input_interval.setRange(5, 200)
        self.spin_min_input_interval.setValue(self.ctx.get("safety_limits", "min_interval_between_inputs_ms", default=25))
        self.spin_min_input_interval.valueChanged.connect(lambda v: self.ctx.set(v, "safety_limits", "min_interval_between_inputs_ms"))
        safe_layout.addRow("Min Input Interval (ms):", self.spin_min_input_interval)

        layout.addWidget(safe_group)

        # Runtime
        rt_group = QGroupBox("Runtime")
        rt_layout = QFormLayout(rt_group)

        self.combo_log_level = QComboBox()
        self.combo_log_level.addItems(["DEBUG", "INFO", "WARNING", "ERROR"])
        self.combo_log_level.setCurrentText(self.ctx.get("runtime", "log_level", default="INFO"))
        self.combo_log_level.currentTextChanged.connect(lambda v: self.ctx.set(v, "runtime", "log_level"))
        rt_layout.addRow("Log Level:", self.combo_log_level)

        self.chk_stop_death = QCheckBox()
        self.chk_stop_death.setChecked(self.ctx.get("runtime", "stop_on_death", default=True))
        self.chk_stop_death.toggled.connect(lambda v: self.ctx.set(v, "runtime", "stop_on_death"))
        rt_layout.addRow("Stop on Death:", self.chk_stop_death)

        self.chk_stop_disc = QCheckBox()
        self.chk_stop_disc.setChecked(self.ctx.get("runtime", "stop_on_disconnect", default=True))
        self.chk_stop_disc.toggled.connect(lambda v: self.ctx.set(v, "runtime", "stop_on_disconnect"))
        rt_layout.addRow("Stop on Disconnect:", self.chk_stop_disc)

        layout.addWidget(rt_group)

        # Save button
        btn_save = QPushButton("Salvar configurações")
        set_button_icon(btn_save, "save")
        btn_save.clicked.connect(self._save_config)
        layout.addWidget(btn_save)
        layout.addStretch()
        return w

    def _create_debug_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)

        # Scanner test
        scan_group = QGroupBox("Scanner Tests")
        scan_layout = QVBoxLayout(scan_group)

        btn_scan_sections = QPushButton("Run: scanner.py sections")
        btn_scan_sections.clicked.connect(lambda: self._run_scanner_test("sections"))
        scan_layout.addWidget(btn_scan_sections)

        # AOB origin: existing GUI debug literal; author/source not recorded.
        # ASSUMPTION — not validated in memory; scanner searches disk only.
        # Intended target: ASCII Player literal; not proof of a player pointer.
        btn_scan_aob = QPushButton('Run: scanner.py aob --section .rdata "50 6C 61 79 65 72"')
        btn_scan_aob.clicked.connect(lambda: self._run_scanner_test("aob"))
        scan_layout.addWidget(btn_scan_aob)

        btn_scan_aob_list = QPushButton("Run: scanner.py aob-list")
        btn_scan_aob_list.clicked.connect(lambda: self._run_scanner_test("aob-list"))
        scan_layout.addWidget(btn_scan_aob_list)

        btn_mem_check = QPushButton("Run: memory.py --check")
        btn_mem_check.clicked.connect(lambda: self._run_scanner_test("memory-check"))
        scan_layout.addWidget(btn_mem_check)

        layout.addWidget(scan_group)

        # Output
        out_group = QGroupBox("Output")
        out_layout = QVBoxLayout(out_group)
        self.debug_output = QTextEdit()
        self.debug_output.setReadOnly(True)
        self.debug_output.setFont(QFont("Consolas", 9))
        out_layout.addWidget(self.debug_output)
        layout.addWidget(out_group)

        return w

    def _setup_tray(self):
        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(icon("power"))
        self.tray.setToolTip("PokeBot")
        menu = QMenu()
        act_show = QAction("Show", self)
        act_show.triggered.connect(self.show)
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
        if self.engine:
            if self.engine.ctx.is_paused():
                self.engine.resume()
                self.btn_pause.setText("PAUSAR")
                set_button_icon(self.btn_pause, "pause")
            else:
                self.engine.pause()
                self.btn_pause.setText("RETOMAR")
                set_button_icon(self.btn_pause, "play")

    def _on_worker_finished(self):
        if hasattr(self, "_log_handler") and self._log_handler:
            try:
                logging.getLogger("pokebot").removeHandler(self._log_handler)
            except Exception:
                pass
            self._log_handler = None

        self.btn_start.setEnabled(self.resolver is not None)
        self.btn_start.setText("LIGAR BOT")
        self.btn_stop.setEnabled(False)
        self.btn_pause.setEnabled(False)
        self.btn_pause.setText("PAUSAR")
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
        # Dashboard
        running = status.get("running", False)
        self.lbl_dash_status.setText("RUNNING" if running else "STOPPED")
        self.lbl_dash_status.setStyleSheet(
            f"color: {DARK_PALETTE['green'] if running else DARK_PALETTE['red']}; font-size: 18px; font-weight: bold;"
        )

        # Modules
        mods = status.get("modules", {})
        active = sum(1 for v in mods.values() if v)
        total = len(mods)
        self.lbl_dash_modules.setText(f"{active}/{total} active")

        # Stats
        stats = status.get("stats", {})
        for key, label in self.stat_labels.items():
            label.setText(str(stats.get(key, 0)))

        # Navigator
        nav = status.get("navigator", {})
        self._refresh_waypoint_table()

    def _update_status_bar(self):
        connected = self.resolver is not None and self._memory_valid
        updating = self.resolver is not None and self._position_updating
        self.lbl_connection.setText("● Memória conectada" if connected else "● Atualizando posição..." if updating else "● Desconectado")
        color = DARK_PALETTE['green'] if connected else DARK_PALETTE['yellow'] if updating else DARK_PALETTE['red']
        self.lbl_connection.setStyleSheet(f"color: {color};")
        walker = self.tab_cavebot.walker
        running = self.worker is not None or (walker is not None and walker.is_running)
        self.btn_toggle_bot.setText("DESLIGAR BOT" if running else "LIGAR BOT")
        object_name = "stopBtn" if running else "startBtn"
        if self.btn_toggle_bot.objectName() != object_name:
            self.btn_toggle_bot.setObjectName(object_name)
            set_button_icon(self.btn_toggle_bot, "stop" if running else "play", size=26,
                            color="#ffffff" if running else "#052b1a")
            self.btn_toggle_bot.style().unpolish(self.btn_toggle_bot)
            self.btn_toggle_bot.style().polish(self.btn_toggle_bot)
        self.lbl_header_connection.setText(self.lbl_connection.text() + (" · " + self.lbl_dash_process.text() if connected else ""))
        self.lbl_header_connection.setStyleSheet(f"color: {color};")
        self.lbl_active_profile.setText(self.tab_cavebot.active_profile or "Hunt sem salvar")
        healer_enabled = self.ctx.get("modules", "healer", "enabled", default=False)
        if self.chk_healer_enabled.isChecked() != healer_enabled:
            blocker = QSignalBlocker(self.chk_healer_enabled)
            self.chk_healer_enabled.setChecked(healer_enabled)
            del blocker
        self.lbl_healer_state.setText("Configurado · ativo" if healer_enabled and running else "Configurado · bot desligado" if healer_enabled else "Desativado")
        self.lbl_loop_state.setText("Ativado" if self.tab_cavebot.chk_loop.isChecked() else "Desativado")
        self.btn_toggle_bot.setEnabled((connected or running) and not self._closing and self._pending_target is None)
        self.master_glow.setEnabled(self.btn_toggle_bot.isEnabled() and not running)
        self.lbl_dash_status.setStyleSheet(f"color: {DARK_PALETTE['green'] if running else DARK_PALETTE['fg_muted']}; font-size: 16px; font-weight: bold;")
        self.lbl_dash_status.setText("BOT LIGADO" if running else "BOT DESLIGADO — leitura ativa" if connected
                                     else "AGUARDANDO POSIÇÃO" if updating else "SEM CONEXÃO")
        self.btn_select_process.setEnabled(not self._closing and not running and not (walker and walker.recording)
                                           and (self.memory_thread is None or self.resolver is not None))
        self.combo_processes.setEnabled(self.btn_select_process.isEnabled())
        self.pokemon_healer_selector.btn_test_healer.setEnabled(connected and not self._closing)
        start_ts = self.ctx.stats.get("uptime_start")
        elapsed = max(0, int(time.time() - start_ts)) if running and start_ts else 0
        hours, remainder = divmod(elapsed, 3600)
        minutes, seconds = divmod(remainder, 60)
        text = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        self.lbl_uptime.setText(f"Uptime: {text}")
        self.lbl_dash_uptime.setText(text)

    def _run_scanner_test(self, test: str):
        import subprocess
        self.debug_output.clear()
        self.debug_output.append(f"$ Running {test}...")
        try:
            if test == "sections":
                cmd = ["python", "scanner.py", "sections"]
            elif test == "aob":
                # AOB origin: existing GUI debug literal; author/source not recorded.
                # ASSUMPTION — not validated in memory; scanner searches disk only.
                # Intended target: ASCII Player literal; not proof of a player pointer.
                cmd = ["python", "scanner.py", "aob", "--section", ".rdata", "50 6C 61 79 65 72"]
            elif test == "aob-list":
                cmd = ["python", "scanner.py", "aob-list"]
            elif test == "memory-check":
                cmd = ["python", "memory.py", "--check"]
            else:
                return
            result = subprocess.run(cmd, cwd="C:\\pokebot", capture_output=True, text=True, timeout=30)
            self.debug_output.append(result.stdout)
            if result.stderr:
                self.debug_output.append(f"STDERR:\n{result.stderr}")
            self.debug_output.append(f"Exit code: {result.returncode}")
        except subprocess.TimeoutExpired:
            self.debug_output.append("TIMEOUT")
        except Exception as exc:
            self.debug_output.append(f"ERROR: {exc}")

    def _log(self, level: str, msg: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        color = {
            "DEBUG": DARK_PALETTE["fg_muted"],
            "INFO": DARK_PALETTE["fg"],
            "WARNING": DARK_PALETTE["yellow"],
            "ERROR": DARK_PALETTE["red"],
        }.get(level, DARK_PALETTE["fg"])
        self.log_view.append(f'<span style="color:{color}">[{timestamp}] [{level}] {html.escape(msg)}</span>')
        self.debug_output.append(f"[{timestamp}] [{level}] {msg}")

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
