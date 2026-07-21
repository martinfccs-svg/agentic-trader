"""
autopsy.py -- reconstruct the closed-trade history and answer the question:
did swing bleed from BAD ENTRIES or BROKEN EXITS?

trades.jsonl turned out to be empty (every close to date predates the
recorder wiring), so this reads the sources that DO exist, in order of
preference:

  SOURCE 1 (default): the audit trail. audit.py has logged every close as a
  JSON line {"event":"close", ticker, qty, price, entry, realized, system,
  via, ts} since it shipped.
      railway shell:  cat /data/audit.jsonl > audit.jsonl   (copy it off)
      then locally:   python autopsy.py audit.jsonl

  SOURCE 2 (fallback): Alpaca's own order history, rebuilt into round trips.
      python autopsy.py --from-alpaca
  Uses ALPACA_API_KEY/ALPACA_SECRET_KEY (or APCA_* names) from env. Pulls
  filled orders, attributes system via the bot-{system}-... client_order_id
  prefix, and pairs buys->sells FIFO per ticker. Manual dashboard closes
  have no bot coid, so their SELL side is attributed to the ticker's last
  bot BUY's system.

Optional:
  --system swing            filter to one system (default: report all)
  --exclude-dates 2026-07-16,2026-07-09
        Show the stats twice: full history, and with known incident days
        removed. The Jul-16 forced liquidations (UNH/INTC/MU dumped on
        invented stops) are BUG losses, not STRATEGY losses; mixing them in
        convicts the strategy of the infrastructure's crimes.

READING THE VERDICT (printed at the end):
  - Win% ~30-45 with avg_win comfortably > |avg_loss|  -> entries may be
    fine; the bleed is exits/sizing. Repair, don't replace.
  - Win% <35 AND avg_win <= |avg_loss|                 -> no evident edge;
    entries are the problem. Replacement (swing_v2) is the right track.
  - A few outsized losers dominating total loss        -> exit/incident
    problem regardless of win%.
This is decision support from YOUR data, not financial advice; small samples
(under ~20 trades) support only weak conclusions, and the script says so.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone


# --------------------------------------------------------------- source 1
def load_audit_closes(path: str) -> list[dict]:
    """Closed trades from audit.jsonl 'close' events. Skips corrupt lines."""
    out = []
    try:
        fh = open(path, encoding="utf-8")
    except OSError as e:
        sys.exit(f"cannot open {path}: {e}")
    with fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                j = json.loads(line)
            except json.JSONDecodeError:
                print(f"  ! skipping corrupt line {lineno}", file=sys.stderr)
                continue
            if j.get("event") != "close":
                continue
            try:
                out.append({
                    "ticker": j["ticker"],
                    "system": j.get("system", "?"),
                    "qty": float(j.get("qty", 0) or 0),
                    "entry": float(j.get("entry", 0) or 0),
                    "exit": float(j.get("price", 0) or 0),
                    "realized": float(j.get("realized", 0) or 0),
                    "via": j.get("via", "?"),
                    "ts": j.get("ts", ""),
                })
            except (KeyError, TypeError, ValueError):
                print(f"  ! close event missing fields at line {lineno}",
                      file=sys.stderr)
    return out


# --------------------------------------------------------------- source 2
def load_alpaca_roundtrips() -> list[dict]:
    """Rebuild round trips from Alpaca's filled-order history (FIFO pairing
    per ticker). Attribution: bot-{system} coid on the BUY; a SELL with no
    bot coid (manual close, bracket leg) inherits the open lot's system."""
    try:
        import requests
    except ImportError:
        sys.exit("pip install requests (or use the audit.jsonl source)")
    key = os.environ.get("ALPACA_API_KEY") or os.environ.get("APCA_API_KEY_ID")
    sec = (os.environ.get("ALPACA_SECRET_KEY")
           or os.environ.get("APCA_API_SECRET_KEY"))
    if not key or not sec:
        sys.exit("Set ALPACA_API_KEY / ALPACA_SECRET_KEY for --from-alpaca.")
    base = os.environ.get("APCA_API_BASE_URL",
                          "https://paper-api.alpaca.markets").rstrip("/")
    h = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec}

    orders, until = [], None
    for _page in range(40):                       # up to ~20k orders
        params = {"status": "closed", "limit": 500, "direction": "desc",
                  "nested": "false"}
        if until:
            params["until"] = until
        r = requests.get(f"{base}/v2/orders", params=params, headers=h,
                         timeout=30)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        orders.extend(batch)
        until = batch[-1].get("submitted_at")
        if len(batch) < 500:
            break
    fills = []
    for o in orders:
        if str(o.get("status", "")).lower() != "filled":
            continue
        if not o.get("filled_avg_price") or not o.get("filled_qty"):
            continue
        fills.append({
            "ticker": o["symbol"],
            "side": str(o.get("side", "")).lower(),
            "qty": float(o["filled_qty"]),
            "price": float(o["filled_avg_price"]),
            "ts": o.get("filled_at") or o.get("submitted_at") or "",
            "coid": o.get("client_order_id") or "",
        })
    fills.sort(key=lambda f: f["ts"])

    def system_of(coid: str) -> str | None:
        if coid.startswith("bot-"):
            parts = coid.split("-")
            if len(parts) >= 3:
                return parts[1]
        return None

    lots: dict[str, list[dict]] = defaultdict(list)   # FIFO open lots
    trades: list[dict] = []
    for f in fills:
        t = f["ticker"]
        if f["side"] == "buy":
            lots[t].append({"qty": f["qty"], "price": f["price"],
                            "system": system_of(f["coid"]) or "?",
                            "ts": f["ts"]})
        else:  # sell: consume lots FIFO
            remaining = f["qty"]
            while remaining > 1e-9 and lots[t]:
                lot = lots[t][0]
                take = min(remaining, lot["qty"])
                realized = (f["price"] - lot["price"]) * take
                trades.append({
                    "ticker": t, "system": lot["system"], "qty": take,
                    "entry": lot["price"], "exit": f["price"],
                    "realized": realized,
                    "via": system_of(f["coid"]) and "bot_sell" or "leg_or_manual",
                    "ts": f["ts"],
                })
                lot["qty"] -= take
                remaining -= take
                if lot["qty"] <= 1e-9:
                    lots[t].pop(0)
            if remaining > 1e-9:
                print(f"  ! {t}: sell of {remaining:.0f} with no open lot "
                      f"(history predates window?) — skipped",
                      file=sys.stderr)
    return trades


