"""scan_health.py — observability and cadence guards for the signal pipeline.

Built after 2026-07-08 PM: three strategies at zero signals with no way to
tell patient from broken. Three tools, one module:

1. check_bar_depth(feed, universe, enabled)
   Boot-time guard. meanrev needs a 200-day SMA and xsectmom a 126-day
   momentum window; if the daily-candle fetch returns fewer bars, those
   strategies are STRUCTURALLY DEAD and will log nothing forever. This
   samples the universe at startup, compares history depth to each enabled
   strategy's lookback, and screams (log + audit/ntfy) if starved.

2. ScanFunnel
   Per-strategy gate-by-gate counters, emitted as a single rate-limited
   line: funnel[swing]: universe=63 bars_ok=61 uptrend=14 breakout=2
   vol_confirm=0 -> signals=0. Makes "zero signals" diagnosable at a
   glance: a healthy-but-selective scan shows attrition through gates;
   a starved one dies at bars_ok.

3. DailyRebalanceGate
   Wall-clock once-per-trading-day gate for xsectmom. The old cadence was
   a CYCLE COUNT (~780 cycles ≈ "daily" at 30s cycles) which silently
   became ~74 minutes when cycles sped up to ~5.7s — 5x designed turnover.
   Loop speed must never define portfolio turnover again.

Integration points (also see SCAN_HEALTH_INTEGRATION.md):
  main.py     -> check_bar_depth after reconcile     (already wired)
  scanner.py  -> ScanFunnel in each scan_* method    (two lines per gate)
  xsection.py -> DailyRebalanceGate in maybe_rebalance
Env overrides: SWING_LOOKBACK_BARS, MEANREV_LOOKBACK_BARS,
XSECT_LOOKBACK_BARS, FUNNEL_EMIT_SECS, XSECT_REBALANCE_ET (e.g. "10:00").
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import audit

log = logging.getLogger("scan_health")

ET = ZoneInfo("America/New_York")

# Bars of daily history each strategy needs, with headroom over its longest
# indicator (200-SMA -> 210, 126-day momentum -> 140, 50-SMA/20-high -> 60).
LOOKBACK_REQUIREMENTS = {
    "swing": int(os.getenv("SWING_LOOKBACK_BARS", "60")),
    "meanrev": int(os.getenv("MEANREV_LOOKBACK_BARS", "210")),
    "xsectmom": int(os.getenv("XSECT_LOOKBACK_BARS", "140")),
}


# ------------------------------------------------------------------ 1. depth
def check_bar_depth(feed, universe, enabled_systems, sample_size: int = 8):
    """Sample daily-bar depth across the universe and compare against every
    enabled strategy's lookback. Returns the list of starved strategy names
    (empty = healthy). Never raises; logs and audits loudly instead."""
    sample = list(universe)[:sample_size]
    depths: dict[str, int] = {}
    for ticker in sample:
        try:
            bars = feed.get_daily_bars(ticker)
            depths[ticker] = len(bars) if bars else 0
        except AttributeError:
            log.warning("bar-depth check skipped: feed has no "
                        "get_daily_bars (simulated feed?)")
            return []
        except Exception as e:  # noqa: BLE001
            log.warning("bar-depth check: %s fetch failed: %s", ticker, e)
    if not depths:
        log.warning("bar-depth check: no sample obtained — cannot verify")
        return []

    # Judge on the TYPICAL depth (median), not the worst ticker — one new
    # listing (e.g. a recent IPO in emerging tech) shouldn't fail the feed.
    ds = sorted(depths.values())
    median = ds[len(ds) // 2]
    log.info("bar-depth: sampled %d tickers, median=%d bars (min=%d max=%d)",
             len(ds), median, ds[0], ds[-1])

    starved = []
    for strat, need in LOOKBACK_REQUIREMENTS.items():
        if strat not in enabled_systems:
            continue
        if median >= need:
            log.info("bar-depth: %-8s needs %3d bars -> OK", strat, need)
        else:
            starved.append(strat)
            log.critical("bar-depth: %-8s needs %d bars but feed returns "
                         "~%d — this strategy is STRUCTURALLY DEAD and will "
                         "never signal. Widen the daily-candle fetch window.",
                         strat, need, median)
    if starved:
        audit.record("data_starved", notify=True, strategies=starved,
                     median_bars=median,
                     required={s: LOOKBACK_REQUIREMENTS[s] for s in starved})
    return starved


# ----------------------------------------------------------------- 2. funnel
class ScanFunnel:
    """Gate-by-gate attrition counters for one strategy's scan pass.

    Usage in scanner.py, inside e.g. scan_meanrev():
        f = self._funnels["meanrev"]          # created once per strategy
        f.start_pass(len(self._universe))
        for ticker in self._universe:
            bars = ...
            if len(bars) < NEED: continue
            f.count("bars_ok")
            if not price > sma200: continue
            f.count("uptrend")
            if not rsi < 30: continue
            f.count("oversold")
            signals.append(...)
        f.finish(len(signals))
    Emits at most once per FUNNEL_EMIT_SECS (default 300) — or immediately
    whenever signals > 0, because those lines are the interesting ones.
    """

    def __init__(self, strategy: str,
                 emit_every_secs: float | None = None) -> None:
        self.strategy = strategy
        self._emit_every = (emit_every_secs if emit_every_secs is not None
                            else float(os.getenv("FUNNEL_EMIT_SECS", "300")))
        self._last_emit = 0.0
        self._universe = 0
        self._stages: dict[str, int] = {}

    def start_pass(self, universe_size: int) -> None:
        self._universe = universe_size
        self._stages = {}

    def count(self, stage: str) -> None:
        self._stages[stage] = self._stages.get(stage, 0) + 1

    def finish(self, signals: int) -> None:
        now = time.monotonic()
        if signals == 0 and (now - self._last_emit) < self._emit_every:
            return
        self._last_emit = now
        chain = " ".join(f"{k}={v}" for k, v in self._stages.items())
        log.info("funnel[%s]: universe=%d %s -> signals=%d",
                 self.strategy, self._universe, chain or "(no gates hit)",
                 signals)


# ------------------------------------------------------------- 3. rebalance
class DailyRebalanceGate:
    """True exactly once per US trading date, at/after a wall-clock ET time.
    Replaces cycle-count cadence, which broke when cycle speed changed.

    2026-07-09 incident fix: gate state now PERSISTS to disk. The original
    kept _last_run_date in memory, so every redeploy re-armed it — three
    mid-session deploys fired three same-day rebalances, and the third ran
    on breaker-degraded data and dumped INTC/MU (-$99 realized). State
    lives on the /data volume (REBALANCE_GATE_STATE to change) and survives
    restarts; a corrupt/missing file degrades to the old in-memory behavior.

    Usage in xsection.py:
        self._gate = DailyRebalanceGate()          # in __init__
        def maybe_rebalance(self):
            if not self._gate.should_run():
                return
            ...existing rebalance logic...
    Callers that decide NOT to act after the gate fires (market closed,
    degraded data, kill switch) must call rearm() so it can retry later.
    """

    def __init__(self, at_et: str | None = None,
                 state_path: str | None = None) -> None:
        raw = at_et or os.getenv("XSECT_REBALANCE_ET", "10:00")
        hh, mm = raw.split(":")
        self._hour, self._minute = int(hh), int(mm)
        self._state_path = state_path or os.getenv(
            "REBALANCE_GATE_STATE", "/data/rebalance_gate_state.txt")
        self._last_run_date = self._load_state()
        self._logged_wait_date = None
        self._retry_not_before = 0.0
        if self._last_run_date is not None:
            log.info("rebalance gate: restored state — last ran %s",
                     self._last_run_date)

    def _load_state(self):
        try:
            with open(self._state_path, encoding="utf-8") as fh:
                s = fh.read().strip()
            return datetime.strptime(s, "%Y-%m-%d").date() if s else None
        except (OSError, ValueError):
            return None

    def _save_state(self) -> None:
        try:
            d = os.path.dirname(self._state_path) or "."
            os.makedirs(d, exist_ok=True)
            with open(self._state_path, "w", encoding="utf-8") as fh:
                fh.write(str(self._last_run_date) if self._last_run_date
                         else "")
        except OSError as e:
            log.warning("rebalance gate: cannot persist state to %s (%s) — "
                        "a redeploy may re-fire the gate today",
                        self._state_path, e)

    def rearm(self, retry_after_minutes: float = 15.0) -> None:
        """Allow the gate to fire again today, after a cooldown. Call when
        the rebalance was skipped after the gate opened (degraded data,
        kill switch) so it retries once conditions recover. The cooldown
        prevents the fire->skip->rearm loop from retrying every cycle
        (observed 2026-07-09: CRITICAL spam every ~5s while degraded)."""
        self._last_run_date = None
        self._retry_not_before = time.monotonic() + retry_after_minutes * 60
        self._save_state()
        log.info("rebalance gate: re-armed (next retry in >=%.0f min)",
                 retry_after_minutes)

    def mark_done_today(self, now: datetime | None = None) -> None:
        """Record today as handled WITHOUT running. Use when the gate fired
        but no rebalance is possible for the rest of the day (market closed
        after fire time: holiday or post-16:00 ET) — re-arming in that case
        loops the gate every cycle until midnight."""
        now_et = (now or datetime.now(tz=ET)).astimezone(ET)
        self._last_run_date = now_et.date()
        self._save_state()
        log.info("rebalance gate: marked done for %s (market closed)",
                 self._last_run_date)

    def should_run(self, now: datetime | None = None) -> bool:
        now_et = (now or datetime.now(tz=ET)).astimezone(ET)
        today = now_et.date()
        if self._last_run_date == today:
            return False                      # already ran this date
        if time.monotonic() < self._retry_not_before:
            return False                      # in retry cooldown after rearm
        target = now_et.replace(hour=self._hour, minute=self._minute,
                                second=0, microsecond=0)
        if now_et < target:
            if self._logged_wait_date != today:
                self._logged_wait_date = today
                log.info("rebalance gate: waiting for %02d:%02d ET",
                         self._hour, self._minute)
            return False
        self._last_run_date = today
        self._save_state()
        log.warning("rebalance gate: OPEN for %s (>= %02d:%02d ET) — "
                    "running today's rebalance", today, self._hour,
                    self._minute)
        return True
