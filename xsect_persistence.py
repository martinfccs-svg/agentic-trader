"""xsect_persistence.py — rank hysteresis for the cross-sectional rotation.

Built from a MEASURED problem, not a hypothesis. Two independent readings
said the same thing:

  * the replay showed 51-58 rotations/year across 3 slots — roughly 17-19
    round trips per slot per year
  * the autopsy found 9 INTC trades and 9 ARM trades, nearly all losses

Neither is a signal-quality failure. It is the ranking oscillating around
the top-N boundary: a name drifts from rank 3 to rank 4, gets sold, drifts
back to 3, gets bought, and each round trip pays costs and often a stop.

HYSTERESIS, applied at both ends:

  ENTRY   a name must hold rank <= top_n for ENTRY_PERIODS consecutive
          rebalances before it is bought. A one-day visit to the top does
          not earn capital.
  EXIT    a held name is sold only once it falls past EXIT_RANK (default
          top_n + 2). Slipping from 3rd to 4th is noise, not a signal.

The asymmetry is the point: a wide band between "good enough to buy" and
"bad enough to sell" is what stops boundary oscillation from becoming
turnover.

STATE lives on /data, which persists as of 2026-07-28. A per-process dict
would reset on every redeploy and the entry counter would never reach 2 —
the feature would look installed and never fire, the same failure mode that
kept the meanrev time stop dead for weeks. FAILS OPEN: if the state cannot
be read or written, hysteresis is skipped and the rotation behaves exactly
as it did before, logged loudly.

Env:
  XSECT_ENTRY_PERIODS   consecutive rebalances at rank <= top_n before a buy
                        (default 2; 1 disables entry hysteresis)
  XSECT_EXIT_RANK_PAD   how far past top_n a holding must fall to be sold
                        (default 2; 0 disables exit hysteresis)
  XSECT_PERSIST_PATH    default /data/xsect_persistence.json
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

log = logging.getLogger("xsect_persistence")

PATH = os.getenv("XSECT_PERSIST_PATH", "/data/xsect_persistence.json")
ENTRY_PERIODS = int(os.getenv("XSECT_ENTRY_PERIODS", "2"))
EXIT_RANK_PAD = int(os.getenv("XSECT_EXIT_RANK_PAD", "2"))
_warned = False
_state_ok = True     # False once a read/write fails: hysteresis then SKIPS
                     # rather than blocks. Without this, an unwritable volume
                     # means streaks never accumulate and entry_allowed()
                     # returns False forever — silently halting every new
                     # position. A protective feature must never become a
                     # deadlock (the 2026-07-07 lesson).


def _load() -> dict:
    global _warned
    try:
        with open(PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}
    except Exception as e:  # noqa: BLE001
        global _state_ok
        _state_ok = False
        if not _warned:
            _warned = True
            log.error("xsect_persistence: cannot read %s (%s) — FAILING OPEN, "
                      "hysteresis DISABLED (entries permitted as before)",
                      PATH, e)
        return {}


def _save(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(PATH) or ".", exist_ok=True)
        tmp = PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, PATH)
    except Exception as e:  # noqa: BLE001
        global _state_ok
        _state_ok = False
        log.error("xsect_persistence: cannot write %s (%s) — streaks cannot "
                  "accumulate, so entry hysteresis is DISABLED rather than "
                  "blocking every entry forever", PATH, e)


def update(ranked_tickers: list[str], top_n: int) -> dict:
    """Record this rebalance. `ranked_tickers` is the FULL ranking, best
    first. Returns {ticker: consecutive periods at rank <= top_n}."""
    state = _load()
    streaks = dict(state.get("streaks", {}))
    qualifying = set(ranked_tickers[:top_n])
    for t in ranked_tickers:
        if t in qualifying:
            streaks[t] = int(streaks.get(t, 0)) + 1
        elif t in streaks:
            del streaks[t]                      # streak broken; start over
    state["streaks"] = streaks
    state["last_rebalance"] = datetime.now(timezone.utc).isoformat()
    _save(state)
    return streaks


def entry_allowed(ticker: str, streaks: dict) -> tuple[bool, str]:
    """May a NEW position be opened in this name?"""
    if ENTRY_PERIODS <= 1:
        return True, "entry hysteresis disabled"
    if not _state_ok:
        return True, "state unavailable — hysteresis skipped, not blocking"
    n = int(streaks.get(ticker, 0))
    if n >= ENTRY_PERIODS:
        return True, f"held top-N for {n} rebalances"
    return False, f"only {n}/{ENTRY_PERIODS} rebalances at top-N"


def exit_rank_threshold(top_n: int) -> int:
    """A holding is sold only once its rank falls PAST this number."""
    return top_n + max(0, EXIT_RANK_PAD)


def should_hold(ticker: str, ranked_tickers: list[str], top_n: int
                ) -> tuple[bool, str]:
    """Should an EXISTING holding be kept? True while it ranks within the
    exit band, even if it has slipped out of the top N."""
    if not _state_ok:
        # Without state we cannot reason about persistence; fall back to the
        # original behaviour (out of top N == sell) rather than inventing one.
        return False, "state unavailable — original exit rule"
    thresh = exit_rank_threshold(top_n)
    try:
        rank = ranked_tickers.index(ticker) + 1
    except ValueError:
        return False, "no longer ranked at all"
    if rank <= top_n:
        return True, f"rank {rank} (in top {top_n})"
    if rank <= thresh:
        return True, (f"rank {rank} — slipped out of top {top_n} but inside "
                      f"the exit band ({thresh}); holding rather than churning")
    return False, f"rank {rank} > exit band {thresh}"
