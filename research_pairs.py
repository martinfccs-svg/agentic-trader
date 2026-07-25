"""research_pairs.py — OFFLINE stat-arb pair discovery. LOCAL TOOL, never deployed.
Answers the one question that decides whether a statistical-arbitrage engine
is worth building at all: do tradeable cointegrated pairs EXIST in this
universe? A universe deliberately spread across 14 sectors to AVOID
correlation may contain very few — cointegration usually lives within a
sector (XOM/CVX, JPM/BAC, KO/PEP), and this universe holds at most 2-3 names
per sector by design.
Deliberately stdlib + Alpaca only — NO pandas / numpy / statsmodels / sklearn.
The running bot is pure-Python over lists of floats; this tool honors that so
nothing here implies a dependency the container would have to carry.
Method (honest about its limits):
 1. Pearson correlation of daily closes — cheap first filter.
 2. Engle-Granger STEP ONLY: OLS hedge ratio (hand-rolled), then an
 Augmented Dickey-Fuller-style stationarity check on the residual spread
 via its lag-1 autoregression coefficient and half-life of mean reversion.
 This is a SCREEN, not statsmodels' full coint() with its exact critical
 values — it will rank candidates correctly but you should treat the
 pass/fail as "worth a closer look," not proof.
 3. Reports each surviving pair with hedge ratio, spread half-life, current
 z-score, and same-sector flag (in-sector pairs are more trustworthy —
 a cross-sector "cointegration" is more likely to be coincidence).
USAGE (env: ALPACA_API_KEY / ALPACA_SECRET_KEY, or APCA_* names):
 python research_pairs.py --symbols-file universe.txt --days 365
 python research_pairs.py --symbols-file universe.txt --days 365 --in-sector-only
Not financial advice; a screen for whether to invest ENGINEERING effort.
"""
from __future__ import annotations
import argparse
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from itertools import combinations

try:
    import requests
except ImportError:
    requests = None

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from sector_map import sector_of
except Exception:  # noqa: BLE001 — tool still runs without it
    def sector_of(_t):
        return "?"


STOCK_DATA = "https://data.alpaca.markets/v2/stocks"


def fetch_closes(symbols, days):
    if requests is None:
        sys.exit("pip install requests")
    key = os.environ.get("ALPACA_API_KEY") or os.environ.get("APCA_API_KEY_ID")
    sec = (os.environ.get("ALPACA_SECRET_KEY")
           or os.environ.get("APCA_API_SECRET_KEY"))
    if not key or not sec:
        sys.exit("Set ALPACA_API_KEY / ALPACA_SECRET_KEY (or APCA_* names).")
    h = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec}
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    out: dict[str, dict] = {}
    for i in range(0, len(symbols), 50):
        chunk, page = symbols[i:i + 50], None
        while True:
            p = {"symbols": ",".join(chunk), "timeframe": "1Day",
                 "start": start, "limit": 10000, "adjustment": "split"}
            if page:
                p["page_token"] = page
            r = requests.get(f"{STOCK_DATA}/bars", params=p, headers=h, timeout=30)
            r.raise_for_status()
            j = r.json()
            for s, bars in j.get("bars", {}).items():
                out.setdefault(s, {})
                for b in bars:
                    out[s][b["t"][:10]] = float(b["c"])
            page = j.get("next_page_token")
            if not page:
                break
    return out


def _mean(v):
    return sum(v) / len(v) if v else 0.0


def correlation(a, b):
    n = len(a)
    if n < 2:
        return 0.0
    ma, mb = _mean(a), _mean(b)
    num = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((x - mb) ** 2 for x in b))
    return num / (da * db) if da and db else 0.0


def ols_hedge_ratio(y, x):
    """Slope of y on x through OLS (hand-rolled). Returns beta."""
    n = len(x)
    mx, my = _mean(x), _mean(y)
    cov = sum((x[i] - mx) * (y[i] - my) for i in range(n))
    var = sum((x[i] - mx) ** 2 for i in range(n))
    return cov / var if var else 0.0


