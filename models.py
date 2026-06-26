"""Shared data models for the working (non-stub) system."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class System(str, Enum):
    SWING = "swing"
    INTRADAY = "intraday"


class SignalSource(str, Enum):
    INSIDER = "insider"
    CONGRESSIONAL = "congressional"
    SOCIAL = "social"


class FeedCriticality(str, Enum):
    SIGNAL = "signal"
    PRICE = "price"


class HealthState(str, Enum):
    CLOSED = "closed"
    HALF_OPEN = "half_open"
    OPEN = "open"


class Action(str, Enum):
    OPENED = "opened"
    REJECTED_BY_CONFIRMATION = "rejected_by_confirmation"
    REJECTED_BY_LIQUIDITY = "rejected_by_liquidity"
    REJECTED_BY_RISK = "rejected_by_risk"
    REJECTED_BY_FRESHNESS = "rejected_by_freshness"
    REJECTED_BY_KILL_SWITCH = "rejected_by_kill_switch"
    WATCHLISTED = "watchlisted"


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass
class Signal:
    source: SignalSource
    ticker: str
    detected_at: float = field(default_factory=time.time)
    transaction_date: Optional[str] = None   # "YYYY-MM-DD"
    raw: dict = field(default_factory=dict)

    @property
    def lag_days(self) -> Optional[float]:
        """Days between the underlying transaction and detection. The edge
        decays from the transaction date, so stale signals are filtered."""
        if not self.transaction_date:
            return None
        try:
            txn = datetime.strptime(self.transaction_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
        return (datetime.now(timezone.utc) - txn).total_seconds() / 86_400.0


@dataclass
class Bars:
    """OHLCV history for a ticker, oldest-first."""
    ticker: str
    close: list[float] = field(default_factory=list)
    high: list[float] = field(default_factory=list)
    low: list[float] = field(default_factory=list)
    volume: list[float] = field(default_factory=list)


@dataclass
class Quote:
    ticker: str
    price: float
    volume: float = 0.0
    vwap: Optional[float] = None
    rel_volume: Optional[float] = None
    atr: Optional[float] = None
    avg_dollar_volume: Optional[float] = None
    sma: Optional[float] = None
    as_of: float = field(default_factory=time.time)


@dataclass
class Fill:
    ticker: str
    side: Side
    shares: float
    price: float           # cost-adjusted fill price
    commission: float
    at: float = field(default_factory=time.time)


@dataclass
class Position:
    ticker: str
    system: System
    shares: float
    entry_price: float        # cost-adjusted
    entry_time: float
    stop_price: float
    source: SignalSource
    high_water: float = 0.0   # for trailing stop
    last_price: Optional[float] = None

    @property
    def unrealized_pnl(self) -> Optional[float]:
        if self.last_price is None:
            return None
        return (self.last_price - self.entry_price) * self.shares


@dataclass
class FeedHealth:
    endpoint: str
    criticality: FeedCriticality
    state: HealthState = HealthState.CLOSED
    consecutive_failures: int = 0
    last_status_code: Optional[int] = None
    last_error: Optional[str] = None
    last_success_at: Optional[float] = None
    opened_at: Optional[float] = None

    @property
    def is_available(self) -> bool:
        return self.state is not HealthState.OPEN
