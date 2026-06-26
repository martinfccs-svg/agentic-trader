"""Per-system kill switch (v6). Feeds are now price-only (quote + candle), both
PRICE-critical, so any feed loss is an emergency. Also enforces the daily loss
limit as a halt condition.
"""

from __future__ import annotations

import logging
from typing import Callable

from config import DAILY_LOSS_LIMIT, ENDPOINTS, SYSTEM_REQUIRED_FEEDS
from models import FeedCriticality, System

log = logging.getLogger("kill_switch")


class KillSwitch:
    def __init__(self, feed, broker):
        self._feed, self._broker = feed, broker
        self._on_price_lost: dict[System, Callable[[str], None]] = {}

    def register_price_loss_handler(self, system, handler):
        self._on_price_lost[system] = handler

    def _down(self, system, crit):
        return [k for k in SYSTEM_REQUIRED_FEEDS[system]
                if ENDPOINTS[k].criticality is crit and not self._feed.health(k).is_available]

    def may_open(self, system):
        if self._broker.realized_today <= -abs(DAILY_LOSS_LIMIT):
            log.error("%s: daily loss limit hit (%.2f) -> halting entries.",
                      system.value, self._broker.realized_today)
            return False
        if self._down(system, FeedCriticality.PRICE):
            log.error("%s: PRICE feed down -> halting entries (emergency).", system.value)
            return False
        return True

    def check_emergencies(self):
        for system in SYSTEM_REQUIRED_FEEDS:
            if self._down(system, FeedCriticality.PRICE) and system in self._on_price_lost:
                self._on_price_lost[system](f"price feed lost for {system.value}")
