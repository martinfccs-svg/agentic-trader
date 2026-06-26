"""Per-system kill switch (working version).

SIGNAL feed down -> may_open False; keep managing open positions.
PRICE feed down  -> emergency; fire the registered handler (intraday flatten).
Also enforces the daily loss limit as a halt condition.
"""

from __future__ import annotations

import logging
from typing import Callable

from config import DAILY_LOSS_LIMIT, ENDPOINTS, SYSTEM_REQUIRED_FEEDS
from models import FeedCriticality, System

log = logging.getLogger("kill_switch")


class KillSwitch:
    def __init__(self, feed, broker) -> None:
        self._feed = feed
        self._broker = broker
        self._on_price_lost: dict[System, Callable[[str], None]] = {}

    def register_price_loss_handler(self, system: System, handler: Callable[[str], None]) -> None:
        self._on_price_lost[system] = handler

    def _down(self, system: System, crit: FeedCriticality) -> list[str]:
        return [k for k in SYSTEM_REQUIRED_FEEDS[system]
                if ENDPOINTS[k].criticality is crit and not self._feed.health(k).is_available]

    def may_open(self, system: System) -> bool:
        if self._broker.realized_today <= -abs(DAILY_LOSS_LIMIT):
            log.error("%s: daily loss limit hit (%.2f) -> halting entries.",
                      system.value, self._broker.realized_today)
            return False
        if self._down(system, FeedCriticality.PRICE):
            log.error("%s: PRICE feed down -> halting entries (emergency).", system.value)
            return False
        if self._down(system, FeedCriticality.SIGNAL):
            log.warning("%s: signal feed down -> halting new entries, still managing.",
                        system.value)
            return False
        return True

    def check_emergencies(self) -> None:
        for system in SYSTEM_REQUIRED_FEEDS:
            if self._down(system, FeedCriticality.PRICE) and system in self._on_price_lost:
                self._on_price_lost[system](f"price feed lost for {system.value}")
