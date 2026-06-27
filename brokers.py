"""Execution layer.

PaperBroker  - simulates cost-adjusted fills. Used in PAPER mode (now against
               REAL prices from Finnhub). Realized vs unrealized kept separate.
AlpacaBroker - places real orders via Alpaca's Trading API. Paper or live
               depending on the endpoint; live money is gated in config
               (live_money_armed()) and re-checked here before every order.

Finnhub is data only and cannot execute — that is why execution lives here, in a
separate broker, not in the feed layer.
"""

from __future__ import annotations

import logging
import time
from typing import Optional, Protocol

from config import (
    ALPACA_API_KEY,
    ALPACA_PAPER,
    ALPACA_SECRET_KEY,
    COMMISSION_PER_TRADE,
    SLIPPAGE_BPS,
    START_EQUITY,
    live_money_armed,
)
from models import Fill, Position, Side, System
from trade_record import TradeRecord, TradeRecorder

log = logging.getLogger("broker")


class Broker(Protocol):
    positions: dict[str, Position]
    realized_pnl: dict[System, float]
    realized_today: float
    def buy(self, ticker: str, shares: float, price: float, system: System,
            source, stop_price: float) -> Optional[Position]: ...
    def sell(self, ticker: str, price: float) -> float: ...
    def mark(self, ticker: str, price: float) -> None: ...
    def unrealized_pnl(self, system: System | None = None) -> float: ...
    @property
    def equity(self) -> float: ...
    def reset_daily(self) -> None: ...


# ============================ PAPER ======================================

class PaperBroker:
    def __init__(self, start_equity: float = START_EQUITY, recorder=None) -> None:
        self._recorder = recorder
        self.cash = start_equity
        self.start_equity = start_equity
        self.positions: dict[str, Position] = {}
        self.fills: list[Fill] = []
        self.realized_pnl: dict[System, float] = {s: 0.0 for s in System}
        self.realized_today = 0.0

    @staticmethod
    def _slip(price: float, side: Side) -> float:
        adj = price * (SLIPPAGE_BPS / 10_000.0)
        return price + adj if side is Side.BUY else price - adj

    def buy(self, ticker, shares, price, system, source, stop_price):
        fp = self._slip(price, Side.BUY)
        self.cash -= fp * shares + COMMISSION_PER_TRADE
        self.fills.append(Fill(ticker, Side.BUY, shares, fp, COMMISSION_PER_TRADE))
        pos = Position(ticker, system, shares, fp, time.time(), stop_price,
                       source, entry_stop=stop_price, high_water=fp, last_price=fp)
        self.positions[ticker] = pos
        log.info("[PAPER] BUY %s x%.4f @ %.4f stop %.4f [%s]",
                 ticker, shares, fp, stop_price, system.value)
        return pos

    def sell(self, ticker, price):
        pos = self.positions.pop(ticker)
        fp = self._slip(price, Side.SELL)
        self.cash += fp * pos.shares - COMMISSION_PER_TRADE
        realized = (fp - pos.entry_price) * pos.shares - COMMISSION_PER_TRADE
        self.realized_pnl[pos.system] += realized
        self.realized_today += realized
        self.fills.append(Fill(ticker, Side.SELL, pos.shares, fp, COMMISSION_PER_TRADE))
        log.info("[PAPER] SELL %s x%.4f @ %.4f -> %.2f [%s]",
                 ticker, pos.shares, fp, realized, pos.system.value)
        self._emit(pos, fp, realized)
        return realized

    def _emit(self, pos, exit_price, realized):
        if self._recorder:
            self._recorder.record(TradeRecord.build(
                pos.ticker, pos.system.value, pos.source.value if pos.source else "",
                pos.entry_time, time.time(), pos.entry_price, exit_price,
                pos.shares, pos.entry_stop, realized))

    def mark(self, ticker, price):
        pos = self.positions.get(ticker)
        if pos:
            pos.last_price = price
            pos.high_water = max(pos.high_water, price)

    def unrealized_pnl(self, system=None):
        return sum(p.unrealized_pnl or 0.0 for p in self.positions.values()
                   if system is None or p.system is system)

    @property
    def equity(self):
        held = sum((p.last_price or p.entry_price) * p.shares for p in self.positions.values())
        return self.cash + held

    def reset_daily(self):
        self.realized_today = 0.0


# ============================ ALPACA (real) ==============================

