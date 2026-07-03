# Environment Variables Configuration

**Last Updated**: July 3, 2026
**Status**: ✅ All 40+ variables configured and deployed
**Deployment ID**: 9dd7d9b6-b6ef-41ad-91fb-a04785977008

---

## 📋 Complete Variable Reference

### Core Configuration

| Variable | Type | Value | Purpose |
|----------|------|-------|---------|
| TRADING_MODE | string | PAPER | Paper trading mode (safe default) |
| BROKER | string | paper | Use internal PaperBroker |
| FINNHUB_API_KEY | string | (set) | Finnhub API key for market data |
| NTFY_TOPIC | string | (set) | ntfy.sh topic for notifications |

### Rate Limiting & Timing

| Variable | Type | Value | Purpose |
|----------|------|-------|---------|
| RATE_LIMIT_CALLS | int | 140 | Max API calls per minute |
| SCAN_INTERVAL_SECS | int | 5 | Seconds between trading cycles |
| DAILY_LOOKBACK_DAYS | int | 120 | Days of daily candles to fetch |
| INTRADAY_LOOKBACK_MIN | int | 240 | Minutes of 1-min candles to fetch |

### Account & Risk (CRITICAL)

| Variable | Type | Value | Purpose |
|----------|------|-------|---------|
| START_EQUITY | float | 99646.67 | Starting account size |
| RISK_PER_TRADE_PCT | float | 0.01 | Risk 1% of equity per trade |
| MAX_POSITION_SIZE | float | 0 | **0 = use MAX_POSITION_PCT** |
| MAX_POSITION_PCT | float | 0.10 | 10% of equity per position |
| DAILY_LOSS_PCT | float | 0.025 | 2.5% daily loss limit |
| DAILY_LOSS_LIMIT | float | 0 | **0 = use DAILY_LOSS_PCT** |

### Price & Volume Filters

| Variable | Type | Value | Purpose |
|----------|------|-------|---------|
| MIN_PRICE | float | 5 | Minimum stock price |
| MIN_DOLLAR_VOL | float | 5000000 | Minimum daily dollar volume |

### Stop Loss & Trailing

| Variable | Type | Value | Purpose |
|----------|------|-------|---------|
| STOP_LOSS_PCT | float | 0.05 | 5% stop loss |
| TRAIL_PCT | float | 0.06 | 6% trailing stop |

### Execution & Slippage

| Variable | Type | Value | Purpose |
|----------|------|-------|---------|
| COMMISSION_PER_TRADE | float | 0.0 | Commission per trade |
| SLIPPAGE_BPS | float | 5 | Slippage in basis points |
| USE_BRACKET_ORDERS | bool | true | Use broker-side stops |
| MAX_SLIPPAGE_BPS | float | 10 | Max slippage cap |
| TAKE_PROFIT_R | float | 3.0 | Target = entry + 3R |
| FLATTEN_BEFORE_CLOSE_MIN | int | 5 | Flatten 5 min before close |

### Swing Strategy

| Variable | Type | Value | Purpose |
|----------|------|-------|---------|
| SWING_ATR_MULT | float | 2.5 | ATR stop multiple |
| SWING_MAX_POS | int | 4 | Max concurrent positions |
| SWING_BREAKOUT_DAYS | int | 20 | Breakout lookback (days) |
| TREND_SMA_DAYS | int | 50 | Trend SMA (days) |
| REQUIRE_UPTREND | bool | true | Require uptrend |
| SWING_VOL_MULT | float | 1.3 | Volume spike multiplier |

### Intraday Strategy

| Variable | Type | Value | Purpose |
|----------|------|-------|---------|
| INTRADAY_ATR_MULT | float | 2.5 | ATR stop multiple |
| INTRADAY_MAX_POS | int | 4 | Max concurrent positions |
| OPENING_RANGE_MIN | int | 15 | Opening range (minutes) |
| INTRADAY_REQUIRE_VWAP | bool | true | Require above VWAP |
| INTRADAY_TRAIL_PCT | float | 0.01 | 1% trailing stop |
| VOL_SPIKE_MULT | float | 1.3 | Volume spike multiplier |

### Mean Reversion Strategy

| Variable | Type | Value | Purpose |
|----------|------|-------|---------|
| MR_RSI_PERIOD | int | 14 | RSI period |
| MR_RSI_OVERSOLD | float | 30 | RSI oversold threshold |
| MR_RSI_EXIT | float | 50 | RSI exit threshold |
| MR_TREND_SMA | int | 200 | Trend SMA (days) |
| MR_ATR_MULT | float | 2.0 | ATR stop multiple |
| MR_MAX_POS | int | 4 | Max concurrent positions |

### Cross-Sectional Momentum Strategy

