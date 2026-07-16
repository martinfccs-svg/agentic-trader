"""Cross-sectional (relative) momentum engine.

Different shape from the other engines: instead of per-ticker signals, it ranks
the WHOLE universe by trailing return and holds the top N. On a rebalance
cadence it sells names that dropped out of the top N and buys the new entrants.
A wide ATR stop is the only between-rebalance safety net.

This is relative strength ("own the strongest names"), not absolute breakout —
a distinct return stream from the time-series momentum in the swing book.

2026-07-09 changes (after two sessions of zero positions):
  1. CADENCE: XSECT.rebalance_cycles replaced by scan_health.DailyRebalanceGate.
  2. LIQUIDITY UNIT BUG: gate now tests daily-bar dollar volume (was 1-min,
     ~390x too strict — same bug fixed in the intraday engine on Jul 3).
  3. TRANSPARENCY: every rebalance logs per-gate stats, top-N, ENTER/EXIT.
  4. ROBUSTNESS: per-ticker try/except; broker-side leg fills reconciled.

2026-07-09 PM incident fixes (the -$196 forced rotation at 15:03 UTC):
  5. DEGRADED-DATA GUARD: with the quote breaker open, only 2/63 names were
     rankable; the rebalance treated that 2-name "top 3" as authoritative
     and dumped INTC/MU. A rotation now requires minimum ranking coverage
     (XSECT_MIN_RANKABLE, default max(2*top_n, 40% of universe)); below it,
     the whole rotation is SKIPPED — no exits, no entries — and the gate
     re-arms to retry once the feed recovers.
  6. ROTATION ATOMICITY: if the kill switch blocks entries, the exits are
     skipped too. A rotation is a swap; selling the old names while unable
     to buy the new ones just liquidates the book into an outage. (Stop-
     loss exits in manage_open_positions remain ungated — those ARE
     risk-reducing and always run.)
  7. QUOTE BURST REDUCTION: ranking no longer quotes all 63 names (that
     burst helped trip the 429s/breaker). Ranking uses daily bars only;
     quotes are fetched just for the handful of names actually traded.
"""

from __future__ import annotations

import logging
import os

from config import MIN_DOLLAR_VOL, MIN_PRICE, XSECT
from indicators import avg_dollar_volume, trailing_return
from models import Action, Signal, SignalSource, System
from risk import position_size
from safety import market_is_open
from scan_health import DailyRebalanceGate

log = logging.getLogger("xsectmom")

# Minimum rankable names required before a rotation may trade. 0 = auto:
# max(2 * top_n, 40% of universe). Below the threshold the ranking is
# considered blind (feed degradation) and the rotation is skipped.
XSECT_MIN_RANKABLE = int(os.getenv("XSECT_MIN_RANKABLE", "0"))


class NullNotifier:
    """No-op stand-in for the optional trade notifier (2026-07-16). See the
    note in swing_engine.py."""

    def __getattr__(self, _name):
        return lambda *a, **k: None


