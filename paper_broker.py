"""Paper broker: the execution + accounting layer that v4.1 was missing.

This is where the -$15-vs-$450 reconciliation problem gets fixed structurally:
realized P&L moves only when a position is CLOSED; unrealized is a separate,
clearly-labelled mark. They are never added into one ambiguous number.

PAPER mode fills at the observed price plus a modeled cost (slippage +
commission), so the scorecard is cost-adjusted rather than fantasy. LIVE mode
is intentionally not auto-executing here; it raises so you wire your own
confirmed-execution path (e.g. Robinhood MCP with a phone confirm).
"""

from __future__ import annotations

import logging

from config import (
    COMMISSION_PER_TRADE,
    SLIPPAGE_BPS,
    START_EQUITY,
    TRADING_MODE,
)
from models import Fill, Position, Side, System

log = logging.getLogger("broker")


class PaperBroker:
    def __init__(self, start_equity: float = START_EQUITY) -> None:
        self.cash = start_equity
        self.start_equity = start_equity
        self.positions: dict[str, Position] = {}
        self.fills: list[Fill] = []
        # Realized P&L is split per system so each book is judged on its own.
        self.realized_pnl: dict[System, float] = {s: 0.0 for s in System}
        self.realized_today: float = 0.0

    # ----- cost model -----------------------------------------------------

    @staticmethod
    def _apply_slippage(price: float, side: Side) -> float:
        adj = price * (SLIPPAGE_BPS / 10_000.0)
        return price + adj if side is Side.BUY else price - adj

    # ----- open / close ---------------------------------------------------

    def buy(self, ticker: str, shares: float, price: float, system: System,
            source, stop_price: float) -> Position:
        if TRADING_MODE == "LIVE":
            raise NotImplementedError(
                "LIVE mode must route through a confirmed-execution path, not "
                "the paper broker. Wire your broker/MCP here with a phone confirm."
            )
        fill_price = self._apply_slippage(price, Side.BUY)
        cost = fill_price * shares + COMMISSION_PER_TRADE
        self.cash -= cost
        self.fills.append(Fill(ticker, Side.BUY, shares, fill_price, COMMISSION_PER_TRADE))
        pos = Position(
            ticker=ticker, system=system, shares=shares, entry_price=fill_price,
            entry_time=__import__("time").time(), stop_price=stop_price,
            source=source, high_water=fill_price, last_price=fill_price,
        )
        self.positions[ticker] = pos
        log.info("BUY %s x%.4f @ %.4f (stop %.4f) [%s]",
                 ticker, shares, fill_price, stop_price, system.value)
        return pos

    def sell(self, ticker: str, price: float) -> float:
        """Close a position. Returns realized P&L for the trade."""
        pos = self.positions.pop(ticker)
        fill_price = self._apply_slippage(price, Side.SELL)
        proceeds = fill_price * pos.shares - COMMISSION_PER_TRADE
        self.cash += proceeds
        cost_basis = pos.entry_price * pos.shares
        realized = proceeds - cost_basis
        self.realized_pnl[pos.system] += realized
        self.realized_today += realized
        self.fills.append(Fill(ticker, Side.SELL, pos.shares, fill_price, COMMISSION_PER_TRADE))
        log.info("SELL %s x%.4f @ %.4f -> realized %.2f [%s]",
                 ticker, pos.shares, fill_price, realized, pos.system.value)
        return realized

    # ----- marks + equity -------------------------------------------------

    def mark(self, ticker: str, price: float) -> None:
        pos = self.positions.get(ticker)
        if pos:
            pos.last_price = price
            pos.high_water = max(pos.high_water, price)

    def unrealized_pnl(self, system: System | None = None) -> float:
        total = 0.0
        for pos in self.positions.values():
            if system and pos.system is not system:
                continue
            total += pos.unrealized_pnl or 0.0
        return total

    @property
    def equity(self) -> float:
        """Cash + marked value of open positions. The honest single number."""
        held = sum((p.last_price or p.entry_price) * p.shares for p in self.positions.values())
        return self.cash + held

    def reset_daily(self) -> None:
        self.realized_today = 0.0
