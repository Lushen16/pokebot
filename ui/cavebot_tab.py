from typing import Optional
import logging
import time
from PySide6.QtCore import Qt, QTimer, Signal, QSignalBlocker
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QAbstractItemView,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
from modules.walker import Walker, WalkerState
from core.profiles import ProfileManager
from gui.icons import ACTION_ICONS, icon, set_button_icon
from ui.combat_tab import CollapsibleSection

log = logging.getLogger("pokebot.ui.cavebot")


class CavebotTab(QWidget):
    route_start_requested = Signal()
    healer_profile_loaded = Signal(dict)

    def __init__(self, walker: Optional[Walker] = None, parent: Optional[QWidget] = None,
                 profile_manager=None, healer_provider=None) -> None:
        super().__init__(parent)
        self.walker = walker
        self.active_profile = ""
        self.profile_manager = profile_manager or ProfileManager()
        self.healer_provider = healer_provider or (lambda: {})
        self._destroy_stop_callback = None
        self._last_rendered_waypoints: list[dict] = []
        self._health_warning_key = None
        self._health_warning_since = None
        self._health_warning_logged = False
        self._setup_ui()
        self._apply_icons()
        self._setup_timers()
        if walker is not None:
            self.set_walker(walker)

    def _apply_icons(self):
        for button, name in ((self.btn_save_profile, "save"), (self.btn_load_profile, "load"),
                             (self.btn_delete_profile, "trash"), (self.btn_add_wp, "plus"),
                             (self.btn_add_current, "position"), (self.btn_record, "record"),
                             (self.btn_clear_wp, "trash"), (self.btn_delete_wp, "trash"),
                             (self.btn_start_route, "play"),
                             (self.btn_start_nearest, "position"), (self.btn_stop_route, "stop")):
            set_button_icon(button, name)

    def set_walker(self, walker: Walker) -> None:
        if self._destroy_stop_callback is not None:
            self.destroyed.disconnect(self._destroy_stop_callback)
        if self.walker is not None and self.walker is not walker:
            self.walker.request_stop()
        self.walker = walker
        self._destroy_stop_callback = walker.request_stop
        self.destroyed.connect(self._destroy_stop_callback)
        self.chk_loop.setChecked(self.walker.loop)
        self._sync_distance_guard()
        self.spin_interval.setValue(self.walker.record_sqm_interval)
        with QSignalBlocker(self.spin_arrival_tolerance):
            self.spin_arrival_tolerance.setValue(self.walker.arrival_tolerance)
        with QSignalBlocker(self.chk_dynamic_lure), \
                QSignalBlocker(self.spin_dynamic_max), \
                QSignalBlocker(self.spin_dynamic_resume), \
                QSignalBlocker(self.chk_dynamic_move), \
                QSignalBlocker(self.edit_dynamic_move):
            self.chk_dynamic_lure.setChecked(self.walker.dynamic_lure_enabled)
            self.spin_dynamic_max.setValue(self.walker.dynamic_lure_max_distance)
            self.spin_dynamic_resume.setMaximum(self.walker.dynamic_lure_max_distance - 1)
            self.spin_dynamic_resume.setValue(self.walker.dynamic_lure_resume_distance)
            self.chk_dynamic_move.setChecked(self.walker.dynamic_lure_use_move_command)
            self.edit_dynamic_move.setText(self.walker.dynamic_lure_move_command)
        self._sync_waypoint_list()

    def _setup_ui(self) -> None:
        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(12)
        heading = QLabel("Rota")
        heading.setObjectName("pageTitle")
        subtitle = QLabel("Grave, edite e execute a hunt atual. O controle da rota fica nesta aba.", self)
        subtitle.setObjectName("subtitle")
        subtitle.setWordWrap(True)
        main_layout.addWidget(heading)
        main_layout.addWidget(subtitle)
        profiles_group = QGroupBox("Perfil de Hunt")
        profiles_layout = QHBoxLayout(profiles_group)
        self.combo_profiles = QComboBox()
        self.combo_profiles.setMinimumWidth(180)
        self.btn_save_profile = QPushButton("Salvar Nova Hunt")
        self.btn_load_profile = QPushButton("Carregar Hunt")
        self.btn_save_profile.clicked.connect(self._on_save_profile)
        self.btn_load_profile.clicked.connect(self._on_load_profile)
        self.btn_delete_profile = QPushButton("Deletar Hunt")
        self.btn_delete_profile.clicked.connect(self._on_delete_profile)
        profiles_layout.addWidget(self.combo_profiles, 1)
        profiles_layout.addWidget(self.btn_save_profile)
        profiles_layout.addWidget(self.btn_load_profile)
        profiles_layout.addWidget(self.btn_delete_profile)
        main_layout.addWidget(profiles_group)
        self._refresh_profiles()
        recording_hint = QLabel("Grave um trajeto, salve a Hunt e inicie a rota. A gravação adiciona pontos à fila atual.")
        recording_hint.setObjectName("hint")
        recording_hint.setWordWrap(True)
        main_layout.addWidget(recording_hint)

        # 1. Painel de Debug de Posição em Tempo Real
        debug_group = QGroupBox("POSIÇÃO E ESTADO")
        debug_layout = QHBoxLayout(debug_group)
        self.lbl_position = QLabel("X: ---, Y: ---, Z: ---")
        self.lbl_position.setAlignment(Qt.AlignCenter)
        self.lbl_position.setStyleSheet(
            "font-size: 14px; font-weight: bold; color: #00E676; padding: 6px;"
        )
        debug_layout.addWidget(self.lbl_position)
        self.lbl_state = QLabel("PARADO")
        debug_layout.addWidget(self.lbl_state)
        main_layout.addWidget(debug_group)

        # 2. Entrada e Gravação de Waypoints
        self.advanced_section = CollapsibleSection("Edição manual e opções da rota", self)
        input_group = QGroupBox("Waypoints e gravação")
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
        self.spin_z.setToolTip("Informativo nas hunts normais. Verificado somente em Stair.")

        coords_row.addWidget(self.spin_x)
        coords_row.addWidget(self.spin_y)
        coords_row.addWidget(self.spin_z)
        input_layout.addLayout(coords_row)

        action_row = QHBoxLayout()
        self.combo_action = QComboBox()
        self.combo_action.addItems(["Walk", "Wait", "Say", "Safe Spot", "BOX", "Stair"])
        self.edit_param = QLineEdit()
        self.edit_param.setPlaceholderText("Wait: milissegundos; Say: mensagem")
        self.spin_interval = QSpinBox()
        self.spin_interval.setRange(1, 1000)
        self.spin_interval.setValue(self.walker.record_sqm_interval if self.walker else 3)
        self.spin_interval.setPrefix("Gravar a cada (SQM): ")
        self.combo_action.currentTextChanged.connect(self._on_record_options_changed)
        self.edit_param.textChanged.connect(self._on_record_options_changed)
        self.spin_interval.valueChanged.connect(self._on_record_options_changed)
        action_row.addWidget(self.combo_action)
        action_row.addWidget(self.edit_param)
        action_row.addWidget(self.spin_interval)
        input_layout.addLayout(action_row)

        tolerance_row = QHBoxLayout()
        tolerance_row.addWidget(QLabel("Tolerância de Chegada (SQM)"))
        self.spin_arrival_tolerance = QSpinBox(self)
        self.spin_arrival_tolerance.setRange(0, 2)
        self.spin_arrival_tolerance.setValue(self.walker.arrival_tolerance if self.walker else 1)
        self.spin_arrival_tolerance.valueChanged.connect(self._on_arrival_tolerance_changed)
        tolerance_row.addWidget(self.spin_arrival_tolerance)
        tolerance_row.addStretch()
        input_layout.addLayout(tolerance_row)
        tolerance_help = QLabel("0 = exato. 1+ = margem para Walk em rotas longas. "
                               "Boxes, escadas e demais ações sempre exigem chegada exata.")
        tolerance_help.setWordWrap(True)
        input_layout.addWidget(tolerance_help)

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
        self.advanced_section.content_layout.addWidget(input_group)

        # 3. Lista de Waypoints
        queue_group = QGroupBox("Fila de Waypoints")
        queue_layout = QVBoxLayout(queue_group)
        self.table_waypoints = QTableWidget(0, 6, self)
        self.table_waypoints.setMinimumHeight(190)
        self.table_waypoints.setHorizontalHeaderLabels(["#", "X", "Y", "Z", "Ação", "Parâmetro"])
        self.table_waypoints.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table_waypoints.verticalHeader().hide()
        self.table_waypoints.setAlternatingRowColors(True)
        self.table_waypoints.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table_waypoints.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table_waypoints.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table_waypoints.setAccessibleName("Waypoints da hunt")
        self.table_waypoints.setColumnHidden(3, True)
        self.chk_show_floor = QCheckBox("Mostrar andar (Z)", self)
        self.chk_show_floor.setToolTip("Z é preservado na Hunt. Caminhadas normais usam apenas X e Y.")
        self.chk_show_floor.toggled.connect(
            lambda checked: self.table_waypoints.setColumnHidden(3, not checked))
        queue_layout.addWidget(self.chk_show_floor)
        queue_layout.addWidget(self.table_waypoints)
        self.btn_delete_wp = QPushButton("Deletar Waypoint selecionado", self)
        self.btn_delete_wp.clicked.connect(self._on_delete_waypoint)
        queue_layout.addWidget(self.btn_delete_wp)
        main_layout.addWidget(queue_group, 1)

        # 4. Controles Iniciar / Parar e Loop
        control_group = QGroupBox("Controle da Rota")
        self.control_group = control_group
        control_outer = QVBoxLayout(control_group)
        control_layout = QHBoxLayout()

        self.chk_loop = QCheckBox("Repetir Hunt (Loop)")
        self.chk_loop.setChecked(True)
        self.chk_loop.toggled.connect(self._on_loop_toggled)

        self.btn_start_route = QPushButton("Iniciar Rota")
        self.btn_start_route.clicked.connect(self._on_start_route)

        self.btn_start_nearest = QPushButton("Iniciar no Mais Próximo")
        self.btn_start_nearest.clicked.connect(self._on_start_nearest)

        self.btn_stop_route = QPushButton("Parar Rota")
        self.btn_stop_route.setEnabled(False)
        self.btn_stop_route.clicked.connect(self._on_stop_route)

        control_layout.addWidget(self.btn_record)
        control_layout.addWidget(self.chk_loop)
        control_layout.addWidget(self.btn_start_route)
        control_layout.addWidget(self.btn_start_nearest)
        control_layout.addWidget(self.btn_stop_route)
        control_outer.addLayout(control_layout)

        dynamic_row = QHBoxLayout()
        distance_row = QHBoxLayout()
        self.chk_distance_guard = QCheckBox("Aguardar Pokémon distante")
        self.spin_distance_max = QSpinBox(self)
        self.spin_distance_max.setRange(1, 30)
        self.spin_distance_max.setPrefix("Aguardar a: ")
        self.spin_distance_max.setSuffix(" SQM")
        self.spin_distance_resume = QSpinBox(self)
        self.spin_distance_resume.setRange(0, 29)
        self.spin_distance_resume.setPrefix("Retomar a: ")
        self.spin_distance_resume.setSuffix(" SQM")
        distance_row.addWidget(self.chk_distance_guard)
        distance_row.addWidget(self.spin_distance_max)
        distance_row.addWidget(self.spin_distance_resume)
        distance_row.addStretch()
        control_outer.addLayout(distance_row)
        distance_help = QLabel("Só aguarda: não envia move nem busca o Pokémon. Exige posição confirmada; retomada menor que aguardar. Salvo na Hunt.")
        distance_help.setWordWrap(True)
        control_outer.addWidget(distance_help)
        self._sync_distance_guard()
        self.chk_distance_guard.toggled.connect(self._on_distance_guard_changed)
        self.spin_distance_max.valueChanged.connect(self._on_distance_guard_changed)
        self.spin_distance_resume.valueChanged.connect(self._on_distance_guard_changed)
        self.chk_dynamic_lure = QCheckBox("Lure dinâmico (acompanhar o Pokémon)")
        self.chk_dynamic_lure.setChecked(
            self.walker.dynamic_lure_enabled if self.walker is not None else False)
        self.chk_dynamic_lure.setToolTip(
            "Com o Pokémon longe durante a rota, o treinador para e vai buscá-lo; "
            "ao chegar perto, retoma o percurso.")
        self.spin_dynamic_max = QSpinBox(self)
        self.spin_dynamic_max.setRange(1, 30)
        self.spin_dynamic_max.setSuffix(" SQM")
        self.spin_dynamic_max.setPrefix("Distância máx.: ")
        self.spin_dynamic_max.setValue(
            self.walker.dynamic_lure_max_distance if self.walker is not None else 5)
        self.spin_dynamic_resume = QSpinBox(self)
        self.spin_dynamic_resume.setRange(0, 29)
        self.spin_dynamic_resume.setSuffix(" SQM")
        self.spin_dynamic_resume.setPrefix("Voltar a andar a: ")
        self.spin_dynamic_resume.setValue(
            self.walker.dynamic_lure_resume_distance if self.walker is not None else 3)
        dynamic_row.addWidget(self.chk_dynamic_lure)
        dynamic_row.addWidget(self.spin_dynamic_max)
        dynamic_row.addWidget(self.spin_dynamic_resume)
        dynamic_row.addStretch()
        control_outer.addLayout(dynamic_row)

        move_row = QHBoxLayout()
        self.chk_dynamic_move = QCheckBox("Comandar Pokémon com move")
        self.chk_dynamic_move.setChecked(
            self.walker.dynamic_lure_use_move_command if self.walker is not None else True)
        self.chk_dynamic_move.setToolTip(
            "Usa o comando de chat move para trazer o Pokémon até o treinador. "
            "Desmarcado, o treinador vai até o Pokémon.")
        self.edit_dynamic_move = QLineEdit(self)
        self.edit_dynamic_move.setText(
            self.walker.dynamic_lure_move_command if self.walker is not None else "{name}, move!")
        self.edit_dynamic_move.setPlaceholderText("{name}, move!")
        self.edit_dynamic_move.setToolTip(
            "Use {name} para o Pokémon ativo, e {x}, {y}, {z} para o tile do treinador. "
            "Ex.: {name}, move! ou {name}, move {x} {y} {z}!")
        move_row.addWidget(self.chk_dynamic_move)
        move_row.addWidget(self.edit_dynamic_move, 1)
        control_outer.addLayout(move_row)

        dynamic_help = QLabel(
            "O Pokémon é a fonte da distância (Chebyshev). Ao passar do limite, "
            "a rota para e o bot manda o comando move para trazer o Pokémon; "
            "ao chegar na distância de retomada, a rota volta a andar. "
            "Desligado, o Walker segue o percurso normalmente.")
        dynamic_help.setWordWrap(True)
        control_outer.addWidget(dynamic_help)

        self.chk_dynamic_lure.toggled.connect(self._on_dynamic_lure_changed)
        self.spin_dynamic_max.valueChanged.connect(self._on_dynamic_max_changed)
        self.spin_dynamic_resume.valueChanged.connect(self._on_dynamic_lure_changed)
        self.chk_dynamic_move.toggled.connect(self._on_dynamic_lure_changed)
        self.edit_dynamic_move.editingFinished.connect(self._on_dynamic_lure_changed)

        main_layout.insertWidget(5, control_group)
        main_layout.addWidget(self.advanced_section)

    def _refresh_profiles(self, selected: str = "") -> None:
        self.combo_profiles.clear()
        try:
            self.combo_profiles.addItems(self.profile_manager.list_profiles())
            if selected:
                self.combo_profiles.setCurrentText(selected)
        except (OSError, ValueError) as error:
            self.lbl_profile_error = str(error)
            self.combo_profiles.setToolTip(str(error))

    def _on_save_profile(self) -> None:
        if self.walker is None:
            QMessageBox.warning(self, "Sem conexão", "Selecione o processo no cabeçalho primeiro.")
            return
        name, accepted = QInputDialog.getText(self, "Salvar Hunt", "Nome do perfil:",
                                              text=self.combo_profiles.currentText())
        if not accepted:
            return
        try:
            if name.strip() in self.profile_manager.list_profiles():
                if QMessageBox.question(self, "Substituir Hunt", "Substituir o perfil existente?") != QMessageBox.Yes:
                    return
            data = self.walker.route_snapshot()
            data["config_healer"] = self.healer_provider()
            self.profile_manager.save_profile(name, data)
            self.active_profile = name.strip()
            self._refresh_profiles(name.strip())
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "Falha ao salvar Hunt", str(error))

    def _on_delete_profile(self) -> None:
        name = self.combo_profiles.currentText()
        if not name:
            return
        if self.walker is not None and (self.walker.is_running or self.walker.recording):
            QMessageBox.warning(self, "Hunt em uso", "Pare a rota e a gravação antes de deletar uma Hunt.")
            return
        if QMessageBox.question(self, "Deletar Hunt", f'Deletar "{name}"? A rota atual será preservada.') != QMessageBox.Yes:
            return
        try:
            self.profile_manager.delete_profile(name)
            if self.active_profile == name:
                self.active_profile = ""
            self._refresh_profiles()
        except (OSError, ValueError, KeyError) as error:
            QMessageBox.warning(self, "Falha ao deletar Hunt", str(error))

    def _on_load_profile(self) -> None:
        if self.walker is None:
            QMessageBox.warning(self, "Sem conexão", "Selecione o processo no cabeçalho primeiro.")
            return
        if self.walker.is_running or self.walker.recording:
            QMessageBox.warning(self, "Hunt em uso", "Pare a rota e a gravação antes de carregar uma Hunt.")
            return
        try:
            data = self.profile_manager.load_profile(self.combo_profiles.currentText())
            self.walker.replace_route(data["waypoints"], data["loop"], data["arrival_tolerance"],
                                      distance_guard=data.get("distance_guard"))
            self._sync_distance_guard()
            with QSignalBlocker(self.spin_arrival_tolerance):
                self.spin_arrival_tolerance.setValue(data["arrival_tolerance"])
            self.chk_loop.setChecked(data["loop"])
            self.active_profile = self.combo_profiles.currentText()
            self.healer_profile_loaded.emit(data["config_healer"])
            self._sync_waypoint_list()
        except (OSError, ValueError, KeyError) as error:
            QMessageBox.warning(self, "Falha ao carregar Hunt", str(error))

    def _setup_timers(self) -> None:
        self.debug_timer = QTimer(self)
        self.debug_timer.setInterval(100)
        self.debug_timer.timeout.connect(self._refresh_state)
        self.debug_timer.start()

    def _on_arrival_tolerance_changed(self, value: int) -> None:
        if self.walker is not None:
            self.walker.arrival_tolerance = value

    def _sync_distance_guard(self) -> None:
        enabled = self.walker.distance_guard_enabled if self.walker is not None else False
        maximum = self.walker.distance_guard_max if self.walker is not None else 5
        resume = self.walker.distance_guard_resume if self.walker is not None else 3
        with QSignalBlocker(self.chk_distance_guard), QSignalBlocker(self.spin_distance_max), QSignalBlocker(self.spin_distance_resume):
            self.chk_distance_guard.setChecked(enabled)
            self.spin_distance_max.setValue(maximum)
            self.spin_distance_resume.setMaximum(maximum - 1)
            self.spin_distance_resume.setValue(resume)

    def _on_distance_guard_changed(self, *_args) -> None:
        maximum = self.spin_distance_max.value()
        with QSignalBlocker(self.spin_distance_resume):
            self.spin_distance_resume.setMaximum(maximum - 1)
        if self.walker is not None:
            self.walker.configure_distance_guard(
                enabled=self.chk_distance_guard.isChecked(),
                max_distance=maximum, resume_distance=self.spin_distance_resume.value())

    def _on_dynamic_max_changed(self, value: int) -> None:
        with QSignalBlocker(self.spin_dynamic_resume):
            self.spin_dynamic_resume.setMaximum(max(0, int(value) - 1))
        self._on_dynamic_lure_changed()

    def _on_dynamic_lure_changed(self, *_args) -> None:
        if self.walker is None:
            return
        max_distance = max(1, int(self.spin_dynamic_max.value()))
        resume = min(int(self.spin_dynamic_resume.value()), max_distance - 1)
        if resume != self.spin_dynamic_resume.value():
            with QSignalBlocker(self.spin_dynamic_resume):
                self.spin_dynamic_resume.setValue(resume)
        self.walker.configure_dynamic_lure(
            enabled=self.chk_dynamic_lure.isChecked(),
            max_distance=max_distance,
            resume_distance=resume,
            use_move_command=self.chk_dynamic_move.isChecked(),
            move_command=self.edit_dynamic_move.text().strip() or "{name}, move!",
        )

    def _on_loop_toggled(self, checked: bool) -> None:
        if self.walker is not None:
            self.walker.set_loop(checked)

    def _current_position(self):
        position = self.walker.read_player_position() if self.walker is not None else None
        if position is None or not Walker.is_valid_position(*position) or position[:2] == (0, 0):
            return None
        return position

    def _on_delete_waypoint(self) -> None:
        if self.walker is None:
            return
        selected = self.table_waypoints.selectionModel().selectedRows()
        if not selected:
            return
        try:
            self.walker.remove_waypoint(selected[0].row())
        except (RuntimeError, IndexError) as error:
            QMessageBox.warning(self, "Excluir waypoint", str(error))
            return
        self._sync_waypoint_list()

    def _on_add_waypoint(self) -> None:
        if self.walker is not None and self._configure_recording():
            position = self._current_position()
            self.spin_z.setValue(position[2] if position is not None else 7)
            try:
                self.walker.add_waypoint(
                    self.spin_x.value(),
                    self.spin_y.value(),
                    self.spin_z.value(),
                    self.combo_action.currentText(),
                    self.edit_param.text(),
                )
            except ValueError as error:
                QMessageBox.warning(self, "Waypoint inválido", str(error))
                return
            self._sync_waypoint_list()

    def _on_add_current_position(self) -> None:
        if self.walker is not None and self._configure_recording():
            pos = self._current_position()
            self.spin_z.setValue(pos[2] if pos is not None else 7)
            if pos is not None:
                x, y, z = pos
                self.spin_x.setValue(x)
                self.spin_y.setValue(y)
                self.spin_z.setValue(z)
                self.walker.add_waypoint(x, y, z, self.combo_action.currentText(), self.edit_param.text())
                self._sync_waypoint_list()

    def _on_toggle_record(self) -> None:
        if self.walker is None:
            return

        if not self.walker.recording:
            if not self._configure_recording():
                return
            # Se a rota estava andando, para antes de começar a gravar
            if self.walker.is_running:
                self._on_stop_route()
            self.walker.start_recording(clear_existing=False)
            self.btn_record.setText("Parar Gravação")
            set_button_icon(self.btn_record, "stop", color="#ffffff")
            self.btn_record.setStyleSheet(
                "font-weight: bold; background-color: #D32F2F; color: white;"
            )
            self.btn_start_route.setEnabled(False)
            self.btn_start_nearest.setEnabled(False)
        else:
            self.walker.stop_recording()
            self.btn_record.setText("Gravar Hunt")
            set_button_icon(self.btn_record, "record")
            self.btn_record.setStyleSheet("font-weight: bold;")
            self.btn_start_route.setEnabled(True)
            self.btn_start_nearest.setEnabled(True)

        self._sync_waypoint_list()

    def _on_clear_waypoints(self) -> None:
        if self.walker is not None:
            self.walker.clear_waypoints()
            self._sync_waypoint_list()

    def _on_start_route(self) -> None:
        if self.walker is not None:
            wps = self.walker.waypoint_snapshot()
            if not wps:
                log.warning("[CAVEBOT GUI] Tentativa de iniciar rota sem waypoints cadastrados.")
                self.lbl_state.setText("SEM WAYPOINTS")
                return
            if self.walker.recording:
                self.walker.stop_recording()
                self.btn_record.setText("Gravar Hunt")
                set_button_icon(self.btn_record, "record")
                self.btn_record.setStyleSheet("font-weight: bold;")

            self.walker.set_loop(self.chk_loop.isChecked())
            log.info("[CAVEBOT GUI] Iniciando rota com %d waypoints. Loop: %s. Tolerância: %d. Waypoints: %s",
                     len(wps), self.chk_loop.isChecked(), self.walker.arrival_tolerance, wps)
            log.info("Teclas direcionais da rota (VK): %s", self.walker.movement_keys)
            self.route_start_requested.emit()
            self.walker.start()
            self.btn_start_route.setEnabled(False)
            self.btn_start_nearest.setEnabled(False)
            self.btn_stop_route.setEnabled(True)
            self.btn_record.setEnabled(False)

    def _on_start_nearest(self) -> None:
        if self.walker is not None and self.walker.start_nearest():
            self._sync_waypoint_list()
            self._on_start_route()

    def _configure_recording(self, show_error: bool = True) -> bool:
        if self.walker is None:
            return False
        try:
            self.walker.configure_recording(self.combo_action.currentText(), self.edit_param.text(),
                                            self.spin_interval.value())
        except ValueError as error:
            if show_error:
                QMessageBox.warning(self, "Parâmetro inválido", str(error))
            return False
        return True

    def _on_record_options_changed(self, *_args) -> None:
        self._configure_recording(show_error=False)

    def _on_stop_route(self) -> None:
        if self.walker is not None:
            log.info("[CAVEBOT GUI] Parando rota.")
            self.walker.request_stop()
            stopped = not self.walker.is_running
            self.btn_start_route.setEnabled(stopped)
            self.btn_start_nearest.setEnabled(stopped)
            self.btn_stop_route.setEnabled(False)
            self.btn_record.setEnabled(stopped)
            self.lbl_state.setText("PARANDO" if not stopped else "PARADO")

    def _sync_waypoint_list(self) -> None:
        self._last_rendered_waypoints = self.walker.waypoint_snapshot() if self.walker is not None else []
        selected = self.table_waypoints.currentRow()
        scroll = self.table_waypoints.verticalScrollBar().value()
        self.table_waypoints.setRowCount(len(self._last_rendered_waypoints))
        for index, waypoint in enumerate(self._last_rendered_waypoints):
            values = (index + 1, waypoint["x"], waypoint["y"], waypoint["z"],
                      waypoint.get("action", "Walk"), waypoint.get("param", ""))
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 0:
                    item.setIcon(icon(ACTION_ICONS.get(waypoint.get("action", "Walk"), "walk")))
                    item.setToolTip(waypoint.get("action", "Walk"))
                self.table_waypoints.setItem(index, column, item)
        if 0 <= selected < self.table_waypoints.rowCount():
            self.table_waypoints.selectRow(selected)
        self.table_waypoints.verticalScrollBar().setValue(scroll)

    def _update_health_warning(self, position) -> None:
        walker = self.walker
        reason = None
        if (walker is not None and walker.is_running and not walker.recording
                and walker.waypoint_snapshot() and not walker.pause_reasons):
            if position is None:
                reason = "memory"
            elif walker.state == WalkerState.IDLE:
                reason = "idle"
            elif walker.state == WalkerState.WAITING and time.monotonic() >= walker.wait_until:
                reason = "wait"
        key = (id(walker), reason) if reason else None
        now = time.monotonic()
        if key != self._health_warning_key:
            self._health_warning_key = key
            self._health_warning_since = now if key else None
            self._health_warning_logged = False
        self.lbl_state.setStyleSheet("")
        if key is None or now - self._health_warning_since < 5.0:
            return
        message = ("Walker parado, aguardando leitura de memória..." if reason == "memory"
                   else "Walker parado, verifique o waypoint e o andar de destino...")
        self.lbl_state.setText("AGUARDANDO MEMÓRIA" if reason == "memory" else "ROTA SEM PROGRESSO")
        self.lbl_state.setStyleSheet("color: #FFAB00; font-weight: bold;")
        if not self._health_warning_logged:
            log.warning(message)
            self._health_warning_logged = True

    def _refresh_state(self) -> None:
        if self.walker is None:
            self.lbl_state.setText("SEM CONEXÃO")
        elif self.walker.recording:
            self.lbl_state.setText("GRAVANDO")
        elif self.walker.stop_event.is_set() and self.walker.is_running:
            self.lbl_state.setText("PARANDO")
        else:
            self.lbl_state.setText({WalkerState.IDLE: "PARADO", WalkerState.MOVING: "MOVENDO",
                                    WalkerState.WAITING: "AGUARDANDO"}[self.walker.state])
        if self.walker is not None and self.walker.recording:
            self.walker.record_step_if_moved()

        self.btn_delete_wp.setEnabled(self.walker is not None
                                      and not self.walker.is_running and not self.walker.recording)
        pos = self._current_position()
        self._update_health_warning(pos)
        if self.walker is not None and not self.walker.recording and self.walker.pause_reasons:
            labels = {"user": "PAUSA MANUAL", "combat": "COMBATE",
                      "distance": "AGUARDANDO POKÉMON",
                      "distance_unavailable": "POSIÇÃO DO POKÉMON INDISPONÍVEL",
                      "watchdog": "SEM PROGRESSO: PARE E REINICIE A ROTA"}
            self.lbl_state.setText(" / ".join(labels[reason] for reason in sorted(self.walker.pause_reasons)))
            self.lbl_state.setStyleSheet("color: #FFAB00; font-weight: bold;")
        if self.walker is not None and self.walker.recording and pos is None:
            self.lbl_state.setText("GRAVAÇÃO: AGUARDANDO POSIÇÃO")
            self.lbl_state.setStyleSheet("color: #FFAB00; font-weight: bold;")
        if pos is None:
            self.lbl_position.setText("X: --, Y: --, Z: -- (Atualizando...)")
            self.lbl_position.setStyleSheet(
                "font-size: 14px; font-weight: bold; color: #FFAB00; padding: 6px;"
            )
        else:
            x, y, z = pos
            status_tag = " [GRAVANDO]" if (self.walker and self.walker.recording) else ""
            self.lbl_position.setText(f"X: {x}, Y: {y}, Z: {z}{status_tag}")
            self.lbl_position.setStyleSheet(
                "font-size: 14px; font-weight: bold; color: #00E676; padding: 6px;"
            )

        if self.walker is not None:
            if self.walker.waypoint_snapshot() != self._last_rendered_waypoints:
                self._sync_waypoint_list()
            if not self.walker.is_running and not self.walker.recording:
                self.btn_start_route.setEnabled(True)
                self.btn_start_nearest.setEnabled(True)
                self.btn_stop_route.setEnabled(False)
                self.btn_record.setEnabled(True)

    def closeEvent(self, event) -> None:
        self.debug_timer.stop()
        if self.walker is not None:
            self.walker.request_stop()
        super().closeEvent(event)

    def showEvent(self, event) -> None:
        self.debug_timer.start()
        super().showEvent(event)
