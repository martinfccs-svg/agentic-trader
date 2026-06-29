"""Cross-sectional (relative) momentum engine.

Different shape from the other engines: instead of per-ticker signals, it ranks
the WHOLE universe by trailing return and holds the top N. On a rebalance
cadence it sells names that dropped out of the top N and buys the new entrants.
A wide ATR stop is the only between-rebalance safety net.

This is relative strength ("own the strongest names"), not absolute breakout —
a distinct return stream from the time-series momentum in the swing book.
"""

from __future__ import annotations

import logging

from config import MIN_DOLLAR_VOL, MIN_PRICE, XSECT
from indicators import trailing_return
from models import Action, Signal, SignalSource, System
from risk import position_size

log = logging.getLogger("xsectmom")


class CrossSectionalMomentumEngine:
    def __init__(self, feed, broker, kill, logger, universe):
        self._feed, self._broker, self._kill, self._log = feed, broker, kill, logger
        self._universe = universe
        self._since_rebalance = 0

    def handle_signal(self, signal: Signal):
        # Not signal-driven; rebalance() does the work. No-op keeps the router happy.
        return

    def _rank(self) -> list[str]:
        scored = []
        for t in self._universe:
            bars = self._feed.get_daily_bars(t)
            if bars is None:
                continue
            ret = trailing_return(bars.close, XSECT.lookback_days, XSECT.skip_days)
            q = self._feed.get_quote(t)
            if ret is None or q is None:
                continue
            if q.price < MIN_PRICE or (q.avg_dollar_volume or 0) < MIN_DOLLAR_VOL:
                continue
            scored.append((ret, t))
        scored.sort(reverse=True)
        return [t for _, t in scored[:XSECT.top_n]]

    def maybe_rebalance(self):
        """Call once per cycle; acts only every XSECT.rebalance_cycles."""
        self._since_rebalance += 1
        if self._since_rebalance < XSECT.rebalance_cycles:
            return
        self._since_rebalance = 0
        self.rebalance()

    def rebalance(self):
        if not self._kill.may_open(System.XSECTMOM):
            return
        target = set(self._rank())
        held = {t for t, p in self._broker.positions.items() if p.system is System.XSECTMOM}

        # Sell names that fell out of the top N.
        for ticker in held - target:
            q = self._feed.get_quote(ticker)
            if q:
                self._log.record_close(System.XSECTMOM, self._broker.sell(ticker, q.price))

        # Buy new entrants.
        for ticker in target - held:
            q = self._feed.get_quote(ticker)
            if q is None or q.atr is None:
                continue
            stop = q.price - XSECT.atr_stop_multiple * q.atr
            shares = position_size(self._broker.equity, q.price, stop, getattr(self._broker, "cash", 1e12))
            if shares <= 0:
                continue
            self._broker.buy(ticker, shares, q.price, System.XSECTMOM, SignalSource.REL_STRENGTH, stop)
            self._log.record(Signal(SignalSource.REL_STRENGTH, ticker, reason="top-N relative strength"),
                             System.XSECTMOM, Action.OPENED, f"shares={shares:.2f}")

    def manage_open_positions(self):
        # Between rebalances, only the protective stop is active.
        for ticker in list(self._broker.positions):
            pos = self._broker.positions[ticker]
            if pos.system is not System.XSECTMOM:
                continue
            q = self._feed.get_quote(ticker)
            if q is None:
                continue
            self._broker.mark(ticker, q.price)
            if q.price <= pos.stop_price:
                self._log.record_close(System.XSECTMOM, self._broker.sell(ticker, q.price))