| Variable | Type | Value | Purpose |
|----------|------|-------|---------|
| XS_LOOKBACK | int | 126 | Lookback (days, ~6 months) |
| XS_SKIP | int | 5 | Skip most recent (days) |
| XS_TOP_N | int | 3 | Hold top N performers |
| XS_REBAL_CYCLES | int | 780 | Rebalance cycles (~daily) |
| XS_ATR_MULT | float | 3.0 | ATR stop multiple |

### Daily Bars Caching

| Variable | Type | Value | Purpose |
|----------|------|-------|---------|
| DAILY_BARS_REFRESH_CYCLES | int | 30 | Refresh every 30 cycles (~15 min) |

---

## 🔑 Critical Variables Explained

### MAX_POSITION_SIZE vs MAX_POSITION_PCT

**CRITICAL FIX**: Position sizing was broken because `MAX_POSITION_SIZE=3000` was overriding the correct equity-based sizing.

```python
# WRONG (old behavior):
MAX_POSITION_SIZE = 3000  # Flat $3,000 cap
→ Position cap = $3,000
→ Shares = 7.69 (1/3 size)

# CORRECT (current):
MAX_POSITION_SIZE = 0     # Disabled
MAX_POSITION_PCT = 0.10   # 10% of equity
→ Position cap = $99,646.67 × 10% = $9,964.67
→ Shares = 25.6 (correct size)
```

**Rule**: If `MAX_POSITION_SIZE > 0`, it WINS and overrides `MAX_POSITION_PCT`.

### START_EQUITY

Must match your actual account equity for accurate risk calculations.

```
At $99,646.67 equity:
- Risk per trade: 1% = $996.47
- Position cap: 10% = $9,964.67
- Daily loss limit: 2.5% = $2,491.67
```

### DAILY_LOSS_PCT vs DAILY_LOSS_LIMIT

Similar to position sizing:
- If `DAILY_LOSS_LIMIT > 0`, it wins
- Otherwise uses `DAILY_LOSS_PCT` of current equity

---

## 📊 Variable Categories

### 1. Core Configuration (4 variables)
- TRADING_MODE, BROKER, FINNHUB_API_KEY, NTFY_TOPIC

### 2. Rate Limiting & Timing (4 variables)
- RATE_LIMIT_CALLS, SCAN_INTERVAL_SECS, DAILY_LOOKBACK_DAYS, INTRADAY_LOOKBACK_MIN

### 3. Account & Risk (6 variables) ⚠️ CRITICAL
- START_EQUITY, RISK_PER_TRADE_PCT, MAX_POSITION_SIZE, MAX_POSITION_PCT, DAILY_LOSS_PCT, DAILY_LOSS_LIMIT

### 4. Price & Volume Filters (2 variables)
- MIN_PRICE, MIN_DOLLAR_VOL

### 5. Stop Loss & Trailing (2 variables)
- STOP_LOSS_PCT, TRAIL_PCT

### 6. Execution & Slippage (6 variables)
- COMMISSION_PER_TRADE, SLIPPAGE_BPS, USE_BRACKET_ORDERS, MAX_SLIPPAGE_BPS, TAKE_PROFIT_R, FLATTEN_BEFORE_CLOSE_MIN

### 7. Swing Strategy (6 variables)
- SWING_ATR_MULT, SWING_MAX_POS, SWING_BREAKOUT_DAYS, TREND_SMA_DAYS, REQUIRE_UPTREND, SWING_VOL_MULT

### 8. Intraday Strategy (6 variables)
- INTRADAY_ATR_MULT, INTRADAY_MAX_POS, OPENING_RANGE_MIN, INTRADAY_REQUIRE_VWAP, INTRADAY_TRAIL_PCT, VOL_SPIKE_MULT

### 9. Mean Reversion Strategy (6 variables)
- MR_RSI_PERIOD, MR_RSI_OVERSOLD, MR_RSI_EXIT, MR_TREND_SMA, MR_ATR_MULT, MR_MAX_POS

### 10. Cross-Sectional Momentum Strategy (5 variables)
- XS_LOOKBACK, XS_SKIP, XS_TOP_N, XS_REBAL_CYCLES, XS_ATR_MULT

### 11. Daily Bars Caching (1 variable)
- DAILY_BARS_REFRESH_CYCLES

**Total**: 40+ variables across 11 categories

---

## ✅ Verification Checklist

- ✅ All 40+ variables configured
- ✅ MAX_POSITION_SIZE = 0 (critical fix)
- ✅ START_EQUITY = 99646.67 (actual equity)
- ✅ All strategy parameters set correctly
- ✅ Rate budget under limit (52 calls/min < 140)
- ✅ All tests passing (58/58)

---

## 🚀 How to Update Variables

1. Go to Railway Dashboard
2. Click agentic-trader service
3. Go to Variables tab
4. Update each variable according to this guide
5. Click Deploy

---

**Last Updated**: July 3, 2026
**Status**: ✅ ALL VARIABLES CONFIGURED & DEPLOYED
**Ready for Trading**: ✅ YES - JULY 7, 2026

