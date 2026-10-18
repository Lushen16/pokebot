from typing import Optional
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
from modules.walker import Walker


class CavebotTab(QWidget):
    def __init__(self, walker: Optional[Walker] = None, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.walker = walker
        self._last_rendered_waypoints: list[dict[str, int]] = []
        self._setup_ui()
        self._setup_timers()

    def set_walker(self, walker: Walker) -> None:
        self.walker = walker
        self.chk_loop.setChecked(self.walker.loop)
        self._sync_waypoint_list()

    def _setup_ui(self) -> None:
        main_layout = QVBoxLayout(self)

        # 1. Painel de Debug de Posição em Tempo Real
        debug_group = QGroupBox("Debug — Posição Atual em Tempo Real")
        debug_layout = QHBoxLayout(debug_group)
        self.lbl_position = QLabel("X: ---, Y: ---, Z: ---")
        self.lbl_position.setAlignment(Qt.AlignCenter)
        self.lbl_position.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: #00E676; padding: 6px;"
        )
        debug_layout.addWidget(self.lbl_position)
        main_layout.addWidget(debug_group)

        # 2. Entrada e Gravação de Waypoints
        input_group = QGroupBox("Gerenciar / Gravar Waypoints")
        input_layout = QVBoxLayout(input_group)

        coords_row = QHBoxLayout()
        self.spin_x = QSpinBox()
        self.spin_x.setRange(-100000, 100000)
        self.spin_x.setPrefix("X: ")

        self.spin_y = QSpinBox()
        self.spin_y.setRange(-100000, 100000)
        self.spin_y.setPrefix("Y: ")

        self.spin_z = QSpinBox()
        self.spin_z.setRange(0, 15)
        self.spin_z.setValue(7)
        self.spin_z.setPrefix("Z: ")

        coords_row.addWidget(self.spin_x)
        coords_row.addWidget(self.spin_y)
        coords_row.addWidget(self.spin_z)
        input_layout.addLayout(coords_row)

        buttons_row = QHBoxLayout()
        self.btn_add_wp = QPushButton("Adicionar Waypoint")
        self.btn_add_wp.clicked.connect(self._on_add_waypoint)

        self.btn_add_current = QPushButton("Usar Posição Atual")
        self.btn_add_current.clicked.connect(self._on_add_current_position)

        self.btn_record = QPushButton("Gravar Hunt")
        self.btn_record.setStyleSheet("font-weight: bold;")
        self.btn_record.clicked.connect(self._on_toggle_record)

        self.btn_clear_wp = QPushButton("Limpar Fila")
        self.btn_clear_wp.clicked.connect(self._on_clear_waypoints)

        buttons_row.addWidget(self.btn_add_wp)
        buttons_row.addWidget(self.btn_add_current)
        buttons_row.addWidget(self.btn_record)
        buttons_row.addWidget(self.btn_clear_wp)
        input_layout.addLayout(buttons_row)
        main_layout.addWidget(input_group)

        # 3. Lista de Waypoints
        queue_group = QGroupBox("Fila de Waypoints")
        queue_layout = QVBoxLayout(queue_group)
        self.list_waypoints = QListWidget()
        queue_layout.addWidget(self.list_waypoints)
        main_layout.addWidget(queue_group)

        # 4. Controles Iniciar / Parar e Loop
        control_group = QGroupBox("Controle da Rota")
        control_layout = QHBoxLayout(control_group)

        self.chk_loop = QCheckBox("Repetir Hunt (Loop)")
        self.chk_loop.setChecked(True)
        self.chk_loop.toggled.connect(self._on_loop_toggled)

        self.btn_start_route = QPushButton("Iniciar Rota")
        self.btn_start_route.clicked.connect(self._on_start_route)

        self.btn_stop_route = QPushButton("Parar Rota")
        self.btn_stop_route.setEnabled(False)
        self.btn_stop_route.clicked.connect(self._on_stop_route)

        control_layout.addWidget(self.chk_loop)
        control_layout.addWidget(self.btn_start_route)
        control_layout.addWidget(self.btn_stop_route)
        main_layout.addWidget(control_group)

    def _setup_timers(self) -> None:
        self.debug_timer = QTimer(self)
        self.debug_timer.setInterval(100)
        self.debug_timer.timeout.connect(self._refresh_state)
        self.debug_timer.start()

    def _on_loop_toggled(self, checked: bool) -> None:
        if self.walker is not None:
            self.walker.set_loop(checked)

    def _on_add_waypoint(self) -> None:
        if self.walker is not None:
            self.walker.add_waypoint(
                self.spin_x.value(),
                self.spin_y.value(),
                self.spin_z.value(),
            )
            self._sync_waypoint_list()

    def _on_add_current_position(self) -> None:
        if self.walker is not None:
            pos = self.walker.read_player_position()
            if pos is not None:
                x, y, z = pos
                self.spin_x.setValue(x)
                self.spin_y.setValue(y)
                self.spin_z.setValue(z)
                self.walker.add_waypoint(x, y, z)
                self._sync_waypoint_list()

    def _on_toggle_record(self) -> None:
        if self.walker is None:
            return

        if not self.walker.recording:
            # Se a rota estava andando, para antes de começar a gravar
            if getattr(self.walker, "running", False):
                self._on_stop_route()
            self.walker.start_recording(clear_existing=False)
            self.btn_record.setText("Parar Gravação")
            self.btn_record.setStyleSheet(
                "font-weight: bold; background-color: #D32F2F; color: white;"
            )
            self.btn_start_route.setEnabled(False)
        else:
            self.walker.stop_recording()
            self.btn_record.setText("Gravar Hunt")
            self.btn_record.setStyleSheet("font-weight: bold;")
            self.btn_start_route.setEnabled(True)

        self._sync_waypoint_list()

    def _on_clear_waypoints(self) -> None:
        if self.walker is not None:
            self.walker.clear_waypoints()
            self._sync_waypoint_list()

    def _on_start_route(self) -> None:
        if self.walker is not None:
            if self.walker.recording:
                self.walker.stop_recording()
                self.btn_record.setText("Gravar Hunt")
                self.btn_record.setStyleSheet("font-weight: bold;")

            self.walker.set_loop(self.chk_loop.isChecked())
            self.walker.start()
            self.btn_start_route.setEnabled(False)
            self.btn_stop_route.setEnabled(True)
            self.btn_record.setEnabled(False)

    def _on_stop_route(self) -> None:
        if self.walker is not None:
            self.walker.stop()
            self.btn_start_route.setEnabled(True)
            self.btn_stop_route.setEnabled(False)
            self.btn_record.setEnabled(True)

    def _sync_waypoint_list(self) -> None:
        self.list_waypoints.clear()
        if self.walker is not None:
            self._last_rendered_waypoints = [dict(wp) for wp in self.walker.waypoints]
            for idx, wp in enumerate(self.walker.waypoints):
                prefix = "-> " if idx == 0 else "   "
                self.list_waypoints.addItem(
                    f"{prefix}#{idx + 1} | X: {wp['x']}, Y: {wp['y']}, Z: {wp['z']}"
                )
        else:
            self._last_rendered_waypoints = []

    def _refresh_state(self) -> None:
        if self.walker is not None and self.walker.recording:
            self.walker.record_step_if_moved()

        pos = self.walker.read_player_position() if self.walker is not None else None
        if pos is None:
            self.lbl_position.setText("X: ---, Y: ---, Z: --- (Leitura Falhou)")
            self.lbl_position.setStyleSheet(
                "font-size: 14px; font-weight: bold; color: #FF5252; padding: 6px;"
            )
        else:
            x, y, z = pos
            status_tag = " [GRAVANDO]" if (self.walker and self.walker.recording) else ""
            self.lbl_position.setText(f"X: {x}, Y: {y}, Z: {z}{status_tag}")
            self.lbl_position.setStyleSheet(
                "font-size: 14px; font-weight: bold; color: #00E676; padding: 6px;"
            )

        if self.walker is not None and self.walker.waypoints != self._last_rendered_waypoints:
            self._sync_waypoint_list()
