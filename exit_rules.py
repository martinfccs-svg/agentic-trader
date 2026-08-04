"""exit_rules.py — composable exit PREDICATES. A library, not a manager.

Same shape as indicators.py: shared implementations, local policy. Each
function answers one narrow question and returns either a reason string or
None. Nothing here decides which rules a desk uses, in what order, or with
what parameters — that is the strategy's job and it stays in the strategy.

WHY THIS EXISTS
The mechanics of exiting were consolidated into exit_exec (sell, book,
notify, arm the cooldown). The ARITHMETIC underneath the policies was still
duplicated four times:

    xsection        tiered ATR trail  (3xATR at +10%, 2xATR at +20%)
    swing_v2        ATR trail after +1R
    swing_engine    ATR trail from high water
    meanrev_scoring ATR trail inside its six-rung ladder

All four implement "ratchet the stop up toward a high-water mark, never
widen it". Four copies means four places to get the never-widen guarantee
right, and the newest was written days ago. That is the same argument that
folded seven EMA implementations into indicators.py — not tidiness, but the
observation that duplicated arithmetic drifts and nothing reports it.

WHAT THIS IS NOT
Not an ExitPolicy engine that owns exit decisions. meanrev's ladder,
swing_v2's EMA20-plus-time, xsect's rank-based rotation and intraday's clock
are not variants of one abstraction — merging them would mean inventing a
common shape they do not have. Every function below is a pure function of
its arguments, so a desk can use one, several, or none.
"""

from __future__ import annotations

from typing import Optional


def ratchet_stop(current_stop: float, high_water: float, atr: Optional[float],
                 atr_mult: float) -> float:
    """The never-widen guarantee, in one place.

    Returns the higher of the current stop and (high_water - atr_mult*atr).
    A stop returned by this function can only ever move UP. Every desk that
    trails was reimplementing exactly this comparison.
    """
    if atr is None or atr <= 0 or atr_mult <= 0:
        return current_stop
    return max(current_stop, high_water - atr_mult * atr)


def tiered_trail_mult(entry: float, high_water: float,
                      tiers: list[tuple[float, float]]) -> Optional[float]:
    """Pick an ATR multiple from a gain ladder, or None if no tier applies.

    tiers: [(gain_fraction, atr_mult), ...] — evaluated best-tier-first, so
    order them loosest-gain last. xsect uses [(0.20, 2.0), (0.10, 3.0)]:
    tighter once more profit is banked.
    """
    if entry <= 0:
        return None
    gain = (high_water / entry) - 1.0
    for min_gain, mult in sorted(tiers, key=lambda t: -t[0]):
        if gain >= min_gain:
            return mult
    return None


def trail_after_r(entry: float, high_water: float, r: float,
                  after_r: float, atr: Optional[float],
                  atr_mult: float, current_stop: float) -> float:
    """ATR trail that only engages once `after_r` of open profit is banked.

    Engaging immediately just converts the structure stop into a tighter one
    and shakes the trade out in normal noise before the thesis has room —
    which is why swing_v2 gates its trail this way.
    """
    if r <= 0 or entry <= 0:
        return current_stop
    if high_water < entry + after_r * r:
        return current_stop
    return ratchet_stop(current_stop, high_water, atr, atr_mult)


def ratchet_stop_pct(current_stop: float, high_water: float,
                     pct: float) -> float:
    """Percentage trail — the same never-widen guarantee, different metric.

    Intraday trails a fixed percentage of the high-water mark rather than a
    multiple of ATR, because on 1-minute bars ATR is a noisy denominator and
    the desk's stops are already structure-derived. Kept separate from
    ratchet_stop rather than merged behind a flag: they answer to different
    inputs, and a single function taking "either an ATR or a percentage"
    would be the kind of false unification this library exists to avoid.
    """
    if pct <= 0:
        return current_stop
    return max(current_stop, high_water * (1.0 - pct))


def stale_thesis_stop(days_held: int, max_days: int) -> Optional[str]:
    """Time stop with NO profit condition — for desks where age alone
    invalidates the setup.

    Distinct from time_stop() on purpose. A mean-reversion trade that has not
    reverted in N days has a stale thesis whether or not it happens to be
    green; a trend trade that is winning after N days is doing exactly what
    was asked of it. Collapsing the two would silently stop meanrev exiting
    profitable-but-stale positions.
    """
    if max_days <= 0 or days_held < max_days:
        return None
    return f"time({days_held}d)"


ADX_TRAIL_BANDS = ((35.0, 3.0), (20.0, 2.0), (0.0, 1.5))


def select_atr_trail_band(adx: Optional[float], default: float = 2.0) -> float:
    """ATR multiple chosen by trend STRENGTH, not fixed.

        ADX > 35   ->  3.0x   a strong trend has earned room to breathe
        ADX 20-35  ->  2.0x   the normal case
        ADX < 20   ->  1.5x   drift, not trend — take what is there

    Three parameters where there was one, so it is a sweep option rather than
    a default. The principle is sound (a fixed distance treats a 40-ADX trend
    and a 15-ADX drift identically); whether the specific bands help is a
    question for the harness. A missing ADX returns the default rather than
    guessing a band.
    """
    if adx is None:
        return default
    for floor, mult in ADX_TRAIL_BANDS:
        if adx >= floor:
            return mult
    return default


