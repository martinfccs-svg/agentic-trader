"""Cross-sectional (relative) momentum engine.

Different shape from the other engines: instead of per-ticker signals, it ranks
the WHOLE universe by trailing return and holds the top N. On a rebalance
cadence it sells names that dropped out of the top N and buys the new entrants.
A wide ATR stop is the only between-rebalance safety net.

This is relative strength ("own the strongest names"), not absolute breakout —
a distinct return stream from the time-series momentum in the swing book.

2026-07-09 changes (after two sessions of zero positions):
  1. CADENCE: XSECT.rebalance_cycles replaced by scan_health.DailyRebalanceGate.
     780 cycles was "daily" at 30s cycles; at ~5.7s cycles it silently became
     ~74 minutes. Wall-clock (10:00 ET) survives loop-speed changes.
  2. LIQUIDITY UNIT BUG (likely the real killer): the gate compared
     q.avg_dollar_volume — computed from 1-MIN bars, ~390x too small — against
     the DAILY threshold MIN_DOLLAR_VOL, same bug fixed in the intraday engine
     on Jul 3. Every name could fail this gate on every rebalance. Now tests
     daily-bar dollar volume, like intraday does.
  3. TRANSPARENCY: every rebalance logs ranked/insufficient/no-quote/liquidity
     counts, the top-N with scores, holdings, and each ENTER/EXIT — plus a
     CRITICAL naming data starvation when nothing is rankable.
  4. ROBUSTNESS: exits no longer skipped when the kill switch blocks opens;
     per-ticker try/except so one bad name can't abort a rebalance or the
     manage loop; broker-side leg fills booked via reconcile_filled_legs.
"""

from __future__ import annotations

import logging

from config import MIN_DOLLAR_VOL, MIN_PRICE, XSECT
from indicators import avg_dollar_volume, trailing_return
from models import Action, Signal, SignalSource, System
from risk import position_size
from safety import market_is_open
from scan_health import DailyRebalanceGate

log = logging.getLogger("xsectmom")