# ---------------------------------------------------------------- analysis
def analyze(trades: list[dict], label: str):
    if not trades:
        print(f"\n== {label}: no closed trades ==")
        return
    wins = [t for t in trades if t["realized"] > 0]
    losses = [t for t in trades if t["realized"] <= 0]
    total = sum(t["realized"] for t in trades)
    gross_loss = sum(t["realized"] for t in losses)
    n = len(trades)
    print(f"\n== {label} ==")
    print(f"  trades: {n}   win%: {len(wins)/n*100:.0f}   "
          f"net P&L: {total:+,.2f}")
    if wins:
        print(f"  avg win : {statistics.mean(t['realized'] for t in wins):+,.2f}"
              f"   best: {max(t['realized'] for t in wins):+,.2f}")
    if losses:
        print(f"  avg loss: {statistics.mean(t['realized'] for t in losses):+,.2f}"
              f"   worst: {min(t['realized'] for t in losses):+,.2f}")
    if wins and losses:
        pf = sum(t["realized"] for t in wins) / abs(gross_loss) \
            if gross_loss else float("inf")
        print(f"  profit factor: {pf:.2f}   "
              f"expectancy/trade: {total/n:+,.2f}")
    # concentration of damage
    worst3 = sorted(losses, key=lambda t: t["realized"])[:3]
    if losses and gross_loss:
        share = sum(t["realized"] for t in worst3) / gross_loss * 100
        print(f"  worst 3 losers = {share:.0f}% of all losses:")
        for t in worst3:
            print(f"    {t['ts'][:10]} {t['ticker']:<6} x{t['qty']:<6.0f} "
                  f"{t['entry']:.2f}->{t['exit']:.2f}  {t['realized']:+,.2f} "
                  f"({t['via']})")
    via = defaultdict(int)
    for t in trades:
        via[t["via"]] += 1
    print("  exit paths:", dict(via))
    if n < 20:
        print(f"  NOTE: only {n} trades — treat every number above as a "
              "hint, not a finding.")


