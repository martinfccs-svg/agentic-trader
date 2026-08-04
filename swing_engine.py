"""Swing engine (v6): consumes TREND signals from the price-action scanner.
Daily timeframe. Wide ATR stop, risk-based sizing, liquidity filter, multi-day
hold, NO end-of-day flatten. Works against whichever Broker is wired (paper or
alpaca) — the engine doesn't know or care which.
"""

from __future__ import annotations

import logging
import os

import audit
from config import MIN_DOLLAR_VOL, MIN_PRICE, SWING
from models import Action, Signal, System
from risk import position_size
import portfolio_manager
import loss_cooldown
import exit_exec
import swing_exit_policy
import correlation_manager
from safety import market_is_open
from indicators import ema as _ema  # canonical (2026-08-02)

log = logging.getLogger("swing")

# True when swing is running swing_v2's strategy end to end. Read from the
# same env var swing_v2 uses, so entries and exits can never disagree about
# which strategy the desk is running.
_V2_ROUTE = os.getenv("SWING_V2_ROUTE", "off").strip().lower() in (
    "on", "true", "1", "yes")
_V2_TIME_STOP = int(os.getenv("SWING_V2_TIME_STOP_DAYS", "15"))

# PER-DESK RISK (2026-08-01). The engine sizes every desk at
# RISK_PER_TRADE_PCT (1%), but swing_v2 was written AND BACKTESTED at 0.75%
# — so routing it through the engine unchanged would deploy positions ~33%
# larger than the ones that were measured. Sharpe is scale-invariant, but
# max drawdown is not: the tested -6.6% would become roughly -8.8%, eroding
# the one advantage this strategy robustly has.
#
# RISK_PER_TRADE_PCT is global, so lowering it would shrink meanrev,
# intraday and xsect too. This scales SWING's share count only, leaving the
# other desks alone. Default: match the backtest when routing v2, otherwise
# leave the engine's own figure untouched.
def _swing_risk_pct() -> float:
    env = os.getenv("SWING_RISK_PCT")
    if env:
        return float(env)
    if _V2_ROUTE:
        return float(os.getenv("SWING_V2_RISK_PCT", "0.0075"))
    return _ENGINE_RISK


try:
    from config import RISK_PER_TRADE_PCT as _ENGINE_RISK
except Exception:  # noqa: BLE001
    _ENGINE_RISK = 0.01
_SWING_RISK = None      # resolved lazily so tests can re-read the env


def _trading_days_since(entry_epoch: float) -> int:
    from datetime import date, datetime, timezone
    a = datetime.fromtimestamp(entry_epoch, tz=timezone.utc).date()
    b = datetime.now(timezone.utc).date()
    days, cur = 0, a
    while cur < b:
        cur = date.fromordinal(cur.toordinal() + 1)
        if cur.weekday() < 5:
            days += 1
    return days

