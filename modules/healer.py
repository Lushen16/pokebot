"""Cura do player por hotkey com troca de Pokémon e cooldown independente.

Preserva Healer(ctx, bus, resolver) e as chaves usadas pela GUI.
Mecânica de cura dinâmica:
1. Pausa o Cavebot (healing_in_progress).
2. Pressiona hotkey para trocar para o Pokémon de cura selecionado (ex: Togepi -> Tecla 4).
3. Aguarda delay de troca (pokemon_swap_delay_ms).
4. Se configurado, bate Revive (ex: Shift+F ou F12) e aguarda delay (revive_delay_ms).
5. Pressiona hotkey da skill de cura configurada (ex: F4) ou envia comando no chat (m4).
6. Aguarda delay da skill (pokemon_skill_delay_ms).
7. Pressiona a hotkey do Pokemon de reset, quando configurada.
8. Retorna ao Pokemon de combate (ex: Spearow -> Tecla 1) e retoma o Cavebot.
"""

from __future__ import annotations

import logging
import math
import time
from typing import Optional

from modules.base import Module

log = logging.getLogger("pokebot.modules.healer")

_VK_MAP = {f"F{number}": 0x6F + number for number in range(1, 13)}
_VK_MAP.update({str(number): ord(str(number)) for number in range(10)})
_VK_MAP.update({f"M{number}": 0x6F + number for number in range(1, 13)})
_VK_MAP.update({chr(letter): letter for letter in range(ord('A'), ord('Z') + 1)})
_VK_MAP.update({chr(letter): letter for letter in range(ord('a'), ord('z') + 1)})
_VK_MAP["ENTER"] = 0x0D
_VK_MAP["RETURN"] = 0x0D
_VK_MAP["SPACE"] = 0x20
_VK_MAP["SHIFT"] = 0x10
_VK_MAP["LSHIFT"] = 0xA0
_VK_MAP["RSHIFT"] = 0xA1
_VK_MAP["CTRL"] = 0x11
_VK_MAP["CONTROL"] = 0x11
_VK_MAP["LCTRL"] = 0xA2
_VK_MAP["RCTRL"] = 0xA3
_VK_MAP["ALT"] = 0x12
_VK_MAP["LALT"] = 0xA4
_VK_MAP["RALT"] = 0xA5
_VK_MAP["TAB"] = 0x09
_VK_MAP["ESCAPE"] = 0x1B
_VK_MAP["ESC"] = 0x1B


def _resolve_vk(hotkey: str) -> Optional[int]:
    if not hotkey:
        return None
    key = str(hotkey).strip().upper()
    if key in ("NENHUM", "NONE", "DESATIVADO", ""):
        return None
    if key in _VK_MAP:
        return _VK_MAP[key]
    try:
        code = int(key, 0)
    except ValueError as exc:
        raise ValueError(f"hotkey inválida: {hotkey!r}") from exc
    if not 1 <= code <= 0xFE:
        raise ValueError(f"VK fora do intervalo: {hotkey!r}")
    return code


def _resolve_hotkey(hotkey: str) -> list[int]:
    """Resolve uma hotkey ou combinação de teclas (ex: 'Shift+F', 'Ctrl+1', 'F12')."""
    if not hotkey:
        return []
    s = str(hotkey).strip()
    if s.upper() in ("NENHUM", "NONE", "DESATIVADO", ""):
        return []
    parts = [p.strip() for p in s.replace("+", " ").split() if p.strip()]
    vk_list = []
    for part in parts:
        code = _resolve_vk(part)
        if code is not None:
            vk_list.append(code)
    return vk_list


