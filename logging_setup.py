"""
logging_setup.py -- fixes the two logging issues from the 2026-07-20 review.

PROBLEM 1: every log line arrives in Railway tagged severity=error, because
the default StreamHandler writes to stderr and Railway maps stderr -> error.
Real exceptions are indistinguishable from routine INFO chatter.
FIX: route DEBUG/INFO to stdout, WARNING+ to stderr. Railway then shows
INFO as info and only genuine warnings/errors as red.

PROBLEM 2: ~50 identical lines/minute (the 6-line P&L block every 7-second
cycle) bury anything interesting.
FIX: log_if_changed() -- emits a block only when its content changed, or
every `every` calls as a heartbeat, whichever comes first.

USAGE in trader.py (replace your existing logging.basicConfig):

    from logging_setup import setup_logging, log_if_changed
    log = setup_logging()                      # once, at startup

    # in the cycle loop, instead of six log.info(...) calls:
    block = (f"swing    realized={sr:.2f} unrealized={su:.2f} open={so}\n"
             f"intraday realized={ir:.2f} unrealized={iu:.2f} open={io_}\n"
             f"meanrev  realized={mr:.2f} unrealized={mu:.2f} open={mo}\n"
             f"xsectmom realized={xr:.2f} unrealized={xu:.2f} open={xo}\n"
             f"equity={eq:.2f}")
    log_if_changed("pnl_block", block, every=10)
"""

from __future__ import annotations

import logging
import sys


class _MaxLevel(logging.Filter):
    def __init__(self, level: int):
        super().__init__()
        self.level = level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno <= self.level


def setup_logging(level: int = logging.INFO,
                  fmt: str = "%(asctime)s %(name)s %(levelname)s %(message)s"
                  ) -> logging.Logger:
    """Root logger: DEBUG/INFO -> stdout, WARNING+ -> stderr. Idempotent."""
    root = logging.getLogger()
    root.setLevel(level)
    # Remove pre-existing stream handlers so re-deploys / re-imports don't
    # double-log or resurrect the old stderr-for-everything handler.
    for h in list(root.handlers):
        if isinstance(h, logging.StreamHandler):
            root.removeHandler(h)
    formatter = logging.Formatter(fmt)

    out = logging.StreamHandler(sys.stdout)
    out.setLevel(logging.DEBUG)
    out.addFilter(_MaxLevel(logging.INFO))
    out.setFormatter(formatter)
    root.addHandler(out)

    err = logging.StreamHandler(sys.stderr)
    err.setLevel(logging.WARNING)
    err.setFormatter(formatter)
    root.addHandler(err)
    return logging.getLogger("main")


_last_blocks: dict[str, str] = {}
_since_emit: dict[str, int] = {}


def log_if_changed(key: str, block: str, every: int = 10,
                   logger: logging.Logger | None = None) -> bool:
    """Emit `block` (multi-line ok) only if it differs from the last emission
    under `key`, or as a heartbeat every `every` suppressed calls.
    Returns True if it logged."""
    lg = logger or logging.getLogger("main")
    _since_emit[key] = _since_emit.get(key, 0) + 1
    if _last_blocks.get(key) == block and _since_emit[key] < every:
        return False
    _last_blocks[key] = block
    _since_emit[key] = 0
    for line in block.splitlines():
        lg.info(line)
    return True
