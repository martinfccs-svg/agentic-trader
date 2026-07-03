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


# ---------------------------------------------------------------------------
# US equity market calendar (NYSE/Nasdaq). Computed by rule, not a stale list,
# so it works for any year. Rules: New Year's Day, MLK Day (3rd Mon Jan),
# Presidents Day (3rd Mon Feb), Good Friday, Memorial Day (last Mon May),
# Juneteenth, Independence Day, Labor Day (1st Mon Sep), Thanksgiving
# (4th Thu Nov), Christmas. Sat holidays observe Friday; Sun observe Monday.
# Early closes (13:00 ET): July 3 (when a weekday and July 4 is a market
# holiday weekday-observed), day after Thanksgiving, Christmas Eve (weekday).
#
# Discovered live: on July 3, 2026 (July 4 observed) the bot ran all day with
# market_open=True against frozen holiday prices. Harmless that day; on an
# EARLY-CLOSE day the same gap could hold intraday positions past a 13:00
# close into a long weekend -- exactly what the intraday book must never do.
# ---------------------------------------------------------------------------
from datetime import date, timedelta


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year + (month == 12), (month % 12) + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    # Anonymous Gregorian algorithm.
    a = year % 19; b, c = divmod(year, 100); d, e = divmod(b, 4)
    f = (b + 8) // 25; g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _observed(d: date) -> date:
    if d.weekday() == 5:                 # Sat -> observed Friday
        return d - timedelta(days=1)
    if d.weekday() == 6:                 # Sun -> observed Monday
        return d + timedelta(days=1)
    return d


def market_holidays(year: int) -> set[date]:
    hs = {
        _observed(date(year, 1, 1)),                      # New Year's Day
        _nth_weekday(year, 1, 0, 3),                      # MLK Day
        _nth_weekday(year, 2, 0, 3),                      # Presidents Day
        _easter(year) - timedelta(days=2),                # Good Friday
        _last_weekday(year, 5, 0),                        # Memorial Day
        _observed(date(year, 6, 19)),                     # Juneteenth
        _observed(date(year, 7, 4)),                      # Independence Day
        _nth_weekday(year, 9, 0, 1),                      # Labor Day
        _nth_weekday(year, 11, 3, 4),                     # Thanksgiving
        _observed(date(year, 12, 25)),                    # Christmas
    }
    return hs


def early_closes(year: int) -> set[date]:
    """13:00 ET closes: Jul 3 (weekday, when Jul 4 is a holiday on a weekday or
    weekend-observed elsewhere), day after Thanksgiving, Christmas Eve (weekday)."""
    ec: set[date] = set()
    jul3 = date(year, 7, 3)
    if jul3.weekday() < 5 and jul3 not in market_holidays(year):
        ec.add(jul3)
    ec.add(_nth_weekday(year, 11, 3, 4) + timedelta(days=1))   # day after Thanksgiving
    dec24 = date(year, 12, 24)
    if dec24.weekday() < 5:
        ec.add(dec24)
    return ec


def _close_time(today: date) -> dtime:
    if today in early_closes(today.year):
        return dtime(13, 0)              # 1:00 PM ET early close
    return _parse(MARKET_CLOSE)


def market_is_open(now: datetime | None = None) -> bool:
    now = now or datetime.now(ET)
    today = now.date()
    if now.weekday() >= 5:               # weekend
        return False
    if today in market_holidays(now.year):
        return False
    return _parse(MARKET_OPEN) <= now.timetz().replace(tzinfo=None) <= _close_time(today)


def near_close(now: datetime | None = None) -> bool:
    """True within FLATTEN_BEFORE_CLOSE_MIN of the close (intraday flatten
    window). Uses the EARLY close time on half-days, so the flatten fires
    before a 13:00 close instead of waiting for a 16:00 that never comes."""
    now = now or datetime.now(ET)
    close = _close_time(now.date())
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
