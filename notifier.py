"""ntfy.sh notification integration for trade alerts."""

from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger("notifier")


class Notifier:
    """Send trade notifications via ntfy.sh."""

    def __init__(self) -> None:
        self.topic = os.environ.get("NTFY", "").strip()
        self.enabled = bool(self.topic)

    def notify_entry(self, ticker: str, shares: float, price: float, system: str, source: str = "") -> None:
        """Send notification for trade entry."""
        if not self.enabled:
            return
        message = f"BUY {shares:.0f} {ticker} @ ${price:.2f}"
        title = f"Trade Entry ({system})"
        self._send(message, title, priority="high")

    def notify_exit(self, ticker: str, shares: float, exit_price: float, entry_price: float, pnl: float, system: str) -> None:
        """Send notification for trade exit."""
        if not self.enabled:
            return
        pnl_pct = (pnl / (entry_price * shares)) * 100 if shares > 0 else 0
        message = f"SELL {shares:.0f} {ticker} @ ${exit_price:.2f} | {pnl:+.2f} ({pnl_pct:+.1f}%)"
        title = f"Trade Exit ({system})"
        priority = "high" if pnl > 0 else "default"
        self._send(message, title, priority=priority)

    def _send(self, message: str, title: str = "Agentic Trader", priority: str = "default") -> None:
        """Send a notification to ntfy.sh."""
        if not self.enabled:
            return
        headers = {"Title": title, "Priority": priority, "Tags": "trading"}
        try:
            requests.post(self.topic, data=message, headers=headers, timeout=3)
        except Exception as e:
            log.warning("ntfy notification error: %s", e)