def spread_stats(y, x, beta):
    """Residual spread = y - beta*x. Return (half_life, adf_like, z_last).
    half_life from the AR(1) coefficient of the spread's change on its level;
    adf_like is that coefficient (more negative = faster reversion =
    stronger stationarity signal)."""
    spread = [y[i] - beta * x[i] for i in range(len(x))]
    # AR(1): delta_s_t = rho * s_{t-1} + eps
    s_lag = spread[:-1]
    ds = [spread[i] - spread[i - 1] for i in range(1, len(spread))]
    ml, md = _mean(s_lag), _mean(ds)
    cov = sum((s_lag[i] - ml) * (ds[i] - md) for i in range(len(ds)))
    var = sum((v - ml) ** 2 for v in s_lag)
    rho = cov / var if var else 0.0
    half_life = -math.log(2) / rho if rho < 0 else float("inf")
    m = _mean(spread)
    sd = math.sqrt(_mean([(v - m) ** 2 for v in spread]))
    z_last = (spread[-1] - m) / sd if sd else 0.0
    return half_life, rho, z_last


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols-file", default=None,
                    help="optional; if omitted, reads UNIVERSE from config.py "
                    "so the scan always matches what the bot trades")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--corr-min", type=float, default=0.80)
    ap.add_argument("--half-life-max", type=float, default=30.0,
                    help="max days-to-revert to be tradeable (too slow = dead money)")
    ap.add_argument("--in-sector-only", action="store_true")
    a = ap.parse_args()

    if a.symbols_file:
        syms = [l.strip().upper() for l in open(a.symbols_file)
                if l.strip() and not l.startswith("#")]
        source = a.symbols_file
    else:
        try:
            from config import UNIVERSE
            syms = [t.strip().upper() for t in UNIVERSE]
            source = "config.UNIVERSE"
        except Exception as e:  # noqa: BLE001
            sys.exit(f"no --symbols-file given and could not import UNIVERSE "
                     f"from config.py ({e}). Run from the repo dir, or pass "
                     f"--symbols-file.")

    print(f"universe: {len(syms)} symbols from {source}")
    print(f"fetching {len(syms)} symbols x {a.days}d ...")
    closes = fetch_closes(syms, a.days)

    # align on common dates
    common = set.intersection(*(set(closes[s]) for s in closes if closes.get(s))) \
        if closes else set()
    dates = sorted(common)

    if len(dates) < 90:
        sys.exit(f"only {len(dates)} common trading days — need >= 90.")

    series = {s: [closes[s][d] for d in dates] for s in closes
              if all(d in closes[s] for d in dates)}
    print(f"{len(series)} symbols with {len(dates)} aligned days\n")

    results = []
    pairs = list(combinations(sorted(series), 2))
    for aa, bb in pairs:
        if a.in_sector_only and sector_of(aa) != sector_of(bb):
            continue
        ya, xb = series[aa], series[bb]
        corr = correlation(ya, xb)
        if corr < a.corr_min:
            continue
        beta = ols_hedge_ratio(ya, xb)
        if beta <= 0:
            continue
        hl, rho, z = spread_stats(ya, xb, beta)
        stationary = rho < -0.02 and hl <= a.half_life_max  # screen, not proof
        if not stationary:
            continue
        results.append({
            "pair": f"{aa}/{bb}", "corr": corr, "beta": beta,
            "half_life": hl, "z": z,
            "same_sector": sector_of(aa) == sector_of(bb),
            "sectors": f"{sector_of(aa)}/{sector_of(bb)}",
        })

    results.sort(key=lambda r: (not r["same_sector"], r["half_life"]))

    print(f"scanned {len(pairs)} possible pairs -> {len(results)} passed the "
          f"screen (corr>={a.corr_min}, mean-reverting, half-life<="
          f"{a.half_life_max:.0f}d)\n")

    if not results:
        print("NO tradeable pairs found. For a 14-sector universe this is a "
              "REAL answer, not a failure: it means stat-arb infrastructure "
              "(shorting, multi-leg positions, sci-py deps) would be built "
              "for pairs that don't exist here. Consider adding same-sector "
              "clusters (e.g. more energy or bank names) first, or skip "
              "stat-arb for this universe.")
        return

    print(f"{'pair':<14}{'sectors':<26}{'corr':>6}{'beta':>7}"
          f"{'half_life':>11}{'z_now':>8}{'same_sec':>9}")
    print("-" * 82)
    for r in results[:25]:
        print(f"{r['pair']:<14}{r['sectors']:<26}{r['corr']:>6.2f}"
              f"{r['beta']:>7.2f}{r['half_life']:>10.1f}d{r['z']:>8.2f}"
              f"{'YES' if r['same_sector'] else 'no':>9}")

    print("\nHow to read: same-sector pairs with half-life 5-20 days and "
          "|z_now| > 2 are the real candidates. Cross-sector 'pairs' are "
          "likely coincidence — treat with suspicion. If only a handful "
          "survive, a full engine (with shorting + multi-leg positions) is a "
          "large build for a small opportunity; weigh accordingly.")


if __name__ == "__main__":
    main()

