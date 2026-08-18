"""replay_metrics.py — turn a replay's trades into defensible performance numbers.

LOCAL TOOL. Never deployed.

WHY THIS EXISTS SEPARATELY FROM THE HARNESS

Sharpe computed from 55 trade outcomes and Sharpe computed from daily equity
returns are different statistics, and only the second is comparable to a
published index figure. The first measures dispersion ACROSS TRADES; the
second measures dispersion across TIME, which is what an investor
experiences and what SPY's 1.29 is calculated from.

Comparing a trade-based Sharpe to an index's time-based Sharpe is not a
close-enough approximation. It is two different numbers wearing one name.

The same applies to drawdown. Summing trade P&L and tracking the running peak
gives drawdown in DOLLARS of closed profit. It ignores open positions
entirely — a book down 8% intraday shows no drawdown at all until something
closes. Percentage-of-equity drawdown from a daily curve is the number that
compares to an index.

WHAT IT DELIBERATELY DOES NOT DO

It does not decide whether a strategy should deploy. It computes the numbers
and prints the comparison; the promotion rules live where they always have —
fixed before the run, in the harness.
"""

from __future__ import annotations

import math
import statistics
from datetime import date, datetime, timedelta

TRADING_DAYS = 252


# ---------------------------------------------------------------------------
# EQUITY CURVE
# ---------------------------------------------------------------------------

def equity_curve(trades: list, start_equity: float,
                 start_day: str = None, end_day: str = None) -> list:
    """[(date, equity), ...] daily, from closed trades.

    HONEST LIMITATION, stated because it changes how the output should be
    read: this is a CLOSED-TRADE curve. Equity steps on the day a trade
    exits, and open positions contribute nothing until then. So intra-trade
    drawdown is invisible and the real peak-to-trough is deeper than what
    this reports.

    A true mark-to-market curve needs the harness to record daily portfolio
    value, which is a change to the replay loop rather than to this file.
    Until that exists, treat every drawdown here as a FLOOR — the best case,
    not the observed case.
    """
    if not trades:
        return []
    def _d(ts):
        if isinstance(ts, str):
            return ts[:10]
        return datetime.utcfromtimestamp(float(ts)).strftime("%Y-%m-%d")

    by_day: dict = {}
    for t in trades:
        d = _d(t.get("exit_time") or t.get("exit_date"))
        by_day[d] = by_day.get(d, 0.0) + float(t.get("realized_pnl", 0.0) or 0.0)

    days = sorted(by_day)
    first = start_day or days[0]
    last = end_day or days[-1]
    cur = date.fromisoformat(first)
    stop = date.fromisoformat(last)
    eq = float(start_equity)
    out = []
    while cur <= stop:
        if cur.weekday() < 5:                     # trading days only
            eq += by_day.get(cur.isoformat(), 0.0)
            out.append((cur.isoformat(), eq))
        cur += timedelta(days=1)
    return out


def _returns(curve: list) -> list:
    return [curve[i][1] / curve[i - 1][1] - 1
            for i in range(1, len(curve)) if curve[i - 1][1] > 0]


# ---------------------------------------------------------------------------
# METRICS
# ---------------------------------------------------------------------------

def metrics(curve: list, trades: list = None) -> dict:
    """Everything computable from a daily equity curve."""
    if len(curve) < 3:
        return {"error": f"{len(curve)} points — need at least 3"}
    r = _returns(curve)
    if not r:
        return {"error": "no returns"}

    start, end = curve[0][1], curve[-1][1]
    years = len(curve) / TRADING_DAYS
    mean = statistics.mean(r)
    sd = statistics.pstdev(r) or 1e-12
    # Downside deviation for Sortino: only returns BELOW zero. A strategy
    # punished for upside volatility is punished for working.
    down = [x for x in r if x < 0]
    dsd = (statistics.pstdev(down) if len(down) > 1 else sd) or 1e-12

    peak, mdd, mdd_start, mdd_end, cur_peak_day = start, 0.0, None, None, curve[0][0]
    for day, v in curve:
        if v > peak:
            peak, cur_peak_day = v, day
        dd = v / peak - 1 if peak else 0.0
        if dd < mdd:
            mdd, mdd_start, mdd_end = dd, cur_peak_day, day

    out = {
        "start_equity": round(start, 2),
        "end_equity": round(end, 2),
        "total_return_pct": round(100 * (end / start - 1), 2) if start else 0.0,
        "cagr_pct": round(100 * ((end / start) ** (1 / years) - 1), 2)
        if years > 0 and start > 0 and end > 0 else 0.0,
        "sharpe": round(mean / sd * math.sqrt(TRADING_DAYS), 2),
        "sortino": round(mean / dsd * math.sqrt(TRADING_DAYS), 2),
        "vol_annual_pct": round(100 * sd * math.sqrt(TRADING_DAYS), 2),
        "max_dd_pct": round(100 * mdd, 2),
        "max_dd_from": mdd_start, "max_dd_to": mdd_end,
        "trading_days": len(curve),
        "years": round(years, 2),
    }
    if trades:
        w = [float(t.get("realized_pnl", 0)) for t in trades
             if float(t.get("realized_pnl", 0) or 0) > 0]
        l = [float(t.get("realized_pnl", 0)) for t in trades
             if float(t.get("realized_pnl", 0) or 0) <= 0]
        n = len(w) + len(l)
        out.update({
            "trades": n,
            "win_rate_pct": round(100 * len(w) / n, 1) if n else 0.0,
            "avg_win": round(statistics.mean(w), 2) if w else 0.0,
            "avg_loss": round(statistics.mean(l), 2) if l else 0.0,
            "profit_factor": round(sum(w) / abs(sum(l)), 2) if l and sum(l) else None,
            "expectancy": round((sum(w) + sum(l)) / n, 2) if n else 0.0,
            "largest_win": round(max(w), 2) if w else 0.0,
            "largest_loss": round(min(l), 2) if l else 0.0,
            "worst_losing_streak": _streak(trades),
        })
    return out


