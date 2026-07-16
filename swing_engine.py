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
from safety import market_is_open

log = logging.getLogger("swing")


class NullNotifier:
    """No-op stand-in for the optional trade notifier (2026-07-16).

    The engines were given a required `notifier` arg while main.py never
    passed one, so build() raised TypeError and the deploy could not boot.
    Rather than delete the notify_* calls (destroying work) or guess at the
    notifier's API, the parameter is now OPTIONAL and defaults to this
    null object: every notify_* call becomes a silent no-op.

    NOTE: audit.py independently mirrors every fill / close / halt / boot to
    ntfy, so phone alerting is NOT lost while no notifier is wired. To
    restore the engines' own notifications, construct the real notifier in
    main.py's build() and pass notifier=<it> to each engine.
    """

    def __getattr__(self, _name):
        return lambda *a, **k: None


class SwingRiskEngine:
    def __init__(self, feed, broker, kill, logger, notifier=None):
        self._feed, self._broker, self._kill, self._log = feed, broker, kill, logger
        self._notifier = notifier or NullNotifier()

    def _open(self):
        return sum(1 for p in self._broker.positions.values()
                   if p.system is System.SWING)

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
        # q.avg_dollar_volume and q.atr are DAILY-scale by the feed's contract
        # (feed_layer.get_quote, fixed 2026-07-15). Before that fix this gate
        # and the stop below both ran on 1-minute scale.
        if q.price < MIN_PRICE or (q.avg_dollar_volume or 0) < MIN_DOLLAR_VOL:
            self._log.record(signal, System.SWING, Action.REJECTED_BY_LIQUIDITY)
            return
        stop = q.price - SWING.atr_stop_multiple * q.atr
        shares = position_size(self._broker.equity, q.price, stop,
                               getattr(self._broker, "cash", 1e12))
        if shares <= 0:
            self._log.record(signal, System.SWING, Action.REJECTED_BY_RISK, "size=0")
            return
        pos = self._broker.buy(signal.ticker, shares, q.price, System.SWING,
                               signal.source, stop)
        if pos is None:
            # Broker refused (duplicate coid / existing position). Do not
            # notify or log an open that did not happen.
            log.warning("swing: broker refused %s — no position opened",
                        signal.ticker)
            return
        self._notifier.notify_entry(
            ticker=signal.ticker, shares=shares, price=q.price,
            system=System.SWING.value, source=signal.source.value
        )
        self._log.record(signal, System.SWING, Action.OPENED,
                         f"{signal.reason} shares={shares:.2f} stop={stop:.2f}")

    def manage_open_positions(self):
        # Book any position whose broker-side bracket leg filled since the
        # last cycle (keeps the tracker honest without a phantom close).
        if hasattr(self._broker, "reconcile_filled_legs"):
            for _t, realized in \
                    self._broker.reconcile_filled_legs(System.SWING).items():
                self._log.record_close(System.SWING, realized)
        for ticker in list(self._broker.positions):
            pos = self._broker.positions.get(ticker)
            if pos is None or pos.system is not System.SWING:
                continue
            q = self._feed.get_quote(ticker)
            if q is None:
                continue
            self._broker.mark(ticker, q.price)
            if q.atr is not None:
                pos.stop_price = max(
                    pos.stop_price,
                    pos.high_water - SWING.atr_stop_multiple * q.atr)
            # Local stop is a BACKUP to the broker-side GTC leg, which is
            # live 24/7. Firing it while the market is CLOSED just sells at a
            # stale quote — on 2026-07-16 that dumped UNH/INTC/MU at
            # "quote-est" prices 30 min after the bell. If a stop is genuinely
            # hit during the session, the broker's own leg fills it.
            if q.price <= pos.stop_price and market_is_open():
                try:
                    exit_price = q.price
                    entry_price = pos.entry_price
                    shares = pos.shares
                    realized = self._broker.sell(ticker, exit_price)
                    self._log.record_close(System.SWING, realized)
                    if exit_price is not None and realized is not None:
                        self._notifier.notify_exit(
                            ticker=ticker, shares=shares,
                            exit_price=exit_price, entry_price=entry_price,
                            pnl=realized, system=System.SWING.value,
                        )
                except Exception as e:  # noqa: BLE001 — one exit must not kill the loop
                    log.error("swing stop-exit %s failed (retry next cycle): %s",
                              ticker, e)
