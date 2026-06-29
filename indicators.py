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


def rsi(values: list[float], period: int = 14) -> Optional[float]:
    """Wilder's RSI on a close series. 0-100; <30 oversold, >70 overbought."""
    if len(values) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(values)):
        d = values[i] - values[i - 1]
        gains.append(max(d, 0.0)); losses.append(max(-d, 0.0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100.0 - 100.0 / (1.0 + rs)


def trailing_return(values: list[float], lookback: int, skip: int = 0) -> Optional[float]:
    """Return over `lookback` bars, optionally skipping the most recent `skip`
    bars (classic momentum skips the latest month to avoid short-term reversal)."""
    if len(values) < lookback + skip + 1:
        return None
    end = values[-1 - skip]
    start = values[-1 - skip - lookback]
    if start <= 0:
        return None
    return end / start - 1.0
