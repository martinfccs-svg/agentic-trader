"""
backtest_swing_v2.py -- historical test of swing_v2 on your equity universe.

Runs FOUR configurations over daily bars and compares them to buying SPY:
  A_full   variant-A entry (intraday stop-buy), full exit spec (2R half etc.)
  A_simple variant-A entry, simple exit (full out on close < EMA20, + stop)
  B_full   variant-B entry (confirmed close), full exit spec
  B_simple variant-B entry, simple exit

USAGE
  python backtest_swing_v2.py --symbols-file universe.txt --days 730
  python backtest_swing_v2.py --synthetic          # pipeline check, no keys

  universe.txt = one ticker per line (your 63 symbols). Without the flag it
  uses a small default basket just so the command runs.

Costs: --cost-bps per side (default 5 bps for liquid US equities slippage;
commissions are zero on Alpaca). Fills are pessimistic: variant A fills at
setup_high+0.01 only if that day's high exceeded it; stops fill at the stop
price or the day's open if it gapped through (gap risk included).

Same honesty rules as before: this simulates the past. Beating SPY after
costs on multiple windows is the bar for deployment consideration -- not a
promise about the future, and expect some or all configs to fail.
"""

from __future__ import annotations

import argparse
import math
import os
import random
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from swing_v2 import (detect_setup, ema, atr, RISK_PCT, MAX_NOTIONAL_PCT,   # noqa
                      MAX_CONCURRENT, MAX_NEW_PER_DAY, SETUP_EXPIRY_DAYS,
                      TIME_STOP_DAYS, VOL_MULT_B)
from meanrev_scoring import adx as _adx   # Wilder ADX, already unit-tested


def _rsi(values, period=14):
    if len(values) < period + 1:
        return None
    g = [max(values[i] - values[i-1], 0.0) for i in range(1, len(values))]
    l = [max(values[i-1] - values[i], 0.0) for i in range(1, len(values))]
    ag, al = sum(g[:period]) / period, sum(l[:period]) / period
    for i in range(period, len(g)):
        ag = (ag * (period - 1) + g[i]) / period
        al = (al * (period - 1) + l[i]) / period
    return 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)


