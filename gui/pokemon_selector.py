"""Componente unificado de seleção de cliente e Pokémons para o Healer."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Optional

from PySide6.QtCore import QSignalBlocker, QTimer
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from core.memory import MemoryReader
from core.offsets import load_offsets
from core.party_roster import RosterMember, RosterSnapshot, unavailable_roster
from core.roster_detector import RosterDetector

log = logging.getLogger("pokebot.gui.pokemon_selector")


def empty_roster_snapshot() -> RosterSnapshot:
    return RosterSnapshot(
        pid=None,
        members=tuple(RosterMember(slot=slot, name=None) for slot in range(1, 7)),
        detected=False,
        reason="Nenhum cliente selecionado.",
    )


class PokemonHealerSelector(QWidget):
    """Formulário enxuto e direto para configurar o Healer com base no Roster."""

    def __init__(self, ctx: Any, parent: Optional[QWidget] = None, *,
                 roster_provider: Optional[Any] = None,
                 process_provider: Optional[Callable[[], list[int]]] = None,
                 pid_provider: Optional[Callable[[], Optional[int]]] = None,
                 reader: Optional[MemoryReader] = None,
                 resolver_provider: Optional[Callable[[], Optional[object]]] = None,
                 test_healer_callback: Optional[Callable[[], None]] = None,
                 **kwargs):
        super().__init__(parent)
        self.ctx = ctx
        self.roster_provider = roster_provider
        self.process_provider = process_provider
        self.pid_provider = pid_provider
        self.reader = reader
        self.resolver_provider = resolver_provider
        self.test_healer_callback = test_healer_callback
        self._roster_snapshot = empty_roster_snapshot()
        self._known_pids = []

        layout = QFormLayout(self)
        layout.setContentsMargins(12, 14, 12, 14)
        layout.setSpacing(10)

        # 0. Seletor de Cliente e Botao Detectar
        self.client_combo = QComboBox()
        self.btn_select_client = QPushButton("Selecionar cliente...")
        self.btn_select_client.clicked.connect(self._open_process_dialog)
        self.detect_button = QPushButton("🔄 Detectar Mochila")
        self.detect_button.clicked.connect(self.detect_roster)

        client_box = QHBoxLayout()
        client_box.addWidget(self.client_combo)
        client_box.addWidget(self.btn_select_client)
        client_box.addWidget(self.detect_button)
        layout.addRow("Cliente:", client_box)

        # 1. Dropdown "Pokemon de Cura"
        self.pokemon_combo = QComboBox()
        layout.addRow("Pokémon de Cura:", self.pokemon_combo)

        # 2. Dropdown "Skill de Cura" (m1..m6)
        self.skill_combo = QComboBox()
        for idx in range(1, 7):
            move_code = f"m{idx}"
            self.skill_combo.addItem(f"M{idx} (F{idx})", move_code)
        selected_move = str(self.ctx.get("healer", "heal_move", default="m4")).lower()
        idx = self.skill_combo.findData(selected_move)
        if idx >= 0:
            self.skill_combo.setCurrentIndex(idx)
        layout.addRow("Skill de Cura:", self.skill_combo)

        # 3. Hotkey do Revive (Action Bar - suporta 1 tecla ou combinacoes como Shift+F)
        revive_box = QHBoxLayout()
        self.revive_combo = QComboBox()
        presets = [
            "Shift+F", "Ctrl+F", "Shift+1", "Ctrl+1",
            "F12", "F11", "F10", "F9", "F8", "F7", "F6", "F5", "F4", "F3", "F2", "F1",
            "Nenhum",
        ]
        saved_revive = str(self.ctx.get("healer", "revive_hotkey", default="Shift+F")).strip()
        if not saved_revive:
            saved_revive = "Shift+F"
        if saved_revive not in presets:
            self.revive_combo.addItem(saved_revive, saved_revive)
        for p in presets:
            self.revive_combo.addItem(p, "" if p == "Nenhum" else p)
        idx = self.revive_combo.findText(saved_revive)
        if idx >= 0:
            self.revive_combo.setCurrentIndex(idx)
        else:
            self.revive_combo.setCurrentIndex(0)

        self.btn_record_revive = QPushButton("⌨ Gravar")
        self.btn_record_revive.setToolTip("Clique e pressione as teclas no teclado (ex: Shift + F)")
        self.btn_record_revive.clicked.connect(self._record_revive_key)

        self.btn_clear_revive = QPushButton("✕ Limpar")
        self.btn_clear_revive.setToolTip("Desativa o Revive")
        self.btn_clear_revive.clicked.connect(self._clear_revive)

        revive_box.addWidget(self.revive_combo, 1)
        revive_box.addWidget(self.btn_record_revive)
        revive_box.addWidget(self.btn_clear_revive)
        layout.addRow("Hotkey do Revive (Action Bar):", revive_box)

        # 4. Dropdown "Pokemon de Caca (Retorno)"
        self.return_combo = QComboBox()
        layout.addRow("Pokémon de Caça (Retorno):", self.return_combo)

        # 5. SpinBox "Curar quando HP < (%)"
        self.spin_hp_thresh = QSpinBox()
        self.spin_hp_thresh.setRange(1, 99)
        self.spin_hp_thresh.setSuffix("%")
        self.spin_hp_thresh.setValue(int(self.ctx.get("healer", "hp_percent_threshold", default=60)))
        layout.addRow("Curar quando HP < (%):", self.spin_hp_thresh)

        # SpinBox "Delay entre Ações (ms)"
        self.spin_action_delay = QSpinBox()
        self.spin_action_delay.setRange(200, 3000)
        self.spin_action_delay.setSingleStep(50)
        self.spin_action_delay.setSuffix(" ms")
        self.spin_action_delay.setValue(int(self.ctx.get("healer", "pokemon_swap_delay_ms", default=1000)))
        self.spin_action_delay.setToolTip("Tempo de espera (em ms) entre puxar o Togepi, usar o Move de heal, aplicar o Revive e voltar.")
        layout.addRow("Delay entre Ações (ms):", self.spin_action_delay)

        # 6. Botao "Salvar e Ativar Healer"
        self.btn_save_activate = QPushButton("💾 Salvar e Ativar Healer")
        self.btn_save_activate.setFixedHeight(36)
        self.btn_save_activate.setStyleSheet(
            "font-weight: bold; font-size: 13px; background-color: #00aa66; color: white; border-radius: 4px;"
        )
        self.btn_save_activate.clicked.connect(self.save_and_activate_healer)
        layout.addRow(self.btn_save_activate)

        # 7. Botao "Testar Healer (Ignorar HP)"
        self.btn_test_healer = QPushButton("▶ Testar Healer (Ignorar HP)")
        self.btn_test_healer.setFixedHeight(34)
        self.btn_test_healer.setStyleSheet(
            "font-weight: bold; font-size: 12px; background-color: #2b5b84; color: white; border-radius: 4px;"
        )
        self.btn_test_healer.clicked.connect(self.test_healer)
        layout.addRow(self.btn_test_healer)

        # Status Label
        self.status = QLabel("Nomes nao detectados. Slots F1-F6.")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color: #aaaaaa; font-size: 11px;")
        layout.addRow(self.status)

        # Componentes mantidos para compatibilidade
        self.slot_combos = {slot: QComboBox() for slot in range(1, 7)}
        for c in self.slot_combos.values():
            c.setEditable(False)
        self.reset_pokemon_combo = QComboBox()
        self.reset_delay = QSpinBox()
        self.reset_delay.setRange(0, 10000)
        self.reset_delay.setValue(int(self.ctx.get("healer", "pokemon_reset_delay_ms", default=300)))

        self.refresh()
        self.detect_roster()
        self._populate_roster()

        self.pokemon_combo.currentIndexChanged.connect(self._choose_pokemon)
        self.return_combo.currentIndexChanged.connect(self._choose_return)
        self.reset_pokemon_combo.currentIndexChanged.connect(self._choose_reset)
        self.skill_combo.currentIndexChanged.connect(self._choose_skill)
        self.revive_combo.currentIndexChanged.connect(self._choose_revive)
        self.spin_hp_thresh.valueChanged.connect(self._on_hp_changed)
        self.reset_delay.valueChanged.connect(lambda value: self.ctx.set(value, "healer", "pokemon_reset_delay_ms"))
        self.client_combo.currentIndexChanged.connect(self._choose_client)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1500)

    def _clear_revive(self):
        idx = self.revive_combo.findText("Nenhum")
        if idx >= 0:
            self.revive_combo.setCurrentIndex(idx)
        else:
            self.revive_combo.setCurrentIndex(0)
        self._choose_revive()

    def _record_revive_key(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Gravar Tecla do Revive")
        d_layout = QVBoxLayout(dialog)
        d_layout.addWidget(QLabel("Pressione a combinação de teclas desejada (ex: Shift + F):"))
        key_edit = QKeySequenceEdit(dialog)
        cur = self._get_revive_hotkey()
        if cur and cur.upper() != "NENHUM":
            key_edit.setKeySequence(QKeySequence(cur))
        d_layout.addWidget(key_edit)
        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, dialog)
        btn_box.accepted.connect(dialog.accept)
        btn_box.rejected.connect(dialog.reject)
        d_layout.addWidget(btn_box)
        if dialog.exec():
            seq = key_edit.keySequence().toString()
            if seq:
                idx = self.revive_combo.findText(seq)
                if idx < 0:
                    self.revive_combo.insertItem(0, seq, seq)
                    idx = 0
                self.revive_combo.setCurrentIndex(idx)
                self._choose_revive()

    def _get_revive_hotkey(self) -> str:
        data = self.revive_combo.currentData()
        if data is not None:
            return str(data).strip()
        text = self.revive_combo.currentText().strip()
        if text.upper() in ("NENHUM", "NONE", "DESATIVADO", ""):
            return ""
        return text

    def _choose_revive(self, *_):
        hk = self._get_revive_hotkey()
        self.ctx.set(hk, "healer", "revive_hotkey")

    @staticmethod
    def _populate(combo, entries, selected):
        blocker = QSignalBlocker(combo)
        combo.clear()
        for text, identity in entries:
            combo.addItem(text, identity)
        idx = combo.findData(selected)
        if idx >= 0:
            combo.setCurrentIndex(idx)
        elif combo.count() > 0:
            combo.setCurrentIndex(0)
        del blocker

    def _populate_roster(self):
        entries = []
        for member in self._roster_snapshot.members:
            hotkey = f"F{member.slot}"
            if member.name and not member.name.startswith("Slot "):
                label = f"{member.name} ({hotkey})"
            else:
                label = f"Slot {member.slot} ({hotkey})"
            entries.append((label, str(member.slot)))

        for member in self._roster_snapshot.members:
            hotkey = f"F{member.slot}"
            if member.name and not member.name.startswith("Slot "):
                label = f"{member.name} ({hotkey})"
            else:
                label = f"Slot {member.slot} ({hotkey})"
            self._populate(self.slot_combos[member.slot], [(label, str(member.slot))], str(member.slot))

        heal_slot = str(self.ctx.get("healer", "heal_pokemon_slot",
                                    default=str(self.ctx.get("healer", "pokemon_heal_hotkey", default="4")))).upper().removeprefix("F")
        return_slot = str(self.ctx.get("healer", "pokemon_return_hotkey", default="1")).upper().removeprefix("F")
        reset_slot = str(self.ctx.get("healer", "pokemon_reset_hotkey", default="")).upper().removeprefix("F")

        self._populate(self.pokemon_combo, entries, heal_slot)
        self._populate(self.return_combo, entries, return_slot)
        self._populate(self.reset_pokemon_combo, [("Sem troca para reset", "")] + entries, reset_slot)

    def get_roster(self, pid: Any) -> RosterSnapshot:
        if type(pid) is not int or pid <= 0:
            return unavailable_roster(pid, "Selecione um cliente.")
        resolver = self.resolver_provider() if callable(self.resolver_provider) else None
        path = Path(resolver.offsets_path) if resolver else Path(__file__).resolve().parents[1] / "offsets.json"
        offsets = load_offsets(path, path.with_name("offsets.schema.json"))
        reader = self.reader if self.reader is not None else MemoryReader(pid)
        owned = self.reader is None
        try:
            if owned and not reader.attach():
                return unavailable_roster(pid, f"Falha ao abrir processo {pid}.")
            detector = RosterDetector(reader, offsets)
            rows = detector.get_roster()
            members = tuple(RosterMember(slot=row["slot"],
                                        name=None if row.get("name", "").startswith("Slot ") else row.get("name"))
                            for row in rows)
            detected = any(member.name for member in members)
            return RosterSnapshot(pid, members, detected,
                                  "pokemon.roster ou layout de nomes ainda nao mapeado." if not detected else "")
        finally:
            if owned:
                reader.detach()

    def _on_hp_changed(self, value: int):
        self.ctx.set(value, "healer", "hp_percent_threshold")

    def save_selection(self):
        self._choose_pokemon()
        self._choose_return()
        self._choose_reset()
        self._choose_skill()
        self._choose_revive()
        self.ctx.set(self.spin_hp_thresh.value(), "healer", "hp_percent_threshold")
        self.ctx.set(self.reset_delay.value(), "healer", "pokemon_reset_delay_ms")
        self.ctx.set(True, "healer", "swap_by_slot_key")
        if hasattr(self, "spin_action_delay"):
            action_delay = self.spin_action_delay.value()
            self.ctx.set(action_delay, "healer", "action_delay_ms")
            self.ctx.set(action_delay, "healer", "pokemon_swap_delay_ms")
            self.ctx.set(action_delay, "healer", "pokemon_skill_delay_ms")
            self.ctx.set(action_delay, "healer", "revive_delay_ms")

    def test_healer(self):
        """Executa a cura manualmente para teste, ignorando o HP."""
        self.save_selection()
        if callable(self.test_healer_callback):
            try:
                self.test_healer_callback()
                self.status.setText("▶ Teste de cura forçada enviado!")
                self.status.setStyleSheet("color: #00ff88; font-size: 11px;")
            except Exception as exc:
                log.exception("Erro ao executar test_healer_callback: %s", exc)
                self.status.setText(f"Erro no teste: {exc}")
                self.status.setStyleSheet("color: #ff4444; font-size: 11px;")
            return

        def _worker():
            try:
                from inputbus import get_bus
                from modules.healer import Healer
                bus = get_bus()
                bus.start()
                resolver = self.resolver_provider() if callable(self.resolver_provider) else None
                healer = Healer(ctx=self.ctx, bus=bus, resolver=resolver)
                healer.force_heal_sequence()
                self.status.setText("▶ Teste de cura forçada executado!")
                self.status.setStyleSheet("color: #00ff88; font-size: 11px;")
            except Exception as exc:
                log.exception("Erro ao executar teste do healer: %s", exc)
                self.status.setText(f"Erro no teste: {exc}")
                self.status.setStyleSheet("color: #ff4444; font-size: 11px;")

        import threading
        threading.Thread(target=_worker, daemon=True).start()

    def save_and_activate_healer(self):
        """Salva as configurações essenciais e ativa o módulo healer."""
        self.save_selection()
        hp_pct = self.spin_hp_thresh.value()
        self.ctx.set(hp_pct, "healer", "hp_percent_threshold")
        self.ctx.set(True, "modules", "healer", "enabled")
        self.ctx.set(True, "healer", "swap_by_slot_key")
        if hasattr(self.ctx, "save_config"):
            self.ctx.save_config()

        heal_slot = self.pokemon_combo.currentData()
        heal_name = self._slot_name(heal_slot) or f"Slot {heal_slot}"
        return_slot = self.return_combo.currentData()
        return_name = self._slot_name(return_slot) or f"Slot {return_slot}"

        self.status.setText(f"HEALER ATIVADO: Cura HP < {hp_pct}% | {heal_name} -> {return_name}")
        self.status.setStyleSheet("color: #00ff88; font-size: 11px; font-weight: bold;")
        log.info("Healer salvo e ativado com sucesso.")

    def _slot_name(self, slot: int | str | None) -> str:
        if not slot:
            return ""
        slot_int = int(slot)
        for member in self._roster_snapshot.members:
            if member.slot == slot_int and member.name:
                return member.name
        return ""

    def _choose_pokemon(self, *_):
        data = self.pokemon_combo.currentData()
        if not data:
            return
        slot = int(data)
        self.ctx.set(slot, "healer", "heal_pokemon_slot")
        self.ctx.set(f"F{slot}", "healer", "pokemon_heal_hotkey")
        self.ctx.set(self._slot_name(slot), "healer", "heal_pokemon_name")

    def _choose_return(self, *_):
        slot = self.return_combo.currentData()
        if not slot:
            return
        self.ctx.set(f"F{slot}", "healer", "pokemon_return_hotkey")
        self.ctx.set(self._slot_name(slot), "healer", "return_pokemon_name")

    def _choose_reset(self, *_):
        slot = self.reset_pokemon_combo.currentData()
        self.ctx.set(f"F{slot}" if slot else "", "healer", "pokemon_reset_hotkey")
        name = self._slot_name(slot) if slot else ""
        self.ctx.set(name, "healer", "reset_pokemon_name")

    def _choose_skill(self, *_):
        move = self.skill_combo.currentData()
        if not move:
            return
        self.ctx.set(move, "healer", "heal_move")
        if move.startswith("m") and move[1:].isdigit():
            self.ctx.set(f"F{move[1:]}", "healer", "pokemon_heal_skill_hotkey")
        else:
            self.ctx.set(move, "healer", "pokemon_heal_skill_hotkey")

    def _choose_client(self, *_):
        pid = self.client_combo.currentData()
        if pid is not None:
            self.ctx.set(pid, "client", "target_pid")
            self.detect_roster()

    def _open_process_dialog(self):
        from gui.dialogs.process_selector import ProcessSelectorDialog
        dialog = ProcessSelectorDialog(self)
        if dialog.exec():
            selected = dialog.selected_process
            if selected:
                pid, name = selected
                self.ctx.set(pid, "client", "target_pid")
                self.ctx.set(name, "client", "process_name")
                self.refresh()
                self.detect_roster()

    def detect_roster(self):
        pid = None
        if self.pid_provider is not None:
            try:
                pid = self.pid_provider()
            except Exception:
                pid = None
        if pid is None:
            pid = self.ctx.get("client", "target_pid", default=None)
        if pid is None:
            pid = self.client_combo.currentData()

        if self.roster_provider is not None:
            try:
                snapshot = self.roster_provider.get_roster(pid)
            except Exception as exc:
                snapshot = unavailable_roster(pid, str(exc))
        else:
            snapshot = self.get_roster(pid)

        if snapshot.pid is not None and pid is not None and snapshot.pid != pid:
            self.status.setText("Snapshot recebido pertence a outro cliente.")
            self._roster_snapshot = unavailable_roster(pid, "Snapshot recebido pertence a outro cliente.")
            self._populate_roster()
            return

        self._roster_snapshot = snapshot
        self._populate_roster()
        if self._roster_snapshot.detected:
            names = [m.name for m in self._roster_snapshot.members if m.name]
            self.status.setText(f"Nomes recebidos da memoria ({len(names)} Pokemon).")
            self.status.setStyleSheet("color: #00ff88; font-size: 11px;")
        else:
            msg = f"Nomes nao detectados. {self._roster_snapshot.reason}" if self._roster_snapshot.reason else "Nomes nao detectados. Slots F1-F6."
            self.status.setText(msg)
            self.status.setStyleSheet("color: #ffaa00; font-size: 11px;")

    def refresh(self):
        if self.process_provider is not None:
            try:
                pids = list(self.process_provider())
            except Exception:
                pids = []
            if pids != self._known_pids:
                self._known_pids = pids
                blocker = QSignalBlocker(self.client_combo)
                self.client_combo.clear()
                for p in pids:
                    self.client_combo.addItem(f"Processo {p}", p)
                del blocker
        else:
            candidates = self.ctx.get("client", "candidates", default=[])
            pids = [entry.get("pid") for entry in candidates if entry.get("pid")]
            if pids != self._known_pids:
                self._known_pids = pids
                blocker = QSignalBlocker(self.client_combo)
                self.client_combo.clear()
                for entry in candidates:
                    pid, name = entry.get("pid"), entry.get("name", "cliente")
                    self.client_combo.addItem(f"{name} ({pid})", pid)
                del blocker

        target_pid = self.ctx.get("client", "target_pid", default=None)
        if target_pid is not None:
            idx = self.client_combo.findData(target_pid)
            if idx >= 0 and idx != self.client_combo.currentIndex():
                blocker = QSignalBlocker(self.client_combo)
                self.client_combo.setCurrentIndex(idx)
                del blocker
                if self._roster_snapshot.pid != target_pid:
                    self.detect_roster()