class CrossSectionalMomentumEngine:
    # NOTE: `notifier` is optional and comes AFTER `universe` so main.py's
    # positional call (feed, broker, kill, logger, UNIVERSE) binds correctly.
    # The repo had it BEFORE universe and REQUIRED, which is what made
    # build() raise TypeError and blocked the 2026-07-16 deploy.
    def __init__(self, feed, broker, kill, logger, universe, notifier=None):
        self._feed, self._broker, self._kill, self._log = feed, broker, kill, logger
        self._notifier = notifier or NullNotifier()
        self._universe = universe
        self._gate = DailyRebalanceGate()   # 10:00 ET; XSECT_REBALANCE_ET to change

    def handle_signal(self, signal: Signal):
        # Not signal-driven; rebalance() does the work. No-op keeps the router happy.
        return

    def _rank(self):
        """Score the universe from DAILY BARS ONLY. Returns (ranked, stats).

        Deliberately quote-free: quoting all 63 names during ranking helped
        trip the Finnhub 429s / breaker on 2026-07-09. Price and liquidity
        gates use the daily bars already in hand; live quotes are fetched
        later, only for the few names actually being traded."""
        scored = []
        stats = {"universe": len(self._universe), "no_bars": 0,
                 "no_return": 0, "price_reject": 0, "liquidity_reject": 0}
        for t in self._universe:
            bars = self._feed.get_daily_bars(t)
            if bars is None or not bars.close:
                stats["no_bars"] += 1
                continue
            ret = trailing_return(bars.close, XSECT.lookback_days, XSECT.skip_days)
            if ret is None:
                stats["no_return"] += 1        # insufficient history for lookback
                continue
            if bars.close[-1] < MIN_PRICE:
                stats["price_reject"] += 1
                continue
            # Liquidity on DAILY dollar volume (the Jul-3 intraday fix,
            # previously unfixed here: quote-level ADV was ~390x too small).
            daily_dv = avg_dollar_volume(bars)
            if (daily_dv or 0) < MIN_DOLLAR_VOL:
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
            # Fire time is >=10:00 ET, so "closed" here means holiday or
            # post-16:00 ET — no rebalance is possible for the rest of the
            # day. Mark done; re-arming would loop the gate every cycle.
            log.info("xsect rebalance: gate open but market closed — "
                     "marking today done")
            self._gate.mark_done_today()
            return
        self.rebalance()

    def rebalance(self):
        scored, stats = self._rank()
        held = {t for t, p in self._broker.positions.items()
                if p.system is System.XSECTMOM}
        top_str = ", ".join(f"{t}({r:+.1%})" for r, t in scored[:XSECT.top_n]) or "EMPTY"
        log.warning("xsect rebalance: ranked=%d/%d (no_bars=%d no_return=%d "
                    "price_reject=%d liquidity_reject=%d) | top%d: %s | held=%s",
                    len(scored), stats["universe"], stats["no_bars"],
                    stats["no_return"], stats["price_reject"],
                    stats["liquidity_reject"], XSECT.top_n, top_str,
                    sorted(held) or "none")

        # ---- DEGRADED-DATA GUARD (2026-07-09: the -$196 forced rotation) ----
        # A ranking built from a sliver of the universe is blind; acting on it
        # sold INTC/MU because they "fell out" of a top-3 computed from 2
        # names. Below minimum coverage: no exits, no entries, retry later.
        min_rankable = XSECT_MIN_RANKABLE or max(
            2 * XSECT.top_n, int(0.4 * stats["universe"]))
        if len(scored) < min_rankable:
            log.critical("xsect rebalance: DEGRADED DATA — only %d/%d names "
                         "rankable (need >=%d). Rotation SKIPPED (no exits, "
                         "no entries); gate re-armed to retry once the feed "
                         "recovers. If no_return dominates it's history "
                         "starvation; if no_bars dominates the candle "
                         "endpoint/breaker is down.",
                         len(scored), stats["universe"], min_rankable)
            self._gate.rearm()
            return

        # ---- ROTATION ATOMICITY (2026-07-09) --------------------------------
        # A rotation is a swap: selling the old names while the kill switch
        # blocks buying the new ones just liquidates the book into an outage.
        # If entries can't happen, skip the whole rotation and retry later.
        # (Stop-loss exits in manage_open_positions remain ungated.)
        if not self._kill.may_open(System.XSECTMOM):
            log.warning("xsect rebalance: kill switch active — rotation "
                        "SKIPPED whole (exits AND entries); holdings keep "
                        "their broker-side stops; gate re-armed")
            self._gate.rearm()
            return

        target = {t for _, t in scored[:XSECT.top_n]}

        # Sell names that fell out of the top N.
        for ticker in sorted(held - target):
            q = self._feed.get_quote(ticker)
            pos = self._broker.positions.get(ticker)
            if pos is None:
                continue
            price = q.price if q and q.price else pos.entry_price
            try:
                entry_price = pos.entry_price
                shares = pos.shares
                realized = self._broker.sell(ticker, price)
                self._log.record_close(System.XSECTMOM, realized)
                if price is not None and realized is not None:
                    self._notifier.notify_exit(
                        ticker=ticker, shares=shares, exit_price=price,
                        entry_price=entry_price, pnl=realized,
                        system=System.XSECTMOM.value,
                    )
                log.warning("xsect rebalance: EXIT %s (fell out of top%d)",
                            ticker, XSECT.top_n)
            except Exception as e:  # noqa: BLE001 — one exit must not block the rest
                log.error("xsect rebalance: exit %s failed (retry next "
                          "rebalance): %s", ticker, e)

        # Buy new entrants (kill switch already verified before the exits —
        # rotation atomicity: we only get here if entries are permitted).
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
            self._notifier.notify_entry(
                ticker=ticker, shares=shares, price=q.price,
                system=System.XSECTMOM.value,
                source=SignalSource.REL_STRENGTH.value,
            )
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
                    self._log.record_close(System.XSECTMOM, realized)
                    if exit_price is not None and realized is not None:
                        self._notifier.notify_exit(
                            ticker=ticker, shares=shares,
                            exit_price=exit_price, entry_price=entry_price,
                            pnl=realized, system=System.XSECTMOM.value,
                        )
                except Exception as e:  # noqa: BLE001
                    log.error("xsect stop-exit %s failed (will retry next "
                              "cycle): %s", ticker, e)
