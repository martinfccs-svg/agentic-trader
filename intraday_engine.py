"""Intraday engine (v6): consumes MOMENTUM signals from the price-action scanner.

Intraday (1-min) timeframe. The scanner already required relvol spike + above
VWAP + opening-range break, so a fired MOMENTUM signal IS a confirmed entry.
Tight ATR stop, percentage trailing exit, HARD EOD flatten.
"""

from __future__ import annotations

import logging
import os

import audit
import intraday_scoring as ids
from config import INTRADAY, MIN_DOLLAR_VOL, MIN_PRICE
from indicators import atr, avg_dollar_volume
from models import Action, Signal, System
from risk import position_size

log = logging.getLogger("intraday")

# ---------------------------------------------------------------------------
# RE-ACTIVATION GATE (2026-07-23). Intraday was benched Jul 8 for negative-
# expectancy churn under the OLD parameters (1.0xATR hair-trigger stops, a
# 6% trail that never engaged). Both were since fixed in config (2.5xATR,
# 1% trail) — but the fixed version has NEVER traded. Same discipline as
# the swing bench, inverted: re-enable the SYSTEM (ENABLED_SYSTEMS includes
# "intraday") with INTRADAY_ENTRIES=false, and every would-be trade logs as
# a full-fidelity shadow — after every real gate, before the buy — until
# the shadow record earns the flip to true. Scans, exits, and the EOD
# flatten run either way.
# ---------------------------------------------------------------------------
INTRADAY_ENTRIES = os.getenv("INTRADAY_ENTRIES", "true").lower() == "true"

