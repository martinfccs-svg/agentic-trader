"""Orchestration for agentic-trader v6 (live data, pure price action).

Data:      Finnhub paid tier (real candles/quotes) when FINNHUB_API_KEY is set;
           SimulatedFeed otherwise (local testing).
Signals:   PriceActionScanner over candles -> TREND (swing) / MOMENTUM (intraday).
Execution: PaperBroker by default; AlpacaBroker when BROKER=alpaca. Real money
           only when config.live_money_armed() is true.

    python main.py            # one pass (sim if no key) + scorecard
    python main.py --loop      # continuous; deploy shape

Market-hours aware: intraday entries only while open; intraday positions are
flattened near the close; swing holds overnight.
"""

from __future__ import annotations

import argparse
import logging
import os
import time

import audit
from config import INTRADAY_UNIVERSE, SCAN_INTERVAL_SECS, TRADING_MODE, UNIVERSE, live_money_armed
from brokers import build_broker
from feed_layer import SimulatedFeed, build_feed
from intraday_engine import IntradayRiskEngine
from kill_switch import KillSwitch
from models import System
from router import SignalRouter
from safety import market_is_open, near_close, startup_banner
from scanner import PriceActionScanner
from swing_engine import SwingRiskEngine
from swing_v2 import scan_swing_v2, take_pending_signals
import regime
import portfolio_risk
import regime_allocation
import system_state
from meanrev_engine import MeanReversionEngine
from xsection import CrossSectionalMomentumEngine
from trade_logger import TradeLogger
from trade_record import TradeRecorder

# Split-stream logging (2026-07-20): DEBUG/INFO -> stdout, WARNING+ ->
# stderr. The old basicConfig sent everything to stderr, which Railway maps
# to severity=error — 1,008/1,008 log entries tagged error on Jul 20, real
# failures indistinguishable from P&L chatter.
from logging_setup import setup_logging
log = setup_logging()

# After the close nothing can trade, but the loop was still scanning the
# full universe every ~5s (observed 2026-07-07, 164 cycles in 15 min after
# hours) — pure API burn. Slow the cadence when the market is closed.
AFTER_HOURS_INTERVAL_SECS = int(os.getenv("AFTER_HOURS_INTERVAL_SECS", "60"))

# ---------------------------------------------------------------------------
# STRATEGY PROFILE (2026-07-08 operator decision)
# Focus: swing + xsectmom, meanrev at reduced weight. Intraday is BENCHED —
# code stays, engine isn't built, its scanner never runs, its 1-min data cost
# disappears. Benched, not deleted: a positive backtest + feed redundancy is
# the documented path back (see benchmark strategy shelf, rev 9).
# Override per-deploy without a code change: ENABLED_SYSTEMS=swing,xsectmom
# ---------------------------------------------------------------------------
ENABLED_SYSTEMS = {
    s.strip().lower()
    for s in os.getenv("ENABLED_SYSTEMS", "swing,xsectmom,meanrev").split(",")
    if s.strip()
}


