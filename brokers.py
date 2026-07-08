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

import hashlib
import logging
import time
from datetime import datetime, timezone
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
import audit

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
    def __init__(self, start_equity: float = START_EQUITY, recorder=None, clock=None) -> None:
        self._recorder = recorder
        self._clock = clock or time.time   # backtest injects the simulated clock
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
        pos = Position(ticker, system, shares, fp, self._clock(), stop_price,
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
                pos.entry_time, self._clock(), pos.entry_price, exit_price,
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

# Alpaca error codes observed in the 2026-07-06 incident
_POSITION_NOT_FOUND = 40410000   # 404: position not found
_INSUFFICIENT_QTY = 40310000     # 403: qty held for open (bracket) orders


def _alpaca_error_code(err) -> Optional[int]:
    """Extract Alpaca's numeric error code from an APIError, defensively."""
    code = getattr(err, "code", None)
    if isinstance(code, int):
        return code
    msg = str(err)
    for known in (_POSITION_NOT_FOUND, _INSUFFICIENT_QTY):
        if str(known) in msg:
            return known
    return None


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
        # trade_logger.print_scorecard reads broker.start_equity; its absence
        # crashed the shutdown path on 2026-07-06 (AttributeError).
        try:
            self.start_equity = float(self._client.get_account().equity)
        except Exception:  # noqa: BLE001
            self.start_equity = START_EQUITY
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
        from alpaca.common.exceptions import APIError
        from alpaca.trading.requests import (
            LimitOrderRequest, MarketOrderRequest, StopLossRequest, TakeProfitRequest,
        )
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        from config import MAX_SLIPPAGE_BPS, TAKE_PROFIT_R, USE_BRACKET_ORDERS

        qty = round(shares)
        if qty <= 0:
            return None

        # --- Duplicate guard #1: never buy what the broker already holds. ---
        # After a crash-restart the local tracker is empty while Alpaca still
        # holds shares; on 2026-07-06 this tripled TSLA (3 x 24 = 72 shares).
        try:
            existing = self._client.get_open_position(ticker)
            if existing is not None and float(existing.qty) > 0:
                log.error("[ALPACA] REFUSING BUY %s: broker already holds %s "
                          "shares not in local tracker (restart desync). Run "
                          "reconcile_at_startup().", ticker, existing.qty)
                return None
        except APIError as e:
            if _alpaca_error_code(e) != _POSITION_NOT_FOUND:
                log.warning("[ALPACA] pre-buy position check %s: %s", ticker, e)
            # 404 = no existing position: safe to proceed.

        # --- Duplicate guard #2: deterministic client_order_id. -------------
        # Same (system, ticker, minute) after a restart hashes to the same id,
        # so Alpaca rejects the re-fired order instead of filling it again.
        minute = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
        coid = (f"bot-{system.value}-"
                + hashlib.sha256(f"{system.value}:{ticker}:{minute}".encode())
                .hexdigest()[:16])

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
                client_order_id=coid,
            )
            log.warning("[ALPACA] BRACKET BUY %s x%d limit<=%.2f stop=%.2f target=%.2f [%s] coid=%s",
                        ticker, qty, limit, stop_price, target, system.value, coid)
        else:
            order = MarketOrderRequest(symbol=ticker, qty=qty, side=OrderSide.BUY,
                                       time_in_force=TimeInForce.DAY,
                                       client_order_id=coid)
            log.warning("[ALPACA] MARKET BUY %s x%d [%s] coid=%s",
                        ticker, qty, system.value, coid)

        try:
            self._client.submit_order(order)
        except APIError as e:
            if "client_order_id" in str(e).lower() or "duplicate" in str(e).lower():
                log.warning("[ALPACA] duplicate order suppressed for %s "
                            "(coid=%s) — original from before restart still "
                            "stands", ticker, coid)
                return None
            raise
        pos = Position(ticker, system, qty, price, time.time(),
                       stop_price, source, entry_stop=stop_price, high_water=price, last_price=price)
        self.positions[ticker] = pos
        audit.fill(ticker=ticker, qty=qty, price=round(price, 2),
                   stop=round(stop_price, 2), system=system.value, coid=coid)
        return pos

    def _cancel_open_orders(self, ticker) -> None:
        """Cancel all open orders for a ticker. Bracket stop/target legs hold
        the shares (held_for_orders), which made close_position 403 on
        2026-07-06 (GOOGL 27/27 held, TSLA 72/72 held). Cancel first."""
        from alpaca.common.exceptions import APIError
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        try:
            open_orders = self._client.get_orders(
                GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[ticker]))
        except APIError as e:
            log.warning("[ALPACA] could not list open orders for %s: %s", ticker, e)
            return
        for order in open_orders:
            try:
                self._client.cancel_order_by_id(order.id)
                log.info("[ALPACA] canceled order %s for %s (releasing hold)",
                         order.id, ticker)
            except APIError as e:
                log.warning("[ALPACA] cancel %s for %s: %s", order.id, ticker, e)

    def _await_qty_release(self, ticker, timeout: float = 5.0) -> str:
        """Poll until held qty is released. Returns 'ready', 'flat', or 'timeout'."""
        from alpaca.common.exceptions import APIError
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                p = self._client.get_open_position(ticker)
                if float(getattr(p, "qty_available", 0) or 0) > 0:
                    return "ready"
            except APIError as e:
                if _alpaca_error_code(e) == _POSITION_NOT_FOUND:
                    return "flat"   # canceling legs revealed nothing left
                log.warning("[ALPACA] poll %s: %s", ticker, e)
            time.sleep(0.25)
        return "timeout"

    def _await_fill_price(self, order_id, timeout: float = 3.0):
        """Poll an order briefly for its actual filled_avg_price. Market
        closes usually fill sub-second; if not filled in time, return None
        and the caller falls back to the quote estimate."""
        from alpaca.common.exceptions import APIError
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                o = self._client.get_order_by_id(order_id)
                if str(getattr(o, "status", "")).lower().endswith("filled") \
                        and getattr(o, "filled_avg_price", None):
                    return float(o.filled_avg_price)
            except APIError as e:
                log.warning("[ALPACA] poll order %s: %s", order_id, e)
                return None
            time.sleep(0.3)
        return None

    def _find_closing_fill_price(self, ticker):
        """Find the actual fill price of the most recent filled SELL order
        for a ticker — i.e. the bracket leg that closed the position
        broker-side. Returns None if not found."""
        from alpaca.trading.enums import OrderSide, QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        try:
            orders = self._client.get_orders(GetOrdersRequest(
                status=QueryOrderStatus.CLOSED, symbols=[ticker],
                side=OrderSide.SELL, limit=20))
        except Exception as e:  # noqa: BLE001
            log.warning("[ALPACA] closed-order lookup %s: %s", ticker, e)
            return None
        best = None
        for o in orders:
            if not str(getattr(o, "status", "")).lower().endswith("filled"):
                continue
            fap = getattr(o, "filled_avg_price", None)
            fat = getattr(o, "filled_at", None)
            if fap and (best is None or (fat and best[0] and fat > best[0])):
                best = (fat, float(fap))
        return best[1] if best else None

    def reconcile_filled_legs(self, system=None) -> dict[str, float]:
        """Close out locally-tracked positions the broker no longer holds —
        a bracket leg (stop or target) filled broker-side while the local
        tracker still carried the position.

        Observed 2026-07-08: AMZN/NVDA legs filled, phantoms persisted 8+
        minutes (blocking re-entry, inflating unrealized) and were finally
        recorded at the flatten-time QUOTE instead of the leg's real fill.
        This books them promptly, at the ACTUAL fill price when findable.

        Returns {ticker: realized} so the calling engine can feed its own
        trade logger. Never raises.
        """
        out: dict[str, float] = {}
        candidates = [t for t, p in self.positions.items()
                      if system is None or p.system is system]
        if not candidates:
            return out
        try:
            broker_syms = {p.symbol for p in self._client.get_all_positions()}
        except Exception as e:  # noqa: BLE001
            log.warning("[ALPACA] reconcile_filled_legs: cannot list broker "
                        "positions: %s", e)
            return out
        for ticker in candidates:
            if ticker in broker_syms:
                continue
            pos = self.positions.get(ticker)
            if pos is None:
                continue
            fill = self._find_closing_fill_price(ticker)
            if fill is None:
                fill = pos.last_price or pos.entry_price
                log.warning("[ALPACA] RECONCILE %s: leg fill not found — "
                            "booking at last mark %.2f (estimate)",
                            ticker, fill)
            self.positions.pop(ticker, None)
            realized = (fill - pos.entry_price) * pos.shares
            self.realized_pnl[pos.system] += realized
            self.realized_today += realized
            log.warning("[ALPACA] RECONCILED %s: bracket leg filled "
                        "broker-side @ %.2f -> %+.2f [%s]",
                        ticker, fill, realized, pos.system.value)
            audit.close(ticker=ticker, qty=pos.shares, price=round(fill, 2),
                        entry=round(pos.entry_price, 2),
                        realized=round(realized, 2), system=pos.system.value,
                        via="bracket_leg")
            if self._recorder:
                self._recorder.record(TradeRecord.build(
                    pos.ticker, pos.system.value,
                    pos.source.value if pos.source else "",
                    pos.entry_time, time.time(), pos.entry_price, fill,
                    pos.shares, pos.entry_stop, realized))
            out[ticker] = realized
        return out

    def sell(self, ticker, price):
        """Close a position. Order of operations matters:
          1. cancel bracket legs  2. wait for hold release  3. close
          4. pop local position ONLY after broker confirms flat.
        The old version popped first, so a failed close erased local state
        while shares stayed at the broker — the core desync of 2026-07-06.
        A 404 from Alpaca means already flat (a bracket leg filled) and is
        treated as success. Genuine failures re-raise WITHOUT popping, so
        callers can retry next cycle."""
        self._guard_live()
        from alpaca.common.exceptions import APIError

        pos = self.positions.get(ticker)
        if pos is None:
            log.warning("[ALPACA] sell %s: not in local tracker, skipping", ticker)
            return 0.0

        self._cancel_open_orders(ticker)
        state = self._await_qty_release(ticker)

        close_order = None
        if state != "flat":
            try:
                close_order = self._client.close_position(ticker)
            except APIError as e:
                if _alpaca_error_code(e) == _POSITION_NOT_FOUND:
                    log.info("[ALPACA] %s already flat at broker "
                             "(bracket leg filled)", ticker)
                else:
                    # Keep the local position so flatten/manage retries later.
                    log.error("[ALPACA] close %s FAILED (position kept for "
                              "retry): %s", ticker, e)
                    raise
        else:
            log.info("[ALPACA] %s already flat at broker after leg cancel", ticker)

        # Prefer the ACTUAL fill price over the quote estimate. If our close
        # order filled, poll it; if the position was already flat, the real
        # exit was a bracket leg — look up its fill. Fall back to the quote.
        fill = None
        if close_order is not None:
            fill = self._await_fill_price(close_order.id)
        else:
            fill = self._find_closing_fill_price(ticker)
        exit_price = fill if fill is not None else price
        price_src = "fill" if fill is not None else "quote-est"

        self.positions.pop(ticker, None)
        realized = (exit_price - pos.entry_price) * pos.shares
        self.realized_pnl[pos.system] += realized
        self.realized_today += realized
        log.warning("[ALPACA] SELL %s exit=%.4f (%s) -> %+.2f [%s]",
                    ticker, exit_price, price_src, realized, pos.system.value)
        audit.close(ticker=ticker, qty=pos.shares, price=round(exit_price, 2),
                    entry=round(pos.entry_price, 2),
                    realized=round(realized, 2), system=pos.system.value,
                    via=price_src)
        if self._recorder:
            self._recorder.record(TradeRecord.build(
                pos.ticker, pos.system.value, pos.source.value if pos.source else "",
                pos.entry_time, time.time(), pos.entry_price, exit_price,
                pos.shares, pos.entry_stop, realized))
        return realized

    def reconcile_at_startup(self) -> list[str]:
        """Rebuild self.positions from the broker (source of truth at boot).

        After each 2026-07-06 crash the bot restarted with an empty tracker
        while Alpaca still held shares + live bracket legs, so it re-bought
        and later 404'd. This must run BEFORE the first cycle.

        Returns the list of ORPHAN tickers: broker holdings whose origin
        can't be matched to a bot order today. If non-empty, the caller
        must HALT — unknown holdings are unknown risk; do not liquidate
        silently, a human should look.
        """
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        orphans: list[str] = []
        broker_positions = self._client.get_all_positions()
        if not broker_positions:
            log.info("[ALPACA] reconcile: broker flat, nothing to adopt")
            return orphans

        # Map ticker -> System via our coid prefix on today's parent orders
        # (bracket child legs get auto-generated ids, so scan ALL statuses).
        sys_by_ticker: dict[str, System] = {}
        stop_by_ticker: dict[str, float] = {}
        try:
            todays = self._client.get_orders(GetOrdersRequest(
                status=QueryOrderStatus.ALL, limit=500, nested=True))
        except Exception as e:  # noqa: BLE001
            log.error("[ALPACA] reconcile: cannot list orders: %s", e)
            todays = []
        for o in todays:
            coid = getattr(o, "client_order_id", "") or ""
            if coid.startswith("bot-"):
                parts = coid.split("-")
                if len(parts) >= 3:
                    system = next((s for s in System if s.value == parts[1]), None)
                    if system:
                        sys_by_ticker[o.symbol] = system
            sp = getattr(o, "stop_price", None)
            status = str(getattr(o, "status", "")).lower()
            if sp and ("new" in status or "held" in status or "accepted" in status):
                stop_by_ticker[o.symbol] = float(sp)

        for p in broker_positions:
            ticker = p.symbol
            system = sys_by_ticker.get(ticker)
            if system is None:
                orphans.append(ticker)
                log.critical("[ALPACA] reconcile: ORPHAN %s x%s — no bot "
                             "order found; resolve manually before trading",
                             ticker, p.qty)
                continue
            entry = float(p.avg_entry_price)
            qty = float(p.qty)
            last = float(getattr(p, "current_price", None) or entry)
            stop = stop_by_ticker.get(ticker, round(entry * 0.99, 2))
            self.positions[ticker] = Position(
                ticker, system, qty, entry, time.time(), stop, None,
                entry_stop=stop, high_water=max(entry, last), last_price=last)
            log.warning("[ALPACA] reconcile: re-adopted %s x%s [%s] "
                        "entry=%.2f stop=%.2f", ticker, qty, system.value,
                        entry, stop)
        return orphans

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
