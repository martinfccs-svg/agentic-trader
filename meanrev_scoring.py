"""meanrev_scoring.py — multi-factor scored entries + exit ladder for meanrev.

Operator spec (2026-07-22), adjusted for safety:

  HARD GATES (all required — vetoes, never outvotable by score):
    T. RSI(14) < oversold          the trigger (config MR_RSI_OVERSOLD)
    G1. close > EMA200             trend gate (spec upgrades SMA200 -> EMA200)
    G2. market filter              reuse regime.risk_on() — checked by the
                                   CALLER (scanner/engine), not duplicated here

  SCORED CONFIRMATIONS (1 point each, 6 max; buy needs >= MEANREV_SCORE_MIN):
    S1. close below lower Bollinger Band (20, 2.0)
    S2. EMA50 > EMA200             (golden-cross regime)
    S3. ADX(14) < 20               (ranging market — reversion's habitat)
    S4. volume drying              (vol SMA5 < 0.8 x vol SMA20 — exhaustion)
    S5. ATR contraction            (ATR7 < ATR21 — panic subsiding)
    S6. relative strength          (63d return > SPY 63d return — quality dip)

  EXIT LADDER (live mode only; shadow leaves current exits untouched):
    L1. profit >= +1R      -> stop to breakeven
    L2. after L1           -> ATR trailing stop: max(stop, hw - 2.0 x ATR)
    [partial profits DEFERRED: brokers.sell() closes whole positions only;
     a partial-exit path (cancel legs, sell half, re-place stop) is a
     brokers.py project — do not bolt on. Recorded in the spec so it isn't
     forgotten, absent so it can't half-work.]
    L3. time stop          -> MEANREV_TIME_STOP_DAYS (already live, kept)
    L4. trend reversal     -> close < EMA200 -> exit all
    L5. final exit         -> RSI >= MR_RSI_EXIT (mean reached)

Pure functions over close/high/low/volume lists — no feed, no broker, no
side effects — so the SAME code runs in the live scanner (shadow logging),
the live engine (when enabled), and the backtest. Bars here need no opens,
which is why this CAN live on the Finnhub pipe, unlike swing_v2.

Env:
  MEANREV_SCORING     off | shadow (default) | live
  MEANREV_SCORE_MIN   default 4 (of 6)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional
# ema and adx are CANONICAL in indicators.py (2026-08-02). Re-exported
# here so `from meanrev_scoring import adx` keeps working for
# regime_allocation, swing_v2 and the backtests.
from indicators import ema, adx  # noqa: F401
import exit_rules

SCORING_MODE = os.getenv("MEANREV_SCORING", "shadow").strip().lower()

MAX_SCORE = 6   # six scored confirmations exist; gates are vetoes, not points
_raw_min = int(os.getenv("MEANREV_SCORE_MIN", "4"))
SCORE_MIN = min(_raw_min, MAX_SCORE)
if _raw_min > MAX_SCORE:
    import logging as _lg
    _lg.getLogger("meanrev_scoring").critical(
        "MEANREV_SCORE_MIN=%d is IMPOSSIBLE (max score is %d) — a threshold "
        "above the maximum makes the scored strategy silently dead forever, "
        "the exact Jul-8 failure class. Clamped to %d.",
        _raw_min, MAX_SCORE, MAX_SCORE)

# All tunables env-overridable (2026-07-22, operator request) — parameters
# live HERE and in env, never in config.py, so tuning the scored strategy
# can never break the live engine's config contract.
BB_PERIOD = int(os.getenv("MEANREV_BB_PERIOD", "20"))
BB_K = float(os.getenv("MEANREV_BB_K", "2.0"))
ADX_PERIOD = int(os.getenv("MEANREV_ADX_PERIOD", "14"))
ADX_RANGING = float(os.getenv("MEANREV_ADX_MAX", "20"))
VOL_FAST, VOL_SLOW = 5, 20
VOL_DRY_RATIO = float(os.getenv("MEANREV_VOL_DRY_RATIO", "0.8"))
ATR_FAST, ATR_SLOW = 7, 21
RS_LOOKBACK = int(os.getenv("MEANREV_RS_LOOKBACK", "63"))
EMA_FAST, EMA_SLOW = 50, 200
TRAIL_ATR_MULT = float(os.getenv("MEANREV_TRAIL_ATR", "2.0"))
# Emergency volatility exit (2026-07-24): if realised volatility expands far
# beyond what the position was sized for, the environment that justified the
# trade is gone. Entry ATR is DERIVED, not stored — (entry - entry_stop) /
# atr_stop_multiple — because per-position attributes do not survive a
# redeploy and this bot redeploys several times a week. 0 disables.
VOL_EXIT_MULT = float(os.getenv("MEANREV_VOL_EXIT_MULT", "1.8"))


# ------------------------------------------------------------- indicators
def bollinger_lower(values: list[float], n: int = BB_PERIOD,
                    k: float = BB_K) -> Optional[float]:
    if len(values) < n:
        return None
    window = values[-n:]
    mid = sum(window) / n
    var = sum((v - mid) ** 2 for v in window) / n
    return mid - k * (var ** 0.5)


def _atr_window(high, low, close, n) -> Optional[float]:
    if len(close) < n + 1:
        return None
    trs = []
    for i in range(len(close) - n, len(close)):
        trs.append(max(high[i] - low[i], abs(high[i] - close[i - 1]),
                       abs(low[i] - close[i - 1])))
    return sum(trs) / n


# --------------------------------------------------------------- scoring
@dataclass
class ScoreCard:
    ticker: str
    trigger: bool            # RSI < oversold (the reason we're even looking)
    gate_trend: bool         # close > EMA200
    score: int               # 0..6 confirmations
    factors: dict            # name -> bool (for logs/backtest attribution)
    ema200: Optional[float]  # reused by the exit ladder (trend reversal)
    # Reversal confirmation, kept OUT of `score` so the 0-6 scale and the
    # tuned MEANREV_SCORE_MIN keep their meaning.
    reversal_ok: bool = False
    reversal_why: str = ""

    def qualifies(self, score_min: int = SCORE_MIN) -> bool:
        # Market gate (regime) is applied by the CALLER on top of this.
        return self.trigger and self.gate_trend and self.score >= score_min


REVERSAL_MODE = os.getenv("MEANREV_REVERSAL", "shadow").strip().lower()


def reversal_confirmed(close: list[float], high: list[float],
                       low: list[float]) -> tuple[bool, str]:
    """Has the fall actually STOPPED? (confirmed, reason)

    The gap this fills: all six existing factors describe the CONDITION —
    below the lower Bollinger, ADX ranging, volume drying, ATR contracting,
    EMA50 > EMA200, relative strength. Every one can be true while the stock
    is still making new lows every session. So the desk can buy something
    oversold that is becoming more oversold, which is the classic failure of
    an RSI trigger used as an entry rather than as a screen.

    Three checks, ALL required, deliberately cheap and non-parametric:

        1. today's low is NOT the lowest of the last 5 sessions
           — the sequence of lower lows has broken
        2. today closed in the upper half of its own range
           — buyers were present into the close, not just present
        3. today's close is above yesterday's close
           — the most basic possible statement that it turned

    None of these predicts a reversal. Together they say the knife has
    landed, which is a different and much weaker claim — and the right one
    for a rule whose job is to stop buying mid-fall.

    Ships in SHADOW: logged on every candidate, blocking nothing, so the
    cost of requiring it can be counted before it is required.
    """
    if len(close) < 6 or len(low) < 6 or len(high) < 6:
        return False, "insufficient_bars"
    reasons = []
    if low[-1] <= min(low[-5:]):
        reasons.append("still_making_new_lows")
    rng = high[-1] - low[-1]
    if rng <= 0 or (close[-1] - low[-1]) / rng < 0.5:
        reasons.append("closed_in_lower_half")
    if close[-1] <= close[-2]:
        reasons.append("close_not_up")
    if reasons:
        return False, "+".join(reasons)
    return True, "reversal_confirmed"


def score_candidate(ticker: str, close: list[float], high: list[float],
                    low: list[float], volume: list[float],
                    rsi_value: Optional[float], rsi_oversold: float,
                    spy_close: Optional[list[float]]) -> Optional[ScoreCard]:
    """Compute the card, or None if history is insufficient for the slowest
    factor (EMA200 needs 200 bars; the 500-day feed floor covers this)."""
    if len(close) < EMA_SLOW:
        return None
    e200 = ema(close, EMA_SLOW)
    e50 = ema(close, EMA_FAST)
    px = close[-1]

    factors = {}
    bb_low = bollinger_lower(close)
    factors["below_lower_bb"] = bb_low is not None and px < bb_low
    factors["ema50_gt_ema200"] = (e50 is not None and e200 is not None
                                  and e50 > e200)
    a = adx(high, low, close)
    factors["adx_ranging"] = a is not None and a < ADX_RANGING
    if len(volume) >= VOL_SLOW:
        vf = sum(volume[-VOL_FAST:]) / VOL_FAST
        vs = sum(volume[-VOL_SLOW:]) / VOL_SLOW
        factors["volume_drying"] = vs > 0 and vf < VOL_DRY_RATIO * vs
    else:
        factors["volume_drying"] = False
    af = _atr_window(high, low, close, ATR_FAST)
    aslow = _atr_window(high, low, close, ATR_SLOW)
    factors["atr_contraction"] = (af is not None and aslow is not None
                                  and af < aslow)
    if spy_close and len(spy_close) > RS_LOOKBACK and len(close) > RS_LOOKBACK:
        t_ret = close[-1] / close[-1 - RS_LOOKBACK] - 1
        s_ret = spy_close[-1] / spy_close[-1 - RS_LOOKBACK] - 1
        factors["rel_strength"] = t_ret > s_ret
    else:
        factors["rel_strength"] = False

    # Recorded SEPARATELY from `factors`, deliberately: adding a 7th factor
    # would change the 0-6 score scale and silently invalidate
    # MEANREV_SCORE_MIN=4, which was chosen against the current six.
    rev_ok, rev_why = reversal_confirmed(close, high, low)
    return ScoreCard(
        ticker=ticker,
        trigger=(rsi_value is not None and rsi_value < rsi_oversold),
        gate_trend=(e200 is not None and px > e200),
        score=sum(1 for v in factors.values() if v),
        factors=factors,
        ema200=e200,
        reversal_ok=rev_ok,
        reversal_why=rev_why,
    )


def risk_multiplier(score: int, score_min: int = SCORE_MIN) -> float:
    """Conviction sizing: full risk only for the strongest cards.
    6/6 -> 1.00, one above threshold -> 0.75, at threshold -> 0.50.
    Applied ONLY when MEANREV_SCORING=live; shadow sizing is unchanged."""
    if score >= MAX_SCORE:
        return 1.0
    if score >= score_min + 1:
        return 0.75
    return 0.5


# ------------------------------------------------------------ exit ladder
def ladder_decision(price: float, entry: float, entry_stop: float,
                    stop: float, high_water: float, atr14: Optional[float],
                    ema200: Optional[float], last_close: float,
                    rsi_value: Optional[float], rsi_exit: float,
                    held_days: int, time_stop_days: int,
                    atr_stop_multiple: Optional[float] = None
                    ) -> tuple[float, Optional[str]]:
    """One management pass. Returns (new_stop, exit_reason|None).

    Priority (first match wins):
        stop touch > volatility emergency > final RSI > trend reversal > time
    Stop only ever ratchets UP (breakeven, then ATR trail) — never widens.

    Derived state, nothing stored on the Position (redeploy-proof):
        initial risk  = entry - entry_stop
        breakeven hit = implied by the ratchet below
        entry ATR     = initial risk / atr_stop_multiple
    """
    # Arithmetic from exit_rules (2026-08-02); the LADDER ORDER below is this
    # desk's policy and stays here. Note the time stop further down uses
    # stale_thesis_stop, NOT time_stop: a mean-reversion trade that has not
    # reverted is stale whether or not it is green, and exit_rules.time_stop
    # would refuse to exit a winner.
    r = entry - entry_stop
    new_stop = stop
    if r > 0 and price >= entry + r:                       # L1: >= +1R
        new_stop = max(new_stop, entry)                    # breakeven
        if atr14 is not None:                              # L2: ATR trail
            new_stop = exit_rules.ratchet_stop(
                new_stop, high_water, atr14, TRAIL_ATR_MULT)
    hit = exit_rules.stop_hit(price, new_stop)
    if hit:
        return new_stop, "stop"
    # L2b: emergency volatility exit — the trade was sized for entry-ATR; if
    # current ATR has expanded past VOL_EXIT_MULT x that, the regime the
    # thesis assumed no longer exists. Ranked directly under the hard stop.
    if (VOL_EXIT_MULT and atr14 is not None and atr_stop_multiple
            and atr_stop_multiple > 0 and r > 0):
        entry_atr = r / atr_stop_multiple
        if entry_atr > 0 and atr14 > VOL_EXIT_MULT * entry_atr:
            return new_stop, (f"volatility_expansion(atr {atr14:.2f} > "
                              f"{VOL_EXIT_MULT:.1f}x entry {entry_atr:.2f})")
    if rsi_value is not None and rsi_value >= rsi_exit:    # L5: final exit
        return new_stop, f"rsi_reverted({rsi_value:.1f}>={rsi_exit:.0f})"
    if exit_rules.trend_exit(last_close, ema200):          # L4: trend rev.
        return new_stop, "trend_reversal(close<EMA200)"
    stale = exit_rules.stale_thesis_stop(held_days, time_stop_days or 0)
    if stale:                                              # L3: time
        return new_stop, stale
    return new_stop, None
