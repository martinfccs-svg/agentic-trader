"""Mean reversion engine (RSI).

Contrarian to the momentum books: buys oversold names (RSI low) that are still
in a longer-term uptrend, and exits when RSI recovers toward the mean — or on a
protective ATR stop. Tracked as its own system so its return stream can be
correlation-checked against the momentum books (that's the whole point).
"""

from __future__ import annotations

import logging

from config import MEANREV, MIN_DOLLAR_VOL, MIN_PRICE
from indicators import rsi
from models import Action, Signal, System
from risk import position_size
from safety import market_is_open

log = logging.getLogger("meanrev")


class NullNotifier:
    """No-op stand-in for the optional trade notifier (2026-07-16). See the
    note in swing_engine.py: the notifier arg is optional so build() cannot
    TypeError, and audit.py still mirrors fills/closes to ntfy meanwhile."""

    def __getattr__(self, _name):
        return lambda *a, **k: None


class MeanReversionEngine:
    def __init__(self, feed, broker, kill, logger, notifier=None):
        self._feed, self._broker, self._kill, self._log = feed, broker, kill, logger
        self._notifier = notifier or NullNotifier()

    def _open(self):
        return sum(1 for p in self._broker.positions.values()
                   if p.system is System.MEANREV)

    def handle_signal(self, signal: Signal):
        if not self._kill.may_open(System.MEANREV):
            self._log.record(signal, System.MEANREV, Action.REJECTED_BY_KILL_SWITCH)
            return
        if self._open() >= MEANREV.max_positions or signal.ticker in self._broker.positions:
            self._log.record(signal, System.MEANREV, Action.REJECTED_BY_RISK)
            return
        q = self._feed.get_quote(signal.ticker)
        if q is None or q.atr is None:
            self._log.record(signal, System.MEANREV, Action.REJECTED_BY_RISK, "no quote/atr")
            return
        # DAILY-scale by the feed contract (feed_layer.get_quote, Jul 15 fix).
        if q.price < MIN_PRICE or (q.avg_dollar_volume or 0) < MIN_DOLLAR_VOL:
            self._log.record(signal, System.MEANREV, Action.REJECTED_BY_LIQUIDITY)
            return
        stop = q.price - MEANREV.atr_stop_multiple * q.atr
        shares = position_size(self._broker.equity, q.price, stop,
                               getattr(self._broker, "cash", 1e12))
        if shares <= 0:
            self._log.record(signal, System.MEANREV, Action.REJECTED_BY_RISK, "size=0")
            return
        pos = self._broker.buy(signal.ticker, shares, q.price, System.MEANREV,
                               signal.source, stop)
        if pos is None:
            log.warning("meanrev: broker refused %s — no position opened",
                        signal.ticker)
            return
        self._notifier.notify_entry(
            ticker=signal.ticker, shares=shares, price=q.price,
            system=System.MEANREV.value, source=signal.source.value
        )
        self._log.record(signal, System.MEANREV, Action.OPENED,
                         f"{signal.reason} shares={shares:.2f} stop={stop:.2f}")

    def manage_open_positions(self):
        if hasattr(self._broker, "reconcile_filled_legs"):
            for _t, realized in \
                    self._broker.reconcile_filled_legs(System.MEANREV).items():
                self._log.record_close(System.MEANREV, realized)
        for ticker in list(self._broker.positions):
            pos = self._broker.positions.get(ticker)
            if pos is None or pos.system is not System.MEANREV:
                continue
            q = self._feed.get_quote(ticker)
            if q is None:
                continue
            self._broker.mark(ticker, q.price)
            bars = self._feed.get_daily_bars(ticker)
            r = rsi(bars.close, MEANREV.rsi_period) if bars else None
            # Exit when reverted to the mean (RSI recovered) OR stop hit.
            hit_exit = ((r is not None and r >= MEANREV.rsi_exit)
                        or q.price <= pos.stop_price)
            # Local stop is a BACKUP to the broker-side GTC leg, which is
            # live 24/7. Firing it while the market is CLOSED just sells at a
            # stale quote — on 2026-07-16 that dumped UNH/INTC/MU at
            # "quote-est" prices 30 min after the bell. If a stop is genuinely
            # hit during the session, the broker's own leg fills it.
            if hit_exit and market_is_open():
                try:
                    exit_price = q.price
                    entry_price = pos.entry_price
                    shares = pos.shares
                    realized = self._broker.sell(ticker, exit_price)
                    self._log.record_close(System.MEANREV, realized)
                    if exit_price is not None and realized is not None:
                        self._notifier.notify_exit(
                            ticker=ticker, shares=shares,
                            exit_price=exit_price, entry_price=entry_price,
                            pnl=realized, system=System.MEANREV.value,
                        )
                except Exception as e:  # noqa: BLE001
                    log.error("meanrev exit %s failed (retry next cycle): %s",
                              ticker, e)
