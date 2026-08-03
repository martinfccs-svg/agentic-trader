"""exit_analytics.py — which exit rules actually earn their keep? LOCAL TOOL.

Every exit rule in this system was added for a plausible reason and none has
ever been measured. A time stop that fires on trades which would have
recovered is not risk management, it is a leak with a rationale. The only way
to know is to reconstruct what the price DID after each exit and compare.

WHAT IT MEASURES, per exit reason:

  MFE   maximum favourable excursion — the best unrealised gain during the
        hold, in R. The most the trade ever offered.
  MAE   maximum adverse excursion — the worst unrealised loss, in R. How
        much heat was taken to get the result.
  capture   realised R / MFE R. 1.0 means the exit caught the whole move;
            0.3 means two-thirds of what the trade offered was given back.
  drift     what the price did in the N sessions AFTER the exit, in R.
            Strongly positive means the rule exits too early — money left on
            the table. Negative means the exit protected the position, which
            is a time stop or trend exit doing exactly its job.

READING IT

  A rule with high capture AND negative drift is working: it caught most of
  the move and got out before the giveback.

  A rule with low capture AND strongly positive drift is leaking: it is
  cutting trades that went on to work.

  A rule with high MAE and low realised R is letting trades hurt before
  resolving — the stop, not the exit, is the thing to look at.

THE HONEST CAVEAT, stated up front: with a handful of trades per reason this
tool reports noise with decimal places. Roughly 20 exits per REASON before a
comparison means anything, and reasons are many — that is several hundred
trades, i.e. months. It is built now so the question is answerable the day
the data exists, not so it can be answered today.

    railway run cat /data/audit.jsonl > audit.jsonl
    python exit_analytics.py                       # needs ALPACA_* keys
    python exit_analytics.py --system swing --after 10
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

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
        sys.exit("\nNO API CREDENTIALS — this tool reconstructs the price "
                 "path between entry and exit, which needs bars.\n"
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


def load_trades(path):
    """Pair entries with exits from the audit trail."""
    try:
        rows = [json.loads(l) for l in open(path, encoding="utf-8")
                if l.strip()]
    except FileNotFoundError:
        sys.exit(f"{path} not found:\n"
                 f"    railway run cat /data/audit.jsonl > audit.jsonl")
    except json.JSONDecodeError:
        rows = []
        for l in open(path, encoding="utf-8"):
            try:
                rows.append(json.loads(l))
            except Exception:  # noqa: BLE001
                continue

    opens, trades = {}, []
    for r in rows:
        ev = str(r.get("event", ""))
        t, sysname = r.get("ticker"), r.get("system")
        ts = r.get("ts") or r.get("timestamp") or ""
        if not t:
            continue
        if "entry" in ev or "opened" in ev or ev.endswith("_open"):
            opens[(sysname, t)] = {
                "entry": r.get("price") or r.get("px") or r.get("entry"),
                "stop": r.get("stop"), "ts": ts[:10]}
        elif "exit" in ev or "close" in ev:
            o = opens.pop((sysname, t), None)
            pnl = r.get("realized", r.get("pnl"))
            if not o or o["entry"] is None or o["stop"] is None:
                continue
            risk = float(o["entry"]) - float(o["stop"])
            if risk <= 0:
                continue
            trades.append({
                "ticker": t, "system": sysname or "?",
                "reason": (r.get("reason") or "unknown").split("(")[0],
                "entry": float(o["entry"]), "stop": float(o["stop"]),
                "risk": risk, "entry_date": o["ts"],
                "exit": float(r.get("price") or r.get("px") or 0),
                "exit_date": ts[:10],
                "realized": float(pnl) if isinstance(pnl, (int, float)) else None,
            })
    return trades


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("audit_file", nargs="?", default="audit.jsonl")
    ap.add_argument("--system")
    ap.add_argument("--after", type=int, default=5,
                    help="sessions after the exit to measure drift")
    a = ap.parse_args()

    trades = load_trades(a.audit_file)
    if a.system:
        trades = [t for t in trades if t["system"] == a.system]
    print(f"paired trades with entry price AND stop: {len(trades)}")
    if not trades:
        print("\nNothing to analyse. This needs entry events carrying BOTH "
              "price and stop,\nplus exit events carrying a reason — which "
              "exit_exec now writes. The audit\ntrail only began persisting "
              "on 2026-07-28, so re-run once trades accumulate.")
        return

    lo = min(t["entry_date"] for t in trades)
    hi = (datetime.strptime(max(t["exit_date"] for t in trades), "%Y-%m-%d")
          + timedelta(days=a.after * 2 + 10)).strftime("%Y-%m-%d")
    print(f"fetching bars {lo} -> {hi} for {len({t['ticker'] for t in trades})} "
          f"symbols ...")
    bars = fetch_bars(sorted({t["ticker"] for t in trades}), lo, hi)

    for t in trades:
        b = [x for x in bars.get(t["ticker"], [])]
        during = [x for x in b if t["entry_date"] <= x["t"][:10] <= t["exit_date"]]
        after = [x for x in b if x["t"][:10] > t["exit_date"]][:a.after]
        if during:
            t["mfe_r"] = (max(x["h"] for x in during) - t["entry"]) / t["risk"]
            t["mae_r"] = (min(x["l"] for x in during) - t["entry"]) / t["risk"]
        if after and t["exit"]:
            t["drift_r"] = (max(x["h"] for x in after) - t["exit"]) / t["risk"]
        t["realized_r"] = ((t["exit"] - t["entry"]) / t["risk"]
                           if t["exit"] else None)

    groups = defaultdict(list)
    for t in trades:
        groups[t["reason"]].append(t)

    print(f"\n{'='*94}")
    print(f"{'exit reason':<22}{'n':>4}{'avg R':>8}{'MFE R':>8}{'MAE R':>8}"
          f"{'capture':>9}{'drift R':>9}{'win%':>7}")
    print("-" * 94)
    for reason, ts in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        n = len(ts)
        rr = [t["realized_r"] for t in ts if t.get("realized_r") is not None]
        mfe = [t["mfe_r"] for t in ts if "mfe_r" in t]
        mae = [t["mae_r"] for t in ts if "mae_r" in t]
        dr = [t["drift_r"] for t in ts if "drift_r" in t]
        avg_r = sum(rr) / len(rr) if rr else 0.0
        avg_mfe = sum(mfe) / len(mfe) if mfe else 0.0
        avg_mae = sum(mae) / len(mae) if mae else 0.0
        cap = (avg_r / avg_mfe) if avg_mfe > 0 else float("nan")
        avg_dr = sum(dr) / len(dr) if dr else float("nan")
        wins = sum(1 for x in rr if x > 0)
        print(f"{reason:<22}{n:>4}{avg_r:>8.2f}{avg_mfe:>8.2f}{avg_mae:>8.2f}"
              f"{cap:>9.0%}{avg_dr:>9.2f}"
              f"{(wins/len(rr) if rr else 0):>7.0%}")
    print("-" * 94)

    print("\nVERDICTS (only where the sample supports one)")
    for reason, ts in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        n = len(ts)
        if n < 20:
            print(f"  {reason:<22} n={n} — TOO FEW. ~20 exits per reason "
                  f"before this means anything.")
            continue
        mfe = [t["mfe_r"] for t in ts if "mfe_r" in t]
        rr = [t["realized_r"] for t in ts if t.get("realized_r") is not None]
        dr = [t["drift_r"] for t in ts if "drift_r" in t]
        cap = (sum(rr) / len(rr)) / (sum(mfe) / len(mfe)) if mfe and rr else 0
        avg_dr = sum(dr) / len(dr) if dr else 0
        if cap >= 0.6 and avg_dr <= 0.1:
            v = "WORKING — caught most of the move and exited before giveback"
        elif cap < 0.4 and avg_dr > 0.3:
            v = ("LEAKING — cutting trades that went on to work; loosen or "
                 "delay this rule")
        elif avg_dr > 0.5:
            v = "EARLY — significant favourable drift after the exit"
        else:
            v = "mixed — no clear signal"
        print(f"  {reason:<22} n={n} capture {cap:.0%} drift {avg_dr:+.2f}R "
              f"-> {v}")

    print("\nNote: drift is measured over the next "
          f"{a.after} sessions and says what the price DID, not what the "
          f"position\nwould have done — a trailing stop might have exited "
          f"anyway. Treat it as a\ndirection to investigate, not a verdict "
          f"on its own.")


if __name__ == "__main__":
    main()
