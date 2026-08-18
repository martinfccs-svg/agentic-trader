"""Historical feed for backtesting — same interface as the live feed, but it
serves history "as of" a moving cursor so the strategy never sees the future.

This is what lets the backtest reuse the exact live code: the scanner and
engines call get_daily_bars()/get_quote() exactly as they do live, but here
those return only data up to the current simulated day. Advance the cursor one
day at a time and you have a no-lookahead replay.

EXECUTION MODEL (corrected 2026-08-17)
--------------------------------------
The original version returned today's close from BOTH get_daily_bars() and
get_quote(). So a signal was computed from the closing price and then filled
at that same closing price:

    scanner sees today's CLOSE -> "close > 20-day high" -> SIGNAL
    engine asks get_quote()    -> today's CLOSE
    broker fills               -> AT today's close

That is not lookahead in the strict sense — it never sees tomorrow — but the
fill is unobtainable. You cannot know the closing price and also transact at
it. Every entry got a price that depended on information available only after
trading ended, which flatters results in the same direction lookahead does.

Now: DECIDE on today's close, EXECUTE at tomorrow's price.

    get_daily_bars()  bars up to and including today   (decision data)
    get_quote()       price from the NEXT bar          (execution price)

The Quote's INDICATORS (atr, vwap, rel_volume) still come from the decision
slice, because those are what the strategy knew when it decided. Only the
price moves forward. That combination — today's information, tomorrow's fill
— is the honest daily convention, and it is what the crypto harness in this
same codebase already documented as "one-day execution lag (no lookahead)".

Set EXECUTION_LAG=0 to restore the old same-bar behaviour for comparison. It
is deliberately not the default: a harness whose default is optimistic will
be quoted optimistically.

WARM-UP
-------
The old code hardcoded a 30-bar minimum. The strategies use EMA200 and SMA200
trend gates, so at 30 bars those indicators are either unavailable or computed
on a tenth of the history they need — and roughly half a 365-day replay would
run on indicators that are not yet formed. WARMUP_BARS defaults to 200 and is
checked against the longest indicator, not guessed.

Data sources:
  - load_csv_dir(path): one CSV per ticker, columns date,open,high,low,close,volume
  - make_synthetic(...): synthetic history for PLUMBING TESTS ONLY. It is a
    random walk: there is no edge in it by construction, so any performance
    number produced from it measures the harness, not the strategy.
"""

from __future__ import annotations

import csv
import logging
import os
import random
from typing import Optional

from indicators import atr, avg_dollar_volume, relative_volume, sma, vwap
from models import Bars, FeedCriticality, FeedHealth, HealthState, Quote

log = logging.getLogger("historical_feed")

# Bars of history before the first tradeable day. 200 covers EMA200/SMA200,
# the longest indicator any desk uses.
WARMUP_BARS = int(os.getenv("BACKTEST_WARMUP_BARS", "200"))

# 1 = decide today, fill tomorrow (honest). 0 = same-bar fill (optimistic).
EXECUTION_LAG = int(os.getenv("BACKTEST_EXECUTION_LAG", "1"))


