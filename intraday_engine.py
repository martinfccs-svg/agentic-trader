"""Intraday engine (v6): consumes MOMENTUM signals from the price-action scanner.

Intraday (1-min) timeframe. The scanner already required relvol spike + above
VWAP + opening-range break, so a fired MOMENTUM signal IS a confirmed entry.
Tight ATR stop, percentage trailing exit, HARD EOD flatten.
"""

from __future__ import annotations

import logging

from config import INTRADAY, MIN_DOLLAR_VOL, MIN_PRICE
from indicators import avg_dollar_volume
from models import Action, Signal, System
from risk import position_size

log = logging.getLogger("intraday")


class IntradayRiskEngine:
    def __init__(self, feed, broker, kill, logger):
        self._feed, self._broker, self._kill, self._log = feed, broker, kill, logger

    def _open(self):
        return sum(1 for p in self._broker.positions.values() if p.system is System.INTRADAY)

    def handle_signal(self, signal: Signal):
        if not self._kill.may_open(System.INTRADAY):
            self._log.record(signal, System.INTRADAY, Action.REJECTED_BY_KILL_SWITCH)
            return
        if self._open() >= INTRADAY.max_positions or signal.ticker in self._broker.positions:
            self._log.record(signal, System.INTRADAY, Action.REJECTED_BY_RISK)
            return
        q = self._feed.get_quote(signal.ticker)
        if q is None or q.atr is None:
            self._log.record(signal, System.INTRADAY, Action.REJECTED_BY_RISK, "no quote/atr")
            return
        # Liquidity on DAILY dollar volume. q.avg_dollar_volume is computed from
        # 1-min bars here, so comparing it to a daily threshold was ~390x too
        # strict (the unit bug that was rejecting PLTR). Test daily bars instead.
        daily = self._feed.get_daily_bars(signal.ticker)
        daily_dv = avg_dollar_volume(daily) if daily else None
        if q.price < MIN_PRICE or (daily_dv or 0) < MIN_DOLLAR_VOL:
            self._log.record(signal, System.INTRADAY, Action.REJECTED_BY_LIQUIDITY)
            return
        stop = q.price - INTRADAY.atr_stop_multiple * q.atr
        shares = position_size(self._broker.equity, q.price, stop, getattr(self._broker, "cash", 1e12))
        if shares <= 0:
            self._log.record(signal, System.INTRADAY, Action.REJECTED_BY_RISK, "size=0")
            return
        self._broker.buy(signal.ticker, shares, q.price, System.INTRADAY, signal.source, stop)
        self._log.record(signal, System.INTRADAY, Action.OPENED,
                         f"{signal.reason} shares={shares:.2f} stop={stop:.2f}")

    def manage_open_positions(self):
        for ticker in list(self._broker.positions):
            pos = self._broker.positions[ticker]
            if pos.system is not System.INTRADAY:
                continue
            q = self._feed.get_quote(ticker)
            if q is None:
                continue
            self._broker.mark(ticker, q.price)
            pos.stop_price = max(pos.stop_price, pos.high_water * (1 - INTRADAY.trail_pct))
            if q.price <= pos.stop_price:
                self._log.record_close(System.INTRADAY, self._broker.sell(ticker, q.price))

    def flatten_all(self, reason: str):
        for ticker in list(self._broker.positions):
            if self._broker.positions[ticker].system is System.INTRADAY:
                q = self._feed.get_quote(ticker)
                price = q.price if q else self._broker.positions[ticker].entry_price
                self._log.record_close(System.INTRADAY, self._broker.sell(ticker, price))
        log.info("INTRADAY flatten complete (%s)", reason)