class Healer(Module):
    """Cura normal e de emergência usando a fila de input existente com sequência de troca de Pokémon."""

    name = "healer"
    config_key = "healer"

    def __init__(self, ctx=None, bus=None, resolver=None):
        super().__init__(ctx, bus)
        self.resolver = resolver
        self.log = log
        self._last_heal_ts = None
        self._last_emergency_ts = None
        self.heals_sent = 0
        self.emergency_heals_sent = 0
        self.last_hp = None
        self.last_hp_max = None
        self.last_pct = None
        self.last_skip_reason = None
        self._load_settings()

    def _load_settings(self):
        def setting(name, default, alias=None):
            fallback = self.ctx.get("healer", alias, default=default) if (self.ctx and alias) else default
            return self.ctx.get("healer", name, default=fallback) if self.ctx else default

        self.hp_threshold_pct = float(setting("hp_percent_threshold", 90))
        self.emergency_threshold_pct = float(setting("emergency_threshold_pct", 20, "panic_hp_percent"))
        self.hotkey = setting("hotkey", "F1", "hp_hotkey")
        self.emergency_hotkey = setting("emergency_hotkey", "F3", "panic_hotkey")
        self.cooldown_ms = float(setting("cooldown_ms", 800, "delay_between_heals_ms"))
        self.emergency_cooldown_ms = float(setting("emergency_cooldown_ms", 400))
        self.hold_ms = float(setting("hold_ms", 50))

        self.heal_pokemon_name = setting("heal_pokemon_name", "")
        self.heal_pokemon_slot = setting("heal_pokemon_slot", None)
        self.heal_move = setting("heal_move", None)

        default_heal_hk = str(self.heal_pokemon_slot) if self.heal_pokemon_slot else "4"
        self.pokemon_heal_hotkey = str(setting("pokemon_heal_hotkey", default_heal_hk)).strip()

        default_skill_hk = str(self.heal_move).upper() if self.heal_move else "F2"
        self.pokemon_heal_skill_hotkey = str(setting("pokemon_heal_skill_hotkey", default_skill_hk)).strip()

        self.return_pokemon_name = setting("return_pokemon_name", "")
        self.pokemon_return_hotkey = str(setting("pokemon_return_hotkey", "1")).strip()
        self.pokemon_reset_hotkey = str(setting("pokemon_reset_hotkey", "") or "").strip()
        self.pokemon_reset_delay_ms = float(setting("pokemon_reset_delay_ms", 300))
        self.pokemon_swap_delay_ms = int(setting("pokemon_swap_delay_ms", 500))
        self.pokemon_skill_delay_ms = int(setting("pokemon_skill_delay_ms", 500))

        self.revive_hotkey = str(setting("revive_hotkey", "") or "").strip()
        self.revive_delay_ms = int(setting("revive_delay_ms", 400))

        self.swap_by_slot_key = bool(setting("swap_by_slot_key", False))
        if self.swap_by_slot_key:
            if self.pokemon_heal_hotkey.upper().startswith("F") and self.pokemon_heal_hotkey[1:].isdigit():
                self.pokemon_heal_hotkey = self.pokemon_heal_hotkey[1:]
            if self.pokemon_return_hotkey.upper().startswith("F") and self.pokemon_return_hotkey[1:].isdigit():
                self.pokemon_return_hotkey = self.pokemon_return_hotkey[1:]

        if not 0 <= self.emergency_threshold_pct <= self.hp_threshold_pct <= 100:
            raise ValueError("thresholds inválidos: 0 <= emergência <= normal <= 100")
        if any(not math.isfinite(value) or value < 0 for value in
               (self.cooldown_ms, self.emergency_cooldown_ms, self.hold_ms,
                self.pokemon_swap_delay_ms, self.pokemon_skill_delay_ms, self.pokemon_reset_delay_ms,
                self.revive_delay_ms)):
            raise ValueError("cooldown, hold_ms e delays devem ser finitos e não negativos")

        self._vk_normal = _resolve_vk(self.hotkey)
        self._vk_emergency = _resolve_vk(self.emergency_hotkey)
        self._vk_pokemon_heal = _resolve_vk(self.pokemon_heal_hotkey)
        self._vk_pokemon_skill = _resolve_vk(self.pokemon_heal_skill_hotkey)
        self._vk_pokemon_return = _resolve_vk(self.pokemon_return_hotkey)
        self._vk_reset_hotkey = _resolve_vk(self.pokemon_reset_hotkey) if self.pokemon_reset_hotkey else None
        self._vk_revive = _resolve_hotkey(self.revive_hotkey)

    def press_hotkey(self, codes: list[int], hold_ms: float = 50.0, priority: int = 5) -> bool:
        """Pressiona uma tecla única ou combinação de teclas (ex: Shift+F)."""
        if not codes:
            return True
        if len(codes) == 1:
            return self.press(codes[0], hold_ms=hold_ms, priority=priority)
        if hasattr(self.bus, "press_combo"):
            return self.bus.press_combo(codes, hold_ms=int(hold_ms), priority=priority, module=self.name)
        # Fallback para mocks ou barramentos simples
        for c in codes:
            self.press(c, hold_ms=hold_ms, priority=priority)
        return True

    def type_chat_command(self, text: str, priority: int = 5) -> bool:
        """Digita um comando no chat (ex: 'm4') e envia Enter."""
        for char in text:
            vk = _resolve_vk(char.upper())
            if vk is not None:
                if not self.press(vk, hold_ms=self.hold_ms, priority=priority):
                    return False
                time.sleep(0.02)
        time.sleep(0.04)
        return self.press(0x0D, hold_ms=self.hold_ms, priority=priority)

    def force_heal_sequence(self) -> None:
        """Executa a cura manualmente para teste, ignorando o HP."""
        self.log.info("[TESTE] Iniciando sequência forçada de cura...")
        if self.last_hp is None:
            self.last_hp = 100
            self.last_hp_max = 100
            self.last_pct = 100.0
        self._do_heal(is_emergency=False)
        self.log.info("[TESTE] Sequência concluída.")

    def _do_heal(self, is_emergency: bool) -> bool:
        if self.ctx and hasattr(self.ctx, "set"):
            self.ctx.set(True, "combat", "healing_in_progress")

        try:
            target_name = f"{self.heal_pokemon_name} ({self.pokemon_heal_hotkey})" if self.heal_pokemon_name else str(self.pokemon_heal_hotkey)
            move_name = str(self.heal_move).upper() if self.heal_move else f"Skill {self.pokemon_heal_skill_hotkey}"
            self.log.info(
                "%s: hp=%d/%d (%.1f%%) — iniciando cura [%s -> %s -> revive %s -> reset %s -> volta %s]",
                "EMERGENCIA" if is_emergency else "cura normal",
                self.last_hp, self.last_hp_max, self.last_pct,
                target_name, move_name, self.revive_hotkey or "desativado",
                self.pokemon_reset_hotkey or "desativado", self.pokemon_return_hotkey,
            )

            # 1. Troca pro pokémon de cura
            self.log.info("[Healer 1/4] Puxando Pokémon de cura: %s (tecla %s)...", target_name, self.pokemon_heal_hotkey)
            if not self.press(self._vk_pokemon_heal, hold_ms=self.hold_ms, priority=1 if is_emergency else 5):
                self.log.warning("falha ao enviar tecla do pokemon de cura (%s)", self.pokemon_heal_hotkey)
                return False
            time.sleep(self.pokemon_swap_delay_ms / 1000.0)

            # 2. Usa a skill de cura
            move_text = str(self.heal_move or "").lower().strip()
            use_chat = self.ctx.get("healer", "use_chat_command", default=False) if (self.ctx and hasattr(self.ctx, "get")) else False
            if use_chat and move_text.startswith("m") and move_text[1:].isdigit():
                self.log.info("[Healer 2/4] Digitando comando de cura no chat (%s)...", move_text)
                if not self.type_chat_command(move_text, priority=1 if is_emergency else 5):
                    self.log.warning("falha ao digitar comando da skill de cura (%s)", move_text)
                    return False
            elif self._vk_pokemon_skill is not None:
                self.log.info("[Healer 2/4] Acionando Move de cura: %s (tecla %s)...", move_name, self.pokemon_heal_skill_hotkey)
                if not self.press(self._vk_pokemon_skill, hold_ms=self.hold_ms, priority=1 if is_emergency else 5):
                    self.log.warning("falha ao enviar tecla da skill de cura (%s)", self.pokemon_heal_skill_hotkey)
                    return False
            time.sleep(self.pokemon_skill_delay_ms / 1000.0)

            # 3. Bate Revive no Pokémon de cura se configurado
            if self._vk_revive:
                self.log.info("[Healer 3/4] Aplicando Revive no Healer (tecla %s)...", self.revive_hotkey)
                if not self.press_hotkey(self._vk_revive, hold_ms=self.hold_ms, priority=1 if is_emergency else 5):
                    self.log.warning("falha ao enviar tecla do Revive (%s)", self.revive_hotkey)
                    return False
                time.sleep(self.revive_delay_ms / 1000.0)

            # 4. Reset se configurado
            if self._vk_reset_hotkey is not None:
                self.log.info("[Healer Extra] Trocando para Pokémon de reset (tecla %s)...", self.pokemon_reset_hotkey)
                if not self.press(self._vk_reset_hotkey, hold_ms=self.hold_ms, priority=1 if is_emergency else 5):
                    self.log.warning("fila recusou hotkey do Pokemon de reset")
                    return False
                time.sleep(self.pokemon_reset_delay_ms / 1000.0)

            # 5. Retorna pro pokémon de combate
            ret_name = self.return_pokemon_name or f"Slot {self.pokemon_return_hotkey}"
            self.log.info("[Healer 4/4] Retornando ao Pokémon de combate: %s (tecla %s)...", ret_name, self.pokemon_return_hotkey)
            if not self.press(self._vk_pokemon_return, hold_ms=self.hold_ms, priority=10):
                self.log.warning("fila recusou retorno ao pokemon de combate")
                return False
            self.log.info("[Healer Concluído] Sequência executada com sucesso! Retomando Cavebot.")
            return True
        finally:
            if self.ctx and hasattr(self.ctx, "set"):
                self.ctx.set(False, "combat", "healing_in_progress")

    def tick(self):
        try:
            self._tick_healer()
        except Exception:
            self.last_skip_reason = "erro no ciclo de cura"
            self.log.exception("Falha no ciclo do Healer")

    def _tick_healer(self):
        self._load_settings()
        self.last_hp = None
        self.last_hp_max = None
        self.last_pct = None
        if self.resolver is None:
            self.last_skip_reason = "resolver indisponível"
            return

        hp = self.resolver.read_field("player", "hp") if hasattr(self.resolver, "read_field") else None
        hp_max = self.resolver.read_field("player", "hp_max") if hasattr(self.resolver, "read_field") else None
        self.last_hp = hp
        self.last_hp_max = hp_max

        if hp is None or hp_max is None:
            self.last_skip_reason = f"leitura HP falhou (player.hp={hp!r}, player.hp_max={hp_max!r})"
        elif hp_max <= 0 or hp < 0 or hp > hp_max:
            self.last_skip_reason = f"HP inválido: {hp}/{hp_max}"
        elif hp == 0:
            self.last_pct = 0
            self.last_skip_reason = "HP zerado"
        else:
            self.last_pct = hp * 100.0 / hp_max
            self._heal_if_needed()
            return

        now = time.monotonic()
        if not hasattr(self, "_last_fail_log_ts") or (now - self._last_fail_log_ts) > 4.0:
            self._last_fail_log_ts = now
            self.log.info("[Healer] %s", self.last_skip_reason)

    def _heal_if_needed(self):
        emergency = self.last_pct < self.emergency_threshold_pct
        if not emergency and self.last_pct >= self.hp_threshold_pct:
            self.last_skip_reason = "HP acima ou igual ao threshold"
            now = time.monotonic()
            if not hasattr(self, "_last_ok_log_ts") or (now - self._last_ok_log_ts) > 4.0:
                self._last_ok_log_ts = now
                self.log.info(
                    "[Healer] HP monitorado: %.1f%% (Threshold: %.1f%%)",
                    self.last_pct, self.hp_threshold_pct,
                )
            return
        now = time.monotonic()
        previous = self._last_emergency_ts if emergency else self._last_heal_ts
        cooldown = self.emergency_cooldown_ms if emergency else self.cooldown_ms
        if previous is not None and (now - previous) * 1000 < cooldown:
            self.last_skip_reason = "emergência em cooldown" if emergency else "cura em cooldown"
            self.log.debug(self.last_skip_reason)
            return

        if not self._do_heal(is_emergency=emergency):
            self.last_skip_reason = "fila de input recusou a cura"
            self.log.debug(self.last_skip_reason)
            return

        completed_at = time.monotonic()
        self._last_heal_ts = completed_at
        if emergency:
            self._last_emergency_ts = completed_at
            self.emergency_heals_sent += 1
        else:
            self.heals_sent += 1
        self.last_skip_reason = None
        if self.ctx and hasattr(self.ctx, "inc_stat"):
            self.ctx.inc_stat("heals")
        self.log.log(
            logging.WARNING if emergency else logging.INFO,
            "%s: hp=%d/%d (%.1f%%) -> sequência de cura enfileirada; efeito no jogo não confirmado",
            "EMERGÊNCIA" if emergency else "curando",
            self.last_hp, self.last_hp_max, self.last_pct,
        )

    @property
    def stats(self):
        return {
            "heals_sent": self.heals_sent,
            "emergency_heals_sent": self.emergency_heals_sent,
            "last_hp": self.last_hp,
            "last_hp_max": self.last_hp_max,
            "last_hp_pct": round(self.last_pct, 1) if self.last_pct is not None else None,
            "hp_threshold_pct": self.hp_threshold_pct,
            "emergency_threshold_pct": self.emergency_threshold_pct,
            "hotkey": self.hotkey,
            "emergency_hotkey": self.emergency_hotkey,
            "heal_pokemon_name": self.heal_pokemon_name,
            "heal_pokemon_slot": self.heal_pokemon_slot,
            "pokemon_heal_hotkey": self.pokemon_heal_hotkey,
            "heal_move": self.heal_move,
            "pokemon_heal_skill_hotkey": self.pokemon_heal_skill_hotkey,
            "pokemon_return_hotkey": self.pokemon_return_hotkey,
            "pokemon_reset_hotkey": self.pokemon_reset_hotkey,
            "revive_hotkey": self.revive_hotkey,
        }
