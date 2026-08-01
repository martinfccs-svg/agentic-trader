"""
backtest_swing_v2.py -- historical test of swing_v2 on your equity universe.

Runs FOUR configurations over daily bars and compares them to buying SPY:
  A_full   variant-A entry (intraday stop-buy), full exit spec (2R half etc.)
  A_simple variant-A entry, simple exit (full out on close < EMA20, + stop)
  B_full   variant-B entry (confirmed close), full exit spec
  B_simple variant-B entry, simple exit

USAGE
  python backtest_swing_v2.py --symbols-file universe.txt --days 730

  universe.txt = one ticker per line (your 63 symbols). Without the flag it
  uses a small default basket just so the command runs.

Costs: --cost-bps per side (default 5 bps for liquid US equities slippage;
commissions are zero on Alpaca). Fills are pessimistic: variant A fills at
setup_high+0.01 only if that day's high exceeded it; stops fill at the stop
price or the day's open if it gapped through (gap risk included).

Same honesty rules as before: this simulates the past. Beating SPY after
costs on multiple windows is the bar for deployment consideration -- not a
promise about the future, and expect some or all configs to fail.

================================ VALIDATION STATUS ================================
NO REAL-DATA RUN HAS BEEN COMPLETED AS OF 2026-07-25.

Earlier results circulated for this harness came from a synthetic mode that
generated random-walk prices over 10 placeholder symbols. Those runs proved
the CODE EXECUTED; they carried zero information about edge. The tell: one
reported SPY at -22.1% over 365 days while real SPY was up strongly.
THAT MODE HAS BEEN REMOVED. This harness now fetches real bars or exits --
there is no code path here that can manufacture a number.

RULE FOR THIS FILE: performance numbers do not belong in this docstring. Put
real results in a dated document (docs/BACKTEST_RESULTS_<date>.md) where the
window, universe and date are stated. If you find performance figures written
into this file's documentation, they are wrong -- delete them.

To produce a real verdict (from the repo directory, ALPACA_API_KEY /
ALPACA_SECRET_KEY set):

    python backtest_swing_v2.py --days 730
    python backtest_swing_v2.py --days 365

Confirm before reading any number:
  * the first line says "68 symbols from config.UNIVERSE"
  * NO "!!!! SYNTHETIC DATA" banner appears

The bar for promoting swing_v2 out of shadow: beat hold-SPY on Sharpe with a
shallower max drawdown, on BOTH windows, after costs. Expect filt_brkout to
take very few trades -- its statistics will rest on a sample too small to
trust regardless of how they look.
===================================================================================
"""

from __future__ import annotations

import argparse
import math
import os
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from swing_v2 import (detect_setup, ema, atr, RISK_PCT, MAX_NOTIONAL_PCT,   # noqa
                      MAX_CONCURRENT, MAX_NEW_PER_DAY, SETUP_EXPIRY_DAYS,
                      TIME_STOP_DAYS, VOL_MULT_B)
from meanrev_scoring import adx as _adx   # Wilder ADX, already unit-tested
_adx_bt = _adx


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
        sys.exit(
            "\nNO API CREDENTIALS FOUND — cannot fetch real market data.\n\n"
            "  Set your PAPER keys (the same ones Railway uses):\n"
            "      export ALPACA_API_KEY=...\n"
            "      export ALPACA_SECRET_KEY=...\n"
            "  (APCA_API_KEY_ID / APCA_API_SECRET_KEY also work.)\n\n"
            "  There is NO synthetic or offline fallback in this harness --\n"
            "  by design. A run without credentials produces no number at\n"
            "  all, rather than a plausible-looking one. Fix the keys.\n")
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


