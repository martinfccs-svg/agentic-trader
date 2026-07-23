"""intraday_scoring.py — weighted momentum score for intraday v2 (shadow).

Triage of the operator's 22-filter proposal (2026-07-23). Implemented here:
the computable, non-contradictory tier, structured per the proposal's OWN
best idea — a continuous weighted score with a few hard gates — rather than
the 22-way AND chain (which is self-contradictory: pullback-to-EMA9 vs
4-of-5-green-bars-and-rising-highs cannot co-occur, and would trade ~never).

HARD GATES (vetoes):
  G1 time window     trade only 9:35–11:15 and 13:30–15:30 ET; the midday
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

import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

SCORE_MIN = float(os.getenv("INTRADAY_SCORE_MIN", "0.70"))
RV_GATE = float(os.getenv("INTRADAY_RV_GATE", "2.0"))
COOLDOWN_MIN = int(os.getenv("INTRADAY_COOLDOWN_MIN", "45"))
ATR_PCT_MIN, ATR_PCT_MAX = 0.0010, 0.015   # intraday scale, corrected

WINDOWS_ET = (((9, 35), (11, 15)), ((13, 30), (15, 30)))


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


def ema(values: list[float], n: int) -> Optional[float]:
    if len(values) < n:
        return None
    k = 2.0 / (n + 1)
    e = sum(values[:n]) / n
    for x in values[n:]:
        e = x * k + e * (1 - k)
    return e


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
