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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("audit_file", nargs="?", default="audit.jsonl")
    ap.add_argument("--system", help="filter to one desk")
    ap.add_argument("--buckets", type=int, default=4)
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