def run_config(all_bars, dates, variant, simple_exit, start_equity, cost,
               exits=None):
    """exits: {"trail_atr":float, "trail_after_r":float, "vol_exit":float,
    "adx_decay":float}; 0 disables each. The live engine reads these from
    env vars; the harness takes them as arguments so one process can replay
    several configurations and compare them (2026-08-01)."""
    exits = exits or {}
    TRAIL = float(exits.get("trail_atr", 0) or 0)
    TRAIL_AFTER = float(exits.get("trail_after_r", 1.0) or 1.0)
    VOLX = float(exits.get("vol_exit", 0) or 0)
    ADXD = float(exits.get("adx_decay", 0) or 0)
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
            hist = _bars_upto(all_bars[sym], today, inclusive=True)
            atr_now = atr(hist, 14) if (TRAIL or VOLX) else None
            p["hw"] = max(p.get("hw", p["e"]), b["c"])

            # --- adaptive ATR trail: ratchets up only, after TRAIL_AFTER R
            if TRAIL and atr_now and p["r"] > 0 \
                    and p["hw"] >= p["e"] + TRAIL_AFTER * p["r"]:
                p["stop"] = max(p["stop"], p["hw"] - TRAIL * atr_now)

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
            # --- volatility expansion: sized for entry ATR, not this ---
            if not fill and VOLX and atr_now and p.get("atr0", 0) > 0 \
                    and atr_now > VOLX * p["atr0"]:
                fill, reason = b["c"], "vol_expansion"
            # --- ADX decay: the trend that justified the entry is gone ---
            if not fill and ADXD and p.get("adx0", 0) > 0:
                a_now = _adx_bt([x["h"] for x in hist], [x["l"] for x in hist],
                                [x["c"] for x in hist], 14)
                if a_now is not None and a_now < ADXD * p["adx0"]:
                    fill, reason = b["c"], "adx_decay"
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
        # SPY closes are passed as the relative-strength benchmark (added
        # 2026-08-01). detect_setup's bench argument defaults to None, which
        # SKIPS the RS gate — so without this the harness would silently
        # measure the OLD strategy while the live code ran the new one, and
        # the backtest would be answering a question nobody asked.
        bench_hist = [b["c"] for b in
                      _bars_upto(all_bars.get("SPY", []), today,
                                 inclusive=False)] or None
        for sym in syms:
            hist = _bars_upto(all_bars[sym], today, inclusive=False)
            if len(hist) < 60:
                continue
            s, why = detect_setup(sym, hist, bench_hist)
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
                                  "sh": sh, "half": False, "held": 0,
                                  "hw": entry_px, "atr0": s.atr14,
                                  "adx0": getattr(s, "adx_at_setup", 0.0)}
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




def _resolve_universe(a):
    """(symbols, source). Config first, explicit file second, hardcoded list
    only as a loudly-named last resort — a silent DEFAULT_SYMBOLS fallback is
    how a run meant for the real 68 quietly tested 10 names instead."""
    if getattr(a, "symbols_file", None):
        syms = [l.strip().upper() for l in open(a.symbols_file)
                if l.strip() and not l.startswith("#")]
        return syms, a.symbols_file
    try:
        from config import UNIVERSE
        return [t.strip().upper() for t in UNIVERSE], "config.UNIVERSE"
    except Exception as e:  # noqa: BLE001
        return DEFAULT_SYMBOLS, (f"DEFAULT_SYMBOLS fallback — could NOT "
                                 f"import UNIVERSE from config.py ({e}); run "
                                 f"from the repo directory")


# ---------------------------------------------------------------------------
# EXIT SWEEP + PROMOTION RULES
#
# Stated BEFORE the numbers are seen, which is the whole point: a rule chosen
# after looking at results is not a rule, it is a preference wearing one. The
# thresholds below encode what "better" means for a low-drawdown pullback
# sleeve, and every candidate is judged by the same test on BOTH windows.
#
# Why "both windows" is not optional: the same 365-day window has produced
# Sharpe readings of 0.30, 0.77 and 1.50 for this strategy across runs, and a
# 77-setting sweep spanned -0.05 to 1.62. A single-window improvement is
# indistinguishable from a lucky draw.
# ---------------------------------------------------------------------------
MIN_SHARPE_GAIN = 0.05      # must beat baseline by at least this
MAX_DD_WORSENING = 0.005    # 0.5pp; drawdown is this strategy's ONLY robust edge
MIN_TRADES = 30             # below this the statistics are anecdote

EXIT_VARIANTS = [
    ("baseline",     {}),
    ("+ ATR trail",  {"trail_atr": 2.0, "trail_after_r": 1.0}),
    ("+ vol exit",   {"vol_exit": 1.8}),
    ("+ ADX decay",  {"adx_decay": 0.6}),
]


