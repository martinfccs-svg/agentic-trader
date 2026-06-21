# Agentic Trading Agent v4.1

A 24/7 market scanner (Reddit, StockTwits, STOCK Act, SEC Form 4) with a
measured strategy framework and a built-in paper-trading validator.

## What changed from earlier versions

- **Trailing-stop exits** replace the old fixed +3% / −5% targets. The old
  geometry needed a ~63% win rate just to break even; trailing stops let
  winners run and cut losers, which lowers the win rate you need.
- **Liquidity filter** — only trades names above a price and average
  dollar-volume floor, so spread/slippage doesn't eat the edge.
- **Risk-based sizing** — risks a fixed % of equity per trade (default 1%)
  instead of a flat $3,000, capped at `MAX_POSITION_SIZE`.
- **No fabricated confidence** — if a data source fails, the agent drops that
  term or skips the trade instead of inventing a neutral 50%.
- **Paper mode by default** — see below.

## Two modes

| Mode  | What it does                                                        | Money at risk |
|-------|---------------------------------------------------------------------|---------------|
| PAPER | Logs hypothetical, cost-adjusted fills and prints a scorecard       | $0            |
| LIVE  | Sends a phone alert; you confirm each trade in chat (Robinhood MCP) | Real          |

**Start in PAPER.** Let it run for a few weeks. Read the scorecard. Only switch
to `TRADING_MODE=LIVE` if the paper results show a real edge after costs. If
expectancy is at or below zero, the system is telling you not to fund it.

## Files

| File              | Purpose                                              |
|-------------------|------------------------------------------------------|
| `trader.py`       | The agent (scanners + strategy + paper/live modes)   |
| `strategy_lab.py` | Backtest engine, cost model, sizing, and scorecard   |
| `requirements.txt`| Python dependencies                                  |
| `Dockerfile`      | Build/run for Railway (defaults to PAPER mode)        |
| `railway.toml`    | Deployment region + restart policy                   |

## Deploy on Railway

1. Push these files to a GitHub repo.
2. In Railway: New Project → Deploy from repo → it uses the Dockerfile.
3. Set environment variables (Variables tab) — all optional; defaults shown:

```
TRADING_MODE=PAPER          # PAPER (default) or LIVE
STOP_LOSS_PCT=0.05          # initial stop distance
TRAIL_PCT=0.06              # trailing stop distance from the high
RISK_PER_TRADE_PCT=0.01     # risk 1% of equity per trade
MAX_POSITION_SIZE=3000      # $ cap per position
START_EQUITY=50000          # base for paper sizing
MIN_PRICE=5                 # liquidity floor
MIN_DOLLAR_VOL=5000000      # avg daily $ volume floor
REQUIRE_UPTREND=true        # v4.1: only buy stocks above their short avg (no falling knives)
TREND_SMA_DAYS=10           # v4.1: the short average used for the trend check
REQUIRE_VOLUME_SPIKE=true   # v4.1: only buy when today's volume beats its average
VOL_SPIKE_MULT=1.3          # v4.1: how big the volume spike must be (1.3x)
MIN_CONFIRMATIONS=1         # v4.1: # of distinct sources that must agree before buying
MIN_CONFIDENCE=70
SOCIAL_WEIGHT=0.40
DAILY_LOSS_LIMIT=2500
NTFY_TOPIC=your-topic       # for LIVE-mode phone alerts (install the ntfy app)
```

4. Watch the Logs tab. In PAPER mode you'll see hypothetical fills and, after a
   few closed trades, a scorecard with win rate, profit factor, expectancy,
   and max drawdown.

## Measure things yourself

```
python strategy_lab.py --selftest                 # verify the engine math
python strategy_lab.py --backtest AAPL MSFT NVDA  # backtest the exit machinery
python strategy_lab.py --sweep AAPL MSFT NVDA     # test many stop/trail/hold combos
```

The **sweep** is the most useful check: it runs the backtest across a grid of
stop / trailing / hold settings and ranks them. Don't cherry-pick the top row —
read whether the result is *positive across most settings* (a real edge is
stable) or *good in one cell and negative next to it* (that's noise / curve-fit).

## Honest limits

- The **social signals have no proven edge.** This package gives you the tools
  to find out; it does not assume the answer is yes.
- Backtesting validates the **exit/risk/cost machinery** on real prices. The
  signals themselves can only be validated **forward**, in PAPER mode, because
  historical social-media snapshots aren't freely available.
- PAPER P&L is an estimate (fills at observed prices + a modeled cost). Your
  brokerage account is the real source of truth.
- In LIVE mode, trades are **not** auto-executed — you confirm each one in chat.
  A trailing stop is a trigger, not a guaranteed fill price; gaps can exceed it.

Not financial advice. You are responsible for every trade you place.
