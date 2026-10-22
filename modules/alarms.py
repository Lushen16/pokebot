# pokebot/modules/alarms.py
"""Alarms module: death/disconnect/chat GM detection with sound/webhook."""

from __future__ import annotations
import logging
import re
import threading
import time
import winsound
from collections import deque
from modules.base import Module
from core.chat_snapshot import ChatEvent, ChatSnapshotReader

log = logging.getLogger("pokebot.modules.alarms")


def _compile_gm_patterns(patterns):
    compiled = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern, re.IGNORECASE))
        except re.error as exc:
            log.warning("padrão GM inválido %r: %s", pattern, exc)
    return compiled


class Alarms(Module):
    name = "alarms"
    config_key = "alarms"

    def __init__(self, ctx=None, bus=None, resolver=None):
        super().__init__(ctx, bus)
        self.resolver = resolver
        self._was_online = True
        self._was_dead = False
        self._last_gm_check = 0.0
        self._disconnect_grace = 0.0
        gm_source = self.ctx.get("alarms", "gm_name_patterns", default=[])
        self._gm_patterns = _compile_gm_patterns(gm_source)
        self._gm_pattern_source = tuple(gm_source)
        self._chat_stop = threading.Event()
        self._chat_thread: threading.Thread | None = None
        self._chat_reader: ChatSnapshotReader | None = None
        self._chat_last_address = 0
        self._chat_status: dict = {"connected": False, "tick": 0, "events": 0,
                                   "last_error": None, "active": False}
        self._recent_chat: deque[dict] = deque(maxlen=25)

    def start(self):
        super().start()
        if self.enabled:
            self._start_chat_worker()

    def stop(self):
        if hasattr(self, "_chat_stop"):
            self._stop_chat_worker()
        super().stop()

    def tick(self):
        if not self.resolver:
            return

        self._ensure_chat_worker()
        import time
        now = time.monotonic()

        # Death check
        if self.ctx.get("alarms", "beep_on", default=[]):
            dead = self.resolver.read_optional_field("player", "is_dead")
            if dead and not self._was_dead:
                log.warning("ALARM: Death detected!")
                self._was_dead = True
                self._trigger("death")
            elif dead is not None and not dead:
                self._was_dead = False

        # Disconnect check
        online = self.resolver.read_optional_field("player", "is_online")
        if online is not None:
            if not online and self._was_online:
                if self._disconnect_grace == 0:
                    self._disconnect_grace = now + self.ctx.get("alarms", "disconnect_grace_seconds", default=8)
                elif now > self._disconnect_grace:
                    log.warning("ALARM: Disconnect detected!")
                    self._trigger("disconnect")
                    self._disconnect_grace = 0
            elif online:
                self._was_online = True
                self._disconnect_grace = 0

        # GM detection
        self._refresh_gm_patterns()
        if self._gm_patterns:
            scan_radius = self.ctx.get("alarms", "gm_scan_radius_tiles", default=12)
            if now - self._last_gm_check > 5.0:  # check every 5s
                self._last_gm_check = now
                if self._check_gm(scan_radius):
                    log.warning("ALARM: GM detected!")
                    self._trigger("gm_detected")

        # Low HP warning
        low_hp = self.ctx.get("alarms", "low_hp_percent", default=20)
        hp = self.resolver.read_field("player", "hp")
        hp_max = self.resolver.read_field("player", "hp_max")
        if hp is not None and hp_max and hp_max > 0:
            hp_pct = (hp * 100) // hp_max
            if hp_pct <= low_hp:
                self._trigger("low_hp")

    def _ensure_chat_worker(self):
        if not hasattr(self, "_chat_stop"):
            return
        if not self.ctx.get("alarms", "chat_enabled", default=True):
            return
        thread = getattr(self, "_chat_thread", None)
        if thread is not None and thread.is_alive():
            return
        self._start_chat_worker()

    def _start_chat_worker(self):
        if self._chat_thread is not None and self._chat_thread.is_alive():
            return
        self._chat_stop.clear()
        self._chat_thread = threading.Thread(target=self._chat_loop, name="alarms-chat", daemon=True)
        self._chat_thread.start()

    def _stop_chat_worker(self):
        self._chat_stop.set()
        thread = self._chat_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)
        self._chat_thread = None

    def _chat_loop(self):
        proc = getattr(self.resolver, "proc", None) if self.resolver is not None else None
        if proc is None:
            log.debug("chat worker sem processo")
            return
        interval = max(1.0, float(self.ctx.get("alarms", "chat_full_scan_interval_seconds", default=10)))
        self._chat_reader = ChatSnapshotReader(
            proc, full_scan_interval=interval, preferred_address=self._chat_last_address)
        while not self._chat_stop.is_set():
            if not self.ctx.get("alarms", "chat_enabled", default=True):
                self._chat_stop.wait(0.5)
                continue
            try:
                events = self._chat_reader.poll()
                status = dict(self._chat_reader.status)
                status["active"] = True
                self._chat_status = status
                if status.get("address"):
                    self._chat_last_address = int(status["address"])
                for event in events:
                    self._handle_chat_event(event)
            except Exception as exc:
                log.exception("chat poll falhou: %s", exc)
                self._chat_status = {"connected": False, "tick": 0, "events": 0,
                                     "last_error": str(exc), "active": False}
            self._chat_stop.wait(max(0.2, float(self.ctx.get("alarms", "chat_poll_ms", default=500))) / 1000.0)

    def _handle_chat_event(self, event: ChatEvent):
        if event.is_creature:
            return
        if event.self and self.ctx.get("alarms", "chat_ignore_self", default=True):
            return
        gm = self._is_gm_name(event.name)
        if event.is_trade and not gm and not self.ctx.get("alarms", "chat_include_trade", default=False):
            return
        if gm:
            if not self.ctx.get("alarms", "chat_gm_enabled", default=True):
                return
        elif not self.ctx.get("alarms", "chat_player_enabled", default=True):
            return

        label = "GM" if gm else "JOGADOR"
        detail = f"[{label}] {event.name}: {event.text}"
        log.warning("CHAT %s", detail)
        self._recent_chat.append({
            "time": time.strftime("%H:%M:%S"),
            "gm": gm,
            "name": event.name,
            "text": event.text,
            "mode": event.mode,
        })
        if self.ctx.get("alarms", "chat_beep", default=True):
            self._beep_once()
        if self.ctx.get("alarms", "chat_webhook", default=False):
            self._send_webhook("chat_gm" if gm else "chat_player", detail)

    def _is_gm_name(self, name: str) -> bool:
        self._refresh_gm_patterns()
        return any(pattern.search(name) for pattern in self._gm_patterns)

    def _refresh_gm_patterns(self):
        source = tuple(self.ctx.get("alarms", "gm_name_patterns", default=[]))
        if source == getattr(self, "_gm_pattern_source", None):
            return
        self._gm_pattern_source = source
        self._gm_patterns = _compile_gm_patterns(source)

    def _check_gm(self, radius: int) -> bool:
        """Scan nearby creatures for GM name patterns. Placeholder."""
        # PLACEHOLDER — needs entity list offsets, not yet reversed
        # TODO: reverse entity enumeration, names and positions before matching nearby GM names.
        # Requires creature_list block calibrated
        return False

    def _trigger(self, event: str):
        beep_on = self.ctx.get("alarms", "beep_on", default=[])
        webhook_on = self.ctx.get("alarms", "webhook_on", default=[])

        if event in beep_on:
            self._beep()

        if event in webhook_on:
            self._send_webhook(event)

    def _beep(self):
        pattern = self.ctx.get("alarms", "beep_pattern", default={"count": 5, "interval_ms": 180})
        count = pattern.get("count", 5)
        interval = pattern.get("interval_ms", 180)
        for _ in range(count):
            winsound.Beep(1000, 100)
            time.sleep(interval / 1000.0)

    def _beep_once(self):
        try:
            winsound.Beep(1200, 140)
        except Exception:
            pass

    def _send_webhook(self, event: str, detail: str | None = None):
        if not self.ctx.get("notify", "webhook_enabled", default=False):
            return
        url = self.ctx.get("notify", "webhook_url", default="")
        if not url:
            return
        # Fire and forget in background
        threading.Thread(target=self._post_webhook, args=(event, url, detail), daemon=True).start()

    def _post_webhook(self, event: str, url: str, detail: str | None = None):
        try:
            import urllib.request
            import json
            content = f"🚨 **{event.upper()}** detected!"
            if detail:
                content = f"🚨 **{event.upper()}** {detail}"
            data = {
                "username": self.ctx.get("notify", "webhook_username", default="PokeAlliance Bot"),
                "content": content,
            }
            req = urllib.request.Request(
                url,
                data=json.dumps(data).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=self.ctx.get("notify", "timeout_seconds", default=8))
        except Exception as exc:
            log.debug("Webhook failed: %s", exc)

    @property
    def recent_chat(self) -> list[dict]:
        return list(self._recent_chat)

    @property
    def chat_status(self) -> dict:
        return dict(self._chat_status)