class CrossSectionalMomentumEngine:
    def __init__(self, feed, broker, kill, logger, universe):
        self._feed, self._broker, self._kill, self._log = feed, broker, kill, logger
        self._universe = universe
        self._gate = DailyRebalanceGate()   # 10:00 ET; XSECT_REBALANCE_ET to change

    def handle_signal(self, signal: Signal):
        # Not signal-driven; rebalance() does the work. No-op keeps the router happy.
        return

    def _rank(self):
        """Score the universe. Returns (ranked tickers, stats) — the stats make
        a zero-position rebalance explain itself in one log line."""
        scored = []
        stats = {"universe": len(self._universe), "no_bars": 0,
                 "no_return": 0, "no_quote": 0, "liquidity_reject": 0}
        for t in self._universe:
            bars = self._feed.get_daily_bars(t)
            if bars is None:
                stats["no_bars"] += 1
                continue
            ret = trailing_return(bars.close, XSECT.lookback_days, XSECT.skip_days)
            if ret is None:
                stats["no_return"] += 1        # insufficient history for lookback
                continue
            q = self._feed.get_quote(t)
            if q is None:
                stats["no_quote"] += 1
                continue
            # Liquidity on DAILY dollar volume. q.avg_dollar_volume comes from
            # 1-min bars (~390x too small vs a daily threshold) — the same unit
            # bug fixed in the intraday engine on Jul 3, previously unfixed here.
            daily_dv = avg_dollar_volume(bars)
            if q.price < MIN_PRICE or (daily_dv or 0) < MIN_DOLLAR_VOL:
                stats["liquidity_reject"] += 1
                continue
            scored.append((ret, t))
        scored.sort(reverse=True)
        return scored, stats

    def maybe_rebalance(self):
        """Call once per cycle; acts once per trading day at/after 10:00 ET."""
        if not self._gate.should_run():
            return
        if not market_is_open():
            log.info("xsect rebalance: gate open but market closed — re-arming")
            self._gate._last_run_date = None    # don't burn the day on a holiday
            return
        self.rebalance()

    def rebalance(self):
        scored, stats = self._rank()
        target = {t for _, t in scored[:XSECT.top_n]}
        held = {t for t, p in self._broker.positions.items()
                if p.system is System.XSECTMOM}
        top_str = ", ".join(f"{t}({r:+.1%})" for r, t in scored[:XSECT.top_n]) or "EMPTY"
        log.warning("xsect rebalance: ranked=%d/%d (no_bars=%d no_return=%d "
                    "no_quote=%d liquidity_reject=%d) | top%d: %s | held=%s",
                    len(scored), stats["universe"], stats["no_bars"],
                    stats["no_return"], stats["no_quote"],
                    stats["liquidity_reject"], XSECT.top_n, top_str,
                    sorted(held) or "none")
        if not scored:
            log.critical("xsect rebalance: ZERO rankable names. no_return=%d "
                         "dominating means the daily-candle fetch is shorter "
                         "than lookback %d+skip %d (data starvation — widen "
                         "feed_layer window); liquidity_reject=%d dominating "
                         "means the dollar-volume gate is still mis-scaled.",
                         stats["no_return"], XSECT.lookback_days,
                         XSECT.skip_days, stats["liquidity_reject"])
            return

        # Sell names that fell out of the top N — even when the kill switch
        # blocks opens: exits reduce risk and must never be gated by it.
        for ticker in sorted(held - target):
            q = self._feed.get_quote(ticker)
            pos = self._broker.positions.get(ticker)
            if pos is None:
                continue
            price = q.price if q and q.price else pos.entry_price
            try:
                self._log.record_close(System.XSECTMOM,
                                       self._broker.sell(ticker, price))
                log.warning("xsect rebalance: EXIT %s (fell out of top%d)",
                            ticker, XSECT.top_n)
            except Exception as e:  # noqa: BLE001 — one exit must not block the rest
                log.error("xsect rebalance: exit %s failed (retry next "
                          "rebalance): %s", ticker, e)

        # Buy new entrants.
        if not self._kill.may_open(System.XSECTMOM):
            log.warning("xsect rebalance: kill switch blocks entries — "
                        "exits done, entries skipped")
            return
        for ret, ticker in scored[:XSECT.top_n]:
            if ticker in self._broker.positions:
                continue
            q = self._feed.get_quote(ticker)
            if q is None or q.atr is None:
                log.warning("xsect rebalance: skip %s — no quote/ATR", ticker)
                continue
            stop = q.price - XSECT.atr_stop_multiple * q.atr
            shares = position_size(self._broker.equity, q.price, stop,
                                   getattr(self._broker, "cash", 1e12))
            if shares <= 0:
                log.warning("xsect rebalance: skip %s — size=0", ticker)
                continue
            pos = self._broker.buy(ticker, shares, q.price, System.XSECTMOM,
                                   SignalSource.REL_STRENGTH, stop)
            if pos is None:
                log.warning("xsect rebalance: broker refused %s "
                            "(duplicate/existing) — skipped", ticker)
                continue
            self._log.record(Signal(SignalSource.REL_STRENGTH, ticker,
                                    reason="top-N relative strength"),
                             System.XSECTMOM, Action.OPENED,
                             f"shares={shares:.2f}")
            log.warning("xsect rebalance: ENTER %s x%.0f @ %.2f stop=%.2f "
                        "(ret %+.1f%%)", ticker, shares, q.price, stop,
                        ret * 100)

    def manage_open_positions(self):
        # Book any positions whose broker-side stop leg filled since last cycle.
        if hasattr(self._broker, "reconcile_filled_legs"):
            for _t, realized in \
                    self._broker.reconcile_filled_legs(System.XSECTMOM).items():
                self._log.record_close(System.XSECTMOM, realized)
        # Between rebalances, only the protective stop is active.
        for ticker in list(self._broker.positions):
            pos = self._broker.positions.get(ticker)
            if pos is None or pos.system is not System.XSECTMOM:
                continue
            q = self._feed.get_quote(ticker)
            if q is None:
                continue
            self._broker.mark(ticker, q.price)
            if q.price <= pos.stop_price:
                try:
                    self._log.record_close(System.XSECTMOM,
                                           self._broker.sell(ticker, q.price))
                except Exception as e:  # noqa: BLE001
                    log.error("xsect stop-exit %s failed (will retry next "
                              "cycle): %s", ticker, e)
