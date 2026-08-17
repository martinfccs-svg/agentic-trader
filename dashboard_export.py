"""dashboard_export.py — one JSON snapshot of live state, written each cycle.

The dashboard is a static page. It reads this file and renders it. That split
matters: the page cannot compute anything, cannot reach the broker, and cannot
invent a number — it can only show what this module wrote.

WHY A FILE AND NOT AN ENDPOINT
No web server in the trading process. A crashed or slow HTTP handler inside
the cycle would be a trading problem, and a dashboard is never worth that.
Writing a small file to /data costs microseconds and cannot block a fill.

WHAT IT DELIBERATELY DOES NOT DO
It does not compute returns, win rates or expectancy. Those need a trade
history this snapshot does not have, and a dashboard that estimates them from
one moment would be inventing performance figures. It reports LEVELS and
STATE — what is open, what is risked, what the machine is doing right now.

STALENESS IS THE POINT
`written_at` is included so the page can say "3 minutes ago" or "STALE — the
bot may be down". A dashboard that silently shows yesterday's numbers is
worse than one that shows nothing.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone

log = logging.getLogger("dashboard")

OUT_PATH = os.getenv("DASHBOARD_PATH", "/data/dashboard.json")


def _positions(broker) -> list:
    out = []
    for ticker, p in (getattr(broker, "positions", {}) or {}).items():
        try:
            last = getattr(p, "last_price", None) or p.entry_price
            pnl = (last - p.entry_price) * p.shares
            out.append({
                "ticker": ticker,
                "desk": getattr(getattr(p, "system", None), "value", "?"),
                "shares": round(float(p.shares), 2),
                "entry": round(float(p.entry_price), 2),
                "last": round(float(last), 2),
                "stop": round(float(getattr(p, "stop_price", 0) or 0), 2),
                "pnl": round(pnl, 2),
                "pnl_pct": round((last / p.entry_price - 1) * 100, 2)
                if p.entry_price else 0.0,
            })
        except Exception:  # noqa: BLE001 — one bad position must not blank the page
            continue
    return sorted(out, key=lambda r: -abs(r["pnl"]))


def _desks(broker) -> list:
    out = []
    try:
        from models import System
        eq = broker.equity or 1.0
        for s in System:
            realized = broker.realized_pnl.get(s, 0.0)
            unreal = broker.unrealized_pnl(s)
            n = sum(1 for p in broker.positions.values() if p.system is s)
            out.append({
                "desk": s.value, "open": n,
                "realized": round(realized, 2),
                "unrealized": round(unreal, 2),
                "realized_pct": round(100.0 * realized / eq, 3),
                "unrealized_pct": round(100.0 * unreal / eq, 3),
            })
    except Exception as e:  # noqa: BLE001
        log.debug("dashboard: desk rollup unavailable (%s)", e)
    return out


def _gates() -> list:
    """Every portfolio control and whether it ENFORCES or only measures.

    Shown because "we have a heat limit" and "the heat limit blocks trades"
    are different claims, and a dashboard that blurs them is misleading in
    the direction that matters.
    """
    try:
        import portfolio_manager as pm
    except Exception:  # noqa: BLE001
        return []
    rows = [("portfolio heat", pm.HEAT_MAX, "{:.1%}"),
            ("sector budget", pm.SECTOR_MAX_PCT, "{:.0%}"),
            ("desk budget", pm.DESK_BUDGET_PCT, "{:.0%}"),
            ("same ticker", pm.SAME_TICKER_MAX_PCT, "{:.0%}"),
            ("top-5 concentration", pm.TOP_N_MAX_PCT, "{:.0%}"),
            ("liquidity participation", pm.MAX_PARTICIPATION, "{:.2%}")]
    out = []
    for name, val, fmt in rows:
        out.append({"name": name,
                    "enforced": bool(val and val > 0),
                    "limit": fmt.format(val) if val and val > 0 else None})
    out.append({"name": "drawdown scaling",
                "enforced": str(getattr(pm, "DD_SCALE", "")).lower() != "measure",
                "limit": str(getattr(pm, "DD_SCALE", "?"))})
    return out


def write_snapshot(broker, cycle_n: int = 0, health: dict = None,
                   funnel: dict = None, path: str = None) -> bool:
    """Write the snapshot. Returns True on success; never raises."""
    path = path or OUT_PATH
    try:
        eq = float(getattr(broker, "equity", 0.0) or 0.0)
        snap = {
            "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "written_ts": time.time(),
            "cycle": cycle_n,
            "equity": round(eq, 2),
            "cash": round(float(getattr(broker, "cash", 0.0) or 0.0), 2),
            "positions": _positions(broker),
            "desks": _desks(broker),
            "gates": _gates(),
            "health": health or {},
            "funnel": funnel or {},
        }
        try:
            import system_state
            snap["state"] = system_state.current().value
        except Exception:  # noqa: BLE001
            snap["state"] = "unknown"
        try:
            import regime_allocation as ra
            st = ra.last_state()
            if st:
                snap["regime"] = {"label": st.label,
                                  "confidence": round(st.confidence, 1)}
        except Exception:  # noqa: BLE001
            pass
        try:
            import portfolio_manager as pm
            h = pm._heat_now(broker)
            snap["heat_pct"] = round(100.0 * h, 2) if h is not None else None
        except Exception:  # noqa: BLE001
            snap["heat_pct"] = None
        try:
            import config_check
            snap["config_hash"] = config_check.active_hash()
        except Exception:  # noqa: BLE001
            snap["config_hash"] = None

        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(snap, fh, separators=(",", ":"))
        # Atomic replace: the page must never read a half-written file and
        # render a broken or partial book.
        os.replace(tmp, path)
        return True
    except Exception as e:  # noqa: BLE001 — a dashboard never breaks a cycle
        log.error("dashboard snapshot failed (non-fatal): %s", e)
        return False