def sweep_exits(a):
    base, src = _resolve_universe(a)
    print(f"Universe: {len(base)} symbols from {src}")
    windows = [365, 730]
    results = {}
    for days in windows:
        print(f"\n=== fetching {days}d window ===")
        fetch_syms = base + (["SPY"] if "SPY" not in base else [])
        bars = fetch_bars(fetch_syms, days)
        dates = sorted({b["t"][:10] for s in bars for b in bars[s]})
        replay = dates[60:]
        years = max(len(replay) / 252, 1e-9)
        print(f"replay window: {len(replay)} trading days ({years:.2f} yr)")
        for name, cfg in EXIT_VARIANTS:
            curve, trades = run_config(bars, dates, "A", False, 100_000,
                                       a.cost_bps / 10000, exits=cfg)
            results[(days, name)] = stats(curve, trades, years)

    for days in windows:
        print(f"\n{'='*92}\n{days}-DAY WINDOW\n{'='*92}")
        print(f"{'configuration':<16}{'sharpe':>9}{'total':>9}{'maxdd':>9}"
              f"{'trades':>9}{'win%':>8}{'d-sharpe':>10}{'d-maxdd':>10}")
        b = results[(days, "baseline")]
        for name, _ in EXIT_VARIANTS:
            r = results[(days, name)]
            ds = r["sharpe"] - b["sharpe"]
            dd = abs(r["maxdd"]) - abs(b["maxdd"])
            print(f"{name:<16}{r['sharpe']:>9.2f}{r['total']:>8.1%}"
                  f"{r['maxdd']:>9.1%}{r['trades']:>9}{r['win%']:>8.1f}"
                  + ("" if name == "baseline"
                     else f"{ds:>+10.2f}{dd:>+10.1%}"))

    lines = _promotion_report(results, windows)
    for ln in lines:
        print(ln)

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = f"BACKTEST_RESULTS_{stamp}.md"
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(_markdown_report(results, windows, a, stamp))
        print(f"\nreport written: {path}")
        print("  (results belong in a dated document, not in a tool's "
              "docstring — a number in source cannot say WHEN it was true)")
    except Exception as e:  # noqa: BLE001
        print(f"\ncould not write {path}: {e}")


def _verdict(results, windows, name):
    """Per-window checks plus the BINDING reason when a variant is rejected."""
    per_window, promote = [], True
    fails = []
    for days in windows:
        r, b = results[(days, name)], results[(days, "baseline")]
        ds = r["sharpe"] - b["sharpe"]
        dd = abs(r["maxdd"]) - abs(b["maxdd"])
        checks = [
            (ds >= MIN_SHARPE_GAIN, f"Sharpe {ds:+.2f}"
             + ("" if ds >= MIN_SHARPE_GAIN else f" (need +{MIN_SHARPE_GAIN:.2f})")),
            (dd <= MAX_DD_WORSENING,
             f"Max DD {'improved' if dd < 0 else 'worsened'} {abs(dd):.1%}"
             + ("" if dd <= MAX_DD_WORSENING else f" (limit {MAX_DD_WORSENING:.1%})")),
            (r["trades"] >= MIN_TRADES, f"{r['trades']} trades"
             + ("" if r["trades"] >= MIN_TRADES else f" (need {MIN_TRADES})")),
        ]
        ok = all(c[0] for c in checks)
        promote &= ok
        per_window.append((days, ok, checks))
        if not ok:
            if not checks[2][0]:
                fails.append("insufficient sample size")
            elif not checks[1][0]:
                fails.append("drawdown worsened beyond the limit")
            else:
                fails.append("Sharpe gain below threshold")
    reason = ""
    if not promote:
        # sample size invalidates the other metrics, so it outranks them
        if "insufficient sample size" in fails:
            reason = "insufficient sample size"
        elif len(set(fails)) == 1:
            reason = fails[0]
        else:
            reason = "; ".join(sorted(set(fails)))
        if promote is False and all(w[1] for w in per_window[:1]) \
                and not all(w[1] for w in per_window):
            reason += " — held on one window only"
    return promote, per_window, reason


def _promotion_report(results, windows):
    out = ["", "=" * 92, "PROMOTION REPORT", "=" * 92,
           f"  rules, fixed before the run: Sharpe gain >= "
           f"+{MIN_SHARPE_GAIN:.2f} | Max DD worsens <= "
           f"{MAX_DD_WORSENING:.1%} | trades >= {MIN_TRADES}",
           f"  every rule must hold on ALL windows: "
           f"{', '.join(str(w) + 'd' for w in windows)}", ""]
    for name, _ in EXIT_VARIANTS:
        if name == "baseline":
            continue
        promote, per_window, reason = _verdict(results, windows, name)
        out.append(f"  {name}")
        for days, ok, checks in per_window:
            out.append(f"    {days}-day window"
                       + ("" if ok else "   <- fails here"))
            for passed, text in checks:
                out.append(f"      {'PASS' if passed else 'FAIL'}  {text}")
        out.append(f"    Promotion: {'YES' if promote else 'NO'}"
                   + ("" if promote else f"   Reason: {reason}"))
        out.append("")
    out.append("  A rejected variant is not necessarily bad — it is unproven")
    out.append("  on this data, which for deployment purposes is the same "
               "thing.")
    return out