def verdict(trades: list[dict]):
    if len(trades) < 5:
        print("\nVERDICT: sample too small for any diagnosis. Collect more "
              "shadow data before concluding anything.")
        return
    wins = [t for t in trades if t["realized"] > 0]
    losses = [t for t in trades if t["realized"] <= 0]
    winp = len(wins) / len(trades) * 100
    aw = statistics.mean(t["realized"] for t in wins) if wins else 0.0
    al = abs(statistics.mean(t["realized"] for t in losses)) if losses else 0.0
    worst3 = sum(sorted(t["realized"] for t in losses)[:3])
    gl = sum(t["realized"] for t in losses)
    conc = worst3 / gl * 100 if gl else 0
    print("\nVERDICT (heuristic, from YOUR data — see docstring):")
    if conc >= 60:
        print(f"  -> Damage is CONCENTRATED: worst 3 losers are {conc:.0f}% "
              "of all losses. Check those dates against the incident log "
              "(Jul 16 forced liquidations, Jul 17 duplicate stacks) before "
              "blaming the strategy — this pattern usually means broken "
              "exits or infrastructure, not dead entries.")
    if winp >= 30 and aw > al:
        print(f"  -> win% {winp:.0f} with avg win {aw:+,.0f} vs avg loss "
              f"{-al:+,.0f}: entries show signs of life. Repairing exits "
              "may beat replacing the strategy.")
    elif winp < 35 and aw <= al:
        print(f"  -> win% {winp:.0f} AND avg win {aw:+,.0f} <= avg loss "
              f"{-al:+,.0f}: no visible edge in entries. Replacement "
              "(the swing_v2 track) is the better-supported path.")
    else:
        print(f"  -> Mixed picture (win% {winp:.0f}, avg win {aw:+,.0f}, "
              f"avg loss {-al:+,.0f}). Let the backtest and shadow A/B "
              "break the tie.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("audit_file", nargs="?", default="audit.jsonl",
                    help="path to a copy of /data/audit.jsonl")
    ap.add_argument("--from-alpaca", action="store_true",
                    help="rebuild round trips from Alpaca order history "
                         "instead of the audit trail")
    ap.add_argument("--system", help="filter to one system (e.g. swing)")
    ap.add_argument("--exclude-dates",
                    help="comma-separated YYYY-MM-DD incident days; stats "
                         "are shown with AND without them")
    a = ap.parse_args()

    trades = load_alpaca_roundtrips() if a.from_alpaca \
        else load_audit_closes(a.audit_file)
    src = "alpaca order history" if a.from_alpaca else a.audit_file
    print(f"source: {src}   closed trades found: {len(trades)}")
    if a.system:
        trades = [t for t in trades if t["system"] == a.system]
        print(f"filtered to system={a.system}: {len(trades)} trades")
    if not trades:
        print("Nothing to analyze. If the audit file is also thin, run with "
              "--from-alpaca, or export fills from the dashboard.")
        return

    by_sys = defaultdict(list)
    for t in trades:
        by_sys[t["system"]].append(t)
    for sysname in sorted(by_sys):
        analyze(by_sys[sysname], f"system: {sysname}")

    focus = trades
    if a.exclude_dates:
        days = {d.strip() for d in a.exclude_dates.split(",")}
        kept = [t for t in trades if t["ts"][:10] not in days]
        excluded = len(trades) - len(kept)
        analyze(kept, f"ALL (excluding {sorted(days)}: {excluded} trades "
                      "removed)")
        focus = kept
    verdict(focus)


if __name__ == "__main__":
    main()