def _streak(trades: list) -> int:
    worst = cur = 0
    for t in sorted(trades, key=lambda x: x.get("exit_time") or 0):
        if float(t.get("realized_pnl", 0) or 0) <= 0:
            cur += 1
            worst = max(worst, cur)
        else:
            cur = 0
    return worst


# ---------------------------------------------------------------------------
# BENCHMARK
# ---------------------------------------------------------------------------

def benchmark_curve(spy_closes: list, spy_days: list,
                    start_equity: float) -> list:
    """Buy-and-hold SPY over the SAME days, same starting capital.

    Must come from the harness's own feed over the harness's own window. A
    benchmark computed elsewhere, on a different calendar, is not a
    comparison — it is two numbers side by side.
    """
    if not spy_closes or len(spy_closes) != len(spy_days):
        return []
    base = spy_closes[0]
    return [(d, start_equity * (c / base))
            for d, c in zip(spy_days, spy_closes)]


def compare(strategy: dict, bench: dict) -> list:
    """Side-by-side, plus the question leverage answers.

    The comparison people skip: a strategy that returns less with a smaller
    drawdown might just be the index at lower exposure. If levering it to the
    index's drawdown does NOT reach the index's return, the strategy is not
    conservative — it is dominated, and no position sizing fixes that.
    """
    rows = [("CAGR %", strategy.get("cagr_pct"), bench.get("cagr_pct"), "high"),
            ("Sharpe", strategy.get("sharpe"), bench.get("sharpe"), "high"),
            ("Sortino", strategy.get("sortino"), bench.get("sortino"), "high"),
            ("Max drawdown %", strategy.get("max_dd_pct"),
             bench.get("max_dd_pct"), "high"),
            ("Volatility %", strategy.get("vol_annual_pct"),
             bench.get("vol_annual_pct"), "low"),
            ("Total return %", strategy.get("total_return_pct"),
             bench.get("total_return_pct"), "high")]
    out = ["", "=" * 62,
           f"{'':<20}{'STRATEGY':>13}{'BENCHMARK':>13}{'WINNER':>14}",
           "=" * 62]
    for name, a, b, better in rows:
        if a is None or b is None:
            continue
        win = ("strategy" if ((a > b) == (better == "high")) else "benchmark")
        out.append(f"{name:<20}{a:>13}{b:>13}{win:>14}")

    sc, sd = strategy.get("cagr_pct"), strategy.get("max_dd_pct")
    bc, bd = bench.get("cagr_pct"), bench.get("max_dd_pct")
    if all(v is not None for v in (sc, sd, bc, bd)) and sd < 0 and bd < 0:
        lev = abs(bd) / abs(sd)                 # scale to the SAME drawdown
        out += ["", f"  At {lev:.1f}x leverage the strategy would carry the "
                    f"benchmark's drawdown ({bd:.1f}%)",
                f"  and return {sc * lev:.1f}% against the benchmark's {bc:.1f}%.",
                ""]
        if sc * lev >= bc:
            out.append("  -> Risk-adjusted, the strategy WINS: same drawdown, "
                       "more return.")
        else:
            out.append("  -> DOMINATED. Matching the benchmark's risk still "
                       "leaves it short on")
            out.append("     return, so the smaller drawdown is lower exposure, "
                       "not skill.")
    out.append("=" * 62)
    return out
