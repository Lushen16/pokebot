from __future__ import annotations

import threading
from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QFormLayout, QGroupBox, QLabel, QProgressBar, QFrame, QHBoxLayout, QVBoxLayout
from core.position import is_valid_position
from gui.icons import icon


class SetupStep(QFrame):
    def __init__(self, number, title, caption, icon_name, parent=None):
        super().__init__(parent)
        self.setObjectName("setupStep")
        layout = QHBoxLayout(self)
        badge = QLabel(number, self)
        badge.setObjectName("stepBadge")
        symbol = QLabel(self)
        symbol.setPixmap(icon(icon_name).pixmap(24, 24))
        labels = QVBoxLayout()
        heading = QLabel(title, self)
        heading.setObjectName("stepTitle")
        description = QLabel(caption, self)
        description.setObjectName("subtitle")
        description.setWordWrap(True)
        labels.addWidget(heading)
        labels.addWidget(description)
        layout.addWidget(badge)
        layout.addWidget(symbol)
        layout.addLayout(labels, 1)


class LiveStatusPanel(QGroupBox):
    def __init__(self, parent=None):
        super().__init__("PERSONAGEM · AO VIVO", parent)
        layout = QFormLayout(self)
        self.connection = QLabel("SEM CONEXÃO")
        self.name = QLabel("Não calibrado")
        self.hp = QLabel("—")
        self.name.setObjectName("playerName")
        self.hp.setObjectName("statValue")
        self.hp_bar = QProgressBar(self)
        self.hp_bar.setRange(0, 100)
        self.hp_bar.setValue(0)
        self.hp_bar.setTextVisible(False)
        self.hp_percent = QLabel("—", self)
        self.hp_bar.setProperty("health", "unknown")
        self.hp_bar.setFormat("Sem leitura")
        self.hp_bar.setAccessibleName("Vida do personagem")
        self.position = QLabel("X: —  Y: —  Z: —")
        layout.addRow("Conexão:", self.connection)
        layout.addRow("Personagem:", self.name)
        layout.addRow("HP:", self.hp)
        bar_row = QHBoxLayout()
        bar_row.addWidget(self.hp_bar, 1)
        bar_row.addWidget(self.hp_percent)
        layout.addRow(bar_row)
        layout.addRow("Posição:", self.position)

    def show_snapshot(self, snapshot):
        position = snapshot.get("position")
        position_valid = (isinstance(position, (tuple, list)) and len(position) == 3
                          and is_valid_position(*position))
        valid = snapshot.get("valid", False) and position_valid
        updating = not position_valid and snapshot.get("hp_valid", False)
        self.connection.setText("MEMÓRIA CONECTADA" if valid else "ATUALIZANDO POSIÇÃO…" if updating else "LEITURA INDISPONÍVEL")
        color = "#00c853" if valid else "#ffab00" if updating else "#ff5252"
        self.setStyleSheet("QGroupBox { border: 1px solid " + color + "; }")
        self.name.setText(str(snapshot.get("name") or "Não calibrado"))
        hp, maximum = snapshot.get("hp"), snapshot.get("hp_max")
        self.hp.setText(f"{hp} / {maximum}" if hp is not None and maximum is not None else "—")
        hp_valid = (type(hp) is int and type(maximum) is int and maximum > 0 and 0 <= hp <= maximum)
        self.hp_bar.setValue(round(100 * hp / maximum) if hp_valid else 0)
        self.hp_bar.setEnabled(hp_valid)
        fraction = hp / maximum if hp_valid else None
        health = "unknown" if fraction is None else "critical" if fraction < 0.2 else "warning" if fraction <= 0.6 else "good"
        if self.hp_bar.property("health") != health:
            self.hp_bar.setProperty("health", health)
            self.hp_bar.style().unpolish(self.hp_bar)
            self.hp_bar.style().polish(self.hp_bar)
        self.hp_bar.setFormat("%p%" if hp_valid else "Sem leitura")
        self.hp_percent.setText(f"{round(100 * fraction)}%" if hp_valid else "—")
        self.position.setText(f"X: {position[0]}  Y: {position[1]}  Z: {position[2]}" if position_valid
                              else "X: --, Y: --, Z: -- (Atualizando...)")
        self.connection.setToolTip(snapshot.get("error", ""))


class MemoryMonitor(QThread):
    connected = Signal(object)
    snapshot_ready = Signal(dict)
    failed = Signal(str)

    def __init__(self, target, config_path, parent=None, connector=None):
        super().__init__(parent)
        self.target = target
        self.config_path = config_path
        self.resolver = None
        self._stop_event = threading.Event()
        self._connector = connector

    def stop(self):
        self._stop_event.set()

    def read_snapshot(self):
        resolver = self.resolver
        values = {}
        errors = []
        for field in ("hp", "hp_max", "x", "y", "z"):
            try:
                values[field] = resolver.read_entry("player." + field)
            except Exception as error:
                values[field] = None
                errors.append(f"player.{field}: {error}")
                continue
            if values[field] is None:
                errors.append(f"player.{field}: {resolver.last_error or 'leitura indisponível'}")
        position = tuple(values[axis] for axis in "xyz")
        position_valid = is_valid_position(*position)
        if not position_valid:
            errors.append(f"Posição inválida: X={position[0]} Y={position[1]} Z={position[2]}")
        hp, maximum = values["hp"], values["hp_max"]
        hp_valid = hp is not None and maximum is not None and maximum > 0 and 0 <= hp <= maximum
        if not hp_valid:
            errors.append(f"HP inválido: {hp}/{maximum}")
        return {"hp": hp, "hp_max": maximum, "position": position if position_valid else None,
                "hp_valid": hp_valid, "position_valid": position_valid,
                "name": None, "valid": position_valid and hp_valid, "error": "; ".join(errors)}

    def run(self):
        try:
            from memory import connect_live_resolver
            connector = self._connector or connect_live_resolver
            self.resolver = connector(self.target, config_path=self.config_path)
            self.connected.emit(self.resolver)
            while not self._stop_event.is_set():
                try:
                    self.snapshot_ready.emit(self.read_snapshot())
                except Exception as error:
                    self.snapshot_ready.emit({"valid": False, "error": str(error)})
                self._stop_event.wait(0.1)
        except Exception as error:
            self.failed.emit(str(error))
