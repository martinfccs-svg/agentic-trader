"""Swing engine (v6): consumes TREND signals from the price-action scanner.

Daily timeframe. Wide ATR stop, risk-based sizing, liquidity filter, multi-day
hold, NO end-of-day flatten. Works against whichever Broker is wired (paper or
alpaca) — the engine doesn't know or care which.
"""

from __future__ import annotations

import logging

from config import MIN_DOLLAR_VOL, MIN_PRICE, SWING
from models import Action, Signal, System
from risk import position_size

log = logging.getLogger("swing")


class SwingRiskEngine:
    def __init__(self, feed, broker, kill, logger):
        self._feed, self._broker, self._kill, self._log = feed, broker, kill, logger

    def _open(self):
        return sum(1 for p in self._broker.positions.values() if p.system is System.SWING)

    def handle_signal(self, signal: Signal):
        if not self._kill.may_open(System.SWING):
            self._log.record(signal, System.SWING, Action.REJECTED_BY_KILL_SWITCH)
            return
        if self._open() >= SWING.max_positions or signal.ticker in self._broker.positions:
            self._log.record(signal, System.SWING, Action.REJECTED_BY_RISK)
            return
        q = self._feed.get_quote(signal.ticker)
        if q is None or q.atr is None:
            self._log.record(signal, System.SWING, Action.REJECTED_BY_RISK, "no quote/atr")
            return
        if q.price < MIN_PRICE or (q.avg_dollar_volume or 0) < MIN_DOLLAR_VOL:
            self._log.record(signal, System.SWING, Action.REJECTED_BY_LIQUIDITY)
            return
        stop = q.price - SWING.atr_stop_multiple * q.atr
        shares = position_size(self._broker.equity, q.price, stop, getattr(self._broker, "cash", 1e12))
        if shares <= 0:
            self._log.record(signal, System.SWING, Action.REJECTED_BY_RISK, "size=0")
            return
        self._broker.buy(signal.ticker, shares, q.price, System.SWING, signal.source, stop)
        self._log.record(signal, System.SWING, Action.OPENED,
                         f"{signal.reason} shares={shares:.2f} stop={stop:.2f}")

    def manage_open_positions(self):
        for ticker in list(self._broker.positions):
            pos = self._broker.positions[ticker]
            if pos.system is not System.SWING:
                continue
            q = self._feed.get_quote(ticker)
            if q is None:
                continue
            self._broker.mark(ticker, q.price)
            if q.atr is not None:
                pos.stop_price = max(pos.stop_price, pos.high_water - SWING.atr_stop_multiple * q.atr)
            if q.price <= pos.stop_price:
                self._log.record_close(System.SWING, self._broker.sell(ticker, q.price))
