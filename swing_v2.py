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
# ---------------------------------------------------------------------------
# TWO GATES ADDED 2026-08-01, both single-parameter and both reusing code
# that already exists elsewhere in this system rather than inventing new
# machinery:
#
#   RELATIVE STRENGTH — swing_v2 was the ONLY scored strategy without it.
#   meanrev_scoring compares 63-day return vs SPY; intraday_scoring compares
#   since-open return vs SPY. This closes that inconsistency with the same
#   63-day window meanrev uses. A stock breaking out while lagging the index
#   is a weaker breakout than the same pattern in a leader.
#
#   ADX — reuses the single Wilder implementation in meanrev_scoring (which
#   regime_allocation also imports), so there is ONE ADX in the codebase, not
#   a fourth. Threshold matches the regime allocator's 20: below that a
#   "trend" is drift, and a pullback inside drift is just noise.
#
# Deliberately NOT added from the same review: a 10-factor composite score
# (10 free weights fitted to one regime), sector-relative strength (no sector
# ETF feed), an earnings-calendar factor (no fundamental pipeline), and
# top-N-by-score daily selection (that is xsectmom; two ranking desks on one
# universe is one desk with extra steps).
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# ADAPTIVE EXITS (2026-08-01) — all DEFAULT OFF, and that is deliberate.
#
# Exits move this strategy's numbers more than entries do: A_full and
# A_simple differ ONLY in the 2R half-exit, and that single difference moved
# Sharpe 0.81 -> 0.71 over 730 days. So exit changes are high-value AND the
# easiest place to overfit. Each option below is therefore switchable and off
# until the replay prices it — the deployed configuration should never be one
# the harness has not measured.
#
# Two of the three reuse logic that already exists rather than inventing it:
#   volatility expansion  = meanrev_scoring's rule (current ATR > k x entry
#                           ATR), with entry ATR DERIVED from stop distance
#   ATR trail             = xsection's tiered trail (looser far from entry,
#                           tighter once a gain is banked)
#   ADX decay             = new, but reuses the shared Wilder ADX and the
#                           entry reading swing_v2 now already computes
# ---------------------------------------------------------------------------
TRAIL_ATR = float(os.getenv("SWING_V2_TRAIL_ATR", "0"))        # 0 = off
TRAIL_AFTER_R = float(os.getenv("SWING_V2_TRAIL_AFTER_R", "1.0"))
VOL_EXIT_MULT = float(os.getenv("SWING_V2_VOL_EXIT", "0"))     # 0 = off
ADX_DECAY_FRAC = float(os.getenv("SWING_V2_ADX_DECAY", "0"))   # 0 = off

RS_LOOKBACK = int(os.getenv("SWING_V2_RS_LOOKBACK", "63"))
RS_REQUIRED = os.getenv("SWING_V2_RS", "on").strip().lower() not in (
    "off", "false", "0", "no")
ADX_MIN = float(os.getenv("SWING_V2_ADX_MIN", "20"))
ADX_REQUIRED = os.getenv("SWING_V2_ADX", "on").strip().lower() not in (
    "off", "false", "0", "no")

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
    """{sym: [{'t','o','h','l','c','v'}, ...]} oldest->newest, daily bars.

    `start` is REQUIRED: omitted, Alpaca defaults it to the current day and
    returns ~1 bar per symbol — which killed all 63 names at the 60-bar
    minimum on first deploy (2026-07-20, kills: insufficient_history=63).
    250 calendar days ≈ 172 trading bars, comfortable headroom over the
    120-bar window we keep."""
    from datetime import timedelta, timezone as _tz
    start = (datetime.now(_tz.utc) - timedelta(days=250)).strftime("%Y-%m-%d")
    out: dict[str, list[dict]] = {}
    for i in range(0, len(symbols), 50):
        chunk = symbols[i:i + 50]
        page = None
        while True:
            params = {"symbols": ",".join(chunk), "timeframe": "1Day",
                      "start": start, "limit": 10000, "adjustment": "split"}
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
    return {s: _drop_partial_bar(b)[-limit:] for s, b in out.items()}