def build():
    feed = build_feed(UNIVERSE)
    recorder = TradeRecorder()          # writes trades.jsonl for Monte Carlo
    broker = build_broker(recorder=recorder)
    logger = TradeLogger()
    kill = KillSwitch(feed, broker)

    swing = SwingRiskEngine(feed, broker, kill, logger) \
        if "swing" in ENABLED_SYSTEMS else None
    intraday = IntradayRiskEngine(feed, broker, kill, logger) \
        if "intraday" in ENABLED_SYSTEMS else None
    meanrev = MeanReversionEngine(feed, broker, kill, logger) \
        if "meanrev" in ENABLED_SYSTEMS else None
    xsect = CrossSectionalMomentumEngine(feed, broker, kill, logger, UNIVERSE) \
        if "xsectmom" in ENABLED_SYSTEMS else None

    if intraday is not None:
        # Only the intraday book flattens on price loss; others hold their
        # broker-side stops through an outage.
        kill.register_price_loss_handler(System.INTRADAY, intraday.flatten_all)

    routes = {}
    if swing:    routes[System.SWING] = swing
    if intraday: routes[System.INTRADAY] = intraday
    if meanrev:  routes[System.MEANREV] = meanrev
    if xsect:    routes[System.XSECTMOM] = xsect
    router = SignalRouter(routes)

    scanner = PriceActionScanner(feed, UNIVERSE, INTRADAY_UNIVERSE)
    engines = [e for e in (swing, intraday, meanrev, xsect) if e is not None]
    log.warning("strategy profile: enabled=%s | benched=%s",
                sorted(ENABLED_SYSTEMS),
                sorted({s.value for s in System} - ENABLED_SYSTEMS))
    # GATE BANNER (2026-07-23): every safety flag, stated at boot, from the
    # ACTUAL module constants the engines use — never from re-read env vars
    # that could drift from them. Exists because a status summarizer had to
    # guess INTRADAY_ENTRIES ("40% confidence") and guessed the sector cap
    # wrong; flags must be log-visible facts, not inferences.
    try:
        from swing_engine import SWING_ENTRIES as _sw
        from intraday_engine import INTRADAY_ENTRIES as _ie, \
            INTRADAY_V2_GATE as _v2
        from xsection import XSECT_SECTOR_CAP as _cap
        import meanrev_scoring as _mrs
        import regime as _rg
        import regime_allocation as _ra
        import loss_cooldown as _lc
        import correlation_manager as _cm
        import swing_v2 as _sv2
        import swing_engine as _se
        import portfolio_manager as _pm
        import promotion_registry as _pr
        log.warning("GATES: SWING_ENTRIES=%s INTRADAY_ENTRIES=%s "
                    "INTRADAY_V2_GATE=%s XSECT_SECTOR_CAP=%d "
                    "REGIME_FILTER=%s MEANREV_SCORING=%s "
                    "MEANREV_SCORE_MIN=%d REGIME_ALLOC=%s "
                    "SWING_LOSS_COOLDOWN_DAYS=%d CORRELATION_MODE=%s "
                    "(warn %.2f / block %.2f) SWING_V2_ROUTE=%s "
                    "SWING_RISK=%.4f PORTFOLIO_HEAT_MAX=%s SECTOR_MAX_PCT=%s "
                    "CONCENTRATION_TOP%d=%s DRAWDOWN_SCALING=%s "
                    "DESK_BUDGET=%s MAX_PARTICIPATION=%s RANK_SIGNALS=%s "
                    "BEARTREND=%s "
                    "| %s",
                    _sw, _ie, _v2, _cap, _rg.ENABLED, _mrs.SCORING_MODE,
                    _mrs.SCORE_MIN, _ra.MODE, _lc.DAYS.get("swing", 0),
                    _cm.MODE, _cm.WARN_CORR, _cm.BLOCK_CORR,
                    "ON (swing runs swing_v2)" if _sv2.ROUTE_LIVE else "off",
                    _se._swing_risk_pct(),
                    f"{_pm.HEAT_MAX:.1%}" if _pm.HEAT_MAX > 0 else "0 (measure)",
                    f"{_pm.SECTOR_MAX_PCT:.0%}" if _pm.SECTOR_MAX_PCT > 0
                    else "0 (measure)",
                    _pm.TOP_N,
                    f"{_pm.TOP_N_MAX_PCT:.0%}" if _pm.TOP_N_MAX_PCT > 0
                    else "0 (measure)",
                    _pm.DD_SCALE,
                    # DESK_BUDGET sits HERE because that is where its %s sits
                    # in the format string. Inserting it earlier shifted every
                    # later argument by one, which fed a string to the %d for
                    # CONCENTRATION_TOP. The count still matched (24/24), so
                    # the arity check passed while the ORDER was wrong —
                    # matching counts prove nothing about alignment.
                    f"{_pm.DESK_BUDGET_PCT:.0%}" if _pm.DESK_BUDGET_PCT > 0
                    else "0 (measure)",
                    f"{_pm.MAX_PARTICIPATION:.2%}" if _pm.MAX_PARTICIPATION > 0
                    else "0 (measure)",
                    "on" if RANK_SIGNALS else "off",
                    __import__("beartrend_scoring").MODE + " (research only)",
                    _pr.banner() + " | " + _cfg_hash_text())
        # Full detail on its own lines: which desk took which value, from
        # where. A setting that changes trading must be READ at boot, never
        # inferred from a file nobody opened.
        for _line in _pr.describe():
            log.warning("%s", _line)
        # WHAT EACH DESK IS ACTUALLY RUNNING, grouped and resolved. The GATES
        # line above is dense by design; this is the one an operator reads to
        # answer "what is meanrev doing right now?".
        try:
            import config_check as _cc2
            for _line in _cc2.active_config_report():
                log.warning("%s", _line)
        except Exception as _e:  # noqa: BLE001
            log.error("active config report failed (non-fatal): %s", _e)
        if _ie and "intraday" in ENABLED_SYSTEMS:
            log.critical("INTRADAY ENTRIES ARE LIVE (INTRADAY_ENTRIES "
                         "unset or true). If shadow mode was intended, set "
                         "INTRADAY_ENTRIES=false NOW.")
    except Exception as e:  # noqa: BLE001 — banner must never block boot
        log.error("gate banner failed (non-fatal): %s", e)
    return feed, broker, logger, kill, swing, intraday, meanrev, xsect, router, scanner, engines


RANK_SIGNALS = os.getenv("RANK_SIGNALS", "on").strip().lower() not in (
    "off", "false", "0", "no")

# ---------------------------------------------------------------------------
# SYMBOL QUARANTINE (2026-08-04)
#
# Per-signal isolation stops one bad symbol killing a cycle. It does not stop
# that symbol failing identically every cycle for the rest of the day —
# logging the same stack trace, burning the same work, and burying anything
# new in the noise.
#
# Deliberately IN-MEMORY and NOT persisted: a restart should clear it, because
# a restart is often exactly what fixes the underlying condition. Persisting a
# quarantine would let a transient failure bench a symbol across deploys.
#
# Only ROUTING EXCEPTIONS count. A signal legitimately rejected by risk, a
# gate, or the portfolio manager is the system working — quarantining on
# rejections would silence the desks.
# ---------------------------------------------------------------------------
QUARANTINE_FAILURES = int(os.getenv("QUARANTINE_FAILURES", "3"))
QUARANTINE_MINUTES = float(os.getenv("QUARANTINE_MINUTES", "30"))
_route_failures: dict = {}          # ticker -> [timestamps]
_quarantined: dict = {}             # ticker -> release timestamp


