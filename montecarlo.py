"""Monte Carlo analysis for trade distribution.

Reads trades.jsonl (written by TradeRecorder) and runs distribution analysis
to estimate probability of ruin, median final equity, and confidence in the strategy.
"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger("montecarlo")

# Minimum number of trades to trust the Monte Carlo result
MIN_TRUSTWORTHY = 30


@dataclass
class MonteCarloResult:
    """Result of a Monte Carlo simulation."""
    
    prob_ruin: float  # Probability of ruin (0.0 to 1.0)
    median_final: float  # Median final equity across all runs
    trustworthy: bool  # True if sample size >= MIN_TRUSTWORTHY
    num_trades: int  # Number of trades analyzed
    num_runs: int  # Number of Monte Carlo runs performed


def run_monte_carlo(
    trades: list,
    start_equity: float = 50_000,
    runs: int = 1000,
) -> MonteCarloResult:
    """Run Monte Carlo simulation on a list of trades.
    
    Args:
        trades: List of TradeRecord objects (each has realized_pnl)
        start_equity: Starting account equity
        runs: Number of Monte Carlo iterations
    
    Returns:
        MonteCarloResult with prob_ruin, median_final, and trustworthy flag
    """
    num_trades = len(trades)
    trustworthy = num_trades >= MIN_TRUSTWORTHY
    
    if num_trades == 0:
        return MonteCarloResult(
            prob_ruin=0.0,
            median_final=start_equity,
            trustworthy=False,
            num_trades=0,
            num_runs=runs,
        )
    
    # Extract P&L from each trade
    pnls = [t.realized_pnl for t in trades]
    
    # Run Monte Carlo: randomly resample trades with replacement
    final_equities = []
    ruin_count = 0
    
    for _ in range(runs):
        equity = start_equity
        # Randomly sample trades (with replacement) and apply their P&L
        for _ in range(num_trades):
            pnl = random.choice(pnls)
            equity += pnl
            if equity <= 0:
                ruin_count += 1
                break
        final_equities.append(max(equity, 0))
    
    # Calculate results
    final_equities.sort()
    median_final = final_equities[len(final_equities) // 2]
    prob_ruin = ruin_count / runs if runs > 0 else 0.0
    
    return MonteCarloResult(
        prob_ruin=prob_ruin,
        median_final=median_final,
        trustworthy=trustworthy,
        num_trades=num_trades,
        num_runs=runs,
    )


def load_trades_from_jsonl(filepath: str = "/app/trades.jsonl") -> list:
    """Load trades from trades.jsonl file.
    
    Args:
        filepath: Path to trades.jsonl
    
    Returns:
        List of trade dicts (one per line)
    """
    trades = []
    path = Path(filepath)
    
    if not path.exists():
        log.warning("trades.jsonl not found at %s", filepath)
        return trades
    
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    trade = json.loads(line)
                    trades.append(trade)
        log.info("Loaded %d trades from %s", len(trades), filepath)
    except Exception as e:
        log.error("Failed to load trades: %s", e)
    
    return trades
