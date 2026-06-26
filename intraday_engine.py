"""Intraday momentum engine (working).

Social buzz only populates a watchlist. A live price-momentum confirmation gate
(relative-volume spike + price above VWAP) is the actual entry trigger. Tight
ATR stop, percentage trailing exit, HARD end-of-day flatten.
"""

from __future__ import annotations

import logging

from config import INTRADAY, MIN_DOLLAR_VOL, MIN_PRICE
from models import Action, Quote, Signal, System
from risk import position_size

log = logging.getLogger("intraday")


class IntradayRiskEngine:
    def __init__(self, feed, broker, kill, logger) -> None:
        self._feed = feed
        self._broker = broker
        self._kill = kill
        self._log = logger
        self._watchlist: set[str] = set()

    def _open_count(self) -> int:
        return sum(1 for p in self._broker.positions.values() if p.system is System.INTRADAY)

    def handle_signal(self, signal: Signal) -> None:
        """Social signal -> watchlist only. Never opens directly."""
        if not self._kill.may_open(System.INTRADAY):
            self._log.record(signal, System.INTRADAY, Action.REJECTED_BY_KILL_SWITCH)
            return
        self._watchlist.add(signal.ticker)
        self._log.record(signal, System.INTRADAY, Action.WATCHLISTED)

    def _momentum_confirmed(self, q: Quote) -> bool:
        if INTRADAY.require_volume_spike:
            if q.rel_volume is None or q.rel_volume < INTRADAY.min_rel_volume:
                return False
        if INTRADAY.require_above_vwap:
            if q.vwap is None or q.price < q.vwap:
                return False
        return True

    def evaluate_watchlist(self) -> None:
        if not self._kill.may_open(System.INTRADAY):
            return
        for ticker in list(self._watchlist):
            if ticker in self._broker.positions:
                continue
            if self._open_count() >= INTRADAY.max_positions:
                break
            quote = self._feed.get_quote(ticker)
            if quote is None or quote.atr is None:
                continue
            if quote.price < MIN_PRICE or (quote.avg_dollar_volume or 0) < MIN_DOLLAR_VOL:
                continue
            if self._momentum_confirmed(quote):
                stop = quote.price - INTRADAY.atr_stop_multiple * quote.atr
                shares = position_size(self._broker.equity, quote.price, stop, self._broker.cash)
                if shares <= 0:
                    continue
                self._broker.buy(ticker, shares, quote.price, System.INTRADAY,
                                 None, stop)
                self._log.record(Signal(__import__("models").SignalSource.SOCIAL, ticker),
                                 System.INTRADAY, Action.OPENED,
                                 f"relvol={quote.rel_volume:.2f} shares={shares:.2f}")
                self._watchlist.discard(ticker)

    def manage_open_positions(self) -> None:
        """Percentage trailing stop from the high-water mark + hard stop."""
        for ticker in list(self._broker.positions):
            pos = self._broker.positions[ticker]
            if pos.system is not System.INTRADAY:
                continue
            quote = self._feed.get_quote(ticker)
            if quote is None:
                continue
            self._broker.mark(ticker, quote.price)
            trail_stop = pos.high_water * (1 - INTRADAY.trail_pct)
            pos.stop_price = max(pos.stop_price, trail_stop)
            if quote.price <= pos.stop_price:
                realized = self._broker.sell(ticker, quote.price)
                self._log.record_close(System.INTRADAY, realized)

    def flatten_all(self, reason: str) -> None:
        for ticker in list(self._broker.positions):
            if self._broker.positions[ticker].system is System.INTRADAY:
                quote = self._feed.get_quote(ticker)
                price = quote.price if quote else self._broker.positions[ticker].entry_price
                realized = self._broker.sell(ticker, price)
                self._log.record_close(System.INTRADAY, realized)
        log.info("INTRADAY flatten complete (%s)", reason)