def _quarantine_check(ticker: str) -> bool:
    """True if this ticker is currently benched. Releases automatically."""
    until = _quarantined.get(ticker)
    if until is None:
        return False
    if time.time() >= until:
        del _quarantined[ticker]
        _route_failures.pop(ticker, None)
        log.warning("QUARANTINE RELEASED %s — routing resumes", ticker)
        return False
    return True


def _quarantine_record(ticker: str) -> None:
    """Record a routing exception; bench the ticker if it keeps failing."""
    now = time.time()
    window = QUARANTINE_MINUTES * 60
    hits = [t for t in _route_failures.get(ticker, []) if now - t < window]
    hits.append(now)
    _route_failures[ticker] = hits
    if len(hits) >= QUARANTINE_FAILURES and ticker not in _quarantined:
        _quarantined[ticker] = now + window
        log.error("QUARANTINE %s — %d routing failures in %.0f min. Skipping "
                  "it for %.0f min so the same exception stops flooding the "
                  "log and burning cycle time. Everything else continues.",
                  ticker, len(hits), QUARANTINE_MINUTES, QUARANTINE_MINUTES)


class _contained:
    """Run a non-fatal section, log uniformly, and COUNT the failure.

    There are ~19 `except Exception` blocks in this file. Each is deliberate —
    telemetry, research and reconciliation must never kill a trading cycle —
    but each is also a place the system can run DEGRADED with no outward
    sign. If the cycle-health line throws every cycle, or beartrend fails
    silently for a week, nothing today would say so.

    This does not change what is caught. It makes the catching visible: the
    section is named, the failure is logged once with that name, and the name
    is surfaced on the health line so "quietly broken" becomes "broken, and
    stated".

        with _contained("cycle_health", _degraded):
            ...

    Reduces the boilerplate the review flagged, but that is a side effect —
    the reason is that a swallowed exception nobody counts is a bug that can
    live forever.
    """

    def __init__(self, name: str, sink: list = None):
        self.name, self.sink = name, sink

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc is None:
            return False
        log.error("contained failure in %s (non-fatal): %s", self.name, exc)
        if self.sink is not None:
            self.sink.append(self.name)
        return True          # swallow, exactly as before


def _state_set(state, why: str = "", force: bool = False) -> None:
    """Set the lifecycle state without letting telemetry break the boot.

    Wrapped because the states that matter most — HALTED on an orphan, on a
    failed reconcile — are set on paths that are already handling a serious
    problem. A state-tracking import error must not become the thing that
    stops a halt from being recorded.
    """
    try:
        import system_state
        system_state.set_state(state, why, force=force)
    except Exception:  # noqa: BLE001
        pass


def _cfg_hash_text() -> str:
    """The fingerprint of the configuration this process is running.

    Printed at boot AND stamped on every trade record, so "what config
    produced trade #1842?" is answerable six weeks later from the trade
    itself rather than from a boot banner that scrolled away.
    """
    try:
        import config_check as _cc
        h, pairs = _cc.config_hash()
        return f"CONFIG_HASH={h}({len(pairs)} set)"
    except Exception:  # noqa: BLE001
        return "CONFIG_HASH=unavailable"


def _entries_blocked() -> list:
    """Fatal invariants, read fresh. Boot is never blocked; ENTRIES are."""
    try:
        import config_check as _cc
        return list(getattr(_cc, "ENTRIES_BLOCKED", []) or [])
    except Exception:  # noqa: BLE001
        return []


def _stage_text(stage: dict, total: float, threshold: float = 1.0) -> str:
    """Name the stages that actually cost time. Silent on a fast cycle —
    a heartbeat that prints five numbers every second is a heartbeat nobody
    reads."""
    if not stage or total < threshold:
        return ""
    slow = sorted(((v, k) for k, v in stage.items() if v >= 0.2),
                  reverse=True)[:3]
    if not slow:
        return ""
    return " | " + " ".join(f"{k}={v:.1f}s" for v, k in slow)


def _signal_quality(sig):
    """A desk-local quality figure, or None if that desk has no score.

    Reads only what the signal already carries — nothing is computed or
    invented here."""
    raw = getattr(sig, "raw", None) or {}
    for key in ("score",):                    # intraday (0-1), meanrev (0-6)
        if isinstance(raw.get(key), (int, float)):
            return float(raw[key])
    card = raw.get("card")
    if isinstance(card, dict) and isinstance(card.get("score"), (int, float)):
        return float(card["score"])
    if isinstance(raw.get("adx"), (int, float)):
        return float(raw["adx"])              # swing_v2: trend strength
    return None