# ---------------------------------------------------------------------------
# filt_brkout — the operator's filtered-breakout proposal (2026-07-22),
# CORRECTED: breakout vs the PRIOR 20 highs (the submitted code's
# tail(20).max() included today's bar, requiring close >= today's own high —
# near-signal-less silently; indicators.prior_high documents this trap).
# Signal on completed bar t: close > max(high[t-20:t]) AND EMA50>EMA200 AND
# close>EMA200 AND 45<=RSI(14)<=65 AND ADX(14)>20 AND vol > 1.2 x avg20.
# Entry next open; stop = entry - 2.0*ATR14; sole exit = 2.0*ATR trail off
# the highest close (never widens); gap-honest fills.
# ---------------------------------------------------------------------------
def run_filtered_breakout(all_bars, dates, start_equity, cost):
    equity = start_equity
    curve = [equity]
    positions = {}   # sym -> {e, stop, hc, sh}
    trades = []
    syms = [x for x in all_bars if x != "SPY"]
    for di in range(60, len(dates) - 1):
        today = dates[di]
        # exits
        for sym in list(positions):
            p = positions[sym]
            b = _bar(all_bars[sym], today)
            if not b:
                continue
            p["hc"] = max(p["hc"], b["c"])
            a = atr(_bars_upto(all_bars[sym], today, inclusive=True), 14)
            if a:
                p["stop"] = max(p["stop"], p["hc"] - 2.0 * a)
            fill = None
            if b["o"] <= p["stop"]:
                fill = b["o"]
            elif b["l"] <= p["stop"]:
                fill = p["stop"]
            if fill:
                pnl = p["sh"] * (fill - p["e"]) - p["sh"] * fill * cost
                equity += pnl
                trades.append({"sym": sym, "pnl": pnl, "reason": "trail",
                               "held": 0})
                del positions[sym]
        # entries from yesterday's completed bar
        entries_today = 0
        for sym in syms:
            if sym in positions or len(positions) >= MAX_CONCURRENT                     or entries_today >= MAX_NEW_PER_DAY:
                continue
            hist = _bars_upto(all_bars[sym], today, inclusive=False)
            if len(hist) < 210:
                continue
            closes = [x["c"] for x in hist]
            cur = hist[-1]
            prior_high20 = max(x["h"] for x in hist[-21:-1])
            if not cur["c"] > prior_high20:
                continue
            e50, e200 = ema(closes, 50), ema(closes, 200)
            if not (e50 and e200 and e50 > e200 and cur["c"] > e200):
                continue
            r = _rsi(closes, 14)
            if r is None or not (45 <= r <= 65):
                continue
            a_val = _adx([x["h"] for x in hist], [x["l"] for x in hist],
                         closes, 14)
            if a_val is None or a_val <= 20:
                continue
            av20 = sum(x["v"] for x in hist[-21:-1]) / 20
            if not (av20 and cur["v"] > 1.2 * av20):
                continue
            b = _bar(all_bars[sym], today)
            a14 = atr(hist, 14)
            if not b or not a14:
                continue
            entry_px = b["o"]
            stop = entry_px - 2.0 * a14
            dist = entry_px - stop
            if dist <= 0:
                continue
            sh = int(min(equity * RISK_PCT / dist,
                         equity * MAX_NOTIONAL_PCT / entry_px))
            if sh <= 0:
                continue
            equity -= sh * entry_px * cost
            positions[sym] = {"e": entry_px, "stop": stop, "hc": entry_px,
                              "sh": sh}
            entries_today += 1
        mtm = equity
        for sym, p in positions.items():
            b = _bar(all_bars[sym], today)
            if b:
                mtm += p["sh"] * (b["c"] - p["e"])
        curve.append(mtm)
    return curve, trades

try:
    import requests
except ImportError:
    requests = None

DEFAULT_SYMBOLS = ["AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA",
                   "AMD", "JPM", "XOM"]
STOCK_DATA = "https://data.alpaca.markets/v2/stocks"


def fetch_bars(symbols, days):
    key = os.environ.get("ALPACA_API_KEY") or os.environ.get("APCA_API_KEY_ID")
    sec = (os.environ.get("ALPACA_SECRET_KEY")
           or os.environ.get("APCA_API_SECRET_KEY"))
    if not key or not sec:
        sys.exit("Set ALPACA_API_KEY / ALPACA_SECRET_KEY (or APCA_* names), "
                 "or use --synthetic.")
    h = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec}
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    out = {}
    for i in range(0, len(symbols), 50):
        chunk, page = symbols[i:i + 50], None
        while True:
            params = {"symbols": ",".join(chunk), "timeframe": "1Day",
                      "start": start, "limit": 10000, "adjustment": "split"}
            if page:
                params["page_token"] = page
            r = requests.get(f"{STOCK_DATA}/bars", params=params, headers=h,
                             timeout=30)
            r.raise_for_status()
            j = r.json()
            for s, bs in j.get("bars", {}).items():
                out.setdefault(s, []).extend(bs)
            page = j.get("next_page_token")
            if not page:
                break
            time.sleep(0.25)
    return out


def synthetic(symbols, days, seed=11):
    rng = random.Random(seed)
    start = datetime(2024, 1, 2)
    out = {}
    for sym in symbols + ["SPY"]:
        drift, vol, px = rng.uniform(-0.0003, 0.001), rng.uniform(0.012, 0.03), 100.0
        bars, d = [], start
        while len(bars) < days:
            if d.weekday() < 5:
                o = px * math.exp(vol * rng.gauss(0, 0.3))
                c = o * math.exp(drift + vol * rng.gauss(0, 1))
                hi = max(o, c) * (1 + abs(rng.gauss(0, vol / 2)))
                lo = min(o, c) * (1 - abs(rng.gauss(0, vol / 2)))
                v = max(1, rng.lognormvariate(13, 0.5))
                bars.append({"t": d.strftime("%Y-%m-%dT00:00:00Z"), "o": o,
                             "h": hi, "l": lo, "c": c, "v": v})
                px = c
            d += timedelta(days=1)
        out[sym] = bars
    return out