class AlpacaBroker:
    """Real order execution via Alpaca. VERIFY against alpaca-py before trusting:
    field names and SDK signatures can change. Live money requires
    config.live_money_armed() to be true; otherwise every order is refused.
    """

    def __init__(self, recorder=None) -> None:
        self._recorder = recorder
        if not (ALPACA_API_KEY and ALPACA_SECRET_KEY):
            raise RuntimeError("BROKER=alpaca but ALPACA_API_KEY/SECRET not set.")
        try:
            from alpaca.trading.client import TradingClient
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("pip install alpaca-py") from exc
        # paper=True hits the paper endpoint; paper=False is REAL money.
        self._client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=ALPACA_PAPER)
        self.positions: dict[str, Position] = {}
        self.realized_pnl: dict[System, float] = {s: 0.0 for s in System}
        self.realized_today = 0.0
        mode = "LIVE-MONEY" if live_money_armed() else "alpaca-paper"
        log.warning("AlpacaBroker initialised in %s mode", mode)

    def _guard_live(self) -> None:
        if not ALPACA_PAPER and not live_money_armed():
            raise RuntimeError(
                "Refusing real-money order: live gate not armed. Set TRADING_MODE=LIVE, "
                "BROKER=alpaca, ALPACA_PAPER=false, and LIVE_CONFIRM to the exact phrase."
            )

    def buy(self, ticker, shares, price, system, source, stop_price):
        self._guard_live()
        from alpaca.trading.requests import (
            LimitOrderRequest, MarketOrderRequest, StopLossRequest, TakeProfitRequest,
        )
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        from config import MAX_SLIPPAGE_BPS, TAKE_PROFIT_R, USE_BRACKET_ORDERS

        qty = round(shares)
        if qty <= 0:
            return None

        if USE_BRACKET_ORDERS:
            # Marketable LIMIT entry: cap the worst fill we'll accept (slippage guard).
            limit = round(price * (1 + MAX_SLIPPAGE_BPS / 10_000.0), 2)
            # Broker-side protective stop + target. These live on Alpaca, so they
            # trigger even if this bot is down -- the stop no longer dies with us.
            risk_per_share = max(price - stop_price, 0.01)
            target = round(price + TAKE_PROFIT_R * risk_per_share, 2)
            order = LimitOrderRequest(
                symbol=ticker, qty=qty, side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY, limit_price=limit,
                order_class=OrderClass.BRACKET,
                stop_loss=StopLossRequest(stop_price=round(stop_price, 2)),
                take_profit=TakeProfitRequest(limit_price=target),
            )
            log.warning("[ALPACA] BRACKET BUY %s x%d limit<=%.2f stop=%.2f target=%.2f [%s]",
                        ticker, qty, limit, stop_price, target, system.value)
        else:
            order = MarketOrderRequest(symbol=ticker, qty=qty, side=OrderSide.BUY,
                                       time_in_force=TimeInForce.DAY)
            log.warning("[ALPACA] MARKET BUY %s x%d [%s]", ticker, qty, system.value)

        self._client.submit_order(order)
        pos = Position(ticker, system, qty, price, time.time(),
                       stop_price, source, entry_stop=stop_price, high_water=price, last_price=price)
        self.positions[ticker] = pos
        return pos

    def sell(self, ticker, price):
        self._guard_live()
        pos = self.positions.pop(ticker)
        self._client.close_position(ticker)
        realized = (price - pos.entry_price) * pos.shares
        self.realized_pnl[pos.system] += realized
        self.realized_today += realized
        log.warning("[ALPACA] SELL %s submitted -> est %.2f [%s]",
                    ticker, realized, pos.system.value)
        if self._recorder:
            self._recorder.record(TradeRecord.build(
                pos.ticker, pos.system.value, pos.source.value if pos.source else "",
                pos.entry_time, time.time(), pos.entry_price, price,
                pos.shares, pos.entry_stop, realized))
        return realized

    def mark(self, ticker, price):
        pos = self.positions.get(ticker)
        if pos:
            pos.last_price = price
            pos.high_water = max(pos.high_water, price)

    def unrealized_pnl(self, system=None):
        return sum(p.unrealized_pnl or 0.0 for p in self.positions.values()
                   if system is None or p.system is system)

    @property
    def equity(self):
        try:
            return float(self._client.get_account().equity)
        except Exception:  # noqa: BLE001
            held = sum((p.last_price or p.entry_price) * p.shares
                       for p in self.positions.values())
            return held

    def reset_daily(self):
        self.realized_today = 0.0


def build_broker(recorder=None):
    from config import BROKER
    if BROKER == "alpaca":
        return AlpacaBroker(recorder=recorder)
    return PaperBroker(recorder=recorder)