class HistoricalFeed:
    def __init__(self, full: dict[str, Bars], dates: dict[str, list[str]],
                 warmup: int = None, execution_lag: int = None) -> None:
        self._full = full                 # ticker -> complete Bars
        self._dates = dates               # ticker -> list of date strings (aligned)
        self._cursor = 0
        self._max = max((len(b.close) for b in full.values()), default=0)
        self._warmup = WARMUP_BARS if warmup is None else warmup
        self._lag = EXECUTION_LAG if execution_lag is None else execution_lag
        self.fills_at_next_bar = bool(self._lag)
        if self._max <= self._warmup + 20:
            log.error("historical feed: %d bars against a %d-bar warmup — "
                      "fewer than 20 tradeable days remain. Load more history "
                      "or lower BACKTEST_WARMUP_BARS (and know that the long "
                      "indicators will be unformed).", self._max, self._warmup)
        log.info("historical feed: %d bars, warmup %d, execution lag %d "
                 "(%s)", self._max, self._warmup, self._lag,
                 "decide today / fill tomorrow" if self._lag
                 else "SAME-BAR FILL — optimistic, comparison only")
        # health surface so the kill switch is satisfied (always healthy here)
        self._health = {
            "quote": FeedHealth("quote", FeedCriticality.PRICE, HealthState.CLOSED),
            "candle": FeedHealth("candle", FeedCriticality.PRICE, HealthState.CLOSED),
        }

    # ----- cursor control -----
    @property
    def cursor(self) -> int:
        return self._cursor

    @property
    def warmup(self) -> int:
        return self._warmup

    def set_cursor(self, i: int) -> None:
        if i < self._warmup:
            log.warning("historical feed: cursor %d is inside the %d-bar "
                        "warmup; long indicators will be unformed. Starting "
                        "at %d instead.", i, self._warmup, self._warmup)
            i = self._warmup
        self._cursor = i

    def start(self) -> None:
        """Position the cursor at the first tradeable bar."""
        self._cursor = self._warmup

    def has_next(self) -> bool:
        # With a one-day lag the LAST bar cannot be a decision day: there is
        # no following bar to fill against.
        return self._cursor < self._max - 1 - self._lag

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
        """Everything the strategy KNOWS as of today. Never includes tomorrow."""
        b = self._full.get(ticker)
        if b is None:
            return None
        end = self._cursor + 1
        if end < self._warmup:
            return None
        return Bars(ticker, close=b.close[:end], high=b.high[:end],
                    low=b.low[:end], volume=b.volume[:end])

    def get_daily_bars(self, ticker: str) -> Optional[Bars]:
        return self._slice(ticker)

    def get_intraday_bars(self, ticker: str) -> Optional[Bars]:
        # Daily replay: there is no intraday series. The intraday desk
        # therefore CANNOT trade in this harness — any "whole system" result
        # from a daily replay excludes it, and that should be stated in the
        # report rather than inferred from a missing row.
        return None

    def get_quote(self, ticker: str) -> Optional[Quote]:
        """Indicators as of the DECISION bar; price from the EXECUTION bar.

        The split is the whole point. A strategy deciding on Monday's close
        gets Monday's ATR and Tuesday's fill price — which is what it would
        actually get in production, where the order is placed after the close
        and executes the next session.
        """
        bars = self._slice(ticker)
        if bars is None or not bars.close:
            return None
        full = self._full.get(ticker)
        px = bars.close[-1]
        if self._lag and full is not None:
            i = self._cursor + self._lag
            if i < len(full.close):
                px = full.close[i]
            else:
                return None          # no bar to fill against: no quote
        return Quote(
            ticker=ticker, price=px, volume=bars.volume[-1],
            atr=atr(bars), vwap=vwap(bars), rel_volume=relative_volume(bars),
            avg_dollar_volume=avg_dollar_volume(bars), sma=sma(bars.close, 10),
        )


# ----------------------------------------------------------------------------
# Loaders
# ----------------------------------------------------------------------------

def load_csv_dir(path: str, warmup: int = None) -> HistoricalFeed:
    """Load one CSV per ticker: filename TICKER.csv, header date,open,high,low,close,volume."""
    full: dict[str, Bars] = {}
    dates: dict[str, list[str]] = {}
    need = (WARMUP_BARS if warmup is None else warmup) + 20
    short = []
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
        if len(b.close) >= need:
            full[ticker] = b
            dates[ticker] = ds
        elif b.close:
            short.append(f"{ticker}:{len(b.close)}")
    if short:
        # Named, not silently dropped. A universe that quietly shrank is a
        # different experiment from the one you think you ran.
        log.warning("historical feed: %d ticker(s) dropped for having fewer "
                    "than %d bars (warmup + 20): %s", len(short), need,
                    ", ".join(sorted(short)[:12]))
    if not full:
        raise SystemExit(
            f"No usable CSVs in {path}. Need date,open,high,low,close,volume "
            f"and at least {need} rows per ticker (warmup {WARMUP_BARS} + 20 "
            f"tradeable days).")
    return HistoricalFeed(full, dates, warmup=warmup)


def make_synthetic(tickers: list[str], days: int = 800, seed: int = 11,
                   warmup: int = None) -> HistoricalFeed:
    """Synthetic daily history — FOR PLUMBING TESTS ONLY.

    This is a random walk with drift. There is no exploitable structure in it,
    so a strategy cannot have an edge here and any CAGR or Sharpe it produces
    is noise. Use it to prove the harness runs end to end; never to judge a
    strategy.
    """
    log.warning("SYNTHETIC DATA: this is a random walk. Performance numbers "
                "from it measure the harness, not the strategy. Use --csv for "
                "any result you intend to act on.")
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
    return HistoricalFeed(full, dates, warmup=warmup)