# ---------------------------------------------------------------------------
# ENTRY BENCH (2026-07-20 operator decision: "swing has been bleeding").
# SWING_ENTRIES=false suppresses NEW entries only, at the last possible
# moment — after every real gate (kill switch, max positions, liquidity,
# sizing) has passed — so each suppressed line is a full-fidelity shadow
# trade for the A/B against swing_v2.
#
# Deliberately NOT done via ENABLED_SYSTEMS: benching there unbuilds the
# engine, which (a) trips the benched_held boot HALT while the 4 open swing
# positions exist, and (b) kills scans and stop management. This flag keeps
# the engine alive: scans run (ghost #1), manage_open_positions still
# trails/exits the open book, only the buy is withheld.
# ---------------------------------------------------------------------------
SWING_ENTRIES = os.getenv("SWING_ENTRIES", "true").lower() == "true"


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
        # Per-ticker loss cooldown (2026-07-26). From the autopsy: swing's
        # closed trades were 6 on META, 5 of them losses — a re-entry pattern,
        # not six independent bad signals. Blocks re-entry into a name that
        # recently stopped us out. SWING_LOSS_COOLDOWN_DAYS=0 disables.
        _cool, _why = loss_cooldown.in_cooldown("swing", signal.ticker)
        if _cool:
            log.warning("SWING COOLDOWN %s: %s", signal.ticker, _why)
            self._log.record(signal, System.SWING, Action.REJECTED_BY_RISK,
                             f"loss cooldown: {_why}")
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
        # Regime allocation (2026-07-24): scale SHARES, not the equity passed
        # to position_size — scaling equity would also scale the 10%-notional
        # cap, loosening a risk limit as a side effect of a sizing decision.
        # Returns 1.0 unless REGIME_ALLOC=live.
        # Per-desk risk: RECOMPUTED, not scaled. Scaling the number
        # position_size returned is wrong whenever the notional cap binds —
        # and with a 10% cap on liquid names it binds often. Worked example
        # (RTX, equity 95k, risk/share 14.41): the correct 0.75% size is
        # min(49.4 risk-shares, 43.45 cap-shares) = 43.45, while scaling the
        # capped 1% figure gives 43.45 x 0.75 = 32.59 — a 25% under-size that
        # would NOT match the backtest, which applies risk% and the cap in
        # this same order.
        _risk = _swing_risk_pct()
        if abs(_risk - _ENGINE_RISK) > 1e-12:
            try:
                from config import max_position_dollars
                _dist = q.price - stop
                if _dist > 0:
                    _cash = getattr(self._broker, "cash", 1e12)
                    _before = shares
                    shares = min(self._broker.equity * _risk / _dist,
                                 max_position_dollars(self._broker.equity)
                                 / q.price,
                                 _cash / q.price)
                    log.info("swing risk %.4f (engine default %.4f): "
                             "%.2f -> %.2f shares", _risk, _ENGINE_RISK,
                             _before, shares)
            except Exception as e:  # noqa: BLE001 — never break sizing
                log.error("swing per-desk risk failed (%s) — using the "
                          "engine's own size", e)

        # ONE decision point (2026-08-02). Heat, sector budget, correlation,
        # regime and the final notional clamp are evaluated together by
        # portfolio_manager and logged as a single auditable line. These used
        # to be separate calls in each engine — seven call sites across four
        # files, which is precisely how a multiplier gets applied twice.
        shares, _pdec = portfolio_manager.apply(
            shares, self._feed, self._broker, signal.ticker, System.SWING,
            q.price, stop, self._broker.equity)
        if shares <= 0:
            self._log.record(signal, System.SWING, Action.REJECTED_BY_RISK,
                             f"portfolio manager: {_pdec}")
            return
        if shares <= 0:
            self._log.record(signal, System.SWING, Action.REJECTED_BY_RISK, "size=0")
            return
        if not SWING_ENTRIES:
            # Full dry-run complete; withhold only the order. Mirrored to the
            # persistent audit trail (never notifies, never raises) because
            # Railway purges logs on redeploy and these lines ARE the A/B data.
            log.warning("SWING1 SHADOW would_trade %s x%.2f @ %.2f stop=%.2f "
                        "(%s) — entries benched via SWING_ENTRIES=false",
                        signal.ticker, shares, q.price, stop, signal.reason)
            audit.record("swing1_shadow_signal", notify=False,
                         ticker=signal.ticker, shares=round(shares, 2),
                         px=round(q.price, 2), stop=round(stop, 2),
                         reason=signal.reason)
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
                # Bracket legs are how most positions actually close (autopsy:
                # exit paths were overwhelmingly bracket_leg). Arming the
                # cooldown only from the local-stop path would leave it
                # effectively dead.
                if realized is not None and realized < 0:
                    loss_cooldown.note_loss("swing", _t)
        for ticker in list(self._broker.positions):
            pos = self._broker.positions.get(ticker)
            if pos is None or pos.system is not System.SWING:
                continue
            q = self._feed.get_quote(ticker)
            if q is None:
                continue
            self._broker.mark(ticker, q.price)

            # ---- EXIT REGIME (2026-08-01) --------------------------------
            # With SWING_V2_ROUTE=on the desk runs swing_v2's strategy, and
            # its exits are part of what the backtest measured: hold while
            # price stays above the 20-EMA, plus a time stop if the trade has
            # not reached +1R. Grafting v2 entries onto old swing's 2.5xATR
            # trail would deploy a combination that was never tested.
            # The structure stop set at entry stays as the hard floor and is
            # never widened.
            v2_exits = _V2_ROUTE
            if not v2_exits and q.atr is not None:
                pos.stop_price = max(
                    pos.stop_price,
                    pos.high_water - SWING.atr_stop_multiple * q.atr)
            elif v2_exits:
                bars = self._feed.get_daily_bars(ticker)
                closes = bars.close if bars else []
                e20 = _ema(closes, 20) if len(closes) >= 20 else None
                held = _trading_days_since(pos.entry_time)
                r = (pos.entry_price - (pos.entry_stop or pos.stop_price))

                # SHARED POLICY (2026-08-04). This block used to implement
                # swing's exit ladder inline — and it had already drifted from
                # the backtest, which additionally checked volatility
                # expansion and ADX decay. The harness was measuring a
                # strategy this desk does not run. Both now call the same
                # module, so a sweep result and live behaviour cannot describe
                # different things.
                _cfg = swing_exit_policy.SwingExitConfig(
                    trail_atr=float(os.getenv("SWING_V2_TRAIL_ATR", "0")),
                    trail_after_r=float(os.getenv("SWING_V2_TRAIL_AFTER_R", "1.0")),
                    staged_lock=os.getenv("SWING_V2_STAGED_LOCK", "").lower()
                    in ("on", "true", "1", "yes"),
                    adx_trail=os.getenv("SWING_V2_ADX_TRAIL", "").lower()
                    in ("on", "true", "1", "yes"),
                    vol_exit_mult=float(os.getenv("SWING_V2_VOL_EXIT", "0")),
                    adx_decay_frac=float(os.getenv("SWING_V2_ADX_DECAY", "0")),
                    time_stop_days=_V2_TIME_STOP)
                _ctx = swing_exit_policy.SwingExitContext.from_price(
                    entry=pos.entry_price,
                    stop=pos.stop_price, r=r,
                    high_water=max(pos.high_water or pos.entry_price, q.price),
                    held_days=held, price=q.price,
                    atr_now=q.atr, ema20=e20)
                _new_stop, why, _ = swing_exit_policy.evaluate(_ctx, _cfg)
                if _new_stop > pos.stop_price:
                    pos.stop_price = _new_stop
                if why and market_is_open():
                    # Mechanics live in exit_exec: sell, book, notify, arm the
                    # cooldown, contain failure. The POLICY above (which
                    # reason fired) stays here, where the desk's own rules
                    # belong.
                    exit_exec.close_position(
                        self._broker, self._log, ticker, q.price, why,
                        System.SWING, self._notifier, "swing")
                    continue
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
                    if realized is not None and realized < 0:
                        loss_cooldown.note_loss("swing", ticker)
                    if exit_price is not None and realized is not None:
                        self._notifier.notify_exit(
                            ticker=ticker, shares=shares,
                            exit_price=exit_price, entry_price=entry_price,
                            pnl=realized, system=System.SWING.value,
                        )
                except Exception as e:  # noqa: BLE001 — one exit must not kill the loop
                    log.error("swing stop-exit %s failed (retry next cycle): %s",
                              ticker, e)