def _drop_partial_bar(bars: list[dict]) -> list[dict]:
    """Remove today's IN-PROGRESS daily bar (fix 2026-08-01).

    fetch_daily_bars sends no `end`, so Alpaca returns the current session's
    bar while it is still forming. detect_setup then evaluated `bars[-1]` --
    a candle that had not finished -- despite documenting "completed daily
    bars only". Three consequences, all visible in the 2026-07-21 session
    logs where the funnel read no_bullish_candle=11/setup_volume=6 at midday
    and 8/9 by the close, then froze after the bell:

      * engulfing / hammer / strong-close were judged on a forming candle,
        so setups appeared and vanished during the day
      * cur["v"] was PARTIAL volume, making the 1.2x test far too strict in
        the morning and progressively looser toward the close
      * _daily_exits compared against b[-1]["c"], which mid-session is just
        the live price -- so the EMA20 exit could fire on a "close" that was
        not one

    After the bell the session bar IS complete, so it is kept: the rule is
    "drop it only while it is still forming".
    """
    if not bars:
        return bars
    now = datetime.now(ET)
    last_day = bars[-1]["t"][:10]
    if last_day != now.strftime("%Y-%m-%d"):
        return bars                      # last bar is a prior session
    closed = (now.hour, now.minute) >= (16, 0)
    if closed:
        return bars                      # today's bar has finished
    return bars[:-1]


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
    adx_at_setup: float = 0.0  # for the ADX-decay exit; 0 = unknown


def detect_setup(sym: str, bars: list[dict],
                 bench_closes: Optional[list[float]] = None
                 ) -> tuple[Optional[Setup], str]:
    """Returns (Setup|None, kill_reason). Uses completed daily bars only.

    bench_closes: SPY closes, aligned newest-last. When absent the relative
    strength gate is SKIPPED rather than failing the candidate — a missing
    benchmark must not silently reject the whole universe.
    """
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
    # --- ADX: is this actually a trend, or drift? ---
    adx_now = None
    if ADX_REQUIRED or ADX_DECAY_FRAC > 0:
        try:
            from meanrev_scoring import adx as _adx
            adx_now = _adx([b["h"] for b in bars], [b["l"] for b in bars],
                           [b["c"] for b in bars], 14)
        except Exception:  # noqa: BLE001 — a missing helper must not reject
            adx_now = None
        if ADX_REQUIRED and adx_now is not None and adx_now < ADX_MIN:
            return None, f"adx_weak({adx_now:.0f}<{ADX_MIN:.0f})"
    # --- relative strength: is this a leader or a laggard? ---
    if RS_REQUIRED and bench_closes and len(bench_closes) > RS_LOOKBACK \
            and len(closes) > RS_LOOKBACK:
        b0 = bench_closes[-1 - RS_LOOKBACK]
        c0 = closes[-1 - RS_LOOKBACK]
        if b0 > 0 and c0 > 0:
            stock_ret = closes[-1] / c0 - 1
            bench_ret = bench_closes[-1] / b0 - 1
            if stock_ret <= bench_ret:
                return None, (f"rel_strength({stock_ret:+.1%}<="
                              f"{bench_ret:+.1%})")
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
                 vr, av20, cur["t"][:10],
                 adx_at_setup=(adx_now or 0.0)), "ok"


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
    high_water: float = 0.0    # highest close since entry, for the trail
    entry_atr: float = 0.0     # ATR14 at entry, for the volatility exit
    entry_adx: float = 0.0     # ADX14 at entry, for the decay exit


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
    if ROUTE_LIVE and variant == LIVE_VARIANT:
        # Hand the trade to the engine. Sizing, gates, brackets, reconcile and
        # every portfolio overlay are the engine's job — swing_v2's own
        # `shares` figure is deliberately discarded here so there is exactly
        # one sizing authority.
        try:
            from models import Signal, SignalSource
            _pending.append(Signal(
                SignalSource.TREND, s.symbol,
                reason=(f"swing_v2 {variant}: pullback+candle, "
                        f"stop={stop:.2f}"),
                raw={"stop": stop, "variant": variant,
                     "atr14": s.atr14, "adx": s.adx_at_setup,
                     "source": "swing_v2"}))
            BOOK.entries_today[variant] = BOOK.entries_today.get(variant, 0) + 1
            log.warning("SWING2 ROUTED var=%s %s px=%.2f stop=%.2f -> "
                        "swing engine (engine sizes and executes)",
                        variant, s.symbol, px, stop)
            _audit_mirror("swing2_routed", variant=variant, ticker=s.symbol,
                          px=round(px, 2), stop=round(stop, 2))
        except Exception as e:  # noqa: BLE001 — never break the scan
            log.error("SWING2 route failed for %s (%s) — no order placed",
                      s.symbol, e)
        return

    pos = Position(s.symbol, variant, px, stop, px - stop, shares,
                   datetime.now(ET).strftime("%Y-%m-%d"),
                   high_water=px, entry_atr=s.atr14,
                   entry_adx=s.adx_at_setup)
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
    if fraction < 1.0 and n <= 0:
        # int(1 * 0.5) == 0: the old code sold NOTHING, then set
        # half_taken=True and moved the stop to breakeven -- flagging profit
        # that was never taken. With the 10% notional cap, small share counts
        # are routine on high-priced names (CAT at ~$900 sizes to 8 shares).
        # Too small to halve: take the whole position at the 2R target.
        log.info("SWING2 %s: %d share(s) too few to halve — taking the full "
                 "position at %s", p.symbol, p.shares, reason)
        n, fraction = p.shares, 1.0
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


