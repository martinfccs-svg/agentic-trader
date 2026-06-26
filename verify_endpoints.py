"""STEP ZERO after setting your paid key: confirm the candle endpoints work.

    export FINNHUB_API_KEY=...
    pip install finnhub-python
    python verify_endpoints.py

You paid for daily + intraday candles; this checks both resolutions return data.
EMPTY/ERROR on a candle row means the strategy has no inputs - fix before running.
"""

from __future__ import annotations

import time

from config import DAILY_RESOLUTION, ENDPOINTS, FINNHUB_API_KEY, INTRADAY_RESOLUTION

T = "AAPL"


def main():
    if not FINNHUB_API_KEY:
        raise SystemExit("Set FINNHUB_API_KEY first.")
    import finnhub
    c = finnhub.Client(api_key=FINNHUB_API_KEY)
    now = int(time.time())
    probes = [
        ("quote", lambda: c.quote(T)),
        (f"candle/{DAILY_RESOLUTION}", lambda: c.stock_candles(T, DAILY_RESOLUTION, now - 30*86400, now)),
        (f"candle/{INTRADAY_RESOLUTION}min", lambda: c.stock_candles(T, INTRADAY_RESOLUTION, now - 6*3600, now)),
    ]
    print(f"{'probe':<20}{'status':<12}detail")
    print("-" * 60)
    for name, fn in probes:
        try:
            r = fn()
            if name.startswith("candle"):
                ok = isinstance(r, dict) and r.get("s") == "ok" and r.get("c")
                status, detail = ("OK", f"{len(r['c'])} bars") if ok else ("EMPTY/NODATA", str(r)[:60])
            else:
                ok = isinstance(r, dict) and r.get("c")
                status, detail = ("OK", f"price={r.get('c')}") if ok else ("EMPTY", str(r)[:60])
        except Exception as exc:  # noqa: BLE001
            status, detail = "ERROR", f"status={getattr(exc,'status_code','?')} {exc!r}"
        print(f"{name:<20}{status:<12}{detail}")
        time.sleep(0.5)


if __name__ == "__main__":
    main()
