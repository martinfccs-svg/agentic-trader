"""
backtest_xsect.py -- replay the cross-sectional rotation over history and
measure what the sector cap (and optionally the regime filter and the five
universe additions) would have changed. LOCAL TOOL — never deploys.

Answers, from data instead of argument:
  1. What does cap=1 cost in raw momentum vs uncapped?  (it concentrates
     less, so in a single-sector melt-up it SHOULD underperform — the
     question is by how much, against how much drawdown it saves)
  2. How often was the uncapped top-3 all one sector?
  3. Do the five additions (ABBV MRK PGR CME DHI) change rankings enough
     to matter?
  4. Does the SPY/200-SMA regime gate help or just sit out rallies?

USAGE (same env keys as everything else: ALPACA_* or APCA_*):
  python backtest_xsect.py --symbols-file universe.txt --days 730
  python backtest_xsect.py --symbols-file universe.txt --days 730 \
      --with-additions --regime

Mechanics mirror the live engine: rank by trailing return over
--lookback (default 126) skipping the most recent --skip days (default 5),
hold the top --top-n (default 3) equal-weight, re-rank daily (live gate is
once/day at 10:00 ET). Costs: --cost-bps per side on turnover (default 5).
No ATR stops in the replay — both variants omit them equally, so the
COMPARISON is fair even though absolute numbers are gentler than live.

Honesty: history, not prophecy. If cap=1 looks worse over a window that was
one long AI rally, that is the cap doing its job in the one regime where
diversification is pure cost; judge it on drawdown and on multi-window
consistency, not one number. Not financial advice.
"""

from __future__ import annotations

import argparse
import math
import os
import statistics
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sector_map import sector_of, UNIVERSE_ADDITIONS  # noqa: E402

try:
    import requests
except ImportError:
    requests = None

STOCK_DATA = "https://data.alpaca.markets/v2/stocks"


def fetch_bars(symbols, days):
    if requests is None:
        sys.exit("pip install requests")
    key = os.environ.get("ALPACA_API_KEY") or os.environ.get("APCA_API_KEY_ID")
    sec = (os.environ.get("ALPACA_SECRET_KEY")
           or os.environ.get("APCA_API_SECRET_KEY"))
    if not key or not sec:
        sys.exit("Set ALPACA_API_KEY / ALPACA_SECRET_KEY (or APCA_* names).")
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
            for s2, bs in j.get("bars", {}).items():
                out.setdefault(s2, []).extend(bs)
            page = j.get("next_page_token")
            if not page:
                break
            time.sleep(0.25)
    return {s2: b for s2, b in out.items()}


def closes_by_date(bars):
    return {b["t"][:10]: float(b["c"]) for b in bars}


def select(ranked, top_n, cap):
    """Same walk as the shipped xsection.py loop."""
    if cap <= 0:
        return ranked[:top_n]
    sel, used = [], {}
    for ret, t in ranked:
        if len(sel) >= top_n:
            break
        s2 = sector_of(t)
        if used.get(s2, 0) >= cap:
            continue
        used[s2] = used.get(s2, 0) + 1
        sel.append((ret, t))
    return sel