# Renamed from adx_trail_mult (2026-08-04): the old name claimed to trail
# something, and it does not — it SELECTS the policy parameter a trail then
# uses. Alias kept so existing call sites keep working.
adx_trail_mult = select_atr_trail_band


def staged_profit_lock(entry: float, high_water: float,
                       atr: Optional[float], current_stop: float,
                       ema20: Optional[float] = None) -> tuple[float, str]:
    """Progressive protection in ATR units: the further a trade runs, the
    less of the gain it is allowed to give back.

        +1 ATR  ->  stop to breakeven
        +2 ATR  ->  lock 0.5 ATR of profit
        +3 ATR  ->  2xATR trail from the high water mark
        +5 ATR  ->  trail the 20-EMA (tightest, for extended runners)

    Returns (stop, stage_label). NEVER widens — every candidate stop goes
    through ratchet-style max().

    THE TRADE-OFF, stated because it is not free: tightening as a trade runs
    reduces giveback and also cuts winners short. A trend strategy earns its
    return from a small number of large moves, and a stop that tightens at
    +3 ATR will exit some of the trades that would have gone to +10. Whether
    that is a net gain is an empirical question, which is why this ships as a
    sweep option rather than a default.
    """
    if atr is None or atr <= 0 or entry <= 0:
        return current_stop, "none"
    gain_atr = (high_water - entry) / atr
    stop, stage = current_stop, "none"
    if gain_atr >= 1.0:
        stop, stage = max(stop, entry), "breakeven"
    if gain_atr >= 2.0:
        stop, stage = max(stop, entry + 0.5 * atr), "lock_0.5atr"
    if gain_atr >= 3.0:
        stop, stage = max(stop, high_water - 2.0 * atr), "trail_2atr"
    if gain_atr >= 5.0 and ema20 is not None:
        stop, stage = max(stop, ema20), "ema20_trail"
    return stop, stage


def volatility_exit(atr_now: Optional[float], atr_at_entry: Optional[float],
                    mult: float) -> Optional[str]:
    """Realised volatility has expanded far past what the position was sized
    for, so the environment the setup assumed no longer exists."""
    if not atr_now or not atr_at_entry or mult <= 0:
        return None
    if atr_now > mult * atr_at_entry:
        return (f"vol_expansion({atr_now:.2f} > {mult:.1f}x"
                f"{atr_at_entry:.2f})")
    return None


def trend_exit(close: Optional[float], reference: Optional[float],
               label: str = "ema20") -> Optional[str]:
    """Price closed below the trend line the thesis rested on."""
    if close is None or reference is None:
        return None
    if close < reference:
        return f"{label}(close {close:.2f} < {reference:.2f})"
    return None


def time_stop(days_held: int, max_days: int, price: float, entry: float,
              r: float, require_r: float = 1.0) -> Optional[str]:
    """Held long enough without the move working — capital, not just risk.

    Only fires when the trade has FAILED to reach `require_r`; a winner that
    is simply taking its time is not what a time stop is for.
    """
    if max_days <= 0 or days_held < max_days:
        return None
    if r > 0 and price >= entry + require_r * r:
        return None
    return f"time({days_held}d without +{require_r:g}R)"


def rank_exit(rank: Optional[int], exit_rank: int) -> Optional[str]:
    """For rotation desks: the name fell out of contention entirely.

    Distinct from every price-based rule above — xsect exits because
    something else ranks higher, not because this position did anything
    wrong.
    """
    if rank is None:
        return "no longer ranked"
    if rank > exit_rank:
        return f"rank {rank} > exit band {exit_rank}"
    return None


def gap_exit(open_price: float, stop: float, low: float = None
             ) -> tuple[Optional[float], Optional[str]]:
    """(fill_price, reason) when a stop is breached — gap-aware.

    The distinction that matters, and which a naive stop check gets wrong:

        stop = 100, tomorrow OPENS at 94

    You do not exit at 100. You exit at 94, and the extra 6 points of loss
    are real. Modelling it as a clean 100 understates every gap risk in the
    book. The backtest has always handled this; the library did not, so any
    live path doing its own stop arithmetic would have booked the optimistic
    number.

    Broker-side GTC stops handle gaps naturally — this is for the LOCAL
    checks (intraday, xsect) and for recording the honest fill.
    """
    if stop <= 0:
        return None, None
    if open_price <= stop:
        return open_price, f"gap_stop(open {open_price:.2f} <= {stop:.2f})"
    if low is not None and low <= stop:
        return stop, f"stop({stop:.2f} touched intraday)"
    return None, None


def profit_target(price: float, entry: float, r: float,
                  target_r: float) -> Optional[str]:
    """Fixed R-multiple objective. Returns a reason or None.

    Lived only inside the backtest as the 2R half-exit; moved here so the
    live and replay paths can share one definition rather than two that
    happen to agree today.
    """
    if r <= 0 or target_r <= 0:
        return None
    if price >= entry + target_r * r:
        return f"target({target_r:g}R)"
    return None


def stop_hit(price: float, stop: float) -> Optional[str]:
    """Local stop check. NOTE: the broker-side GTC stop is the real
    protection — this exists for desks that also watch intraday, and must
    only be consulted while the market is open (firing on a stale after-hours
    quote is what caused the 2026-07-16 liquidations)."""
    if stop > 0 and price <= stop:
        return f"stop({price:.2f} <= {stop:.2f})"
    return None
