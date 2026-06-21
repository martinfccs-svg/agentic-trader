#!/usr/bin/env python3
"""
strategy_lab.py — Measure before you risk money
================================================

Purpose
-------
This tool answers the only question that matters before funding the agent:
"Does this approach actually make money after real-world costs?"

It does TWO honest jobs, because they need different evidence:

  1. BACKTEST (runnable today) — tests the exit / risk / cost machinery on
     real historical prices. It validates the parts that ARE testable:
       - trailing-stop geometry (so winners run, losers are cut)
       - realistic transaction costs (spread + slippage)
       - liquidity filtering (don't trade names where costs eat the edge)
       - risk-based position sizing (fixed fractional risk, not flat $)
     Entries are pluggable. The included demo uses a transparent momentum
     rule so you can see the harness produce real numbers — it is NOT a
     claim that momentum is profitable, just a testable example.

  2. PAPER-TRADE (run forward) — the social signals (Reddit, StockTwits,
     STOCK Act, Form 4) CANNOT be backtested for free: historical social
     snapshots aren't available. The only honest way to measure them is to
     run the live signal logic forward with ZERO real money and log the
     hypothetical fills. Point this at your trader.py scanners (see
     paper_trade_step) and let it accumulate a track record for a few weeks.

What this does NOT do
---------------------
  - It does not place real orders. Ever.
  - It does not prove the social signals work. It gives you the apparatus
    to find out. If the paper-trade shows no edge after costs, the
    profitable decision is to not deploy.

Usage
-----
  python strategy_lab.py --selftest          # verify the engine math (no network)
  python strategy_lab.py --backtest AAPL MSFT NVDA --days 730
  python strategy_lab.py --papertrade         # forward sim scaffold

Not financial advice. You are responsible for any trade you place.
"""

import argparse
import math
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, List, Dict, Callable

try:
    import requests
except ImportError:
    requests = None


# ─────────────────────────────────────────────────────────────────────────────
# Cost model — the thing a 3% target quietly loses to
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class CostModel:
    """Round-trip frictions as a fraction of price, applied per side.
    0.0015 one-way ≈ 0.30% round trip — reasonable for a LIQUID large/mid cap.
    Thin small-caps are far worse, which is exactly why we filter for liquidity."""
    one_way_pct: float = 0.0015

    def buy_fill(self, price: float) -> float:
        return price * (1 + self.one_way_pct)   # you pay a bit more than mid

    def sell_fill(self, price: float) -> float:
        return price * (1 - self.one_way_pct)   # you receive a bit less than mid


# ─────────────────────────────────────────────────────────────────────────────
# Risk config — the recommendations, made explicit and tunable
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class RiskConfig:
    init_stop_pct: float = 0.05          # initial protective stop below entry
    trail_pct: float = 0.06              # trailing stop distance from the high
    take_profit_pct: Optional[float] = None   # None => let trailing stop run (better geometry)
    max_hold_days: int = 15              # exit if neither stop nor target hits
    risk_per_trade_pct: float = 0.01     # risk 1% of equity per trade
    # Liquidity filter (skip names where costs would eat the edge):
    min_price: float = 5.0
    min_avg_dollar_vol: float = 5_000_000.0
    avg_vol_lookback: int = 20


@dataclass
class Trade:
    ticker: str
    entry_date: str
    exit_date: str
    entry: float
    exit: float
    shares: float
    ret_pct: float        # net of costs
    pnl: float            # net of costs, in dollars
    reason: str
    held_days: int


# ─────────────────────────────────────────────────────────────────────────────
# Data
# ─────────────────────────────────────────────────────────────────────────────
def fetch_history(ticker: str, days: int = 730) -> List[Dict]:
    """Daily OHLCV bars from Yahoo's unofficial chart endpoint.
    Returns list of {date, o, h, l, c, v} ascending. [] on failure."""
    if requests is None:
        raise RuntimeError("requests not installed: pip install requests")
    rng = "2y" if days <= 730 else "5y"
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/"
           f"{ticker}?interval=1d&range={rng}")
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        if r.status_code != 200:
            return []
        res = r.json()["chart"]["result"][0]
        ts = res["timestamp"]
        q = res["indicators"]["quote"][0]
        bars = []
        for i, t in enumerate(ts):
            o, h, l, c, v = q["open"][i], q["high"][i], q["low"][i], q["close"][i], q["volume"][i]
            if None in (o, h, l, c):
                continue
            bars.append({
                "date": datetime.utcfromtimestamp(t).strftime("%Y-%m-%d"),
                "o": float(o), "h": float(h), "l": float(l), "c": float(c),
                "v": float(v or 0),
            })
        return bars[-days:]
    except Exception as e:
        print(f"  fetch {ticker} failed: {e}", file=sys.stderr)
        return []


