"""
swing_v2.py -- pullback-continuation swing strategy, shadow-first.

Implements the agreed spec exactly. Ships in SHADOW mode: it computes real
signals against live prices and logs every entry/exit it WOULD make, with
structured lines you can grep and analyze, but places no orders until
SWING_V2_MODE=live is set deliberately. Both entry variants (A and B) run
side by side in shadow so live data accumulates on each while the backtest
argues in parallel.

SPEC (defaults are textbook starting values, NOT calibrated advice):
  Trend filter   close > SMA50  AND  EMA20 > SMA50
  Pullback       within last 3 bars, day low came within 0.5*ATR14 of EMA20
  Setup candle   bullish engulfing | hammer | strong close (definitions below)
  Entry A        intraday stop-buy at setup_high + 0.01; volume test applied
                 to the SETUP candle (>= 1.2x 20d avg) since breakout-day
                 volume is unknowable intraday
  Entry B        next open after a full CLOSE above setup_high on >= 1.5x
                 20d avg volume (original rule, one day later)
  Setup expiry   3 trading days
  Stop           farther of: entry_candle_low - 1*ATR14, or recent swing low
                 (lowest low of prior 10 bars). Never widened.
  Winner exit    half off at +2R, stop to breakeven; rest out on close below
                 EMA20. Time stop: 15 trading days without +1R -> close.
  Sizing         risk 0.75% of equity per trade; shares = risk$/stop_dist;
                 <= 10% equity notional; <= 5 concurrent; <= 2 new/day.

ENV
  SWING_V2_MODE        shadow (default) | live | off
  SWING_V2_ENTRY       A | B   (live mode only; shadow runs both)
  SWING_V2_RISK_PCT    default 0.0075
  APCA_* keys          same as the rest of the bot

INTEGRATION
  from swing_v2 import scan_swing_v2
  # every main-loop cycle (it self-throttles internally):
  scan_swing_v2(UNIVERSE_SYMBOLS, equity=current_equity,
                health_record=health.record)

Structured log lines to analyze later (grep-able):
  SWING2 FUNNEL ...            per-refresh funnel WITH kill reasons
  SWING2 SHADOW_ENTRY ...      variant, sym, px, stop, shares, risk$
  SWING2 SHADOW_EXIT ...       reason in {stop, half_2R, ema20, time}
  SWING2 LIVE_* ...            same, live mode only
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from typing import Callable, Optional
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger("agentic_trader.swing_v2")
ET = ZoneInfo("America/New_York")

MODE = os.environ.get("SWING_V2_MODE", "shadow").lower()
LIVE_VARIANT = os.environ.get("SWING_V2_ENTRY", "A").upper()
RISK_PCT = float(os.environ.get("SWING_V2_RISK_PCT", "0.0075"))
MAX_NOTIONAL_PCT = 0.10
MAX_CONCURRENT = 5
MAX_NEW_PER_DAY = 2
SETUP_EXPIRY_DAYS = 3
TIME_STOP_DAYS = 15
PULLBACK_ATR_MULT = 0.5
VOL_MULT_A = 1.2       # setup-candle volume vs 20d avg (variant A)
VOL_MULT_B = 1.5       # breakout-day volume vs 20d avg (variant B)
REFRESH_SECONDS = 900  # recompute daily indicators/setups every 15 min

# State lives on the /data volume (same reason as audit.py: Railway's
# filesystem is ephemeral and logs are purged on redeploy). Falls back
# loudly to ./ if the volume isn't mounted — degraded, never silent.
_STATE_PREF = os.environ.get("SWING_V2_STATE", "/data/swing_v2_state.json")
_state_resolved: Optional[str] = None


def _state_path() -> str:
    global _state_resolved
    if _state_resolved:
        return _state_resolved
    try:
        d = os.path.dirname(_STATE_PREF) or "."
        os.makedirs(d, exist_ok=True)
        with open(_STATE_PREF, "a", encoding="utf-8"):
            pass
        _state_resolved = _STATE_PREF
    except OSError as e:
        _state_resolved = "./swing_v2_state.json"
        log.error("swing_v2: %s not writable (%s) — falling back to %s; "
                  "shadow book will NOT survive redeploys. Mount the /data "
                  "volume or set SWING_V2_STATE.", _STATE_PREF, e,
                  _state_resolved)
    return _state_resolved

ALPACA_TRADE_BASE = os.environ.get(
    "APCA_API_BASE_URL", "https://paper-api.alpaca.markets").rstrip("/")
ALPACA_STOCK_DATA = "https://data.alpaca.markets/v2/stocks"


def _auth() -> dict:
    # Key naming: this codebase uses ALPACA_API_KEY / ALPACA_SECRET_KEY
    # (see config.py / brokers.py). Alpaca's own SDK convention is
    # APCA_API_KEY_ID / APCA_API_SECRET_KEY. Accept both, repo names first —
    # reading only the APCA_* names 403'd every data fetch on first deploy
    # (2026-07-20, caught by Railway's env suggestion).
    key = os.environ.get("ALPACA_API_KEY") or os.environ.get("APCA_API_KEY_ID", "")
    sec = (os.environ.get("ALPACA_SECRET_KEY")
           or os.environ.get("APCA_API_SECRET_KEY", ""))
    if not key or not sec:
        log.error("swing_v2: no Alpaca keys found under ALPACA_* or APCA_* "
                  "names — data fetches will fail (shadow-only; trading "
                  "unaffected)")
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec}


# ---------------------------------------------------------------------------
# DATA
# ---------------------------------------------------------------------------

def fetch_daily_bars(symbols: list[str], limit: int = 120) -> dict[str, list[dict]]:
    """{sym: [{'t','o','h','l','c','v'}, ...]} oldest->newest, IEX/SIP daily."""
    out: dict[str, list[dict]] = {}
    for i in range(0, len(symbols), 50):
        chunk = symbols[i:i + 50]
        page = None
        while True:
            params = {"symbols": ",".join(chunk), "timeframe": "1Day",
                      "limit": 10000, "adjustment": "split"}
            if page:
                params["page_token"] = page
            r = requests.get(f"{ALPACA_STOCK_DATA}/bars", params=params,
                             headers=_auth(), timeout=30)
            r.raise_for_status()
            j = r.json()
            for s, bars in j.get("bars", {}).items():
                out.setdefault(s, []).extend(bars)
            page = j.get("next_page_token")
            if not page:
                break
            time.sleep(0.25)
    return {s: b[-limit:] for s, b in out.items()}


def latest_prices(symbols: list[str]) -> dict[str, float]:
    out = {}
    for i in range(0, len(symbols), 100):
        chunk = symbols[i:i + 100]
        r = requests.get(f"{ALPACA_STOCK_DATA}/trades/latest",
                         params={"symbols": ",".join(chunk)},
                         headers=_auth(), timeout=15)
        if r.status_code == 200:
            for s, t in r.json().get("trades", {}).items():
                out[s] = float(t["p"])
    return out


# ---------------------------------------------------------------------------
# INDICATORS & CANDLES  (pure functions -> unit-testable, backtest-shared)
# ---------------------------------------------------------------------------

def sma(vals, n):   return sum(vals[-n:]) / n if len(vals) >= n else None

def ema(vals, n):
    if len(vals) < n:
        return None
    k = 2 / (n + 1)
    e = sum(vals[:n]) / n
    for v in vals[n:]:
        e = v * k + e * (1 - k)
    return e

def atr(bars, n=14):
    if len(bars) < n + 1:
        return None
    trs = []
    for i in range(len(bars) - n, len(bars)):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / n

def is_engulfing(prev, cur):
    return (prev["c"] < prev["o"] and cur["c"] > cur["o"]
            and cur["o"] <= prev["c"] and cur["c"] >= prev["o"])

def is_hammer(cur):
    body = abs(cur["c"] - cur["o"])
    rng = cur["h"] - cur["l"]
    if rng <= 0 or body <= 0:
        return False
    lower_wick = min(cur["c"], cur["o"]) - cur["l"]
    return lower_wick >= 2 * body and cur["c"] >= cur["l"] + rng * (2 / 3)

def is_strong_close(cur, atr_val):
    rng = cur["h"] - cur["l"]
    body = cur["c"] - cur["o"]
    return (rng > 0 and atr_val and body >= 0.5 * atr_val
            and cur["c"] >= cur["h"] - 0.25 * rng)

def swing_low(bars, lookback=10):
    return min(b["l"] for b in bars[-lookback:])


# ---------------------------------------------------------------------------
# SETUP DETECTION (daily) -- with funnel kill-reason logging
# ---------------------------------------------------------------------------

@dataclass
class Setup:
    symbol: str
    setup_high: float
    setup_low: float
    atr14: float
    swing_low_lvl: float
    vol_ratio_setup: float     # setup-candle volume / 20d avg
    avg_vol20: float
    created: str               # ISO date of setup candle
    age_days: int = 0


def detect_setup(sym: str, bars: list[dict]) -> tuple[Optional[Setup], str]:
    """Returns (Setup|None, kill_reason). Uses completed daily bars only."""
    if len(bars) < 60:
        return None, "insufficient_history"
    closes = [b["c"] for b in bars]
    s50 = sma(closes, 50)
    e20 = ema(closes, 20)
    a14 = atr(bars, 14)
    if not (s50 and e20 and a14):
        return None, "indicator_nan"
    cur, prev = bars[-1], bars[-2]
    if not (cur["c"] > s50 and e20 > s50):
        return None, "trend_filter"
    pulled = any(abs(b["l"] - e20) <= PULLBACK_ATR_MULT * a14 or b["l"] < e20
                 for b in bars[-3:])
    if not pulled:
        return None, "no_pullback"
    if not (is_engulfing(prev, cur) or is_hammer(cur)
            or is_strong_close(cur, a14)):
        return None, "no_bullish_candle"
    av20 = sum(b["v"] for b in bars[-21:-1]) / 20
    vr = cur["v"] / av20 if av20 else 0
    if vr < VOL_MULT_A:
        return None, f"setup_volume({vr:.2f}x<{VOL_MULT_A}x)"
    return Setup(sym, cur["h"], cur["l"], a14, swing_low(bars[:-1], 10),
                 vr, av20, cur["t"][:10]), "ok"


# ---------------------------------------------------------------------------
# POSITION BOOK (shadow or live share the same bookkeeping)
# ---------------------------------------------------------------------------

@dataclass
class Position:
    symbol: str
    variant: str               # "A" | "B"
    entry_px: float
    stop: float
    r: float                   # per-share risk at entry
    shares: int
    entry_date: str
    half_taken: bool = False
    bars_held: int = 0


class Book:
    def __init__(self):
        self.setups: dict[str, Setup] = {}
        self.pos: dict[str, Position] = {}     # key: f"{variant}:{sym}"
        self.entries_today: dict[str, int] = {}  # variant -> count
        self.day: str = ""

    def save(self):
        try:
            with open(_state_path(), "w") as f:
                json.dump({"setups": {k: asdict(v) for k, v in self.setups.items()},
                           "pos": {k: asdict(v) for k, v in self.pos.items()},
                           "day": self.day,
                           "entries_today": self.entries_today}, f)
        except Exception as e:
            log.warning("swing_v2 state save failed: %s", e)

    def load(self):
        try:
            with open(_state_path()) as f:
                raw = f.read().strip()
            if not raw:
                return   # fresh boot: writability probe leaves an empty file
            j = json.loads(raw)
            self.setups = {k: Setup(**v) for k, v in j.get("setups", {}).items()}
            self.pos = {k: Position(**v) for k, v in j.get("pos", {}).items()}
            self.day = j.get("day", "")
            self.entries_today = j.get("entries_today", {})
            log.info("swing_v2 state restored: %d setups, %d positions",
                     len(self.setups), len(self.pos))
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning("swing_v2 state load failed (starting clean): %s", e)


BOOK = Book()
BOOK.load()
_last_refresh = 0.0


def _concurrent(variant: str) -> int:
    return sum(1 for k in BOOK.pos if k.startswith(variant + ":"))


def _size(equity: float, entry: float, stop: float) -> int:
    risk_dollars = equity * RISK_PCT
    dist = entry - stop
    if dist <= 0:
        return 0
    shares = int(risk_dollars / dist)
    max_shares = int(equity * MAX_NOTIONAL_PCT / entry)
    return max(0, min(shares, max_shares))


def _enter(variant: str, s: Setup, px: float, equity: float, live: bool):
    stop = min(s.setup_low - 1.0 * s.atr14, s.swing_low_lvl)
    # spec: "whichever is FARTHER" (lower) from entry -> min of the two
    shares = _size(equity, px, stop)
    if shares <= 0:
        log.info("SWING2 FUNNEL %s %s -> killed: size_zero", variant, s.symbol)
        return
    pos = Position(s.symbol, variant, px, stop, px - stop, shares,
                   datetime.now(ET).strftime("%Y-%m-%d"))
    BOOK.pos[f"{variant}:{s.symbol}"] = pos
    BOOK.entries_today[variant] = BOOK.entries_today.get(variant, 0) + 1
    log.info("SWING2 SHADOW_ENTRY var=%s %s px=%.2f stop=%.2f shares=%d "
             "risk$=%.0f", variant, s.symbol, px, stop, shares,
             shares * (px - stop))
    _audit_mirror("swing2_shadow_entry", variant=variant, ticker=s.symbol,
                  px=round(px, 2), stop=round(stop, 2), shares=shares)


def _exit(key: str, px: float, reason: str, live: bool, fraction: float = 1.0):
    p = BOOK.pos[key]
    n = int(p.shares * fraction)
    pnl = (px - p.entry_px) * n
    log.info("SWING2 SHADOW_EXIT var=%s %s px=%.2f shares=%d pnl=%.2f "
             "reason=%s held=%dd", p.variant, p.symbol, px, n, pnl, reason,
             p.bars_held)
    _audit_mirror("swing2_shadow_exit", variant=p.variant, ticker=p.symbol,
                  px=round(px, 2), shares=n, pnl=round(pnl, 2), reason=reason,
                  held_days=p.bars_held)
    if fraction >= 1.0:
        del BOOK.pos[key]
    else:
        p.shares -= n
        p.half_taken = True
        p.stop = p.entry_px  # breakeven


def _audit_mirror(event: str, **fields) -> None:
    """Mirror shadow trades into the persistent audit trail (audit.jsonl on
    /data) so they survive Railway's log purge on redeploy. Follows audit.py's
    design rule: never raises, never notifies (shadow trades are not phone-
    worthy), and absence of audit.py (local dev) degrades to logs only."""
    try:
        import audit
        audit.record(event, notify=False, **fields)
    except Exception:  # noqa: BLE001 — mirror is best-effort by design
        pass


_live_refused_logged = False


def _refuse_live_mode() -> bool:
    """swing_v2 does NOT support live trading in this codebase, deliberately.
    The broker layer stamps client_order_id=bot-{system}-{hash} and
    reconcile_at_startup HALTS on unattributable positions — raw orders from
    this module would orphan its own fills and halt the bot at next boot.
    The path to live for v2 is porting its signal into the engine framework
    (SwingRiskEngine-shaped), not bypassing it. Returns True if a refusal
    was made."""
    global _live_refused_logged
    if MODE == "live" and not _live_refused_logged:
        _live_refused_logged = True
        log.critical("SWING_V2_MODE=live REFUSED: v2 orders would bypass the "
                     "bot-{system} client_order_id convention and be halted "
                     "as ORPHANS at next reconcile. Running SHADOW. To go "
                     "live, port the v2 signal into the engine framework.")
        _audit_mirror("swing2_live_refused")
    return MODE == "live"


# ---------------------------------------------------------------------------
# MAIN SCAN -- call every cycle; self-throttles
# ---------------------------------------------------------------------------

def scan_swing_v2(symbols: list[str], equity: float,
                  health_record: Optional[Callable] = None):
    global _last_refresh
    if MODE == "off":
        return
    _refuse_live_mode()
    live = False   # structurally shadow-only in this codebase; see above
    today = datetime.now(ET).strftime("%Y-%m-%d")
    if BOOK.day != today:
        BOOK.day, BOOK.entries_today = today, {}

    # ---- slow path: refresh daily indicators & setups every 15 min --------
    if time.time() - _last_refresh > REFRESH_SECONDS:
        _last_refresh = time.time()
        try:
            bars = fetch_daily_bars(symbols)
        except Exception as e:
            log.error("SWING2 data fetch failed: %s", e)
            if health_record:
                health_record("swing_v2_data", False, str(e)[:80])
            return
        if health_record:
            health_record("swing_v2_data", True, f"{len(bars)}/{len(symbols)} syms")
        kills: dict[str, int] = {}
        new = 0
        for sym in symbols:
            if sym not in bars:
                kills["no_bars"] = kills.get("no_bars", 0) + 1
                continue
            s, why = detect_setup(sym, bars[sym])
            if s:
                if sym not in BOOK.setups:
                    new += 1
                BOOK.setups[sym] = s
            else:
                kills[why.split("(")[0]] = kills.get(why.split("(")[0], 0) + 1
        # age & expire setups; refresh variant-B confirmation from bars
        for sym in list(BOOK.setups):
            st = BOOK.setups[sym]
            st.age_days = _trading_days_between(st.created, today)
            if st.age_days > SETUP_EXPIRY_DAYS:
                del BOOK.setups[sym]
                kills["expired"] = kills.get("expired", 0) + 1
            elif sym in bars:
                _maybe_variant_b_entry(st, bars[sym], equity, live)
        # bars_held & EMA20/time exits use completed daily bars
        _daily_exits(bars, live)
        kill_str = " ".join(f"{k}={v}" for k, v in sorted(kills.items()))
        log.info("SWING2 FUNNEL universe=%d setups_active=%d new=%d | kills: %s",
                 len(symbols), len(BOOK.setups), new, kill_str or "none")
        BOOK.save()

    # ---- fast path: intraday triggers (variant A entries, stop exits) -----
    watch = list({s for s in BOOK.setups} |
                 {k.split(":")[1] for k in BOOK.pos})
    if not watch:
        return
    try:
        px = latest_prices(watch)
    except Exception as e:
        log.warning("SWING2 latest prices failed: %s", e)
        return

    for sym, st in list(BOOK.setups.items()):
        p = px.get(sym)
        if not p:
            continue
        key = f"A:{sym}"
        if (p > st.setup_high + 0.01 and key not in BOOK.pos
                and _concurrent("A") < MAX_CONCURRENT
                and BOOK.entries_today.get("A", 0) < MAX_NEW_PER_DAY):
            _enter("A", st, st.setup_high + 0.01, equity, live and
                   LIVE_VARIANT == "A")

    for key in list(BOOK.pos):
        p = BOOK.pos[key]
        cur = px.get(p.symbol)
        if not cur:
            continue
        this_live = live and p.variant == LIVE_VARIANT
        if cur <= p.stop:
            _exit(key, cur, "stop", this_live)
        elif not p.half_taken and cur >= p.entry_px + 2 * p.r:
            _exit(key, cur, "half_2R", this_live, fraction=0.5)
    BOOK.save()


def _maybe_variant_b_entry(st: Setup, bars: list[dict], equity: float,
                           live: bool):
    """B: yesterday CLOSED above setup high on >=1.5x vol -> enter today."""
    key = f"B:{st.symbol}"
    if key in BOOK.pos or _concurrent("B") >= MAX_CONCURRENT \
            or BOOK.entries_today.get("B", 0) >= MAX_NEW_PER_DAY:
        return
    last = bars[-1]
    if last["t"][:10] <= st.created:
        return
    if last["c"] > st.setup_high and st.avg_vol20 \
            and last["v"] >= VOL_MULT_B * st.avg_vol20:
        _enter("B", st, last["c"], equity, live and LIVE_VARIANT == "B")


def _daily_exits(bars: dict[str, list[dict]], live: bool):
    for key in list(BOOK.pos):
        p = BOOK.pos[key]
        b = bars.get(p.symbol)
        if not b or len(b) < 21:
            continue
        p.bars_held = _trading_days_between(p.entry_date,
                                            datetime.now(ET).strftime("%Y-%m-%d"))
        e20 = ema([x["c"] for x in b], 20)
        last_close = b[-1]["c"]
        this_live = live and p.variant == LIVE_VARIANT
        if e20 and last_close < e20 and p.half_taken:
            _exit(key, last_close, "ema20", this_live)
        elif e20 and last_close < e20 and p.bars_held >= 2:
            _exit(key, last_close, "ema20", this_live)
        elif p.bars_held >= TIME_STOP_DAYS \
                and last_close < p.entry_px + p.r:
            _exit(key, last_close, "time", this_live)


def _trading_days_between(d1: str, d2: str) -> int:
    a = date.fromisoformat(d1)
    b = date.fromisoformat(d2)
    days, cur = 0, a
    while cur < b:
        cur = date.fromordinal(cur.toordinal() + 1)
        if cur.weekday() < 5:
            days += 1
    return days
