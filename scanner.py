"""Price-action scanner: turns candles into signals. No social/insider/congress.

Two scans over the universe:

  swing (TREND)     - on DAILY bars: close breaks above the prior N-day high,
                      price above the trend SMA, with a volume expansion.
  intraday (MOMENTUM) - on INTRADAY (1-min) bars: relative-volume spike, price
                      above VWAP, and a break above the opening range high.

The scanner only *flags* candidates. The engines apply liquidity, sizing, stop,
and the final confirmation before anything is bought. Each signal carries a
`reason` string so the funnel is auditable.

2026-07-09: ScanFunnel wired through every scan. Each pass now emits a
rate-limited attrition line, e.g.
    funnel[swing]: universe=63 bars_ok=61 breakout=3 uptrend=2 vol_confirm=0 -> signals=0
so "zero signals" is diagnosable at a glance: healthy selectivity shows
attrition through the gates; data starvation dies at bars_ok. The combined
boolean checks became sequential continues so each gate is countable — the
truth table is unchanged.
"""

from __future__ import annotations

import logging
from typing import Optional

from config import INTRADAY, MEANREV, SWING
from indicators import (
    avg_dollar_volume,
    opening_range_high,
    prior_high,
    relative_volume,
    rsi,
    sma,
    vwap,
)
from models import Bars, Signal, SignalSource
from scan_health import ScanFunnel

import meanrev_scoring as mrs
import regime

log = logging.getLogger("scanner")

# Scorecard log throttle (2026-07-22, first night in production): a sticky
# RSI trigger (IONQ) printed an identical card every cycle — ~600 lines/hr
# at session cadence. Log a ticker's card only when its CONTENT changes;
# zero information lost, repeats suppressed.
_last_cards: dict[str, str] = {}


