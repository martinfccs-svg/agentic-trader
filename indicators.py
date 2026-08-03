"""Technical indicators. Pure functions over Bars; unit-tested in selftest.py."""
from __future__ import annotations
from typing import Optional
from models import Bars
def ema(values: list[float], period: int) -> Optional[float]:
    """Exponential moving average, seeded with the SMA of the first `period`.

    CANONICAL (2026-08-02). Seven byte-identical copies of this function had
    accumulated across meanrev_scoring, intraday_scoring, xsection, swing_v2,
    swing_engine and regime_allocation (twice) — because indicators.py had
    sma, atr, rsi and vwap but never an ema, so every module that needed one
    wrote it again. They were all correct today. The risk is tomorrow: a fix
    applied to one copy leaves six running the old behaviour, and nothing
    reports the divergence. ADX already demonstrated the pattern, which is
    why it now lives here too.
    """
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def ema_series(values: list[float], period: int) -> list[float]:
    """EMA at every point from `period` onward — for slope and trend tests."""
    if len(values) < period:
        return []
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    out = [e]
    for v in values[period:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def adx(high: list[float], low: list[float], close: list[float],
        n: int = 14) -> Optional[float]:
    """Wilder's ADX — trend STRENGTH, not direction. Needs ~2n+1 bars.

    MOVED VERBATIM from meanrev_scoring (2026-08-02), not rewritten. That
    distinction matters: the first attempt at this consolidation REIMPLEMENTED
    the function from memory and produced 91.7 where the original gave 89.0 —
    a simple mean of the last n DX values instead of Wilder smoothing applied
    to the DX series. Consolidation that rewrites is not consolidation; it is
    an eighth implementation with extra steps. meanrev_scoring re-exports this
    so existing imports keep working.
    """
    if len(close) < 2 * n + 1:
        return None
    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(close)):
        up, down = high[i] - high[i - 1], low[i - 1] - low[i]
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
        trs.append(max(high[i] - low[i], abs(high[i] - close[i - 1]),
                       abs(low[i] - close[i - 1])))
    atr_s = sum(trs[:n]); pdm_s = sum(plus_dm[:n]); mdm_s = sum(minus_dm[:n])
    dxs = []
    for i in range(n, len(trs)):
        atr_s = atr_s - atr_s / n + trs[i]
        pdm_s = pdm_s - pdm_s / n + plus_dm[i]
        mdm_s = mdm_s - mdm_s / n + minus_dm[i]
        if atr_s <= 0:
            continue
        pdi = 100 * pdm_s / atr_s
        mdi = 100 * mdm_s / atr_s
        if pdi + mdi == 0:
            continue
        dxs.append(100 * abs(pdi - mdi) / (pdi + mdi))
    if len(dxs) < n:
        return None
    a = sum(dxs[:n]) / n
    for d in dxs[n:]:
        a = (a * (n - 1) + d) / n
    return a


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
