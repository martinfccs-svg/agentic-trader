"""Feed layer: the single gateway to market/signal data.

Keeps the skeleton's guarantees (one rate-limit budget, per-endpoint circuit
breaker that records the real error, visible health) and adds two concrete
data sources behind a common interface:

  FinnhubFeed   - real data. Parsing is implemented against Finnhub's documented
                  response shapes; VERIFY with verify_endpoints.py since the
                  free/premium split and exact fields can differ on your tier.
  SimulatedFeed - deterministic synthetic data so the whole system runs and is
                  testable with zero external dependencies (used by selftest and
                  by main.py when FINNHUB_API_KEY is unset).
"""

from __future__ import annotations

import logging
import math
import random
import threading
import time
from collections import deque
from typing import Callable, Optional, Protocol

from config import (
    BREAKER_COOLDOWN_SECONDS,
    BREAKER_FAILURE_THRESHOLD,
    ENDPOINTS,
    RATE_LIMIT_CALLS,
    RATE_LIMIT_WINDOW_SECONDS,
)
from indicators import atr, avg_dollar_volume, relative_volume, sma, vwap
from models import (
    Bars,
    FeedHealth,
    HealthState,
    Quote,
    Signal,
    SignalSource,
)

log = logging.getLogger("feed")


# ====================== rate limiter + breaker ============================

class RateLimiter:
    def __init__(self, max_calls: int, window_seconds: int) -> None:
        self._max = max_calls
        self._window = window_seconds
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
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
    def __init__(self, health: FeedHealth) -> None:
        self.health = health

    def allow_request(self) -> bool:
        h = self.health
        if h.state is HealthState.OPEN:
            assert h.opened_at is not None
            if time.time() - h.opened_at >= BREAKER_COOLDOWN_SECONDS:
                h.state = HealthState.HALF_OPEN
                return True
            return False
        return True

    def record_success(self) -> None:
        h = self.health
        h.consecutive_failures = 0
        h.last_success_at = time.time()
        h.last_status_code = 200
        h.last_error = None
        h.state = HealthState.CLOSED

    def record_failure(self, status_code: Optional[int], error: str) -> None:
        h = self.health
        h.consecutive_failures += 1
        h.last_status_code = status_code
        h.last_error = error
        log.warning("feed %s failure #%d (status=%s): %s",
                    h.endpoint, h.consecutive_failures, status_code, error)
        if h.consecutive_failures >= BREAKER_FAILURE_THRESHOLD:
            if h.state is not HealthState.OPEN:
                h.opened_at = time.time()
                log.error("breaker %s -> OPEN after %d failures (status=%s). "
                          "Diagnose root cause.", h.endpoint,
                          h.consecutive_failures, status_code)
            h.state = HealthState.OPEN


# ====================== data feed interface ===============================

class DataFeed(Protocol):
    def get_bars(self, ticker: str) -> Optional[Bars]: ...
    def get_quote(self, ticker: str) -> Optional[Quote]: ...
    def get_signals(self) -> list[Signal]: ...
    def get_social_buzz(self, ticker: str) -> Optional[float]: ...


class _HealthMixin:
    """Provides the breaker/health surface the kill switch reads."""
    def __init__(self) -> None:
        self._breakers = {
            key: CircuitBreaker(FeedHealth(endpoint=key, criticality=ep.criticality))
            for key, ep in ENDPOINTS.items()
        }

    def health(self, key: str) -> FeedHealth:
        return self._breakers[key].health

    def all_health(self) -> dict[str, FeedHealth]:
        return {k: b.health for k, b in self._breakers.items()}


# ====================== Finnhub (real) ====================================

