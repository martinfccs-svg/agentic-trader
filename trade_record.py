"""Trade recording for Monte Carlo analysis.

TradeRecorder appends closed trades to trades.jsonl (one JSON line per trade).
Each line is a TradeRecord with entry/exit times and prices, shares, realized P&L, and R-multiple.

The file is the bridge to montecarlo.py: the bot writes it as trades close,
and montecarlo.py reads it back to run distribution analysis.

Flow: position closes → broker calls recorder.record(...) → JSON line appended → Monte Carlo consumes it.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from models import Position, System

log = logging.getLogger("trade_record")


@dataclass
class TradeRecord:
    """One closed trade: entry/exit times and prices, shares, realized P&L, R-multiple."""
    
    ticker: str
    system: str  # "swing" or "intraday"
    entry_time: float
    exit_time: float
    entry_price: float
    exit_price: float
    shares: float
    realized_pnl: float
    r_multiple: float  # (exit_price - entry_price) / (entry_price - stop_price)
    recorded_at: float = field(default_factory=time.time)
    
    def to_dict(self) -> dict:
        """Convert to dict for JSON serialization."""
        return asdict(self)


class TradeRecorder:
    """Appends closed trades to trades.jsonl."""
    
    def __init__(self, filepath: str = "/app/trades.jsonl") -> None:
        self.filepath = Path(filepath)
        # Create parent directory if it doesn't exist
        self.filepath.parent.mkdir(parents=True, exist_ok=True)
    
    def record(self, position: Position, exit_price: float, realized_pnl: float) -> None:
        """Record a closed position as a JSON line in trades.jsonl.
        
        Args:
            position: The closed Position object (has entry_time, entry_price, stop_price, etc.)
            exit_price: The price at which the position was closed
            realized_pnl: The realized P&L from the trade
        """
        exit_time = time.time()
        
        # Calculate R-multiple: (exit - entry) / (entry - stop)
        # If stop >= entry (invalid stop), R-multiple is 0
        risk = position.entry_price - position.entry_stop
        if risk <= 0:
            r_multiple = 0.0
        else:
            r_multiple = (exit_price - position.entry_price) / risk
        
        # Create the trade record
        record = TradeRecord(
            ticker=position.ticker,
            system=position.system.value,
            entry_time=position.entry_time,
            exit_time=exit_time,
            entry_price=position.entry_price,
            exit_price=exit_price,
            shares=position.shares,
            realized_pnl=realized_pnl,
            r_multiple=r_multiple,
        )
        
        # Append as JSON line
        try:
            with open(self.filepath, "a") as f:
                f.write(json.dumps(record.to_dict()) + "\n")
            log.info(
                "RECORDED %s %s x%.4f entry=%.4f exit=%.4f pnl=%.2f r=%.2f",
                position.ticker, position.system.value, position.shares,
                position.entry_price, exit_price, realized_pnl, r_multiple
            )
        except Exception as e:
            log.error("Failed to record trade: %s", e)
