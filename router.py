"""Router: one source -> one system, looked up from a frozen table.

Benching-aware (2026-07-08): the ENABLED_SYSTEMS profile means some systems
legitimately have no engine at boot. The router warns once per benched
system instead of refusing to start, and drops any stray signal addressed
to a benched system instead of crashing the cycle with a KeyError.

The distinction is kept deliberately: a signal for a system that was never
wired is EXPECTED (benched — drop quietly with a warning); anything else
missing from the table is still a real wiring bug and logged as an error.
"""
from __future__ import annotations

import logging

from config import SOURCE_TO_SYSTEM
from models import Signal, System

log = logging.getLogger("router")


class SignalRouter:
    def __init__(self, engines: dict[System, object]) -> None:
        self._engines = engines
        # Systems in the routing table with no engine are benched, not
        # misconfigured. Record them so route() can tell an expected drop
        # from an impossible one.
        self._benched: set[System] = set()
        for system in SOURCE_TO_SYSTEM.values():
            if system not in engines:
                self._benched.add(system)
                log.warning("router: %s has no engine wired (benched) — "
                            "its signals will be dropped", system.value)

    def route(self, signal: Signal) -> None:
        system = SOURCE_TO_SYSTEM.get(signal.source)
        if system is None:
            log.error("Unmapped source %s (%s); dropping.",
                      signal.source, signal.ticker)
            return
        engine = self._engines.get(system)
        if engine is None:
            if system in self._benched:
                # Defensive: scanners for benched systems shouldn't run,
                # but a stray signal must be dropped, not crash the cycle.
                log.warning("router: dropping %s signal for benched "
                            "system %s", signal.ticker, system.value)
            else:
                # Enabled-but-missing is a genuine wiring bug — loud.
                log.error("router: NO ENGINE for enabled system %s "
                          "(signal %s dropped) — check build()",
                          system.value, signal.ticker)
            return
        engine.handle_signal(signal)
