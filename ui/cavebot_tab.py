from typing import Optional
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
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
    """Aba Cavebot conectada à instância do Walker controlada pelo Engine."""

    def __init__(self, walker: Optional[Walker] = None, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.walker = walker
        self._setup_ui()
        self._setup_timers()

    def set_walker(self, walker: Walker) -> None:
        self.walker = walker
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

        # 2. Entrada de Waypoints
        input_group = QGroupBox("Adicionar Waypoint")
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

        self.btn_clear_wp = QPushButton("Limpar Fila")
        self.btn_clear_wp.clicked.connect(self._on_clear_waypoints)

        buttons_row.addWidget(self.btn_add_wp)
        buttons_row.addWidget(self.btn_add_current)
        buttons_row.addWidget(self.btn_clear_wp)
        input_layout.addLayout(buttons_row)

        main_layout.addWidget(input_group)

        # 3. Lista de Waypoints na Fila
        queue_group = QGroupBox("Fila de Waypoints")
        queue_layout = QVBoxLayout(queue_group)
        self.list_waypoints = QListWidget()
        queue_layout.addWidget(self.list_waypoints)
        main_layout.addWidget(queue_group)

        # 4. Controles Iniciar / Parar Rota
        control_group = QGroupBox("Controle da Rota")
        control_layout = QHBoxLayout(control_group)

        self.btn_start_route = QPushButton("Iniciar Rota")
        self.btn_start_route.clicked.connect(self._on_start_route)

        self.btn_stop_route = QPushButton("Parar Rota")
        self.btn_stop_route.setEnabled(False)
        self.btn_stop_route.clicked.connect(self._on_stop_route)

        control_layout.addWidget(self.btn_start_route)
        control_layout.addWidget(self.btn_stop_route)
        main_layout.addWidget(control_group)

    def _setup_timers(self) -> None:
        self.debug_timer = QTimer(self)
        self.debug_timer.setInterval(100)
        self.debug_timer.timeout.connect(self._refresh_state)
        self.debug_timer.start()

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

    def _on_clear_waypoints(self) -> None:
        if self.walker is not None:
            self.walker.clear_waypoints()
            self._sync_waypoint_list()

    def _on_start_route(self) -> None:
        if self.walker is not None:
            self.walker.start()
            self.btn_start_route.setEnabled(False)
            self.btn_stop_route.setEnabled(True)

    def _on_stop_route(self) -> None:
        if self.walker is not None:
            self.walker.stop()
            self.btn_start_route.setEnabled(True)
            self.btn_stop_route.setEnabled(False)

    def _sync_waypoint_list(self) -> None:
        self.list_waypoints.clear()
        if self.walker is not None:
            for idx, wp in enumerate(self.walker.waypoints):
                self.list_waypoints.addItem(
                    f"#{idx + 1} -> X: {wp['x']}, Y: {wp['y']}, Z: {wp['z']}"
                )

    def _refresh_state(self) -> None:
        if self.walker is not None:
            pos = self.walker.read_player_position()
        else:
            pos = None

        if pos is None:
            self.lbl_position.setText("X: ---, Y: ---, Z: --- (Leitura Falhou)")
            self.lbl_position.setStyleSheet(
                "font-size: 14px; font-weight: bold; color: #FF5252; padding: 6px;"
            )
        else:
            x, y, z = pos
            self.lbl_position.setText(f"X: {x}, Y: {y}, Z: {z}")
            self.lbl_position.setStyleSheet(
                "font-size: 14px; font-weight: bold; color: #00E676; padding: 6px;"
            )

        if self.walker is not None and self.list_waypoints.count() != len(self.walker.waypoints):
            self._sync_waypoint_list()
