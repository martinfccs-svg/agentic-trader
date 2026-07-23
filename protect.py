"""protect.py — place a broker-side GTC protective stop (or queue a close)
for one position via the Alpaca API. For when the dashboard is unreachable.

Built for the UNH situation (5 consecutive boot CRITICALs: position held
with NO stop leg at the broker). A stop placed here is precisely what
reconcile_at_startup looks for — next boot it will be discovered and
adopted, and the CRITICAL stops firing.

SAFETY MODEL (same philosophy as the bot):
  * DRY RUN by default — prints exactly what it would do, places nothing.
  * --confirm required to actually submit.
  * Refuses to duplicate: if an open sell-stop already exists for the
    ticker, it reports it and exits.
  * Stop price: pass --stop explicitly, or let it propose
    last_close - 2.5 x ATR(14) (old swing's own multiple) from Alpaca
    daily bars, shown before anything is placed.
  * client_order_id 'manual-protect-...' — deliberately NOT 'bot-' so the
    bot's attribution never mistakes it for its own system's order.

USAGE (env: ALPACA_API_KEY / ALPACA_SECRET_KEY, or APCA_* names;
       APCA_API_BASE_URL defaults to the paper endpoint):

  python protect.py UNH                        # dry run: show plan
  python protect.py UNH --confirm              # place the GTC stop
  python protect.py UNH --stop 420 --confirm   # place at your price
  python protect.py UNH --close --confirm      # queue a market SELL
                                               # (fills at next open)

  BATCH (2026-07-23): several tickers in one command. Each is handled
  independently — a failure on one does NOT abandon the rest half-done
  (the Jul-6 flatten lesson), and a summary prints at the end.

  python protect.py UNP PLD UNH UPS --close            # dry run all four
  python protect.py UNP PLD UNH UPS --close --confirm  # queue all four
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

TRADE = os.environ.get("APCA_API_BASE_URL",
                       "https://paper-api.alpaca.markets").rstrip("/")
DATA = "https://data.alpaca.markets/v2/stocks"


def auth():
    k = os.environ.get("ALPACA_API_KEY") or os.environ.get("APCA_API_KEY_ID")
    s = (os.environ.get("ALPACA_SECRET_KEY")
         or os.environ.get("APCA_API_SECRET_KEY"))
    if not k or not s:
        sys.exit("Set ALPACA_API_KEY / ALPACA_SECRET_KEY (or APCA_* names).")
    return {"APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s}


def get(url, **params):
    r = requests.get(url, params=params or None, headers=auth(), timeout=15)
    r.raise_for_status()
    return r.json()


def handle(t: str, a) -> str:
    """Process ONE ticker. Returns a short status for the batch summary.
    Never raises or exits: in a batch, one ticker's problem must not
    abandon the others half-done (the Jul-6 flatten lesson)."""
    # 1) position must exist
    try:
        pos = get(f"{TRADE}/v2/positions/{t}")
    except requests.HTTPError:
        print(f"{t}: NO OPEN POSITION at this account/endpoint ({TRADE}).")
        return f"{t}: skipped (no position)"
    qty = abs(float(pos["qty"]))
    entry = float(pos["avg_entry_price"])
    cur = float(pos.get("current_price") or entry)
    print(f"{t}: {qty:g} shares, entry {entry:.2f}, last {cur:.2f}, "
          f"unrealized {float(pos.get('unrealized_pl') or 0):+.2f}")

    # 2) inspect open orders. In STOP mode an existing sell-stop means
    #    "done, don't duplicate". In CLOSE mode those same orders are
    #    OBSTACLES: their legs hold the shares, so the market sell would be
    #    rejected — they must be cancelled first (brokers.py's own
    #    legs-first pattern).
    open_orders = get(f"{TRADE}/v2/orders", status="open", symbols=t,
                      limit=100, nested="true")
    blocking = [o for o in open_orders
                if any(leg.get("symbol") == t and leg.get("side") == "sell"
                       for leg in [o] + (o.get("legs") or []))]
    if not a.close:
        for o in open_orders:
            legs = [o] + (o.get("legs") or [])
            for leg in legs:
                if (leg.get("symbol") == t and leg.get("side") == "sell"
                        and "stop" in str(leg.get("type", ""))):
                    print(f"An open sell-stop ALREADY EXISTS: "
                          f"id={leg['id'][:8]} type={leg['type']} "
                          f"stop={leg.get('stop_price')} "
                          f"qty={leg.get('qty')}. Not duplicating; done.")
                    return f"{t}: stop already exists ({leg.get('stop_price')})"

    if a.close:
        order = {"symbol": t, "qty": str(qty), "side": "sell",
                 "type": "market", "time_in_force": "day",
                 "client_order_id":
                     f"manual-close-{t}-{datetime.now():%Y%m%d%H%M}"}
        print(f"\nPLAN: ", end="")
        if blocking:
            print(f"cancel {len(blocking)} open order(s) whose sell legs "
                  f"hold the shares ("
                  + ", ".join(o['id'][:8] for o in blocking)
                  + "), THEN ", end="")
        print(f"queue MARKET SELL {qty:g} {t} (day order — fills at "
              "next market open). Reconcile will book it as a manual close; "
              "realized P&L lands in the audit trail via the closing-fill "
              "search. The cancelled stop is moot once the position is "
              "gone.")
    else:
        stop_px = a.stop
        if stop_px is None:
            # propose entry-intent style: last_close - 2.5 x ATR(14)
            start = (datetime.now(timezone.utc)
                     - timedelta(days=60)).strftime("%Y-%m-%d")
            bars = get(f"{DATA}/bars", symbols=t, timeframe="1Day",
                       start=start, limit=1000)["bars"].get(t, [])
            if len(bars) < 15:
                print(f"{t}: not enough bars to propose a stop — pass --stop.")
                return f"{t}: SKIPPED (no bars for proposal)"
            trs = []
            for i in range(len(bars) - 14, len(bars)):
                h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
                trs.append(max(h - l, abs(h - pc), abs(l - pc)))
            atr14 = sum(trs) / 14
            last_close = bars[-1]["c"]
            stop_px = round(last_close - 2.5 * atr14, 2)
            print(f"proposed stop = last close {last_close:.2f} "
                  f"- 2.5 x ATR14 {atr14:.2f} = {stop_px:.2f}")
        if stop_px >= cur:
            print(f"REFUSED: stop {stop_px:.2f} >= current price "
                  f"{cur:.2f} would fill instantly (the Jul-16 bug "
                  "class). Pass a lower --stop.")
            return f"{t}: REFUSED (stop >= price)"
        order = {"symbol": t, "qty": str(qty), "side": "sell",
                 "type": "stop", "stop_price": str(stop_px),
                 "time_in_force": "gtc",
                 "client_order_id":
                     f"manual-protect-{t}-{datetime.now():%Y%m%d%H%M}"}
        print(f"\nPLAN: place GTC STOP SELL {qty:g} {t} @ {stop_px:.2f} "
              f"({(stop_px / cur - 1) * 100:+.1f}% from last). Lives at the "
              "broker 24/7; reconcile will discover and adopt it next boot.")

    if not a.confirm:
        print("\nDRY RUN — nothing submitted. Re-run with --confirm to "
              "place this order.")
        return f"{t}: dry run (nothing submitted)"
    if a.close and blocking:
        for o in blocking:
            rc = requests.delete(f"{TRADE}/v2/orders/{o['id']}",
                                 headers=auth(), timeout=15)
            if rc.status_code >= 400 and rc.status_code != 404:
                print(f"Cancel of order {o['id'][:8]} FAILED "
                      f"{rc.status_code}: {rc.text[:200]} — NOT selling "
                      "(shares would still be held by the leg).")
                return f"{t}: FAILED (cancel rejected; position untouched)"
            print(f"cancelled {o['id'][:8]}")
    r = requests.post(f"{TRADE}/v2/orders",
                      headers={**auth(), "Content-Type": "application/json"},
                      data=json.dumps(order), timeout=15)
    if r.status_code >= 400:
        print(f"Order REJECTED {r.status_code}: {r.text[:300]}")
        return f"{t}: FAILED (order rejected)"
    j = r.json()
    print(f"SUBMITTED: id={j['id'][:8]} status={j['status']} "
          f"type={j['type']} tif={j['time_in_force']}")
    kind = "market SELL queued" if a.close else "GTC stop placed"
    return f"{t}: {kind} (id={j['id'][:8]}, {j['status']})"


def main():
    ap = argparse.ArgumentParser(
        description="Place GTC protective stops or queue closes via the "
                    "Alpaca API. Dry run unless --confirm. Multiple tickers "
                    "may be given; each is handled independently.")
    ap.add_argument("tickers", nargs="+",
                    help="one or more symbols, e.g. UNP PLD UNH UPS")
    ap.add_argument("--stop", type=float,
                    help="explicit stop price (single ticker only)")
    ap.add_argument("--close", action="store_true",
                    help="queue a market SELL of the whole position instead "
                         "of placing a stop (fills at next open)")
    ap.add_argument("--confirm", action="store_true",
                    help="actually submit; without this, dry run only")
    a = ap.parse_args()
    tickers = [t.upper() for t in a.tickers]
    if a.stop is not None and len(tickers) > 1:
        sys.exit("--stop takes an explicit price and cannot apply to "
                 "multiple tickers. Run them one at a time.")
    auth()   # fail fast on missing credentials, before touching anything

    results = []
    for i, t in enumerate(tickers):
        if i:
            print("\n" + "-" * 62)
        try:
            results.append(handle(t, a))
        except Exception as e:  # noqa: BLE001 — one ticker must not abort
            print(f"{t}: UNEXPECTED ERROR: {e}")
            results.append(f"{t}: FAILED ({type(e).__name__})")

    if len(tickers) > 1 or not a.confirm:
        print("\n" + "=" * 62)
        print("SUMMARY" + ("" if a.confirm else "  (DRY RUN — nothing sent)"))
        for r in results:
            print("  " + r)
        if not a.confirm:
            print("\nRe-run with --confirm to submit.")


if __name__ == "__main__":
    main()

