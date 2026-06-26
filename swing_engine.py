"""Swing engine (working).

Insider + congressional signals. Wide ATR stop, risk-based sizing, liquidity +
freshness + optional uptrend filters, multi-day hold, NO end-of-day flatten.
"""

from __future__ import annotations

import logging

from config import MIN_DOLLAR_VOL, MIN_PRICE, SWING
from models import Action, Signal, System
from risk import position_size

log = logging.getLogger("swing")


class SwingRiskEngine:
    def __init__(self, feed, broker, kill, logger) -> None:
        self._feed = feed
        self._broker = broker
        self._kill = kill
        self._log = logger

    def _open_count(self) -> int:
        return sum(1 for p in self._broker.positions.values() if p.system is System.SWING)

    def handle_signal(self, signal: Signal) -> None:
        if not self._kill.may_open(System.SWING):
            self._log.record(signal, System.SWING, Action.REJECTED_BY_KILL_SWITCH)
            return

        lag = signal.lag_days
        if lag is not None and lag > SWING.max_signal_lag_days:
            self._log.record(signal, System.SWING, Action.REJECTED_BY_FRESHNESS,
                             f"lag={lag:.1f}d")
            return

        if self._open_count() >= SWING.max_positions:
            self._log.record(signal, System.SWING, Action.REJECTED_BY_RISK, "max positions")
            return
        if signal.ticker in self._broker.positions:
            return  # already held

        quote = self._feed.get_quote(signal.ticker)
        if quote is None or quote.atr is None:
            self._log.record(signal, System.SWING, Action.REJECTED_BY_RISK, "no quote/atr")
            return

        # Liquidity filter.
        if quote.price < MIN_PRICE or (quote.avg_dollar_volume or 0) < MIN_DOLLAR_VOL:
            self._log.record(signal, System.SWING, Action.REJECTED_BY_LIQUIDITY)
            return

        # Optional uptrend filter (no falling knives).
        if SWING.require_uptrend and quote.sma is not None and quote.price < quote.sma:
            self._log.record(signal, System.SWING, Action.REJECTED_BY_CONFIRMATION, "below SMA")
            return

        stop = quote.price - SWING.atr_stop_multiple * quote.atr
        shares = position_size(self._broker.equity, quote.price, stop, self._broker.cash)
        if shares <= 0:
            self._log.record(signal, System.SWING, Action.REJECTED_BY_RISK, "size=0")
            return

        self._broker.buy(signal.ticker, shares, quote.price, System.SWING, signal.source, stop)
        self._log.record(signal, System.SWING, Action.OPENED,
                         f"shares={shares:.2f} stop={stop:.2f}")

    def manage_open_positions(self) -> None:
        """Mark, ratchet stop on ATR, exit on stop. No EOD flatten."""
        for ticker in list(self._broker.positions):
            pos = self._broker.positions[ticker]
            if pos.system is not System.SWING:
                continue
            quote = self._feed.get_quote(ticker)
            if quote is None:
                continue
            self._broker.mark(ticker, quote.price)
            # Ratchet a wide trailing stop using ATR from the high-water mark.
            if quote.atr is not None:
                trail = pos.high_water - SWING.atr_stop_multiple * quote.atr
                pos.stop_price = max(pos.stop_price, trail)
            if quote.price <= pos.stop_price:
                realized = self._broker.sell(ticker, quote.price)
                self._log.record_close(System.SWING, realized)
