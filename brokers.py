"""Execution layer.

PaperBroker  - simulates cost-adjusted fills. Used in PAPER mode (now against
               REAL prices from Finnhub). Realized vs unrealized kept separate.
AlpacaBroker - places real orders via Alpaca's Trading API. Paper or live
               depending on the endpoint; live money is gated in config
               (live_money_armed()) and re-checked here before every ENTRY.
               Closes are always allowed: reducing risk must never be blocked
               by the gate.

Finnhub is data only and cannot execute — that is why execution lives here, in a
separate broker, not in the feed layer.

Incident history this file defends against (2026-07-06 and 2026-07-07):
  * bot crashed mid-manage on a stale position (404) and died          -> sell() treats 404 as already-flat
  * shutdown flatten 403'd: bracket legs held all shares               -> cancel legs, await release, then close
  * restart with empty tracker while broker held shares                -> reconcile_at_startup()
  * restart while UNFILLED entry orders were still resting; they
    filled later as untracked exposure and the account went negative   -> reconcile sweeps OPEN ORDERS too
  * pre-buy safety check failed open on a network error                -> now fails CLOSED (skip the trade)
  * swing positions older than the order-lookback misread as orphans   -> persistent positions.json sidecar
  * realized P&L estimated from quotes, not fills (understated losses
    exactly when stops were firing)                                    -> read filled_avg_price after close
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
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

# Sidecar files (survive restarts; Railway volumes or repo-local dir).
STATE_DIR = os.environ.get("STATE_DIR", "state")
POSITIONS_FILE = os.path.join(STATE_DIR, "positions.json")

# How far back reconcile looks for our parent orders. Swing holds overnight,
# so "today's orders" is not enough (that misread old swings as orphans).
RECONCILE_ORDER_LOOKBACK_DAYS = 7


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

# Alpaca error codes observed in the 2026-07-06/07 incidents
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
    config.live_money_armed() to be true for ENTRIES; closes always allowed.
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
        self._last_good_equity: Optional[float] = None
        # trade_logger.print_scorecard reads broker.start_equity; its absence
        # crashed the shutdown path on 2026-07-06 (AttributeError).
        try:
            self.start_equity = float(self._client.get_account().equity)
            self._last_good_equity = self.start_equity
        except Exception:  # noqa: BLE001
            self.start_equity = START_EQUITY
        mode = "LIVE-MONEY" if live_money_armed() else "alpaca-paper"
        log.warning("AlpacaBroker initialised in %s mode", mode)

    # ---------------------- persistence sidecar --------------------------
    # The order-history lookback cannot always reattribute old swing holds
    # (parent order ages out of the window). positions.json is a durable
    # secondary matcher so a legit multi-day swing isn't flagged ORPHAN and
    # doesn't halt the bot on every restart.

    def _save_book(self) -> None:
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            data = {
                t: {
                    "system": p.system.value,
                    "shares": p.shares,
                    "entry_price": p.entry_price,
                    "entry_time": p.entry_time,
                    "stop_price": p.stop_price,
                    "entry_stop": p.entry_stop,
                    "source": p.source.value if p.source else None,
                }
                for t, p in self.positions.items()
            }
            tmp = POSITIONS_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=1)
            os.replace(tmp, POSITIONS_FILE)   # atomic: no torn file on crash
        except Exception as e:  # noqa: BLE001
            log.error("[ALPACA] could not persist position book: %s", e)

    @staticmethod
    def _load_saved_book() -> dict:
        try:
            with open(POSITIONS_FILE) as f:
                return json.load(f)
        except FileNotFoundError:
            return {}
        except Exception as e:  # noqa: BLE001
            log.error("[ALPACA] could not read saved position book: %s", e)
            return {}

    # ---------------------- gates & guards --------------------------------

    def _guard_live_entry(self) -> None:
        if not ALPACA_PAPER and not live_money_armed():
            raise RuntimeError(
                "Refusing real-money ENTRY: live gate not armed. Set TRADING_MODE=LIVE, "
                "BROKER=alpaca, ALPACA_PAPER=false, and LIVE_CONFIRM to the exact phrase."
            )

    # ---------------------- orders ----------------------------------------

    def buy(self, ticker, shares, price, system, source, stop_price):
        self._guard_live_entry()
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
        # FAIL CLOSED: if we cannot VERIFY the broker is flat in this ticker,
        # we skip the trade. Missing one entry costs a trade; a duplicate
        # fill on unverified state is how accounts blow up.
        try:
            existing = self._client.get_open_position(ticker)
            if existing is not None and float(existing.qty) > 0:
                log.error("[ALPACA] REFUSING BUY %s: broker already holds %s "
                          "shares not in local tracker (restart desync). Run "
                          "reconcile_at_startup().", ticker, existing.qty)
                return None
        except APIError as e:
            if _alpaca_error_code(e) != _POSITION_NOT_FOUND:
                log.error("[ALPACA] REFUSING BUY %s: pre-buy position check "
                          "failed (%s) — cannot verify broker state, "
                          "failing closed", ticker, e)
                return None
            # 404 = no existing position: safe to proceed.
        except Exception as e:  # noqa: BLE001  (network, timeout, etc.)
            log.error("[ALPACA] REFUSING BUY %s: pre-buy position check "
                      "errored (%s) — failing closed", ticker, e)
            return None

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
        self._save_book()
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

    def _actual_sell_fill(self, ticker, order=None, fallback: float = 0.0,
                          timeout: float = 5.0) -> float:
        """Best-effort ACTUAL exit price.

        Realized P&L used to be estimated from the quote we happened to have,
        which understates losses exactly when stops fire (the quote lags the
        stop fill). Since realized_today feeds the daily-loss kill switch,
        estimate error there is a safety problem, not a cosmetic one.

        1. If we submitted the close ourselves, poll THAT order for
           filled_avg_price.
        2. Otherwise (404 path: a bracket leg filled while we weren't
           looking) find the most recent filled SELL for the symbol.
        Falls back to the passed-in quote if the API is unhelpful."""
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        deadline = time.monotonic() + timeout
        order_id = getattr(order, "id", None)
        while time.monotonic() < deadline:
            try:
                if order_id is not None:
                    o = self._client.get_order_by_id(order_id)
                    fap = getattr(o, "filled_avg_price", None)
                    if fap:
                        return float(fap)
                else:
                    closed = self._client.get_orders(GetOrdersRequest(
                        status=QueryOrderStatus.CLOSED, symbols=[ticker],
                        limit=10, nested=True))
                    for o in closed:
                        if "sell" in str(getattr(o, "side", "")).lower():
                            fap = getattr(o, "filled_avg_price", None)
                            if fap:
                                return float(fap)
                    break   # nothing filled yet and nothing to poll by id
            except Exception as e:  # noqa: BLE001
                log.warning("[ALPACA] fill lookup %s: %s", ticker, e)
                break
            time.sleep(0.25)
        log.warning("[ALPACA] using quote %.4f as exit for %s (actual fill "
                    "unavailable — realized P&L is an estimate)", fallback, ticker)
        return fallback

    def sell(self, ticker, price):
        """Close a position. Order of operations matters:
          1. cancel bracket legs  2. wait for hold release  3. close
          4. pop local position ONLY after broker confirms flat.
        The old version popped first, so a failed close erased local state
        while shares stayed at the broker — the core desync of 2026-07-06.
        A 404 from Alpaca means already flat (a bracket leg filled) and is
        treated as success. Genuine failures re-raise WITHOUT popping, so
        callers can retry next cycle.

        NOTE: no live-money gate here. If the gate is unarmed but live
        positions exist, blocking the CLOSE would trap the risk on. Reducing
        exposure is always allowed; we just log loudly."""
        from alpaca.common.exceptions import APIError

        if not ALPACA_PAPER and not live_money_armed():
            log.critical("[ALPACA] closing LIVE position %s while live gate "
                         "is UNARMED — allowed (risk reduction), but find out "
                         "why a live position exists unarmed.", ticker)

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

        exit_price = self._actual_sell_fill(ticker, order=close_order, fallback=price)

        self.positions.pop(ticker, None)
        self._save_book()
        realized = (exit_price - pos.entry_price) * pos.shares
        self.realized_pnl[pos.system] += realized
        self.realized_today += realized
        log.warning("[ALPACA] SELL %s exit=%.4f -> %.2f [%s]",
                    ticker, exit_price, realized, pos.system.value)
        if self._recorder:
            self._recorder.record(TradeRecord.build(
                pos.ticker, pos.system.value, pos.source.value if pos.source else "",
                pos.entry_time, time.time(), pos.entry_price, exit_price,
                pos.shares, pos.entry_stop, realized))
        return realized

    # ---------------------- startup reconciliation -----------------------

    def reconcile_at_startup(self) -> list[str]:
        """Rebuild self.positions from the broker (source of truth at boot).

        After each 2026-07-06 crash the bot restarted with an empty tracker
        while Alpaca still held shares + live bracket legs, so it re-bought
        and later 404'd. This must run BEFORE the first cycle.

        Two sweeps:
          1. POSITIONS — re-adopt anything we can attribute to a bot order
             (coid history) or to the persisted positions.json sidecar.
          2. OPEN ORDERS — an UNFILLED entry from before a crash has no
             position yet, so sweep #1 cannot see it; it fills later as
             untracked exposure. (This is how the 2026-07-07 account went
             from +98.6k to negative with the bot reporting open=0.)
             Bracket legs protecting an adopted position are kept — they
             are the broker-side stop. Stale bot ENTRY orders with no
             position are canceled. Anything not ours is an ORPHAN.

        Returns the list of ORPHANs: broker holdings/orders whose origin
        can't be matched to this bot. If non-empty, the caller must HALT —
        unknown holdings are unknown risk; do not liquidate silently,
        a human should look.
        """
        from alpaca.common.exceptions import APIError
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        orphans: list[str] = []
        saved = self._load_saved_book()

        broker_positions = self._client.get_all_positions()

        # ---- Sweep 1: positions --------------------------------------
        # Map ticker -> System via our coid prefix on recent parent orders
        # (bracket child legs get auto-generated ids, so scan ALL statuses).
        # Lookback covers multi-day swing holds; positions.json is the
        # durable fallback when even that window is too short.
        sys_by_ticker: dict[str, System] = {}
        stop_by_ticker: dict[str, float] = {}
        try:
            lookback = datetime.now(timezone.utc) - timedelta(
                days=RECONCILE_ORDER_LOOKBACK_DAYS)
            recent = self._client.get_orders(GetOrdersRequest(
                status=QueryOrderStatus.ALL, limit=500, nested=True,
                after=lookback))
        except Exception as e:  # noqa: BLE001
            log.error("[ALPACA] reconcile: cannot list orders: %s", e)
            recent = []
        for o in recent:
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
            entry_time = time.time()
            if system is None and ticker in saved:
                # Secondary matcher: the persisted book. Covers swing holds
                # whose parent order aged out of the order-history window.
                sv = saved[ticker]
                system = next((s for s in System if s.value == sv.get("system")), None)
                if system:
                    entry_time = float(sv.get("entry_time") or entry_time)
                    if ticker not in stop_by_ticker and sv.get("stop_price"):
                        stop_by_ticker[ticker] = float(sv["stop_price"])
                    log.info("[ALPACA] reconcile: %s matched via persisted "
                             "book [%s]", ticker, system.value)
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
                ticker, system, qty, entry, entry_time, stop, None,
                entry_stop=stop, high_water=max(entry, last), last_price=last)
            log.warning("[ALPACA] reconcile: re-adopted %s x%s [%s] "
                        "entry=%.2f stop=%.2f", ticker, qty, system.value,
                        entry, stop)

        # ---- Sweep 2: resting open orders ----------------------------
        adopted = set(self.positions)
        try:
            resting = self._client.get_orders(GetOrdersRequest(
                status=QueryOrderStatus.OPEN, limit=500, nested=True))
        except Exception as e:  # noqa: BLE001
            # Cannot trade without knowing what's resting: caller halts.
            log.critical("[ALPACA] reconcile: cannot list OPEN orders: %s", e)
            raise
        for o in resting:
            if o.symbol in adopted:
                continue   # bracket legs protecting an adopted position: keep
            coid = getattr(o, "client_order_id", "") or ""
            if coid.startswith("bot-"):
                try:
                    self._client.cancel_order_by_id(o.id)
                    log.warning("[ALPACA] reconcile: canceled stale bot order "
                                "%s for %s (no matching position — would have "
                                "filled as untracked exposure)", o.id, o.symbol)
                except APIError as e:
                    log.error("[ALPACA] reconcile: cancel %s for %s failed: %s",
                              o.id, o.symbol, e)
                    orphans.append(f"order:{o.symbol}")
            else:
                orphans.append(f"order:{o.symbol}")   # not ours — human decides
                log.critical("[ALPACA] reconcile: ORPHAN open order %s on %s "
                             "(not bot-tagged) — resolve manually", o.id, o.symbol)

        if not broker_positions and not orphans:
            log.info("[ALPACA] reconcile: broker flat, nothing to adopt")
        self._save_book()
        return orphans

    # ---------------------- runtime audit ---------------------------------

    def broker_position_symbols(self) -> set[str]:
        """Symbols the BROKER says we hold. Used by the per-cycle book audit
        in main.cycle(). Raises on API failure — the caller decides whether
        an unverifiable book is tolerable this cycle."""
        return {p.symbol for p in self._client.get_all_positions()}

    # ---------------------- marks / pnl / equity ---------------------------

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
        """Broker-reported account equity. On API failure, return the last
        good reading LOUDLY rather than a silently-wrong number: the old
        fallback summed held value with no cash, so a flat account read as
        $0 and could have tripped (or masked) loss limits incorrectly."""
        try:
            eq = float(self._client.get_account().equity)
            self._last_good_equity = eq
            return eq
        except Exception as e:  # noqa: BLE001
            if self._last_good_equity is not None:
                log.error("[ALPACA] equity fetch failed (%s) — returning last "
                          "good reading %.2f (STALE)", e, self._last_good_equity)
                return self._last_good_equity
            log.error("[ALPACA] equity fetch failed (%s) and no prior reading "
                      "— returning start_equity %.2f (STALE)", e, self.start_equity)
            return self.start_equity

    def reset_daily(self):
        self.realized_today = 0.0


def build_broker(recorder=None):
    from config import BROKER
    if BROKER == "alpaca":
        return AlpacaBroker(recorder=recorder)
    return PaperBroker(recorder=recorder)
