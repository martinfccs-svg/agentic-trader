"""Technical indicators. Pure functions over Bars; unit-tested in selftest.py."""

from __future__ import annotations

from typing import Optional

from models import Bars


def sma(values: list[float], period: int) -> Optional[float]:
    if len(values) < period or period <= 0:
        return None
    return sum(values[-period:]) / period


def atr(bars: Bars, period: int = 14) -> Optional[float]:
    n = len(bars.close)
    if n < period + 1:
        return None
    trs = []
    for i in range(1, n):
        h, l, pc = bars.high[i], bars.low[i], bars.close[i - 1]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    a = sum(trs[:period]) / period
    for tr in trs[period:]:
        a = (a * (period - 1) + tr) / period
    return a


def vwap(bars: Bars) -> Optional[float]:
    if not bars.close or sum(bars.volume) == 0:
        return None
    num = sum(((bars.high[i] + bars.low[i] + bars.close[i]) / 3.0) * bars.volume[i]
              for i in range(len(bars.close)))
    return num / sum(bars.volume)


def relative_volume(bars: Bars, lookback: int = 20) -> Optional[float]:
    if len(bars.volume) < lookback + 1:
        return None
    base = sum(bars.volume[-(lookback + 1):-1]) / lookback
    return None if base == 0 else bars.volume[-1] / base


def avg_dollar_volume(bars: Bars, lookback: int = 20) -> Optional[float]:
    if len(bars.close) < lookback:
        return None
    return sum(bars.close[i] * bars.volume[i] for i in range(-lookback, 0)) / lookback


def prior_high(bars: Bars, lookback: int) -> Optional[float]:
    """Highest high over the prior `lookback` bars, EXCLUDING the latest bar
    (so a breakout = latest close above this)."""
    if len(bars.high) < lookback + 1:
        return None
    return max(bars.high[-(lookback + 1):-1])


def opening_range_high(bars: Bars, minutes: int) -> Optional[float]:
    """High of the first `minutes` 1-min bars (opening range breakout)."""
    if len(bars.high) < minutes:
        return None
    return max(bars.high[:minutes])
