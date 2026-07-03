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
# Position cap: percentage of CURRENT equity, so it scales and never goes stale.
# (The old flat $3,000 was sized for a $50k account; at $100k it silently became
# 2x tighter and was binding on every trade, overriding risk-based sizing.)
MAX_POSITION_PCT = _f("MAX_POSITION_PCT", 0.10)     # 10% of equity per position
# Legacy override: if MAX_POSITION_SIZE is set explicitly in env, it still wins.
MAX_POSITION_SIZE = _f("MAX_POSITION_SIZE", 0)      # 0 = use MAX_POSITION_PCT


def max_position_dollars(equity: float) -> float:
    """The dollar cap for one position. Env MAX_POSITION_SIZE (if >0) wins;
    otherwise a percentage of current equity."""
    if MAX_POSITION_SIZE > 0:
        return MAX_POSITION_SIZE
    return equity * MAX_POSITION_PCT
# Daily loss halt: percentage of CURRENT equity (flat dollars go stale as the
# cap did). Legacy override: set DAILY_LOSS_LIMIT explicitly and it wins.
DAILY_LOSS_PCT = _f("DAILY_LOSS_PCT", 0.025)        # 2.5% of equity
DAILY_LOSS_LIMIT = _f("DAILY_LOSS_LIMIT", 0)        # 0 = use DAILY_LOSS_PCT


def daily_loss_dollars(equity: float) -> float:
    """Dollar halt threshold. Env DAILY_LOSS_LIMIT (if >0) wins; else % of equity."""
    if DAILY_LOSS_LIMIT > 0:
        return DAILY_LOSS_LIMIT
    return equity * DAILY_LOSS_PCT

MIN_PRICE = _f("MIN_PRICE", 5)
MIN_DOLLAR_VOL = _f("MIN_DOLLAR_VOL", 5_000_000)

STOP_LOSS_PCT = _f("STOP_LOSS_PCT", 0.05)
TRAIL_PCT = _f("TRAIL_PCT", 0.06)

COMMISSION_PER_TRADE = _f("COMMISSION_PER_TRADE", 0.0)
SLIPPAGE_BPS = _f("SLIPPAGE_BPS", 5)

# Execution tightening (Alpaca):
USE_BRACKET_ORDERS = _b("USE_BRACKET_ORDERS", True)   # broker-side stop + target
MAX_SLIPPAGE_BPS = _f("MAX_SLIPPAGE_BPS", 10)         # marketable-limit cap
TAKE_PROFIT_R = _f("TAKE_PROFIT_R", 3.0)              # target = entry + R x risk/share

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
    atr_stop_multiple: float = _f("INTRADAY_ATR_MULT", 2.5)  # was 1.0: stops were 0.1-0.2% of price -> churn
    max_positions: int = _i("INTRADAY_MAX_POS", 4)
    min_rel_volume: float = _f("VOL_SPIKE_MULT", 1.3)
    opening_range_min: int = _i("OPENING_RANGE_MIN", 15)
    require_above_vwap: bool = _b("INTRADAY_REQUIRE_VWAP", True)
    trail_pct: float = _f("INTRADAY_TRAIL_PCT", 0.01)  # was 6% (15x wider than the stop -> never engaged); 1% matches intraday scale


SWING = SwingParams()
INTRADAY = IntradayParams()

@dataclass(frozen=True)
class MeanRevParams:
    rsi_period: int = _i("MR_RSI_PERIOD", 14)
    rsi_oversold: float = _f("MR_RSI_OVERSOLD", 30)      # buy below this
    rsi_exit: float = _f("MR_RSI_EXIT", 50)              # exit above this
    trend_sma_days: int = _i("MR_TREND_SMA", 200)        # only buy dips in uptrends
    atr_stop_multiple: float = _f("MR_ATR_MULT", 2.0)
    max_positions: int = _i("MR_MAX_POS", 4)


@dataclass(frozen=True)
class XSectParams:
    lookback_days: int = _i("XS_LOOKBACK", 126)          # ~6 months
    skip_days: int = _i("XS_SKIP", 5)                    # skip most-recent week
    top_n: int = _i("XS_TOP_N", 3)                       # hold top N of universe
    rebalance_cycles: int = _i("XS_REBAL_CYCLES", 780)   # ~daily at 30s cycles (was 50 = ~25min; 6-month-lookback ranking should not churn intraday)
    atr_stop_multiple: float = _f("XS_ATR_MULT", 3.0)    # wide protective stop


MEANREV = MeanRevParams()
XSECT = XSectParams()




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
    System.MEANREV:  ["quote", "candle"],
    System.XSECTMOM: ["quote", "candle"],
}

SOURCE_TO_SYSTEM: dict[SignalSource, System] = {
    SignalSource.TREND:          System.SWING,
    SignalSource.MOMENTUM:       System.INTRADAY,
    SignalSource.MEAN_REVERSION: System.MEANREV,
    SignalSource.REL_STRENGTH:   System.XSECTMOM,
}

# ---------------------------------------------------------------------------
# Universe: ~36 liquid large-caps across 8 sectors. The old 16-name list was
# almost all mega-cap tech -- one correlated pond, which undermined strategy
# diversification and made the cross-sectional top-3 ranking nearly meaningless.
# Override with env UNIVERSE / INTRADAY_UNIVERSE (comma-separated).
# ---------------------------------------------------------------------------
UNIVERSE = os.environ.get(
    "UNIVERSE",
    # tech / semis
    "AAPL,MSFT,NVDA,AMD,AVGO,CRM,INTC,PLTR,"
    # consumer / retail
    "AMZN,TSLA,WMT,COST,HD,MCD,NKE,DIS,"
    # communication / media
    "GOOGL,META,NFLX,T,"
    # financials
    "JPM,BAC,GS,V,MA,"
    # healthcare
    "UNH,JNJ,LLY,PFE,"
    # energy
    "XOM,CVX,COP,"
    # industrials
    "CAT,BA,GE,UPS,"
    # defense
    "LMT,RTX,NOC,GD,LHX,HII,TDG,HWM,"
    # utilities
    "NEE,DUK,SO,"
    # consumer staples
    "PG,KO,PEP,"
    # materials (incl. gold)
    "LIN,FCX,NEM,"
    # REITs
    "PLD,AMT,"
    # logistics / transports
    "FDX,UNP,"
    # emerging tech: AI infrastructure, quantum, drones
    "SMCI,ARM,MU,IONQ,AVAV,KTOS",
).split(",")

# Intraday scans a LIQUID SUBSET every cycle (1-min data is the expensive,
# per-cycle cost); daily strategies scan the FULL universe on a slow refresh.
INTRADAY_UNIVERSE = os.environ.get(
    "INTRADAY_UNIVERSE",
    "AAPL,MSFT,NVDA,AMD,AMZN,TSLA,META,GOOGL,NFLX,AVGO,PLTR,JPM",
).split(",")

# Daily bars don't change during the session: refresh them every N cycles
# instead of every cycle (30 cycles ~= 15 min at 30s cycles). This is what
# keeps the wider universe inside the Finnhub rate budget.
DAILY_BARS_REFRESH_CYCLES = _i("DAILY_BARS_REFRESH_CYCLES", 30)
