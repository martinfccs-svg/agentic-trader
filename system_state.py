"""system_state.py — name the state the system is actually in.

The lifecycle already exists implicitly: booting, reconciling, trading,
degraded, halted, shutting down. It was spread across a kill-switch flag, a
market-hours check, a reconciliation step and a list of failed telemetry
sections. Every one of those is a state; none of them was called one.

WHY THIS IS MORE THAN BOOKKEEPING

_contained() (2026-08-06) made a failed subsystem VISIBLE — it names the
section on the health line. Nothing acts on it. So the heat readout could
fail for fifty consecutive cycles and produce fifty log lines and zero
behaviour change, while portfolio_manager's heat gate sat blind and the desks
kept opening positions against a limit nobody was measuring.

That is the gap this closes. A subsystem failing ONCE is noise — a timeout,
a rate limit, a bad tick. The same subsystem failing for N cycles running is
a different fact, and it deserves a different response: say so loudly, and
let the caller decide whether to keep trading against a control that is not
working.

WHAT IT DELIBERATELY DOES NOT DO

It does not halt trading by itself. A telemetry module that can stop the
desks is a bigger hazard than the degradation it is reporting — this project
already deleted one small file that could flatten the book. It reports state
and escalates; main.py decides.

STATES

    BOOTING      process started, nothing verified yet
    RECONCILING  comparing local positions against the broker
    TRADING      normal operation
    DEGRADED     a subsystem has failed for CONSECUTIVE_LIMIT cycles running
    HALTED       kill switch, drawdown halt, or an unreconciled position
    CLOSED       market shut; the loop runs but places nothing
    STOPPING     shutdown in progress
"""

from __future__ import annotations

import logging
import os
import time
from enum import Enum

log = logging.getLogger("state")


class State(Enum):
    BOOTING = "booting"
    RECONCILING = "reconciling"
    TRADING = "trading"
    DEGRADED = "degraded"
    HALTED = "halted"
    CLOSED = "closed"
    STOPPING = "stopping"


# How many consecutive cycles a section must fail before it counts as
# degradation rather than a blip. Three cycles is roughly three minutes —
# long enough to rule out a single timeout, short enough to notice within
# one trading session.
CONSECUTIVE_LIMIT = int(os.getenv("DEGRADED_AFTER_CYCLES", "3"))

# Sections whose failure means a RISK CONTROL is blind, as opposed to a
# cosmetic readout going missing. Listed explicitly rather than inferred,
# because "which failures matter" is a judgement and should be written down.
CRITICAL_SECTIONS = {
    "heat_readout",         # portfolio heat gate cannot see exposure
    "portfolio_manager",    # the single decision point
    "reconcile",            # local book may disagree with the broker
    "drawdown",             # drawdown ladder cannot measure the peak
}

# PRECEDENCE. Without it, the per-cycle TRADING/CLOSED update would clobber
# HALTED every cycle — a kill switch that a routine telemetry write silently
# cancels. Higher wins; the cycle may only set a state at or above what it
# is replacing when that state is transient.
_RANK = {State.BOOTING: 0, State.CLOSED: 1, State.TRADING: 1,
         State.RECONCILING: 2, State.DEGRADED: 3, State.HALTED: 4,
         State.STOPPING: 5}

_state = State.BOOTING
_since = time.time()
_streaks: dict = {}          # section -> consecutive failing cycles


def current() -> State:
    return _state


def set_state(new: State, why: str = "", force: bool = False) -> None:
    """Transition, logged once, respecting precedence.

    Repeated identical states are not re-logged — a state machine that
    narrates every cycle is one nobody reads.

    `force` is required to step DOWN in rank, so the routine per-cycle
    "we are TRADING" update cannot quietly clear a HALTED or DEGRADED state.
    Recovering from those should be a deliberate act, not a side effect of
    the next loop iteration.
    """
    global _state, _since
    if new is _state:
        return
    if not force and _RANK[new] < _RANK[_state]:
        return                      # refuse to silently de-escalate
    log.warning("STATE %s -> %s%s (held %.0fs)", _state.value, new.value,
                f" [{why}]" if why else "", time.time() - _since)
    _state, _since = new, time.time()


def note_cycle(failed_sections: list) -> dict:
    """Record this cycle's contained failures and report what has persisted.

    Returns {"degraded": [...], "critical": [...]} where `degraded` are
    sections that have now failed CONSECUTIVE_LIMIT cycles in a row, and
    `critical` is the subset that blinds a risk control.

    A section that succeeds resets its streak — this measures CONSECUTIVE
    failure, not cumulative. An intermittent failure is a different problem
    from a persistent one and conflating them would raise the alarm for the
    wrong reason.
    """
    failed = set(failed_sections or [])
    for name in list(_streaks):
        if name not in failed:
            if _streaks[name] >= CONSECUTIVE_LIMIT:
                log.warning("RECOVERED %s after %d failing cycles", name,
                            _streaks[name])
            del _streaks[name]
    for name in failed:
        _streaks[name] = _streaks.get(name, 0) + 1

    persistent = sorted(n for n, c in _streaks.items()
                        if c >= CONSECUTIVE_LIMIT)
    critical = sorted(set(persistent) & CRITICAL_SECTIONS)
    if persistent:
        # THE TRANSITION THAT WAS MISSING. note_cycle detected degradation,
        # logged it, and left the state at TRADING — so every state-aware
        # reader saw a healthy system while a risk control was blind. The
        # detection was correct and the machine never left the happy path.
        set_state(State.DEGRADED,
                  f"{','.join(persistent)} failing {CONSECUTIVE_LIMIT}+ cycles")
    elif _state is State.DEGRADED:
        set_state(State.TRADING, "all sections recovered", force=True)

    if critical:
        log.critical("DEGRADED: %s has failed %d cycles running. A RISK "
                     "CONTROL IS BLIND — the desks are still trading against "
                     "a limit nothing is measuring. Investigate before the "
                     "next entry.", ", ".join(critical),
                     max(_streaks[n] for n in critical))
    elif persistent:
        log.error("DEGRADED: %s has failed %d cycles running (telemetry "
                  "only, no risk control affected)", ", ".join(persistent),
                  max(_streaks[n] for n in persistent))
    return {"degraded": persistent, "critical": critical}


def banner() -> str:
    return f"STATE={_state.value} for {time.time() - _since:.0f}s" + (
        f" | persistent failures: {sorted(_streaks)}" if _streaks else "")