class PriceActionScanner:
    def __init__(self, feed, universe: list[str], intraday_universe: list[str] | None = None) -> None:
        self._feed = feed
        self._universe = universe                                # daily strategies: full breadth
        self._intraday_universe = intraday_universe or universe  # per-cycle cost: liquid subset
        self._funnels = {s: ScanFunnel(s) for s in ("swing", "intraday", "meanrev")}

    # ----- swing: daily breakout + trend -----
    def scan_swing(self) -> list[Signal]:
        out: list[Signal] = []
        f = self._funnels["swing"]
        f.start_pass(len(self._universe))
        for t in self._universe:
            bars = self._feed.get_daily_bars(t)
            if bars is None or len(bars.close) < SWING.trend_sma_days + 1:
                continue
            close = bars.close[-1]
            hi = prior_high(bars, SWING.breakout_lookback)
            trend = sma(bars.close, SWING.trend_sma_days)
            rv = relative_volume(bars)
            if hi is None or trend is None or rv is None:
                continue
            f.count("bars_ok")
            if not close > hi:
                continue
            f.count("breakout")
            if SWING.require_uptrend and not close > trend:
                continue
            f.count("uptrend")
            if not rv >= SWING.vol_spike_mult:
                continue
            f.count("vol_confirm")
            out.append(Signal(SignalSource.TREND, t,
                              reason=f"close>{hi:.2f} 20d-high, >SMA{SWING.trend_sma_days}, rv={rv:.2f}"))
        f.finish(len(out))
        return out

    # ----- intraday: opening-range / momentum breakout -----
    def scan_intraday(self) -> list[Signal]:
        out: list[Signal] = []
        f = self._funnels["intraday"]
        f.start_pass(len(self._intraday_universe))
        for t in self._intraday_universe:
            bars = self._feed.get_intraday_bars(t)
            if bars is None or len(bars.close) < INTRADAY.opening_range_min + 1:
                continue
            close = bars.close[-1]
            orh = opening_range_high(bars, INTRADAY.opening_range_min)
            vw = vwap(bars)
            rv = relative_volume(bars)
            if orh is None or rv is None:
                continue
            f.count("bars_ok")
            if not close > orh:
                continue
            f.count("orb_break")
            if INTRADAY.require_above_vwap and not (vw is not None and close > vw):
                continue
            f.count("above_vwap")
            if not rv >= INTRADAY.min_rel_volume:
                continue
            f.count("vol_confirm")
            out.append(Signal(SignalSource.MOMENTUM, t,
                              reason=f"close>{orh:.2f} ORH, >VWAP, rv={rv:.2f}"))
        f.finish(len(out))
        return out

    # ----- mean reversion: RSI oversold within an uptrend (contrarian) -----
    # 2026-07-22: multi-factor SCORING added (meanrev_scoring.py) behind
    # MEANREV_SCORING = off | shadow (default) | live.
    #   shadow: signals from the CURRENT rule, unchanged; every trigger also
    #           gets a scorecard logged, so score distributions accumulate
    #           with zero behavior change.
    #   live:   signals require hard gates (RSI trigger + close>EMA200 +
    #           regime risk-on) AND score >= MEANREV_SCORE_MIN of 6.
    def scan_meanrev(self) -> list[Signal]:
        out: list[Signal] = []
        f = self._funnels["meanrev"]
        f.start_pass(len(self._universe))
        scoring = mrs.SCORING_MODE
        spy_close = None
        if scoring in ("shadow", "live"):
            spy = self._feed.get_daily_bars("SPY")   # slow-TTL cached; 1 call
            spy_close = spy.close if spy is not None else None
            market_ok = regime.risk_on(self._feed)
        for t in self._universe:
            bars = self._feed.get_daily_bars(t)
            if bars is None or len(bars.close) < MEANREV.trend_sma_days + 1:
                continue
            r = rsi(bars.close, MEANREV.rsi_period)
            trend = sma(bars.close, MEANREV.trend_sma_days)
            if r is None or trend is None:
                continue
            f.count("bars_ok")
            close = bars.close[-1]

            card = None
            if scoring in ("shadow", "live") and r < MEANREV.rsi_oversold:
                # Score only triggered names: the trigger is the reason to look.
                card = mrs.score_candidate(t, bars.close, bars.high, bars.low,
                                           bars.volume, r,
                                           MEANREV.rsi_oversold, spy_close)
                if card:
                    card_str = ("score=%d/6 trigger=%s trend_gate=%s "
                                "market=%s | %s" % (card.score, card.trigger,
                                card.gate_trend, market_ok,
                                " ".join(k for k, v in card.factors.items()
                                         if v) or "(no factors)"))
                    if _last_cards.get(t) != card_str:
                        _last_cards[t] = card_str
                        log.info("meanrev_score %s: %s", t, card_str)

            if scoring == "live":
                if card and card.qualifies() and market_ok:
                    f.count("uptrend"); f.count("oversold"); f.count("scored")
                    out.append(Signal(SignalSource.MEAN_REVERSION, t,
                                      reason=f"RSI={r:.1f} score={card.score}/6 "
                                             f">EMA200"))
                continue

            # ---- current rule (off + shadow modes): unchanged behavior ----
            # Buy oversold, but only in a longer-term uptrend (avoid falling knives).
            if not close > trend:
                continue
            f.count("uptrend")
            if not r < MEANREV.rsi_oversold:
                continue
            f.count("oversold")
            if card is not None and not card.qualifies():
                log.info("meanrev_score %s: CURRENT rule signals but scored "
                         "rule would REJECT (score=%d/6 < %d) — divergence "
                         "logged for the A/B", t, card.score, mrs.SCORE_MIN)
            out.append(Signal(SignalSource.MEAN_REVERSION, t,
                              reason=f"RSI={r:.1f}<{MEANREV.rsi_oversold:.0f}, >SMA{MEANREV.trend_sma_days}"))
        f.finish(len(out))
        return out

    def scan_all(self) -> list[Signal]:
        return self.scan_swing() + self.scan_intraday() + self.scan_meanrev()
