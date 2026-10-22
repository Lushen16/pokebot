# pokebot/core/combo_repeater.py
"""Repetidor de combo com intervalo, usado pelo Target/Perfect Lure.

O cliente pode perder o primeiro comando de AutoCombo (chat fechando, lag ou
alvo trocando). Em vez de mandar uma vez so, o repetidor dispara o combo N
vezes em intervalos configuraveis.
"""

from __future__ import annotations

import time
from typing import Callable, Optional


class ComboRepeater:
    def __init__(
        self,
        send: Callable[[], bool],
        *,
        count: int = 3,
        interval: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.send = send
        self.count = max(1, min(10, int(count)))
        self.interval = max(0.5, min(60.0, float(interval)))
        self.clock = clock
        self._remaining = 0
        self._next_at = 0.0
        self._last_error_at = 0.0
        self._last_send_ok = True

    @property
    def remaining(self) -> int:
        return self._remaining

    @property
    def active(self) -> bool:
        return self._remaining > 0

    def trigger(self, now: Optional[float] = None) -> bool:
        """Inicia uma sequencia se nao houver uma ativa. Nao reinicia no meio."""
        now = self.clock() if now is None else float(now)
        if self._remaining > 0:
            return self._last_send_ok
        self._remaining = self.count
        self._next_at = now
        self.tick(now)
        return self._last_send_ok

    def tick(self, now: Optional[float] = None) -> int:
        now = self.clock() if now is None else float(now)
        sent = 0
        while self._remaining > 0 and now >= self._next_at:
            try:
                ok = bool(self.send())
            except Exception:  # noqa: BLE001 - envio de input nao pode derrubar o tick
                ok = False
            if not ok:
                self._last_send_ok = False
                self._last_error_at = now
                self._next_at = now + 0.5
                break
            self._last_send_ok = True
            self._remaining -= 1
            sent += 1
            self._next_at = now + self.interval
        return sent

    def cancel(self) -> None:
        self._remaining = 0
        self._next_at = 0.0


__all__ = ["ComboRepeater"]
