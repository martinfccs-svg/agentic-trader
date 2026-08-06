"""swing_exit_policy.py — ONE swing exit policy, used by live AND replay.

exit_rules.py centralised the exit ARITHMETIC. This centralises the swing
desk's exit POLICY: which rules apply, in what order, under what conditions.
Those are different things, and leaving the second duplicated undid much of
the benefit of fixing the first.

THE DRIFT THIS CLOSES, measured before it was written:

    backtest order : stop/gap -> vol_expansion -> adx_decay -> ema20 -> time
    live order     : ema20 -> time

The live engine checked neither volatility expansion nor ADX decay. So if the
sweep had promoted `vol_exit`, the live desk would not have implemented it —
the harness would have measured a strategy the bot does not run. That is the
same class of failure as the vanished hold_SPY row and the harness that never
exercised its own gates, and it is the third time it has appeared.

WHY THIS DOES NOT CONTRADICT "POLICY STAYS PER DESK"

Two different claims:
  * meanrev's ladder and swing's exits should stay separate  — still true,
    they answer different questions and merging them would invent a shape
    they do not share.
  * swing's LIVE policy and swing's REPLAY policy should be one thing —
    also true, and previously violated. One desk, one policy, two callers.

meanrev_exit_policy and xsect_exit_policy would be separate modules with
their own orderings, not branches inside this one.

CALLERS SUPPLY THEIR OWN WORLD
The live engine has a quote; the backtest has an OHLC bar. Both build a
SwingExitContext and get back the same decision. Gap handling degrades
correctly when only a price is available.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import exit_rules


@dataclass
class SwingExitConfig:
    """Which options are on. Defaults match the LIVE production defaults —
    structure stop, EMA20 trend break, time stop — with everything the sweep
    has not yet promoted switched off.

    The right-tail argument is why the defaults are sparse: a trend strategy
    earns its return from the few trades that run furthest, and each
    additional exit is another chance to cut one of them short. Options are
    added here to be MEASURED, not to be stacked.
    """
    trail_atr: float = 0.0          # 0 = off
    trail_after_r: float = 1.0
    adx_trail: bool = False
    staged_lock: bool = False
    percent_lock: bool = False      # profit floor in PERCENT, not ATR
    vol_exit_mult: float = 0.0      # 0 = off
    adx_decay_frac: float = 0.0     # 0 = off
    rs_exit_lag: float = 0.0        # 0 = off; e.g. 0.05 = 5pp behind bench
    rs_exit_min_days: int = 5
    ema20_grace_days: int = 2
    time_stop_days: int = 15
    time_stop_require_r: float = 1.0


@dataclass
class SwingExitContext:
    """One position, one bar. Live passes price for open/high/low/close."""
    entry: float
    stop: float
    r: float                       # per-share risk at entry
    high_water: float
    held_days: int
    open: float
    high: float
    low: float
    close: float
    atr_now: Optional[float] = None
    atr_at_entry: Optional[float] = None
    ema20: Optional[float] = None
    adx_now: Optional[float] = None
    adx_at_entry: Optional[float] = None
    # Returns SINCE ENTRY, for the relative-strength exit. Callers compute
    # them from their own data; the policy only compares.
    stock_return: Optional[float] = None
    bench_return: Optional[float] = None

    @classmethod
    def from_price(cls, entry, stop, r, high_water, held_days, price,
                   **kw):
        """Live convenience: one price stands in for the whole bar. Gap
        handling then reduces to a simple stop check, which is correct —
        a live quote cannot tell you where the session opened."""
        return cls(entry=entry, stop=stop, r=r, high_water=high_water,
                   held_days=held_days, open=price, high=price, low=price,
                   close=price, **kw)


def evaluate(ctx: SwingExitContext, cfg: SwingExitConfig
             ) -> tuple[float, Optional[str], Optional[float]]:
    """(new_stop, exit_reason or None, exit_price or None).

    ORDER MATTERS and is the policy:
      1. ratchet the stop UP first, so an exit this bar uses the tightest
         stop the trade has earned
      2. stop / gap — the hard floor, checked before anything discretionary
      3. volatility expansion — the environment the size assumed is gone
      4. ADX decay — the trend that justified the entry is gone
      5. EMA20 close, after a grace period — the thesis broke
      6. time stop — the trade has not worked and the capital can
    """
    stop = ctx.stop

    # ---- 1. ratchet UP (never widens; each helper enforces that) --------
    if cfg.trail_atr > 0 and ctx.atr_now and ctx.r > 0:
        stop = exit_rules.trail_after_r(
            ctx.entry, ctx.high_water, ctx.r, cfg.trail_after_r,
            ctx.atr_now, cfg.trail_atr, stop)

    if cfg.adx_trail and ctx.atr_now and ctx.r > 0 \
            and ctx.high_water >= ctx.entry + ctx.r:
        stop = exit_rules.ratchet_stop(
            stop, ctx.high_water, ctx.atr_now,
            exit_rules.select_atr_trail_band(ctx.adx_now))

    if cfg.staged_lock and ctx.atr_now:
        stop, _ = exit_rules.staged_profit_lock(
            ctx.entry, ctx.high_water, ctx.atr_now, stop, ctx.ema20)

    # Composes with the ATR ladder — whichever stop is HIGHER wins, because
    # both only ratchet up. On a low-volatility name the ATR rungs are finer;
    # on a volatile one (ARM at 11.3% ATR) they are unreachable and this is
    # the only thing that locks anything.
    if cfg.percent_lock:
        stop, _ = exit_rules.percent_profit_lock(
            ctx.entry, ctx.high_water, stop)

    # ---- 2. the hard floor ---------------------------------------------
    fill, why = exit_rules.gap_exit(ctx.open, stop, ctx.low)
    if why:
        return stop, ("gap_stop" if why.startswith("gap") else "stop"), fill

    # ---- 3. volatility expansion ---------------------------------------
    if cfg.vol_exit_mult > 0:
        why = exit_rules.volatility_exit(ctx.atr_now, ctx.atr_at_entry,
                                         cfg.vol_exit_mult)
        if why:
            return stop, "vol_expansion", ctx.close

    # ---- 4. ADX decay ---------------------------------------------------
    if cfg.adx_decay_frac > 0 and ctx.adx_at_entry and ctx.adx_now is not None:
        if ctx.adx_now < cfg.adx_decay_frac * ctx.adx_at_entry:
            return stop, "adx_decay", ctx.close

    # ---- 4b. relative strength decay -----------------------------------
    # Before the trend break on purpose: a name can stop leading the index
    # while its own trend is still technically intact, and that is precisely
    # the case price-based exits miss.
    if cfg.rs_exit_lag > 0:
        why = exit_rules.relative_strength_exit(
            ctx.stock_return, ctx.bench_return, cfg.rs_exit_lag,
            ctx.held_days, cfg.rs_exit_min_days)
        if why:
            return stop, "rs_decay", ctx.close

    # ---- 5. trend break -------------------------------------------------
    if ctx.held_days >= cfg.ema20_grace_days and \
            exit_rules.trend_exit(ctx.close, ctx.ema20):
        return stop, "ema20", ctx.close

    # ---- 6. time stop ---------------------------------------------------
    why = exit_rules.time_stop(ctx.held_days, cfg.time_stop_days,
                               ctx.close, ctx.entry, ctx.r,
                               cfg.time_stop_require_r)
    if why:
        return stop, "time", ctx.close

    return stop, None, None
