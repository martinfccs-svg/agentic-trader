"""loss_cooldown.py — per-ticker re-entry block after a losing exit.

Built from a MEASURED finding (autopsy, 2026-07-26), not a hypothesis: swing's
closed trades were 6 on META with 5 losses. Those were not six independent bad
signals — the strategy was stopped out and kept going back to the same name.
The autopsy's repeat-entry diagnostic states the distinction it turns on:

    concentrated repeats  -> missing re-entry protection (this module)
    losses spread evenly  -> the entries themselves are weak (a different fix)

Deliberately NOT applied to xsectmom. That desk RANKS the universe and holds
the top N; blocking a name that legitimately ranks top-3 means holding the #4
name or cash instead, which is a strategy change rather than a bug fix. Its
repeat entries are rotation churn around the ranking boundary, and the right
treatment there is hysteresis (enter at rank <= N, exit only past N+2) or a
minimum hold — see the xsect notes, not this file.

Intraday already has its own cooldown in intraday_scoring (45 minutes, with an
EMA20-reclaim early release) because its timeframe is minutes, not days. This
module is for the daily desks.

STATE lives on the /data volume, deliberately:
  A per-process dict would reset on every redeploy, and this bot redeploys
  several times a week — the cooldown would silently never apply. The same
  reasoning that kept break_even/partial_exit_done off the Position object.

FAILS OPEN: any read/write problem means no cooldown is enforced, logged loudly.
A protective overlay must never become a new way to block all trading.

Env:
  SWING_LOSS_COOLDOWN_DAYS   default 5 trading days. 0 disables.
  LOSS_COOLDOWN_PATH         default /data/loss_cooldown.json
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timezone

log = logging.getLogger("loss_cooldown")

PATH = os.getenv("LOSS_COOLDOWN_PATH", "/data/loss_cooldown.json")
DAYS = {
    "swing": int(os.getenv("SWING_LOSS_COOLDOWN_DAYS", "5")),
    "meanrev": int(os.getenv("MEANREV_LOSS_COOLDOWN_DAYS", "0")),
}
_warned = False


def _load() -> dict:
    try:
        with open(PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}
    except Exception as e:  # noqa: BLE001
        global _warned
        if not _warned:
            _warned = True
            log.error("loss_cooldown: cannot read %s (%s) — FAILING OPEN, no "
                      "re-entry blocks will be applied", PATH, e)
        return {}


def _save(state: dict) -> None:
    try:
        tmp = PATH + ".tmp"
        os.makedirs(os.path.dirname(PATH) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, PATH)
    except Exception as e:  # noqa: BLE001
        log.error("loss_cooldown: cannot write %s (%s) — cooldown will not "
                  "survive this process", PATH, e)


def _trading_days_between(a: date, b: date) -> int:
    days, cur = 0, a
    while cur < b:
        cur = date.fromordinal(cur.toordinal() + 1)
        if cur.weekday() < 5:
            days += 1
    return days


def note_loss(system: str, ticker: str) -> None:
    """Record a losing exit. Call ONLY when realized P&L < 0."""
    if DAYS.get(system, 0) <= 0:
        return
    state = _load()
    key = f"{system}:{ticker}"
    today = datetime.now(timezone.utc).date().isoformat()
    entry = state.get(key) or {"count": 0}
    entry["last_loss"] = today
    entry["count"] = int(entry.get("count", 0)) + 1
    state[key] = entry
    _save(state)
    log.warning("loss_cooldown: %s %s recorded losing exit #%d — blocked for "
                "%d trading days", system, ticker, entry["count"],
                DAYS[system])


def in_cooldown(system: str, ticker: str) -> tuple[bool, str]:
    """(blocked, reason). Fails open: unreadable state never blocks."""
    n = DAYS.get(system, 0)
    if n <= 0:
        return False, "disabled"
    rec = _load().get(f"{system}:{ticker}")
    if not rec or not rec.get("last_loss"):
        return False, "no prior loss"
    try:
        last = date.fromisoformat(rec["last_loss"])
    except Exception:  # noqa: BLE001
        return False, "unparseable date — failing open"
    elapsed = _trading_days_between(last, datetime.now(timezone.utc).date())
    if elapsed < n:
        return True, (f"lost on {ticker} {elapsed}/{n} trading days ago "
                      f"(loss #{rec.get('count', 1)})")
    return False, f"cooldown expired ({elapsed} >= {n} days)"