# When entries are LIVE, optionally require the v2 scorecard to approve
# (gates + score >= INTRADAY_SCORE_MIN). Default off: flipping entries on
# restores plain v6 behavior; flipping this on makes v2 the gatekeeper.
# Fixes the reviewer-caught gap where live mode computed the score and then
# ignored it. Fail-open: if the card can't be computed, v6 decides.
INTRADAY_V2_GATE = os.getenv("INTRADAY_V2_GATE", "false").lower() == "true"


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
        if ids.in_cooldown(signal.ticker):
            # Loss cooldown (2026-07-23): the direct fix for the Jul-8 churn
            # pattern — no immediate re-entry into a name that just stopped
            # us out. Applies in shadow AND live.
            self._log.record(signal, System.INTRADAY, Action.REJECTED_BY_RISK,
                             "cooldown after loss")
            return
        if self._open() >= INTRADAY.max_positions or signal.ticker in self._broker.positions:
            self._log.record(signal, System.INTRADAY, Action.REJECTED_BY_RISK)
            return
        q = self._feed.get_quote(signal.ticker)
        if q is None:
            self._log.record(signal, System.INTRADAY, Action.REJECTED_BY_RISK, "no quote")
            return
        # Liquidity on DAILY dollar volume. (As of 2026-07-15 the quote
        # reports daily-scale liquidity by contract, but fetching daily bars
        # here is explicit and costs nothing — they're cached.)
        daily = self._feed.get_daily_bars(signal.ticker)
        daily_dv = avg_dollar_volume(daily) if daily else None
        if q.price < MIN_PRICE or (daily_dv or 0) < MIN_DOLLAR_VOL:
            self._log.record(signal, System.INTRADAY, Action.REJECTED_BY_LIQUIDITY)
            return
        # INTRADAY-scale ATR, computed explicitly from 1-min bars. q.atr is
        # now DAILY-scale by contract (see feed_layer.get_quote): using it
        # here would make this engine's stop ~20x too wide for a strategy
        # that flattens before the close. This engine is the one place that
        # legitimately wants 1-minute risk scale, so it asks for it directly.
        intra = self._feed.get_intraday_bars(signal.ticker)
        intra_atr = atr(intra) if intra else None
        if intra_atr is None:
            self._log.record(signal, System.INTRADAY, Action.REJECTED_BY_RISK, "no intraday atr")
            return
        # cooldown re-check with closes: early release if price reclaimed
        # its 1-min EMA20 (trend resumed != revenge trade)
        if ids.in_cooldown(signal.ticker, closes_1m=intra.close):
            self._log.record(signal, System.INTRADAY, Action.REJECTED_BY_RISK,
                             "cooldown after loss (EMA20 not reclaimed)")
            return
        stop = q.price - INTRADAY.atr_stop_multiple * intra_atr
        shares = position_size(self._broker.equity, q.price, stop, getattr(self._broker, "cash", 1e12))
        if shares <= 0:
            self._log.record(signal, System.INTRADAY, Action.REJECTED_BY_RISK, "size=0")
            return
        # v2 scorecard (2026-07-23): computed for EVERY qualified signal so
        # each shadow line carries both the v6 verdict (would_trade) and the
        # v2 score — one record, two strategies' evidence. Uses only data
        # already fetched this cycle; 5m/15m resampled locally, zero extra
        # Finnhub calls. Scoring failure must never block v6.
        card = None
        try:
            spy_q = self._feed.get_quote("SPY")
            spy_i = self._feed.get_intraday_bars("SPY")
            spy_open_ret = (spy_i.close[-1] / spy_i.close[0] - 1) \
                if spy_i and spy_i.close else None
            spy_d = self._feed.get_daily_bars("SPY")   # slow-TTL cached
            spy_above_e50 = None
            if spy_d and len(spy_d.close) >= 50 and spy_q:
                e50 = ids.ema(spy_d.close, 50)
                if e50 is not None:
                    spy_above_e50 = spy_q.price > e50
            card = ids.score_intraday(
                signal.ticker, intra.close, intra.high, intra.low,
                intra.volume, q.price, q.vwap, intra_atr, q.rel_volume,
                spy_q.price if spy_q else None,
                spy_q.vwap if spy_q else None, spy_open_ret,
                spy_above_ema50=spy_above_e50)
        except Exception as e:  # noqa: BLE001
            log.warning("intraday scorecard failed for %s: %s",
                        signal.ticker, e)
        card_str = ""
        if card:
            card_str = (" | v2: score=%.2f gates[win=%s mkt=%s rv=%s vol=%s] "
                        "v2_stop=%s (v6_stop=%.2f)"
                        % (card.score, card.gate_window, card.gate_market,
                           card.gate_rv, card.gate_volband,
                           card.v2_stop, stop))

        if INTRADAY_ENTRIES and INTRADAY_V2_GATE and card is not None \
                and not card.qualifies():
            self._log.record(signal, System.INTRADAY,
                             Action.REJECTED_BY_CONFIRMATION,
                             f"v2 gate: score={card.score:.2f} "
                             f"gates_ok={card.gates_ok}")
            return

        if INTRADAY_ENTRIES and INTRADAY_V2_GATE and card is not None \
                and card.v2_stop is not None and card.v2_stop < q.price:
            # Live-v2 coherence (2026-07-23 review): the stop that GATED the
            # trade must also SIZE and PROTECT it — sizing on v6's ATR stop
            # while entering on v2's approval risks one level and executes
            # another. Score-tiered sizing rides the same branch: full size
            # only for the highest-conviction cards. Shadow and live-v6
            # rungs are untouched by any of this.
            stop = card.v2_stop
            shares = position_size(self._broker.equity, q.price, stop,
                                   getattr(self._broker, "cash", 1e12))
            tier = (1.0 if card.score >= 0.85
                    else 0.75 if card.score >= 0.75 else 0.5)
            shares = shares * tier
            if shares <= 0:
                self._log.record(signal, System.INTRADAY,
                                 Action.REJECTED_BY_RISK,
                                 "size=0 (v2 stop/tier)")
                return
            log.info("intraday v2-live sizing %s: stop=%.2f (structure) "
                     "tier=%.2f shares=%.2f score=%.2f", signal.ticker,
                     stop, tier, shares, card.score)

        if not INTRADAY_ENTRIES:
            # Full dry-run complete; withhold only the order. Mirrored to the
            # persistent audit trail (Railway purges logs) — these lines ARE
            # the re-activation evidence.
            log.warning("INTRADAY SHADOW would_trade %s x%.2f @ %.2f "
                        "stop=%.2f (%s)%s — entries gated via "
                        "INTRADAY_ENTRIES=false", signal.ticker, shares,
                        q.price, stop, signal.reason, card_str)
            audit.record("intraday_shadow_signal", notify=False,
                         ticker=signal.ticker, shares=round(shares, 2),
                         px=round(q.price, 2), stop=round(stop, 2),
                         reason=signal.reason,
                         v2_score=(card.score if card else None),
                         v2_gates_ok=(card.gates_ok if card else None),
                         v2_stop=(card.v2_stop if card else None),
                         v2_parts=({k: round(v, 3)
                                    for k, v in card.parts.items()}
                                   if card else None))
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
        # First, book any positions whose bracket legs filled broker-side.
        # 2026-07-08: AMZN/NVDA legs filled and the phantoms lingered 8+ min,
        # blocking re-entry and inflating unrealized. Now they're closed in
        # the books at the leg's actual fill price on the next cycle.
        if hasattr(self._broker, "reconcile_filled_legs"):
            for _ticker, realized in \
                    self._broker.reconcile_filled_legs(System.INTRADAY).items():
                self._log.record_close(System.INTRADAY, realized)

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
                    realized = self._broker.sell(ticker, q.price)
                    self._log.record_close(System.INTRADAY, realized)
                    if realized is not None and realized < 0:
                        # feed the loss cooldown: no immediate re-entry into
                        # a name that just stopped us out (Jul-8 churn fix)
                        ids.note_loss(ticker)
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