def replay(px, dates, syms, top_n, lookback, skip, cap, cost, spy_close,
           regime_on, sma_days=200):
    warm = lookback + skip + 1
    equity = [1.0]
    held: set[str] = set()
    turnover_units = 0.0
    sector_mix = Counter()      # distinct-sector count of daily holdings
    monoculture_days = 0
    days_counted = 0
    risk_off_days = 0

    for di in range(warm, len(dates) - 1):
        d, d1 = dates[di], dates[di + 1]
        # regime check on SPY closes up to d
        allowed = True
        if regime_on:
            hist = [spy_close[x] for x in dates[:di + 1] if x in spy_close]
            if len(hist) >= sma_days:
                allowed = hist[-1] > sum(hist[-sma_days:]) / sma_days
        if not allowed:
            risk_off_days += 1

        ranked = []
        for t in syms:
            c = px[t]
            if d not in c:
                continue
            past_i = di - skip - lookback
            skip_i = di - skip
            dp, ds = dates[past_i], dates[skip_i]
            if dp in c and ds in c and c[dp] > 0:
                ranked.append((c[ds] / c[dp] - 1, t))
        ranked.sort(reverse=True)
        target = {t for _, t in select(ranked, top_n, cap)} if ranked else set()
        if regime_on and not allowed:
            target = held        # rotation skipped whole, per live semantics

        churn = len(held ^ target)
        turnover_units += churn / max(top_n, 1)
        held = target

        if held:
            secs = {sector_of(t) for t in held}
            sector_mix[len(secs)] += 1
            if len(secs) == 1 and len(held) == top_n:
                monoculture_days += 1
            days_counted += 1
            rets = [px[t][d1] / px[t][d] - 1 for t in held
                    if d1 in px[t] and d in px[t]]
            r = sum(rets) / len(held) if rets else 0.0
        else:
            r = 0.0
        cost_hit = (churn / max(top_n, 1)) * cost
        equity.append(equity[-1] * (1 + r) * (1 - cost_hit))

    n = len(equity) - 1
    years = n / 252
    rets = [equity[i + 1] / equity[i] - 1 for i in range(n)]
    sd = statistics.pstdev(rets) or 1e-12
    peak, dd = equity[0], 0.0
    for v in equity:
        peak = max(peak, v)
        dd = min(dd, v / peak - 1)
    return {
        "total": equity[-1] - 1,
        "cagr": equity[-1] ** (1 / years) - 1 if years and equity[-1] > 0 else 0,
        "sharpe": statistics.mean(rets) / sd * math.sqrt(252),
        "maxdd": dd,
        "turnover/yr": turnover_units / years if years else 0,
        "avg_sectors": (sum(k * v for k, v in sector_mix.items())
                        / days_counted) if days_counted else 0,
        "monoculture%": monoculture_days / days_counted * 100
        if days_counted else 0,
        "risk_off_days": risk_off_days,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols-file", required=True)
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--top-n", type=int, default=3)
    ap.add_argument("--lookback", type=int, default=126)
    ap.add_argument("--skip", type=int, default=5)
    ap.add_argument("--cost-bps", type=float, default=5.0)
    ap.add_argument("--with-additions", action="store_true",
                    help="also run the universe + ABBV MRK PGR CME DHI")
    ap.add_argument("--regime", action="store_true",
                    help="also run each variant under the SPY/200SMA gate")
    a = ap.parse_args()

    base = [l.strip().upper() for l in open(a.symbols_file)
            if l.strip() and not l.startswith("#")]
    fetch_syms = sorted(set(base) | set(UNIVERSE_ADDITIONS) | {"SPY"})
    print(f"fetching {len(fetch_syms)} symbols x {a.days}d ...")
    raw = fetch_bars(fetch_syms, a.days)
    px = {s2: closes_by_date(b) for s2, b in raw.items()}
    dates = sorted(set().union(*(set(c) for c in px.values())))
    spy = px.get("SPY", {})
    print(f"{len(dates)} trading days\n")

    universes = {"base": base}
    if a.with_additions:
        universes["base+adds"] = base + [t for t in UNIVERSE_ADDITIONS
                                        if t not in base]
    rows = {}
    for uname, syms in universes.items():
        syms = [s2 for s2 in syms if s2 in px]
        for cap, cname in ((0, "uncapped"), (1, "cap1")):
            for reg in ([False, True] if a.regime else [False]):
                label = f"{uname}/{cname}" + ("/regime" if reg else "")
                rows[label] = replay(px, dates, syms, a.top_n, a.lookback,
                                     a.skip, cap, a.cost_bps / 10000, spy,
                                     reg)
    # SPY benchmark
    sd = [spy[d] for d in dates if d in spy]
    if len(sd) > 2:
        eq = [v / sd[0] for v in sd]
        rets = [eq[i + 1] / eq[i] - 1 for i in range(len(eq) - 1)]
        peak, dd = eq[0], 0.0
        for v in eq:
            peak = max(peak, v)
            dd = min(dd, v / peak - 1)
        years = len(rets) / 252
        rows["hold_SPY"] = {
            "total": eq[-1] - 1,
            "cagr": eq[-1] ** (1 / years) - 1 if years else 0,
            "sharpe": statistics.mean(rets)
            / (statistics.pstdev(rets) or 1e-12) * math.sqrt(252),
            "maxdd": dd, "turnover/yr": 0, "avg_sectors": 0,
            "monoculture%": 0, "risk_off_days": 0}

    cols = ["total", "cagr", "sharpe", "maxdd", "turnover/yr",
            "avg_sectors", "monoculture%", "risk_off_days"]
    print(f"{'variant':<26}" + "".join(f"{c:>14}" for c in cols))
    print("-" * 140)
    for name, st in rows.items():
        row = f"{name:<26}"
        for c in cols:
            v = st[c]
            row += (f"{v:>13.1%} " if c in ("total", "cagr", "maxdd")
                    else f"{v:>13.1f} ")
        print(row)
    print("\nHow to read: monoculture% is the share of days the uncapped "
          "top-3 was ONE sector three times over — the number the cap "
          "exists to kill. Judge cap1 on maxdd and Sharpe across BOTH "
          "windows (--days 730 and 365), not on total return in a "
          "single-sector rally, where trailing it is the cap working. "
          "regime variants sitting out days is visible in risk_off_days.")


if __name__ == "__main__":
    main()
