"""Feed layer: single gateway to Finnhub data (real, paid tier).

Provides separate DAILY bars (for the swing scanner) and INTRADAY 1-min bars
(for the intraday scanner), real quotes, plus the rate limiter / circuit breaker
/ health surface. SimulatedFeed remains so the system runs and is testable with
no key (local dev / CI). Finnhub is data only — execution lives in brokers.py.

v6.1: the health surface now exposes is_down(key) with breaker-based
semantics (OPEN, or at/over the consecutive-failure threshold). Consumers —
the kill switch in particular — must use this instead of judging health by
success recency: the quote endpoint is only exercised on the entry/manage
path, so "no recent success" is the NORMAL state of a flat book, not an
outage. Recency-as-health deadlocked all entries on 2026-07-07.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections import deque
from typing import Optional, Protocol

from config import (
    DAILY_LOOKBACK_DAYS,
    DAILY_RESOLUTION,
    ENDPOINTS,
    INTRADAY_LOOKBACK_MIN,
    INTRADAY_RESOLUTION,
    RATE_LIMIT_CALLS,
    RATE_LIMIT_WINDOW_SECONDS,
)
from indicators import atr, avg_dollar_volume, relative_volume, sma, vwap
from models import Bars, FeedHealth, HealthState, Quote

log = logging.getLogger("feed")

BREAKER_FAILURE_THRESHOLD = 3
BREAKER_COOLDOWN_SECONDS = 300


class RateLimiter:
    def __init__(self, max_calls, window):
        self._max, self._window = max_calls, window
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self):
        while True:
            with self._lock:
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= self._window:
                    self._calls.popleft()
                if len(self._calls) < self._max:
                    self._calls.append(now)
                    return
                sleep_for = self._window - (now - self._calls[0])
            time.sleep(max(sleep_for, 0.01))


class CircuitBreaker:
    def __init__(self, health: FeedHealth):
        self.health = health

    def allow_request(self):
        h = self.health
        if h.state is HealthState.OPEN:
            if time.time() - (h.opened_at or 0) >= BREAKER_COOLDOWN_SECONDS:
                h.state = HealthState.HALF_OPEN
                return True
            return False
        return True

    def record_success(self):
        h = self.health
        h.consecutive_failures = 0
        h.last_success_at = time.time()
        h.last_status_code = 200
        h.last_error = None
        h.state = HealthState.CLOSED

    def record_failure(self, status, error):
        h = self.health
        h.consecutive_failures += 1
        h.last_status_code = status
        h.last_error = error
        log.warning("feed %s failure #%d (status=%s): %s",
                    h.endpoint, h.consecutive_failures, status, error)
        if h.consecutive_failures >= BREAKER_FAILURE_THRESHOLD:
            if h.state is not HealthState.OPEN:
                h.opened_at = time.time()
                log.error("breaker %s -> OPEN (status=%s). Diagnose root cause.",
                          h.endpoint, status)
            h.state = HealthState.OPEN


class DataFeed(Protocol):
    def get_daily_bars(self, ticker: str) -> Optional[Bars]: ...
    def get_intraday_bars(self, ticker: str) -> Optional[Bars]: ...
    def get_quote(self, ticker: str) -> Optional[Quote]: ...


class _HealthMixin:
    def __init__(self):
        self._breakers = {k: CircuitBreaker(FeedHealth(endpoint=k, criticality=ep.criticality))
                          for k, ep in ENDPOINTS.items()}

    def health(self, key): return self._breakers[key].health
    def all_health(self): return {k: b.health for k, b in self._breakers.items()}

    def is_down(self, key) -> bool:
        """True iff the endpoint is ACTIVELY FAILING: breaker OPEN, or at/over
        the consecutive-failure threshold (covers the window between the
        first failures and the breaker formally opening).

        Deliberately NOT based on last_success_at recency. An endpoint that
        has not been called recently is healthy until proven otherwise —
        every real failure is recorded the moment it happens, so there is no
        detection gap, and recency-as-health deadlocks any endpoint that is
        only exercised conditionally (quote is only called on entry/manage,
        so a flat book made it look 'stale' and vetoed all entries on
        2026-07-07)."""
        h = self._breakers[key].health
        return (h.state is HealthState.OPEN
                or h.consecutive_failures >= BREAKER_FAILURE_THRESHOLD)


class FinnhubFeed(_HealthMixin):
    def __init__(self, client):
        super().__init__()
        self._client = client
        self._limiter = RateLimiter(RATE_LIMIT_CALLS, RATE_LIMIT_WINDOW_SECONDS)
        self._cache: dict = {}        # per-cycle memo (quotes, intraday bars)
        self._daily_cache: dict = {}  # ticker -> (cycle_stamp, Bars); slow TTL
        self._cycle_n = 0

    def new_cycle(self):
        """Advance the cycle clock and clear the per-cycle memo. Daily bars are
        NOT cleared here -- they refresh on a slow TTL (DAILY_BARS_REFRESH_CYCLES)
        because daily candles don't change during the session. This tiering is
        what lets a ~36-name universe fit inside the Finnhub rate budget:
        intraday cost is per-cycle for a liquid subset; daily cost amortizes to
        a couple of calls per minute across the whole universe."""
        self._cycle_n += 1
        self._cache.clear()

    def _memo(self, key, fn):
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    def _call(self, key, *args, **kwargs):
        b = self._breakers[key]
        if not b.allow_request():
            return None
        method = getattr(self._client, ENDPOINTS[key].client_method)
        self._limiter.acquire()
        try:
            r = method(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            b.record_failure(getattr(exc, "status_code", None), repr(exc))
            return None
        b.record_success()
        return r

    def _candles(self, ticker, resolution, frm, to) -> Optional[Bars]:
        raw = self._call("candle", ticker, resolution, frm, to)
        if not raw or raw.get("s") != "ok":
            return None
        return Bars(ticker, close=raw["c"], high=raw["h"], low=raw["l"], volume=raw["v"])

    def get_daily_bars(self, ticker):
        # Slow-TTL cache: daily candles don't change intraday.
        from config import DAILY_BARS_REFRESH_CYCLES
        hit = self._daily_cache.get(ticker)
        if hit is not None and (self._cycle_n - hit[0]) < DAILY_BARS_REFRESH_CYCLES:
            return hit[1]
        now = int(time.time())
        bars = self._candles(ticker, DAILY_RESOLUTION, now - DAILY_LOOKBACK_DAYS * 86_400, now)
        if bars is not None:
            self._daily_cache[ticker] = (self._cycle_n, bars)
            return bars
        # Fetch failed (breaker open / transient): serve stale rather than nothing.
        return hit[1] if hit is not None else None

    def get_intraday_bars(self, ticker):
        def fetch():
            now = int(time.time())
            return self._candles(ticker, INTRADAY_RESOLUTION, now - INTRADAY_LOOKBACK_MIN * 60, now)
        return self._memo(("intra", ticker), fetch)

    def get_quote(self, ticker):
        def fetch():
            raw = self._call("quote", ticker)
            if not raw or "c" not in raw:
                return None
            # Cached bars: get_intraday/get_daily reuse the per-cycle memo.
            bars = self.get_intraday_bars(ticker) or self.get_daily_bars(ticker)
            return Quote(
                ticker=ticker, price=raw["c"],
                atr=atr(bars) if bars else None,
                vwap=vwap(bars) if bars else None,
                rel_volume=relative_volume(bars) if bars else None,
                avg_dollar_volume=avg_dollar_volume(bars) if bars else None,
                sma=sma(bars.close, 10) if bars else None,
            )
        return self._memo(("quote", ticker), fetch)


class SimulatedFeed(_HealthMixin):
    """No-key fallback for local testing. Deterministic per seed."""

    def __init__(self, tickers, seed=7):
        super().__init__()
        self._rng = random.Random(seed)
        self._tickers = tickers
        self._daily = {t: self._series(t, 320, 0.02) for t in tickers}
        self._intraday = {t: self._series(t, 240, 0.004) for t in tickers}

    def _series(self, ticker, n, vol):
        price = self._rng.uniform(8, 60)
        b = Bars(ticker)
        drift = self._rng.uniform(-0.0005, 0.001)
        for i in range(n):
            price = max(price * (1 + drift + self._rng.gauss(0, vol)), 1.0)
            b.close.append(round(price, 2))
            b.high.append(round(price * (1 + abs(self._rng.gauss(0, vol / 2))), 2))
            b.low.append(round(price * (1 - abs(self._rng.gauss(0, vol / 2))), 2))
            v = self._rng.uniform(2_000_000, 8_000_000)
            if i == n - 1:
                v *= self._rng.choice([1.0, 1.0, 1.6, 2.2])
            b.volume.append(round(v))
        return b

    def get_daily_bars(self, ticker): return self._daily.get(ticker)
    def get_intraday_bars(self, ticker): return self._intraday.get(ticker)
    def new_cycle(self): pass        # local data; nothing to cache/clear

    def get_quote(self, ticker):
        bars = self._intraday[ticker]
        return Quote(ticker, price=bars.close[-1], volume=bars.volume[-1],
                     atr=atr(bars), vwap=vwap(bars), rel_volume=relative_volume(bars),
                     avg_dollar_volume=avg_dollar_volume(bars), sma=sma(bars.close, 10))

    def step_prices(self):
        for store in (self._daily, self._intraday):
            for b in store.values():
                nxt = max(b.close[-1] * (1 + self._rng.gauss(0.0008, 0.012)), 1.0)
                b.close.append(round(nxt, 2))
                b.high.append(round(nxt * 1.004, 2))
                b.low.append(round(nxt * 0.996, 2))
                b.volume.append(round(self._rng.uniform(2_000_000, 8_000_000)))


def build_feed(tickers):
    from config import FINNHUB_API_KEY
    if FINNHUB_API_KEY:
        import finnhub
        log.info("using FinnhubFeed (live data)")
        return FinnhubFeed(finnhub.Client(api_key=FINNHUB_API_KEY))
    log.warning("FINNHUB_API_KEY unset -> SimulatedFeed (testing only)")
    return SimulatedFeed(tickers)
