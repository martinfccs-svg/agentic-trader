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


def stop_hit(price: float, stop: float) -> Optional[str]:
    """Local stop check. NOTE: the broker-side GTC stop is the real
    protection — this exists for desks that also watch intraday, and must
    only be consulted while the market is open (firing on a stale after-hours
    quote is what caused the 2026-07-16 liquidations)."""
    if stop > 0 and price <= stop:
        return f"stop({price:.2f} <= {stop:.2f})"
    return None
