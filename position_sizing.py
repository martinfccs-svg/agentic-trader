"""position_sizing.py — how many shares does a desk WANT?

The ownership split this completes:

    position_sizing.py    "how many shares does this desk want?"
    portfolio_manager.py  "can the book afford them?"

Both questions were being answered in the same place. Base risk lived in
risk.position_size, per-desk risk lived inside swing_engine, and everything
after lived in portfolio_manager — so the next desk needing a non-default
risk percentage would have copied swing's private helper. That is how four
ATR trails and seven EMAs happened.

WHAT WAS ALREADY BUILT, and is not re-implemented here:

  Volatility sizing is not a missing step — it IS the sizing step. Shares
  come from a RISK BUDGET divided by the ATR-derived stop distance, so a
  volatile name automatically gets fewer shares for the same dollar risk:

      NVDA $200, 8% stop = $16/share, $500 budget  ->  31 shares ($6,200)
      CAT  $200, 4% stop = $8/share,  $500 budget  ->  62 shares ($12,400)

  Same risk, half the size, because the stop is twice as wide. That has been
  the mechanism since the first engine.

WHAT THIS FILE ADDS: one place for the per-desk risk percentage, so all four
desks read the same table instead of each growing a private copy.

WHAT IT DELIBERATELY DOES NOT DO: heat, sector, correlation, concentration,
liquidity, regime, drawdown. Those are portfolio_manager's, in that order,
and splitting them back out would undo the consolidation that removed seven
scattered call sites.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("sizing")

# Per-desk risk as a fraction of equity. A desk appears here only when it
# has a REASON to differ — swing's 0.75% is the figure its backtest measured,
# and deploying the engine's 1% default instead would have run positions a
# third larger than anything that was tested.
_DESK_RISK_ENV = {
    "swing": "SWING_RISK_PCT",
    "meanrev": "MEANREV_RISK_PCT",
    "intraday": "INTRADAY_RISK_PCT",
    "xsectmom": "XSECT_RISK_PCT",
}


def _engine_default() -> float:
    try:
        from config import RISK_PER_TRADE_PCT
        return float(RISK_PER_TRADE_PCT)
    except Exception:  # noqa: BLE001
        return 0.01


def risk_pct(desk: str, routed_variant: str = None) -> float:
    """Risk budget for one desk, as a fraction of equity.

    Precedence: explicit env var -> desk-specific default -> engine global.

    The swing case is the one that matters today: when swing is running
    swing_v2's strategy it sizes at 0.75%, because that is what the backtest
    measured. Sizing it at the engine's 1% would deploy positions a third
    larger than the configuration whose Sharpe and drawdown were measured —
    Sharpe is scale-invariant, drawdown is not.
    """
    env = os.getenv(_DESK_RISK_ENV.get(desk, ""), "")
    if env.strip():
        try:
            return float(env)
        except ValueError:
            log.error("sizing: %s=%r is not a number — using the default",
                      _DESK_RISK_ENV.get(desk), env)
    if desk == "swing" and (routed_variant or "").lower() in ("v2", "swing_v2"):
        return float(os.getenv("SWING_V2_RISK_PCT", "0.0075"))
    return _engine_default()


def desired_shares(equity: float, price: float, stop: float, desk: str,
                   cash: float = None, routed_variant: str = None) -> float:
    """Shares this desk wants, before any portfolio constraint.

        shares = min(risk_budget / stop_distance,   <- volatility sizing
                     notional_cap / price,          <- single-name limit
                     cash / price)                  <- can we pay for it

    RECOMPUTED, never scaled. Scaling a figure that has already been clamped
    is wrong whenever the cap binds — and with a 10% cap on liquid names it
    binds most of the time. Worked example that made this concrete: RTX at
    equity 95k, $14.41 risk/share. The correct 0.75% size is
    min(49.4 risk-shares, 43.45 cap-shares) = 43.45. Scaling the capped 1%
    figure gives 43.45 x 0.75 = 32.59 — a 25% under-size that would not have
    matched the backtest.
    """
    dist = price - stop
    if dist <= 0 or price <= 0 or equity <= 0:
        return 0.0
    r = risk_pct(desk, routed_variant)
    try:
        from config import max_position_dollars
        cap_shares = max_position_dollars(equity) / price
    except Exception:  # noqa: BLE001
        cap_shares = equity * 0.10 / price
    limits = [equity * r / dist, cap_shares]
    if cash is not None:
        limits.append(max(0.0, cash) / price)
    return max(0.0, min(limits))


def explain(equity: float, price: float, stop: float, desk: str,
            cash: float = None, routed_variant: str = None) -> str:
    """One line saying WHICH limit bound. A size is easier to trust when the
    reason for it is stated rather than inferred from arithmetic."""
    dist = price - stop
    if dist <= 0:
        return f"{desk}: invalid stop distance {dist:.4f}"
    r = risk_pct(desk, routed_variant)
    by_risk = equity * r / dist
    try:
        from config import max_position_dollars
        by_cap = max_position_dollars(equity) / price
    except Exception:  # noqa: BLE001
        by_cap = equity * 0.10 / price
    by_cash = (max(0.0, cash) / price) if cash is not None else float("inf")
    binding = min((by_risk, "risk budget"), (by_cap, "notional cap"),
                  (by_cash, "cash"))
    return (f"{desk} risk={r:.4f} dist={dist:.2f} | risk {by_risk:.1f} · "
            f"cap {by_cap:.1f}" + (f" · cash {by_cash:.1f}" if cash is not None
                                   else "")
            + f" -> {binding[0]:.1f} shares ({binding[1]} binds)")
