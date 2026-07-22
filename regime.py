"""regime.py — one reflex the system lacked: noticing the weather.

Every strategy in this bot is long-only momentum-family. In a genuine
market decline they all either lose or idle; nothing routes capital away
from risk because nothing knows the regime changed. This module is the
minimal fix: a single portfolio-level gate — risk is ON while the market
proxy trades above its long moving average, OFF below it.

Applied to NEW ENTRIES ONLY. Exits, stops, flattens, and rebalance-driven
sells are never gated by regime (reducing risk must always be allowed).

Wiring (2026-07-22):
  main.py     — entry signals are held, not routed, while risk_off
  xsection.py — rotations are skipped whole (atomicity, same as the kill
                switch path) while risk_off; protective stops still run

Design rules, learned the hard way elsewhere in this codebase:
  - FAIL-OPEN on missing data. If SPY bars can't be fetched or are too
    shallow, the gate reports risk-ON and logs loudly. A protective overlay
    must never become a new deadlock class (see the 2026-07-07
    recency-as-health lockout). Real feed outages are already handled by
    breakers and the kill switch.
  - TTL-cached (default 30 min): the answer changes at daily-bar speed;
    re-asking every 5-second cycle is waste.
  - Escape hatch: REGIME_FILTER=off restores prior behavior with no deploy.

Env:
  REGIME_FILTER      on (default) | off
  REGIME_SYMBOL      default SPY
  REGIME_SMA_DAYS    default 200
  REGIME_TTL_SECS    default 1800
"""

from __future__ import annotations

import logging
import os
import time

log = logging.getLogger("regime")

ENABLED = os.getenv("REGIME_FILTER", "on").strip().lower() not in (
    "off", "false", "0", "no")
SYMBOL = os.getenv("REGIME_SYMBOL", "SPY")
SMA_DAYS = int(os.getenv("REGIME_SMA_DAYS", "200"))
TTL = float(os.getenv("REGIME_TTL_SECS", "1800"))

_cache: tuple[float, bool] | None = None      # (checked_at, risk_on)
_last_state: bool | None = None
_data_warned = False


def risk_on(feed) -> bool:
    """True when new entries are permitted under the regime filter.
    Never raises; degrades to True (with a loud log) on any data problem."""
    global _cache, _last_state, _data_warned
    if not ENABLED:
        return True
    now = time.time()
    if _cache and now - _cache[0] < TTL:
        return _cache[1]

    state = True
    try:
        bars = feed.get_daily_bars(SYMBOL)
        closes = getattr(bars, "close", None) if bars is not None else None
        if not closes or len(closes) < SMA_DAYS:
            if not _data_warned:
                _data_warned = True
                log.error("regime: %s has %s daily bars (< %d needed) — "
                          "filter FAILING OPEN (risk-on). Entries are NOT "
                          "being regime-gated until this resolves.",
                          SYMBOL, len(closes) if closes else 0, SMA_DAYS)
        else:
            _data_warned = False
            sma = sum(closes[-SMA_DAYS:]) / SMA_DAYS
            state = closes[-1] > sma
            if state is not _last_state:
                log.warning("regime: %s %.2f vs %d-SMA %.2f -> %s "
                            "(new entries %s)", SYMBOL, closes[-1], SMA_DAYS,
                            sma, "RISK-ON" if state else "RISK-OFF",
                            "permitted" if state else "held")
                try:
                    import audit
                    audit.record("regime_change", notify=True,
                                 state="risk_on" if state else "risk_off",
                                 symbol=SYMBOL, price=round(closes[-1], 2),
                                 sma=round(sma, 2))
                except Exception:  # noqa: BLE001 — mirror is best-effort
                    pass
                _last_state = state
    except Exception as e:  # noqa: BLE001 — overlay must never break a cycle
        log.error("regime: check failed (%s) — failing open (risk-on)", e)

    _cache = (now, state)
    return state