def run_config(all_bars, dates, variant, simple_exit, start_equity, cost):
    equity = start_equity
    cash_curve = [equity]
    positions = {}       # sym -> dict
    setups = {}          # sym -> (Setup, created_idx)
    trades = []
    syms = [s for s in all_bars if s != "SPY"]

    for di in range(60, len(dates) - 1):
        today, entries_today = dates[di], 0
        # 1) exits on today's bar
        for sym in list(positions):
            p = positions[sym]
            b = _bar(all_bars[sym], today)
            if not b:
                continue
            p["held"] += 1
            closes = _closes_upto(all_bars[sym], today)
            e20 = ema(closes, 20)
            fill = None; reason = None
            if b["o"] <= p["stop"]:
                fill, reason = b["o"], "gap_stop"
            elif b["l"] <= p["stop"]:
                fill, reason = p["stop"], "stop"
            elif not simple_exit and not p["half"] and b["h"] >= p["e"] + 2 * p["r"]:
                px = p["e"] + 2 * p["r"]
                n = p["sh"] // 2
                equity += n * (px - p["e"]) - n * px * cost
                p["sh"] -= n; p["half"] = True; p["stop"] = p["e"]
            if not fill and e20 and b["c"] < e20 and p["held"] >= 2:
                fill, reason = b["c"], "ema20"
            if not fill and p["held"] >= TIME_STOP_DAYS and b["c"] < p["e"] + p["r"]:
                fill, reason = b["c"], "time"
            if fill:
                pnl = p["sh"] * (fill - p["e"]) - p["sh"] * fill * cost
                equity += pnl
                trades.append({"sym": sym, "pnl": pnl, "reason": reason,
                               "held": p["held"]})
                del positions[sym]
        # 2) detect fresh setups on yesterday's completed bar
        for sym in syms:
            hist = _bars_upto(all_bars[sym], today, inclusive=False)
            if len(hist) < 60:
                continue
            s, why = detect_setup(sym, hist)
            if s:
                setups[sym] = (s, di)
        # expire
        for sym in list(setups):
            if di - setups[sym][1] > SETUP_EXPIRY_DAYS:
                del setups[sym]
        # 3) entries on today's bar
        for sym, (s, _) in list(setups.items()):
            if sym in positions or len(positions) >= MAX_CONCURRENT \
                    or entries_today >= MAX_NEW_PER_DAY:
                continue
            b = _bar(all_bars[sym], today)
            if not b:
                continue
            entry_px = None
            if variant == "A" and b["h"] > s.setup_high + 0.01:
                entry_px = max(s.setup_high + 0.01, b["o"])  # gap-honest fill
            elif variant == "B":
                prev = _bar(all_bars[sym], dates[di - 1])
                if prev and prev["c"] > s.setup_high and s.avg_vol20 \
                        and prev["v"] >= VOL_MULT_B * s.avg_vol20:
                    entry_px = b["o"]
            if entry_px:
                stop = min(s.setup_low - s.atr14, s.swing_low_lvl)
                dist = entry_px - stop
                if dist <= 0:
                    continue
                sh = int(min(equity * RISK_PCT / dist,
                             equity * MAX_NOTIONAL_PCT / entry_px))
                if sh <= 0:
                    continue
                equity -= sh * entry_px * cost
                positions[sym] = {"e": entry_px, "stop": stop, "r": dist,
                                  "sh": sh, "half": False, "held": 0}
                entries_today += 1
                del setups[sym]
        # 4) mark equity
        mtm = equity
        for sym, p in positions.items():
            b = _bar(all_bars[sym], today)
            if b:
                mtm += p["sh"] * (b["c"] - p["e"])
        cash_curve.append(mtm)
    return cash_curve, trades


