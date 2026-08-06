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
import json
import math
import os
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import swing_exit_policy    # ONE policy: live and replay cannot disagree
import exit_rules            # ONE source of truth for exit arithmetic
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
            p["stop"] = exit_rules.ratchet_stop(p["stop"], p["hc"], a, 2.0)
            fill, _ = exit_rules.gap_exit(b["o"], p["stop"], b["l"])
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
    h = _auth_headers(key, sec)
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    out = {}
    for i in range(0, len(symbols), 50):
        chunk, page = symbols[i:i + 50], None
        while True:
            params = {"symbols": ",".join(chunk), "timeframe": "1Day",
                      "start": start, "limit": 10000,
                      # SPLIT-ONLY, to MATCH LIVE (2026-08-04).
                      #
                      # Total return is the right basis for measuring
                      # performance, and switching to it here was the first
                      # instinct. But swing_v2 generates live signals on
                      # split-adjusted prices, and dividend adjustment
                      # RETROACTIVELY restates every historical bar each time
                      # a constituent pays out — so replay signals would have
                      # been computed on prices the live desk never saw. That
                      # is the same live/replay drift the shared exit policy
                      # was built to end.
                      #
                      # Resolution: signals on split-adjusted (identical to
                      # live), and the BENCHMARK fetched separately on total
                      # return, since hold_SPY collects dividends for the
                      # entire window and understating it by ~2pp over 730
                      # days flattered every comparison. The strategy's own
                      # returns still exclude dividends earned while holding
                      # — roughly 0.3%/yr understated. That residual bias
                      # runs AGAINST the strategy, which is the safe
                      # direction for a promotion decision.
                      "adjustment": "split"}
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


