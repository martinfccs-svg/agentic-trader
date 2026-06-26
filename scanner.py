"""Price-action scanner: turns candles into signals. No social/insider/congress.

Two scans over the universe:

  swing (TREND)     - on DAILY bars: close breaks above the prior N-day high,
                      price above the trend SMA, with a volume expansion.
  intraday (MOMENTUM) - on INTRADAY (1-min) bars: relative-volume spike, price
                      above VWAP, and a break above the opening range high.

The scanner only *flags* candidates. The engines apply liquidity, sizing, stop,
and the final confirmation before anything is bought. Each signal carries a
`reason` string so the funnel is auditable.
"""

from __future__ import annotations

import logging
from typing import Optional

from config import INTRADAY, SWING
from indicators import (
    avg_dollar_volume,
    opening_range_high,
    prior_high,
    relative_volume,
    sma,
    vwap,
)
from models import Bars, Signal, SignalSource

log = logging.getLogger("scanner")


class PriceActionScanner:
    def __init__(self, feed, universe: list[str]) -> None:
        self._feed = feed
        self._universe = universe

    # ----- swing: daily breakout + trend -----
    def scan_swing(self) -> list[Signal]:
        out: list[Signal] = []
        for t in self._universe:
            bars = self._feed.get_daily_bars(t)
            if bars is None or len(bars.close) < SWING.trend_sma_days + 1:
                continue
            close = bars.close[-1]
            hi = prior_high(bars, SWING.breakout_lookback)
            trend = sma(bars.close, SWING.trend_sma_days)
            rv = relative_volume(bars)
            if hi is None or trend is None or rv is None:
                continue
            breakout = close > hi
            uptrend = (not SWING.require_uptrend) or close > trend
            vol_ok = rv >= SWING.vol_spike_mult
            if breakout and uptrend and vol_ok:
                out.append(Signal(SignalSource.TREND, t,
                                  reason=f"close>{hi:.2f} 20d-high, >SMA{SWING.trend_sma_days}, rv={rv:.2f}"))
        return out

    # ----- intraday: opening-range / momentum breakout -----
    def scan_intraday(self) -> list[Signal]:
        out: list[Signal] = []
        for t in self._universe:
            bars = self._feed.get_intraday_bars(t)
            if bars is None or len(bars.close) < INTRADAY.opening_range_min + 1:
                continue
            close = bars.close[-1]
            orh = opening_range_high(bars, INTRADAY.opening_range_min)
            vw = vwap(bars)
            rv = relative_volume(bars)
            if orh is None or rv is None:
                continue
            momentum = close > orh
            above_vwap = (not INTRADAY.require_above_vwap) or (vw is not None and close > vw)
            vol_ok = rv >= INTRADAY.min_rel_volume
            if momentum and above_vwap and vol_ok:
                out.append(Signal(SignalSource.MOMENTUM, t,
                                  reason=f"close>{orh:.2f} ORH, >VWAP, rv={rv:.2f}"))
        return out

    def scan_all(self) -> list[Signal]:
        return self.scan_swing() + self.scan_intraday()
