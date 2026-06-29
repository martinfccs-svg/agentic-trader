"""Router: one source -> one system, looked up from a frozen table."""

from __future__ import annotations

import logging

from config import SOURCE_TO_SYSTEM
from models import Signal, System

log = logging.getLogger("router")


class SignalRouter:
    def __init__(self, engines: dict[System, object]) -> None:
        self._engines = engines
        for system in SOURCE_TO_SYSTEM.values():
            if system not in engines:
                raise ValueError(f"No engine wired for {system}")

    def route(self, signal: Signal) -> None:
        system = SOURCE_TO_SYSTEM.get(signal.source)
        if system is None:
            log.error("Unmapped source %s (%s); dropping.", signal.source, signal.ticker)
            return
        self._engines[system].handle_signal(signal)
