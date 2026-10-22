# pokebot/modules/auto_target.py
"""Auto-target module: creature selection and attack."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from modules.base import Module

log = logging.getLogger("pokebot.modules.auto_target")

# Perfect Lure: associacao do Pokemon ativo a uma criatura do scan.
LURE_HP_TOL = 1
LURE_FOLLOWER_MAX_DIST = 3
LURE_IDENTITY_STABLE_TICKS = 2
LURE_IDENTITY_TTL_S = 2.0
POKEMON_POSITION_TTL_S = 0.75


class ActionKind(str, Enum):
    NONE = "none"
    SELECT_TARGET = "select_target"
    WAIT_GROUP = "wait_group"
    DETOUR = "detour"
    COMBO = "combo"
    WAIT_PENDING = "wait_pending"
    HEAL = "heal"
    RELEASE_COMBAT = "release_combat"
    ATTACK = "attack"


@dataclass(frozen=True)
class ActionDecision:
    kind: ActionKind
    reason: str = ""
    target: Optional[dict] = None


class AutoTarget(Module):
    name = "auto_target"
    config_key = "auto_target"

    def __init__(self, ctx=None, bus=None, resolver=None, walker=None, healer=None):
        super().__init__(ctx, bus)
        self.resolver = resolver
        self.walker = walker
        self.healer = healer
        self._last_attack = 0.0
        self._current_target_id = None
        self._attack_hotkey_vk = 0
        self._target_hotkey_vk = 0x09
        self._target_select_delay = 0.15
        self._last_target_select_at = 0.0
        self._target_selected_at = 0.0
        self._last_attack_target_id = None
        self._last_attack_hp = None
        self._last_attack_hp_at = 0.0
        from core.combo_repeater import ComboRepeater
        repeat_count = int(self._cfg_get("combo_repeat_count", 3) or 3)
        try:
            repeat_interval = float(self._cfg_get("combo_repeat_interval_ms", 2000) or 2000) / 1000.0
        except (TypeError, ValueError):
            repeat_interval = 2.0
        self._combo_repeater = ComboRepeater(
            self._send_combo_once, count=repeat_count, interval=repeat_interval)
        self._reader = self._build_reader()
        self._load_hotkey()
        self._perfect = None
        self._last_lure_status = None
        self._scratch = {}
        self._lure_roster_sig = None
        self._lure_identity_id = None
        self._lure_identity_at = 0.0
        self._lure_identity_hp = None
        self._lure_candidate_id = None
        self._lure_candidate_count = 0
        self._last_lure_unavailable = ""
        self._combat_paused = False
        self._last_target_seen = 0.0
        self._last_decision: Optional[ActionDecision] = None
        self._minimap = None
        self._best_tile_selector = None
        self._best_tile_map_warned = False
        self._last_map_refresh = 0.0
        self._last_map_retry = 0.0
        self._last_pokemon_pos = None
        self._last_pokemon_pos_at = 0.0
        self._last_pokemon_name = None
        if self.walker is not None and hasattr(self.walker, "pokemon_position_provider"):
            self.walker.pokemon_position_provider = self.active_pokemon_position
        if self.walker is not None and hasattr(self.walker, "pokemon_move_command_provider"):
            self.walker.pokemon_move_command_provider = self.send_pokemon_move_command

    def _cfg_get(self, key, default=None):
        return self.ctx.get("auto_target", key, default=default)

    def _cfg_any(self, *keys, default=None):
        """Primeira chave presente em auto_target (compat com chaves antigas)."""
        for key in keys:
            val = self.ctx.get("auto_target", key, default=None)
            if val is not None:
                return val
        return default

    def _perfect_enabled(self) -> bool:
        return bool(self._cfg_any("perfect_lure_enabled", "perfect_lure", default=True))

    def _focus_shiny_enabled(self) -> bool:
        """Com `focus_shiny`, só criaturas shiny (estrela/boss) entram no alvo."""
        return bool(self._cfg_get("focus_shiny", False))

    def _passes_shiny_filter(self, creature: dict) -> bool:
        return not self._focus_shiny_enabled() or bool(creature.get("shiny"))

    def _build_reader(self):
        """EntityReader ancorado no registry (fast path por assinatura)."""
        if not self.resolver:
            return None
        proc = getattr(self.resolver, "proc", None)
        if proc is None:
            return None
        try:
            from core.entities import EntityReader
        except Exception:
            return None

        radius = int(self.ctx.get("auto_target", "scan_radius", default=40) or 40)

        def registry_provider():
            begin = self.resolver.resolve_entry("world.entity_list_ptr")
            end = self.resolver.resolve_entry("world.entity_list_end")
            if not begin or not end or end <= begin:
                return []
            anchors = []
            for slot in range(begin, end, 0x10):
                base = proc.read_ptr(slot)
                if base and base > 0x10000:
                    anchors.append(base + 0x20)
            return anchors

        def regions_provider():
            from core.tools.scan_value import iter_regions
            return list(iter_regions(proc))

        return EntityReader(proc, registry_provider=registry_provider,
                            regions_provider=regions_provider, radius=radius)

    def _player_pos(self):
        if not self.resolver:
            return None
        x = self.resolver.read_entry("player.x")
        y = self.resolver.read_entry("player.y")
        if x is None or y is None:
            return None
        return int(x), int(y)

    def _load_hotkey(self):
        hk = {
            "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73, "F5": 0x74,
            "F6": 0x75, "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79,
            "F11": 0x7A, "F12": 0x7B, "TAB": 0x09, "SPACE": 0x20,
            "ENTER": 0x0D,
        }
        atk_hk = self.ctx.get("auto_target", "attack_hotkey", default="F5")
        self._attack_hotkey_configured = bool(str(atk_hk).strip())
        self._attack_hotkey_vk = hk.get(str(atk_hk).upper(), 0x74)
        target_hk = self.ctx.get("auto_target", "target_hotkey", default="Tab")
        self._target_hotkey_vk = hk.get(str(target_hk).upper(), 0x09)
        try:
            delay = int(self.ctx.get("auto_target", "target_select_delay_ms", default=150) or 0)
        except (TypeError, ValueError):
            delay = 150
        self._target_select_delay = max(0, min(1000, delay)) / 1000.0

    def _effective_combo_command(self) -> str:
        return str(self._cfg_get("combo_command", "") or "").strip()

    def _combo_spellwords(self) -> list[str]:
        raw = str(self._cfg_get("combo_spellwords", "") or "").strip()
        if not raw:
            return []
        return [token for token in raw.replace(",", " ").replace(";", " ").split() if token]

    def _perfect_combo_command(self) -> str:
        return self._effective_combo_command() or ("combo" if self._combo_spellwords() else "")

    def _should_combo_attack(self) -> bool:
        mode = str(self._cfg_get("target_mode", "attack_hotkey") or "attack_hotkey")
        command = self._effective_combo_command()
        return mode == "combo" or (not self._attack_hotkey_configured
                                   and bool(command or self._combo_spellwords()
                                            or str(self._cfg_get("combo_hotkey", "F5") or "").strip()))

    def _send_combo_once(self) -> bool:
        if self.bus is None:
            return False
        spells = self._combo_spellwords()
        try:
            if spells:
                log.info("AUTO_TARGET: combo golpes separados %s", spells)
                ok = True
                for spell in spells:
                    ok = bool(self.bus.send_text(spell, priority=10, module=self.name)) and ok
                return ok
            command = self._effective_combo_command()
            if not command:
                from modules.target import parse_hotkey
                hotkey = str(self._cfg_get("combo_hotkey", "F5") or "").strip()
                codes = parse_hotkey(hotkey)
                if not codes:
                    log.warning("AUTO_TARGET: hotkey de combo inválida: %r", hotkey)
                    return False
                log.info("AUTO_TARGET: combo por hotkey %s (execução no cliente não confirmada)", hotkey)
                return self._press_combo(codes)
            log.info("AUTO_TARGET: combo '%s'", command)
            return bool(self.bus.send_text(command, priority=10, module=self.name))
        except Exception as exc:  # noqa: BLE001
            log.warning("Falha ao enviar combo: %s", exc)
            return False

    def _trigger_combo_sequence(self, _command=None) -> bool:
        return self._combo_repeater.trigger(time.monotonic())

    def _press_combo(self, codes, kind="combo") -> bool:
        bus = self.bus
        if bus is None or not hasattr(bus, "press_combo"):
            return False
        return bool(bus.press_combo(list(codes), hold_ms=50, priority=10, module=self.name))

    def own_pokemon_names(self) -> list[str]:
        roster = self.ctx.get("pokemon_roster", default={}) or {}
        if isinstance(roster, dict):
            return [str(v) for v in roster.values() if v]
        return []

    def _player_z(self):
        if not self.resolver:
            return None
        try:
            z = self.resolver.read_entry("player.z")
        except Exception:  # noqa: BLE001 - leitura ao vivo pode falhar
            return None
        if z is None:
            return None
        try:
            return int(z) & 0xFF
        except (TypeError, ValueError):
            return None

    def _active_pokemon_key(self):
        """Identidade logica do Pokemon ativo (config `active_pokemon`).

        Sem essa chave nao da para detectar a troca; as leituras de HP seguem
        sendo validas, apenas nao ha descarte por identidade.
        """
        name = self._cfg_get("active_pokemon", None)
        return str(name).strip().lower() if name else None

    def _reset_lure_identity(self) -> None:
        self._lure_identity_id = None
        self._lure_identity_at = 0.0
        self._lure_identity_hp = None
        self._lure_candidate_id = None
        self._lure_candidate_count = 0
        self._last_pokemon_pos = None
        self._last_pokemon_pos_at = 0.0
        self._last_pokemon_name = None

    @staticmethod
    def _lure_creature_id(creature):
        cid = creature.get("creature_id")
        if cid is None:
            cid = creature.get("anchor")
        if cid is None:
            cid = (str(creature.get("name") or "").lower(),
                   creature.get("x"), creature.get("y"))
        return cid

    def _resolve_lure_identity(self, creatures, player, hp_percent, now=None):
        """Resolve identidade/posicao/HP do Pokemon ativo.

        A associacao so e confirmada quando UMA criatura casa nome do roster
        (ou `active_pokemon`), HP% igual ao lido (`pokemon.hp_percent`) e
        proximidade do personagem, por `LURE_IDENTITY_STABLE_TICKS` ticks com
        o mesmo id. Se o HP global vier zerado/indisponivel e houver um unico
        candidato por nome/proximidade, o HP da criatura assume. Mudanca de
        roster ou de id invalida tudo. Sem criatura confirmada, a posicao fica
        indisponivel; o HP continua utilizavel apenas dentro de
        `LURE_IDENTITY_TTL_S` apos a ultima confirmacao.
        """
        from modules.perfect_lure import POSITION_UNAVAILABLE

        now = time.monotonic() if now is None else float(now)
        roster = sorted({str(n).strip().lower() for n in self.own_pokemon_names() if n})
        explicit = self._active_pokemon_key()
        sig = (tuple(roster), explicit)
        if sig != self._lure_roster_sig:
            self._lure_roster_sig = sig
            self._reset_lure_identity()

        hp_value = None
        if hp_percent is not None:
            try:
                candidate_hp = int(hp_percent)
                if 0 <= candidate_hp <= 100:
                    hp_value = candidate_hp
            except (TypeError, ValueError):
                hp_value = None

        candidate = None
        resolved_hp = hp_value
        names = {explicit} if explicit else set(roster)
        nearby = []
        if names and creatures and player is not None:
            px, py = player
            for creature in creatures:
                if creature.get("is_player") or creature.get("is_npc") or creature.get("is_dead"):
                    continue
                name = str(creature.get("name") or "").strip().lower()
                if name not in names:
                    continue
                try:
                    distance = max(abs(int(creature.get("x", px)) - px),
                                   abs(int(creature.get("y", py)) - py))
                except (TypeError, ValueError):
                    continue
                if distance > LURE_FOLLOWER_MAX_DIST:
                    continue
                nearby.append(creature)

            hp_matches = []
            if hp_value is not None:
                for creature in nearby:
                    try:
                        if abs(int(creature.get("hp_percent", -999)) - hp_value) <= LURE_HP_TOL:
                            hp_matches.append(creature)
                    except (TypeError, ValueError):
                        continue
            if len(hp_matches) == 1:
                candidate = hp_matches[0]
                resolved_hp = hp_value
            elif not hp_matches and len(nearby) == 1:
                # O offset global de HP pode falhar (ex.: lendo 0 apos update).
                # Com um unico candidato de nome/proximidade, o HP da propria
                # criatura e a fonte confiavel.
                try:
                    creature_hp = int(nearby[0].get("hp_percent", -1))
                except (TypeError, ValueError):
                    creature_hp = -1
                if 0 <= creature_hp <= 100 and (hp_value is None or hp_value == 0):
                    candidate = nearby[0]
                    resolved_hp = creature_hp

        def unavailable(key):
            return {"key": key, "position_confirmed": False,
                    "hp_confirmed": False, "hp": None, "x": None, "y": None,
                    "z": None, "reason": POSITION_UNAVAILABLE}

        if candidate is None:
            age = now - self._lure_identity_at
            if (self._lure_identity_id is not None
                    and age <= LURE_IDENTITY_TTL_S
                    and self._lure_identity_hp is not None):
                return {"key": self._lure_identity_id, "position_confirmed": False,
                        "hp_confirmed": True, "hp": self._lure_identity_hp,
                        "x": None, "y": None, "z": None,
                        "reason": POSITION_UNAVAILABLE}
            self._reset_lure_identity()
            return unavailable(None)

        cid = self._lure_creature_id(candidate)
        if cid != self._lure_candidate_id:
            self._lure_candidate_id = cid
            self._lure_candidate_count = 1
            if self._lure_identity_id is not None and cid != self._lure_identity_id:
                # Troca detectada: invalida imediatamente antes de revalidar.
                self._reset_lure_identity()
                self._lure_candidate_id = cid
                self._lure_candidate_count = 1
        else:
            self._lure_candidate_count += 1

        if self._lure_candidate_count < LURE_IDENTITY_STABLE_TICKS:
            return unavailable(None)

        self._lure_identity_id = cid
        self._lure_identity_at = now
        self._lure_identity_hp = resolved_hp
        name = str(candidate.get("name") or "").strip()
        if name:
            self._last_pokemon_name = name
        return {"key": cid, "position_confirmed": True,
                "hp_confirmed": resolved_hp is not None,
                "hp": resolved_hp, "name": name,
                "x": int(candidate.get("x")), "y": int(candidate.get("y")),
                "z": candidate.get("z"), "reason": ""}

    def _eligible_lure_monsters(self, creatures, player, floor_z):
        """Monstros elegiveis (mesmas regras de targeting) no mesmo andar."""
        if not creatures or player is None:
            return []
        px, py = player
        limit = int(self._cfg_get("attack_range_tiles", 7) or 7)
        whitelist = {str(n).lower() for n in (self._cfg_get("name_whitelist", []) or [])}
        blacklist = {str(n).lower() for n in (self._cfg_get("name_blacklist", []) or [])}
        blacklist |= {n.lower() for n in self.own_pokemon_names()}
        ignore_players = bool(self._cfg_get("ignore_players", True))
        ignore_npcs = bool(self._cfg_get("ignore_npcs", True))
        ignore_dead = bool(self._cfg_get("ignore_dead", True))
        floor = None if floor_z is None else (int(floor_z) & 0xFF)
        out = []
        for c in creatures:
            if ignore_players and c.get("is_player"):
                continue
            if ignore_npcs and c.get("is_npc"):
                continue
            if ignore_dead and c.get("is_dead"):
                continue
            if int(c.get("hp_percent", 100) or 0) <= 0:
                continue
            name = str(c.get("name") or "").lower()
            if whitelist and name not in whitelist:
                continue
            if name in blacklist:
                continue
            if floor is not None and c.get("z") is not None:
                if (int(c.get("z")) & 0xFF) != floor:
                    continue
            distance = max(abs(int(c.get("x", px)) - px), abs(int(c.get("y", py)) - py))
            if distance > limit:
                continue
            out.append(c)
        return out

    def _build_lure_snapshot(self):
        """Leitura de mundo para o Perfect Lure (pokemon + monstros elegiveis)."""
        from modules.perfect_lure import LureSnapshot
        player = self._player_pos()
        pz = self._player_z()
        creatures = self._get_creatures(player) if player else []
        self._scratch["player"] = player
        self._scratch["creatures"] = creatures

        hp = None
        if self.resolver is not None:
            try:
                hp = self.resolver.read_entry("pokemon.hp_percent")
            except Exception:  # noqa: BLE001
                hp = None

        ident = self._resolve_lure_identity(creatures, player, hp)
        if ident["position_confirmed"]:
            px, py, poz = ident["x"], ident["y"], ident["z"]
            if poz is not None:
                self._last_pokemon_pos = (int(px), int(py), int(poz))
                self._last_pokemon_pos_at = time.monotonic()
        else:
            px = py = poz = None
        floor_z = poz if poz is not None else pz
        monsters = self._eligible_lure_monsters(creatures, player, floor_z)
        hp_confirmed = bool(ident["hp_confirmed"])
        return LureSnapshot(
            valid=player is not None,
            position_confirmed=bool(ident["position_confirmed"]),
            hp_confirmed=hp_confirmed,
            monsters=tuple(monsters),
            pokemon_x=px, pokemon_y=py, pokemon_z=poz,
            pokemon_hp_percent=ident["hp"] if hp_confirmed else None,
            pokemon_key=ident["key"],
            unavailable_reason="" if ident["position_confirmed"] else ident["reason"],
        )

    def active_pokemon_position(self):
        """Ultima posicao confirmada do Pokemon ativo (para o Lure dinamico)."""
        now = time.monotonic()
        if (self._last_pokemon_pos is not None
                and now - self._last_pokemon_pos_at <= POKEMON_POSITION_TTL_S):
            return self._last_pokemon_pos
        # Cache expirado: revalida identidade/posicao em vez de devolver leitura velha.
        player = self._player_pos()
        if player is None:
            return None
        creatures = self._get_creatures(player)
        hp = self._lure_hp_percent()
        ident = self._resolve_lure_identity(creatures, player, hp)
        if not ident.get("position_confirmed"):
            return None
        z = ident.get("z")
        if z is None:
            z = self._player_z()
        pos = (int(ident["x"]), int(ident["y"]), int(z or 0))
        self._last_pokemon_pos = pos
        self._last_pokemon_pos_at = now
        return pos

    def pokemon_position_age(self):
        if self._last_pokemon_pos_at <= 0.0:
            return None
        return max(0.0, time.monotonic() - self._last_pokemon_pos_at)

    def active_pokemon_name(self):
        if self._last_pokemon_name:
            return self._last_pokemon_name
        explicit = self._cfg_get("active_pokemon", "")
        if explicit:
            return str(explicit).strip()
        names = self.own_pokemon_names()
        return names[0] if len(names) == 1 else None

    def send_pokemon_move_command(self, target=None) -> bool:
        """Envia o comando de chat que manda o Pokemon para o tile alvo."""
        name = self.active_pokemon_name()
        if not name:
            return False
        template = str(self.ctx.get(
            "walker", "dynamic_lure_move_command", default="{name}, move!"))
        if not template.strip():
            return False
        if target is None:
            target = self._player_pos()
        if target is None:
            return False
        try:
            x, y = int(target[0]), int(target[1])
            z = int(target[2]) if len(target) > 2 and target[2] is not None else 0
        except (TypeError, ValueError, IndexError):
            return False
        try:
            command = template.format(name=name, x=x, y=y, z=z)
        except (KeyError, ValueError):
            command = template
        bus = self.bus
        sender = getattr(bus, "send_text", None)
        if not callable(sender):
            return False
        try:
            return bool(sender(command, priority=8, module=self.name))
        except Exception as exc:  # noqa: BLE001
            log.warning("Comando move do Pokemon falhou: %s", exc)
            return False

    def _load_best_tile_selector(self):
        if not bool(self._cfg_get("best_tile_enabled", True)):
            return None
        if self._best_tile_selector is not None:
            now = time.monotonic()
            if now - self._last_map_refresh >= 5.0:
                self._last_map_refresh = now
                try:
                    self._minimap.refresh_if_changed()
                except Exception:  # noqa: BLE001 - arquivo pode estar sendo escrito
                    pass
            return self._best_tile_selector
        now = time.monotonic()
        if now - self._last_map_retry < 10.0:
            return None
        self._last_map_retry = now
        try:
            from core.minimap_map import OtmmMinimap
            from core.best_tile import BestTileSelector
        except Exception as exc:  # noqa: BLE001
            log.debug("Best tile indisponivel: %s", exc)
            return None
        minimap = OtmmMinimap.load_latest()
        if minimap is None:
            if not self._best_tile_map_warned:
                log.info("Best tile: minimapa do PokeAlliance nao encontrado")
                self._best_tile_map_warned = True
            return None
        self._minimap = minimap
        self._best_tile_selector = BestTileSelector(minimap)
        self._last_map_refresh = time.monotonic()
        return self._best_tile_selector

    def _best_tile_provider(self, _snapshot):
        selector = self._load_best_tile_selector()
        if selector is None:
            return None
        player = self._scratch.get("player") or self._player_pos()
        if player is None:
            return None
        if len(player) < 3:
            z = self._player_z()
            player = (player[0], player[1], 0 if z is None else int(z))
        creatures = self._scratch.get("creatures")
        if creatures is None:
            creatures = self._get_creatures(player)
        radius = int(self._cfg_get("best_tile_radius", 4) or 4)
        margin = int(self._cfg_get("best_tile_wall_margin", 3) or 3)
        tile = selector.select(player, creatures, radius=radius, wall_margin=margin)
        if tile is None:
            return None
        log.info("Best tile: (%d,%d,%d) parede=%d open3=%d open5=%d %s",
                 tile.x, tile.y, tile.z, getattr(tile, "wall_distance", 0),
                 getattr(tile, "open_3x3", 0), getattr(tile, "open_5x5", 0),
                 getattr(tile, "reason", ""))
        return {"x": tile.x, "y": tile.y, "z": tile.z}

    def _heal_after_damage(self) -> bool:
        if not bool(self._cfg_get("post_combat_heal", True)):
            return False
        healer = self.healer
        if healer is None or not hasattr(healer, "force_heal_sequence"):
            return False
        try:
            healer.force_heal_sequence()
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("Healer pos-combate falhou: %s", exc)
            return False

    def _ensure_perfect(self):
        if self._perfect is None:
            from modules.perfect_lure import PerfectLureController
            from modules.target import parse_hotkey
            self._perfect = PerfectLureController(
                snapshot_provider=self._build_lure_snapshot,
                press_combo=self._press_combo,
                walker=self.walker,
                is_connected=lambda: self.resolver is not None,
                stop_event=getattr(self.ctx, "stop_event", None),
                combo_codes=parse_hotkey(self._cfg_get("combo_hotkey", "F5")),
                revive_codes=parse_hotkey(self._cfg_get("revive_hotkey", "Shift+F")),
                combo_command=self._perfect_combo_command(),
                send_text=self._trigger_combo_sequence,
                combo_active=lambda: self._combo_repeater.active,
                best_tile_provider=self._best_tile_provider,
                heal_callback=self._heal_after_damage,
                kill_all_on_damage=bool(self._cfg_get("kill_all_on_damage", True)),
                damage_threshold_pct=int(self._cfg_get("damage_threshold_pct", 1) or 1),
                finish_remaining_count=int(self._cfg_get("finish_remaining_count", 0) or 0),
                force_kill_max_ms=int(self._cfg_get("force_kill_max_ms", 8000) or 8000),
                best_tile_max_wait_ms=int(
                    self._cfg_get("best_tile_max_wait_ms", 2500) or 2500),
            )
        return self._perfect

    def _sync_perfect_settings(self):
        from modules.target import parse_hotkey
        self._perfect.configure(
            enabled=self._perfect_enabled(),
            min_monsters_to_lure=int(self._cfg_any("min_monsters_to_lure", "lure_target_count",
                                                   default=8) or 8),
            lure_radius_sqm=int(self._cfg_any("lure_radius_sqm", default=1) or 1),
            emergency_hp_percent=int(self._cfg_any("emergency_hp_percent", default=40) or 40),
            max_lure_wait_ms=int(self._cfg_any("max_lure_wait_ms", "perfect_lure_wait_ms",
                                               default=2500) or 2500),
            group_stable_ms=int(self._cfg_any("group_stable_ms", default=300) or 300),
            combo_wait_ms=int(self._cfg_get("combo_wait_ms", 1000) or 1000),
            revive_delay_ms=int(self._cfg_get("revive_delay_ms", 800) or 800),
            cooldown_ms=int(self._cfg_get("attack_cooldown_ms", 900) or 900),
            combo_codes=parse_hotkey(self._cfg_get("combo_hotkey", "F5")),
            revive_codes=parse_hotkey(self._cfg_get("revive_hotkey", "Shift+F")),
            combo_command=self._perfect_combo_command(),
            post_combat_revive=bool(self._cfg_get("post_combat_revive", True)),
            best_tile_provider=self._best_tile_provider,
            heal_callback=self._heal_after_damage,
            kill_all_on_damage=bool(self._cfg_get("kill_all_on_damage", True)),
            damage_threshold_pct=int(self._cfg_get("damage_threshold_pct", 1) or 1),
            finish_remaining_count=int(self._cfg_get("finish_remaining_count", 0) or 0),
            force_kill_max_ms=int(self._cfg_get("force_kill_max_ms", 8000) or 8000),
            best_tile_max_wait_ms=int(self._cfg_get("best_tile_max_wait_ms", 2500) or 2500),
        )

    def _lure_hp_percent(self):
        """HP do Pokemon ativo, com fallback para o ultimo HP confirmado."""
        hp = None
        if self.resolver is not None:
            try:
                hp = self.resolver.read_entry("pokemon.hp_percent")
            except Exception:  # noqa: BLE001
                hp = None
        try:
            value = int(hp)
        except (TypeError, ValueError):
            value = None
        if value is not None and 0 < value <= 100:
            return value
        if (self._lure_identity_hp is not None
                and time.monotonic() - self._lure_identity_at <= LURE_IDENTITY_TTL_S):
            return self._lure_identity_hp
        return hp

    def _run_lure_combat(self, lure_count: int) -> bool:
        from modules.target import CombatController
        player = self._player_pos()
        if player is None:
            return False
        entities = self._get_creatures(player)
        controller = CombatController(
            entities_provider=lambda: self._get_creatures(self._player_pos()),
            player_provider=self._player_pos,
            press_combo=self._press_combo,
            send_text=getattr(self.bus, "send_text", None),
            player_hp_provider=self._lure_hp_percent,
            walker=self.walker,
            stop_event=getattr(self.ctx, "stop_event", None),
            lure_target_count=lure_count,
            lure_radius=int(self._cfg_get("lure_radius", 7) or 7),
            perfect_lure=bool(self._cfg_get("perfect_lure", True)),
            perfect_lure_distance=int(self._cfg_get("perfect_lure_distance", 3) or 3),
            perfect_lure_wait_ms=int(self._cfg_get("perfect_lure_wait_ms", 1000) or 1000),
            attack_range=int(self._cfg_get("attack_range_tiles", 7) or 7),
            combo_hotkey=self._cfg_get("combo_hotkey", "F5"),
            combo_command=self._perfect_combo_command(),
            focus_shiny=bool(self._cfg_get("focus_shiny", True)),
            revive_hotkey=self._cfg_get("revive_hotkey", "Shift+F"),
            revive_delay_ms=int(self._cfg_get("revive_delay_ms", 800) or 800),
            combo_wait_ms=int(self._cfg_get("combo_wait_ms", 1000) or 1000),
            max_combat_loops=int(self._cfg_get("max_combat_loops", 5) or 5),
            post_combat_revive=bool(self._cfg_get("post_combat_revive", True)),
            prioritize=self._cfg_get("prioritize", "nearest"),
            blacklist=self._cfg_get("name_blacklist", []) or [],
            own_names=self.own_pokemon_names(),
        )
        return controller.run(entities=entities, player=player)

    def _pause_route_for_combat(self, target: dict) -> None:
        """Freia a rota enquanto o alvo estiver ao alcance."""
        self._last_target_seen = time.monotonic()
        if self._combat_paused:
            return
        pause = getattr(self.walker, "pause_for_combat", None)
        if not callable(pause):
            return
        pause()
        self._combat_paused = True
        if hasattr(self.ctx, "set"):
            self.ctx.set(True, "combat", "target_active")
        log.info("AUTO_TARGET: rota pausada para combate (%s)",
                 target.get("name") or "alvo")

    def _resume_route_after_combat(self, now: float | None = None, force: bool = False) -> None:
        """Retoma a rota apos o combate, respeitando o atraso configurado."""
        if not self._combat_paused:
            return
        now = time.monotonic() if now is None else float(now)
        if not force:
            delay = float(self._cfg_get("combat_resume_delay_ms", 1500) or 0) / 1000.0
            if now - self._last_target_seen < delay:
                return
        resume = getattr(self.walker, "resume_route", None)
        if callable(resume):
            resume()
        self._combat_paused = False
        if hasattr(self.ctx, "set"):
            self.ctx.set(False, "combat", "target_active")
        log.info("AUTO_TARGET: rota retomada apos combate")

    def stop(self):
        self._combo_repeater.cancel()
        if hasattr(self.ctx, "set"):
            self.ctx.set(False, "combat", "target_active")
        self._resume_route_after_combat(force=True)
        super().stop()

    def tick(self):
        if not self.resolver:
            return
        now = time.monotonic()
        stopped = getattr(self.ctx, "is_stopped", None)
        paused = getattr(self.ctx, "is_paused", None)
        if (callable(stopped) and stopped()) or (callable(paused) and paused()):
            decision = ActionDecision(ActionKind.NONE, "pausa manual/stop")
            self._log_decision(decision)
            return
        self._scratch = {}
        self._combo_repeater.tick(now)
        if (not self._perfect_enabled() and self.walker is not None
                and getattr(self.walker, "distance_guard_enabled", False)):
            self.active_pokemon_position()
        decision = self._decide_action(now)
        self._log_decision(decision)
        self._execute_decision(decision, now)

    def _decide_action(self, now: float) -> ActionDecision:
        if self._perfect_enabled():
            controller = self._ensure_perfect()
            self._sync_perfect_settings()
            status = controller.tick(now)
            self._last_lure_status = status
            if status.handled:
                mapping = {
                    "luring": ActionKind.WAIT_GROUP,
                    "moving_to_tile": ActionKind.DETOUR,
                    "waiting_for_group": ActionKind.WAIT_GROUP,
                    "combo_pending": ActionKind.WAIT_PENDING,
                    "executing_combo": ActionKind.COMBO,
                    "force_killing": ActionKind.COMBO,
                    "healing": ActionKind.HEAL,
                    "cooldown": ActionKind.WAIT_PENDING,
                }
                kind = mapping.get(str(status.state), ActionKind.WAIT_PENDING)
                return ActionDecision(kind, f"lure:{status.state}")
            player = self._scratch.get("player")
            creatures = self._scratch.get("creatures")
            if player is None:
                player = self._player_pos()
            if creatures is None:
                creatures = self._get_creatures(player)
        else:
            if int(self._cfg_get("lure_target_count", 0) or 0) > 0:
                self._log_legacy_lure_once()
            player = self._player_pos()
            creatures = self._get_creatures(player)

        target = self._select_target(creatures, player)
        if not target:
            return ActionDecision(ActionKind.RELEASE_COMBAT, "sem alvo")
        creature_id = target.get("creature_id")
        if creature_id != self._current_target_id:
            return ActionDecision(ActionKind.SELECT_TARGET, "alvo novo", target)
        cd = self.ctx.get("auto_target", "attack_cooldown_ms", default=900) / 1000.0
        if now - self._last_attack < cd:
            return ActionDecision(ActionKind.NONE, "cooldown", target)
        if now - self._target_selected_at < self._target_select_delay:
            return ActionDecision(ActionKind.WAIT_PENDING, "aguardando selecao", target)
        return ActionDecision(ActionKind.ATTACK, "alvo pronto", target)

    def _execute_decision(self, decision: ActionDecision, now: float) -> None:
        if decision.kind == ActionKind.NONE:
            return
        if decision.kind == ActionKind.RELEASE_COMBAT:
            self._combo_repeater.cancel()
            self._resume_route_after_combat(now)
            return
        if decision.kind in (ActionKind.WAIT_GROUP, ActionKind.DETOUR, ActionKind.COMBO,
                             ActionKind.WAIT_PENDING, ActionKind.HEAL):
            if hasattr(self.ctx, "set"):
                self.ctx.set(True, "combat", "target_active")
            self._ensure_target_selected(now)
            if self.walker is not None:
                self._combat_paused = bool(getattr(self.walker, "combat_paused", False))
            return
        target = decision.target
        if not target:
            return
        if decision.kind == ActionKind.SELECT_TARGET:
            self._pause_route_for_combat(target)
            self._ensure_target_selected(now, target)
            return
        if decision.kind == ActionKind.ATTACK:
            self._execute_attack(now, target)

    def _execute_attack(self, now: float, target: dict) -> None:
        self._pause_route_for_combat(target)
        cd = self.ctx.get("auto_target", "attack_cooldown_ms", default=900) / 1000.0
        if now - self._last_attack < cd:
            return
        target = self._ensure_target_selected(now, target)
        if target is None or now - self._target_selected_at < self._target_select_delay:
            return
        creature_id = target.get("creature_id")
        if creature_id is None:
            return
        hp_now = target.get("hp_percent")
        log.info("AUTO_TARGET: alvo solicitado %s (%s) hp=%s",
                 target.get("name"), creature_id, hp_now)
        previous_hp = self._last_attack_hp
        if (creature_id == self._last_attack_target_id
                and hp_now == self._last_attack_hp
                and now - self._last_attack_hp_at >= 2.5):
            log.info("AUTO_TARGET: alvo %s sem dano; reselecionando", creature_id)
            self.press(self._target_hotkey_vk)
            self._target_selected_at = now
            self._last_attack_hp_at = now
        elif (creature_id != self._last_attack_target_id
              or hp_now != self._last_attack_hp):
            self._last_attack_target_id = creature_id
            self._last_attack_hp = hp_now
            self._last_attack_hp_at = now
        if self._should_combo_attack():
            accepted = self._trigger_combo_sequence()
        else:
            accepted = self.press(self._attack_hotkey_vk)
        if previous_hp is not None and hp_now != previous_hp:
            log.info("AUTO_TARGET: progresso observado no alvo %s: HP %s -> %s",
                     creature_id, previous_hp, hp_now)
        log.info("AUTO_TARGET: comando de ataque enviado creature=%d hp=%s accepted=%s "
                 "(execucao no cliente nao confirmada)", creature_id, hp_now, bool(accepted))
        self._last_attack = now
        self._current_target_id = creature_id
        self.ctx.inc_stat("creatures_killed")

    def _log_decision(self, decision: ActionDecision) -> None:
        previous = self._last_decision
        cid = (decision.target or {}).get("creature_id")
        key = (decision.kind, decision.reason, cid)
        old_key = None if previous is None else (
            previous.kind, previous.reason, (previous.target or {}).get("creature_id"))
        if key != old_key:
            log.info("AUTO_TARGET decisao: %s -> %s; motivo=%s; alvo=%s",
                     old_key[0].value if old_key else "none",
                     decision.kind.value, decision.reason, cid)
        self._last_decision = decision

    def _log_legacy_lure_once(self) -> None:
        if getattr(self, "_legacy_lure_warned", False):
            return
        self._legacy_lure_warned = True
        log.info("AUTO_TARGET: lure legado bloqueante desativado no tick; "
                 "use Perfect Lure para agrupar")

    def _get_creatures(self, player=None) -> list[dict]:
        """Le as criaturas ao redor do player via EntityReader (fast path)."""
        if self._reader is None:
            return []
        if player is None:
            player = self._player_pos()
        if player is None:
            return []
        px, py = player
        try:
            entities = self._reader.scan(px, py)
        except Exception as exc:  # noqa: BLE001 - leitura ao vivo pode falhar
            log.debug("EntityReader falhou: %s", exc)
            return []
        return [{"name": e.name, "x": e.x, "y": e.y, "z": e.z,
                 "creature_id": e.creature_id if e.creature_id is not None else e.anchor,
                 "hp_percent": e.hp_percent, "anchor": e.anchor, "shiny": e.shiny,
                 "is_npc": e.is_npc, "is_player": e.is_player, "is_dead": e.is_dead}
                for e in entities]

    def _ensure_target_selected(self, now: float, target: dict | None = None):
        """Seleciona o alvo no cliente com a hotkey de SWITCH_TARGET.

        O jogo so aceita skills quando existe um alvo; o Target antigo apertava
        a skill direto e recebia "precisa de um alvo". Retorna o alvo escolhido.
        """
        if target is None:
            player = self._scratch.get("player") or self._player_pos()
            creatures = self._scratch.get("creatures")
            if creatures is None:
                creatures = self._get_creatures(player)
            target = self._select_target(creatures, player)
        if not target:
            return None
        creature_id = target.get("creature_id")
        if creature_id is None:
            return target
        if creature_id != self._current_target_id:
            if now - self._last_target_select_at < 0.25:
                return target
            self._last_target_select_at = now
            self._target_selected_at = now
            self._current_target_id = creature_id
            self.press(self._target_hotkey_vk)
            log.info("AUTO_TARGET: solicitando alvo (Tab) %s (%s); "
                     "confirmacao no cliente indisponivel",
                     target.get("name"), creature_id)
        return target

    def _select_target(self, creatures: list[dict], player=None) -> dict | None:
        if not creatures:
            return None

        if player is None:
            player = self._player_pos()
        if player:
            px, py = player[0], player[1]
        else:
            px = py = None
        floor = None
        if player is not None and len(player) > 2 and player[2] is not None:
            floor = int(player[2]) & 0xFF
        else:
            pz = self._player_z()
            floor = None if pz is None else (int(pz) & 0xFF)
        limit = int(self.ctx.get("auto_target", "attack_range_tiles", default=7) or 7)
        whitelist = {n.lower() for n in (self.ctx.get("auto_target", "name_whitelist", default=[]) or [])}
        blacklist = {n.lower() for n in (self.ctx.get("auto_target", "name_blacklist", default=[]) or [])}
        # Nunca atacar os proprios pokemons do roster (ex.: o follower Togepi)
        roster = self.ctx.get("pokemon_roster", default={}) or {}
        if isinstance(roster, dict):
            blacklist |= {str(v).lower() for v in roster.values() if v}

        # Apply filters
        filtered = []
        for c in creatures:
            if self.ctx.get("auto_target", "ignore_players", default=True) and c.get("is_player"):
                continue
            if self.ctx.get("auto_target", "ignore_npcs", default=True) and c.get("is_npc"):
                continue
            if self.ctx.get("auto_target", "ignore_dead", default=True) and c.get("is_dead"):
                continue
            hp_pct = c.get("hp_percent", 100)
            if hp_pct <= 0:
                continue
            if floor is not None and c.get("z") is not None:
                if (int(c.get("z")) & 0xFF) != floor:
                    continue
            name = (c.get("name") or "").lower()
            if whitelist and name not in whitelist:
                continue
            if name in blacklist:
                continue
            # Range check (Chebyshev) quando ha posicao do player
            if px is not None and py is not None:
                distance = max(abs(c.get("x", px) - px), abs(c.get("y", py) - py))
                if distance > limit:
                    continue
                c = {**c, "distance": distance}
            filtered.append(c)

        if not filtered:
            return None

        # Prioritize. focus_shiny e prioridade, nao exclusividade: sem shiny
        # na tela o Target continua atacando os monstros comuns.
        mode = self.ctx.get("auto_target", "prioritize", default="lowest_hp_percent")
        shiny_first = self._focus_shiny_enabled()
        if mode == "lowest_hp_percent":
            filtered.sort(key=lambda c: (
                (not bool(c.get("shiny"))) if shiny_first else False,
                c.get("hp_percent", 100), c.get("distance", 0)))
        elif mode == "nearest":
            filtered.sort(key=lambda c: (
                (not bool(c.get("shiny"))) if shiny_first else False,
                c.get("distance", 0)))
        elif mode == "highest_level":
            filtered.sort(key=lambda c: (
                (not bool(c.get("shiny"))) if shiny_first else False,
                -int(c.get("level", 1) or 1)))

        return filtered[0]
