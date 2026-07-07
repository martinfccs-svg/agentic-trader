"""Per-system kill switch (v6.1). Feeds are price-only (quote + candle), both
PRICE-critical, so any feed loss is an emergency. Also enforces the daily loss
limit as a halt condition.

v6.1 fix (2026-07-07 afternoon incident): "feed down" is now defined by the
circuit breaker's FAILURE state, not by success recency.

The old predicate — `not feed.health(k).is_available` — judged an endpoint
unavailable when it had no recent successful call. But the quote endpoint is
only exercised on the entry/manage path: the scanners run on bars. So the
moment the bot went flat, nothing fetched quotes, the quote endpoint looked
"stale", and every subsequent entry was vetoed with "PRICE feed down" — which
in turn guaranteed quotes were never fetched again. Self-locking: the bot
could only enter trades while it already held a position keeping the endpoint
warm. 85 of 86 signals were vetoed over 16 minutes while the feed logged zero
failures.

Correct semantics: DOWN means actively failing (breaker OPEN, or at/over the
consecutive-failure threshold). An endpoint that has simply not been asked
recently is healthy until proven otherwise — the breaker records every real
failure the instant one happens, so there is no detection gap.
"""
from __future__ import annotations

import logging
from typing import Callable

from config import ENDPOINTS, SYSTEM_REQUIRED_FEEDS, daily_loss_dollars
from models import FeedCriticality, System

log = logging.getLogger("kill_switch")


class KillSwitch:
    def __init__(self, feed, broker):
        self._feed, self._broker = feed, broker
        self._on_price_lost: dict[System, Callable[[str], None]] = {}
        # Remember what we've already alarmed about so a persistent outage
        # logs on state CHANGE, not every cycle (the old version emitted the
        # same ERROR line 96 times in 16 minutes), and so the emergency
        # flatten fires ONCE per outage instead of re-firing every cycle
        # against an already-flat book.
        self._down_last_cycle: dict[System, bool] = {}
        self._loss_halt_logged = False

    def register_price_loss_handler(self, system, handler):
        self._on_price_lost[system] = handler

    # ------------------------------------------------------------------
    # Feed state. Prefer the feed's own is_down() (correct breaker-based
    # semantics, added in feed_layer v6.1); fall back to reading the health
    # record directly so this file still works against an older feed layer.
    # ------------------------------------------------------------------

    def _feed_is_down(self, key) -> bool:
        if hasattr(self._feed, "is_down"):
            return self._feed.is_down(key)
        # Fallback: same semantics, computed here. NOTE: deliberately NOT
        # `not health.is_available` — recency is not failure (see module
        # docstring for the deadlock that caused).
        from feed_layer import BREAKER_FAILURE_THRESHOLD
        from models import HealthState
        h = self._feed.health(key)
        return (h.state is HealthState.OPEN
                or h.consecutive_failures >= BREAKER_FAILURE_THRESHOLD)

    def _down(self, system, crit):
        return [k for k in SYSTEM_REQUIRED_FEEDS[system]
                if ENDPOINTS[k].criticality is crit and self._feed_is_down(k)]

    # ------------------------------------------------------------------
    # Entry gate: called by engines before opening a position.
    # ------------------------------------------------------------------

    def may_open(self, system):
        if self._broker.realized_today <= -abs(daily_loss_dollars(self._broker.equity)):
            if not self._loss_halt_logged:
                log.error("%s: daily loss limit hit (%.2f) -> halting entries "
                          "for the rest of the session.",
                          system.value, self._broker.realized_today)
                self._loss_halt_logged = True
            return False
        down = self._down(system, FeedCriticality.PRICE)
        if down:
            log.error("%s: PRICE feed(s) %s FAILING -> refusing entry.",
                      system.value, down)
            return False
        return True

    # ------------------------------------------------------------------
    # Emergency sweep: called once per cycle from main.cycle().
    # Fires the registered flatten handler on the DOWN transition only;
    # logs recovery on the UP transition.
    # ------------------------------------------------------------------

    def check_emergencies(self):
        for system in SYSTEM_REQUIRED_FEEDS:
            down = bool(self._down(system, FeedCriticality.PRICE))
            was_down = self._down_last_cycle.get(system, False)
            if down and not was_down:
                which = self._down(system, FeedCriticality.PRICE)
                log.error("%s: PRICE feed(s) %s DOWN (breaker/failures) -> "
                          "emergency.", system.value, which)
                handler = self._on_price_lost.get(system)
                if handler:
                    try:
                        handler(f"price feed emergency: {which}")
                    except Exception as e:  # noqa: BLE001
                        # A flatten failure here must not kill the cycle;
                        # main's retry wrapper + broker retry semantics
                        # handle it next cycle.
                        log.critical("%s: emergency handler raised: %s",
                                     system.value, e)
            elif was_down and not down:
                log.warning("%s: PRICE feeds recovered -> entries re-enabled.",
                            system.value)
            self._down_last_cycle[system] = down

    def reset_daily(self):
        """Call at session roll (alongside broker.reset_daily) so the daily
        loss halt un-latches for the new session."""
        self._loss_halt_logged = False
         