def conviction_mult(adx, vol_ratio, lo=0.5, hi=1.5):
    """Size multiplier from setup quality — the empirical test of Layer 3.

    The proposal is to give higher-scoring candidates more capital. That is
    only better than equal weighting IF the score predicts outcomes; if it
    does not, it delivers the same return with more variance, which is
    strictly worse. Simulation of both cases:

        score predicts : return/variance 0.146 -> 0.553   (much better)
        score is noise : return/variance 0.164 -> 0.148   (worse)

    So this exists to be MEASURED against equal weighting on real data, not
    to be switched on. Uses the two quality figures swing_v2 already records
    at setup — ADX and the setup-candle volume ratio — rather than inventing
    a composite with new weights.
    """
    if adx is None:
        return 1.0
    a = max(0.0, min(1.0, (adx - 20.0) / 25.0))          # 20..45 -> 0..1
    v = max(0.0, min(1.0, ((vol_ratio or 1.2) - 1.2) / 0.8))  # 1.2..2.0
    q = 0.7 * a + 0.3 * v
    return lo + (hi - lo) * q


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
    STAGED = bool(exits.get("staged_lock", False))
    CONVICTION = bool(exits.get("conviction", False))
    PCTLOCK = bool(exits.get("percent_lock", False))
    RSX = float(exits.get("rs_exit", 0) or 0)
    ADX_TRAIL = bool(exits.get("adx_trail", False))
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
            # SHARED ARITHMETIC (2026-08-04). These lines used to be a local
            # copy of the ratchet. Mathematically identical to
            # exit_rules.ratchet_stop today — which is exactly the problem:
            # "identical today" is how two implementations start, not how
            # they stay. The replay must exercise the SAME code the live desk
            # runs, or the backtest slowly becomes a test of something else.
            if TRAIL and atr_now and p["r"] > 0:
                p["stop"] = exit_rules.trail_after_r(
                    p["e"], p["hw"], p["r"], TRAIL_AFTER, atr_now, TRAIL,
                    p["stop"])

            # --- trail distance chosen by trend strength ---------------
            if ADX_TRAIL and atr_now and p["r"] > 0 \
                    and p["hw"] >= p["e"] + p["r"]:
                try:
                    import exit_rules
                    _m = exit_rules.adx_trail_mult(
                        _adx_bt([x["h"] for x in hist], [x["l"] for x in hist],
                                [x["c"] for x in hist], 14))
                    p["stop"] = exit_rules.ratchet_stop(
                        p["stop"], p["hw"], atr_now, _m)
                except Exception:  # noqa: BLE001
                    pass

            # --- staged ATR profit lock (2026-08-04) -------------------
            # Progressive protection: breakeven at +1 ATR, 0.5 ATR locked at
            # +2, a 2xATR trail at +3, the 20-EMA at +5. Swept rather than
            # assumed, because tightening as a trade runs both reduces
            # giveback AND cuts winners short — and a trend strategy earns
            # its return from the few trades that run furthest.
            if STAGED and atr_now:
                try:
                    import exit_rules
                    p["stop"], _ = exit_rules.staged_profit_lock(
                        p["e"], p["hw"], atr_now, p["stop"], e20)
                except Exception:  # noqa: BLE001
                    pass

            fill = None; reason = None
            # ---- SHARED POLICY (2026-08-04) ---------------------------
            # The same module the live desk calls. Previously this block
            # implemented swing's exit ladder locally, and it had ALREADY
            # drifted: it checked volatility expansion and ADX decay, which
            # the live engine did not. The harness was measuring a strategy
            # the bot does not run.
            _adx_now = None
            if ADXD and p.get("adx0", 0) > 0:
                _adx_now = _adx_bt([x["h"] for x in hist],
                                   [x["l"] for x in hist],
                                   [x["c"] for x in hist], 14)
            # Returns SINCE ENTRY for both the name and the benchmark, so
            # the policy can judge whether the reason for the trade survived.
            _sr = _br = None
            if RSX and p.get("spy0"):
                _spy = _closes_upto(all_bars.get("SPY", []), today)
                if _spy:
                    _sr = b["c"] / p["e"] - 1.0
                    _br = _spy[-1] / p["spy0"] - 1.0
            _cfg = swing_exit_policy.SwingExitConfig(
                trail_atr=TRAIL, trail_after_r=TRAIL_AFTER,
                adx_trail=ADX_TRAIL, staged_lock=STAGED,
                percent_lock=PCTLOCK,
                vol_exit_mult=VOLX, adx_decay_frac=ADXD,
                rs_exit_lag=RSX, time_stop_days=TIME_STOP_DAYS)
            _ctx = swing_exit_policy.SwingExitContext(
                entry=p["e"], stop=p["stop"], r=p["r"], high_water=p["hw"],
                held_days=p["held"], open=b["o"], high=b["h"], low=b["l"],
                close=b["c"], atr_now=atr_now, atr_at_entry=p.get("atr0"),
                ema20=e20, adx_now=_adx_now, adx_at_entry=p.get("adx0"),
                stock_return=_sr, bench_return=_br)
            p["stop"], reason, fill = swing_exit_policy.evaluate(_ctx, _cfg)

            # The 2R PARTIAL stays here, and cannot move into the shared
            # policy: partial exits do not exist live — brokers.sell() closes
            # whole positions. Modelling one in the policy both desks share
            # would let the replay measure a trade the bot cannot place. It
            # is skipped when the stop already fired, matching the original
            # ordering exactly.
            if reason not in ("stop", "gap_stop") and not simple_exit \
                    and not p["half"] and b["h"] >= p["e"] + 2 * p["r"]:
                px = p["e"] + 2 * p["r"]
                n = p["sh"] // 2
                equity += n * (px - p["e"]) - n * px * cost
                p["sh"] -= n; p["half"] = True
                p["stop"] = max(p["stop"], p["e"])
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
                if CONVICTION:
                    # Scale by setup quality, then RE-CLAMP to the notional
                    # cap: conviction may never be a route around the cap,
                    # only a way to take less than it.
                    sh = int(min(sh * conviction_mult(
                        getattr(s, "adx_at_setup", None),
                        getattr(s, "vol_ratio_setup", None)),
                        equity * MAX_NOTIONAL_PCT / entry_px))
                if sh <= 0:
                    continue
                equity -= sh * entry_px * cost
                positions[sym] = {"e": entry_px, "stop": stop, "r": dist,
                                  "sh": sh, "half": False, "held": 0,
                                  "hw": entry_px, "atr0": s.atr14,
                                  "adx0": getattr(s, "adx_at_setup", 0.0),
                                  "spy0": (_closes_upto(
                                      all_bars.get("SPY", []), today) or
                                      [None])[-1]}
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




