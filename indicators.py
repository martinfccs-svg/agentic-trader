"""Technical indicators. Real implementations, unit-tested in selftest.py.

Pure functions over Bars so they're trivial to verify and reuse across both
engines. No external dependencies.
"""

from __future__ import annotations

from typing import Optional

from models import Bars


def sma(values: list[float], period: int) -> Optional[float]:
    if len(values) < period or period <= 0:
        return None
    return sum(values[-period:]) / period


def atr(bars: Bars, period: int = 14) -> Optional[float]:
    """Average True Range via Wilder's smoothing.

    True range = max(high-low, |high-prev_close|, |low-prev_close|).
    Returns None until there are enough bars.
    """
    n = len(bars.close)
    if n < period + 1:
        return None
    trs: list[float] = []
    for i in range(1, n):
        h, l, pc = bars.high[i], bars.low[i], bars.close[i - 1]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    # Wilder: seed with simple average of first `period` TRs, then smooth.
    atr_val = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr_val = (atr_val * (period - 1) + tr) / period
    return atr_val


def vwap(bars: Bars) -> Optional[float]:
    """Volume-weighted average price over the supplied bars (typical price)."""
    if not bars.close or sum(bars.volume) == 0:
        return None
    num = 0.0
    for i in range(len(bars.close)):
        typical = (bars.high[i] + bars.low[i] + bars.close[i]) / 3.0
        num += typical * bars.volume[i]
    return num / sum(bars.volume)


def relative_volume(bars: Bars, lookback: int = 20) -> Optional[float]:
    """Latest bar volume vs the average of the prior `lookback` bars."""
    if len(bars.volume) < lookback + 1:
        return None
    recent = bars.volume[-1]
    base = sum(bars.volume[-(lookback + 1):-1]) / lookback
    if base == 0:
        return None
    return recent / base


def avg_dollar_volume(bars: Bars, lookback: int = 20) -> Optional[float]:
    """Average daily dollar volume = mean(close * volume) over lookback."""
    if len(bars.close) < lookback:
        return None
    dvs = [bars.close[i] * bars.volume[i] for i in range(-lookback, 0)]
    return sum(dvs) / lookback
