"""portfolio_risk.py — total open risk ("heat") across ALL strategies.

The gap this closes: every engine caps its OWN position count, and the kill
switch halts on realized daily loss — but nothing measures how much is at
risk across the whole book at once. With swing 3 + intraday 2 + meanrev 4 +
xsectmom 3 all live, twelve concurrent positions at 1% risk each is 12% of
equity exposed against a 2.5% daily-loss halt: the halt fires long after the
exposure was taken, not before.

Heat = sum over open positions of (price - stop) x shares, as a fraction of
equity. Everything is DERIVED from fields the registry already persists
(entry_price, entry_stop, stop_price, shares) — no new position attributes,
so nothing resets on redeploy. That failure mode is why this module exists
in this shape: a proposal to store break_even / partial_exit_done flags on
the Position object would have silently reset them on every deploy.

UNPROTECTED POSITIONS COUNT AS FULL NOTIONAL. A position whose stop is 0.0
(reconcile's deliberately-unreachable placeholder) is not low-risk, it is
UNBOUNDED risk. Counting it honestly is the point — UNH sat like that for
six sessions and no number anywhere in the system said so.

Env:
  PORTFOLIO_HEAT_MAX   ceiling as a fraction of equity, e.g. 0.06 = 6%.
                       DEFAULT 0.0 = MEASURE ONLY (log, never block).
                       Turn gating on only after watching the numbers.
  PORTFOLIO_HEAT_LOG   on (default) | off
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("portfolio_risk")

HEAT_MAX = float(os.getenv("PORTFOLIO_HEAT_MAX", "0"))
HEAT_LOG = os.getenv("PORTFOLIO_HEAT_LOG", "on").strip().lower() not in (
    "off", "false", "0", "no")

_last_logged: float | None = None
_LOG_DELTA = 0.005          # re-log only when heat moves by >= 0.5pp


def position_risk(pos) -> tuple[float, bool]:
    """(dollars at risk, protected?) for one position.
    Unprotected (stop <= 0) => the whole position value is at risk."""
    price = getattr(pos, "last_price", None) or pos.entry_price
    shares = abs(pos.shares)
    stop = pos.stop_price or 0.0
    if stop <= 0:
        return price * shares, False
    return max(0.0, price - stop) * shares, True


def open_risk(broker) -> tuple[float, list[str]]:
    """Total dollars at risk, plus the tickers with no usable stop."""
    total = 0.0
    unprotected: list[str] = []
    for ticker, pos in getattr(broker, "positions", {}).items():
        try:
            risk, protected = position_risk(pos)
        except Exception:  # noqa: BLE001 — a bad position must not break risk math
            continue
        total += risk
        if not protected:
            unprotected.append(ticker)
    return total, unprotected


def heat(broker) -> float:
    """Open risk as a fraction of equity. 0.0 if equity is unavailable."""
    equity = getattr(broker, "equity", 0) or 0
    if equity <= 0:
        return 0.0
    total, _ = open_risk(broker)
    return total / equity


def check(broker, system=None, adding: float = 0.0) -> tuple[bool, str]:
    """Called before opening a new position.

    Returns (allowed, reason). With PORTFOLIO_HEAT_MAX=0 this ALWAYS allows
    and only logs — measure first, gate later, the same discipline the
    sector cap and every shadow gate followed.
    """
    global _last_logged
    equity = getattr(broker, "equity", 0) or 0
    if equity <= 0:
        return True, "no equity reading"
    total, unprotected = open_risk(broker)
    h = (total + adding) / equity

    if HEAT_LOG and (_last_logged is None or abs(h - _last_logged) >= _LOG_DELTA):
        _last_logged = h
        log.warning("portfolio heat %.2f%% of equity ($%.0f at risk across "
                    "%d positions)%s%s", h * 100, total,
                    len(getattr(broker, "positions", {})),
                    f" | UNPROTECTED (no stop): {', '.join(unprotected)}"
                    if unprotected else "",
                    f" | ceiling {HEAT_MAX:.1%}" if HEAT_MAX > 0
                    else " | MEASURE-ONLY (PORTFOLIO_HEAT_MAX=0)")

    if HEAT_MAX <= 0:
        return True, f"measure-only (heat={h:.2%})"
    if h > HEAT_MAX:
        return False, (f"portfolio heat {h:.2%} would exceed ceiling "
                       f"{HEAT_MAX:.2%}")
    return True, f"heat {h:.2%} within {HEAT_MAX:.2%}"