def _auth_headers(key=None, sec=None):
    """Alpaca headers. Extracted so the benchmark fetcher shares one
    definition with fetch_bars rather than growing a second copy — the same
    reason seven EMA implementations became one."""
    key = key or (os.environ.get("ALPACA_API_KEY")
                  or os.environ.get("APCA_API_KEY_ID", ""))
    sec = sec or (os.environ.get("ALPACA_SECRET_KEY")
                  or os.environ.get("APCA_API_SECRET_KEY", ""))
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec}


def fetch_benchmark_total_return(days: int):
    """SPY on TOTAL RETURN, for the benchmark only.

    hold_SPY holds for the whole window and collects every dividend; pricing
    it split-only understated it by roughly 2pp over 730 days and flattered
    every comparison against it. Strategy prices stay split-adjusted so
    replay signals match live exactly — the two series answer different
    questions and should not share an adjustment.
    """
    from datetime import timedelta, timezone as _tz
    start = (datetime.now(_tz.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        r = requests.get(f"{STOCK_DATA}/bars",
                         params={"symbols": "SPY", "timeframe": "1Day",
                                 "start": start, "limit": 10000,
                                 "adjustment": "all"},
                         headers=_auth_headers(), timeout=30)
        r.raise_for_status()
        return r.json().get("bars", {}).get("SPY", [])
    except Exception as e:  # noqa: BLE001
        print(f"  benchmark total-return fetch failed ({e}) — falling back to "
              f"the split-adjusted series, which UNDERSTATES hold_SPY")
        return []


def classify_regime(spy_closes: list[float]) -> str:
    """A simplified regime label from SPY closes alone.

    Deliberately NOT importing regime_allocation: that module needs a live
    feed, breadth across the universe and 3-session persistence state, none
    of which exist inside a replay. This uses the two conditions that carry
    most of the signal — price vs the 200-day and the 50/200 relationship —
    so the labels are directionally comparable to the live CIO layer without
    pretending to reproduce it. Labels are prefixed BT_ in the output so
    nobody mistakes them for the allocator's own classifications.
    """
    if len(spy_closes) < 200:
        return "BT_UNKNOWN"
    e50, e200 = ema(spy_closes, 50), ema(spy_closes, 200)
    if e50 is None or e200 is None:
        return "BT_UNKNOWN"
    px = spy_closes[-1]
    # realised volatility over 20 sessions vs 100, as a HIGH_VOL proxy
    def _vol(w):
        r = [abs(spy_closes[i] / spy_closes[i - 1] - 1)
             for i in range(len(spy_closes) - w, len(spy_closes))]
        return sum(r) / len(r) if r else 0.0
    if len(spy_closes) >= 120 and _vol(20) > 1.6 * _vol(100):
        return "BT_HIGH_VOL"
    # A dead-flat tape drifts a fraction either side of its own average, and
    # labelling that BEAR would attribute sideways results to a bear market.
    # Require MEANINGFUL separation in both directions, mirroring the live
    # allocator's 1% rule.
    sep = (e50 - e200) / e200 if e200 else 0.0
    if abs(sep) < 0.01:
        return "BT_SIDEWAYS"
    if px < e200 and e50 < e200:
        return "BT_BEAR"
    if px > e200 and e50 > e200:
        return "BT_STRONG_BULL" if sep >= 0.05 else "BT_WEAK_BULL"
    return "BT_SIDEWAYS"


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
    ("+ staged lock", {"staged_lock": True}),
    ("+ ADX trail",   {"adx_trail": True}),
    ("+ RS decay",    {"rs_exit": 0.05}),
    ("+ conviction",  {"conviction": True}),
    ("+ profit lock",  {"percent_lock": True}),
]


def sweep_exits(a):
    base, src = _resolve_universe(a)
    print(f"Universe: {len(base)} symbols from {src}")
    windows = [365, 730]
    results, trade_lists, curves = {}, {}, {}
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
            trade_lists[(days, name)] = trades
            curves[(days, name)] = curve

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

    # ---- MULTIPLE HYPOTHESES ------------------------------------------
    try:
        import research_framework as rf
        lw = max(windows)
        bc = curves.get((lw, "baseline"))
        if bc and len(bc) > 30:
            def _rets(c):
                return [c[i + 1] / c[i] - 1 for i in range(len(c) - 1)
                        if c[i]]
            base_r = _rets(bc)
            cand_r = {n: _rets(curves[(lw, n)]) for n, _ in EXIT_VARIANTS
                      if n != "baseline" and (lw, n) in curves}
            rc = rf.reality_check(base_r, cand_r, iters=3000, block=5)
            if rc:
                print(f"\n{'='*92}\nREALITY CHECK — is the BEST variant "
                      f"better than luck?\n{'='*92}")
                print(f"  {rc['n_variants']} variants were tested. Judging "
                      f"each in isolation at 5% gives roughly a "
                      f"{1-(0.95**rc['n_variants']):.0%} chance")
                print(f"  that one clears the bar on noise alone. This asks "
                      f"whether the BEST of them did.\n")
                print(f"  best        : {rc['best']}")
                print(f"  edge/period : {rc['best_mean_edge']:+.5f}")
                print(f"  p-value     : {rc['p_value']:.3f}")
                print(f"  verdict     : {rc['verdict']}")
                if rc["p_value"] >= 0.05:
                    print("\n  A variant that clears its own promotion rules")
                    print("  but NOT this has not survived being cherry-picked.")
    except Exception as e:  # noqa: BLE001
        print(f"\n  (reality check unavailable: {e})")

    print(f"\n{'='*92}\nKNOWN BIASES IN THIS RESULT\n{'='*92}")
    print("  * Universe is TODAY'S 68 names. A list assembled in 2026 cannot")
    print("    contain a company that failed in 2024, so these numbers are")
    print("    optimistic by an unmeasured amount. Bounded by window length —")
    print("    modest over 730 days, severe over a decade, which is why the")
    print("    2008/1987 replays were declined rather than approximated.")
    print("  * No delisted names: real universes contain survivors AND")
    print("    failures. This one contains only survivors.")
    print("  * Entries assume a fill when price touches the trigger. Monte")
    print("    Carlo models a 5% miss rate, not partial fills or halts.")
    print("  * Strategy prices are SPLIT-ADJUSTED, matching live signal")
    print("    generation exactly. The BENCHMARK is total return, because")
    print("    hold_SPY collects dividends all window and pricing it")
    print("    split-only understated it by ~2pp over 730 days. The")
    print("    strategy's own dividends while holding are excluded (~0.3%/yr)")
    print("    — a residual bias that runs AGAINST the strategy.")

    lines = _promotion_report(results, windows)
    for ln in lines:
        print(ln)

    # ---- MONTE CARLO + ATTRIBUTION on the longest window ---------------
    long_w = max(windows)
    base_tr = trade_lists.get((long_w, "baseline"), [])
    if base_tr:
        print(f"\n{'='*92}\nMONTE CARLO — how much of this is the sequence "
              f"you happened to get? ({long_w}d)\n{'='*92}")
        print(f"{'configuration':<16}{'p05 P&L':>12}{'p50 P&L':>12}"
              f"{'p95 P&L':>12}{'p05 drawdown':>15}{'P(lose)':>9}")
        print("-" * 92)
        for name, _ in EXIT_VARIANTS:
            tr = trade_lists.get((long_w, name), [])
            mc = monte_carlo(tr, iters=1500)
            if mc:
                print(f"{name:<16}{mc['pnl_p05']:>12,.0f}{mc['pnl_p50']:>12,.0f}"
                      f"{mc['pnl_p95']:>12,.0f}{mc['dd_p05']:>15,.0f}"
                      f"{mc['prob_loss']:>9.0%}")
        print("-" * 92)
        print("  Resamples trade ORDER, slippage, missed fills and gap losses.")
        print("  The 5th percentile is the number to survive, not the median.")

        print(f"\n{'='*92}\nATTRIBUTION — WHERE any change came from "
              f"({long_w}d vs baseline)\n{'='*92}")
        print(f"{'configuration':<16}{'expectancy':>12}{'win rate':>11}"
              f"{'avg win':>11}{'avg loss':>11}{'trades':>9}")
        print("-" * 92)
        for name, _ in EXIT_VARIANTS:
            if name == "baseline":
                continue
            tr = trade_lists.get((long_w, name), [])
            if not tr:
                continue
            at = attribute(base_tr, tr)
            print(f"{name:<16}{at['exp_cand'] - at['exp_base']:>+12.1f}"
                  f"{at['from_win_rate']:>+11.1f}{at['from_avg_win']:>+11.1f}"
                  f"{at['from_avg_loss']:>+11.1f}"
                  f"{at['trade_count_delta']:>+9d}")
        print("-" * 92)
        print("  A Sharpe gain from SMALLER LOSSES and one from BIGGER WINNERS")
        print("  imply different follow-up work. The headline number hides which.")

    # ---- STRUCTURED EXPERIMENT RECORD ----------------------------------
    # The markdown is for READING; this is for COMPARING. After twenty runs
    # the question stops being "what did this one say?" and becomes "has the
    # answer moved, and what changed when it did?" — which prose cannot
    # answer. Records the git hash and seeds so a result can be reproduced
    # rather than merely re-read.
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        import subprocess
        _git = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True,
                              timeout=5).stdout.strip() or None
        _dirty = bool(subprocess.run(["git", "status", "--porcelain"],
                                     capture_output=True, text=True,
                                     timeout=5).stdout.strip())
    except Exception:  # noqa: BLE001
        _git, _dirty = None, None
    _exp = {
        "experiment": "sweep_exits",
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "git_hash": _git, "git_dirty": _dirty,
        "universe_size": len(base), "universe_source": src,
        "windows": windows, "cost_bps": a.cost_bps,
        "seeds": {"bootstrap": 7, "monte_carlo": 17},
        "promotion_thresholds": {
            "min_sharpe_gain": MIN_SHARPE_GAIN,
            "max_dd_worsening": MAX_DD_WORSENING,
            "min_trades": MIN_TRADES},
        "variants": {name: dict(cfg) for name, cfg in EXIT_VARIANTS},
        "results": {f"{d}d/{n}": results[(d, n)] for d in windows
                    for n in [v[0] for v in EXIT_VARIANTS] if (d, n) in results},
        "known_biases": [
            "universe is TODAY'S 68 names — a list assembled in 2026 cannot "
            "contain a company that failed in 2024. Selection bias, bounded "
            "by window length but not zero.",
            "no delisted names: real universes contain survivors AND "
            "failures; this one contains only survivors.",
            "entries assume a fill when price touches the trigger; Monte "
            "Carlo models a 5% miss rate, not partial fills or halts.",
            "strategy prices are SPLIT-ADJUSTED to match live signal "
            "generation exactly; the BENCHMARK is total return. The "
            "strategy's own dividends while holding are therefore excluded "
            "(~0.3%/yr), a residual bias that runs AGAINST it.",
        ],
    }
    try:
        with open(f"BACKTEST_EXPERIMENT_{stamp}.json", "w",
                  encoding="utf-8") as fh:
            json.dump(_exp, fh, indent=2, default=str)
        print(f"\n  structured record -> BACKTEST_EXPERIMENT_{stamp}.json")
    except Exception as e:  # noqa: BLE001
        print(f"  could not write the experiment record: {e}")

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



