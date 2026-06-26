"""Configuration for agentic-trader v6 (live-data, pure price action).

Data:      Finnhub paid tier (real candles + quotes + websocket).
Signals:   pure price action (a scanner over candles) - no social/insider/congress.
Execution: a Broker. PAPER by default. LIVE requires Alpaca creds AND an explicit
           confirmation env var, so real money is never one accidental flag away.

Run `python verify_endpoints.py` once after setting your key to confirm the
candle endpoints you paid for actually return data.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from models import FeedCriticality, SignalSource, System


def _f(name: str, d: float) -> float: return float(os.environ.get(name, d))
def _i(name: str, d: int) -> int: return int(os.environ.get(name, d))
def _b(name: str, d: bool) -> bool:
    return os.environ.get(name, str(d)).strip().lower() in ("1", "true", "yes", "on")


# ============================ MODE + DATA =================================
TRADING_MODE = os.environ.get("TRADING_MODE", "PAPER").upper()   # PAPER | LIVE
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

# Paid tier: 150 calls/min. Budget under it.
RATE_LIMIT_CALLS = _i("RATE_LIMIT_CALLS", 140)
RATE_LIMIT_WINDOW_SECONDS = 60

# Seconds between trading cycles in --loop mode.
SCAN_INTERVAL_SECS = _i("SCAN_INTERVAL_SECS", 5)

DAILY_RESOLUTION = "D"                       # swing system
INTRADAY_RESOLUTION = os.environ.get("INTRADAY_RESOLUTION", "1")  # 1-min
DAILY_LOOKBACK_DAYS = _i("DAILY_LOOKBACK_DAYS", 120)
INTRADAY_LOOKBACK_MIN = _i("INTRADAY_LOOKBACK_MIN", 240)

# ============================ EXECUTION (BROKER) =========================
# "paper"  -> internal PaperBroker (simulated fills on real prices). Default.
# "alpaca" -> AlpacaBroker (real order API; paper or live per URL).
BROKER = os.environ.get("BROKER", "paper").lower()

ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "")
ALPACA_PAPER = _b("ALPACA_PAPER", True)   # paper endpoint unless explicitly false

# THE REAL-MONEY GATE. To trade live you must set ALL of:
#   TRADING_MODE=LIVE, BROKER=alpaca, ALPACA_PAPER=false,
#   and LIVE_CONFIRM exactly equal to the phrase below.
LIVE_CONFIRM_PHRASE = "I_UNDERSTAND_I_CAN_LOSE_REAL_MONEY"
LIVE_CONFIRM = os.environ.get("LIVE_CONFIRM", "")


def live_money_armed() -> bool:
    return (TRADING_MODE == "LIVE" and BROKER == "alpaca"
            and not ALPACA_PAPER and LIVE_CONFIRM == LIVE_CONFIRM_PHRASE)


# ============================ ACCOUNT + RISK =============================
START_EQUITY = _f("START_EQUITY", 50_000)          # paper accounting only
RISK_PER_TRADE_PCT = _f("RISK_PER_TRADE_PCT", 0.01)
MAX_POSITION_SIZE = _f("MAX_POSITION_SIZE", 3_000)
DAILY_LOSS_LIMIT = _f("DAILY_LOSS_LIMIT", 2_500)

MIN_PRICE = _f("MIN_PRICE", 5)
MIN_DOLLAR_VOL = _f("MIN_DOLLAR_VOL", 5_000_000)

STOP_LOSS_PCT = _f("STOP_LOSS_PCT", 0.05)
TRAIL_PCT = _f("TRAIL_PCT", 0.06)

COMMISSION_PER_TRADE = _f("COMMISSION_PER_TRADE", 0.0)
SLIPPAGE_BPS = _f("SLIPPAGE_BPS", 5)

MARKET_OPEN = os.environ.get("MARKET_OPEN", "09:30")
MARKET_CLOSE = os.environ.get("MARKET_CLOSE", "16:00")
FLATTEN_BEFORE_CLOSE_MIN = _i("FLATTEN_BEFORE_CLOSE_MIN", 5)


# ============================ STRATEGY PARAMS ============================
@dataclass(frozen=True)
class SwingParams:
    atr_stop_multiple: float = _f("SWING_ATR_MULT", 2.5)
    max_positions: int = _i("SWING_MAX_POS", 4)
    breakout_lookback: int = _i("SWING_BREAKOUT_DAYS", 20)
    trend_sma_days: int = _i("TREND_SMA_DAYS", 50)
    require_uptrend: bool = _b("REQUIRE_UPTREND", True)
    vol_spike_mult: float = _f("SWING_VOL_MULT", 1.3)


@dataclass(frozen=True)
class IntradayParams:
    atr_stop_multiple: float = _f("INTRADAY_ATR_MULT", 1.0)
    max_positions: int = _i("INTRADAY_MAX_POS", 4)
    min_rel_volume: float = _f("VOL_SPIKE_MULT", 1.3)
    opening_range_min: int = _i("OPENING_RANGE_MIN", 15)
    require_above_vwap: bool = _b("INTRADAY_REQUIRE_VWAP", True)
    trail_pct: float = TRAIL_PCT


SWING = SwingParams()
INTRADAY = IntradayParams()


# ============================ FEEDS + ROUTING ============================
@dataclass(frozen=True)
class Endpoint:
    key: str
    client_method: str
    criticality: FeedCriticality
    poll_interval_seconds: int


# Pure price action: only quote + candle.
ENDPOINTS: dict[str, Endpoint] = {
    "quote":  Endpoint("quote", "quote", FeedCriticality.PRICE, 5),
    "candle": Endpoint("candle", "stock_candles", FeedCriticality.PRICE, 30),
}

SYSTEM_REQUIRED_FEEDS: dict[System, list[str]] = {
    System.SWING:    ["quote", "candle"],
    System.INTRADAY: ["quote", "candle"],
}

SOURCE_TO_SYSTEM: dict[SignalSource, System] = {
    SignalSource.TREND:    System.SWING,
    SignalSource.MOMENTUM: System.INTRADAY,
}

UNIVERSE = os.environ.get(
    "UNIVERSE",
    "AAPL,MSFT,NVDA,AMD,TSLA,META,AMZN,GOOGL,F,T,HNRG,TPL,IX,KARD,INTC,PLTR",
).split(",")
