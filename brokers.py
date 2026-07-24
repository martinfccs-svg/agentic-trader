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
import os
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
# Reconciliation timing guards (2026-07-16 phantom-close incident).
# A tracked position absent from the broker's positions API is only treated
# as "closed by a leg" once BOTH hold: the entry is old enough that a
# settlement lag is implausible, and the absence has persisted across
# several cycles. Anything faster is the broker not having caught up yet.
_ENTRY_SETTLE_GRACE_SECS = float(os.getenv("ENTRY_SETTLE_GRACE_SECS", "90"))
_ABSENT_CONFIRM_SECS = float(os.getenv("ABSENT_CONFIRM_SECS", "60"))

_POSITION_NOT_FOUND = 40410000   # 404: position not found
_INSUFFICIENT_QTY = 40310000     # 403: qty held for open (bracket) orders


def _walk_order(order):
    """Yield an order and every nested child leg, recursively.

    Alpaca's GetOrdersRequest(nested=True) rolls multi-leg orders up under the
    parent's `legs` field rather than listing children at top level. Any scan
    that iterates only the returned list therefore never sees a bracket/OTO
    protective stop — the parent is the entry (stop_price=None) and the stop
    is a child. Cost us every "no discoverable stop" warning of 2026-07-16.
    """
    yield order
    for leg in (getattr(order, "legs", None) or []):
        yield from _walk_order(leg)


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


# Systems whose bracket carries a take-profit leg by DEFAULT. xsectmom is
# excluded: a relative-strength book should ride winners until they rotate
# out of rank, not cap them at a fixed R. Any system can be flipped with the
# env var <SYSTEM>_USE_TAKE_PROFIT = true/false (e.g. XSECT_USE_TAKE_PROFIT=true
# to restore the old behavior, or SWING_USE_TAKE_PROFIT=false to try it off).
_TAKE_PROFIT_DEFAULT = {
    "swing": True,
    "meanrev": True,      # mean-reversion explicitly wants a target
    "xsectmom": False,    # ride the trend; exit on rotation or stop
    "intraday": True,
}