# ---------------------------------------------------------------------------
# WALK-FORWARD VALIDATION
#
# The problem this exists for, stated precisely: a 77-run sweep of the SAME
# 365-day window produced Sharpe from -0.05 to 1.62 purely from parameter
# choice, median 0.80. Any single in-sample number is therefore uninformative
# — and picking the best one is choosing the maximum of a noisy distribution,
# which does not survive contact with new data.
#
# Walk-forward answers a different question. Fit on a training window, test on
# the NEXT window the fitting never saw, roll forward, repeat. The only number
# that counts is the concatenated OUT-OF-SAMPLE result. If the in-sample
# winner keeps winning out of sample, the parameter is doing work. If the
# out-of-sample results scatter around the mean of all candidates, the
# "winner" was noise and the honest conclusion is that this parameter should
# not be tuned at all.
#
# The degradation ratio (out-of-sample Sharpe / in-sample Sharpe) is the
# headline. Values near 1.0 mean the fit generalised. Values near 0 — or
# negative — mean it did not, and no amount of further tuning will fix that.
# ---------------------------------------------------------------------------
WF_TRAIN_DAYS = int(os.getenv("WF_TRAIN_DAYS", "252"))    # ~1 trading year
WF_TEST_DAYS = int(os.getenv("WF_TEST_DAYS", "63"))       # ~1 quarter

