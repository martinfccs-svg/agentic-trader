"""Intraday engine (v6): consumes MOMENTUM signals from the price-action scanner.

Intraday (1-min) timeframe. The scanner already required relvol spike + above
VWAP + opening-range break, so a fired MOMENTUM signal IS a confirmed entry.
Tight ATR stop, percentage trailing exit, HARD EOD flatten.
"""

from __future__ import annotations

import logging

import audit
from config import INTRADAY, MIN_DOLLAR_VOL, MIN_PRICE
from indicators import avg_dollar_volume
from models import Action, Signal, System
from risk import position_size

log = logging.getLogger("intraday")


class IntradayRiskEngine:
    def __init__(self, feed, broker, kill, logger):
        self._feed, self._broker, self._kill, self._log = feed, broker, kill, logger
        # One-shot latch: on 2026-07-06 the feed-breaker path re-flattened
        # every ~5s for 15+ minutes, spamming logs and burning API budget
        # (contributing to the Finnhub 429s). Latched after a clean flatten;
        # re-armed when a new position opens.
        self._flattened_latch = False

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
        pos = self._broker.buy(signal.ticker, shares, q.price, System.INTRADAY,
                               signal.source, stop)
        if pos is None:
            # Broker refused: duplicate order suppressed, existing broker-side
            # position, or qty rounded to 0. Not an open — do not record one.
            self._log.record(signal, System.INTRADAY, Action.REJECTED_BY_RISK,
                             "broker refused order (duplicate/existing/qty=0)")
            return
        self._flattened_latch = False   # new position -> flatten may act again
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
                # Crash #2 (2026-07-06) started here: a 404 from Alpaca killed
                # the whole cycle. sell() now handles 404 as already-flat; any
                # genuine failure is contained and retried next cycle.
                try:
                    self._log.record_close(System.INTRADAY,
                                           self._broker.sell(ticker, q.price))
                except Exception as e:  # noqa: BLE001
                    log.error("stop-exit %s failed (will retry next cycle): %s",
                              ticker, e)

    def flatten_all(self, reason: str):
        """Close every intraday position. Guarantees (post-2026-07-06):
          - every ticker is attempted even if an earlier one fails
            (old code: one 404 aborted the loop AND the process, and
            record_close never ran — which is why the daily report showed
            0 trades while the account moved)
          - failures keep their position in the tracker and retry next cycle
          - runs at most once while flat (no 5-second flatten spam)
        """
        tickers = [t for t, p in self._broker.positions.items()
                   if p.system is System.INTRADAY]
        if not tickers:
            if not self._flattened_latch:
                log.info("INTRADAY flatten: nothing open (%s)", reason)
                self._flattened_latch = True
            return

        failed = []
        for ticker in tickers:
            q = self._feed.get_quote(ticker)
            pos = self._broker.positions.get(ticker)
            if pos is None:      # closed elsewhere while iterating
                continue
            price = q.price if q else pos.entry_price
            try:
                self._log.record_close(System.INTRADAY,
                                       self._broker.sell(ticker, price))
            except Exception as e:  # noqa: BLE001
                log.error("flatten %s failed (%s): %s", ticker, reason, e)
                failed.append(ticker)

        if failed:
            # Do NOT latch and do NOT claim success — positions remain in the
            # tracker for retry. The old unconditional "flatten complete" log
            # masked exactly this state.
            log.error("INTRADAY flatten INCOMPLETE (%s): failed=%s — "
                      "will retry next cycle", reason, failed)
            audit.flatten(reason=reason, closed=len(tickers) - len(failed),
                          failed=failed)
        else:
            self._flattened_latch = True
            log.info("INTRADAY flatten complete (%s)", reason)
            audit.flatten(reason=reason, closed=len(tickers), failed=[])