def _bar(bars, d):
    for b in bars:
        if b["t"][:10] == d:
            return b
    return None

def _bars_upto(bars, d, inclusive):
    return [b for b in bars if b["t"][:10] < d or (inclusive and b["t"][:10] <= d)]

def _closes_upto(bars, d):
    return [b["c"] for b in bars if b["t"][:10] <= d]


def stats(curve, trades, years):
    tot = curve[-1] / curve[0] - 1
    rets = [curve[i + 1] / curve[i] - 1 for i in range(len(curve) - 1)]
    sd = statistics.pstdev(rets) or 1e-12
    sharpe = statistics.mean(rets) / sd * math.sqrt(252)
    peak, dd = curve[0], 0
    for v in curve:
        peak = max(peak, v); dd = min(dd, v / peak - 1)
    wins = [t for t in trades if t["pnl"] > 0]
    return {"total": tot, "cagr": (curve[-1] / curve[0]) ** (1 / years) - 1
            if years and curve[-1] > 0 else 0,
            "sharpe": sharpe, "maxdd": dd, "trades": len(trades),
            "win%": len(wins) / len(trades) * 100 if trades else 0,
            "avg_win": statistics.mean(t["pnl"] for t in wins) if wins else 0,
            "avg_loss": statistics.mean(t["pnl"] for t in trades
                                        if t["pnl"] <= 0)
            if len(wins) < len(trades) else 0}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--cost-bps", type=float, default=5.0)
    ap.add_argument("--symbols-file")
    ap.add_argument("--synthetic", action="store_true")
    a = ap.parse_args()

    syms = DEFAULT_SYMBOLS
    if a.symbols_file:
        syms = [l.strip().upper() for l in open(a.symbols_file)
                if l.strip() and not l.startswith("#")]
    print(f"Universe: {len(syms)} symbols | window ~{a.days}d | "
          f"cost {a.cost_bps}bps/side")
    if a.synthetic:
        print("*** SYNTHETIC data -- results meaningless; pipeline test ***")
        bars = synthetic(syms, min(a.days, 500))
    else:
        bars = fetch_bars(syms + ["SPY"], a.days)
    dates = sorted({b["t"][:10] for s in bars.values() for b in s})
    years = len(dates) / 252
    cost = a.cost_bps / 10000

    rows = {}
    for variant in ("A", "B"):
        for simple in (False, True):
            name = f"{variant}_{'simple' if simple else 'full'}"
            curve, trades = run_config(bars, dates, variant, simple,
                                       100_000, cost)
            rows[name] = stats(curve, trades, years)
    curve, trades = run_filtered_breakout(bars, dates, 100_000, cost)
    rows["filt_brkout"] = stats(curve, trades, years)

    if "SPY" in bars:
        spy = [b["c"] for b in bars["SPY"]]
        curve = [100_000 * c / spy[0] for c in spy]
        rows["hold_SPY"] = stats(curve, [], years)

    cols = ["total", "cagr", "sharpe", "maxdd", "trades", "win%",
            "avg_win", "avg_loss"]
    print(f"\n{'config':<10}" + "".join(f"{c:>11}" for c in cols))
    print("-" * 100)
    for name, st in rows.items():
        row = f"{name:<10}"
        for c in cols:
            v = st[c]
            row += (f"{v:>10.1%} " if c in ("total", "cagr", "maxdd")
                    else f"{v:>10.0f} " if c in ("trades", "avg_win", "avg_loss")
                    else f"{v:>10.1f} " if c == "win%"
                    else f"{v:>10.2f} ")
        print(row)
    print("\nBar for deployment consideration: beat hold_SPY on Sharpe with "
          "shallower maxdd, after costs, on BOTH --days 365 and --days 730. "
          "avg_win should exceed |avg_loss| meaningfully for a pullback "
          "system, and win% below ~35 with weak avg_win means the entries "
          "aren't earning their costs.")


if __name__ == "__main__":
    main()
