"""Historical feed for backtesting — same interface as the live feed, but it
serves history "as of" a moving cursor so the strategy never sees the future.

This is what lets the backtest reuse the exact live code: the scanner and
engines call get_daily_bars()/get_quote() exactly as they do live, but here
those return only data up to the current simulated day. Advance the cursor one
day at a time and you have a no-lookahead replay.

Data sources:
  - load_csv_dir(path): one CSV per ticker, columns date,open,high,low,close,volume
  - make_synthetic(...): long synthetic history so the harness runs with no data
"""

from __future__ import annotations

import csv
import os
import random
from typing import Optional

from indicators import atr, avg_dollar_volume, relative_volume, sma, vwap
from models import Bars, FeedCriticality, FeedHealth, HealthState, Quote


class HistoricalFeed:
    def __init__(self, full: dict[str, Bars], dates: dict[str, list[str]]) -> None:
        self._full = full                 # ticker -> complete Bars
        self._dates = dates               # ticker -> list of date strings (aligned)
        self._cursor = 0
        self._max = max((len(b.close) for b in full.values()), default=0)
        # health surface so the kill switch is satisfied (always healthy here)
        self._health = {
            "quote": FeedHealth("quote", FeedCriticality.PRICE, HealthState.CLOSED),
            "candle": FeedHealth("candle", FeedCriticality.PRICE, HealthState.CLOSED),
        }

    # ----- cursor control -----
    @property
    def cursor(self) -> int:
        return self._cursor

    def set_cursor(self, i: int) -> None:
        self._cursor = i

    def has_next(self) -> bool:
        return self._cursor < self._max - 1

    def advance(self) -> None:
        self._cursor += 1

    def current_date(self, ticker: str | None = None) -> Optional[str]:
        t = ticker or next(iter(self._dates), None)
        if t and self._cursor < len(self._dates[t]):
            return self._dates[t][self._cursor]
        return None

    # ----- feed interface (same as live) -----
    def health(self, key: str) -> FeedHealth:
        return self._health[key]

    def all_health(self):
        return dict(self._health)

    def _slice(self, ticker: str) -> Optional[Bars]:
        b = self._full.get(ticker)
        if b is None:
            return None
        end = self._cursor + 1
        if end < 30:                       # need some history for indicators
            return None
        return Bars(ticker, close=b.close[:end], high=b.high[:end],
                    low=b.low[:end], volume=b.volume[:end])

    def get_daily_bars(self, ticker: str) -> Optional[Bars]:
        return self._slice(ticker)

    def get_intraday_bars(self, ticker: str) -> Optional[Bars]:
        return None                        # daily backtest: no intraday replay

    def get_quote(self, ticker: str) -> Optional[Quote]:
        bars = self._slice(ticker)
        if bars is None or not bars.close:
            return None
        return Quote(
            ticker=ticker, price=bars.close[-1], volume=bars.volume[-1],
            atr=atr(bars), vwap=vwap(bars), rel_volume=relative_volume(bars),
            avg_dollar_volume=avg_dollar_volume(bars), sma=sma(bars.close, 10),
        )


# ----------------------------------------------------------------------------
# Loaders
# ----------------------------------------------------------------------------

def load_csv_dir(path: str) -> HistoricalFeed:
    """Load one CSV per ticker: filename TICKER.csv, header date,open,high,low,close,volume."""
    full: dict[str, Bars] = {}
    dates: dict[str, list[str]] = {}
    for fn in sorted(os.listdir(path)):
        if not fn.lower().endswith(".csv"):
            continue
        ticker = os.path.splitext(fn)[0].upper()
        b = Bars(ticker)
        ds: list[str] = []
        with open(os.path.join(path, fn)) as fh:
            for row in csv.DictReader(fh):
                try:
                    b.close.append(float(row["close"])); b.high.append(float(row["high"]))
                    b.low.append(float(row["low"])); b.volume.append(float(row["volume"]))
                    ds.append(row["date"])
                except (KeyError, ValueError):
                    continue
        if len(b.close) >= 60:
            full[ticker] = b
            dates[ticker] = ds
    if not full:
        raise SystemExit(f"No usable CSVs in {path} (need date,open,high,low,close,volume).")
    return HistoricalFeed(full, dates)


def make_synthetic(tickers: list[str], days: int = 800, seed: int = 11) -> HistoricalFeed:
    """Long synthetic daily history so the harness runs with zero real data."""
    rng = random.Random(seed)
    full: dict[str, Bars] = {}
    dates: dict[str, list[str]] = {}
    from datetime import date, timedelta
    start = date(2022, 1, 3)
    for t in tickers:
        price = rng.uniform(10, 80)
        drift = rng.uniform(-0.0003, 0.0009)     # dispersion across names
        b = Bars(t); ds = []
        d = start
        for _ in range(days):
            price = max(price * (1 + drift + rng.gauss(0, 0.018)), 1.0)
            b.close.append(round(price, 2))
            b.high.append(round(price * (1 + abs(rng.gauss(0, 0.01))), 2))
            b.low.append(round(price * (1 - abs(rng.gauss(0, 0.01))), 2))
            b.volume.append(round(rng.uniform(3_000_000, 9_000_000)))
            while d.weekday() >= 5:
                d += timedelta(days=1)
            ds.append(d.isoformat()); d += timedelta(days=1)
        full[t] = b; dates[t] = ds
    return HistoricalFeed(full, dates)
