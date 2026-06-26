# Agentic Trading Agent v6 — live data, pure price action

Runs on **real Finnhub market data**, generates **pure price-action** signals
(no social/insider/congressional), and executes through a **broker** — paper by
default, real money only behind explicit gates.

## Important: Finnhub is data, not a broker

Finnhub provides quotes/candles/ticks. It **cannot place trades**. Execution
goes through a brokerage API. This build uses **Alpaca** (official trading API,
clean paper/live switch). Substitute another broker by writing an adapter with
the same interface as `brokers.PaperBroker`.

## Two systems, both price-action

- **Swing (TREND)** — daily candles: close breaks the prior 20-day high, above
  the 50-day SMA, with a volume expansion. Wide ATR stop (2.5x), multi-day hold,
  no EOD flatten.
- **Intraday (MOMENTUM)** — 1-min candles: relative-volume spike + above VWAP +
  opening-range break. Tight ATR stop (1x), % trailing exit, hard EOD flatten.

`scanner.py` produces the signals; the engines add liquidity/sizing/stop and
execute through whichever broker is wired.

## Run

    python selftest.py     # 14 checks: indicators, sizing, P&L reconciliation, live-gate
    python main.py         # one pass (+ scorecard). Sim data if no key.
    python main.py --loop  # continuous (deploy shape)

With no FINNHUB_API_KEY it uses a simulator so it runs anywhere. With your paid
key it uses live data - after you confirm endpoints:

    export FINNHUB_API_KEY=...
    pip install finnhub-python
    python verify_endpoints.py     # confirms quote + daily + 1-min candles return data

## Going from paper to real money (read this)

This strategy has NO validated track record. The right path: run on live data in
PAPER for weeks, confirm positive cost-adjusted expectancy per system, THEN
consider real money. The defaults keep you in paper.

Real orders require ALL of these - any one missing and the broker refuses to
place a real-money order:

| Variable | Paper (default) | Real money |
|---|---|---|
| TRADING_MODE | PAPER | LIVE |
| BROKER | paper | alpaca |
| ALPACA_PAPER | true | false |
| LIVE_CONFIRM | (unset) | I_UNDERSTAND_I_CAN_LOSE_REAL_MONEY |
| ALPACA_API_KEY / ALPACA_SECRET_KEY | - | your Alpaca keys |

Even then, hard guards stay on: daily loss limit halts new entries, position
size is capped, entries are gated to market hours, intraday flattens near the
close and on shutdown.

## Data -> execution split

| Concern | Provider |
|---|---|
| Quotes, daily candles, 1-min candles | Finnhub (paid) |
| Order execution, account, positions | Alpaca (paper or live) |

## Files

config.py (params, gates, universe) · models.py · indicators.py · scanner.py
(price-action signals) · feed_layer.py (Finnhub + simulated) · brokers.py
(PaperBroker + AlpacaBroker) · safety.py (hours, live banner) · risk.py (sizing)
· router.py · swing_engine.py · intraday_engine.py · kill_switch.py ·
trade_logger.py (per-system scorecard) · verify_endpoints.py · selftest.py ·
main.py.

## What's tested vs. needs your keys

Tested here (selftest + a full sim session): all indicators, sizing, the paper
broker's P&L reconciliation, the scanner, the kill switch, and the live-money
gate (disarmed by default). Needs your credentials to verify against the real
APIs: the live Finnhub response shapes (run verify_endpoints.py) and the Alpaca
order calls in brokers.AlpacaBroker (test on the Alpaca PAPER endpoint first,
ALPACA_PAPER=true, before ever arming live).

Not financial advice. You are responsible for every order placed. Verify the
Alpaca and Finnhub integrations against their current docs before trusting them.