def _rank_within_desk(sigs):
    """Sort each desk's signals by its own score, best first. Desks keep
    their relative order; signals without a score keep arrival order
    (stable sort), so this can only ever re-order WITHIN a desk."""
    if not RANK_SIGNALS or len(sigs) < 2:
        return sigs
    try:
        buckets, order = {}, []
        for s_ in sigs:
            key = getattr(getattr(s_, "source", None), "value", "?")
            if key not in buckets:
                buckets[key] = []
                order.append(key)
            buckets[key].append(s_)
        out = []
        for key in order:
            group = buckets[key]
            scored = [(i, x, _signal_quality(x)) for i, x in enumerate(group)]
            if any(q is not None for _, _, q in scored) and len(group) > 1:
                group = [x for _, x, _ in sorted(
                    scored, key=lambda t: (-(t[2] if t[2] is not None
                                             else float("-inf")), t[0]))]
                log.info("ranked %d %s signal(s): %s", len(group), key,
                         ", ".join(f"{x.ticker}"
                                   f"({_signal_quality(x):.2f})"
                                   if _signal_quality(x) is not None
                                   else x.ticker for x in group))
            out.extend(group)
        return out
    except Exception as e:  # noqa: BLE001 — ordering must never break a cycle
        log.error("signal ranking failed (%s) — using arrival order", e)
        return sigs