def _markdown_report(results, windows, a, stamp) -> str:
    md = [f"# Swing V2 exit sweep — {stamp}", "",
          f"- universe: config.UNIVERSE ({len(_resolve_universe(a)[0])} names)",
          f"- windows: {', '.join(str(w) + 'd' for w in windows)}",
          f"- costs: {a.cost_bps} bps per side",
          f"- rules fixed before the run: Sharpe gain >= "
          f"+{MIN_SHARPE_GAIN:.2f}, Max DD worsens <= "
          f"{MAX_DD_WORSENING:.1%}, trades >= {MIN_TRADES}, "
          f"all windows", ""]
    for days in windows:
        md += [f"## {days}-day window", "",
               "| configuration | sharpe | total | maxdd | trades | win% | "
               "d-sharpe | d-maxdd |",
               "|---|---:|---:|---:|---:|---:|---:|---:|"]
        b = results[(days, "baseline")]
        for name, _ in EXIT_VARIANTS:
            r = results[(days, name)]
            ds = r["sharpe"] - b["sharpe"]
            dd = abs(r["maxdd"]) - abs(b["maxdd"])
            delta = ("| | " if name == "baseline"
                     else f"| {ds:+.2f} | {dd:+.1%} ")
            md.append(f"| {name} | {r['sharpe']:.2f} | {r['total']:.1%} | "
                      f"{r['maxdd']:.1%} | {r['trades']} | {r['win%']:.1f} "
                      f"{delta}|")
        md.append("")
    md += ["## Verdicts", ""]
    for name, _ in EXIT_VARIANTS:
        if name == "baseline":
            continue
        promote, per_window, reason = _verdict(results, windows, name)
        md.append(f"**{name} — {'PROMOTE' if promote else 'REJECT'}**"
                  + ("" if promote else f" ({reason})"))
        for days, ok, checks in per_window:
            for passed, text in checks:
                md.append(f"- {days}d: {'PASS' if passed else 'FAIL'} — {text}")
        md.append("")
    return "\n".join(md)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--cost-bps", type=float, default=5.0)
    ap.add_argument("--sweep-exits", action="store_true",
                    help="replay baseline + each adaptive exit one at a time, "
                         "on BOTH windows, and apply the promotion rules")
    ap.add_argument("--symbols-file", default=None,
                    help="optional; omit to read UNIVERSE from config.py")
    a = ap.parse_args()
    if a.sweep_exits:
        sweep_exits(a)
        return

    # Universe resolution, most-authoritative first. DEFAULT_SYMBOLS used to
    # be the silent fallback, which is how a run intended for the real
    # 68-name universe quietly tested 10 hardcoded names instead.
    syms, _src = _resolve_universe(a)
    print(f"Universe: {len(syms)} symbols from {_src} | window ~{a.days}d | "
          f"cost {a.cost_bps}bps/side")
    # SPY must always be fetched even though it is not in the trading
    # universe: it IS the benchmark, and the promotion bar is stated against
    # it. When the universe switched to config.UNIVERSE (which has no SPY),
    # the hold_SPY row silently disappeared from the results table — a
    # missing benchmark is worse than a wrong one, because nothing looks wrong.
    fetch_syms = syms + (["SPY"] if "SPY" not in syms else [])
    bars = fetch_bars(fetch_syms, a.days)
    if "SPY" not in bars:
        print("\nWARNING: SPY bars unavailable — the hold_SPY benchmark row "
              "will be absent and the promotion bar CANNOT be evaluated.\n")
    dates = sorted({b["t"][:10] for s in bars.values() for b in s})
    # WARMUP-ALIGNED WINDOW (2026-07-25 fix). The replay loops start at
    # dates[WARM], so strategies trade fewer days than the raw fetch. Timing
    # the benchmark over the FULL fetch while strategies traded a subset made
    # the two non-comparable and inflated every annualised figure. Both now
    # use the same window.
    WARM = 60
    replay_dates = dates[WARM:]
    years = max(len(replay_dates) / 252, 1e-9)
    print(f"replay window: {len(replay_dates)} trading days "
          f"({years:.2f} yr) after a {WARM}-day warmup — benchmark timed over "
          f"the SAME window")
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
        # benchmark over replay_dates only, so totals are apples-to-apples
        by_date = {b["t"][:10]: float(b["c"]) for b in bars["SPY"]}
        spy = [by_date[d] for d in replay_dates if d in by_date]
        if len(spy) > 2:
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
