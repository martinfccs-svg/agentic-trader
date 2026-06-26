# Agentic Trading Agent v5 — two-system rebuild

A market scanner + paper-trading validator, rebuilt as **two coherent systems**
on a single data backbone. This replaces the v4.1 single-blend design, where
mismatched signal horizons and one shared stop produced an incoherent hybrid.

- **Swing system** — insider (Form 3/4/5) + congressional (STOCK Act) signals.
  Wide ATR-based stop (2.5× ATR), risk-based sizing, liquidity + freshness +
  uptrend filters, multi-day hold, **no end-of-day flatten**.
- **Intraday momentum system** — social buzz as a **watchlist filter only**; a
  live price-momentum **confirmation gate** (relative-volume spike + price above
  VWAP) is the actual entry trigger. Tight ATR stop (1× ATR), percentage
  trailing exit, **hard EOD flatten**.

The two engines share nothing but the feed layer, the broker, and the logger.

## Runs out of the box

With **no API key**, it uses a deterministic `SimulatedFeed` so the whole
machine runs and is testable with zero dependencies:

```
python selftest.py     # verify indicators, sizing, and P&L reconciliation
python main.py         # one paper session against the simulator -> scorecard
python main.py --loop  # continuous (deployment shape)
```

With a key, it uses live Finnhub data — **after** you confirm access:

```
export FINNHUB_API_KEY=...
pip install finnhub-python
python verify_endpoints.py     # STEP ZERO: which endpoints does your tier allow?
python main.py
```

## The one remaining integration step

Everything runs today against the simulator. The single thing that needs your
key is confirming the live Finnhub **response shapes**. `feed_layer.FinnhubFeed`
parses against Finnhub's documented fields, but the free/premium split and exact
field names vary by tier — `verify_endpoints.py` tells you what you actually
have, and `config.ENDPOINTS` is the one place to fix method names.

## What's real (tested in selftest.py)

ATR / VWAP / SMA / relative-volume math; risk-based sizing with caps; the paper
broker's cost-adjusted fills; and **realized-vs-unrealized P&L reconciliation**
— realized moves only on close, unrealized is a separate mark, never conflated.
This is the structural fix for the v4.1 bug where total equity hid realized
losses. The circuit breaker, health surface, kill-switch severity, and router
mapping are exercised by `main.py` every run.

## Files

| File | Role |
|------|------|
| `config.py` | All params (v4.1 env-var names preserved), endpoint registry, system→feed map. |
| `models.py` | Typed objects + enums. |
| `indicators.py` | ATR, VWAP, SMA, relative/dollar volume. |
| `feed_layer.py` | Rate limiter, circuit breaker, health; `FinnhubFeed` (live) + `SimulatedFeed`. |
| `paper_broker.py` | Cost-adjusted fills, positions, realized/unrealized P&L, equity. |
| `risk.py` | Risk-based position sizing. |
| `router.py` | Source → one system. |
| `swing_engine.py` / `intraday_engine.py` | The two risk models. |
| `kill_switch.py` | Signal-down → halt entries; price-down → emergency flatten; daily loss limit. |
| `trade_logger.py` | Signal funnel + per-system scorecard (win rate, PF, expectancy, max DD). |
| `verify_endpoints.py` | Step-zero access probe. |
| `selftest.py` | Engine-math checks; Docker startup gates on it. |
| `main.py` | Wiring + run loop. |

## Modes

| Mode | Behaviour | Money |
|------|-----------|-------|
| PAPER (default) | Cost-adjusted simulated fills + scorecard | $0 |
| LIVE | Intentionally not auto-executing — `paper_broker.buy` raises so you wire a confirmed-execution path (e.g. Robinhood MCP + phone confirm) | Real |

**Start in PAPER. Let it run for weeks. Only go LIVE if expectancy is positive
after costs.** Three trades is noise; read the scorecard over dozens.

Not financial advice. You are responsible for every trade you place.