def cycle(feed, broker, kill, swing, intraday, meanrev, xsect, router, scanner, engines,
          n: int = 0, force_market_open=False):
    log.info("=== cycle %d start ===", n)
    _cycle_t0 = time.time()
    # Cycle-time bands. A slow cycle is not just slow — past the critical
    # threshold the data underpinning an ENTRY decision is old enough that
    # acting on it is a guess. Exits still run: they use the position's own
    # stop, which does not go stale the way a scan does.
    _CYCLE_WARN = float(os.getenv("CYCLE_WARN_SECS", "5"))
    _CYCLE_CRIT = float(os.getenv("CYCLE_CRIT_SECS", "10"))
    _health = {"scanned": 0, "routed": 0, "failed": 0, "quarantined": 0}
    # PER-STAGE TIMING (2026-08-06). Cycle duration alone said a cycle took
    # 35s and nothing about WHICH part. Yesterday's log ran a 4.2s median
    # against a 35.1s max — an 8x outlier with no way to attribute it. These
    # are cheap wall-clock deltas, not a profiler; the point is to name the
    # stage, then look at it.
    _stage: dict = {}
    _degraded: list = []      # names of contained sections that failed

    def _timed(name):
        class _T:
            def __enter__(s):
                s.t = time.time(); return s
            def __exit__(s, *a):
                _stage[name] = _stage.get(name, 0.0) + (time.time() - s.t)
                return False
        return _T()
    feed.new_cycle()                     # one fetch per ticker this cycle (rate-limit fix)
    kill.check_emergencies()
    is_open = force_market_open or market_is_open()

    # ---- POSITION MANAGEMENT RUNS FIRST (2026-08-14) -------------------
    # This block used to sit AFTER the scan and the routing loop. So a stalled
    # feed inside the scan — the 19-second hang — meant exits, trailing stops
    # and the EOD flatten never ran that cycle. The system would stop
    # PROTECTING the book at exactly the moment it was least healthy.
    #
    # Protecting what you already hold outranks looking for more to buy. Each
    # desk is contained separately so one bad book cannot block the others.
    for e in engines:
        with _contained(f"manage_{type(e).__name__}", _degraded):
            e.manage_open_positions()

    # Scan only the enabled strategies. Skipping scan_intraday() is what
    # removes the 12-name 1-minute data cost while intraday is benched.
    # When SWING_V2_ROUTE=on, swing_v2 OWNS the swing desk: its scanner
    # produces the entries and old swing's breakout scan is switched off.
    # Running both would put two strategies into one set of position slots
    # and make the results unattributable (2026-08-01).
    _v2_route = os.getenv("SWING_V2_ROUTE", "off").strip().lower() in (
        "on", "true", "1", "yes")
    with _timed("scan_swing"):
        swing_sigs = ([] if (_v2_route or not swing) else scanner.scan_swing())
    if _v2_route and swing:
        # Scan and drain BEFORE the routing block below. An earlier wiring
        # ran the v2 scan further down, after routing — so every signal it
        # produced was silently discarded. Contained: a v2 failure must never
        # cost a cycle.
        try:
            scan_swing_v2(UNIVERSE, equity=broker.equity)
            _v2_sigs = take_pending_signals()
            if _v2_sigs:
                log.warning("swing_v2 -> engine: %d signal(s) routed",
                            len(_v2_sigs))
                swing_sigs.extend(_v2_sigs)
        except Exception as e:  # noqa: BLE001
            log.error("swing_v2 routed scan failed (non-fatal): %s", e)
    meanrev_sigs = scanner.scan_meanrev() if meanrev else []
    intraday_sigs = scanner.scan_intraday() \
        if (intraday and is_open and not near_close()) else []
    log.info("scan: %d trend, %d meanrev, %d intraday (market_open=%s)",
             len(swing_sigs), len(meanrev_sigs), len(intraday_sigs), is_open)

    # Route to engines ONLY while the market is open (2026-07-16 fix).
    # Previously only intraday's SCAN was hours-gated; swing/meanrev signals
    # routed around the clock. At 16:27 ET the post-close daily-bar refresh
    # revealed today's completed bar, produced two genuine breakouts, and the
    # bot fired GTC brackets into a CLOSED market (PLD, UNP). Those orders sit
    # until the next open and then fill against stops computed from stale
    # prices — a gap below the stop means an instant stop-out on a position
    # held for zero seconds. Swing/meanrev signals are end-of-day facts; the
    # correct response is to act at the next open, re-derived from live prices.
    #
    # Scans still run while closed on purpose: the funnels are useful
    # observability (they show what WOULD signal), and daily bars are cached
    # so the scan costs almost nothing.
    if is_open:
        sigs = swing_sigs + meanrev_sigs + intraday_sigs
        if sigs and not regime.risk_on(feed):
            # Regime overlay (2026-07-22): entries only. Exits, stops, and
            # flattens are never regime-gated — reducing risk is always
            # allowed. Held signals re-derive at the next risk-on scan.
            log.warning("REGIME risk-off — %d entry signal(s) held, NOT "
                        "routed (set REGIME_FILTER=off to disable)",
                        len(sigs))
        else:
            # WITHIN-DESK RANKING (2026-08-02). `sigs` used to be routed in
            # arrival order, so with a per-day entry cap the first signals to
            # be scanned won the slots — an artefact of universe ordering, not
            # a judgement about the trades. Now each desk's signals are sorted
            # by the score that desk already computes.
            #
            # Deliberately WITHIN desk only: meanrev scores 0-6, intraday 0-1,
            # swing_v2 carries ADX. Those are not comparable, and inventing a
            # common scale would mean inventing weights — the same objection
            # that ruled out the composite momentum score and the weighted
            # portfolio risk score. Cross-desk ranking waits for a shared
            # evidence-based metric that does not yet exist.
            with _timed("rank"):
                sigs = _rank_within_desk(sigs)
            # PER-SIGNAL FAULT ISOLATION (2026-08-04). This loop was
            # unwrapped, so ONE bad signal aborted the whole cycle — which is
            # how a NameError on a single MSFT decision produced 10
            # consecutive dead cycles instead of 10 logged errors. TSLA and
            # everything behind it never ran.
            #
            # The engines already contain their own try/except; this is the
            # backstop for anything they do not catch, including bugs in the
            # routing path itself. A trading system should degrade one signal
            # at a time, never all at once.
            _route_t0 = time.time()
            # FATAL INVARIANTS (2026-08-07). Boot is never blocked, but a
            # configuration that cannot safely OPEN positions must not open
            # them. Everything above this line — reconciliation, exits,
            # position management — still runs, because stranding an open
            # book with no manager is worse than not trading.
            _blocked = _entries_blocked()
            # The previous cycle's duration gates THIS cycle's entries: if the
            # feed was stalling a moment ago, the prices these signals were
            # computed from are suspect.
            _prev = globals().get("_LAST_CYCLE_SECS", 0.0)
            if _prev >= _CYCLE_CRIT:
                _blocked = list(_blocked) + [
                    f"previous cycle took {_prev:.1f}s (critical threshold "
                    f"{_CYCLE_CRIT:.0f}s) — entry data may be stale; exits and "
                    f"stop management continue normally"]
            if _blocked:
                log.critical("ENTRIES BLOCKED — %d signal(s) dropped: %s",
                             len(sigs), "; ".join(_blocked))
                sigs = []
            _routed, _failed_tickers, _skipped = 0, [], []
            _health["scanned"] = len(sigs)
            for sig in sigs:
                tkr = getattr(sig, "ticker", "?")
                if _quarantine_check(tkr):
                    _skipped.append(tkr)
                    continue
                try:
                    router.route(sig)
                    _routed += 1
                except Exception as e:  # noqa: BLE001 — isolate, do not abort
                    _failed_tickers.append(tkr)
                    _quarantine_record(tkr)
                    log.exception("ROUTE FAILED %s (%s) — signal dropped, "
                                  "cycle continues: %s", tkr,
                                  getattr(getattr(sig, "source", None),
                                          "value", "?"), e)
            if _failed_tickers:
                # Name the tickers in the summary: a pattern across cycles is
                # visible at a glance instead of by scrolling stack traces.
                log.error("routing: %d of %d signal(s) failed (%s) — the "
                          "cycle completed anyway", len(_failed_tickers),
                          _routed + len(_failed_tickers),
                          ", ".join(_failed_tickers))
            if _skipped:
                log.warning("routing: skipped %d quarantined signal(s): %s",
                            len(_skipped), ", ".join(_skipped))
            _health.update(routed=_routed, failed=len(_failed_tickers),
                           quarantined=len(_skipped))
            _stage["route"] = time.time() - _route_t0
    elif swing_sigs or meanrev_sigs:
        log.info("market closed — %d signal(s) held, NOT routed. They are "
                 "re-derived from live prices at the next open.",
                 len(swing_sigs) + len(meanrev_sigs))

    # Cross-sectional momentum rebalances on its own cadence (not per signal).
    if xsect:
        xsect.maybe_rebalance()

    # swing_v2 candidate strategy, SHADOW-ONLY (2026-07-20): computes real
    # signals against live prices and writes would-be trades to the audit
    # trail; structurally cannot place orders (live mode is refused — see
    # swing_v2.py). Contained: a v2 failure must never cost a real cycle.
    # Data budget note: v2 fetches its own daily bars from Alpaca's data API
    # (free with the existing broker keys) — zero Finnhub budget impact.
    if not _v2_route:
        try:
            scan_swing_v2(UNIVERSE, equity=broker.equity)
        except Exception as e:  # noqa: BLE001
            log.error("swing_v2 shadow scan failed (non-fatal): %s", e)

    # ---- BEARTREND RESEARCH SCAN (2026-08-03) -------------------------
    # Records short CANDIDATES to /data/research/beartrend/ so the question
    # "would a bear desk have been worth building?" is answerable later. It
    # places no orders and there is no execution path — brokers.py cannot
    # short. Contained: research telemetry must never cost a trading cycle.
    #
    # Runs on the daily-bar cadence, not every cycle: the gates are daily and
    # re-scanning 68 names every minute would add nothing but load.
    _research_t0 = time.time()
    try:
        import beartrend_scoring
        if beartrend_scoring.MODE != "off" and beartrend_scoring.due():
            _st = regime_allocation.current(feed, UNIVERSE)
            _spy = feed.get_daily_bars(regime_allocation.SYMBOL)
            beartrend_scoring.scan(
                UNIVERSE,
                bars_for=lambda t: feed.get_daily_bars(t),
                bench_closes=(_spy.close if _spy else None),
                health_record=None,
                cio_regime=_st.label,
                breadth=_st.breadth_pct)
    except Exception as e:  # noqa: BLE001 — research must never break a cycle
        log.error("beartrend research scan failed (non-fatal): %s", e)
    _stage["research"] = time.time() - _research_t0

    # Hard EOD flatten applies to the intraday book only.
    if intraday and is_open and near_close():
        intraday.flatten_all("near close")

    # Regime allocation (2026-07-24): the CIO layer. Classified once per
    # cycle (TTL-cached inside, so this is cheap) purely so the label and
    # multipliers are always visible in the log. In shadow mode it applies
    # NOTHING — engines call regime_allocation.multiplier() themselves and
    # get 1.0 back until REGIME_ALLOC=live.
    try:
        regime_allocation.current(feed, UNIVERSE)
    except Exception as e:  # noqa: BLE001 — an overlay must never break a cycle
        log.error("regime allocation read failed (non-fatal): %s", e)

    # Portfolio heat (2026-07-24 fix): read ONCE PER CYCLE from the main
    # loop, not from inside an engine's signal handler. The first wiring
    # sat in meanrev.handle_signal — which never fires when nothing is
    # oversold — so the instrument was installed and never took a reading.
    # Measurement must not depend on a signal arriving. Throttled inside
    # portfolio_risk (re-logs only on a >=0.5pp move); gating stays off
    # until PORTFOLIO_HEAT_MAX > 0.
    try:
        portfolio_risk.check(broker)
    except Exception as e:  # noqa: BLE001 — an instrument must never break a cycle
        log.error("portfolio heat read failed (non-fatal): %s", e)

    # ---- ONE-LINE CYCLE HEALTH (2026-08-04) --------------------------
    # Everything here was already being collected and logged in pieces
    # across ~40 lines. One line makes a bad cycle visible at a glance and
    # gives something greppable to trend over time; the detail above stays
    # for when the summary says something is wrong.
    with _contained("cycle_health", _degraded):
        _hb = ""
        with _contained("heat_readout", _degraded):
            import portfolio_manager as _pmh
            _h = _pmh._heat_now(broker)
            _hb = f" heat={_h:.2%}" if _h is not None else ""
        _reg = ""
        with _contained("regime_readout", _degraded):
            import regime_allocation as _ra
            _st = _ra.last_state()
            if _st:
                _reg = f" regime={_st.label}/{_st.confidence:.0f}%"
        # LEVEL BY CONTENT (2026-08-05). This was log.warning unconditionally,
        # so Railway flagged EVERY cycle as an error — 122 of 123 "errors" in
        # a 22-minute window were this line. A monitor that cries wolf on
        # every heartbeat is worse than no monitor: the one real failure is
        # buried in the noise. WARNING only when something actually went
        # wrong; INFO otherwise.
        _is_degraded = (_health["failed"] or _health["quarantined"]
                        or _degraded)
        (log.warning if _is_degraded else log.info)(
            "CYCLE %d HEALTH %.2fs | signals %d routed %d failed %d "
            "quarantined %d | positions %d equity=%.2f%s%s",
            n, time.time() - _cycle_t0, _health["scanned"],
            _health["routed"], _health["failed"],
            _health["quarantined"], len(broker.positions),
            broker.equity, _hb, _reg + _stage_text(_stage,
                                                   time.time() - _cycle_t0)
            + (f" | DEGRADED: {','.join(sorted(set(_degraded)))}"
               if _degraded else ""))

    # One JSON snapshot for the dashboard. Contained: a page that cannot be
    # written must never cost a cycle.
    with _contained("dashboard", _degraded):
        import dashboard_export
        dashboard_export.write_snapshot(
            broker, cycle_n=n,
            health={**_health,
                    "cycle_secs": round(time.time() - _cycle_t0, 2),
                    "market_open": bool(is_open),
                    "degraded": sorted(set(_degraded))},
            funnel=getattr(scanner, "last_funnel", None) or {})

    # Persistent failure is a different fact from an intermittent one. This
    # escalates only after DEGRADED_AFTER_CYCLES consecutive failures, and
    # shouts only when a RISK CONTROL is the thing that went blind.
    _elapsed = time.time() - _cycle_t0
    globals()["_LAST_CYCLE_SECS"] = _elapsed
    if _elapsed >= _CYCLE_CRIT:
        log.critical("CYCLE %d took %.1fs (critical %.0fs) — next cycle's "
                     "ENTRIES are blocked; exits unaffected. Slowest stages: "
                     "%s", n, _elapsed, _CYCLE_CRIT,
                     _stage_text(_stage, _elapsed, 0.0) or "not attributed")
    elif _elapsed >= _CYCLE_WARN:
        log.warning("CYCLE %d took %.1fs (warning %.0fs)%s", n, _elapsed,
                    _CYCLE_WARN, _stage_text(_stage, _elapsed, 0.0))

    with _contained("state_tracking", _degraded):
        import system_state
        system_state.note_cycle(_degraded)
        system_state.set_state(
            system_state.State.CLOSED if not is_open
            else system_state.State.TRADING)


    # Honest P&L: realized and unrealized logged separately, per system.
    #
    # DESK ATTRIBUTION (2026-08-06). Dollars alone do not say which desk drove
    # the day — a desk holding four positions and a desk holding one are not
    # comparable at a glance. Contribution is (desk P&L / equity), so the
    # figures sum to the portfolio's own move and one line answers "who did
    # this?".
    #
    # The two columns mean DIFFERENT things and are kept apart because
    # blending them would be dishonest: realized is CUMULATIVE since process
    # start, unrealized is a LEVEL on open positions right now, not a change
    # since this morning. Turning either into "today's return" would need a
    # day-open snapshot this process does not keep across restarts.
    _eq = broker.equity or 1.0
    for system in System:
        n_open = sum(1 for p in broker.positions.values() if p.system is system)
        _r, _u = broker.realized_pnl[system], broker.unrealized_pnl(system)
        log.info("  %-8s realized=%.2f (%+.2f%% of equity) unrealized=%.2f "
                 "(%+.2f%%) open=%d",
                 system.value, _r, 100.0 * _r / _eq, _u, 100.0 * _u / _eq,
                 n_open)
    _tr = sum(broker.realized_pnl[s] for s in System)
    _tu = sum(broker.unrealized_pnl(s) for s in System)
    log.info("  ATTRIBUTION realized %+.2f%% + unrealized %+.2f%% = %+.2f%% "
             "of equity | booked today %+.2f",
             100.0 * _tr / _eq, 100.0 * _tu / _eq, 100.0 * (_tr + _tu) / _eq,
             getattr(broker, "realized_today", 0.0))
    log.info("  equity=%.2f | === cycle %d complete ===", broker.equity, n)
    return is_open


