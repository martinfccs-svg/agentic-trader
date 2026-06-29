"""Download daily history from Finnhub into the CSV format backtest.py reads.

    export FINNHUB_API_KEY=...
    python fetch_history.py --years 10 --out ./history

Writes ./history/TICKER.csv (date,open,high,low,close,volume) for each name in
the universe. Then: python backtest.py --data ./history

VERIFY against your tier: this uses stock_candles, which must return data for
your key (run verify_endpoints.py first). Field names follow Finnhub's documented
candle response (t/o/h/l/c/v); adjust here if your SDK differs.
"""

from __future__ import annotations

import argparse
import csv
import os
import time
from datetime import datetime, timezone

from config import DAILY_RESOLUTION, FINNHUB_API_KEY, UNIVERSE


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=int, default=10)
    ap.add_argument("--out", default="./history")
    args = ap.parse_args()

    if not FINNHUB_API_KEY:
        raise SystemExit("Set FINNHUB_API_KEY first.")
    import finnhub
    client = finnhub.Client(api_key=FINNHUB_API_KEY)
    os.makedirs(args.out, exist_ok=True)

    now = int(time.time())
    frm = now - args.years * 365 * 86_400
    for ticker in UNIVERSE:
        try:
            r = client.stock_candles(ticker, DAILY_RESOLUTION, frm, now)
        except Exception as exc:  # noqa: BLE001
            print(f"  {ticker}: ERROR {getattr(exc,'status_code','?')} {exc!r}")
            continue
        if not r or r.get("s") != "ok":
            print(f"  {ticker}: no data ({r.get('s') if r else 'none'}) "
                  f"- candle endpoint may be gated on your tier")
            continue
        path = os.path.join(args.out, f"{ticker}.csv")
        with open(path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["date", "open", "high", "low", "close", "volume"])
            for i in range(len(r["c"])):
                d = datetime.fromtimestamp(r["t"][i], tz=timezone.utc).strftime("%Y-%m-%d")
                w.writerow([d, r["o"][i], r["h"][i], r["l"][i], r["c"][i], r["v"][i]])
        print(f"  {ticker}: {len(r['c'])} bars -> {path}")
        time.sleep(0.5)
    print(f"\nDone. Now: python backtest.py --data {args.out}")


if __name__ == "__main__":
    main()
