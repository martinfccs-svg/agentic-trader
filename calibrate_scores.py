"""calibrate_scores.py — do the scores predict anything? LOCAL TOOL.

The prerequisite nobody names. Every proposal for a cross-desk optimizer or
a capital allocator needs an expected return per candidate, and every scoring
system in this bot reports a number on its own private scale:

    meanrev    0-6   confirmations
    intraday   0-1   weighted factors
    swing_v2   ADX   trend strength
    xsectmom   rank  relative, not absolute

Those are not comparable, and normalising them with weights would be
inventing the answer. The only honest bridge is EMPIRICAL: take closed
trades, bucket them by the score they carried at entry, and measure what
actually happened. A score is calibrated when bucket 0.8 wins more often
than bucket 0.6 — and until that is measured, "expected return" is a number
someone made up.

This tool builds nothing and enables nothing. It reports whether the scores
are informative, which is the question that has to be answered before any
optimizer is worth writing.

    python calibrate_scores.py                    # /data pulled locally
    python calibrate_scores.py audit.jsonl --system meanrev

WHAT TO LOOK FOR
  * monotonic win rate across buckets      -> the score is informative
  * flat win rate across buckets           -> the score is noise; an
                                              optimizer fed by it would
                                              allocate on nothing
  * non-monotonic, small n                 -> not enough trades yet, which
                                              is the expected answer for a
                                              while

SAMPLE SIZE IS THE WHOLE GAME. With 11 closed trades this prints a shrug and
says so. Roughly 30 per bucket is where a difference starts to mean
something; that is a few hundred trades, i.e. months. The tool exists now so
that the moment the data arrives the answer is one command away instead of a
project.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict


def load(path: str) -> list[dict]:
    rows = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        sys.exit(f"{path} not found. Pull it first:\n"
                 f"    railway run cat /data/audit.jsonl > audit.jsonl")
    return rows


def _score_of(rec: dict):
    """The entry score, wherever a desk happened to put it."""
    for k in ("score", "entry_score"):
        if isinstance(rec.get(k), (int, float)):
            return float(rec[k])
    card = rec.get("card") or rec.get("scorecard")
    if isinstance(card, dict) and isinstance(card.get("score"), (int, float)):
        return float(card["score"])
    if isinstance(rec.get("adx"), (int, float)):
        return float(rec["adx"])
    return None


def _wilson(wins: int, n: int) -> tuple[float, float]:
    """95% Wilson interval — honest about small samples, unlike wins/n.
    A 3-of-4 bucket is 75% and means nothing; the interval says so."""
    if n == 0:
        return (0.0, 1.0)
    z, p = 1.96, wins / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - m) / d, (c + m) / d)


FEATURES = ("adx", "vol_ratio", "setup_age_days", "risk_per_share",
            "atr14", "risk_pct")


def _cliffs_delta(top, bottom):
    """Non-parametric effect size: P(top > bottom) - P(bottom > top).

    Cohen's d divides by a pooled standard deviation, which assumes the two
    groups are roughly normal. R-multiples are not — a trend strategy earns
    its return from a handful of large winners, so the distribution is
    heavily right-skewed and a few outliers move both the mean and the SD.
    Cliff's delta only counts ORDERINGS, so one +8R winner cannot inflate it.

    Ranges -1 to +1. Conventional reading: |d| < 0.15 negligible,
    < 0.33 small, < 0.47 medium, above that large.
    """
    n_t, n_b = len(top), len(bottom)
    if not n_t or not n_b:
        return float("nan")
    # O(n log n) rather than the naive O(n*m) pairwise comparison
    bot = sorted(bottom)
    import bisect
    greater = sum(bisect.bisect_left(bot, x) for x in top)
    less = sum(n_b - bisect.bisect_right(bot, x) for x in top)
    return (greater - less) / (n_t * n_b)


def _spread_ci(top, bottom, iters=3000, seed=11):
    """Bootstrap CI on the DIFFERENCE between the best and worst bucket.

    "SEPARATES, in order" was a heuristic: a monotonic gradient with a wide
    enough spread. That is a reasonable filter and it is not a claim. This
    resamples both buckets and reports how uncertain the gap actually is —
    if the interval spans zero, the ordering is decoration.
    """
    import random
    import statistics
    if len(top) < 5 or len(bottom) < 5:
        return (float("nan"), float("nan")), float("nan")
    rng = random.Random(seed)
    diffs = []
    for _ in range(iters):
        a = statistics.mean(rng.choice(top) for _ in range(len(top)))
        b = statistics.mean(rng.choice(bottom) for _ in range(len(bottom)))
        diffs.append(a - b)
    # MEDIAN spread alongside the mean, for the same reason: with fat tails
    # the two can disagree, and when they do the mean is following outliers.
    med_diffs = []
    for _ in range(min(iters, 1500)):
        a = statistics.median(rng.choice(top) for _ in range(len(top)))
        b = statistics.median(rng.choice(bottom) for _ in range(len(bottom)))
        med_diffs.append(a - b)
    med_diffs.sort()
    diffs.sort()
    lo, hi = diffs[int(0.025 * iters)], diffs[int(0.975 * iters)]
    m_lo = med_diffs[int(0.025 * len(med_diffs))]
    m_hi = med_diffs[int(0.975 * len(med_diffs))]
    median_spread = statistics.median(top) - statistics.median(bottom)
    # Cohen's d — effect SIZE, separate from significance. A gap can be
    # statistically clear and economically trivial; reporting only a p-value
    # or only a spread hides one or the other.
    sd_pool = ((statistics.pstdev(top) ** 2 + statistics.pstdev(bottom) ** 2)
               / 2) ** 0.5
    d = ((statistics.mean(top) - statistics.mean(bottom)) / sd_pool
         if sd_pool else float("nan"))
    return {"ci": (lo, hi), "cohens_d": d, "cliffs_delta":
            _cliffs_delta(top, bottom), "median_spread": median_spread,
            "median_ci": (m_lo, m_hi)}


def attribute_features(paired, buckets=3, n_features_tested=None,
                       iters=3000, seed=11):
    """Which ENTRY FEATURE actually separates winners from losers?

    Not machine-learning feature importance — empirical attribution. For each
    feature recorded at entry, split the closed trades into buckets and
    compare realised R. If high-ADX entries return +0.4R and low-ADX ones
    -0.1R, ADX is earning its gate. If both buckets return the same, the gate
    is costing you trades and buying nothing.

    THE CRUCIAL LIMIT, because it is easy to over-read: this can only measure
    features that VARY among the trades you TOOK. A hard gate that rejected
    everything below ADX 20 means there are no low-ADX trades to compare —
    so this measures the gate's GRADIENT above its threshold, never whether
    the threshold itself is right. Answering that needs the gate relaxed in
    a backtest, not more live trades.
    """
    import statistics
    out = {}
    for feat in FEATURES:
        pts = [(t[feat], t["pnl_r"]) for t in paired
               if isinstance(t.get(feat), (int, float))
               and t.get("pnl_r") is not None]
        if len(pts) < buckets * 4:
            continue
        pts.sort()
        distinct = sorted({x for x, _ in pts})
        rows = []
        if len(distinct) <= buckets + 2:
            # LOW-CARDINALITY features (setup_age_days takes 0..3) must be
            # grouped BY VALUE. Equal-count bucketing splits inside a tie
            # group — the boundary lands arbitrarily in the middle of "all
            # the 0s" — and that manufactures monotonic-looking gradients out
            # of nothing. Caught by a fixture where setup_age was pure noise
            # and still scored +0.56R monotonic across 150 trades.
            for v in distinct:
                ch = [(x, r) for x, r in pts if x == v]
                if len(ch) < 2:
                    continue
                rows.append({"lo": v, "hi": v, "n": len(ch),
                             "mean_r": statistics.mean(r for _, r in ch),
                             "win": sum(1 for _, r in ch if r > 0) / len(ch)})
        else:
            size = max(1, len(pts) // buckets)
            for i in range(0, len(pts), size):
                ch = pts[i:i + size]
                if len(ch) < 2:
                    continue
                rows.append({"lo": ch[0][0], "hi": ch[-1][0], "n": len(ch),
                             "mean_r": statistics.mean(v for _, v in ch),
                             "win": sum(1 for _, v in ch if v > 0) / len(ch)})
        if len(rows) >= 2:
            spread = rows[-1]["mean_r"] - rows[0]["mean_r"]
            mono = all(rows[i]["mean_r"] <= rows[i + 1]["mean_r"]
                       for i in range(len(rows) - 1))
            # A VERDICT NEEDS BOTH, and the test that produced this rule was
            # a fixture where setup_age was pure noise: it still showed a
            # +0.38R spread because the top bucket happened to draw well, and
            # a spread-only rule called it predictive. A feature that
            # separates should separate IN ORDER — noise produces gaps, not
            # gradients. n is reported because at 30 per bucket even a
            # monotonic gradient is suggestive, not settled.
            n_total = sum(r["n"] for r in rows)
            top_vals = [r for x, r in pts if x >= rows[-1]["lo"]]
            bot_vals = [r for x, r in pts if x <= rows[0]["hi"]]
            _st = _spread_ci(top_vals, bot_vals, iters=iters, seed=seed)
            (ci_lo, ci_hi) = _st["ci"]
            cohen_d = _st["cohens_d"]
            ci_excludes_zero = (ci_lo == ci_lo and ci_lo > 0)
            if not mono:
                verdict = ("not monotonic — bucket gaps without a gradient "
                           "are what noise looks like")
            elif abs(spread) < 0.3:
                verdict = "flat — this feature is not distinguishing anything"
            elif n_total < 90:
                verdict = (f"gradient present but only {n_total} trades — "
                           f"suggestive, not settled")
            elif not ci_excludes_zero:
                verdict = (f"gradient present, but the 95% CI on the gap "
                           f"[{ci_lo:+.2f}, {ci_hi:+.2f}] SPANS ZERO — the "
                           f"ordering is not distinguishable from chance")
            else:
                verdict = (f"SEPARATES — gap {spread:+.2f}R, 95% CI "
                           f"[{ci_lo:+.2f}, {ci_hi:+.2f}], d={cohen_d:+.2f}")
            out[feat] = {"buckets": rows, "spread_r": spread,
                         "monotonic": mono, "n": n_total, "verdict": verdict,
                         "ci": (ci_lo, ci_hi), "cohens_d": cohen_d,
                         "cliffs_delta": _st["cliffs_delta"],
                         "median_spread": _st["median_spread"],
                         "median_ci": _st["median_ci"]}

    # MULTIPLICITY, the same problem the exit sweep has. Testing k features
    # at 5% each gives a 1-(0.95^k) chance that one looks predictive on noise
    # alone. Reported rather than silently corrected, because a Bonferroni
    # threshold on six features is brutal and the honest move is to say how
    # many were tried.
    k = n_features_tested or len(out)
    if k > 1:
        for v in out.values():
            v["n_features_tested"] = k
            v["p_any_false_positive"] = 1 - (0.95 ** k)
    return out


def stability(paired, date_key="exit_date", periods=4, min_per_period=25,
              iters=1500, seed=11):
    """Does each feature still separate ACROSS TIME, or only in one stretch?

    The question a single pooled number cannot answer. A feature that
    separated beautifully in Q1 and inverted in Q4 has a pooled result that
    looks fine and an edge that is gone — and pooling is exactly how you
    would fail to notice.

        Q1  ADX separates   +0.6R
        Q2  ADX separates   +0.5R
        Q3  ADX flat        +0.0R
        Q4  ADX negative    -0.3R

    That pattern averages to something mildly positive and describes a
    feature that stopped working. Splitting by period is what makes decay
    visible while it is happening rather than after it has cost you a year.

    Reports each period's spread plus a SIGN-CONSISTENCY count, which is the
    number that matters: an edge that changes sign is not a weak edge, it is
    a different thing in each regime.
    """
    dated = [t for t in paired if t.get(date_key)]
    if len(dated) < periods * min_per_period:
        return {"error": f"{len(dated)} dated trades — need "
                         f"{periods * min_per_period} for {periods} periods"}
    dated.sort(key=lambda t: t[date_key])
    size = len(dated) // periods
    out = {}
    for i in range(periods):
        chunk = dated[i * size:(i + 1) * size] if i < periods - 1 \
            else dated[i * size:]
        res = attribute_features(chunk, iters=iters, seed=seed)
        label = f"P{i+1} ({chunk[0][date_key]}..{chunk[-1][date_key]})"
        for feat, r in res.items():
            out.setdefault(feat, []).append(
                {"period": label, "n": r["n"], "spread": r["spread_r"],
                 "cliffs": r.get("cliffs_delta", float("nan"))})
    summary = {}
    for feat, rows in out.items():
        signs = [1 if r["spread"] > 0 else -1 for r in rows]
        consistent = abs(sum(signs)) == len(signs)
        summary[feat] = {
            "periods": rows, "sign_consistent": consistent,
            "verdict": ("STABLE — same direction in every period"
                        if consistent else
                        "UNSTABLE — the sign flips between periods, so this "
                        "is regime-dependent, not an edge")}
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("audit_file", nargs="?", default="audit.jsonl")
    ap.add_argument("--system", help="filter to one desk")
    ap.add_argument("--buckets", type=int, default=4)
    ap.add_argument("--bootstrap-iters", type=int, default=3000,
                    help="fewer for a quick look, more for a promotion call")
    ap.add_argument("--seed", type=int, default=11,
                    help="fixed so a result can be reproduced, not just re-read")
    ap.add_argument("--stability", action="store_true",
                    help="split by period: does each feature hold over TIME?")
    a = ap.parse_args()

    rows = load(a.audit_file)
    entries, closes = {}, []
    for r in rows:
        ev = r.get("event", "")
        tkr, sysname = r.get("ticker"), r.get("system")
        if a.system and sysname and sysname != a.system:
            continue
        if "entry" in ev or ev.endswith("_open") or ev == "position_opened":
            sc = _score_of(r)
            if tkr and sc is not None:
                entries[(sysname, tkr)] = sc
        elif "exit" in ev or "close" in ev:
            pnl = r.get("realized", r.get("pnl"))
            if tkr and isinstance(pnl, (int, float)):
                closes.append((sysname, tkr, float(pnl)))

    paired = [(s, t, p, entries[(s, t)]) for s, t, p in closes
              if (s, t) in entries]
    print(f"audit rows {len(rows)} | scored entries {len(entries)} | "
          f"closed trades {len(closes)} | PAIRED {len(paired)}")

    if not paired:
        print("\nNo closed trade could be matched to an entry score.")
        print("Expected right now: the audit trail only began persisting on")
        print("2026-07-28, and scores are recorded on entry events that most")
        print("desks emit only in shadow/scored mode. Re-run once trades have")
        print("accumulated.")
        return

    by_sys = defaultdict(list)
    for s, t, p, sc in paired:
        by_sys[s or "?"].append((sc, p))

    for sysname, pts in sorted(by_sys.items()):
        pts.sort()
        n = len(pts)
        print(f"\n{'='*74}\n{sysname}  ({n} paired trades)\n{'='*74}")
        if n < 8:
            print("  Too few trades to bucket. Listing them instead:")
            for sc, p in pts:
                print(f"    score {sc:>6.2f}   pnl {p:>+9.2f}")
            print("\n  A calibration curve needs roughly 30 trades PER BUCKET")
            print("  before a difference between buckets means anything.")
            continue
        size = max(1, n // a.buckets)
        print(f"{'bucket':<16}{'n':>4}{'win%':>7}{'95% CI':>16}"
              f"{'avg pnl':>10}{'total':>11}")
        print("-" * 74)
        prev_wr, monotonic = None, True
        for i in range(0, n, size):
            chunk = pts[i:i + size]
            if len(chunk) < 2:
                continue
            wins = sum(1 for _, p in chunk if p > 0)
            wr = wins / len(chunk)
            lo, hi = _wilson(wins, len(chunk))
            avg = sum(p for _, p in chunk) / len(chunk)
            tot = sum(p for _, p in chunk)
            print(f"{chunk[0][0]:.2f}-{chunk[-1][0]:.2f}".ljust(16)
                  + f"{len(chunk):>4}{wr:>6.0%}"
                  + f"   [{lo:.0%}, {hi:.0%}]".rjust(16)
                  + f"{avg:>10.2f}{tot:>11.2f}")
            if prev_wr is not None and wr < prev_wr:
                monotonic = False
            prev_wr = wr
        print("-" * 74)
        if monotonic:
            print("  Win rate rises with score -> the score carries "
                  "information.")
        else:
            print("  Win rate does NOT rise monotonically with score. Either")
            print("  the sample is too small, or the score is not predictive —")
            print("  and an optimizer fed by it would be allocating on noise.")
        print("  Check the confidence intervals before believing any of it: "
              "overlapping\n  intervals mean the buckets are not "
              "distinguishable yet.")


if __name__ == "__main__":
    main()
