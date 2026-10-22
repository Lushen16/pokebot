# pokebot/engine.py
"""Module engine: lifecycle management for all bot modules."""

from __future__ import annotations
import logging
import threading
import time
from typing import Any, Optional

from context import get_context, BotContext
from inputbus import get_bus, shutdown_bus
from modules.base import Module
from modules.healer import Healer
from modules.walker import Walker
from modules.auto_food import AutoFood
from modules.cavebot import Cavebot
from modules.auto_target import AutoTarget
from modules.auto_catch import AutoCatch
from modules.alarms import Alarms
from navigator import Navigator

log = logging.getLogger("pokebot.engine")


class Engine:
    """Orchestrates all modules, resolver, input bus, and walker."""

    def __init__(
        self,
        config_path: Optional[str] = None,
        resolver: Any = None,
        memory_reader: Any = None,
        offset_registry: Any = None,
        input_bus: Any = None,
    ) -> None:
        self.ctx: BotContext = get_context(config_path)
        self.resolver = resolver
        self.memory = memory_reader if memory_reader is not None else (resolver.proc if resolver else None)
        self.offsets = offset_registry if offset_registry is not None else resolver
        self.bus = input_bus if input_bus is not None else get_bus()
        self.navigator = Navigator(resolver=resolver, ctx=self.ctx, bus=self.bus)

        self.modules: dict[str, Any] = {}
        self._init_modules()

    def _init_modules(self) -> None:
        # Walker antes dos modulos: o Target precisa dele para pausar/retomar o combate
        self.walker = Walker(
            memory_reader=self.memory,
            offset_registry=self.offsets,
            input_bus=self.bus,
            movement_keys=self.ctx.get("cavebot", "walk_keys", default=None),
        )

        # Order matters: healer first (highest priority), alarms last (monitoring)
        self.healer = Healer(ctx=self.ctx, bus=self.bus, resolver=self.resolver)
        self.modules["healer"] = self.healer
        self.modules["auto_food"] = AutoFood(ctx=self.ctx, bus=self.bus, resolver=self.resolver)
        self.modules["cavebot"] = Cavebot(ctx=self.ctx, bus=self.bus, resolver=self.resolver)
        self.modules["auto_target"] = AutoTarget(ctx=self.ctx, bus=self.bus,
                                                 resolver=self.resolver, walker=self.walker,
                                                 healer=self.healer)
        self.modules["auto_catch"] = AutoCatch(ctx=self.ctx, bus=self.bus, resolver=self.resolver)
        self.modules["alarms"] = Alarms(ctx=self.ctx, bus=self.bus, resolver=self.resolver)
        self.modules["walker"] = self.walker

    def start(self) -> None:
        self.bus.start()
        self.ctx.stop_event.clear()
        self.ctx.stats["uptime_start"] = time.time()
        for name, mod in self.modules.items():
            mod.start()
        log.info("Engine started with %d modules", len(self.modules))

    def stop(self) -> None:
        log.info("Stopping engine...")
        self.ctx.stop()
        self.ctx.stats["uptime_start"] = None
        for name, mod in self.modules.items():
            try:
                mod.stop()
            except Exception as exc:
                log.exception("Error stopping module %s: %s", name, exc)
        shutdown_bus()
        log.info("Engine stopped")

    def stop_all(self) -> None:
        """Alias for stop() for lifecycle control."""
        self.stop()

    def pause(self) -> None:
        self.ctx.pause()
        log.info("Engine paused")

    def resume(self) -> None:
        self.ctx.resume()
        log.info("Engine resumed")

    def reload_config(self) -> None:
        self.ctx.reload_config()
        for mod in self.modules.values():
            if hasattr(mod, "on_config_change"):
                mod.on_config_change()
        log.info("Config reloaded")

    def get_module(self, name: str) -> Optional[Any]:
        return self.modules.get(name)

    def get_status(self) -> dict:
        return {
            "running": not self.ctx.is_stopped(),
            "paused": self.ctx.is_paused(),
            "modules": {
                name: getattr(mod, "enabled", getattr(mod, "is_enabled", False))
                for name, mod in self.modules.items()
            },
            "stats": self.ctx.get_stats(),
            "navigator": self.navigator.get_progress(),
        }


def create_engine(
    config_path: Optional[str] = None,
    resolver: Any = None,
    memory_reader: Any = None,
    offset_registry: Any = None,
    input_bus: Any = None,
) -> Engine:
    return Engine(
        config_path=config_path,
        resolver=resolver,
        memory_reader=memory_reader,
        offset_registry=offset_registry,
        input_bus=input_bus,
    )
