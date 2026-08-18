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

================================ VALIDATION STATUS ================================
NO REAL-DATA RUN HAS BEEN COMPLETED AS OF 2026-07-25.

This harness has NO synthetic mode -- it fetches live bars or exits. Any claim
that the sector cap or regime filter has been "validated" by this script is
unsupported unless it is accompanied by an actual results table containing
monoculture%, avg_sectors, Sharpe and maxdd for each variant.

Separately, and worth keeping straight: the sector cap IS confirmed working in
PRODUCTION -- the 2026-07-24 rotation logged
    sector-capped top3: MU[tech], CAT[industrials], UNP[transports]
    capped out: ARM(tech)
That is live evidence the mechanism fires. It is NOT evidence the cap improves
returns. This replay is what prices that question.

To run (from the repo directory, ALPACA_API_KEY / ALPACA_SECRET_KEY set):

    python backtest_xsect.py --days 730 --with-additions --regime
    python backtest_xsect.py --days 365 --with-additions --regime
===================================================================================
"""

from __future__ import annotations

import argparse
import math
import os
import statistics
import sys
import tool_guard

# LINE-BUFFER STDOUT (2026-08-07). Python block-buffers stdout when it is not
# a TTY — which in a container it never is — so output sits in a 4-8KB buffer
# until the buffer fills or the PROCESS EXITS.
#
# That was survivable until _park_if_service() was added to stop Railway
# restart-looping a completed run. Parking means the process never exits,
# which means the buffer never flushes: a backtest that ran correctly for
# three days and printed absolutely nothing. The fix for one problem created
# the other.
#
# Done in code rather than relying on PYTHONUNBUFFERED so it holds wherever
# this runs, including a shell that forgets to set it.
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except (AttributeError, ValueError):   # pragma: no cover - older/odd streams
    pass
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
        sys.exit(
            "\nNO API CREDENTIALS FOUND — cannot fetch real market data.\n\n"
            "  Set your PAPER keys (the same ones Railway uses):\n"
            "      export ALPACA_API_KEY=...\n"
            "      export ALPACA_SECRET_KEY=...\n"
            "  (APCA_API_KEY_ID / APCA_API_SECRET_KEY also work.)\n\n"
            "  This harness has NO synthetic mode by design — there is no\n"
            "  fallback that produces a number here. Fix the credentials.\n")
    h = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec}
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    out = {}
    for i in range(0, len(symbols), 50):
        chunk, page = symbols[i:i + 50], None
        while True:
            params = {"symbols": ",".join(chunk), "timeframe": "1Day",
                      "start": start, "limit": 10000,
                      # SPLIT-ONLY to match live signal generation; see the
                      # note in backtest_swing_v2.fetch_bars. Total return is
                      # correct for the BENCHMARK, not for the series the
                      # strategy's own indicators are computed from.
                      "adjustment": "split"}
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
           regime_on, sma_days=200, exit_pad=0):
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

        # ---- RANK BAND: hold a slipped leader instead of selling on rank N+1
        #
        # The live desk holds until a name falls PAST top_n + EXIT_RANK_PAD
        # (rank 5 today). This replay had no such concept — it rebalanced to
        # the fresh top-N every period, so it modelled a STRICTER rotation
        # than production and could not test the band at all.
        #
        # The thesis matters here: xsect's edge is that relative-strength
        # leadership PERSISTS. Selling a name the moment it slips from #3 to
        # #4 monetises rank noise, not persistence. Widening the band is the
        # xsect equivalent of removing swing's time stop — it stops the exit
        # rule from cutting the thing the strategy is supposed to capture.
        #
        # exit_pad=0 reproduces the old strict behaviour, so the comparison
        # is like-for-like.
        if exit_pad > 0 and ranked:
            _order = [t for _, t in ranked]
            _band = top_n + exit_pad
            for _t in held:
                if _t in target:
                    continue
                try:
                    if _order.index(_t) < _band:
                        target.add(_t)      # slipped, but still inside the band
                except ValueError:
                    pass                    # no longer ranked at all: let it go

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
        # Daily return series, for block resampling. Not printed — carried
        # so research_framework can ask how fragile the result is.
        "_returns": rets,
    }


def main(preset=None):
    """Run one window. Returns the rows dict so --both-windows can compare.

    `preset` lets the two-window wrapper reuse this entire function instead
    of duplicating the fetch/replay/report path — one code path, two calls,
    so the windows cannot drift apart.
    """
    # A research tool must never be a container entrypoint: it exits,
    # and Railway restarts anything that exits. See tool_guard.
    tool_guard.guard_entrypoint("backtest_xsect.py")
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols-file", default=None,
                    help="optional; omit to read UNIVERSE from config.py")
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--both-windows", action="store_true",
                    help="run 365 AND 730 and report only configurations that "
                         "hold in BOTH — the promotion rule, enforced")
    ap.add_argument("--top-n", type=int, default=3)
    ap.add_argument("--lookback", type=int, default=126)
    ap.add_argument("--skip", type=int, default=5)
    ap.add_argument("--cost-bps", type=float, default=5.0)
    ap.add_argument("--exit-pads", default="0,2,5,10",
                    help="rank bands to test: how far past top_n a holding "
                         "may slip before it is sold. 0 = strict rotation, "
                         "2 = the live desk, larger = more persistence")
    ap.add_argument("--with-additions", action="store_true",
                    help="also run the universe + ABBV MRK PGR CME DHI")
    ap.add_argument("--regime", action="store_true",
                    help="also run each variant under the SPY/200SMA gate")
    a = ap.parse_args()
    # The preset must land BEFORE anything reads a.days. It was applied
    # 29 lines later, after the fetch had already been sized — so the
    # "365-day window" fetched 730 days and both windows were identical.
    # A two-window test that silently runs one window twice is worse
    # than no two-window test, because it looks like corroboration.
    if preset is not None:
        a.days, a.both_windows = preset, False

    if a.symbols_file:
        base = [l.strip().upper() for l in open(a.symbols_file)
                 if l.strip() and not l.startswith("#")]
        _src = a.symbols_file
    else:
        try:
            from config import UNIVERSE
            base = [t.strip().upper() for t in UNIVERSE]
            _src = "config.UNIVERSE"
        except Exception as _e:  # noqa: BLE001
            sys.exit("No --symbols-file and could not import UNIVERSE from "
                     f"config.py ({_e}). Run from the repo directory.")
    print(f"universe: {len(base)} symbols from {_src}")
    fetch_syms = sorted(set(base) | set(UNIVERSE_ADDITIONS) | {"SPY"})
    print(f"fetching {len(fetch_syms)} symbols x {a.days}d ...")
    raw = fetch_bars(fetch_syms, a.days)
    px = {s2: closes_by_date(b) for s2, b in raw.items()}
    dates = sorted(set().union(*(set(c) for c in px.values())))
    spy = px.get("SPY", {})
    expected = int(a.days * 252 / 365)
    print(f"{len(dates)} trading days aligned (expected ~{expected} for "
          f"--days {a.days})")
    if len(dates) < 0.85 * expected:
        # `closes` never existed here — the dict is `px` (2026-08-04). This
        # is the SHORT-WINDOW diagnostic, so it only fires when the aligned
        # window is already suspiciously small: exactly the moment you most
        # need the diagnostic, and exactly when it would instead have raised
        # NameError. Found by an AST scan for names used but never assigned,
        # the same check added after the _alloc incident.
        counts = sorted(((len(px[s]), s) for s in px), key=lambda t: t[0])
        short = ", ".join(f"{s}:{n}" for n, s in counts[:6])
        print(f"\n*** WARNING: the aligned window is only {len(dates)}/"
              f"{expected} days. Dates are intersected across ALL symbols, so "
              f"one short history truncates everything — and CAGR is then "
              f"annualised from too little data (a 50% total over half a year "
              f"reads as 140% CAGR). Fewest bars: {short}. Treat CAGR as "
              f"unreliable here; total, Sharpe and maxdd remain valid.\n")
    print()

    universes = {"base": base}
    if a.with_additions:
        universes["base+adds"] = base + [t for t in UNIVERSE_ADDITIONS
                                        if t not in base]
    a.exit_pads = [int(x) for x in str(a.exit_pads).split(",") if x.strip()]
    rows = {}
    for uname, syms in universes.items():
        syms = [s2 for s2 in syms if s2 in px]
        for cap, cname in ((0, "uncapped"), (1, "cap1")):
            for reg in ([False, True] if a.regime else [False]):
                # Rank band swept alongside the cap. pad=0 is the strict
                # rotation this harness has always modelled; pad=2 matches
                # the LIVE desk; wider values test whether persistence pays.
                for pad in a.exit_pads:
                    label = (f"{uname}/{cname}"
                             + (f"/pad{pad}" if pad else "")
                             + ("/regime" if reg else ""))
                    rows[label] = replay(px, dates, syms, a.top_n, a.lookback,
                                         a.skip, cap, a.cost_bps / 10000, spy,
                                         reg, exit_pad=pad)
    # SPY benchmark — WARMUP-ALIGNED (2026-07-25 fix). replay() starts at
    # dates[lookback+skip+1]; timing the benchmark over the full fetch while
    # the strategies traded a much shorter window made "beats hold_SPY"
    # meaningless (250 benchmark days vs ~117 strategy days on a 365d fetch).
    warm = a.lookback + a.skip + 1
    replay_dates = dates[warm:]
    print(f"replay window: {len(replay_dates)} trading days after a "
          f"{warm}-day warmup — benchmark timed over the SAME window\n")
    sd = [spy[d] for d in replay_dates if d in spy]
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
    # keys prefixed "_" are carried for analysis, never tabulated
    print(f"{'variant':<26}" + "".join(f"{c:>14}" for c in cols))
    print("-" * 140)
    for name, st in rows.items():
        row = f"{name:<26}"
        for c in cols:
            v = st[c]
            row += (f"{v:>13.1%} " if c in ("total", "cagr", "maxdd")
                    else f"{v:>13.1f} ")
        print(row)
    # ---- ROBUSTNESS + PROMOTION, via the shared framework -------------
    try:
        import research_framework as rf
        print(f"\n{'='*140}\nMONTE CARLO — how fragile is each variant?"
              f"\n{'='*140}")
        print(f"{'variant':<26}{'p05 total':>14}{'p50 total':>14}"
              f"{'p95 total':>14}{'p05 drawdown':>16}{'P(lose)':>10}")
        print("-" * 140)
        mc = {}
        for name, st in rows.items():
            r = st.get("_returns")
            if not r:
                continue
            # BLOCK resampling, not iid: momentum trends and reverses in
            # runs, and breaking them understates drawdown in the dangerous
            # direction.
            m = rf.monte_carlo_returns(r, iters=1200, block=10)
            if m:
                mc[name] = m
                print(f"{name:<26}{m['p05']:>13.1%} {m['p50']:>13.1%} "
                      f"{m['p95']:>13.1%} {m['dd_p05']:>15.1%} "
                      f"{m['prob_loss']:>9.0%}")
        print("-" * 140)
        print("  Blocks of 10 trading days keep rotations intact. Judge on the")
        print("  5th percentile — the median is the outcome you hope for.")

        # PROMOTION RULES for THIS harness's question, which is not swing's.
        # Swing asks "promote this exit?"; xsect asks "does the cap help?" —
        # and the honest test is whether it buys diversification at an
        # acceptable cost, not whether it wins on return.
        # Keys are "<universe>/<cap>[/regime]" — e.g. "base/uncapped",
        # "base/cap1" — NOT bare "uncapped"/"cap1". The first version looked
        # up the bare names, found nothing, and the whole promotion block
        # SILENTLY did not run: no error, no output, just an absent section
        # nobody would notice was missing. Same fail-open shape as the RS
        # exit with no benchmark and the harness that skipped its own gates.
        def _pick(suffix):
            for k in rows:
                if k.endswith("/" + suffix) and "/regime" not in k:
                    return rows[k], k
            return None, None
        base, base_k = _pick("uncapped")
        cap, cap_k = _pick("cap1")
        if not (base and cap):
            print(f"\n  (cap comparison skipped: could not find both an "
                  f"uncapped and a cap1 variant among {sorted(rows)[:4]}...)")
        if base and cap:
            print(f"\n{'='*140}\nDOES THE SECTOR CAP EARN ITS COST?"
                  f"  ({base_k} vs {cap_k})\n{'='*140}")
            mono_cut = base["monoculture%"] - cap["monoculture%"]
            dd_gain = abs(base["maxdd"]) - abs(cap["maxdd"])
            sharpe_cost = cap["sharpe"] - base["sharpe"]
            ok, lines = rf.check_promotion([
                ("monoculture days reduced", mono_cut > 0,
                 f"{base['monoculture%']:.1f}% -> {cap['monoculture%']:.1f}%",
                 "any reduction"),
                ("max drawdown improved", dd_gain > 0,
                 f"{base['maxdd']:.1%} -> {cap['maxdd']:.1%}", "improvement"),
                ("Sharpe cost acceptable", sharpe_cost >= -0.30,
                 f"{sharpe_cost:+.2f}", ">= -0.30"),
            ])
            for ln in lines:
                print(ln)
            print(f"\n    VERDICT: {'the cap earns its cost' if ok else 'the cap costs more than it buys ON THIS WINDOW'}")
            print("    Note: a window dominated by one sector's rally is the")
            print("    hardest possible test for a diversification rule —")
            print("    trailing there is the cap working, not failing.")

        path = rf.write_experiment(
            "xsect",
            {"universe_size": len(syms), "days": a.days,
             "cost_bps": a.cost_bps,
             "results": {k: {c: v[c] for c in cols} for k, v in rows.items()},
             "monte_carlo": mc},
            rf.UNIVERSE_BIASES + [
                "equal weighting, not the live portfolio_manager sizing — "
                "replay concentration differs from production.",
                "instant daily rebalance: no partial turnover, participation "
                "limits or queue position.",
            ],
            seeds={"monte_carlo": 17})
        if path:
            print(f"\n  structured record -> {path}")
    except Exception as e:  # noqa: BLE001 — analysis must not break the run
        print(f"\n  (robustness analysis unavailable: {e})")

    print("\nHow to read: monoculture% is the share of days the uncapped "
          "top-3 was ONE sector three times over — the number the cap "
          "exists to kill. Judge cap1 on maxdd and Sharpe across BOTH "
          "windows (--days 730 and 365), not on total return in a "
          "single-sector rally, where trailing it is the cap working. "
          "regime variants sitting out days is visible in risk_off_days.")

    return rows


def _both_windows() -> None:
    """Run 365 and 730, then report only what holds in BOTH.

    The promotion rule this project already uses for swing requires a result
    to survive two independent windows, and for a reason worth restating:
    one window is one market. A rank band that looks best on 365 days of a
    tech melt-up and worst on 730 is not a better band — it is a band fitted
    to a rally.

    Doing it in one invocation matters because the alternative is running
    twice and eyeballing two tables, which is exactly where a config that
    won once gets promoted.
    """
    results = {}
    for days in (365, 730):
        print(f"\n{'#'*100}\n#  {days}-DAY WINDOW\n{'#'*100}")
        results[days] = main(preset=days) or {}

    print("\n" + "=" * 100)
    print("BOTH WINDOWS — a configuration must hold in each to be a candidate")
    print("=" * 100)
    print(f"{'configuration':<28}{'365 cagr':>10}{'365 shrp':>10}{'365 dd':>9}"
          f"{'730 cagr':>10}{'730 shrp':>10}{'730 dd':>9}{'verdict':>22}")
    print("-" * 100)
    common = sorted(set(results[365]) & set(results[730]))
    for k in common:
        if k == "hold_SPY":
            continue
        a1, a2 = results[365][k], results[730][k]
        ok = (a1.get("sharpe", -9) > 0 and a2.get("sharpe", -9) > 0
              and a1.get("cagr", -9) > 0 and a2.get("cagr", -9) > 0)
        stable = ok and abs(a1["sharpe"] - a2["sharpe"]) < 0.6
        verdict = ("holds in both" if stable else
                   "positive but unstable" if ok else "fails one window")
        print(f"{k:<28}{a1.get('cagr',0):>9.1%}{a1.get('sharpe',0):>10.2f}"
              f"{a1.get('maxdd',0):>9.1%}{a2.get('cagr',0):>9.1%}"
              f"{a2.get('sharpe',0):>10.2f}{a2.get('maxdd',0):>9.1%}"
              f"{verdict:>22}")
    print("-" * 100)
    print("  'positive but unstable' means the Sharpe moved more than 0.6")
    print("  between windows — the configuration works, but not consistently")
    print("  enough to trust the number from either one.")


if __name__ == "__main__":
    import sys as _sys
    if "--both-windows" in _sys.argv:
        _both_windows()
    else:
        main()
    tool_guard.park_when_done("backtest_xsect.py")
