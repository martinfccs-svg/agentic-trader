"""Correlation analyzer — operationalizes "uncorrelated signals".

You can't ASSUME two strategies are uncorrelated; you measure it. This reads the
recorded trades, builds a per-day realized-P&L series for each system, and
computes the correlation between every pair. Low or negative correlation is the
prize: it means the combined equity curve is smoother than any single strategy.

    python correlation.py            # reads trades.jsonl

Honest about data: correlation on a few overlapping days is noise, like Monte
Carlo on a few trades. It says so when the overlap is thin.
"""

from __future__ import annotations

import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone

from trade_record import TradeRecorder

MIN_OVERLAP_DAYS = 20      # below this, a pairwise correlation is not trustworthy


def _daily_pnl_by_system(trades) -> dict[str, dict[str, float]]:
    """system -> {date_str -> summed realized pnl that day}."""
    out: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for t in trades:
        day = datetime.fromtimestamp(t.exit_time, tz=timezone.utc).strftime("%Y-%m-%d")
        out[t.system][day] += t.realized_pnl
    return {s: dict(d) for s, d in out.items()}


def _pearson(a: list[float], b: list[float]):
    n = len(a)
    if n < 2:
        return None
    ma, mb = statistics.mean(a), statistics.mean(b)
    cov = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((x - mb) ** 2 for x in b)
    if va == 0 or vb == 0:
        return None
    return cov / (va ** 0.5 * vb ** 0.5)


def correlation_report(trades) -> None:
    series = _daily_pnl_by_system(trades)
    systems = sorted(series)
    print("\n============== STRATEGY CORRELATION ==============")
    if len(systems) < 2:
        print("Need at least two systems with closed trades to compare.")
        print("Let the new strategies run; then re-check.")
        print("==================================================\n")
        return

    for i in range(len(systems)):
        for j in range(i + 1, len(systems)):
            s1, s2 = systems[i], systems[j]
            shared = sorted(set(series[s1]) & set(series[s2]))
            a = [series[s1][d] for d in shared]
            b = [series[s2][d] for d in shared]
            r = _pearson(a, b)
            tag = ""
            if r is None:
                verdict = "n/a"
            elif len(shared) < MIN_OVERLAP_DAYS:
                verdict = f"r={r:+.2f}  (only {len(shared)} shared days - NOT trustworthy yet)"
            else:
                if r < -0.2: tag = "  <- excellent diversifier (negative)"
                elif r < 0.3: tag = "  <- good, largely uncorrelated"
                elif r < 0.6: tag = "  <- mildly correlated"
                else: tag = "  <- highly correlated (little diversification)"
                verdict = f"r={r:+.2f}{tag}"
            print(f"  {s1:9} vs {s2:9}: {verdict}")

    print("\nGoal: hold strategies with LOW or NEGATIVE correlation. That's the only")
    print("free lunch - a smoother combined curve than any single strategy alone.")
    print("Correlation does NOT prove any strategy makes money; it only describes")
    print("how their returns move together. Edge still comes from validation.")
    print("==================================================\n")


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else "trades.jsonl"
    correlation_report(TradeRecorder.load(path))


if __name__ == "__main__":
    main()
