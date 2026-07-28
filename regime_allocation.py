"""regime_allocation.py — the CIO layer: classify the market, size the desks.

Generates NO signals and opens NO positions. It answers one question, once
per day: given the market state, how aggressive should each strategy be?
Every engine's share count is then multiplied by its regime factor.

WHAT IT USES (only data the bot already collects — no VIX, no A/D line, no
correlation matrix; those need feeds this system does not have):

  trend    SPY EMA50 vs EMA200 separated by >=1%, price vs the SLOW average,
           and the 20-day slope of EMA50 with tolerance -> BULL/NEUTRAL/BEAR
           Deliberately slow: an early version tested price against EMA50 and
           a single noisy day flipped the regime while EMA50 sat 7% above
           EMA200 in an obvious uptrend. A classifier that thrashes on daily
           noise makes allocation worse than no allocation, so participation
           is measured against EMA200 and the slope carries a tolerance band.
  vol      SPY ATR(14) as a % of price, compared to its own 100-day median
           -> CALM / NORMAL / VOLATILE   (realized volatility, not implied)
  breadth  share of the universe trading above its own EMA50 — computable
           from bars already cached for every name, so it costs nothing
           -> BROAD (>60%) / MIXED / NARROW (<40%)

Those three collapse to one label: STRONG_BULL, WEAK_BULL, SIDEWAYS, BEAR,
or HIGH_VOL. Documented limits: no implied vol, no advance/decline line, no
new-high/new-low counts. It is a 3-factor classifier honest about its inputs,
not the 8-state one a data vendor could support.

MULTIPLIERS, NOT PERCENTAGES — the important design decision.
A table of target capital percentages would silently re-plumb sizing the day
it went live. Instead every cell is a MULTIPLIER on the sizing an engine
already computes, normalised so WEAK_BULL (the ordinary state) is 1.0
everywhere. Turning this on in a normal market therefore changes nothing;
it only leans in or out as conditions leave the middle.

  regime        swing  pullback  meanrev  intraday  xsectmom
  STRONG_BULL    1.25      1.25     0.50      0.75      1.25
  WEAK_BULL      1.00      1.00     1.00      1.00      1.00
  SIDEWAYS       0.50      0.50     1.50      1.25      0.75
  BEAR           0.25      0.25     0.75      0.75      0.50
  HIGH_VOL       0.25      0.25     0.50      0.50      0.50

Reasoning, not tuning: trend strategies lean in when trend and breadth
agree and stand down when they don't; mean reversion is the reverse; nobody
gets full size in a bear or a volatility shock. These numbers are a HYPOTHESIS
— the doc that proposed them said so, and shadow mode exists to test it.

MODES (env REGIME_ALLOC):
  shadow (DEFAULT)  classify + log the multipliers, apply NOTHING
  live              engines multiply their share counts by the factor
  off               no classification, no logging

Never gates entries to zero: the floor is FLOOR_MULT (default 0.25), so a
regime can shrink a desk but never silently halt it. Halting is what
regime.py's risk-off gate and the kill switch are for — one mechanism per job.

Fails OPEN: any data problem returns WEAK_BULL / all-1.0 multipliers with a
loud log. A sizing overlay must never become a new deadlock class
(2026-07-07 lesson).
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("regime_allocation")

MODE = os.getenv("REGIME_ALLOC", "shadow").strip().lower()
TTL = float(os.getenv("REGIME_ALLOC_TTL_SECS", "3600"))
# A FAILED classification must not be cached for the full TTL. At boot the
# feed has no SPY bars yet, so the first call fails open to WEAK_BULL — and
# with a 1-hour TTL that neutral verdict then stuck for an hour, long after
# the data arrived. Failures are re-tried on this much shorter cadence.
FAIL_TTL = float(os.getenv("REGIME_ALLOC_FAIL_TTL_SECS", "60"))
FLOOR_MULT = float(os.getenv("REGIME_ALLOC_FLOOR", "0.25"))
SYMBOL = os.getenv("REGIME_SYMBOL", "SPY")

EMA_FAST, EMA_SLOW = 50, 200
ADX_PERIOD = 14
ADX_TREND_MIN = float(os.getenv("REGIME_ADX_MIN", "20"))
# ADX measures trend STRENGTH, not direction — a strong downtrend also reads
# high. So it gates BOTH bull and bear: without strength, structure alone is
# just drift, and drift belongs in SIDEWAYS (reviewer item 1).
VOL_PERSIST_DAYS = 5      # a volatility REGIME must persist; a one-day spike
                          # is an event, not a regime (reviewer item 3)
# REGIME PERSISTENCE (2026-07-24). A label must hold for PERSIST_DAYS
# consecutive sessions before it becomes the effective regime. Implemented
# STATELESSLY — the last few sessions are re-classified from the same cached
# bars rather than kept in a counter, because per-run state does not survive
# a redeploy and this bot redeploys several times a week. Deterministic:
# the same history always yields the same effective regime.
PERSIST_DAYS = int(os.getenv("REGIME_PERSIST_DAYS", "3"))
PERSIST_LOOKBACK = int(os.getenv("REGIME_PERSIST_LOOKBACK", "12"))
CONF_BLEND = os.getenv("REGIME_ALLOC_CONF_BLEND", "on").strip().lower() not in (
    "off", "false", "0", "no")
# NOTE: no smoothing state is persisted. Gradual transitions come from
# confidence blending (stateless) and PERSIST_DAYS confirmation (also
# stateless, by re-classifying prior sessions). Earlier drafts declared
# MAX_STEP / STATE_PATH knobs for calendar interpolation; both were removed
# rather than left dangling — an unused env var invites the assumption that
# a feature exists.
SLOPE_DAYS = 20
SLOPE_TOL = 0.005      # EMA50 may sag 0.5% over SLOPE_DAYS and still be "up"
                       # (a pullback inside an uptrend is not a regime change)
TREND_SEP_MIN = 0.01   # EMA50 must clear EMA200 by >=1% to count as a TREND.
                       # Without this, a flat market whose EMA50 sits a hair
                       # above EMA200 was labelled BULL and SIDEWAYS could
                       # never occur — which would starve exactly the regime
                       # mean reversion is supposed to feast in.
ATR_PERIOD, VOL_MEDIAN_DAYS = 14, 100
BREADTH_EMA = 50
BREADTH_BROAD, BREADTH_NARROW = 0.60, 0.40
VOL_CALM, VOL_HOT = 0.80, 1.50      # vs the median ATR%

ALLOCATION: dict[str, dict[str, float]] = {
    # STRONG_BULL leans hardest on the strategies that benefit most from broad,
    # persistent advances (reviewer item 7). Unvalidated preference, not
    # evidence — flagged as such until the shadow log says otherwise.
    "STRONG_BULL": {"swing": 1.25, "pullback": 1.35, "meanrev": 0.50,
                    "intraday": 0.75, "xsectmom": 1.40},
    "WEAK_BULL":   {"swing": 1.00, "pullback": 1.00, "meanrev": 1.00,
                    "intraday": 1.00, "xsectmom": 1.00},
    "SIDEWAYS":    {"swing": 0.50, "pullback": 0.50, "meanrev": 1.50,
                    "intraday": 1.25, "xsectmom": 0.75},
    "BEAR":        {"swing": 0.25, "pullback": 0.25, "meanrev": 0.75,
                    "intraday": 0.75, "xsectmom": 0.50},
    "HIGH_VOL":    {"swing": 0.25, "pullback": 0.25, "meanrev": 0.50,
                    "intraday": 0.50, "xsectmom": 0.50},
}
NEUTRAL = "WEAK_BULL"


# ------------------------------------------------------------- indicators
def ema(values: list[float], n: int) -> Optional[float]:
    if len(values) < n:
        return None
    k = 2.0 / (n + 1)
    e = sum(values[:n]) / n
    for v in values[n:]:
        e = v * k + e * (1 - k)
    return e


def ema_series(values: list[float], n: int) -> list[float]:
    """EMA at each point from n onward (for slope)."""
    if len(values) < n:
        return []
    k = 2.0 / (n + 1)
    e = sum(values[:n]) / n
    out = [e]
    for v in values[n:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def atr_pct(high: list[float], low: list[float], close: list[float],
            n: int = ATR_PERIOD) -> Optional[float]:
    if len(close) < n + 1 or not close[-1]:
        return None
    trs = []
    for i in range(len(close) - n, len(close)):
        trs.append(max(high[i] - low[i], abs(high[i] - close[i - 1]),
                       abs(low[i] - close[i - 1])))
    return (sum(trs) / n) / close[-1]


def _median(v: list[float]) -> Optional[float]:
    if not v:
        return None
    s = sorted(v)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2


# --------------------------------------------------------------- classify
@dataclass
class RegimeState:
    label: str = NEUTRAL
    trend: str = "?"
    vol: str = "?"
    breadth: str = "?"
    breadth_pct: Optional[float] = None        # share above own EMA50
    breadth_200_pct: Optional[float] = None    # share above own EMA200
    sector_participation: Optional[float] = None   # share of sectors leading
    adx: Optional[float] = None
    confidence: int = 0                        # 0-100, factor decisiveness
    detail: str = ""
    failed: bool = False        # True when this is a fail-open placeholder
    multipliers: dict[str, float] = field(default_factory=dict)

    def multiplier(self, system: str) -> float:
        return self.multipliers.get(system.lower(), 1.0)


def _adx(high, low, close, n=ADX_PERIOD):
    """Wilder ADX, reused from meanrev_scoring so there is ONE implementation
    in the codebase rather than two that can drift apart."""
    try:
        from meanrev_scoring import adx as _a
        return _a(high, low, close, n)
    except Exception:  # noqa: BLE001
        return None


def classify(spy_close: list[float], spy_high: list[float],
             spy_low: list[float],
             breadth_pct: Optional[float] = None,
             breadth_200_pct: Optional[float] = None,
             sector_participation: Optional[float] = None) -> RegimeState:
    """Pure function: bars in, regime out. No feed, no side effects."""
    st = RegimeState()
    e50 = ema(spy_close, EMA_FAST)
    e200 = ema(spy_close, EMA_SLOW)
    if e50 is None or e200 is None:
        st.detail = (f"insufficient history ({len(spy_close)} bars, need "
                     f"{EMA_SLOW}) — defaulting to {NEUTRAL}")
        st.multipliers = dict(ALLOCATION[NEUTRAL])
        return st
    px = spy_close[-1]

    # trend: EMA STRUCTURE first, participation vs the SLOW average, and a
    # tolerant slope. Price-vs-EMA50 is deliberately NOT used — it flips on
    # single-day noise (see module docstring).
    series = ema_series(spy_close, EMA_FAST)
    slope_up = (len(series) > SLOPE_DAYS
                and series[-1] >= series[-1 - SLOPE_DAYS] * (1 - SLOPE_TOL))
    # ADX gates trend STRENGTH in both directions (reviewer item 1): a
    # structurally-ordered but momentum-less market is drift, and drift is
    # SIDEWAYS, not a trend. Missing ADX (short history) does not veto.
    st.adx = _adx(spy_high, spy_low, spy_close)
    strong = st.adx is None or st.adx >= ADX_TREND_MIN
    if e50 > e200 * (1 + TREND_SEP_MIN) and px > e200 and slope_up and strong:
        st.trend = "BULL"
    elif e50 < e200 * (1 - TREND_SEP_MIN) and px < e200 and strong:
        st.trend = "BEAR"
    else:
        st.trend = "NEUTRAL"

    # volatility: current ATR% vs its own recent median (realized, not implied)
    cur = atr_pct(spy_high, spy_low, spy_close)
    hist = []
    for end in range(max(ATR_PERIOD + 1, len(spy_close) - VOL_MEDIAN_DAYS),
                     len(spy_close)):
        v = atr_pct(spy_high[:end], spy_low[:end], spy_close[:end])
        if v:
            hist.append(v)
    med = _median(hist)
    if cur and med:
        ratio = cur / med
        # PERSISTENCE (reviewer item 3): a volatility REGIME means elevated
        # for a while. Require the median of the last VOL_PERSIST_DAYS
        # readings to be hot too, so a single shock day is logged as an
        # event but does not re-allocate the whole book.
        recent = hist[-VOL_PERSIST_DAYS:] if len(hist) >= VOL_PERSIST_DAYS else []
        recent_med = _median(recent)
        sustained = bool(recent_med and med and recent_med / med >= VOL_HOT)
        if ratio >= VOL_HOT and sustained:
            st.vol = "VOLATILE"
        elif ratio >= VOL_HOT:
            st.vol = "SPIKE"          # elevated today, not yet a regime
        elif ratio <= VOL_CALM:
            st.vol = "CALM"
        else:
            st.vol = "NORMAL"
    else:
        st.vol = "NORMAL"
        ratio = 1.0

    # breadth: three participation measures, all from bars already cached
    # (reviewer items 2 and 6). % above EMA50 (fast), % above EMA200 (durable),
    # and SECTOR participation — the last matters because a raw name-count can
    # be carried by one crowded sector, which is exactly the narrow-leadership
    # case the reviewer wanted separated from a broad rally.
    st.breadth_pct = breadth_pct
    st.breadth_200_pct = breadth_200_pct
    st.sector_participation = sector_participation
    votes = [v for v in (breadth_pct, breadth_200_pct, sector_participation)
             if v is not None]
    if not votes:
        st.breadth = "MIXED"
    else:
        avg = sum(votes) / len(votes)
        if avg >= BREADTH_BROAD:
            st.breadth = "BROAD"
        elif avg <= BREADTH_NARROW:
            st.breadth = "NARROW"
        else:
            st.breadth = "MIXED"

    # collapse to a label — a SUSTAINED volatility regime outranks everything
    if st.vol == "VOLATILE":
        st.label = "HIGH_VOL"
    elif st.trend == "BEAR":
        st.label = "BEAR"
    elif st.trend == "BULL":
        st.label = ("STRONG_BULL" if st.breadth == "BROAD" else "WEAK_BULL")
    else:
        st.label = "SIDEWAYS" if st.breadth != "BROAD" else "WEAK_BULL"

    # ---- CONFIDENCE (reviewer item 4) ------------------------------------
    # How DECISIVELY does each factor read? Trend 40 / vol 30 / breadth 30.
    # This is not a probability — it is a measure of how far each input sits
    # from its own threshold, used to decide how far to lean.
    sep = abs(e50 / e200 - 1) if e200 else 0.0
    trend_c = 40.0 * min(1.0, sep / (3 * TREND_SEP_MIN)) if st.trend != "NEUTRAL" \
        else 40.0 * max(0.0, 1 - sep / (3 * TREND_SEP_MIN))
    if st.adx is not None:
        trend_c *= min(1.0, max(0.3, st.adx / 30.0))
    vol_c = 30.0 * (1.0 if st.vol in ("VOLATILE", "CALM")
                    else 0.6 if st.vol == "SPIKE" else 0.8)
    if votes:
        avg = sum(votes) / len(votes)
        # decisive when far from the 40-60% muddle, in either direction
        breadth_c = 30.0 * min(1.0, abs(avg - 0.5) / 0.25)
    else:
        breadth_c = 10.0        # no breadth data = weak evidence, not zero
    st.confidence = int(round(min(100.0, trend_c + vol_c + breadth_c)))

    # ---- BLEND (reviewer items 4 + 5) ------------------------------------
    # Low confidence pulls every multiplier toward 1.0 (baseline) instead of
    # applying the full regime lean. This gives GRADUAL transitions without a
    # calendar: a fresh, weakly-evidenced regime barely moves sizing, and the
    # lean grows as the evidence does. Chosen over day-by-day interpolation
    # because it is stateless (nothing to reset on redeploy) and because the
    # transition speed is then driven by evidence, not an arbitrary schedule.
    target = ALLOCATION[st.label]
    w = (st.confidence / 100.0) if CONF_BLEND else 1.0
    st.multipliers = {k: max(FLOOR_MULT, round(1.0 + (v - 1.0) * w, 3))
                      for k, v in target.items()}
    st.detail = (f"{SYMBOL} {px:.2f} EMA50 {e50:.2f} EMA200 {e200:.2f} "
                 f"sep {sep:+.2%} slope_up={slope_up} "
                 f"adx={st.adx:.0f}" if st.adx is not None else
                 f"{SYMBOL} {px:.2f} EMA50 {e50:.2f} EMA200 {e200:.2f}")
    if cur and med:
        st.detail += f" | atr% {cur:.4f} vs med {med:.4f} (x{ratio:.2f})"
    return st


# ------------------------------------------------------ live entry points
_cache: tuple[float, RegimeState] | None = None
_last_label: str | None = None
_clamp_logged: dict[str, float] = {}   # system -> last multiplier logged


def _classify_at(feed, universe, offset: int) -> Optional[RegimeState]:
    """Classification as it would have read `offset` sessions ago, from the
    same cached bars. Pure history — no stored state."""
    bars = feed.get_daily_bars(SYMBOL)
    if bars is None:
        return None
    n = len(bars.close) - offset
    if n < EMA_SLOW:
        return None
    b50 = b200 = sect = None
    if universe:
        b50, b200, sect = breadth_from_feed(feed, universe, offset)
    return classify(bars.close[:n], bars.high[:n], bars.low[:n],
                    b50, b200, sect)


def _persisted(feed, universe, today: RegimeState) -> tuple[RegimeState, int, str]:
    """Apply persistence. Returns (effective_state, run_length, raw_label).

    Rule: today's label becomes effective only once PERSIST_DAYS consecutive
    sessions agree on it. Until then the most recent label that DID achieve a
    full run stays in force. If no run exists anywhere in the lookback, the
    neutral baseline holds — never an unconfirmed regime.
    """
    if PERSIST_DAYS <= 1:
        return today, 1, today.label
    labels = [today.label]
    for off in range(1, PERSIST_LOOKBACK + 1):
        st = _classify_at(feed, universe, off)
        if st is None:
            break
        labels.append(st.label)
    run = 1
    for lab in labels[1:]:
        if lab == labels[0]:
            run += 1
        else:
            break
    if run >= PERSIST_DAYS:
        return today, run, today.label            # confirmed
    # today is unconfirmed — find the most recent label with a full run
    for start in range(1, len(labels)):
        r = 1
        for lab in labels[start + 1:]:
            if lab == labels[start]:
                r += 1
            else:
                break
        if r >= PERSIST_DAYS:
            held = RegimeState(label=labels[start], trend=today.trend,
                               vol=today.vol, breadth=today.breadth,
                               breadth_pct=today.breadth_pct,
                               breadth_200_pct=today.breadth_200_pct,
                               sector_participation=today.sector_participation,
                               adx=today.adx, confidence=today.confidence)
            w = (held.confidence / 100.0) if CONF_BLEND else 1.0
            held.multipliers = {k: max(FLOOR_MULT, round(1.0 + (v - 1.0) * w, 3))
                                for k, v in ALLOCATION[held.label].items()}
            held.detail = (f"{today.detail} | HELD at {held.label}: "
                           f"{today.label} has only {run}/{PERSIST_DAYS} "
                           f"session(s)")
            return held, run, today.label
    neutral = RegimeState(label=NEUTRAL, trend=today.trend, vol=today.vol,
                          breadth=today.breadth, confidence=today.confidence)
    neutral.multipliers = dict(ALLOCATION[NEUTRAL])
    neutral.detail = (f"{today.detail} | no label held {PERSIST_DAYS} "
                      f"sessions in the last {PERSIST_LOOKBACK} — baseline")
    return neutral, run, today.label


def breadth_from_feed(feed, universe, offset: int = 0
                     ) -> tuple[Optional[float], Optional[float],
                                Optional[float]]:
    """One pass over the cached daily bars -> three participation measures:
       (% above own EMA50, % above own EMA200, share of SECTORS leading).

    A sector "leads" when a majority of its members are above their EMA50.
    Sector participation separates a broad advance from narrow leadership,
    which a raw name-count cannot: 12 tech names above EMA50 out of 68 reads
    as 18% breadth whether or not anything else is participating, but sector
    participation shows 1 of 13. Uses ONLY bars the scanners already pulled —
    no extra API calls.
    """
    above50 = total50 = 0
    above200 = total200 = 0
    per_sector: dict[str, list[int]] = {}
    for t in universe:
        try:
            bars = feed.get_daily_bars(t)
            if bars is None:
                continue
            closes = bars.close[:len(bars.close) - offset] if offset else bars.close
            if len(closes) >= BREADTH_EMA:
                e = ema(closes, BREADTH_EMA)
                if e is not None:
                    total50 += 1
                    up = 1 if closes[-1] > e else 0
                    above50 += up
                    try:
                        from sector_map import sector_of
                        per_sector.setdefault(sector_of(t), []).append(up)
                    except Exception:  # noqa: BLE001
                        pass
            if len(closes) >= EMA_SLOW:
                e2 = ema(closes, EMA_SLOW)
                if e2 is not None:
                    total200 += 1
                    if closes[-1] > e2:
                        above200 += 1
        except Exception:  # noqa: BLE001 — one bad symbol must not break breadth
            continue
    b50 = (above50 / total50) if total50 else None
    b200 = (above200 / total200) if total200 else None
    if per_sector:
        leading = sum(1 for v in per_sector.values()
                      if sum(v) > len(v) / 2.0)
        sect = leading / len(per_sector)
    else:
        sect = None
    return b50, b200, sect


def current(feed, universe=None) -> RegimeState:
    """Classified regime, TTL-cached. Never raises; fails open to WEAK_BULL."""
    global _cache, _last_label
    if MODE == "off":
        st = RegimeState(label=NEUTRAL, detail="REGIME_ALLOC=off")
        st.multipliers = dict(ALLOCATION[NEUTRAL])
        return st
    now = time.time()
    if _cache and now - _cache[0] < TTL:
        return _cache[1]
    try:
        bars = feed.get_daily_bars(SYMBOL)
        if bars is None or len(bars.close) < EMA_SLOW:
            raise ValueError(f"{SYMBOL}: "
                             f"{0 if bars is None else len(bars.close)} bars")
        b50 = b200 = sect = None
        if universe:
            b50, b200, sect = breadth_from_feed(feed, universe)
        st = classify(bars.close, bars.high, bars.low, b50, b200, sect)
        st, _run, _raw = _persisted(feed, universe, st)
        if _raw != st.label:
            log.info("regime persistence: today reads %s (%d/%d sessions) — "
                     "holding %s", _raw, _run, PERSIST_DAYS, st.label)
    except Exception as e:  # noqa: BLE001 — an overlay must never break a cycle
        log.error("regime_allocation: classify failed (%s) — FAILING OPEN to "
                  "%s / all multipliers 1.0, retrying in %.0fs", e, NEUTRAL,
                  FAIL_TTL)
        st = RegimeState(label=NEUTRAL, detail=f"failed open: {e}")
        st.multipliers = dict(ALLOCATION[NEUTRAL])
        st.failed = True

    if st.label != _last_label:
        _last_label = st.label
        parts = []
        if st.breadth_pct is not None:
            parts.append(f"{st.breadth_pct:.0%}>EMA50")
        if st.breadth_200_pct is not None:
            parts.append(f"{st.breadth_200_pct:.0%}>EMA200")
        if st.sector_participation is not None:
            parts.append(f"{st.sector_participation:.0%} sectors leading")
        log.warning("REGIME %s [%s] conf=%d%% trend=%s vol=%s breadth=%s%s "
                    "| %s | multipliers %s", st.label,
                    "LIVE" if MODE == "live" else "SHADOW", st.confidence,
                    st.trend, st.vol, st.breadth,
                    f" ({', '.join(parts)})" if parts else "",
                    st.detail,
                    " ".join(f"{k}={v:.2f}" for k, v in
                             sorted(st.multipliers.items())))
        try:
            import audit
            audit.record("regime_allocation", notify=(MODE == "live"),
                         label=st.label, trend=st.trend, vol=st.vol,
                         breadth=st.breadth,
                         breadth_pct=(round(st.breadth_pct, 3)
                                      if st.breadth_pct is not None else None),
                         breadth_200_pct=(round(st.breadth_200_pct, 3)
                                          if st.breadth_200_pct is not None
                                          else None),
                         sector_participation=(
                             round(st.sector_participation, 3)
                             if st.sector_participation is not None else None),
                         adx=(round(st.adx, 1) if st.adx is not None else None),
                         confidence=st.confidence,
                         mode=MODE, multipliers=st.multipliers)
        except Exception:  # noqa: BLE001 — mirror is best-effort
            pass
    # short-cache failures so a transient gap cannot freeze the regime
    _cache = (now - (TTL - FAIL_TTL) if getattr(st, "failed", False) else now,
              st)
    return st


def apply_to_shares(shares: float, feed, system: str, equity: float,
                    price: float, universe=None) -> tuple[float, float]:
    """Apply the regime multiplier to a share count and RE-CLAMP to the
    notional cap. Returns (shares, multiplier_used).

    Why the clamp exists (2026-07-24, found when preparing to go live):
    position_size() already enforces the 10%-of-equity notional cap, and the
    multiplier was applied AFTERWARDS — so any multiplier above 1.0 silently
    breached a hard risk limit (STRONG_BULL xsectmom 1.40 would have put 14%
    of equity in one position). Leaning IN is a sizing preference; the
    notional cap is a structural limit. The limit wins.
    """
    mult = multiplier(feed, system, universe)
    if mult == 1.0:
        return shares, mult
    out = shares * mult
    if mult > 1.0 and equity and price:
        try:
            from config import max_position_dollars
            cap = max_position_dollars(equity) / price
            if out > cap:
                # Throttled (2026-07-27): this fired 161 times in one session
                # for intraday, because its structure stops are tight enough
                # that risk-based sizing ALWAYS exceeds the 10%-of-equity cap
                # — so a lean-in multiplier is clamped away every time and the
                # regime has no effect on that desk. That is the cap working
                # as intended, not an anomaly, so it is stated once per
                # (system, multiplier) rather than on every signal.
                key = (system, round(mult, 2))
                if _clamp_logged.get(system) != key[1]:
                    _clamp_logged[system] = key[1]
                    log.warning("regime sizing %s: x%.2f exceeds the "
                                "notional cap on every signal (%.2f -> %.2f "
                                "shares, capped at %.2f). The cap binds "
                                "first, so the regime lean-in is INERT for "
                                "this desk until stops widen or the "
                                "multiplier drops below 1.0. Logged once "
                                "per multiplier value.",
                                system, mult, shares, out, cap)
                out = cap
        except Exception as e:  # noqa: BLE001 — never break sizing over a clamp
            log.error("regime notional clamp failed (%s) — using unclamped "
                      "%.2f shares", e, out)
    return out, mult


def multiplier(feed, system: str, universe=None) -> float:
    """The number an engine multiplies its share count by.
    Returns 1.0 in shadow/off mode — shadow LOGS but does not apply."""
    st = current(feed, universe)
    if MODE != "live":
        return 1.0
    return st.multiplier(system)
