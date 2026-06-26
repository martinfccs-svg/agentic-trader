"""STEP ZERO: confirm which Finnhub endpoints your tier can actually read.

    export FINNHUB_API_KEY=...
    pip install finnhub-python
    python verify_endpoints.py

EMPTY (200 with no body) usually means the endpoint is premium-gated on your
tier. METHOD_MISSING means the SDK method name changed — fix it in config.py.
Get every feed a system needs to OK before trusting live runs.
"""

from __future__ import annotations

import time

from config import ENDPOINTS, FINNHUB_API_KEY

TEST_TICKER = "AAPL"


def main() -> None:
    if not FINNHUB_API_KEY:
        raise SystemExit("Set FINNHUB_API_KEY first.")
    import finnhub
    client = finnhub.Client(api_key=FINNHUB_API_KEY)
    print(f"{'endpoint':<16}{'criticality':<12}{'premium?':<10}{'status':<14}detail")
    print("-" * 88)
    for ep in ENDPOINTS.values():
        method = getattr(client, ep.client_method, None)
        if method is None:
            status, detail = "METHOD_MISSING", f"no '{ep.client_method}' in SDK"
        else:
            try:
                if ep.key == "candle":
                    now = int(time.time())
                    res = method(TEST_TICKER, "D", now - 7 * 86400, now)
                else:
                    res = method(TEST_TICKER)
                empty = res in (None, {}, []) or (isinstance(res, dict) and not any(res.values()))
                status, detail = ("EMPTY", "200 but no data — likely premium") if empty \
                    else ("OK", "returns data")
            except Exception as exc:  # noqa: BLE001
                status, detail = "ERROR", f"status={getattr(exc,'status_code','?')} {exc!r}"
        flag = "uncertain" if ep.premium_uncertain else "-"
        print(f"{ep.key:<16}{ep.criticality.value:<12}{flag:<10}{status:<14}{detail}")
        time.sleep(1.1)


if __name__ == "__main__":
    main()
