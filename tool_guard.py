"""tool_guard.py — stop a research tool from becoming a restart loop.

WHAT HAPPENED (2026-08-18, deployment 80af817f)

A deployment went out with no start command, so the container fell back to the
Dockerfile's default CMD — a test harness. It ran, finished, and EXITED 0.
Railway's ALWAYS restart policy treats any exit as a crash, restarted it, and
the cycle repeated until the service failed out 18 minutes later.

Nothing in that sequence is a Python bug. Every part behaved as designed:
the tool finished, the container exited, the platform restarted it. The
failure is that a tool which EXITS was reachable as a service entrypoint at
all.

I had already identified this exact mode and built parking into
backtest_swing_v2.py — and then left the other nine tools without it. A guard
that protects one of ten entry points is not a guard, it is a coincidence.

WHY THE GUARD REFUSES *BEFORE* DOING THE WORK

Parking after completion stops the loop but still pays for it: each restart
re-fetches market data, re-runs the analysis, and burns API budget shared with
the live trading account. Refusing at startup costs nothing and makes the
misconfiguration obvious in the first log line rather than the hundredth.

DISTINGUISHING A DEPLOYED SERVICE FROM `railway run`

`railway run` injects Railway's variables while executing on your machine, so
RAILWAY_ENVIRONMENT alone would block legitimate one-off runs. RAILWAY_
DEPLOYMENT_ID is set only INSIDE a deployed container, which is exactly the
case that must not host a research tool.

Set TOOL_ALLOW_SERVICE=1 to run deliberately inside a container anyway. The
tool then completes and PARKS instead of exiting, so a finished run still
cannot loop.
"""

from __future__ import annotations

import os
import sys
import time

_PARK_SECONDS = 3600


def in_deployed_container() -> bool:
    """True only inside a Railway deployment, not under `railway run`."""
    return bool(os.getenv("RAILWAY_DEPLOYMENT_ID"))


def _park(msg_lines: list) -> None:
    """Sleep forever, loudly. Never exit — exiting is what loops."""
    for line in msg_lines:
        print(line, flush=True)
    hours = 0
    while True:
        time.sleep(_PARK_SECONDS)
        hours += 1
        print(f"  [parked {hours}h] still idle by design — this process will "
              f"not exit, because exiting restarts it. Stop the service.",
              flush=True)


def guard_entrypoint(tool: str, does_what: str = "") -> None:
    """Call this as the FIRST statement of a research tool's main().

    Returns immediately for a normal local run. Inside a deployed container
    without explicit permission it parks and never returns, so no work is
    done and no API budget is spent.
    """
    if not in_deployed_container():
        return
    if os.getenv("TOOL_ALLOW_SERVICE", "").strip() in ("1", "true", "yes", "on"):
        print(f"  {tool}: running inside a container by explicit request "
              f"(TOOL_ALLOW_SERVICE). It will PARK when finished rather than "
              f"exit, so it cannot restart-loop.", flush=True)
        return
    _park([
        "",
        "=" * 74,
        f"REFUSING TO RUN: {tool} is a RESEARCH TOOL, not a service.",
        "=" * 74,
        "",
        "  This process is the container's entrypoint, which almost always",
        "  means the service has no start command and fell back to the",
        f"  Dockerfile's default CMD.{(' ' + does_what) if does_what else ''}",
        "",
        "  A research tool exits when it finishes. Railway restarts anything",
        "  that exits. That is a restart loop, and it re-fetches market data",
        "  on every iteration using the same API budget as the live trader.",
        "",
        "  FIX ONE OF THESE:",
        "    - set the service's start command to:  python main.py",
        "    - or give the Dockerfile an explicit:  CMD [\"python\", \"main.py\"]",
        "    - to run this tool once, use:          railway run python "
        f"{os.path.basename(sys.argv[0] or tool)}",
        "",
        "  Parking instead of exiting so the loop stops here. No work has",
        "  been done and no data has been fetched.",
        "=" * 74,
    ])


def park_when_done(tool: str) -> None:
    """Call at the very end of main(), after results are printed.

    Only parks inside a container. A finished run that exits would restart;
    a finished run that parks holds one idle process until you stop it, which
    is the lesser problem and the visible one.
    """
    if not in_deployed_container():
        return
    sys.stdout.flush()
    _park([
        "",
        "=" * 74,
        f"{tool}: RUN COMPLETE — parking instead of exiting.",
        "=" * 74,
        "  Results are above. Exiting here would trigger a restart and re-run",
        "  the whole thing. Read the output, then stop this service.",
        "=" * 74,
    ])