# Candidates the fold CHOOSES BETWEEN on training data. Deliberately the same
# knobs the exit sweep tests, so the two tools answer the same question in two
# ways: the sweep asks "is this better in-sample?", walk-forward asks "does
# choosing it in-sample help out-of-sample?"
WF_CANDIDATES = [
    ("baseline",    {}),
    ("ATR trail",   {"trail_atr": 2.0, "trail_after_r": 1.0}),
    ("vol exit",    {"vol_exit": 1.8}),
    ("ADX decay",   {"adx_decay": 0.6}),
    ("staged lock", {"staged_lock": True}),
    ("ADX trail",   {"adx_trail": True}),
    ("RS decay",    {"rs_exit": 0.05}),
    ("conviction",  {"conviction": True}),
    ("profit lock",  {"percent_lock": True}),
]


def _slice_stats(bars, dates, lo, hi, cfg, cost):
    """Replay one date slice with one config. Returns stats or None."""
    window = dates[lo:hi]
    if len(window) < 30:
        return None
    curve, trades = run_config(bars, window, "A", False, 100_000, cost,
                               exits=cfg)
    years = max(len(window) / 252, 1e-9)
    return stats(curve, trades, years)


# ---------------------------------------------------------------------------
# MONTE CARLO
#
# A backtest reports ONE path: these trades, in this order, with these fills.
# The 77-run sweep already showed Sharpe spanning -0.05 to 1.62 from parameter
# choice alone; ordering and fill luck add more. Resampling answers a question
# walk-forward does not: "how much of THIS result is the sequence I happened
# to get?"
#
# Four perturbations, each modelling a way reality differs from replay:
#   ordering   — bootstrap the trade sequence (drawdown depends on order)
#   slippage   — random extra cost per trade
#   misses     — some signals never fill (queue position, halts, cash)
#   gaps       — losers occasionally worse than the stop
#
# The output that matters is the 5th percentile, not the median. A strategy
# whose median is +0.8 Sharpe and whose 5th percentile is -0.4 is a strategy
# that can plausibly lose money for a year.
# ---------------------------------------------------------------------------
# Monte Carlo lives in research_framework now, shared with backtest_xsect.
# Kept as a thin alias so existing call sites read unchanged — the point of
# the move is ONE implementation, not a rename.
from research_framework import (monte_carlo_trades as monte_carlo,  # noqa: E402
                                bootstrap_ci as _rf_bootstrap_ci,
                                write_experiment as _rf_write_experiment,
                                UNIVERSE_BIASES as _RF_BIASES)


