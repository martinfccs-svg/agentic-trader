"""intraday_scoring.py — weighted momentum score for intraday v2 (shadow).

Triage of the operator's 22-filter proposal (2026-07-23). Implemented here:
the computable, non-contradictory tier, structured per the proposal's OWN
best idea — a continuous weighted score with a few hard gates — rather than
the 22-way AND chain (which is self-contradictory: pullback-to-EMA9 vs
4-of-5-green-bars-and-rising-highs cannot co-occur, and would trade ~never).

HARD GATES (vetoes):
  G1 time window     see WINDOWS_ET below — configurable; the midday
                     dead zone (11:15–13:30) is refused outright
  G2 market filter   SPY above its session VWAP
  G3 rel volume      rv >= 2.0 on the 1-min bar (raised from v6's 1.3)
  G4 vol sanity      intraday ATR% within [0.10%, 1.5%] of price — the
                     proposal's 1.5% MINIMUM was a daily number in intraday
                     scale (would reject ~everything); corrected to a BAND:
                     too quiet is chop, too wild is a halt-and-squeeze

WEIGHTED SCORE (0..1; shadow logs it, live mode would require >= MIN):
  0.25 multi-TF EMA alignment   EMA9>EMA20 on 1m, 5m, 15m (resampled
                                LOCALLY from the 1-min bars already fetched
                                — zero extra Finnhub calls)
  0.20 relative volume          scaled: rv 2.0 -> 0, rv 4.0+ -> full
  0.20 VWAP position            above VWAP but NOT extended: full marks
                                within 0.5 ATR above, fading to 0 at 1.5
                                ATR (the proposal's chase cap)
  0.20 relative strength        outperforming SPY since the open
  0.15 pullback quality         low tagged EMA9 within last 3 bars AND
                                current close back above it (the proposal's
                                pullback, softened from a veto to a factor
                                so it stops fighting the momentum factors)

EXCLUDED, with reasons (recorded so they aren't relitigated):
  - candle body / gap / green-bar filters: need OPEN prices; Bars carry
    none. Finnhub's candle endpoint returns 'o' — adding it to models.Bars
    + feed_layer is the enabling infra patch, deliberately separate.
  - bid/ask spread: quote feed has no bid/ask; dollar-volume gate covers it
  - scale-out 30/30/20: brokers.sell() is whole-position only (same
    deferred brokers.py project as meanrev's partials)
  - daily-loss gate: duplicates the kill switch
  - ADX/MACD on 1-min: deferred; alignment+rv+RS carry the same intent
    with fewer knobs on this timeframe

COOLDOWN (the most on-target item in the proposal — it attacks the actual
Jul-8 churn diagnosis): after a losing intraday exit, the ticker is
untradeable for COOLDOWN_MIN minutes. Engine calls note_loss()/in_cooldown().

Env:
  INTRADAY_SCORE_MIN     default 0.70 (live mode threshold; shadow logs all)
  INTRADAY_RV_GATE       default 2.0
  INTRADAY_COOLDOWN_MIN  default 45
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo
from indicators import ema  # canonical (2026-08-02)

ET = ZoneInfo("America/New_York")

SCORE_MIN = float(os.getenv("INTRADAY_SCORE_MIN", "0.70"))
RV_GATE = float(os.getenv("INTRADAY_RV_GATE", "2.0"))
COOLDOWN_MIN = int(os.getenv("INTRADAY_COOLDOWN_MIN", "45"))
ATR_PCT_MIN, ATR_PCT_MAX = 0.0010, 0.015   # intraday scale, corrected

# SESSION AND MIDDAY BREAK (2026-08-05)
#
# The break was 11:15-13:30 — 135 minutes, 38% of the tradeable day. Narrowed
# to ONE HOUR: 11:00-12:00 ET by default (2026-08-06, set deliberately).
#
# Note this is the EARLY side of the old 11:15-13:30 exclusion, not the
# thinnest hour — 12:00-13:00 typically carries the lowest volume of the US
# session. So the reopened 12:00-13:30 stretch is thinner than the 11:00-11:15
# that is now excluded. The WINDOW-BLOCKED counter and intraday P&L will show
# whether that matters; this is a setting, not a finding.
#
# Configurable rather than hardcoded, so this is a setting that can be tuned
# or reverted from Railway instead of a redeploy — and so config_check can
# range-check it.
#
# WHY A BREAK AT ALL, still: relative volume is a TRAILING 20-bar ratio, so
# during lunch its own baseline is lunch. A 2.2x "surge" at 12:30 is ~55k
# shares/min against ~162k in a QUIET minute at the open — it passes the rv
# gate on a third of the liquidity. No other gate sees absolute tape depth,
# which is why the break cannot simply be deleted.
_log = logging.getLogger("intraday_scoring")


def _hm(env: str, default: tuple) -> tuple:
    raw = os.getenv(env, "").strip()
    if not raw:
        return default
    try:
        h, m = raw.split(":")
        h, m = int(h), int(m)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return (h, m)
    except (ValueError, AttributeError):
        pass
    _log.error("intraday: %s=%r is not HH:MM — using %02d:%02d",
               env, raw, *default)
    return default


SESSION_OPEN_ET = _hm("INTRADAY_SESSION_OPEN", (9, 35))
BREAK_START_ET = _hm("INTRADAY_BREAK_START", (11, 0))
BREAK_END_ET = _hm("INTRADAY_BREAK_END", (12, 0))
SESSION_CLOSE_ET = _hm("INTRADAY_SESSION_CLOSE", (15, 30))

if not (SESSION_OPEN_ET < BREAK_START_ET < BREAK_END_ET < SESSION_CLOSE_ET):
    _log.error("intraday: break %s-%s does not sit inside session %s-%s — "
               "reverting to defaults so the desk cannot trade a nonsense "
               "schedule", BREAK_START_ET, BREAK_END_ET, SESSION_OPEN_ET,
               SESSION_CLOSE_ET)
    SESSION_OPEN_ET, BREAK_START_ET = (9, 35), (11, 0)
    BREAK_END_ET, SESSION_CLOSE_ET = (12, 0), (15, 30)

WINDOWS_ET = ((SESSION_OPEN_ET, BREAK_START_ET),
              (BREAK_END_ET, SESSION_CLOSE_ET))


def schedule_text() -> str:
    """The ACTIVE schedule, derived from the constants.

    The reject legend used to hardcode the schedule as a literal string
    (the old two-window layout). When the windows were narrowed the
    gate changed and the legend did not — so the log would have described a
    schedule the desk was not running, and anyone reading it would have drawn
    the wrong conclusion from a correct observation.

    That is the exact failure this project has a standing rule about: when a
    label is misread, fix the LABEL. A legend that repeats a constant instead
    of reading it is a label waiting to lie.
    """
    return (f"trading window "
            f"{SESSION_OPEN_ET[0]:02d}:{SESSION_OPEN_ET[1]:02d}-"
            f"{BREAK_START_ET[0]:02d}:{BREAK_START_ET[1]:02d} / "
            f"{BREAK_END_ET[0]:02d}:{BREAK_END_ET[1]:02d}-"
            f"{SESSION_CLOSE_ET[0]:02d}:{SESSION_CLOSE_ET[1]:02d} ET")


def excluded_region(now: Optional[datetime] = None) -> Optional[str]:
    """WHICH exclusion is blocking, or None if inside a window.

    "Out of window" covers three different regions with three different
    reasons, and lumping them together makes the live evidence useless for
    deciding about any one of them:

        09:30-09:35  the opening auction imbalance unwinding
        11:15-13:30  THE MIDDAY BREAK — 38% of the tradeable day
        15:30-16:00  closing auction distortion

    Naming the region means a week of logs answers "what does the BREAK
    cost?" rather than "what does being out of window cost?".
    """
    n = now or datetime.now(ET)
    hm = (n.hour, n.minute)
    if hm < SESSION_OPEN_ET:
        return "pre_open"
    if BREAK_START_ET <= hm < BREAK_END_ET:
        return "midday_break"
    if hm >= SESSION_CLOSE_ET:
        return "closing"
    return None


def in_trading_window(now: Optional[datetime] = None) -> bool:
    now = (now or datetime.now(ET)).astimezone(ET)
    hm = (now.hour, now.minute)
    return any(a <= hm < b for a, b in WINDOWS_ET)


def resample(closes: list[float], highs: list[float], lows: list[float],
             volumes: list[float], factor: int):
    """Aggregate 1-min series into factor-minute series (close=last,
    high=max, low=min, vol=sum). Trailing partial bucket included — for
    EMA-alignment purposes a forming bar is information, not noise."""
    c, h, l, v = [], [], [], []
    for i in range(0, len(closes), factor):
        j = min(i + factor, len(closes))
        c.append(closes[j - 1])
        h.append(max(highs[i:j]))
        l.append(min(lows[i:j]))
        v.append(sum(volumes[i:j]))
    return c, h, l, v


def _aligned(closes: list[float]) -> Optional[bool]:
    e9, e20 = ema(closes, 9), ema(closes, 20)
    return None if e9 is None or e20 is None else e9 > e20


@dataclass
class IntradayCard:
    ticker: str
    gate_window: bool
    gate_market: bool
    gate_rv: bool
    gate_volband: bool
    score: float
    parts: dict
    v2_stop: Optional[float] = None   # structure stop (shadow evidence)

    @property
    def gates_ok(self) -> bool:
        return (self.gate_window and self.gate_market and self.gate_rv
                and self.gate_volband)

    @property
    def window_is_sole_blocker(self) -> bool:
        """Would this trade have been taken if the TIME WINDOW did not exist?

        Added 2026-08-05 to make a live question answerable from logs instead
        of argument: "the other criteria already cover it, so drop the
        window." They may not — relative volume is a TRAILING 20-bar ratio, so
        during lunch its baseline is also lunch. A 2.2x lunch 'surge' is ~55k
        shares/min against ~162k in a QUIET minute at the open: it passes the
        rv gate on a third of the liquidity, because rv measures a CHANGE in
        activity rather than a LEVEL.

        Counting how often the window is the ONLY failing gate settles the
        cost side empirically. If it is rare, removing it changes little and
        the debate is moot. If it is common, the window is doing real work
        and opening it needs evidence, not reasoning.
        """
        return (not self.gate_window and self.gate_market and self.gate_rv
                and self.gate_volband and self.score >= SCORE_MIN)

    @property
    def blocked_region(self) -> Optional[str]:
        """Which exclusion stopped it — so the evidence names the region."""
        return excluded_region() if not self.gate_window else None

    def qualifies(self, score_min: float = SCORE_MIN) -> bool:
        return self.gates_ok and self.score >= score_min


def score_intraday(ticker: str,
                   closes_1m: list[float], highs_1m: list[float],
                   lows_1m: list[float], vols_1m: list[float],
                   price: float, vwap: Optional[float],
                   intra_atr: Optional[float], rel_volume: Optional[float],
                   spy_price: Optional[float], spy_vwap: Optional[float],
                   spy_open_ret: Optional[float],
                   spy_above_ema50: Optional[bool] = None,
                   now: Optional[datetime] = None) -> Optional[IntradayCard]:
    """spy_open_ret = SPY's return since today's open (for rel strength).
    Returns None only if 1-min history is too thin to say anything."""
    if len(closes_1m) < 30:
        return None

    gate_window = in_trading_window(now)
    # Market gate (strengthened 2026-07-23): SPY above session VWAP AND —
    # when the caller supplies it — above its daily EMA50 ("no longs below
    # EMA50"). spy_above_ema50=None (data unavailable) does not veto:
    # fail-open, same rule as regime.py.
    gate_market = (spy_price is not None and spy_vwap is not None
                   and spy_price > spy_vwap
                   and spy_above_ema50 is not False)
    rv = rel_volume if rel_volume is not None else 0.0
    gate_rv = rv >= RV_GATE
    atr_pct = (intra_atr / price) if (intra_atr and price) else None
    gate_volband = atr_pct is not None and ATR_PCT_MIN <= atr_pct <= ATR_PCT_MAX

    parts: dict[str, float] = {}
    # multi-TF alignment (local resample; zero extra API calls)
    a1 = _aligned(closes_1m)
    c5, h5, l5, v5 = resample(closes_1m, highs_1m, lows_1m, vols_1m, 5)
    c15, *_ = resample(closes_1m, highs_1m, lows_1m, vols_1m, 15)
    a5, a15 = _aligned(c5), _aligned(c15)
    n_aligned = sum(1 for a in (a1, a5, a15) if a)
    parts["mtf_alignment"] = 0.25 * (n_aligned / 3.0)
    # relative volume, scaled 2.0 -> 0 .. 4.0 -> full
    parts["rel_volume"] = 0.20 * max(0.0, min(1.0, (rv - 2.0) / 2.0))
    # VWAP position: above but not extended
    if vwap and intra_atr and price > vwap:
        dist = (price - vwap) / intra_atr
        parts["vwap_position"] = 0.20 * (1.0 if dist <= 0.5 else
                                         max(0.0, (1.5 - dist)))
    else:
        parts["vwap_position"] = 0.0
    # relative strength vs SPY since open
    stock_open_ret = closes_1m[-1] / closes_1m[0] - 1
    parts["rel_strength"] = (0.20 if spy_open_ret is not None
                             and stock_open_ret > spy_open_ret else 0.0)
    # pullback quality: EMA9 tagged within last 3 bars, close back above
    e9 = ema(closes_1m, 9)
    if e9 is not None:
        tagged = any(l <= e9 for l in lows_1m[-3:])
        parts["pullback"] = 0.15 if (tagged and closes_1m[-1] > e9) else 0.0
    else:
        parts["pullback"] = 0.0

    # v2 structure stop (reviewer #5, bounded): below the tightest nearby
    # structure — min(last-5 low, 1-min EMA20) — minus 0.25 ATR, but never
    # wider than 2.5 x ATR from price (raw min() picks the WIDEST support,
    # so unbounded it can put the stop in the basement). Logged in shadow
    # next to v6's plain 2.5xATR stop so the better stop wins on data.
    v2_stop = None
    if intra_atr and e9 is not None:
        e20_1m = ema(closes_1m, 20)
        structure = min(min(lows_1m[-5:]),
                        e20_1m if e20_1m is not None else min(lows_1m[-5:]))
        v2_stop = max(structure - 0.25 * intra_atr,
                      price - 2.5 * intra_atr)
        v2_stop = round(v2_stop, 2)

    return IntradayCard(ticker, gate_window, gate_market, gate_rv,
                        gate_volband, round(sum(parts.values()), 3), parts,
                        v2_stop=v2_stop)


# ------------------------------------------------------------- cooldown
_cooldowns: dict[str, float] = {}


def note_loss(ticker: str) -> None:
    """Call on a losing intraday exit: ticker untradeable for COOLDOWN_MIN."""
    _cooldowns[ticker] = time.time() + COOLDOWN_MIN * 60


def in_cooldown(ticker: str,
                closes_1m: Optional[list[float]] = None) -> bool:
    """Time-based lockout, with an early release (reviewer suggestion,
    2026-07-23): if price CLOSES back above its 1-min EMA20, the trend has
    resumed and the lockout lifts — re-entering strength is not revenge
    trading. Pass closes_1m to enable; without it, pure time-based."""
    until = _cooldowns.get(ticker)
    if until is None:
        return False
    if time.time() >= until:
        del _cooldowns[ticker]
        return False
    if closes_1m and len(closes_1m) >= 20:
        e20 = ema(closes_1m, 20)
        if e20 is not None and closes_1m[-1] > e20:
            del _cooldowns[ticker]
            return False
    return True
