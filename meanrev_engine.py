"""Mean reversion engine (RSI).

Contrarian to the momentum books: buys oversold names (RSI low) that are still
in a longer-term uptrend, and exits when RSI recovers toward the mean — or on a
protective ATR stop. Tracked as its own system so its return stream can be
correlation-checked against the momentum books (that's the whole point).
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone

from config import MEANREV, MIN_DOLLAR_VOL, MIN_PRICE
from indicators import rsi
import meanrev_scoring as mrs
import portfolio_risk
from models import Action, Signal, System
from risk import position_size
from safety import market_is_open

log = logging.getLogger("meanrev")

# ---------------------------------------------------------------------------
# TIME STOP (2026-07-22 gap fix). Mean reversion's thesis is time-bound —
# "the dip snaps back within days" (STRATEGY_REFERENCE: holding period days)
# — but this engine had no clock. A position whose RSI recovered to just
# under rsi_exit and stalled, price above the STATIC stop, sat in one of
# only 4 slots indefinitely with an expired thesis. Swing resolves every
# trade eventually via its trailing stop; meanrev was the one engine where
# dead money could live rent-free. After N TRADING days without the RSI
# exit or a stop, the position is closed as dead money.
# MEANREV_TIME_STOP_DAYS=0 disables (restores prior behavior, no redeploy).
# ---------------------------------------------------------------------------
MEANREV_TIME_STOP_DAYS = int(os.getenv("MEANREV_TIME_STOP_DAYS", "10"))


def _trading_days_since(entry_epoch: float) -> int:
    """Weekday count from entry date to today (UTC dates). Holidays are
    counted as trading days — a day of slack on a 10-day stop is
    immaterial and not worth a calendar dependency."""
    a = datetime.fromtimestamp(entry_epoch, tz=timezone.utc).date()
    b = datetime.now(timezone.utc).date()
    days, cur = 0, a
    while cur < b:
        cur = date.fromordinal(cur.toordinal() + 1)
        if cur.weekday() < 5:
            days += 1
    return days


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
        # Portfolio heat (2026-07-24): total open risk across ALL strategies,
        # not just this one. MEASURE-ONLY unless PORTFOLIO_HEAT_MAX > 0, so
        # this changes nothing until you have watched the numbers.
        heat_ok, heat_why = portfolio_risk.check(self._broker, System.MEANREV)
        if not heat_ok:
            self._log.record(signal, System.MEANREV, Action.REJECTED_BY_RISK,
                             heat_why)
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
        # Conviction sizing (2026-07-24): only in live scoring mode, where a
        # scorecard actually gated the entry. Scales the SHARE COUNT, never
        # the equity passed to position_size — scaling equity would also
        # scale the 10%-of-equity notional cap, loosening a risk limit as a
        # side effect of a sizing preference.
        if mrs.SCORING_MODE == "live":
            card = getattr(signal, "raw", {}).get("card") if signal.raw else None
            if card is not None:
                mult = mrs.risk_multiplier(card.score)
                shares = shares * mult
                log.info("meanrev conviction sizing %s: score=%d/6 "
                         "mult=%.2f shares=%.2f", signal.ticker, card.score,
                         mult, shares)
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
            # Self-heal a missing stop (2026-07-16). reconcile now adopts
            # with stop=0.0 when it cannot discover one — deliberately
            # unreachable, so nothing invented can force an exit. But this
            # engine has NO trailing logic to re-derive one (swing does), so
            # the position would sit with no local stop indefinitely. Worse:
            # the sell path CANCELS the broker leg before selling, so a sell
            # that fails (e.g. submitted after hours) leaves no broker stop
            # either — exactly the state UNH/INTC/MU were left in.
            #
            # Re-derive the position's ORIGINAL intent: entry - MULT x daily
            # ATR. On MU that reconstructs 612.84 against a real designed stop
            # of 612.10. If ATR has collapsed since entry so that the
            # entry-anchored stop sits AT OR ABOVE the live price, anchor to
            # price instead — a stop above the market is the Jul 16 bug.
            if not pos.stop_price and q.atr:
                derived = pos.entry_price - MEANREV.atr_stop_multiple * q.atr
                if derived >= q.price:
                    derived = q.price - MEANREV.atr_stop_multiple * q.atr
                pos.stop_price = derived
                if not pos.entry_stop:
                    pos.entry_stop = derived
                log.critical("meanrev: %s had NO stop — re-derived %.2f "
                             "(entry %.2f - %.1f x daily ATR %.2f). LOCAL "
                             "stop only: verify a broker-side GTC stop "
                             "exists, this one dies with the process.",
                             ticker, derived, pos.entry_price,
                             MEANREV.atr_stop_multiple, q.atr)
            bars = self._feed.get_daily_bars(ticker)
            r = rsi(bars.close, MEANREV.rsi_period) if bars else None
            held = _trading_days_since(pos.entry_time)

            # ---- EXIT LADDER (2026-07-22): live scoring mode only ---------
            # Breakeven at +1R, then 2xATR trail (stop only ratchets UP),
            # then final-RSI / trend-reversal(close<EMA200) / time — computed
            # by the same pure function the backtest uses. NOTE: when this
            # mode goes live, set MEANREV_USE_TAKE_PROFIT=false so the OTO
            # stop-only bracket lets the ladder own profit-taking; the 3R TP
            # leg would otherwise exit before the trail ever engages.
            # In shadow/off modes the ORIGINAL exit logic below runs
            # unchanged.
            if mrs.SCORING_MODE == "live" and bars is not None:
                e200 = mrs.ema(bars.close, mrs.EMA_SLOW)
                new_stop, ladder_reason = mrs.ladder_decision(
                    price=q.price, entry=pos.entry_price,
                    entry_stop=pos.entry_stop or pos.stop_price,
                    stop=pos.stop_price, high_water=pos.high_water,
                    atr14=q.atr, ema200=e200, last_close=bars.close[-1],
                    rsi_value=r, rsi_exit=MEANREV.rsi_exit,
                    held_days=held,
                    time_stop_days=MEANREV_TIME_STOP_DAYS,
                    atr_stop_multiple=MEANREV.atr_stop_multiple)
                if new_stop > pos.stop_price:
                    pos.stop_price = new_stop
                if ladder_reason and market_is_open():
                    log.warning("meanrev LADDER exit %s: reason=%s held=%dd "
                                "px=%.2f stop=%.2f", ticker, ladder_reason,
                                held, q.price, pos.stop_price)
                    try:
                        exit_price = q.price
                        entry_price = pos.entry_price
                        shares = pos.shares
                        realized = self._broker.sell(ticker, exit_price)
                        self._log.record_close(System.MEANREV, realized)
                        if exit_price is not None and realized is not None:
                            self._notifier.notify_exit(
                                ticker=ticker, shares=shares,
                                exit_price=exit_price,
                                entry_price=entry_price, pnl=realized,
                                system=System.MEANREV.value)
                    except Exception as e:  # noqa: BLE001
                        log.error("meanrev ladder exit %s failed (retry "
                                  "next cycle): %s", ticker, e)
                continue   # ladder owns this position; skip legacy exit
            # Exit reasons in priority order, RECORDED (2026-07-22): the old
            # combined boolean made a reverted winner, a stopped loser, and
            # expired dead money indistinguishable in the logs — a hole the
            # autopsy would have hit. rsi_reverted > stop > time.
            reason = None
            if r is not None and r >= MEANREV.rsi_exit:
                reason = f"rsi_reverted({r:.1f}>={MEANREV.rsi_exit:.0f})"
            elif q.price <= pos.stop_price:
                reason = "stop"
            elif MEANREV_TIME_STOP_DAYS and held >= MEANREV_TIME_STOP_DAYS:
                reason = (f"time({held}d,rsi={r:.1f})" if r is not None
                          else f"time({held}d)")
            hit_exit = reason is not None
            # Local stop is a BACKUP to the broker-side GTC leg, which is
            # live 24/7. Firing it while the market is CLOSED just sells at a
            # stale quote — on 2026-07-16 that dumped UNH/INTC/MU at
            # "quote-est" prices 30 min after the bell. If a stop is genuinely
            # hit during the session, the broker's own leg fills it.
            if hit_exit and market_is_open():
                log.warning("meanrev exit %s: reason=%s held=%dd px=%.2f "
                            "stop=%.2f", ticker, reason, held, q.price,
                            pos.stop_price)
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