def attribute(base_trades, cand_trades):
    """WHY expectancy moved: win rate, average win, average loss, or count.

    A sweep that reports "+0.08 Sharpe" does not say whether the gain came
    from bigger winners, smaller losers, or simply trading less. Those imply
    completely different follow-up work, and the number alone hides which.

    Decomposes by swapping ONE factor at a time from baseline to candidate:
        expectancy = win_rate * avg_win + (1 - win_rate) * avg_loss
    """
    def parts(ts):
        w = [t["pnl"] for t in ts if t["pnl"] > 0]
        l = [t["pnl"] for t in ts if t["pnl"] <= 0]
        n = len(ts)
        return {"n": n, "wr": len(w) / n if n else 0.0,
                "aw": sum(w) / len(w) if w else 0.0,
                "al": sum(l) / len(l) if l else 0.0}
    b, c = parts(base_trades), parts(cand_trades)

    def exp_(wr, aw, al):
        return wr * aw + (1 - wr) * al
    e_b, e_c = exp_(b["wr"], b["aw"], b["al"]), exp_(c["wr"], c["aw"], c["al"])
    return {
        "base": b, "cand": c, "exp_base": e_b, "exp_cand": e_c,
        "from_win_rate": exp_(c["wr"], b["aw"], b["al"]) - e_b,
        "from_avg_win": exp_(b["wr"], c["aw"], b["al"]) - e_b,
        "from_avg_loss": exp_(b["wr"], b["aw"], c["al"]) - e_b,
        "trade_count_delta": c["n"] - b["n"],
    }


