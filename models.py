"""Shared data models for v6 (live-data, price-action)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class System(str, Enum):
    SWING = "swing"
    INTRADAY = "intraday"


class SignalSource(str, Enum):
    # Pure price-action sources (v6). No social/insider/congressional.
    TREND = "trend"        # daily breakout/trend -> swing
    MOMENTUM = "momentum"  # intraday momentum   -> intraday


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
    REJECTED_BY_KILL_SWITCH = "rejected_by_kill_switch"
    REJECTED_BY_MARKET_HOURS = "rejected_by_market_hours"


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass
class Signal:
    source: SignalSource
    ticker: str
    detected_at: float = field(default_factory=time.time)
    reason: str = ""             # why the scanner fired (audit)
    raw: dict = field(default_factory=dict)


@dataclass
class Bars:
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
    price: float
    commission: float
    at: float = field(default_factory=time.time)


@dataclass
class Position:
    ticker: str
    system: System
    shares: float
    entry_price: float
    entry_time: float
    stop_price: float
    source: SignalSource
    entry_stop: float = 0.0   # initial stop, never ratcheted (for R-multiple)
    high_water: float = 0.0
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