def liquidity_ok(bars: List[Dict], idx: int, cfg: RiskConfig) -> bool:
    """Is the name liquid enough at bar `idx` that costs won't dominate?"""
    if bars[idx]["c"] < cfg.min_price:
        return False
    lo = max(0, idx - cfg.avg_vol_lookback)
    window = bars[lo:idx + 1]
    if not window:
        return False
    avg_dollar_vol = sum(b["c"] * b["v"] for b in window) / len(window)
    return avg_dollar_vol >= cfg.min_avg_dollar_vol


# ─────────────────────────────────────────────────────────────────────────────
# Core: simulate one trade with trailing-stop exits + costs + risk sizing
# ─────────────────────────────────────────────────────────────────────────────
def simulate_trade(bars: List[Dict], entry_idx: int, equity: float,
                   cfg: RiskConfig, cost: CostModel) -> Optional[Trade]:
    """Enter at the OPEN of bar entry_idx (signal fired on prior close -> no
    lookahead). Walk forward applying a trailing stop, optional take-profit,
    and a max-hold timeout. Returns a Trade or None if it can't be sized."""
    if entry_idx >= len(bars):
        return None

    entry_raw = bars[entry_idx]["o"]
    entry = cost.buy_fill(entry_raw)
    init_stop = entry_raw * (1 - cfg.init_stop_pct)
    per_share_risk = entry - init_stop
    if per_share_risk <= 0:
        return None

    # Fixed-fractional risk sizing, capped at no-leverage
    risk_dollars = equity * cfg.risk_per_trade_pct
    shares = risk_dollars / per_share_risk
    shares = min(shares, equity / entry)
    if shares <= 0:
        return None

    highest = entry_raw
    for j in range(entry_idx + 1, len(bars)):
        bar = bars[j]
        highest = max(highest, bar["h"])
        stop = max(init_stop, highest * (1 - cfg.trail_pct))

        # Stop hit? (gap-down below stop fills at the open, not the stop)
        if bar["l"] <= stop:
            exit_raw = min(bar["o"], stop)
            return _close(bars, entry_idx, j, entry, cost.sell_fill(exit_raw),
                          shares, "trail/stop")

        # Optional hard take-profit
        if cfg.take_profit_pct is not None:
            tp = entry_raw * (1 + cfg.take_profit_pct)
            if bar["h"] >= tp:
                exit_raw = max(bar["o"], tp)  # gap-up fills better
                return _close(bars, entry_idx, j, entry, cost.sell_fill(exit_raw),
                              shares, "take-profit")

        # Max hold timeout -> exit at close
        if (j - entry_idx) >= cfg.max_hold_days:
            return _close(bars, entry_idx, j, entry, cost.sell_fill(bar["c"]),
                          shares, "time-exit")

    # Ran out of data -> exit at last close
    last = len(bars) - 1
    return _close(bars, entry_idx, last, entry, cost.sell_fill(bars[last]["c"]),
                  shares, "data-end")