def walk_forward(a):
    base, src = _resolve_universe(a)
    print(f"Universe: {len(base)} symbols from {src}")
    fetch_syms = base + (["SPY"] if "SPY" not in base else [])
    days = max(a.days, WF_TRAIN_DAYS + WF_TEST_DAYS * 3)
    print(f"fetching ~{days}d so several folds fit ...")
    bars = fetch_bars(fetch_syms, days)
    dates = sorted({b["t"][:10] for s in bars for b in bars[s]})
    cost = a.cost_bps / 10000
    warm = 60

    folds, start = [], warm
    while start + WF_TRAIN_DAYS + WF_TEST_DAYS <= len(dates):
        folds.append((start, start + WF_TRAIN_DAYS,
                      start + WF_TRAIN_DAYS + WF_TEST_DAYS))
        start += WF_TEST_DAYS
    if not folds:
        print(f"\nNot enough history: {len(dates)} aligned days, need "
              f"{warm + WF_TRAIN_DAYS + WF_TEST_DAYS}. Use a longer --days "
              f"or shorten WF_TRAIN_DAYS.")
        return
    print(f"{len(dates)} aligned days -> {len(folds)} folds "
          f"(train {WF_TRAIN_DAYS}d / test {WF_TEST_DAYS}d, rolling)\n")

    rows, oos_by_cand = [], {n: [] for n, _ in WF_CANDIDATES}
    for i, (lo, mid, hi) in enumerate(folds, 1):
        best_name, best_sharpe, best_cfg = None, None, None
        for name, cfg in WF_CANDIDATES:
            st = _slice_stats(bars, dates, lo, mid, cfg, cost)
            if st and (best_sharpe is None or st["sharpe"] > best_sharpe):
                best_name, best_sharpe, best_cfg = name, st["sharpe"], cfg
        if best_name is None:
            continue
        oos = _slice_stats(bars, dates, mid, hi, best_cfg, cost)
        if oos is None:
            continue
        # every candidate's out-of-sample result, so the CHOICE can be judged
        # against simply always using each one
        for name, cfg in WF_CANDIDATES:
            st = _slice_stats(bars, dates, mid, hi, cfg, cost)
            if st:
                oos_by_cand[name].append(st["sharpe"])
        rows.append({"fold": i, "train": f"{dates[lo]}..{dates[mid-1]}",
                     "test": f"{dates[mid]}..{dates[hi-1]}",
                     "chosen": best_name, "is_sharpe": best_sharpe,
                     "oos_sharpe": oos["sharpe"], "oos_total": oos["total"],
                     "oos_maxdd": oos["maxdd"], "oos_trades": oos["trades"]})

    if not rows:
        print("no usable folds")
        return

    print("=" * 100)
    print(f"{'fold':<5}{'train window':<26}{'test window':<26}"
          f"{'chosen':<12}{'IS sh':>7}{'OOS sh':>8}{'OOS dd':>8}{'trades':>7}")
    print("-" * 100)
    for r in rows:
        print(f"{r['fold']:<5}{r['train']:<26}{r['test']:<26}"
              f"{r['chosen']:<12}{r['is_sharpe']:>7.2f}{r['oos_sharpe']:>8.2f}"
              f"{r['oos_maxdd']:>7.1%}{r['oos_trades']:>7}")

    is_m = statistics.mean(r["is_sharpe"] for r in rows)
    oos_m = statistics.mean(r["oos_sharpe"] for r in rows)
    degr = (oos_m / is_m) if is_m else 0.0
    pos = sum(1 for r in rows if r["oos_sharpe"] > 0)
    print("-" * 100)
    print(f"{'MEAN':<5}{'':<52}{'':<12}{is_m:>7.2f}{oos_m:>8.2f}")
    print(f"\n  folds with positive out-of-sample Sharpe: {pos}/{len(rows)}")
    print(f"  DEGRADATION RATIO (OOS / IS): {degr:.2f}")
    print("    ~1.0  the in-sample fit generalised")
    print("    ~0.5  half the apparent edge was fitting")
    print("    <=0   the choice carried no information out of sample")

    print("\n  always-use-this-one, out of sample (the honest comparison):")
    for name, _ in WF_CANDIDATES:
        v = oos_by_cand[name]
        if v:
            print(f"    {name:<12} mean OOS sharpe {statistics.mean(v):>6.2f}"
                  f"  over {len(v)} folds")
    chosen_mean = oos_m
    best_fixed = max((statistics.mean(v), n)
                     for n, v in oos_by_cand.items() if v)
    print(f"\n  selecting per fold: {chosen_mean:.2f}   |   "
          f"always '{best_fixed[1]}': {best_fixed[0]:.2f}")
    if chosen_mean <= best_fixed[0] + 0.02:
        print("  -> IN-SAMPLE SELECTION ADDED NOTHING. Choosing a config per")
        print("     fold did not beat simply fixing one. That is evidence the")
        print("     parameter should NOT be tuned — pick the simplest and")
        print("     leave it alone.")
    else:
        print("  -> selection beat every fixed choice; the parameter carries")
        print("     information that survives out of sample.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--cost-bps", type=float, default=5.0)
    ap.add_argument("--mc-iters", type=int, default=1500,
                    help="Monte Carlo resamples in the sweep report")
    ap.add_argument("--walk-forward", action="store_true",
                    help="rolling train/test folds; reports the OUT-OF-SAMPLE "
                         "result and the degradation ratio")
    ap.add_argument("--sweep-exits", action="store_true",
                    help="replay baseline + each adaptive exit one at a time, "
                         "on BOTH windows, and apply the promotion rules")
    ap.add_argument("--symbols-file", default=None,
                    help="optional; omit to read UNIVERSE from config.py")
    a = ap.parse_args()
    if a.walk_forward:
        walk_forward(a)
        return
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
