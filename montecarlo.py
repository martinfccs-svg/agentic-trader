"""Monte Carlo analysis for trade distribution.

Reads trades.jsonl (written by TradeRecorder) and runs distribution analysis to
estimate probability of ruin, median final equity, and confidence in the result.

Adjusted from the working version:
  - P&L access handles BOTH dicts (from load_trades_from_jsonl) and TradeRecord
    objects (from TradeRecorder.load) -- the original crashed on dicts.
  - Added a runnable report (python montecarlo.py [path]) that shows the
    small-sample warning the `trustworthy` flag was already computing.
  - Added bad/good-case percentiles and median max drawdown as NEW optional
    fields; the original five fields are unchanged, so existing imports still work.

    python montecarlo.py                     # reads /app/trades.jsonl
    python montecarlo.py trades.jsonl
"""

from __future__ import annotations

import json
import logging
import random
import sys
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("montecarlo")

# Minimum number of trades to trust the Monte Carlo result.
MIN_TRUSTWORTHY = 30


@dataclass
class MonteCarloResult:
    """Result of a Monte Carlo simulation."""

    prob_ruin: float          # Probability of ruin (0.0 to 1.0)
    median_final: float       # Median final equity across all runs
    trustworthy: bool         # True if sample size >= MIN_TRUSTWORTHY
    num_trades: int           # Number of trades analyzed
    num_runs: int             # Number of Monte Carlo runs performed
    # --- added (optional, default-valued so existing callers are unaffected) ---
    p5_final: float = 0.0     # 5th-percentile final equity (bad case)
    p95_final: float = 0.0    # 95th-percentile final equity (good case)
    median_max_drawdown: float = 0.0   # median of each run's worst drawdown


def _pnl(trade) -> float:
    """Read realized P&L from either a TradeRecord object or a plain dict."""
    if hasattr(trade, "realized_pnl"):
        return trade.realized_pnl
    return trade.get("realized_pnl", 0.0)


def run_monte_carlo(
    trades: list,
    start_equity: float = 50_000,
    runs: int = 1000,
    ruin_fraction: float = 0.0,
) -> MonteCarloResult:
    """Run Monte Carlo simulation on a list of trades.

    Args:
        trades: List of TradeRecord objects OR dicts (each has realized_pnl).
        start_equity: Starting account equity.
        runs: Number of Monte Carlo iterations.
        ruin_fraction: Ruin threshold as a fraction of start_equity. Default 0.0
            keeps the original behaviour (ruin = equity hits 0). Set e.g. 0.5 to
            treat a 50% drawdown as ruin.

    Returns:
        MonteCarloResult with prob_ruin, median_final, trustworthy, percentiles,
        and median max drawdown.
    """
    num_trades = len(trades)
    trustworthy = num_trades >= MIN_TRUSTWORTHY

    if num_trades == 0:
        return MonteCarloResult(
            prob_ruin=0.0, median_final=start_equity, trustworthy=False,
            num_trades=0, num_runs=runs,
            p5_final=start_equity, p95_final=start_equity, median_max_drawdown=0.0,
        )

    pnls = [_pnl(t) for t in trades]
    ruin_threshold = start_equity * ruin_fraction

    final_equities: list[float] = []
    drawdowns: list[float] = []
    ruin_count = 0

    for _ in range(runs):
        equity = start_equity
        peak = start_equity
        max_dd = 0.0
        ruined = False
        # Randomly resample trades with replacement and apply their P&L.
        for _ in range(num_trades):
            equity += random.choice(pnls)
            peak = max(peak, equity)
            max_dd = min(max_dd, equity - peak)
            if equity <= ruin_threshold:
                ruined = True
                ruin_count += 1
                break
        final_equities.append(max(equity, 0.0))
        drawdowns.append(max_dd)

    final_equities.sort()
    drawdowns.sort()  # most negative first

    def pct(sorted_vals: list[float], q: float) -> float:
        i = min(int(q * (len(sorted_vals) - 1)), len(sorted_vals) - 1)
        return sorted_vals[i]

    n = len(final_equities)
    return MonteCarloResult(
        prob_ruin=ruin_count / runs if runs > 0 else 0.0,
        median_final=final_equities[n // 2],
        trustworthy=trustworthy,
        num_trades=num_trades,
        num_runs=runs,
        p5_final=pct(final_equities, 0.05),
        p95_final=pct(final_equities, 0.95),
        median_max_drawdown=drawdowns[len(drawdowns) // 2] if drawdowns else 0.0,
    )


def load_trades_from_jsonl(filepath: str = "/app/trades.jsonl") -> list:
    """Load trades from trades.jsonl file.

    Args:
        filepath: Path to trades.jsonl.

    Returns:
        List of trade dicts (one per line). run_monte_carlo accepts these directly.
    """
    trades: list = []
    path = Path(filepath)

    if not path.exists():
        log.warning("trades.jsonl not found at %s", filepath)
        return trades

    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    trades.append(json.loads(line))
        log.info("Loaded %d trades from %s", len(trades), filepath)
    except Exception as e:  # noqa: BLE001
        log.error("Failed to load trades: %s", e)

    return trades


def print_report(r: MonteCarloResult) -> None:
    """Human-readable report. Surfaces the small-sample warning the original
    code computed but never displayed."""
    print("\n================= MONTE CARLO =================")
    if r.num_trades == 0:
        print("No trades found. Let the bot run (paper is fine) so trades.jsonl")
        print("populates, then run this again.")
        print("===============================================\n")
        return
    print(f"trades analysed : {r.num_trades}   |   simulations: {r.num_runs}")
    if not r.trustworthy:
        print(f"!! DIRECTIONAL ONLY: under {MIN_TRUSTWORTHY} trades. Treat as a rough hint,")
        print("   not a result -- resampling few trades just rearranges luck.")
    print(f"final equity    : median {r.median_final:,.0f}  "
          f"| bad case (5th) {r.p5_final:,.0f}  | good (95th) {r.p95_final:,.0f}")
    print(f"max drawdown    : median {r.median_max_drawdown:,.0f}")
    print(f"prob. of ruin   : {r.prob_ruin:.1%}")
    print("Note: this measures the DISPERSION of the edge you have -- it cannot")
    print("tell you the edge is real. Out-of-sample / walk-forward testing does that.")
    print("===============================================\n")


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else "/app/trades.jsonl"
    trades = load_trades_from_jsonl(path)
    print_report(run_monte_carlo(trades))


if __name__ == "__main__":
    main()
