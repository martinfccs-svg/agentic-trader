"""beartrend_review.py — did the shadow observations predict anything?

LOCAL TOOL. Reads the shadow repository written by beartrend_scoring, walks
prices forward, and computes what each observation WOULD have produced as a
short. This is the evidence that decides whether the short-side execution
stack — borrow checks, side-aware position model, inverted exits, gross/net
exposure tracking, reconcile changes — is worth building at all.

    railway run cat /data/beartrend_observations.jsonl > beartrend_obs.jsonl
    python beartrend_review.py                     # needs ALPACA_* keys
    python beartrend_review.py --horizons 1,5,10,20

THE SIGN CONVENTION, stated loudly because getting it backwards would
silently invert every conclusion:

    These are SHORTS. A price DECLINE is favourable.

    return_R = (entry - price_later) / risk_per_share      profit if price fell
    MFE_R    = (entry - lowest_low)  / risk_per_share      best it ever got
    MAE_R    = (highest_high - entry)/ risk_per_share      worst it ever got
    stopped  = highest_high >= stop                        stop is ABOVE entry

WHAT DECIDES THE QUESTION

  expectancy > 0 after the stop is honoured, across enough observations, in
  more than one bearish stretch. Anything less and the infrastructure is not
  justified — a strategy that needs a short-selling stack to lose money more
  efficiently is not worth the engineering.

  Score calibration matters as much as expectancy: if high-scoring
  observations do not outperform low-scoring ones, the ranking is noise and
  the desk cannot allocate intelligently even if the average edge is real.

SAMPLE SIZE. Bear regimes are rare; this repository will fill slowly and in
bursts. ~50 observations across at least two distinct bearish stretches
before the numbers mean anything. A single bad month is one observation of
one market, not evidence.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import tool_guard
from collections import defaultdict
from typing import Optional
from datetime import datetime, timedelta

try:
    import requests
except ImportError:
    requests = None

STOCK_DATA = "https://data.alpaca.markets/v2/stocks"


def _auth():
    key = os.environ.get("ALPACA_API_KEY") or os.environ.get("APCA_API_KEY_ID")
    sec = (os.environ.get("ALPACA_SECRET_KEY")
           or os.environ.get("APCA_API_SECRET_KEY"))
    if not key or not sec:
        sys.exit("\nNO API CREDENTIALS — this walks prices forward from each "
                 "observation, which needs bars.\n"
                 "  export ALPACA_API_KEY=...  ALPACA_SECRET_KEY=...\n")
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec}


def fetch_bars(symbols, start, end):
    if requests is None:
        sys.exit("pip install requests")
    out = defaultdict(list)
    for i in range(0, len(symbols), 50):
        chunk, page = symbols[i:i + 50], None
        while True:
            p = {"symbols": ",".join(chunk), "timeframe": "1Day",
                 "start": start, "end": end, "limit": 10000,
                 "adjustment": "split"}
            if page:
                p["page_token"] = page
            r = requests.get(f"{STOCK_DATA}/bars", params=p,
                             headers=_auth(), timeout=30)
            r.raise_for_status()
            j = r.json()
            for s, bars in j.get("bars", {}).items():
                out[s].extend(bars)
            page = j.get("next_page_token")
            if not page:
                break
    return out


EXIT_POLICIES = ("atr_stop_hold", "ema20_exit", "trail_atr",
                 "breakeven_1R", "time_5d", "time_10d")


def simulate_exits(entry, stop, risk, hist_closes, fwd, atr14):
    """What each candidate EXIT POLICY would have returned, in R.

    The review previously assumed ONE exit — a 2.5xATR stop held to the
    horizon — and therefore measured that exit as much as it measured the
    signal. Comparing policies on the SAME observations separates the two,
    and does it before any exit engine is written rather than after.

    SHORT convention throughout: profit when price FALLS, the stop sits
    ABOVE entry, and a trailing stop ratchets DOWN.
    """
    out = {}
    if not fwd or risk <= 0:
        return out
    closes = list(hist_closes)
    e20 = _ema(closes, 20)

    # 1. current policy: fixed stop, otherwise hold to the horizon
    r = None
    for x in fwd:
        if x["h"] >= stop:
            r = -1.0
            break
    out["atr_stop_hold"] = r if r is not None else (entry - fwd[-1]["c"]) / risk

    # 2. cover on a close back above the 20-EMA (trend recovered)
    r = None
    for x in fwd:
        if x["h"] >= stop:
            r = -1.0
            break
        closes.append(x["c"])
        e20 = _ema(closes, 20)
        if e20 is not None and x["c"] > e20:
            r = (entry - x["c"]) / risk
            break
    out["ema20_exit"] = r if r is not None else (entry - fwd[-1]["c"]) / risk

    # 3. trailing ATR stop, ratcheting DOWN from the lowest low
    r, low, trail = None, entry, stop
    for x in fwd:
        if x["h"] >= trail:
            r = (entry - trail) / risk
            break
        low = min(low, x["l"])
        trail = min(trail, low + 2.5 * (atr14 or risk / 2.5))
    out["trail_atr"] = r if r is not None else (entry - fwd[-1]["c"]) / risk

    # 4. move the stop to breakeven once +1R is banked
    r, be = None, False
    for x in fwd:
        lim = entry if be else stop
        if x["h"] >= lim:
            r = 0.0 if be else -1.0
            break
        if (entry - x["l"]) / risk >= 1.0:
            be = True
    out["breakeven_1R"] = r if r is not None else (entry - fwd[-1]["c"]) / risk

    # 5/6. time stops
    for days, key in ((5, "time_5d"), (10, "time_10d")):
        w = fwd[:days]
        if not w:
            continue
        r = None
        for x in w:
            if x["h"] >= stop:
                r = -1.0
                break
        out[key] = r if r is not None else (entry - w[-1]["c"]) / risk
    return out


def _ema(values, period):
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def _spearman(xs, ys) -> Optional[float]:
    """Rank correlation, hand-rolled (stdlib only, like the rest of the bot).

    Answers "does a higher score predict a better trade?" WITHOUT depending
    on how the buckets happen to be cut — the objection to equal-count
    bucketing, and a fair one.
    """
    n = len(xs)
    if n < 4:
        return None

    def rank(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):          # average ties
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    rx, ry = rank(xs), rank(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    dx = math.sqrt(sum((v - mx) ** 2 for v in rx))
    dy = math.sqrt(sum((v - my) ** 2 for v in ry))
    return num / (dx * dy) if dx and dy else None


def _bootstrap_ci(values, iters=5000, seed=7) -> tuple[float, float]:
    """95% CI around the MEAN by resampling. A point estimate of expectancy
    with no interval invites treating +0.18R and +0.02R as different facts
    when the data cannot distinguish them."""
    import random
    if len(values) < 3:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    means = []
    n = len(values)
    for _ in range(iters):
        means.append(sum(rng.choice(values) for _ in range(n)) / n)
    means.sort()
    return means[int(0.025 * iters)], means[int(0.975 * iters)]


def _streaks(values) -> tuple[int, int]:
    """(longest losing streak, longest run of stop-outs). A +0.20R
    expectancy delivered through twelve consecutive losses is operationally
    different from the same number delivered smoothly."""
    worst = cur = 0
    for v in values:
        cur = cur + 1 if v <= 0 else 0
        worst = max(worst, cur)
    return worst, cur


def _paired_bootstrap(a_vals, b_vals, iters=5000, seed=13):
    """(mean difference, 95% CI, share of resamples favouring A).

    PAIRED because every exit policy is evaluated on the SAME observations.
    Comparing two independent means throws that away and needs far more data
    to detect the same difference; resampling the DIFFERENCES keeps it. This
    is what turns "0.34R vs 0.29R" from a number into a claim.
    """
    n = len(a_vals)
    if n < 5 or n != len(b_vals):
        return None, (float("nan"), float("nan")), float("nan")
    import random
    rng = random.Random(seed)
    diffs = [a_vals[i] - b_vals[i] for i in range(n)]
    obs = sum(diffs) / n
    means = []
    for _ in range(iters):
        means.append(sum(rng.choice(diffs) for _ in range(n)) / n)
    means.sort()
    lo, hi = means[int(0.025 * iters)], means[int(0.975 * iters)]
    return obs, (lo, hi), sum(1 for m in means if m > 0) / iters


def _moments(v):
    """(std, skew, excess kurtosis). +0.20R from a hundred small wins and
    +0.20R from three enormous ones are different systems; the mean alone
    cannot tell them apart."""
    n = len(v)
    if n < 4:
        return (float("nan"),) * 3
    m = sum(v) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in v) / n)
    if sd == 0:
        return (0.0, 0.0, 0.0)
    sk = sum(((x - m) / sd) ** 3 for x in v) / n
    ku = sum(((x - m) / sd) ** 4 for x in v) / n - 3.0
    return sd, sk, ku


def _survival(stop_days, horizon):
    """P(trade still open) by day. Reveals whether shorts fail fast or
    linger — a different operational profile even at equal expectancy."""
    n = len(stop_days)
    if not n:
        return []
    out = []
    for d in range(1, horizon + 1):
        alive = sum(1 for s in stop_days if s is None or s > d)
        out.append((d, alive / n))
    return out


def _wilson(wins: int, n: int) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    z, p = 1.96, wins / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - m) / d, (c + m) / d)


def main():
    # A research tool must never be a container entrypoint: it exits,
    # and Railway restarts anything that exits. See tool_guard.
    tool_guard.guard_entrypoint("beartrend_review.py")
    ap = argparse.ArgumentParser()
    ap.add_argument("obs_file", nargs="?", default="beartrend_obs.jsonl")
    ap.add_argument("--horizons", default="1,5,10,20")
    ap.add_argument("--buckets", type=int, default=3)
    ap.add_argument("--baseline", action="store_true",
                    help="compare against ALL universe names below their "
                         "EMA200 on the same days — the only way to tell "
                         "whether the gates add anything")
    ap.add_argument("--version", help="filter to one research version")
    ap.add_argument("--csv", metavar="PREFIX",
                    help="export per-observation rows and the calibration "
                         "table for plotting elsewhere")
    ap.add_argument("--cost-bps", type=float, default=5.0,
                    help="round-trip slippage+commission, bps of notional")
    ap.add_argument("--borrow-apr", type=float, default=3.0,
                    help="annualised borrow fee %% — a short pays this for "
                         "every day it is held, and ignoring it flatters "
                         "long holds most")
    ap.add_argument("--iters", type=int, default=5000,
                    help="bootstrap resamples; raise for a promotion decision")
    ap.add_argument("--borrow", action="store_true",
                    help="ask Alpaca whether these names are shortable and "
                         "easy to borrow — a research edge on names nobody "
                         "will lend is not tradeable")
    a = ap.parse_args()
    horizons = [int(x) for x in a.horizons.split(",")]

    try:
        obs = [json.loads(l) for l in open(a.obs_file, encoding="utf-8")
               if l.strip()]
    except FileNotFoundError:
        sys.exit(f"{a.obs_file} not found:\n    railway run cat "
                 f"/data/beartrend_observations.jsonl > {a.obs_file}")

    if a.version:
        obs = [o for o in obs if o.get("version") == a.version]
    print(f"observations: {len(obs)}")
    if not obs:
        print("\nNothing recorded yet. Expected — beartrend only produces "
              "observations\nwhen SPY is in a confirmed downtrend, and the "
              "live regime has been\nWEAK_BULL/SIDEWAYS throughout. The "
              "repository fills during bear stretches,\nwhich is exactly when "
              "the question becomes worth answering.")
        return

    days = sorted({o["scan_date"] for o in obs})
    print(f"scan days: {len(days)} ({days[0]} .. {days[-1]})")
    lo = days[0]
    hi = (datetime.strptime(days[-1], "%Y-%m-%d")
          + timedelta(days=max(horizons) * 2 + 15)).strftime("%Y-%m-%d")
    syms = sorted({o["ticker"] for o in obs})
    print(f"fetching bars {lo} -> {hi} for {len(syms)} symbols ...\n")
    bars = fetch_bars(syms, lo, hi)

    scored = []
    for o in obs:
        b = [x for x in bars.get(o["ticker"], [])
             if x["t"][:10] > o["scan_date"]]
        if not b:
            continue
        entry, risk, stop = o["price"], o["risk_per_share"], o["stop"]
        if risk <= 0:
            continue
        rec = dict(o)
        # SHORT convention: profit when price falls.
        for h in horizons:
            window = b[:h]
            if len(window) < h:
                continue
            rec[f"r{h}"] = (entry - window[-1]["c"]) / risk
        full = b[:max(horizons)]
        if full:
            rec["mfe_r"] = (entry - min(x["l"] for x in full)) / risk
            rec["mae_r"] = (max(x["h"] for x in full) - entry) / risk
            # WHICH DAY the stop hit, not merely whether it did. Knowing a
            # trade reached +0.9R before being stopped on day 2 is an exit
            # design finding; "stopped=True" alone is not.
            stop_day = None
            best_before = 0.0
            for i, x in enumerate(full, start=1):
                if stop_day is None:
                    best_before = max(best_before, (entry - x["l"]) / risk)
                if x["h"] >= stop and stop_day is None:
                    stop_day = i
            rec["stopped"] = stop_day is not None
            rec["stop_day"] = stop_day
            rec["max_r_before_stop"] = round(best_before, 3)
            # day the deepest favourable excursion occurred
            lows = [(entry - x["l"]) / risk for x in full]
            rec["days_to_mfe"] = (lows.index(max(lows)) + 1) if lows else None
            # FAIL CLOSED (2026-08-03). This used to default to 0.0 when the
            # full horizon had not elapsed, silently scoring "not enough
            # forward data yet" as "flat trade" — which drags expectancy
            # toward zero and manufactures losing streaks out of observations
            # that simply have not finished. Same fail-open class the scanner
            # was corrected for. A stop IS definitive regardless of horizon,
            # so stopped trades still resolve.
            if rec["stopped"]:
                rec["r_after_stop"] = -1.0
            elif f"r{max(horizons)}" in rec:
                rec["r_after_stop"] = rec[f"r{max(horizons)}"]
            # else: left unset -> excluded from expectancy entirely
        # ---- COSTS (2026-08-03) -------------------------------------
        # A short's expectancy without borrow cost is fiction, and the error
        # grows with holding period — exactly the horizons this tool reports.
        # Charged in R so it is comparable with everything else.
        held_days = rec.get("stop_day") or max(horizons)
        cost_r = 0.0
        if risk > 0:
            cost_r += (entry * (a.cost_bps / 10000.0) * 2) / risk   # round trip
            cost_r += (entry * (a.borrow_apr / 100.0) *
                       (held_days / 252.0)) / risk                  # borrow
        rec["cost_r"] = round(cost_r, 4)
        rec["held_days"] = held_days
        if "r_after_stop" in rec:
            rec["r_net"] = rec["r_after_stop"] - cost_r

        # ---- MARKET-RELATIVE (2026-08-03) ----------------------------
        # If everything fell 5%, shorting anything worked. Alpha subtracts
        # what a short of SPY itself would have returned over the same days.
        spy_bars = bars.get("SPY") or []
        spy_fwd = [x for x in spy_bars if x["t"][:10] > o["scan_date"]]
        if spy_fwd and o.get("spy_price") and "r_after_stop" in rec:
            k = min(held_days, len(spy_fwd))
            spy_ret = (o["spy_price"] - spy_fwd[k - 1]["c"]) / o["spy_price"]
            # express the market move in this trade's own R units
            rec["alpha_r"] = rec["r_after_stop"] - (spy_ret * entry) / risk

        hist_c = [x["c"] for x in bars.get(o["ticker"], [])
                  if x["t"][:10] <= o["scan_date"]]
        if hist_c and full:
            # Prefer the ATR the scanner actually measured; only fall back
            # to the circular reconstruction for legacy rows written before
            # atr14 was stored.
            atr_true = o.get("atr14") or (risk / (o.get("atr_stop_mult") or 2.5))
            rec["exits"] = simulate_exits(entry, stop, risk, hist_c, full,
                                          atr_true)
        scored.append(rec)

    usable = [r for r in scored if "r_after_stop" in r]
    pending = len(scored) - len(usable)
    print(f"observations with enough forward data: {len(usable)}"
          + (f"  ({pending} still maturing — excluded, NOT counted as flat)"
             if pending else ""))
    if not usable:
        print("Not enough forward history yet — re-run after the horizon "
              "has elapsed.")
        return

    print(f"\n{'='*78}\nFORWARD RETURNS (short convention: + means price fell)"
          f"\n{'='*78}")
    print(f"{'horizon':<10}{'n':>5}{'mean R':>9}{'median R':>10}"
          f"{'win%':>8}{'95% CI':>18}")
    print("-" * 78)
    for h in horizons:
        v = [r[f"r{h}"] for r in usable if f"r{h}" in r]
        if not v:
            continue
        wins = sum(1 for x in v if x > 0)
        lo_ci, hi_ci = _wilson(wins, len(v))
        print(f"{h:>3}d{'':<7}{len(v):>5}{statistics.mean(v):>9.2f}"
              f"{statistics.median(v):>10.2f}{wins/len(v):>7.0%}"
              f"   [{lo_ci:.0%}, {hi_ci:.0%}]".rjust(18))

    mfe = [r["mfe_r"] for r in usable if "mfe_r" in r]
    mae = [r["mae_r"] for r in usable if "mae_r" in r]
    stopped = sum(1 for r in usable if r.get("stopped"))
    exp = statistics.mean([r["r_after_stop"] for r in usable])
    print(f"\n{'='*78}\nEXCURSION AND EXPECTANCY\n{'='*78}")
    print(f"  MFE (best unrealised, in R)  : {statistics.mean(mfe):.2f}")
    print(f"  MAE (worst unrealised, in R) : {statistics.mean(mae):.2f}"
          + ("   (negative = price never rose above entry)"
             if statistics.mean(mae) < 0 else ""))
    print(f"  stopped out                  : {stopped}/{len(usable)} "
          f"({stopped/len(usable):.0%})")
    print(f"  EXPECTANCY after the stop    : {exp:+.2f} R per observation")

    net = [r["r_net"] for r in usable if "r_net" in r]
    alph = [r["alpha_r"] for r in usable if "alpha_r" in r]
    if net:
        lo_n, hi_n = _bootstrap_ci(net, iters=a.iters)
        print(f"  costs charged (bps {a.cost_bps:.0f} round trip + "
              f"{a.borrow_apr:.1f}% borrow APR): "
              f"{statistics.mean([r['cost_r'] for r in usable]):.3f} R avg")
        print(f"  EXPECTANCY NET OF COSTS      : "
              f"{statistics.mean(net):+.2f} R   95% CI "
              f"[{lo_n:+.2f}, {hi_n:+.2f}]")
    if alph:
        print(f"  ALPHA vs shorting SPY itself : "
              f"{statistics.mean(alph):+.2f} R"
              + ("   <- the edge is market direction, not selection"
                 if statistics.mean(alph) <= 0 else ""))

    lo_e, hi_e = _bootstrap_ci([r["r_after_stop"] for r in usable],
                               iters=a.iters)
    print(f"  expectancy 95% CI            : [{lo_e:+.2f}, {hi_e:+.2f}]"
          + ("   <- spans zero; not distinguishable from no edge"
             if lo_e < 0 < hi_e else ""))
    lose_streak, _ = _streaks([r["r_after_stop"] for r in usable])
    stop_days = [r["stop_day"] for r in usable if r.get("stop_day")]
    mfe_days = [r["days_to_mfe"] for r in usable if r.get("days_to_mfe")]
    print(f"  longest losing streak        : {lose_streak}")
    if stop_days:
        print(f"  median day the stop hit      : {statistics.median(stop_days):.0f}"
              f"   (max R reached first: "
              f"{statistics.mean([r['max_r_before_stop'] for r in usable if r.get('stopped')]):.2f})")
    if mfe_days:
        print(f"  median day of best excursion : {statistics.median(mfe_days):.0f}")

    # Kelly — informative but dangerous on small samples, so it is shown
    # with the condition under which it means anything.
    src_v = net or [r["r_after_stop"] for r in usable]
    if len(src_v) >= 10:
        mu = statistics.mean(src_v)
        var = statistics.pvariance(src_v)
        if var > 0:
            k = mu / var
            print(f"  Kelly fraction (mean/var)    : {k:.2f}"
                  f"  -> quarter-Kelly {k/4:.2f}")
            print("    Kelly is exquisitely sensitive to estimation error; on")
            print("    this sample treat it as a ceiling, not a target, and")
            print("    ignore it entirely until the CI excludes zero.")

    # rolling expectancy — does the edge decay?
    if len(src_v) >= 30:
        win = 20
        roll = [statistics.mean(src_v[i:i + win])
                for i in range(0, len(src_v) - win + 1, max(1, win // 2))]
        print(f"  rolling expectancy ({win}-obs)   : "
              + " ".join(f"{x:+.2f}" for x in roll))
        if len(roll) >= 3 and roll[-1] < roll[0] - 0.2:
            print("    trending DOWN — possible edge decay, or the later")
            print("    observations came from a different market.")

    # environment segmentation beyond the CIO label
    slopes = [(r.get("spy_ema50_slope"), r["r_after_stop"]) for r in usable
              if r.get("spy_ema50_slope") is not None]
    if len(slopes) >= 12:
        slopes.sort()
        half = len(slopes) // 2
        steep = statistics.mean([x[1] for x in slopes[:half]])
        shallow = statistics.mean([x[1] for x in slopes[half:]])
        print(f"\n  by SPY EMA50 slope (steeper decline first):")
        print(f"    steeper half  n={half:<4} expectancy {steep:+.2f}R")
        print(f"    shallower half n={len(slopes)-half:<3} expectancy "
              f"{shallow:+.2f}R")
        if abs(steep - shallow) > 0.3:
            print("    -> the edge depends on HOW bearish the tape is, not")
            print("       merely that it is bearish.")

    by_regime = defaultdict(list)
    for r in usable:
        by_regime[r.get("cio_regime") or "?"].append(r["r_after_stop"])
    if len(by_regime) > 1:
        print(f"\n  by CIO regime (the edge may exist in only one):")
        for k, v in sorted(by_regime.items(), key=lambda kv: -len(kv[1])):
            print(f"    {k:<12} n={len(v):<4} expectancy {statistics.mean(v):+.2f}R")

    by_ver = defaultdict(list)
    for r in usable:
        by_ver[r.get("version") or "?"].append(r["r_after_stop"])
    if len(by_ver) > 1:
        print(f"\n  by research version (rules changed between these):")
        for k, v in sorted(by_ver.items()):
            print(f"    {k:<12} n={len(v):<4} expectancy {statistics.mean(v):+.2f}R")

    # Uses `scored`, NOT `usable`. Expectancy requires the full horizon to
    # have elapsed; an exit policy does not — each resolves on its own terms,
    # and a stop resolves immediately. Filtering to `usable` admitted only
    # trades that had already stopped out, so every policy scored -1.00R and
    # the comparison was structurally meaningless.
    with_exits = [r for r in scored if r.get("exits")]
    if with_exits:
        print(f"\n{'='*78}\nEXIT POLICY COMPARISON (same observations, "
              f"different exits)\n{'='*78}")
        print("  Measures the EXIT separately from the SIGNAL. The headline")
        print("  expectancy above is one exit's result, not the strategy's.")
        print(f"  Wider sample than expectancy ({len(with_exits)} vs "
              f"{len(usable)}): a policy resolves on its own terms, so it does")
        print("  not need the full horizon to have elapsed.")
        print(f"\n{'policy':<16}{'n':>5}{'mean R':>9}{'win%':>8}{'worst':>9}")
        print("-" * 78)
        rows = []
        for pol in EXIT_POLICIES:
            v = [r["exits"][pol] for r in with_exits if pol in r["exits"]]
            if v:
                rows.append((statistics.mean(v), pol, len(v),
                             sum(1 for x in v if x > 0) / len(v), min(v)))
        for m, pol, n_, w, worst in sorted(rows, reverse=True):
            print(f"{pol:<16}{n_:>5}{m:>9.2f}{w:>7.0%}{worst:>9.2f}")
        print("-" * 78)
        if rows:
            best = max(rows)
            base_pol = "atr_stop_hold"
            common = [r for r in with_exits
                      if best[1] in r["exits"] and base_pol in r["exits"]]
            if best[1] != base_pol and common:
                # NOT `a`/`b`: `a` is the argparse namespace and shadowing
                # it here broke score calibration further down with an
                # AttributeError on a list. Short names in a long function
                # are how that happens.
                pol_vals = [r["exits"][best[1]] for r in common]
                base_vals = [r["exits"][base_pol] for r in common]
                d, (lo_d, hi_d), p_better = _paired_bootstrap(pol_vals,
                                                              base_vals)
                print(f"\n  PAIRED TEST: '{best[1]}' vs '{base_pol}' "
                      f"on the same {len(common)} observations")
                print(f"    mean difference {d:+.2f}R   95% CI "
                      f"[{lo_d:+.2f}, {hi_d:+.2f}]   "
                      f"favours {best[1]} in {p_better:.0%} of resamples")
                if lo_d > 0:
                    print(f"    -> the difference SURVIVES resampling. Worth")
                    print(f"       designing the exit engine around.")
                else:
                    print(f"    -> CI spans zero: the ranking above is not")
                    print(f"       distinguishable from noise on this sample.")
            else:
                print("  the assumed exit is the best of those tested.")

        # distribution shape: the mean hides how the return is earned
        base_v = [r["exits"]["atr_stop_hold"] for r in with_exits
                  if "atr_stop_hold" in r["exits"]]
        if len(base_v) >= 4:
            sd, sk, ku = _moments(base_v)
            print(f"\n  distribution (atr_stop_hold): sd {sd:.2f}  "
                  f"skew {sk:+.2f}  excess kurtosis {ku:+.2f}")
            if sk > 1.0:
                print("    right-skewed: the return comes from a few large")
                print("    winners, so the average understates how often it "
                      "loses.")
            elif sk < -1.0:
                print("    left-skewed: many small wins funding rare large")
                print("    losses — the profile that looks safe until it "
                      "isn't.")

        # survival: do bad shorts linger?
        sdays = [r.get("stop_day") for r in with_exits]
        surv = _survival(sdays, min(10, max(horizons)))
        if surv:
            print("\n  survival (P still open):  "
                  + "  ".join(f"d{d}:{p:.0%}" for d, p in surv[:8]))
            stopped_r = [r for r in with_exits if r.get("stop_day")]
            if stopped_r:
                print(f"    median day a stop hit: "
                      f"{statistics.median([r['stop_day'] for r in stopped_r]):.0f}"
                      f"  ({len(stopped_r)}/{len(with_exits)} stopped)")

    print(f"\n{'='*78}\nSCORE CALIBRATION\n{'='*78}")
    rho = _spearman([r["score"] for r in usable],
                    [r["r_after_stop"] for r in usable])
    if rho is not None:
        print(f"  Spearman rank correlation (score vs realised R): {rho:+.2f}")
        print("    bucket-free, so it does not depend on how the cuts fall")
    if len(usable) < 12:
        print(f"  {len(usable)} observations — too few to bucket. Listing:")
        for r in sorted(usable, key=lambda x: -x["score"])[:12]:
            print(f"    {r['scan_date']} {r['ticker']:<6} score "
                  f"{r['score']:>5.0f}  ->  {r['r_after_stop']:+.2f}R"
                  + ("  (stopped)" if r.get("stopped") else ""))
    else:
        pts = sorted(usable, key=lambda x: x["score"])
        # Deciles once there is enough data to fill them; three buckets until
        # then. Ten buckets on forty observations is four per bucket, which
        # is decoration rather than analysis.
        nb = 10 if len(pts) >= 100 else a.buckets
        size = max(1, len(pts) // nb)
        if nb != a.buckets:
            print(f"  using {nb} buckets (n={len(pts)})")
        print(f"{'score range':<16}{'n':>5}{'mean R':>9}{'win%':>8}"
              f"{'stopped%':>10}")
        print("-" * 78)
        prev, mono = None, True
        for i in range(0, len(pts), size):
            ch = pts[i:i + size]
            if len(ch) < 2:
                continue
            v = [r["r_after_stop"] for r in ch]
            m = statistics.mean(v)
            w = sum(1 for x in v if x > 0) / len(v)
            st = sum(1 for r in ch if r.get("stopped")) / len(ch)
            print(f"{ch[0]['score']:.0f}-{ch[-1]['score']:.0f}".ljust(16)
                  + f"{len(ch):>5}{m:>9.2f}{w:>7.0%}{st:>10.0%}")
            if prev is not None and m < prev:
                mono = False
            prev = m
        print("-" * 78)
        print("  score is informative — higher scores returned more"
              if mono else
              "  score is NOT monotonic — the ranking is not yet predictive")

    # ---- BASELINE: does the selection beat the market it selected from? ---
    baseline_exp = None
    if a.baseline:
        print(f"\n{'='*78}\nBASELINE — is the SELECTION adding value?\n{'='*78}")
        print("  The question a raw expectancy cannot answer: +0.18R is only")
        print("  good if simply shorting everything below its EMA200 on the")
        print("  same days did WORSE. Otherwise the gates are decoration.")
        try:
            from config import UNIVERSE
        except Exception as e:  # noqa: BLE001
            print(f"  cannot import config.UNIVERSE ({e}) — run from the repo "
                  f"directory")
            UNIVERSE = []
        if UNIVERSE:
            uni = sorted(set(UNIVERSE) - set(syms))
            print(f"  fetching {len(uni)} more symbols for the comparison ...")
            ubars = fetch_bars(uni, lo, hi)
            ubars.update(bars)
            base_r = []
            for day in days:
                for t, bb in ubars.items():
                    hist = [x for x in bb if x["t"][:10] <= day]
                    fwd = [x for x in bb if x["t"][:10] > day][:max(horizons)]
                    if len(hist) < 200 or len(fwd) < max(horizons):
                        continue
                    closes_h = [x["c"] for x in hist]
                    e200 = _ema(closes_h, 200)
                    px = closes_h[-1]
                    if e200 is None or px >= e200:
                        continue          # only "eligible bearish" names
                    # same R denominator convention: 2.5 x ATR14
                    trs = []
                    for i in range(len(hist) - 14, len(hist)):
                        trs.append(max(hist[i]["h"] - hist[i]["l"],
                                       abs(hist[i]["h"] - hist[i-1]["c"]),
                                       abs(hist[i]["l"] - hist[i-1]["c"])))
                    atr14 = sum(trs) / 14
                    if atr14 <= 0:
                        continue
                    risk = 2.5 * atr14
                    stop = px + risk
                    hit = any(x["h"] >= stop for x in fwd)
                    base_r.append(-1.0 if hit
                                  else (px - fwd[-1]["c"]) / risk)
            if base_r:
                baseline_exp = statistics.mean(base_r)
                bl, bh = _bootstrap_ci(base_r)
                print(f"\n  BearTrend selections : {exp:+.2f}R  (n={len(usable)})")
                print(f"  every bearish name   : {baseline_exp:+.2f}R  "
                      f"(n={len(base_r)})  95% CI [{bl:+.2f}, {bh:+.2f}]")
                edge = exp - baseline_exp
                print(f"  INCREMENTAL VALUE    : {edge:+.2f}R per observation")
                if edge <= 0.05:
                    print("  -> the gates are NOT adding value over shorting")
                    print("     everything below EMA200. That is a finding, and")
                    print("     it argues against the infrastructure regardless")
                    print("     of whether raw expectancy is positive.")
                else:
                    print("  -> selection beats the eligible pool; the gates")
                    print("     are doing work.")
            else:
                print("  no eligible baseline names with enough history")

    # ---- BORROW: could these have been shorted at all? -------------------
    if a.borrow:
        print(f"\n{'='*78}\nBORROW AVAILABILITY\n{'='*78}")
        print("  Research cannot see this, and it can invalidate everything")
        print("  above: an edge on names nobody will lend is not tradeable.")
        try:
            base = os.environ.get("APCA_API_BASE_URL",
                                  "https://paper-api.alpaca.markets").rstrip("/")
            hdr = _auth()
            shortable = etb = checked = 0
            for t in sorted({r["ticker"] for r in usable}):
                rr = requests.get(f"{base}/v2/assets/{t}", headers=hdr,
                                  timeout=15)
                if rr.status_code != 200:
                    continue
                j = rr.json(); checked += 1
                shortable += bool(j.get("shortable"))
                etb += bool(j.get("easy_to_borrow"))
            if checked:
                print(f"\n  checked {checked} distinct names")
                print(f"  shortable       : {shortable}/{checked} "
                      f"({shortable/checked:.0%})")
                print(f"  easy to borrow  : {etb}/{checked} "
                      f"({etb/checked:.0%})")
                if shortable / checked < 0.8:
                    print("  -> a large share could not have been shorted. The")
                    print("     research expectancy is measuring trades that")
                    print("     could not have been taken.")
        except Exception as e:  # noqa: BLE001
            print(f"  borrow check failed ({e}) — skipped")

    if a.csv:
        import csv as _csv
        obs_path = f"{a.csv}_observations.csv"
        with open(obs_path, "w", newline="", encoding="utf-8") as fh:
            cols = ["scan_date", "ticker", "score", "adx", "rel_strength",
                    "cio_regime", "version", "r_after_stop", "mfe_r",
                    "mae_r", "stopped", "stop_day", "days_to_mfe"]
            w = _csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in usable:
                w.writerow(r)
        print(f"\n  exported {len(usable)} rows -> {obs_path}")
        if with_exits:
            ex_path = f"{a.csv}_exits.csv"
            with open(ex_path, "w", newline="", encoding="utf-8") as fh:
                w = _csv.writer(fh)
                w.writerow(["ticker", "scan_date"] + list(EXIT_POLICIES))
                for r in with_exits:
                    w.writerow([r["ticker"], r["scan_date"]]
                               + [r["exits"].get(p, "") for p in EXIT_POLICIES])
            print(f"  exported exit matrix   -> {ex_path}")

    print(f"\n{'='*78}\nVERDICT\n{'='*78}")
    n, days_n = len(usable), len(days)
    # Four INDEPENDENT conditions, all required. A single expectancy
    # threshold can be cleared by one lucky bear stretch; this cannot.
    # NET of costs, not gross: a short that only works before borrow fees
    # is not a strategy. Falls back to gross if costs were unavailable.
    check_vals = net or [r["r_after_stop"] for r in usable]
    lo_e2, hi_e2 = _bootstrap_ci(check_vals, iters=a.iters)
    exp_net = statistics.mean(check_vals)
    episodes = len({d[:7] for d in days})     # distinct months as a proxy
    checks = [
        (n >= 50, f"observations >= 50", f"{n}"),
        (episodes >= 2, "2+ distinct bearish episodes", f"{episodes}"),
        (lo_e2 > 0, "NET expectancy CI excludes zero",
         f"[{lo_e2:+.2f}, {hi_e2:+.2f}]"),
        (baseline_exp is not None and (exp_net - baseline_exp) > 0.05,
         "beats the eligible-pool baseline by >0.05R",
         (f"{exp_net - baseline_exp:+.2f}R" if baseline_exp is not None
          else "not measured — run with --baseline")),
    ]
    print("  PROMOTION CHECKLIST (all four required)")
    for ok_, label, detail in checks:
        print(f"    [{'PASS' if ok_ else 'FAIL'}]  {label:<42} {detail}")
    if all(c[0] for c in checks):
        print("\n  All four hold. The short-side execution stack has an")
        print("  evidence case: borrow checks, side-aware position model,")
        print("  inverted exits, gross/net exposure tracking.")
    else:
        print("\n  Not all conditions hold — the infrastructure is not")
        print("  justified yet. Nothing here says the strategy is bad; it")
        print("  says the evidence does not yet support a month of work.")
    print()
    if n < 50 or days_n < 10:
        print(f"  {n} observations over {days_n} scan day(s) — NOT ENOUGH.")
        print("  Bear regimes are rare and this repository fills in bursts.")
        print("  ~50 observations across at least two distinct bearish")
        print("  stretches before any of the above should move a decision.")
    elif exp > 0.15:
        print(f"  Expectancy {exp:+.2f}R over {n} observations.")
        print("  The short-side execution stack (borrow checks, side-aware")
        print("  position model, inverted exits, gross/net exposure) has an")
        print("  evidence case. Confirm it holds in a SECOND bear stretch")
        print("  before starting the build.")
    else:
        print(f"  Expectancy {exp:+.2f}R over {n} observations.")
        print("  This does not justify the short-side infrastructure. A")
        print("  strategy that needs a short-selling stack in order to lose")
        print("  money more efficiently is not worth the engineering.")


if __name__ == "__main__":
    main()