def _use_take_profit(system) -> bool:
    """Whether this system's entries attach a take-profit leg. Env override
    key is per-system, tolerant of the XSECT/xsectmom naming: e.g.
    XSECT_USE_TAKE_PROFIT or XSECTMOM_USE_TAKE_PROFIT."""
    name = system.value
    aliases = {name.upper()}
    if name == "xsectmom":
        aliases.add("XSECT")
    for a in aliases:
        raw = os.getenv(f"{a}_USE_TAKE_PROFIT")
        if raw is not None:
            return raw.strip().lower() in ("1", "true", "yes", "on")
    return _TAKE_PROFIT_DEFAULT.get(name, True)


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
        # Entry orders whose actual fill hasn't been confirmed yet:
        # ticker -> entry order id. Drained by refresh_entry_fills().
        self._pending_entry_fills: dict[str, str] = {}
        # ticker -> epoch when first seen missing from the broker (see the
        # reconciliation guards; reset the moment it reappears)
        self._absent_since: dict[str, float] = {}
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

    # ---------------- persistent position registry (2026-07-10) -----------
    # Reconciliation attributed positions to systems by scanning RECENT
    # orders for the bot's coid prefix. Swing positions are held for days
    # or weeks; once the entry order ages out of the scan window, reconcile
    # can't attribute the holding and halts as an orphan (or misattributes
    # it). Provenance must not depend on order recency: every open/close
    # now writes a small registry to the /data volume, and reconcile reads
    # it FIRST, with the coid scan as fallback. Registry corruption or a
    # missing volume degrades gracefully to the old behavior.

    def _position_state_path(self) -> str:
        import os as _os
        return _os.getenv("POSITION_STATE_PATH", "/data/position_state.json")

    def _save_position_state(self) -> None:
        import json as _json
        import os as _os
        state = {}
        for t, p in self.positions.items():
            state[t] = {
                "system": p.system.value,
                "shares": p.shares,
                "entry_price": p.entry_price,
                "entry_stop": p.entry_stop,
                "stop_price": p.stop_price,
                "entry_time": p.entry_time,
                "source": p.source.value if getattr(p, "source", None) else None,
            }
        path = self._position_state_path()
        try:
            d = _os.path.dirname(path) or "."
            _os.makedirs(d, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                _json.dump(state, fh)
            _os.replace(tmp, path)                 # atomic on POSIX
        except OSError as e:
            log.warning("[ALPACA] position registry write failed (%s) — "
                        "reconcile after a long hold may fall back to the "
                        "order scan. Mount a volume at /data.", e)

    def _load_position_state(self) -> dict:
        import json as _json
        try:
            with open(self._position_state_path(), encoding="utf-8") as fh:
                state = _json.load(fh)
            return state if isinstance(state, dict) else {}
        except (OSError, ValueError):
            return {}

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

        # --- Stop persistence: DAY legs die at the close. --------------------
        # Discovered 2026-07-11: Friday's bracket stop legs expired at the
        # bell, leaving the weekend-held swing/xsect positions with NO live
        # broker-side stop. Multi-day systems need GTC brackets; only the
        # intraday book (flat by the close anyway) should use DAY.
        tif = TimeInForce.DAY if system is System.INTRADAY else TimeInForce.GTC

        # --- Duplicate guard #1b: is a BUY already WORKING at the broker? ---
        # A position check is NOT enough. An entry order that has not filled
        # leaves no position, so a restart re-signals the same name and fires
        # a second order. That is precisely how PLD stacked 2x and UNP 3x on
        # 2026-07-17 across five redeploys. If we cannot verify, we do NOT
        # trade: a missed entry is cheap, a duplicate is not.
        try:
            from alpaca.trading.enums import QueryOrderStatus
            from alpaca.trading.requests import GetOrdersRequest
            working = self._client.get_orders(GetOrdersRequest(
                status=QueryOrderStatus.OPEN, symbols=[ticker])) or []
        except Exception as e:  # noqa: BLE001
            log.error("[ALPACA] refusing %s: cannot verify working orders "
                      "(%s). Not trading on an unverified book.", ticker, e)
            return None
        for o in working:
            side = str(getattr(o, "side", "")).lower()
            if "buy" in side:
                log.warning("[ALPACA] refusing %s: a BUY is already working "
                            "at the broker (id=%s status=%s). An unfilled "
                            "entry leaves no position — this is the guard "
                            "that PLD/UNP needed on 2026-07-17.",
                            ticker, getattr(o, "id", "?"),
                            getattr(o, "status", "?"))
                return None

        # --- Duplicate guard #2: deterministic client_order_id. -------------
        # Idempotency WINDOW matters more than the hash. This used to key on
        # the MINUTE, so it only ever blocked a re-fire inside the same 60s —
        # useless against restarts minutes or hours apart. On 2026-07-16/17
        # five redeploys each re-signalled PLD/UNP (whose entries were still
        # working, so no position existed to trip guard #1) and every order
        # got a fresh coid: PLD filled 2x (130 sh vs 65) and UNP 3x (96 sh vs
        # 32) — ~50% of equity in two names against a 10% cap.
        #
        # The multi-day systems trade a name at most once per day by design
        # (xsect rotates once at 10:00 ET; swing holds for days), so key on
        # the DATE: Alpaca then rejects the second entry for that ticker/
        # system/day server-side, no matter how many times we restart.
        # Intraday legitimately re-enters the same name within a session, so
        # it keeps the minute window.
        if system is System.INTRADAY:
            window = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
        else:
            window = datetime.now(timezone.utc).strftime("%Y%m%d")
        coid = (f"bot-{system.value}-"
                + hashlib.sha256(f"{system.value}:{ticker}:{window}".encode())
                .hexdigest()[:16])

        if USE_BRACKET_ORDERS:
            # Marketable LIMIT entry: cap the worst fill we'll accept (slippage guard).
            limit = round(price * (1 + MAX_SLIPPAGE_BPS / 10_000.0), 2)
            risk_per_share = max(price - stop_price, 0.01)

            # --- Take-profit leg: optional, per-system (2026-07-14) ----------
            # xsectmom is a relative-strength strategy: its edge is HOLDING the
            # strongest names until they fall out of the top-3 rank, capturing
            # the long right tail of a trend. A 2R take-profit leg amputates
            # exactly that tail — three rotations running (Jul 10/13/14) it
            # sold the winners by midday and left the momentum book in cash on
            # rising tape. Momentum books should exit on rank rotation or the
            # protective stop, NOT a fixed profit target. Swing/meanrev keep
            # their targets (mean-reversion in particular WANTS a target).
            # Override per system via XSECT_USE_TAKE_PROFIT / <SYS>_USE_TAKE_PROFIT.
            use_tp = _use_take_profit(system)
            if use_tp:
                target = round(price + TAKE_PROFIT_R * risk_per_share, 2)
                order = LimitOrderRequest(
                    symbol=ticker, qty=qty, side=OrderSide.BUY,
                    time_in_force=tif, limit_price=limit,
                    order_class=OrderClass.BRACKET,
                    stop_loss=StopLossRequest(stop_price=round(stop_price, 2)),
                    take_profit=TakeProfitRequest(limit_price=target),
                    client_order_id=coid,
                )
                log.warning("[ALPACA] BRACKET BUY %s x%d limit<=%.2f stop=%.2f "
                            "target=%.2f [%s] coid=%s", ticker, qty, limit,
                            stop_price, target, system.value, coid)
            else:
                # Stop-only: OTO (one-triggers-one) — entry triggers a single
                # protective stop leg, no profit target. The position is
                # exited by the strategy engine (rank rotation) or the stop.
                order = LimitOrderRequest(
                    symbol=ticker, qty=qty, side=OrderSide.BUY,
                    time_in_force=tif, limit_price=limit,
                    order_class=OrderClass.OTO,
                    stop_loss=StopLossRequest(stop_price=round(stop_price, 2)),
                    client_order_id=coid,
                )
                log.warning("[ALPACA] OTO BUY (stop-only, no target) %s x%d "
                            "limit<=%.2f stop=%.2f [%s] coid=%s", ticker, qty,
                            limit, stop_price, system.value, coid)
        else:
            order = MarketOrderRequest(symbol=ticker, qty=qty, side=OrderSide.BUY,
                                       time_in_force=tif,
                                       client_order_id=coid)
            log.warning("[ALPACA] MARKET BUY %s x%d [%s] coid=%s",
                        ticker, qty, system.value, coid)

        try:
            submitted = self._client.submit_order(order)
        except APIError as e:
            if "client_order_id" in str(e).lower() or "duplicate" in str(e).lower():
                log.warning("[ALPACA] duplicate order suppressed for %s "
                            "(coid=%s) — original from before restart still "
                            "stands", ticker, coid)
                return None
            raise

        # -- Entry-fill accuracy (2026-07-10 patch) ---------------------------
        # sell() has recorded actual fills since Jul 8; entries still booked
        # the pre-trade quote, overstating profits by the entry slippage
        # (~$430 unattributed Jul 9, ~$755 Jul 10 — books said +$514 on a day
        # the broker settled at -$240). Poll the entry fill briefly; if it
        # hasn't filled yet, book the quote provisionally and let
        # refresh_entry_fills() correct it within a cycle or two.
        entry_price, price_src = price, "quote-est"
        order_id = getattr(submitted, "id", None)
        if order_id is not None:
            fill = self._await_fill_price(order_id, timeout=3.0)
            if fill is not None:
                entry_price, price_src = fill, "fill"
            else:
                self._pending_entry_fills[ticker] = str(order_id)
        if price_src == "fill" and abs(entry_price - price) > 0.005:
            log.warning("[ALPACA] entry slippage %s: quote %.4f -> fill %.4f "
                        "(%+.4f/share)", ticker, price, entry_price,
                        entry_price - price)

        pos = Position(ticker, system, qty, entry_price, time.time(),
                       stop_price, source, entry_stop=stop_price,
                       high_water=entry_price, last_price=entry_price)
        self.positions[ticker] = pos
        self._save_position_state()
        audit.fill(ticker=ticker, qty=qty, price=round(entry_price, 2),
                   stop=round(stop_price, 2), system=system.value, coid=coid,
                   via=price_src)
        return pos

    def refresh_entry_fills(self) -> None:
        """Correct provisionally-booked entry prices once their orders fill.
        Called from reconcile_filled_legs(), i.e. every manage cycle, so a
        quote-booked entry is corrected within seconds of its fill — every
        downstream realized/R-multiple then uses the broker's actual fill.
        Never raises."""
        from alpaca.common.exceptions import APIError
        for ticker in list(self._pending_entry_fills):
            pos = self.positions.get(ticker)
            if pos is None:                       # closed before confirmation
                self._pending_entry_fills.pop(ticker, None)
                continue
            order_id = self._pending_entry_fills[ticker]
            try:
                o = self._client.get_order_by_id(order_id)
            except APIError as e:
                log.warning("[ALPACA] entry-fill refresh %s: %s", ticker, e)
                continue
            status = str(getattr(o, "status", "")).lower()
            if any(t in status for t in ("canceled", "expired", "rejected")):
                # Entry never filled — the position we booked doesn't exist.
                log.error("[ALPACA] entry order for %s ended %s — removing "
                          "phantom position from the tracker", ticker, status)
                self.positions.pop(ticker, None)
                self._pending_entry_fills.pop(ticker, None)
                self._save_position_state()
                continue

            # ---- PARTIAL FILL: track what ACTUALLY filled (2026-07-17) -----
            # A working limit can sit partially filled indefinitely (entries
            # are GTC for multi-day systems). Previously this path just kept
            # polling and left the position booked at the FULL requested size:
            # AMD was logged as x9 while only 4 shares filled, so every P&L
            # figure on it was inflated 2.25x and its stop was sized for 9
            # against 4 held. Correct the tracked quantity as it fills.
            filled_qty = float(getattr(o, "filled_qty", 0) or 0)
            if not self._is_fully_filled(o):
                if filled_qty > 0 and abs(filled_qty - pos.shares) > 1e-9:
                    log.warning("[ALPACA] %s PARTIAL fill: tracking %.0f sh, "
                                "broker filled %.0f — correcting the tracker "
                                "to %.0f (order still working)",
                                ticker, pos.shares, filled_qty, filled_qty)
                    pos.shares = filled_qty
                    fap = getattr(o, "filled_avg_price", None)
                    if fap:
                        pos.entry_price = float(fap)
                    self._save_position_state()
                continue                          # still working; retry next cycle

            if not getattr(o, "filled_avg_price", None):
                continue
            if filled_qty > 0 and abs(filled_qty - pos.shares) > 1e-9:
                log.warning("[ALPACA] %s final fill qty %.0f differs from "
                            "tracked %.0f — correcting", ticker, filled_qty,
                            pos.shares)
                pos.shares = filled_qty
            fill = float(o.filled_avg_price)
            if abs(fill - pos.entry_price) > 0.005:
                log.warning("[ALPACA] entry CORRECTED %s: quote-est %.4f -> "
                            "actual fill %.4f (%+.4f/share x %.0f)",
                            ticker, pos.entry_price, fill,
                            fill - pos.entry_price, pos.shares)
            pos.entry_price = fill
            pos.high_water = max(pos.high_water, fill)
            self._pending_entry_fills.pop(ticker, None)
            self._save_position_state()

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

    @staticmethod
    def _is_fully_filled(order) -> bool:
        """'partially_filled'.endswith('filled') is True — the naive check
        would book a partial fill's avg price against the full position qty.
        Require terminal FILLED status exactly."""
        status = str(getattr(order, "status", "")).lower()
        return status.endswith("filled") and "partial" not in status

    def _await_fill_price(self, order_id, timeout: float = 3.0):
        """Poll an order briefly for its actual filled_avg_price. Market
        closes usually fill sub-second; if not filled in time, return None
        and the caller falls back to the quote estimate."""
        from alpaca.common.exceptions import APIError
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                o = self._client.get_order_by_id(order_id)
                if self._is_fully_filled(o) \
                        and getattr(o, "filled_avg_price", None):
                    return float(o.filled_avg_price)
            except APIError as e:
                log.warning("[ALPACA] poll order %s: %s", order_id, e)
                return None
            time.sleep(0.3)
        return None

    def _find_closing_fill_price(self, ticker, after_ts: float | None = None):
        """Find the actual fill price of the most recent filled SELL order
        for a ticker — i.e. the bracket leg that closed the position
        broker-side. Returns None if not found.

        `after_ts` (epoch seconds) is REQUIRED in practice: without it this
        scan returns the newest closed sell order regardless of age. On
        2026-07-16 that booked THREE phantom closes against the previous
        DAY's exits (MU at 943.24 — yesterday's fill price, while MU was
        actually trading at 847) and invented +$742.62 of profit that never
        existed. A closing fill that predates the entry cannot possibly have
        closed it.
        """
        from alpaca.trading.enums import OrderSide, QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        try:
            orders = self._client.get_orders(GetOrdersRequest(
                status=QueryOrderStatus.CLOSED, symbols=[ticker],
                side=OrderSide.SELL, limit=20))
        except Exception as e:  # noqa: BLE001
            log.warning("[ALPACA] closed-order lookup %s: %s", ticker, e)
            return None
        cutoff = None
        if after_ts is not None:
            cutoff = datetime.fromtimestamp(after_ts, tz=timezone.utc)
        best = None
        for o in orders:
            if not self._is_fully_filled(o):
                continue
            fap = getattr(o, "filled_avg_price", None)
            fat = getattr(o, "filled_at", None)
            if not fap:
                continue
            if cutoff is not None:
                if fat is None:
                    continue            # undatable: cannot prove it's ours
                try:
                    if fat <= cutoff:
                        continue        # predates the entry — not our exit
                except TypeError:
                    continue            # unparseable timestamp: skip, be safe
            if best is None or (fat and best[0] and fat > best[0]):
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
        # First, correct any entries booked at quote while their fill was
        # pending — zero new wiring: engines already call this every cycle.
        self.refresh_entry_fills()
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
                self._absent_since.pop(ticker, None)   # present: reset
                continue
            pos = self.positions.get(ticker)
            if pos is None:
                continue

            # ---- GUARD 1: entry not confirmed filled -------------------
            # A position booked at quote-est whose entry order is still
            # working is not AT the broker yet. Absence proves nothing.
            if ticker in self._pending_entry_fills:
                continue

            # ---- GUARD 2: settlement grace ------------------------------
            # Even after an entry fills, the position can lag the positions
            # API by seconds. On 2026-07-16 this window (ONE cycle, 0.74s
            # after entry for INTC) was read as "the leg closed it" and
            # three live positions were deleted from the tracker while the
            # account actually held them — the bot went blind to ~$9.6k of
            # real risk and reported +$742.62 of profit that did not exist.
            absent_for = time.time() - self._absent_since.setdefault(
                ticker, time.time())
            age = time.time() - pos.entry_time
            if age < _ENTRY_SETTLE_GRACE_SECS or absent_for < _ABSENT_CONFIRM_SECS:
                continue

            # ---- GUARD 3: prove the close, never invent it --------------
            # The closing fill must POSTDATE the entry. If we cannot prove
            # a close, we do NOT book one: an unprovable close was exactly
            # the 2026-07-16 fabrication. Keep the position, shout, retry.
            fill = self._find_closing_fill_price(ticker, after_ts=pos.entry_time)
            if fill is None:
                log.critical("[ALPACA] %s: absent from broker for %.0fs but NO "
                             "post-entry closing fill found. NOT booking a "
                             "close (an unprovable close is how phantom P&L "
                             "gets invented). Position kept; verify manually "
                             "if this persists.", ticker, absent_for)
                continue

            self._absent_since.pop(ticker, None)
            self.positions.pop(ticker, None)
            self._save_position_state()
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
        self._save_position_state()
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
        can't be matched to the persisted registry OR a bot order in the
        recent order scan. If non-empty, the caller must HALT — unknown
        holdings are unknown risk; do not liquidate silently, a human
        should look. (2026-07-10: registry is now the PRIMARY source, so
        multi-day swing holds reconcile correctly long after their entry
        orders age out of the scan window.)
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
        # Order listing is the PRIMARY attribution source. A transient
        # failure here used to be indistinguishable from "this account has
        # no bot orders" — on 2026-07-23 23:47 Alpaca returned a 500
        # (code 50010000) and reconcile declared ALL SIX healthy positions
        # orphans, halting with "resolve in the Alpaca dashboard" for a
        # problem that did not exist there. Retry first; remember whether
        # the listing actually succeeded.
        todays = []
        listing_ok = False
        for attempt in range(3):
            try:
                todays = self._client.get_orders(GetOrdersRequest(
                    status=QueryOrderStatus.ALL, limit=500, nested=True))
                listing_ok = True
                break
            except Exception as e:  # noqa: BLE001
                log.error("[ALPACA] reconcile: cannot list orders "
                          "(attempt %d/3): %s", attempt + 1, e)
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
        for o in todays:
            coid = getattr(o, "client_order_id", "") or ""
            if coid.startswith("bot-"):
                parts = coid.split("-")
                if len(parts) >= 3:
                    system = next((s for s in System if s.value == parts[1]), None)
                    if system:
                        # Orders arrive NEWEST FIRST. setdefault = the newest
                        # bot order for a ticker wins. The old unconditional
                        # assignment let the OLDEST win — on 2026-07-11 it
                        # attributed META to a Jul-8 intraday order instead
                        # of the Jul-10 swing order, poisoned the registry,
                        # and boot-looped the service all weekend.
                        sys_by_ticker.setdefault(o.symbol, system)
            # ---- walk the parent AND its nested legs (2026-07-17 fix) -------
            # nested=True rolls bracket/OTO child legs up UNDER the parent's
            # `legs` field instead of returning them as top-level orders. The
            # loop therefore only ever saw PARENTS — and a parent is the limit
            # BUY, whose stop_price is None. The protective stop lives in the
            # leg. Result: stop_by_ticker was EMPTY on every boot since the
            # registry shipped, so every position logged "no discoverable
            # stop" even when its GTC leg was alive and well at Alpaca (ARM,
            # 2026-07-16). Walk the legs.
            for node in _walk_order(o):
                sp = getattr(node, "stop_price", None)
                status = str(getattr(node, "status", "")).lower()
                if sp and ("new" in status or "held" in status
                           or "accepted" in status):
                    stop_by_ticker[node.symbol] = float(sp)

        registry = self._load_position_state()

        for p in broker_positions:
            ticker = p.symbol
            reg = registry.get(ticker)
            reg_system = None
            if reg:
                reg_system = next((s for s in System
                                   if s.value == reg.get("system")), None)
            order_system = sys_by_ticker.get(ticker)

            if reg_system and order_system and reg_system is not order_system:
                # Disagreement: the newest bot ORDER is direct evidence of
                # who traded this ticker last; a stale/poisoned registry
                # entry must not outvote it. Heal the registry (saved at
                # the end of reconcile) and say so loudly.
                log.warning("[ALPACA] reconcile: %s registry says %s but the "
                            "newest bot order says %s — trusting the order "
                            "and correcting the registry",
                            ticker, reg_system.value, order_system.value)
                system = order_system
            else:
                system = reg_system or order_system
                if system and reg_system:
                    log.info("[ALPACA] reconcile: %s attributed to %s via "
                             "position registry", ticker, system.value)
            if system is None:
                orphans.append(ticker)
                if listing_ok:
                    log.critical("[ALPACA] reconcile: ORPHAN %s x%s — no bot "
                                 "order found; resolve manually before "
                                 "trading", ticker, p.qty)
                else:
                    # The order API failed, so "no bot order found" is NOT
                    # evidence of an orphan — it is evidence of nothing.
                    # Halt anyway (never trade without knowing broker
                    # state), but say the true reason: this clears itself
                    # when the broker API recovers, and the dashboard is
                    # not where it gets fixed.
                    log.critical("[ALPACA] reconcile: UNVERIFIED %s x%s — "
                                 "broker order API is failing, so system "
                                 "attribution could not be checked. This is "
                                 "NOT necessarily an orphan. Halting until "
                                 "the API recovers; no dashboard action "
                                 "needed. (Registry had no entry for this "
                                 "ticker either.)", ticker, p.qty)
                continue
            entry = float(p.avg_entry_price)        # broker fill = truth
            qty = float(p.qty)
            last = float(getattr(p, "current_price", None) or entry)
            # Restore the TRUE entry time from the registry (2026-07-22).
            # This was always saved (_save_position_state) but never read
            # back: every redeploy stamped adopted positions with BOOT time,
            # silently resetting any holding-period logic — the meanrev
            # time stop would count from the last deploy, not the entry,
            # and with several deploys a week it would never fire. Boot
            # time remains the fallback for a cold registry.
            entry_time = time.time()
            if reg and reg.get("entry_time"):
                try:
                    entry_time = float(reg["entry_time"])
                except (TypeError, ValueError):
                    pass
            # Stop preference: live leg order > registry > 1% fallback
            stop = stop_by_ticker.get(ticker)
            if stop is None and reg and reg.get("stop_price"):
                stop = float(reg["stop_price"])
            if stop is None:
                # NEVER invent a triggerable stop (2026-07-16 incident).
                # This used to default to entry * 0.99. On the first boot with
                # a cold registry and an order scan that found no stop legs,
                # that put an invented stop ABOVE the live price for every
                # position already >1% underwater — so the first manage cycle
                # read price <= stop and liquidated them. UNH (real stop
                # 405.14, price 423.38) was sold at -279.62; INTC at -99.24;
                # MU at -41.76. Three positions killed at losses their
                # strategies never sanctioned, after hours, on stale quotes.
                #
                # 0.0 is unreachable: the position can never be stopped out by
                # a number this code made up. Swing/meanrev re-derive a real
                # trailing stop from daily ATR on the first manage cycle
                # (max(stop, high_water - MULT*atr)); xsectmom relies on its
                # broker-side GTC leg. If neither exists the position is
                # genuinely unprotected — hence CRITICAL, not a warning.
                stop = 0.0
                log.critical("[ALPACA] reconcile: %s adopted with NO known "
                             "stop (registry cold AND no stop leg found). "
                             "Stop set to 0.0 — deliberately unreachable so "
                             "nothing invented can force an exit. The engine "
                             "will re-derive from ATR next cycle; VERIFY a "
                             "protective stop exists at the broker.", ticker)
            self.positions[ticker] = Position(
                ticker, system, qty, entry, entry_time, stop, None,
                entry_stop=stop, high_water=max(entry, last), last_price=last)
            log.warning("[ALPACA] reconcile: re-adopted %s x%s [%s] "
                        "entry=%.2f stop=%.2f", ticker, qty, system.value,
                        entry, stop)
        self._save_position_state()   # prune closed/stale entries
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
