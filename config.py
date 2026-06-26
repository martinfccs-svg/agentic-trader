"""Configuration for agentic-trader v5 (two-system rebuild).

Preserves the v4.1 environment-variable names and defaults so this is
continuous with the existing Railway setup. New in v5: feeds are split into
two coherent systems (swing vs intraday) and risk is managed per system.

STEP ZERO before live data: run `python verify_endpoints.py` with a real key.
Until then `main.py` runs against a SimulatedFeed so the system is testable
with zero external dependencies.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from models import FeedCriticality, SignalSource, System


def _f(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _i(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _b(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------
# Mode + credentials
# --------------------------------------------------------------------------
TRADING_MODE = os.environ.get("TRADING_MODE", "PAPER").upper()   # PAPER (default) or LIVE
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")                    # LIVE-mode phone alerts

# --------------------------------------------------------------------------
# Account + sizing (v4.1 names preserved)
# --------------------------------------------------------------------------
START_EQUITY = _f("START_EQUITY", 50_000)
RISK_PER_TRADE_PCT = _f("RISK_PER_TRADE_PCT", 0.01)   # risk 1% of equity per trade
MAX_POSITION_SIZE = _f("MAX_POSITION_SIZE", 3_000)    # $ cap per position
DAILY_LOSS_LIMIT = _f("DAILY_LOSS_LIMIT", 2_500)      # halt new entries past this realized loss

# --------------------------------------------------------------------------
# Liquidity filter (v4.1)
# --------------------------------------------------------------------------
MIN_PRICE = _f("MIN_PRICE", 5)
MIN_DOLLAR_VOL = _f("MIN_DOLLAR_VOL", 5_000_000)

# --------------------------------------------------------------------------
# Exits (v4.1): trailing-stop geometry
# --------------------------------------------------------------------------
STOP_LOSS_PCT = _f("STOP_LOSS_PCT", 0.05)   # fallback fixed stop distance
TRAIL_PCT = _f("TRAIL_PCT", 0.06)           # trailing stop distance from the high

# --------------------------------------------------------------------------
# Cost model for PAPER fills (so paper P&L is cost-adjusted, not fantasy)
# --------------------------------------------------------------------------
COMMISSION_PER_TRADE = _f("COMMISSION_PER_TRADE", 0.0)
SLIPPAGE_BPS = _f("SLIPPAGE_BPS", 5)        # 5 bps each way

# --------------------------------------------------------------------------
# Per-system parameters
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class SwingParams:
    atr_stop_multiple: float = _f("SWING_ATR_MULT", 2.5)   # wide
    max_positions: int = _i("SWING_MAX_POS", 4)
    max_signal_lag_days: int = _i("SWING_MAX_LAG_DAYS", 10)
    require_uptrend: bool = _b("REQUIRE_UPTREND", True)
    trend_sma_days: int = _i("TREND_SMA_DAYS", 10)


@dataclass(frozen=True)
class IntradayParams:
    atr_stop_multiple: float = _f("INTRADAY_ATR_MULT", 1.0)  # tight
    max_positions: int = _i("INTRADAY_MAX_POS", 4)
    min_rel_volume: float = _f("VOL_SPIKE_MULT", 1.3)        # v4.1 VOL_SPIKE_MULT
    require_volume_spike: bool = _b("REQUIRE_VOLUME_SPIKE", True)
    require_above_vwap: bool = _b("INTRADAY_REQUIRE_VWAP", True)
    trail_pct: float = TRAIL_PCT


SWING = SwingParams()
INTRADAY = IntradayParams()


# --------------------------------------------------------------------------
# Endpoint registry + rate limit + breaker (unchanged contract from skeleton)
# --------------------------------------------------------------------------
RATE_LIMIT_CALLS = 55
RATE_LIMIT_WINDOW_SECONDS = 60
BREAKER_FAILURE_THRESHOLD = 3
BREAKER_COOLDOWN_SECONDS = 300


@dataclass(frozen=True)
class Endpoint:
    key: str
    client_method: str
    criticality: FeedCriticality
    poll_interval_seconds: int
    premium_uncertain: bool = False


ENDPOINTS: dict[str, Endpoint] = {
    "insider": Endpoint("insider", "stock_insider_transactions",
                        FeedCriticality.SIGNAL, 120, premium_uncertain=True),
    "congressional": Endpoint("congressional", "congressional_trading",
                              FeedCriticality.SIGNAL, 300, premium_uncertain=True),
    "social": Endpoint("social", "stock_social_sentiment",
                       FeedCriticality.SIGNAL, 300, premium_uncertain=True),
    "quote": Endpoint("quote", "quote", FeedCriticality.PRICE, 5),
    "candle": Endpoint("candle", "stock_candles", FeedCriticality.PRICE, 60),
}

SYSTEM_REQUIRED_FEEDS: dict[System, list[str]] = {
    System.SWING:    ["insider", "congressional", "candle"],
    System.INTRADAY: ["social", "quote", "candle"],
}

SOURCE_TO_SYSTEM: dict[SignalSource, System] = {
    SignalSource.INSIDER:       System.SWING,
    SignalSource.CONGRESSIONAL: System.SWING,
    SignalSource.SOCIAL:        System.INTRADAY,
}