class FinnhubFeed(_HealthMixin):
    def __init__(self, client) -> None:
        super().__init__()
        self._client = client
        self._limiter = RateLimiter(RATE_LIMIT_CALLS, RATE_LIMIT_WINDOW_SECONDS)
        self._universe: list[str] = []   # set by main from your scan list

    def set_universe(self, tickers: list[str]) -> None:
        self._universe = tickers

    def _call(self, key: str, *args, **kwargs):
        # Skip if endpoint is disabled in config
        if key not in ENDPOINTS:
            return None
        breaker = self._breakers[key]
        if not breaker.allow_request():
            return None
        method = getattr(self._client, ENDPOINTS[key].client_method)
        self._limiter.acquire()
        try:
            result = method(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            breaker.record_failure(getattr(exc, "status_code", None), repr(exc))
            return None
        breaker.record_success()
        return result

    def get_bars(self, ticker: str) -> Optional[Bars]:
        now = int(time.time())
        raw = self._call("candle", ticker, "D", now - 60 * 86_400, now)
        if not raw or raw.get("s") != "ok":
            return None
        return Bars(ticker=ticker, close=raw["c"], high=raw["h"],
                    low=raw["l"], volume=raw["v"])

    def get_quote(self, ticker: str) -> Optional[Quote]:
        raw = self._call("quote", ticker)
        if not raw or "c" not in raw:
            return None
        bars = self.get_bars(ticker)
        return Quote(
            ticker=ticker, price=raw["c"],
            atr=atr(bars) if bars else None,
            vwap=vwap(bars) if bars else None,
            rel_volume=relative_volume(bars) if bars else None,
            avg_dollar_volume=avg_dollar_volume(bars) if bars else None,
            sma=sma(bars.close, 10) if bars else None,
        )

    def get_signals(self) -> list[Signal]:
        signals: list[Signal] = []
        for ticker in self._universe:
            # Only call insider if endpoint is enabled
            if "insider" in ENDPOINTS:
                ins = self._call("insider", ticker)
                for row in (ins or {}).get("data", []):
                    # Finnhub insider rows expose transactionDate + change (+buy/-sell).
                    if row.get("change", 0) > 0:
                        signals.append(Signal(SignalSource.INSIDER, ticker,
                                              transaction_date=row.get("transactionDate"),
                                              raw=row))
            # Only call congressional if endpoint is enabled
            if "congressional" in ENDPOINTS:
                cong = self._call("congressional", ticker)
                for row in (cong or {}).get("data", []):
                    if str(row.get("transactionType", "")).lower().startswith("purchase"):
                        signals.append(Signal(SignalSource.CONGRESSIONAL, ticker,
                                              transaction_date=row.get("transactionDate"),
                                              raw=row))
        return signals

    def get_social_buzz(self, ticker: str) -> Optional[float]:
        # Only call social if endpoint is enabled
        if "social" not in ENDPOINTS:
            return None
        raw = self._call("social", ticker)
        if not raw:
            return None
        rows = (raw.get("reddit") or []) + (raw.get("twitter") or [])
        if not rows:
            return None
        return sum(r.get("mention", 0) for r in rows)


# ====================== Simulated (no key needed) =========================

class SimulatedFeed(_HealthMixin):
    """Deterministic synthetic market. Lets the full system run and be tested
    with zero external dependencies. Not random across runs unless reseeded."""

    def __init__(self, tickers: list[str], seed: int = 7) -> None:
        super().__init__()
        self._rng = random.Random(seed)
        self._tickers = tickers
        self._t = 0
        self._series: dict[str, Bars] = {t: self._make_series(t) for t in tickers}

    def _make_series(self, ticker: str) -> Bars:
        price = self._rng.uniform(8, 60)
        b = Bars(ticker=ticker)
        drift = self._rng.uniform(-0.001, 0.002)
        for i in range(80):
            price *= (1 + drift + self._rng.gauss(0, 0.02))
            price = max(price, 1.0)
            hi = price * (1 + abs(self._rng.gauss(0, 0.01)))
            lo = price * (1 - abs(self._rng.gauss(0, 0.01)))
            vol = self._rng.uniform(2_000_000, 8_000_000)
            if i == 79:  # final bar: simulate an intraday volume spike sometimes
                vol *= self._rng.choice([1.0, 1.0, 1.8, 2.5])
            b.close.append(round(price, 2)); b.high.append(round(hi, 2))
            b.low.append(round(lo, 2)); b.volume.append(round(vol))
        return b

    def get_bars(self, ticker: str) -> Optional[Bars]:
        return self._series.get(ticker)

    def get_quote(self, ticker: str) -> Optional[Quote]:
        bars = self._series[ticker]
        return Quote(
            ticker=ticker, price=bars.close[-1], volume=bars.volume[-1],
            atr=atr(bars), vwap=vwap(bars), rel_volume=relative_volume(bars),
            avg_dollar_volume=avg_dollar_volume(bars), sma=sma(bars.close, 10),
        )

    def get_signals(self) -> list[Signal]:
        out: list[Signal] = []
        for t in self._tickers:
            roll = self._rng.random()
            if roll < 0.15 and "insider" in ENDPOINTS:
                out.append(Signal(SignalSource.INSIDER, t,
                                  transaction_date=_recent_date(self._rng)))
            elif roll < 0.25 and "congressional" in ENDPOINTS:
                out.append(Signal(SignalSource.CONGRESSIONAL, t,
                                  transaction_date=_recent_date(self._rng)))
            elif roll < 0.45 and "social" in ENDPOINTS:
                out.append(Signal(SignalSource.SOCIAL, t))
        return out

    def get_social_buzz(self, ticker: str) -> Optional[float]:
        return self._rng.uniform(0, 100)

    def step_prices(self) -> None:
        """Advance one bar so positions can move and exits can trigger."""
        for bars in self._series.values():
            last = bars.close[-1]
            nxt = max(last * (1 + self._rng.gauss(0.0005, 0.02)), 1.0)
            bars.close.append(round(nxt, 2))
            bars.high.append(round(nxt * 1.005, 2))
            bars.low.append(round(nxt * 0.995, 2))
            bars.volume.append(round(self._rng.uniform(2_000_000, 8_000_000)))


def _recent_date(rng: random.Random) -> str:
    from datetime import datetime, timedelta, timezone
    days = rng.choice([1, 2, 3, 5, 8, 14, 30])   # some fresh, some stale
    d = datetime.now(timezone.utc) - timedelta(days=days)
    return d.strftime("%Y-%m-%d")


def build_feed(tickers: list[str]):
    """Return a real Finnhub feed if a key is present, else the simulator."""
    from config import FINNHUB_API_KEY
    if FINNHUB_API_KEY:
        import finnhub  # type: ignore
        feed = FinnhubFeed(finnhub.Client(api_key=FINNHUB_API_KEY))
        feed.set_universe(tickers)
        log.info("using FinnhubFeed (live data)")
        return feed
    log.warning("FINNHUB_API_KEY unset -> using SimulatedFeed (paper/testing only)")
    return SimulatedFeed(tickers)
