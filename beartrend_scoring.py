"""beartrend_scoring.py — short-candidate detection. SHADOW ONLY, by design.

WHAT THIS IS AND IS NOT

This module finds and scores weak stocks in a weak market. It produces
candidates and logs them. It places no orders, and there is no execution path
behind it — deliberately, for two reasons that are worth stating plainly
before anyone wires one up.

1. THE BROKER CANNOT SHORT. brokers.py issues OrderSide.BUY exclusively;
   sell() closes a long. There is no short entry, no borrow check, no
   negative-position accounting, and Position carries no `side` field. Boot
   reconcile would treat a short as an unattributable holding and HALT. This
   is the same blocker that ruled out statistical arbitrage on 2026-07-26,
   and it has not moved.

2. IT WOULD TRADE NOTHING TODAY ANYWAY. The proposed regime table gives
   beartrend 0.00 in STRONG_BULL and WEAK_BULL. The live regime has been
   WEAK_BULL/SIDEWAYS throughout, with SPY ~6% above its 200-day. A complete
   execution layer would sit idle until a bear market arrives — infrastructure
   built for a strategy that cannot be evaluated for months.

So this ships as the research half: scan, score, log. If a bear regime
arrives and the shadow candidates look good, the short-side infrastructure
becomes a justified project with evidence behind it. If they look poor, the
project is avoided entirely. Same order as the pairs research that correctly
talked us out of stat-arb: prove the opportunity before building the machine.

ENTRY LOGIC (mirrors swing_v2's long gates, inverted)
  market      SPY close < EMA50 < EMA200 and EMA50 falling
  trend       stock close < EMA20 < EMA50 < EMA200
  strength    ADX >= 20 (a trend, not drift)
  weakness    stock's N-day return BELOW SPY's (a laggard, not a leader)
  breakdown   close at or below the 20-day low
  volume      >= 1.2x the 20-day average
  NOT extended  RSI > 25 — refuses to short what has already collapsed,
                because that is where violent rebounds live

SCORE 0-100 from five factors, so candidates can be ranked rather than taken
first-come. Weights are a starting point, NOT calibrated — the same caveat
that applies to every other score in this system until calibrate_scores.py
has enough trades to test them.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Optional

from indicators import adx, atr, ema, rsi, trailing_return

log = logging.getLogger("beartrend")

# The safety contract is EXECUTABLE, not documentary. A comment saying
# "shadow only" does not stop someone setting BEARTREND_MODE=live and
# believing it means something.
_ALLOWED_MODES = {"off", "shadow"}
MODE = os.getenv("BEARTREND_MODE", "shadow").strip().lower()
if MODE not in _ALLOWED_MODES:
    raise RuntimeError(
        f"Invalid BEARTREND_MODE={MODE!r}. This desk supports only "
        f"{sorted(_ALLOWED_MODES)} — there is no execution path, because "
        f"brokers.py cannot short.")

EMA_SLOPE_DAYS = int(os.getenv("BEARTREND_EMA_SLOPE_DAYS", "20"))
ADX_MIN = float(os.getenv("BEARTREND_ADX_MIN", "20"))
VOL_MULT = float(os.getenv("BEARTREND_VOL_MULT", "1.2"))
BREAKDOWN_DAYS = int(os.getenv("BEARTREND_BREAKDOWN_DAYS", "20"))
RS_LOOKBACK = int(os.getenv("BEARTREND_RS_LOOKBACK", "63"))
RSI_FLOOR = float(os.getenv("BEARTREND_RSI_FLOOR", "25"))
ATR_STOP_MULT = float(os.getenv("BEARTREND_ATR_STOP", "2.5"))
# NOT a gate. Passing every hard gate already scores >= 65 (stack 25 +
# adx >=10 + breakdown 20 + volume 10 + some RS), so a 60 threshold rejected
# essentially nothing. The score exists to RANK. This threshold only decides
# which observations get a detailed log line.
LOG_SCORE_MIN = float(os.getenv("BEARTREND_LOG_SCORE_MIN", "60"))


@dataclass(frozen=True)
class BearTrendObservation:
    """RESEARCH TELEMETRY — deliberately NOT the executable signal contract.

    Named for what it is. A `BearTrendObservation` invites a future caller to route
    it into the candidate queue; an observation does not. The type boundary
    is the control, and it is stronger than a comment or a boolean flag.
    Frozen so nothing downstream can mutate it into something order-shaped.
    """
    ticker: str
    price: float
    stop: float                 # ABOVE entry — shorts are inverted
    risk_per_share: float
    score: float
    adx: float = 0.0
    rel_strength: float = 0.0
    reasons: tuple = field(default_factory=tuple)

    def line(self) -> str:
        return (f"{self.ticker} @ {self.price:.2f} stop {self.stop:.2f} "
                f"(risk {self.risk_per_share:.2f}/sh) score {self.score:.0f} "
                f"adx {self.adx:.0f} rs {self.rel_strength:+.1%} | "
                + ",".join(self.reasons))


def market_permits_shorts(spy_close: list[float]) -> tuple[bool, str]:
    """SPY must be in a confirmed downtrend. Deliberately strict: shorting
    into a market that is merely pausing is how squeezes happen."""
    # FAIL CLOSED (2026-08-03). The slope test used to be skipped entirely
    # when history was between 200 and 220 bars — so the gate could return
    # True having never checked whether EMA50 was falling, which is half the
    # documented condition. Require the history the test needs.
    required = 200 + EMA_SLOPE_DAYS
    if len(spy_close) < required:
        return False, f"insufficient_spy_history({len(spy_close)}<{required})"
    e50, e200 = ema(spy_close, 50), ema(spy_close, 200)
    prior_e50 = ema(spy_close[:-EMA_SLOPE_DAYS], 50)
    if e50 is None or e200 is None or prior_e50 is None:
        return False, "spy_indicator_nan"
    px = spy_close[-1]
    if not (px < e50 < e200):
        return False, (f"spy_not_bearish(px {px:.2f} e50 {e50:.2f} "
                       f"e200 {e200:.2f})")
    if e50 >= prior_e50:
        return False, (f"spy_ema50_not_falling(now {e50:.2f} "
                       f"prior {prior_e50:.2f})")
    return True, "ok"


def validate_series(closes, highs, lows, volumes) -> Optional[str]:
    """Reject misaligned or short input BEFORE any gate can silently skip.

    Several gates used `if len(x) > N:` guards, which meant a truncated array
    made the gate vanish rather than fail. A candidate could then satisfy
    "the documented strategy" without ever having been tested against the
    breakdown or volume condition.
    """
    lengths = {"close": len(closes), "high": len(highs),
               "low": len(lows), "volume": len(volumes)}
    if len(set(lengths.values())) != 1:
        return "unaligned_bars(" + ",".join(
            f"{k}={v}" for k, v in lengths.items()) + ")"
    required = max(200, BREAKDOWN_DAYS + 1, RS_LOOKBACK + 1, 21)
    if len(closes) < required:
        return f"insufficient_history({len(closes)}<{required})"
    for name, series in (("close", closes), ("high", highs),
                         ("low", lows), ("volume", volumes)):
        if any(v is None for v in series):
            return f"missing_values({name})"
    return None


def score_candidate(closes: list[float], highs: list[float],
                    lows: list[float], volumes: list[float],
                    bench_closes: Optional[list[float]]) -> tuple[float, list]:
    """0-100 with the factors that earned it. Higher = weaker stock."""
    pts, why = 0.0, []
    e20, e50, e200 = ema(closes, 20), ema(closes, 50), ema(closes, 200)
    px = closes[-1]

    # 25 — depth of trend alignment
    if e20 and e50 and e200 and px < e20 < e50 < e200:
        pts += 25; why.append("full_bearish_stack")
    elif e50 and e200 and e50 < e200:
        pts += 12; why.append("partial_stack")

    # 25 — relative weakness vs the index
    if bench_closes and len(bench_closes) > RS_LOOKBACK and \
            len(closes) > RS_LOOKBACK:
        sr = trailing_return(closes, RS_LOOKBACK)
        br = trailing_return(bench_closes, RS_LOOKBACK)
        # Unavailable != zero. Awarding no points is the same OUTCOME here,
        # but writing it explicitly keeps the rule uniform across the module.
        gap = (sr - br) if (sr is not None and br is not None) else 0.0
        if gap < 0:
            pts += min(25.0, 25.0 * min(1.0, abs(gap) / 0.20))
            why.append(f"rs{gap:+.0%}")

    # 20 — trend strength
    a = adx(highs, lows, closes, 14)
    if a is not None:
        pts += min(20.0, 20.0 * min(1.0, a / 40.0))
        if a >= ADX_MIN:
            why.append(f"adx{a:.0f}")

    # 20 — breakdown to new lows
    if len(lows) > BREAKDOWN_DAYS:
        low_n = min(lows[-BREAKDOWN_DAYS - 1:-1])
        if px <= low_n:
            pts += 20; why.append(f"{BREAKDOWN_DAYS}d_low")
        elif px <= low_n * 1.02:
            pts += 10; why.append("near_low")

    # 10 — volume confirmation
    if len(volumes) > 21:
        av = sum(volumes[-21:-1]) / 20
        if av and volumes[-1] >= VOL_MULT * av:
            pts += 10; why.append(f"vol{volumes[-1]/av:.1f}x")

    return min(100.0, pts), why


def detect(ticker: str, closes: list[float], highs: list[float],
           lows: list[float], volumes: list[float], atr14: Optional[float],
           bench_closes: Optional[list[float]]
           ) -> tuple[Optional[BearTrendObservation], str]:
    """(observation, kill_reason).

    EVERY documented gate FAILS CLOSED (2026-08-03). Previously ADX, relative
    strength, breakdown and volume all became optional when their input was
    missing or short — so the shadow record could contain candidates that
    never satisfied the strategy being researched, and the research would
    have evaluated a looser strategy than the documented one.
    """
    bad = validate_series(closes, highs, lows, volumes)
    if bad:
        return None, bad
    if not bench_closes:
        return None, "no_benchmark"
    if len(bench_closes) <= RS_LOOKBACK:
        return None, f"insufficient_benchmark({len(bench_closes)}<={RS_LOOKBACK})"

    e20, e50, e200 = ema(closes, 20), ema(closes, 50), ema(closes, 200)
    if e20 is None or e50 is None or e200 is None:
        return None, "indicator_nan"
    px = closes[-1]
    if not (px < e20 < e50 < e200):
        return None, "trend_filter"

    a = adx(highs, lows, closes, 14)
    if a is None:
        return None, "adx_nan"
    if a < ADX_MIN:
        return None, f"adx_weak({a:.1f}<{ADX_MIN:.1f})"

    # Refuse what has already collapsed: oversold names are where violent
    # rebounds live, and a short squeeze is the one loss with no ceiling.
    # `<=` because the documented rule is RSI > floor, so equality rejects.
    r = rsi(closes, 14)
    if r is None:
        return None, "rsi_nan"
    if r <= RSI_FLOOR:
        return None, f"too_extended(rsi {r:.1f}<={RSI_FLOOR:.1f})"

    # `or 0.0` is banned here: a genuine 0.0% return and an unavailable
    # calculation are different facts and must not collapse into one.
    sr = trailing_return(closes, RS_LOOKBACK)
    br = trailing_return(bench_closes, RS_LOOKBACK)
    if sr is None or br is None:
        return None, "relative_strength_nan"
    if sr >= br:
        return None, f"not_weak({sr:+.1%}>={br:+.1%})"

    prior_low = min(lows[-BREAKDOWN_DAYS - 1:-1])
    if px > prior_low:
        return None, f"no_breakdown({px:.2f}>{prior_low:.2f})"

    avg_vol = sum(volumes[-21:-1]) / 20
    if avg_vol <= 0:
        return None, "invalid_average_volume"
    vr = volumes[-1] / avg_vol
    if vr < VOL_MULT:
        return None, f"volume({vr:.2f}x<{VOL_MULT:.2f}x)"

    if atr14 is None or atr14 <= 0:
        return None, "no_atr"

    # The stop sits ABOVE entry. Everything downstream assumes long, which is
    # precisely why this terminates at telemetry.
    stop = px + ATR_STOP_MULT * atr14
    risk = stop - px
    if risk <= 0:
        return None, "invalid_short_risk"

    score, why = score_candidate(closes, highs, lows, volumes, bench_closes)
    return BearTrendObservation(
        ticker=ticker, price=px, stop=stop, risk_per_share=risk,
        score=score, adx=a, rel_strength=sr - br, reasons=tuple(why)), "ok"


# Research artifacts live under /data/research/beartrend/ rather than beside
# live trading state — a namespace separation, so nothing in the research
# chain can be mistaken for something the bot acts on. The legacy flat path
# is still honoured if it already exists, so no history is stranded.
_RESEARCH_DIR = os.getenv("BEARTREND_RESEARCH_DIR", "/data/research/beartrend")
_LEGACY_OBS = "/data/beartrend_observations.jsonl"
OBS_PATH = os.getenv(
    "BEARTREND_OBS_PATH",
    _LEGACY_OBS if os.path.exists(_LEGACY_OBS)
    else os.path.join(_RESEARCH_DIR, "beartrend_observations.jsonl"))

# RESEARCH VERSION — bump this whenever a GATE changes (thresholds, added or
# removed conditions). Six months of observations are worthless if nobody can
# tell which rules produced which rows. Stamped on every record so v1 and v2
# can be compared instead of silently blended.
RESEARCH_VERSION = os.getenv("BEARTREND_VERSION", "v1")


def _rule_fingerprint() -> str:
    """The gate values in force, so a version bump that forgets to change
    RESEARCH_VERSION is still detectable after the fact."""
    return (f"adx>={ADX_MIN:g},rsi>{RSI_FLOOR:g},vol>={VOL_MULT:g}x,"
            f"brk={BREAKDOWN_DAYS}d,rs={RS_LOOKBACK}d,"
            f"atrstop={ATR_STOP_MULT:g}")


def record_observations(obs: list, spy_px: float, spy_e50: Optional[float],
                        spy_e200: Optional[float], spy_slope: float,
                        cio_regime: Optional[str],
                        breadth: Optional[float] = None) -> int:
    """Append today's observations to the shadow repository on /data.

    This is the whole point of the desk right now. An observation is only
    research if it can be scored LATER against what the price actually did —
    a log line that scrolls away proves nothing. One row per observation per
    scan day; beartrend_review.py walks prices forward and computes
    expectancy, MFE/MAE and score calibration from it.

    Appends rather than rewrites, so a redeploy cannot truncate history, and
    /data persists as of 2026-07-28. Fails open: losing a day of research
    telemetry must never affect trading.
    """
    if not obs:
        return 0
    import json
    from datetime import datetime, timezone
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = 0
    try:
        os.makedirs(os.path.dirname(OBS_PATH) or ".", exist_ok=True)
        with open(OBS_PATH, "a", encoding="utf-8") as fh:
            for c in obs:
                fh.write(json.dumps({
                    "scan_date": day, "ticker": c.ticker,
                    "price": round(c.price, 4), "stop": round(c.stop, 4),
                    "risk_per_share": round(c.risk_per_share, 4),
                    "score": round(c.score, 1), "adx": round(c.adx, 1),
                    "rel_strength": round(c.rel_strength, 4),
                    "reasons": list(c.reasons),
                    "spy_price": round(spy_px, 4),
                    "spy_ema50": round(spy_e50, 4) if spy_e50 else None,
                    "spy_ema200": round(spy_e200, 4) if spy_e200 else None,
                    "spy_ema50_slope": round(spy_slope, 4),
                    "cio_regime": cio_regime,
                    "side": "short",
                    "version": RESEARCH_VERSION,
                    "rules": _rule_fingerprint(),
                    # Store the ACTUAL ATR14, not something the reviewer has
                    # to reconstruct as risk/ATR_STOP_MULT. That inference is
                    # circular — it can only ever return the multiple that
                    # was used to build the stop, so it could never reveal a
                    # mis-set stop multiple.
                    "atr14": round(c.risk_per_share / ATR_STOP_MULT, 6),
                    "atr_stop_mult": ATR_STOP_MULT,
                    "breadth": (round(breadth, 4)
                                if breadth is not None else None),
                }) + "\n")
                rows += 1
    except Exception as e:  # noqa: BLE001 — telemetry must never break a cycle
        log.error("beartrend: cannot write %s (%s) — observations lost for "
                  "today, trading unaffected", OBS_PATH, e)
        return 0
    return rows


REFRESH_SECONDS = int(os.getenv("BEARTREND_REFRESH_SECONDS", "900"))
_last_scan = 0.0


def due() -> bool:
    """Own the cadence here rather than depending on a caller-side helper.

    The gates are daily; re-scanning 68 names every cycle would add load and
    no information. 15 minutes matches swing_v2, so both research desks tick
    on the same rhythm.
    """
    import time
    global _last_scan
    if time.time() - _last_scan < REFRESH_SECONDS:
        return False
    _last_scan = time.time()
    return True


def scan(universe, bars_for, bench_closes, health_record=None,
         cio_regime: Optional[str] = None,
         breadth: Optional[float] = None) -> list:
    """Scan, rank, log. Returns OBSERVATIONS — research telemetry only.

    cio_regime: the CIO layer's current label, recorded alongside this
    engine's own tactical read. The two are deliberately separate facts: a
    future execution path would require BOTH (CIO says BEAR *and* the
    tactical gate agrees), and logging both now lets us measure whether the
    CIO's 3-session persistence delay gives away too much of a bearish move.
    """
    if MODE == "off":
        # An inactive desk is HEALTHY, not missing. Report it either way, or
        # the monitor cannot tell "switched off" from "crashed".
        if health_record:
            health_record("beartrend", True, "off")
        return []

    ok, why = market_permits_shorts(bench_closes or [])
    if not ok:
        log.info("BEARTREND inactive: %s | cio_regime=%s", why,
                 cio_regime or "?")
        if health_record:
            health_record("beartrend", True, f"inactive:{why}")
        return []

    spy_px = bench_closes[-1]
    spy_e50, spy_e200 = ema(bench_closes, 50), ema(bench_closes, 200)
    spy_prior = ema(bench_closes[:-EMA_SLOPE_DAYS], 50)

    kills, out = {}, []
    for t in universe:
        try:
            b = bars_for(t)
            if b is None:
                kills["no_bars"] = kills.get("no_bars", 0) + 1
                continue
            obs, reason = detect(t, b.close, b.high, b.low, b.volume,
                                 atr(b, 14), bench_closes)
            if obs:
                out.append(obs)
            else:
                kills[reason.split("(")[0]] = \
                    kills.get(reason.split("(")[0], 0) + 1
        except Exception as e:  # noqa: BLE001 — one bad symbol must not stop the scan
            kills["error"] = kills.get("error", 0) + 1
            log.debug("beartrend %s: %s", t, e)

    out.sort(key=lambda c: -c.score)
    # "OBSERVATION", not "SHADOW_SHORT": the latter reads like a simulated
    # order in an operational log. This desk produces research, not fills.
    log.warning("BEARTREND OBSERVATIONS universe=%d qualified=%d | "
                "spy %.2f e50 %.2f e200 %.2f slope %+.2f | cio_regime=%s "
                "tactical_bear=True | NO EXECUTION PATH | kills: %s",
                len(universe), len(out), spy_px, spy_e50 or 0.0,
                spy_e200 or 0.0,
                ((spy_e50 or 0.0) - (spy_prior or 0.0)),
                cio_regime or "?",
                " ".join(f"{k}={v}" for k, v in sorted(kills.items())) or "none")
    for c in out[:5]:
        if c.score >= LOG_SCORE_MIN:
            log.warning("BEARTREND OBSERVATION %s", c.line())
    written = record_observations(out, spy_px, spy_e50, spy_e200,
                                  (spy_e50 or 0.0) - (spy_prior or 0.0),
                                  cio_regime, breadth)
    if written:
        log.warning("BEARTREND: %d observation(s) recorded to %s for later "
                    "forward-return scoring", written, OBS_PATH)
    if health_record:
        health_record("beartrend", True, f"{len(out)} observations")
    return out
