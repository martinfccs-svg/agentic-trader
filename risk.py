"""Shared risk sizing: risk a fixed % of equity per trade, capped.

size = (equity * RISK_PER_TRADE_PCT) / risk_per_share, where risk_per_share is
the distance from entry to stop. Capped by MAX_POSITION_SIZE and available cash.
This replaces v4.1's flat $3,000 sizing with risk-based sizing.
"""

from __future__ import annotations

from config import MAX_POSITION_SIZE, RISK_PER_TRADE_PCT


def position_size(equity: float, entry: float, stop: float, cash: float) -> float:
    risk_per_share = entry - stop
    if risk_per_share <= 0:
        return 0.0
    dollar_risk = equity * RISK_PER_TRADE_PCT
    shares = dollar_risk / risk_per_share
    # cap by max position notional and by cash on hand
    shares = min(shares, MAX_POSITION_SIZE / entry, cash / entry)
    return max(shares, 0.0)
