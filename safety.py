"""Safety guards. Conservative by default; all the things that should stop a
bot from doing something dumb with real money.
"""

from __future__ import annotations

import logging
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from config import (
    FLATTEN_BEFORE_CLOSE_MIN,
    MARKET_CLOSE,
    MARKET_OPEN,
    TRADING_MODE,
    live_money_armed,
)

log = logging.getLogger("safety")
ET = ZoneInfo("America/New_York")


def _parse(hhmm: str) -> dtime:
    h, m = hhmm.split(":")
    return dtime(int(h), int(m))


def market_is_open(now: datetime | None = None) -> bool:
    now = now or datetime.now(ET)
    if now.weekday() >= 5:          # weekend
        return False
    return _parse(MARKET_OPEN) <= now.timetz().replace(tzinfo=None) <= _parse(MARKET_CLOSE)


def near_close(now: datetime | None = None) -> bool:
    """True within FLATTEN_BEFORE_CLOSE_MIN of the close (intraday flatten window)."""
    now = now or datetime.now(ET)
    close = _parse(MARKET_CLOSE)
    minutes_to_close = (close.hour * 60 + close.minute) - (now.hour * 60 + now.minute)
    return 0 <= minutes_to_close <= FLATTEN_BEFORE_CLOSE_MIN


def startup_banner() -> None:
    """Print loudly what mode we are in so there is never ambiguity."""
    if live_money_armed():
        log.warning("=" * 60)
        log.warning("LIVE MONEY ARMED. Real orders will be placed with real funds.")
        log.warning("If this is not intended, unset LIVE_CONFIRM / set ALPACA_PAPER=true NOW.")
        log.warning("=" * 60)
    else:
        log.info("Mode=%s, live money NOT armed (safe: paper accounting/execution).",
                 TRADING_MODE)