def _close(bars, entry_idx, exit_idx, entry_eff, exit_eff, shares, reason) -> Trade:
    pnl = (exit_eff - entry_eff) * shares
    ret = (exit_eff - entry_eff) / entry_eff
    return Trade(
        ticker="", entry_date=bars[entry_idx]["date"], exit_date=bars[exit_idx]["date"],
        entry=round(entry_eff, 4), exit=round(exit_eff, 4), shares=round(shares, 3),
        ret_pct=ret, pnl=pnl, reason=reason, held_days=exit_idx - entry_idx,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Example entry rule (transparent, pluggable — NOT a claim it's profitable)
# ─────────────────────────────────────────────────────────────────────────────
def momentum_breakout_entries(bars: List[Dict], cfg: RiskConfig,
                              lookback: int = 20) -> List[int]:
    """Signal a long when today's close makes a new `lookback`-day high.
    Returns entry bar indices (you enter next day's open)."""
    idxs = []
    for i in range(lookback, len(bars) - 1):
        window_high = max(b["h"] for b in bars[i - lookback:i])
        if bars[i]["c"] > window_high and liquidity_ok(bars, i, cfg):
            idxs.append(i + 1)   # enter NEXT open
    return idxs


# ─────────────────────────────────────────────────────────────────────────────
# Backtest driver (sequential equity model — documented simplification)
# ─────────────────────────────────────────────────────────────────────────────
def backtest(tickers: List[str], cfg: RiskConfig, cost: CostModel,
             days: int, entry_fn: Callable, start_equity: float = 50_000.0):
    """Sequential, non-overlapping backtest: trades are resolved in exit-date
    order and equity compounds. This is a simplification (a real portfolio
    holds positions concurrently), good for comparing configs and estimating
    expectancy — not a substitute for a full portfolio simulator."""
    all_trades: List[Trade] = []
    for t in tickers:
        bars = fetch_history(t, days)
        if len(bars) < cfg.avg_vol_lookback + 25:
            print(f"  {t}: not enough data, skipping")
            continue
        for ei in entry_fn(bars, cfg):
            tr = simulate_trade(bars, ei, start_equity, cfg, cost)
            if tr:
                tr.ticker = t
                all_trades.append(tr)
        print(f"  {t}: {sum(1 for x in all_trades if x.ticker == t)} trades")

    all_trades.sort(key=lambda x: x.exit_date)
    return all_trades


# ─────────────────────────────────────────────────────────────────────────────
# Config sweep — test many stop/trail/hold combos, expose parameter fragility
# ─────────────────────────────────────────────────────────────────────────────
def sweep(tickers: List[str], cost: CostModel, days: int, entry_fn: Callable,
          init_stops=(0.03, 0.05, 0.08), trails=(0.04, 0.06, 0.10),
          holds=(10, 20), start_equity: float = 50_000.0):
    """Run the backtest across a grid of exit parameters and rank the results.

    The point is NOT to pick the single best number — that's curve-fitting.
    The point is to see whether the edge SURVIVES across nearby settings. A real
    edge is stable; a result that's great at one combo and negative right next
    to it is noise you'd be fooling yourself with."""
    # Fetch each ticker once, reuse across all combos (fair + fast)
    data = {}
    for t in tickers:
        bars = fetch_history(t, days)
        if len(bars) >= 45:
            data[t] = bars
        else:
            print(f"  {t}: not enough data, skipping")
    if not data:
        print("No usable data. Nothing to sweep.")
        return

    rows = []
    for s in init_stops:
        for tr in trails:
            if tr < s:        # a trailing stop tighter than the initial stop is odd; skip
                continue
            for h in holds:
                cfg = RiskConfig(init_stop_pct=s, trail_pct=tr, max_hold_days=h,
                                 risk_per_trade_pct=0.01)
                trades = []
                for tk, bars in data.items():
                    for ei in entry_fn(bars, cfg):
                        x = simulate_trade(bars, ei, start_equity, cfg, cost)
                        if x:
                            x.ticker = tk
                            trades.append(x)
                m = compute_metrics(trades, cfg, start_equity)
                if m and m["n"] >= 5:        # ignore combos with too few trades
                    rows.append((s, tr, h, m))

    if not rows:
        print("No combo produced enough trades to evaluate.")
        return

    rows.sort(key=lambda r: r[3]["expectancy"], reverse=True)

    print("\n" + "=" * 74)
    print(" CONFIG SWEEP  —  ranked by expectancy per trade (net of costs)")
    print("=" * 74)
    print("  stop  trail  hold | trades  win%   PF    exp%   maxDD   return")
    print("  " + "-" * 68)
    for s, tr, h, m in rows:
        pf = "inf" if m["profit_factor"] == float("inf") else f"{m['profit_factor']:.2f}"
        print(f"  {s*100:>3.0f}%  {tr*100:>3.0f}%  {h:>3d}d | "
              f"{m['n']:>5d}  {m['win_rate']:>4.0f}  {pf:>5}  "
              f"{m['expectancy']:>+5.2f}  {m['max_dd']:>5.1f}%  {m['total_ret']:>+6.1f}%")
    print("=" * 74)

    exps = [m["expectancy"] for *_, m in rows]
    best, worst = max(exps), min(exps)
    positive = sum(1 for e in exps if e > 0)
    print(f"  Combos tested        : {len(rows)}")
    print(f"  Positive expectancy  : {positive}/{len(rows)}")
    print(f"  Expectancy range     : {worst:+.2f}%  ..  {best:+.2f}%  per trade")
    if positive == 0:
        print("  READ: nothing here is profitable after costs. Don't fund it.")
    elif positive < len(rows) * 0.6:
        print("  READ: edge is FRAGILE — profitable only in some settings. Treat as noise.")
    else:
        print("  READ: edge holds across most settings. Promising — validate forward in PAPER.")
    print("  A robust strategy is stable across nearby parameters, not a single lucky cell.")


# ─────────────────────────────────────────────────────────────────────────────
# Performance report — the honest scorecard
# ─────────────────────────────────────────────────────────────────────────────
def compute_metrics(trades: List[Trade], cfg: RiskConfig,
                    start_equity: float = 50_000.0) -> Optional[dict]:
    """Reduce a list of trades to the numbers that decide profitability.
    Returns None if there are no trades."""
    if not trades:
        return None
    wins = [t for t in trades if t.ret_pct > 0]
    losses = [t for t in trades if t.ret_pct <= 0]
    n = len(trades)
    avg_win = (sum(t.ret_pct for t in wins) / len(wins) * 100) if wins else 0.0
    avg_loss = (sum(t.ret_pct for t in losses) / len(losses) * 100) if losses else 0.0
    gross_win = sum(t.ret_pct for t in wins)
    gross_loss = abs(sum(t.ret_pct for t in losses))

    equity = peak = start_equity
    max_dd = 0.0
    for t in trades:
        equity *= (1 + cfg.risk_per_trade_pct * (t.ret_pct / cfg.init_stop_pct))
        peak = max(peak, equity)
        max_dd = max(max_dd, (peak - equity) / peak)

    return {
        "n": n,
        "win_rate": len(wins) / n * 100,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "break_even": (abs(avg_loss) / (avg_win + abs(avg_loss)) * 100)
                      if (avg_win + abs(avg_loss)) else 0.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss else float("inf"),
        "expectancy": sum(t.ret_pct for t in trades) / n * 100,
        "max_dd": max_dd * 100,
        "end_equity": equity,
        "total_ret": (equity - start_equity) / start_equity * 100,
    }


def report(trades: List[Trade], cfg: RiskConfig, start_equity: float = 50_000.0):
    m = compute_metrics(trades, cfg, start_equity)
    if m is None:
        print("\nNo trades generated. Nothing to evaluate.")
        return
    pf = m["profit_factor"]
    print("\n" + "=" * 58)
    print(" STRATEGY SCORECARD  (net of estimated costs)")
    print("=" * 58)
    print(f"  Trades                : {m['n']}")
    print(f"  Win rate              : {m['win_rate']:.1f}%")
    print(f"  Avg win / avg loss    : +{m['avg_win']:.2f}% / {m['avg_loss']:.2f}%")
    print(f"  Break-even win rate   : {m['break_even']:.1f}%   <- you must beat this")
    print(f"  Profit factor         : {pf:.2f}   (>1 = profitable, want >1.3)")
    print(f"  Expectancy / trade    : {m['expectancy']:+.2f}%   <- the number that matters")
    print(f"  Max drawdown          : {m['max_dd']:.1f}%")
    print(f"  Modeled equity        : ${start_equity:,.0f} -> ${m['end_equity']:,.0f} ({m['total_ret']:+.1f}%)")
    print("=" * 58)
    expectancy, profit_factor = m["expectancy"], pf
    if expectancy <= 0:
        print("  VERDICT: negative expectancy after costs. As-is, this loses money.")
    elif profit_factor < 1.3:
        print("  VERDICT: marginal. The edge is thin and fragile to cost assumptions.")
    else:
        print("  VERDICT: positive on this data. Validate forward before funding.")
    print("  Reminder: a clean backtest is necessary, not sufficient. Paper-trade next.")


# ─────────────────────────────────────────────────────────────────────────────
# Forward paper-trade scaffold (validates the LIVE social signals, $0 risk)
# ─────────────────────────────────────────────────────────────────────────────
def paper_trade_step(signal, get_price_fn, cost: CostModel, book: dict, cfg: RiskConfig):
    """Call this from your live loop instead of execute_buy/execute_sell.
    `signal` = {ticker, action, confidence}. Records HYPOTHETICAL fills only.

    To wire it up: import your scanners from trader.py, and where trader.py
    would alert you, call paper_trade_step(...) instead. After a few weeks,
    feed the closed `book` trades into report() to see if the signals had an edge.
    """
    t = signal["ticker"]
    price = get_price_fn(t)
    if not price:
        return  # rec #4: never act on missing data (no fabricated confidence)

    if signal["action"] == "BUY" and t not in book:
        book[t] = {"entry": cost.buy_fill(price), "high": price,
                   "date": datetime.utcnow().strftime("%Y-%m-%d %H:%M")}
    elif t in book:
        book[t]["high"] = max(book[t]["high"], price)
        stop = max(book[t]["entry"] * (1 - cfg.init_stop_pct),
                   book[t]["high"] * (1 - cfg.trail_pct))
        if price <= stop or signal["action"] == "SELL":
            ret = (cost.sell_fill(price) - book[t]["entry"]) / book[t]["entry"]
            print(f"[PAPER] CLOSE {t}  {ret*100:+.2f}%  (entry {book[t]['date']})")
            del book[t]


# ─────────────────────────────────────────────────────────────────────────────
# Self-test (no network) — proves the engine math is right
# ─────────────────────────────────────────────────────────────────────────────
def _synthetic(prices):
    bars = []
    for i, p in enumerate(prices):
        bars.append({"date": f"2024-01-{i+1:02d}", "o": p, "h": p * 1.01,
                     "l": p * 0.99, "c": p, "v": 10_000_000})
    return bars


def selftest():
    cost = CostModel(one_way_pct=0.0)  # isolate the engine from costs
    cfg = RiskConfig(init_stop_pct=0.05, trail_pct=0.06, max_hold_days=50)

    # 1) Steady climb then reversal -> trailing stop should bank a gain
    rise = [100, 105, 110, 120, 130, 140, 150, 140, 120]  # peak 150, h=151.5
    tr = simulate_trade(_synthetic(rise), 0, 50_000, cfg, cost)
    assert tr is not None and tr.ret_pct > 0, f"expected profit, got {tr.ret_pct:.4f}"
    assert tr.reason == "trail/stop"

    # 2) Smooth decline -> initial stop fills right around -5%
    drop = [100, 99, 98, 97, 96, 95, 94, 93]
    tr2 = simulate_trade(_synthetic(drop), 0, 50_000, cfg, cost)
    assert tr2 is not None and tr2.ret_pct < 0
    assert tr2.ret_pct >= -0.06, f"smooth-stop loss should be ~-5%, got {tr2.ret_pct:.4f}"

    # 2b) Gap-DOWN through the stop fills at the open, NOT the stop -> worse than -5%.
    #     (This is the honest gap risk: a stop is a trigger, not a guaranteed price.)
    gap = [100, 99, 96, 90, 80]   # close 96 then opens 90, below the 95 stop
    tr_gap = simulate_trade(_synthetic(gap), 0, 50_000, cfg, cost)
    assert tr_gap.ret_pct < -0.05, f"gap should exceed the stop loss, got {tr_gap.ret_pct:.4f}"

    # 3) Costs make a flat round-trip a loss
    flat = [100, 100, 100, 100, 100]
    trc = simulate_trade(_synthetic(flat), 0, 50_000,
                         RiskConfig(max_hold_days=2), CostModel(one_way_pct=0.002))
    assert trc.ret_pct < 0, "round-trip costs should turn a flat trade negative"

    # 4) Report + metrics run and compute break-even sanely
    for x in (tr, tr2):
        x.ticker = "TEST"
    m = compute_metrics([tr, tr2], cfg)
    assert m and m["n"] == 2 and m["win_rate"] == 50.0, m
    report([tr, tr2], cfg)

    # 5) Sweep runs end-to-end on synthetic data (no network): patch fetch_history
    import builtins  # noqa
    global fetch_history
    real_fetch = fetch_history
    saw = []
    p = 50.0
    for _ in range(8):                       # 8 rise/dip cycles -> many breakouts
        for _ in range(8):
            p += 4; saw.append(round(p, 2))
        for _ in range(4):
            p -= 5; saw.append(round(p, 2))
    fetch_history = lambda t, d: _synthetic(saw)
    try:
        sweep(["AAA", "BBB"], CostModel(), 120, momentum_breakout_entries,
              init_stops=(0.05, 0.08), trails=(0.06, 0.10), holds=(20,))
    finally:
        fetch_history = real_fetch

    print("\nSELF-TEST PASSED ✔  engine, metrics, sweep, costs, and report all check out.")


# ─────────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Measure the strategy before risking money.")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--backtest", nargs="+", metavar="TICKER")
    ap.add_argument("--sweep", nargs="+", metavar="TICKER")
    ap.add_argument("--papertrade", action="store_true")
    ap.add_argument("--days", type=int, default=730)
    args = ap.parse_args()

    cfg = RiskConfig()
    cost = CostModel()

    if args.selftest:
        selftest()
    elif args.backtest:
        print(f"Backtesting {len(args.backtest)} tickers over ~{args.days} days...")
        trades = backtest([t.upper() for t in args.backtest], cfg, cost,
                          args.days, momentum_breakout_entries)
        report(trades, cfg)
    elif args.sweep:
        print(f"Sweeping exit params on {len(args.sweep)} tickers over ~{args.days} days...")
        sweep([t.upper() for t in args.sweep], cost, args.days, momentum_breakout_entries)
    elif args.papertrade:
        print("Paper-trade scaffold ready. Wire paper_trade_step() into your live loop")
        print("(import your scanners from trader.py) and let it run forward with $0.")
        print("See paper_trade_step docstring for the 3-line integration.")
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