def run(loop: bool, cycles: int = 40):
    feed, broker, logger, kill, swing, intraday, meanrev, xsect, router, scanner, engines = build()
    startup_banner()
    log.info("agentic-trader v6 | mode=%s | broker live-armed=%s",
             TRADING_MODE, live_money_armed())

    # Volume persistence canary (2026-07-26). Runs FIRST, before reconcile,
    # because reconcile's behaviour depends on the registry existing: an empty
    # registry silently degrades entry_time to boot time and reports "no known
    # stop". A 898-byte audit.jsonl alongside 63 closed trades at the broker
    # is what prompted this check.
    try:
        import config_check
        config_check.validate()
    except Exception as e:  # noqa: BLE001 — a validator must never block boot
        log.error("config_check failed (non-fatal): %s", e)

    try:
        import volume_check
        volume_check.check()
    except Exception as e:  # noqa: BLE001 — a diagnostic must never block boot
        log.error("volume_check failed (non-fatal): %s", e)

    sim = isinstance(feed, SimulatedFeed)
    audit.boot(mode=TRADING_MODE, live_armed=live_money_armed(),
               equity=broker.equity, sim=sim,
               deployment=os.getenv("RAILWAY_DEPLOYMENT_ID", "local"))

    # Broker is the source of truth at boot. After each 2026-07-06 crash the
    # bot restarted with an empty tracker while Alpaca still held shares and
    # live bracket legs -> re-bought TSLA (72 shares) and 404'd on manage.
    # Re-adopt bot-created positions; HALT on anything unrecognized.
    _state_set(system_state.State.RECONCILING, "comparing book vs broker")
    if hasattr(broker, "reconcile_at_startup"):
        try:
            orphans = broker.reconcile_at_startup()
        except Exception as e:  # noqa: BLE001
            _state_set(system_state.State.HALTED,
                       "reconciliation failed", force=True)
            log.critical("startup reconciliation failed (%s) — HALTING; "
                         "cannot trade without knowing broker state", e)
            audit.halt(reason=f"reconciliation failed: {e}")
            time.sleep(600)   # gentle halt: no restart storm
            return
        audit.reconcile(adopted=sorted(broker.positions), orphans=orphans,
                        profile=sorted(ENABLED_SYSTEMS))
        if orphans:
            _state_set(system_state.State.HALTED,
                       f"orphan positions: {orphans}", force=True)
            log.critical("ORPHAN positions at broker: %s — HALTING. Resolve "
                         "in the Alpaca dashboard, then restart.", orphans)
            audit.halt(reason=f"orphan positions at broker: {orphans}")
            time.sleep(600)   # gentle halt: no restart storm
            return
        # A position belonging to a BENCHED system has no engine to manage
        # its stops/exits. Refuse to run rather than babysit it blind.
        benched_held = sorted({t for t, p in broker.positions.items()
                               if p.system.value not in ENABLED_SYSTEMS})
        if benched_held:
            log.critical("positions %s belong to benched system(s) — HALTING. "
                         "Close them manually or re-enable the system via "
                         "ENABLED_SYSTEMS, then restart.", benched_held)
            audit.halt(reason=f"positions held by benched system: {benched_held}")
            # Deliberate halt, but Railway restarts exited processes
            # immediately — on 2026-07-11 that turned this halt into a
            # 2-second boot loop (and a ntfy ping per boot, all weekend).
            # Sleep before exiting so the loop is gentle and the phone
            # gets one ping per ~10 minutes, not per 2 seconds.
            log.critical("halted — sleeping 10 minutes before exit to "
                         "prevent a restart storm")
            time.sleep(600)
            return

    # Verify the feed returns enough daily history for each enabled
    # strategy's lookback (200-SMA, 126d momentum...). A short fetch window
    # makes a strategy silently signal-less forever — loud beats silent.
    if not sim:
        try:
            from scan_health import check_bar_depth
            starved = check_bar_depth(feed, UNIVERSE, ENABLED_SYSTEMS)
            if starved:
                log.critical("strategies %s are data-starved — running "
                             "anyway (no bad trades possible, just none), "
                             "but fix the fetch window.", starved)
        except Exception as e:  # noqa: BLE001
            log.warning("bar-depth check failed (non-fatal): %s", e)

    i = 0
    consecutive_failures = 0
    MAX_CONSECUTIVE_FAILURES = 10
    try:
        if loop:
            while True:
                i += 1
                # Contain per-cycle failures. On 2026-07-06 a single Alpaca
                # 404 propagated up, killed the process, and the deployment
                # restart loop (3 restarts in 13 min) caused duplicate orders
                # and Finnhub 429s. A bad cycle should log, back off, and let
                # the next cycle retry — not take the process down.
                try:
                    is_open = cycle(feed, broker, kill, swing, intraday, meanrev, xsect, router, scanner, engines,
                                    n=i, force_market_open=sim)
                    consecutive_failures = 0
                except KeyboardInterrupt:
                    raise
                except Exception as e:  # noqa: BLE001
                    is_open = True   # assume open on failure: retry promptly
                    consecutive_failures += 1
                    backoff = min(2 ** consecutive_failures, 60)
                    log.error("cycle %d failed (%d consecutive): %s — "
                              "backing off %ds", i, consecutive_failures, e,
                              backoff, exc_info=True)
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        log.critical("%d consecutive cycle failures — "
                                     "something is structurally broken; "
                                     "exiting to shutdown flatten",
                                     consecutive_failures)
                        audit.crash(error=str(e),
                                    consecutive_failures=consecutive_failures,
                                    cycle=i)
                        break
                    time.sleep(backoff)
                if sim:
                    feed.step_prices()
                # Market-aware cadence: full speed while open, slow scan
                # after hours (nothing can trade; save the API budget).
                time.sleep(SCAN_INTERVAL_SECS if is_open
                           else AFTER_HOURS_INTERVAL_SECS)
        else:
            for i in range(1, cycles + 1):
                cycle(feed, broker, kill, swing, intraday, meanrev, xsect, router, scanner, engines,
                      n=i, force_market_open=sim)
                if sim:
                    feed.step_prices()
    except KeyboardInterrupt:
        log.warning("interrupt -> flattening intraday before exit")
    finally:
        # STOPPING is set FIRST in the finally block — before the flatten —
        # so a shutdown that dies partway through still left a record that
        # shutdown had begun. Crash #3 (2026-07-06) died INSIDE this block.
        _state_set(system_state.State.STOPPING, "shutdown", force=True)
        # Crash #3 (2026-07-06) died INSIDE this block: flatten raised on a
        # held bracket, and separately print_scorecard raised AttributeError.
        # Shutdown must complete no matter what either step does.
        try:
            if intraday is not None:
                intraday.flatten_all("shutdown")   # never leave intraday hanging
        except Exception as e:  # noqa: BLE001
            log.critical("shutdown flatten raised (positions may remain at "
                         "broker — check the Alpaca dashboard): %s", e)
        try:
            logger.print_scorecard(broker)
        except Exception as e:  # noqa: BLE001
            log.error("scorecard failed during shutdown: %s", e)
        try:
            audit.scorecard(
                equity=broker.equity,
                realized_today=broker.realized_today,
                realized_by_system={s.value: round(v, 2)
                                    for s, v in broker.realized_pnl.items()})
        except Exception as e:  # noqa: BLE001
            log.error("audit scorecard failed: %s", e)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true")
    run(loop=ap.parse_args().loop)
