"""The standard trade record — the one ingredient everything else consumes.

Every source of closed trades emits this same shape: the live/paper broker now,
and the future backtest harness later. Monte Carlo, cost analysis, and the
scorecard all read THIS and nothing else, so a new trade source plugs in with
zero downstream changes.

The key normalized field is `r_multiple`: realized P&L expressed in units of the
risk you originally took (entry-to-initial-stop). +3R = made three times the
planned risk; -1R = lost the full planned risk. R-multiples make trades of
different sizes and prices comparable, which is exactly what Monte Carlo needs.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Optional


@dataclass
class TradeRecord:
    ticker: str
    system: str                 # "swing" | "intraday"
    source: str                 # signal source that opened it
    entry_time: float
    exit_time: float
    entry_price: float
    exit_price: float
    shares: float
    realized_pnl: float
    initial_risk: float         # dollars risked at entry = (entry - initial_stop) * shares
    r_multiple: float           # realized_pnl / initial_risk

    @staticmethod
    def build(ticker, system, source, entry_time, exit_time, entry_price,
              exit_price, shares, entry_stop, realized_pnl) -> "TradeRecord":
        risk_per_share = max(entry_price - entry_stop, 0.0)
        initial_risk = risk_per_share * shares
        r = (realized_pnl / initial_risk) if initial_risk > 0 else 0.0
        return TradeRecord(
            ticker=ticker, system=system, source=source,
            entry_time=entry_time, exit_time=exit_time,
            entry_price=round(entry_price, 4), exit_price=round(exit_price, 4),
            shares=round(shares, 4), realized_pnl=round(realized_pnl, 2),
            initial_risk=round(initial_risk, 2), r_multiple=round(r, 4),
        )


class TradeRecorder:
    """Appends TradeRecords to a JSONL file as trades close. One line per trade.
    This file is the bridge: the bot writes it live; Monte Carlo reads it."""

    def __init__(self, path: str = "trades.jsonl") -> None:
        self._path = path

    def record(self, tr: TradeRecord) -> None:
        try:
            with open(self._path, "a") as fh:
                fh.write(json.dumps(asdict(tr)) + "\n")
        except OSError:
            pass

    @staticmethod
    def load(path: str = "trades.jsonl") -> list[TradeRecord]:
        out: list[TradeRecord] = []
        try:
            with open(path) as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        out.append(TradeRecord(**json.loads(line)))
        except FileNotFoundError:
            pass
        return out