# ---------------------------------------------------------------------------
# LIVE PORT (2026-08-01). swing_v2 still refuses to place its OWN orders —
# that refusal is correct and stays. What changes: when SWING_V2_ROUTE=on it
# EMITS Signal objects instead of shadow-booking them, and swing_engine
# executes. The engine already owns bracket orders, boot reconcile, the bench
# gate, the loss cooldown, the regime multiplier and the correlation check;
# routing through it means swing_v2 inherits all of that rather than
# reimplementing any of it.
#
# The signal carries the STRUCTURE STOP in signal.raw. Without it the engine
# would size on its own 2.5xATR stop, which is not what the backtest measured
# — the structure stop IS part of the strategy, not an execution detail.
# ---------------------------------------------------------------------------
ROUTE_LIVE = os.getenv("SWING_V2_ROUTE", "off").strip().lower() in (
    "on", "true", "1", "yes")
_pending: list = []          # Signals produced this cycle, drained by main


def take_pending_signals() -> list:
    """Drain and return Signals for the router. Empty unless ROUTE_LIVE."""
    global _pending
    out, _pending = _pending, []
    return out


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
            # SPY is fetched alongside the universe as the RS benchmark. It is
            # never traded here — it exists only to answer "is this name
            # leading or lagging the index?", the gate meanrev and intraday
            # already apply and swing_v2 did not.
            bars = fetch_daily_bars(symbols + (["SPY"] if "SPY" not in symbols
                                               else []))
        except Exception as e:
            log.error("SWING2 data fetch failed: %s", e)
            if health_record:
                health_record("swing_v2_data", False, str(e)[:80])
            return
        if health_record:
            health_record("swing_v2_data", True, f"{len(bars)}/{len(symbols)} syms")
        bench = [b["c"] for b in bars.get("SPY", [])] or None
        if RS_REQUIRED and not bench:
            log.warning("SWING2: SPY bars unavailable — relative-strength "
                        "gate SKIPPED this pass (failing open rather than "
                        "rejecting the whole universe)")
        kills: dict[str, int] = {}
        new = 0
        for sym in symbols:
            if sym not in bars:
                kills["no_bars"] = kills.get("no_bars", 0) + 1
                continue
            s, why = detect_setup(sym, bars[sym], bench)
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
                and key.replace("A:", "B:") not in BOOK.pos
                and _concurrent("A") < MAX_CONCURRENT
                and BOOK.entries_today.get("A", 0) < MAX_NEW_PER_DAY):
            # Book the OBSERVED price, not the trigger (fix 2026-08-01).
            # The breach is only detected AFTER it happens, so a real
            # stop-buy fills at or above wherever price is now -- never back
            # at setup_high + 0.01. Recording the trigger understated every
            # A entry by the gap between the two and biased the A/B
            # comparison in A's favour, since B books an observed price.
            fill = max(p, st.setup_high + 0.01)
            slip = fill - (st.setup_high + 0.01)
            if slip > 0:
                log.info("SWING2 A slippage %s: trigger %.2f -> observed "
                         "%.2f (+%.2f/share)", sym, st.setup_high + 0.01,
                         fill, slip)
            _enter("A", st, fill, equity, live and LIVE_VARIANT == "A")

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
    # Spec: enter at the NEXT OPEN after a completed close above setup_high.
    # The old code entered at last["c"] -- the breakout bar's own close --
    # which is both the wrong price and unobtainable in real time (fix
    # 2026-08-01). Confirmation now needs bars[-2]; the fill is bars[-1]["o"].
    if len(bars) < 2:
        return
    confirm, entry_bar = bars[-2], bars[-1]
    if confirm["t"][:10] <= st.created:
        return
    if entry_bar["t"][:10] <= confirm["t"][:10]:
        return
    if confirm["c"] > st.setup_high and st.avg_vol20 \
            and confirm["v"] >= VOL_MULT_B * st.avg_vol20:
        if f"A:{st.symbol}" in BOOK.pos:
            return                        # variant A already holds this name
        _enter("B", st, entry_bar["o"], equity, live and LIVE_VARIANT == "B")


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
        p.high_water = max(p.high_water or p.entry_px, last_close)
        atr_now = atr(b, 14)

        # ---- ADAPTIVE TRAILING STOP (off unless SWING_V2_TRAIL_ATR > 0) ---
        # Ratchets UP only, and only after the trade has banked TRAIL_AFTER_R
        # of open profit — a trail that engages immediately just converts the
        # structure stop into a tighter one and stops the trade out in normal
        # noise before the thesis has had room.
        if TRAIL_ATR > 0 and atr_now and p.r > 0:
            # exit_rules owns the arithmetic (engage-after-R + never-widen);
            # the thresholds stay here as this strategy's parameters.
            try:
                import exit_rules
                p.stop = exit_rules.trail_after_r(
                    p.entry_px, p.high_water, p.r, TRAIL_AFTER_R,
                    atr_now, TRAIL_ATR, p.stop)
            except ImportError:      # standalone/backtest use
                if p.high_water >= p.entry_px + TRAIL_AFTER_R * p.r:
                    p.stop = max(p.stop, p.high_water - TRAIL_ATR * atr_now)

        # ---- exit priority: stop-like reasons first, then structure -------
        if VOL_EXIT_MULT > 0 and atr_now and p.entry_atr > 0 \
                and atr_now > VOL_EXIT_MULT * p.entry_atr:
            # Same rule meanrev's ladder uses: the trade was sized for the
            # volatility at entry; if realised volatility has expanded far
            # past that, the environment the setup assumed no longer exists.
            _exit(key, last_close, f"vol_expansion({atr_now:.2f}>"
                  f"{VOL_EXIT_MULT:.1f}x{p.entry_atr:.2f})", this_live)
        elif ADX_DECAY_FRAC > 0 and p.entry_adx > 0:
            adx_now = None
            try:
                from meanrev_scoring import adx as _adx
                adx_now = _adx([x["h"] for x in b], [x["l"] for x in b],
                               [x["c"] for x in b], 14)
            except Exception:  # noqa: BLE001
                adx_now = None
            if adx_now is not None \
                    and adx_now < ADX_DECAY_FRAC * p.entry_adx:
                _exit(key, last_close, f"adx_decay({adx_now:.0f}<"
                      f"{ADX_DECAY_FRAC:.2f}x{p.entry_adx:.0f})", this_live)
            elif e20 and last_close < e20 and (p.half_taken or p.bars_held >= 2):
                _exit(key, last_close, "ema20", this_live)
            elif p.bars_held >= TIME_STOP_DAYS \
                    and last_close < p.entry_px + p.r:
                _exit(key, last_close, "time", this_live)
        elif e20 and last_close < e20 and (p.half_taken or p.bars_held >= 2):
            # Two branches collapsed into one condition (2026-08-01): the
            # old code had separate half_taken and bars_held>=2 arms that
            # took identical action.
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
