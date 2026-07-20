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
from swing_v2 import scan_swing_v2
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
    return feed, broker, logger, kill, swing, intraday, meanrev, xsect, router, scanner, engines


def cycle(feed, broker, kill, swing, intraday, meanrev, xsect, router, scanner, engines,
          n: int = 0, force_market_open=False):
    log.info("=== cycle %d start ===", n)
    feed.new_cycle()                     # one fetch per ticker this cycle (rate-limit fix)
    kill.check_emergencies()
    is_open = force_market_open or market_is_open()

    # Scan only the enabled strategies. Skipping scan_intraday() is what
    # removes the 12-name 1-minute data cost while intraday is benched.
    swing_sigs = scanner.scan_swing() if swing else []
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
        for sig in swing_sigs + meanrev_sigs + intraday_sigs:
            router.route(sig)
    elif swing_sigs or meanrev_sigs:
        log.info("market closed — %d signal(s) held, NOT routed. They are "
                 "re-derived from live prices at the next open.",
                 len(swing_sigs) + len(meanrev_sigs))

    # Cross-sectional momentum rebalances on its own cadence (not per signal).
    if xsect:
        xsect.maybe_rebalance()

    # Manage every book each cycle (even when entries are halted).
    for e in engines:
        e.manage_open_positions()

    # swing_v2 candidate strategy, SHADOW-ONLY (2026-07-20): computes real
    # signals against live prices and writes would-be trades to the audit
    # trail; structurally cannot place orders (live mode is refused — see
    # swing_v2.py). Contained: a v2 failure must never cost a real cycle.
    # Data budget note: v2 fetches its own daily bars from Alpaca's data API
    # (free with the existing broker keys) — zero Finnhub budget impact.
    try:
        scan_swing_v2(UNIVERSE, equity=broker.equity)
    except Exception as e:  # noqa: BLE001
        log.error("swing_v2 shadow scan failed (non-fatal): %s", e)

    # Hard EOD flatten applies to the intraday book only.
    if intraday and is_open and near_close():
        intraday.flatten_all("near close")

    # Honest P&L: realized and unrealized logged separately, per system.
    for system in System:
        n_open = sum(1 for p in broker.positions.values() if p.system is system)
        log.info("  %-8s realized=%.2f unrealized=%.2f open=%d",
                 system.value, broker.realized_pnl[system],
                 broker.unrealized_pnl(system), n_open)
    log.info("  equity=%.2f | === cycle %d complete ===", broker.equity, n)
    return is_open


def run(loop: bool, cycles: int = 40):
    feed, broker, logger, kill, swing, intraday, meanrev, xsect, router, scanner, engines = build()
    startup_banner()
    log.info("agentic-trader v6 | mode=%s | broker live-armed=%s",
             TRADING_MODE, live_money_armed())

    sim = isinstance(feed, SimulatedFeed)
    audit.boot(mode=TRADING_MODE, live_armed=live_money_armed(),
               equity=broker.equity, sim=sim,
               deployment=os.getenv("RAILWAY_DEPLOYMENT_ID", "local"))

    # Broker is the source of truth at boot. After each 2026-07-06 crash the
    # bot restarted with an empty tracker while Alpaca still held shares and
    # live bracket legs -> re-bought TSLA (72 shares) and 404'd on manage.
    # Re-adopt bot-created positions; HALT on anything unrecognized.
    if hasattr(broker, "reconcile_at_startup"):
        try:
            orphans = broker.reconcile_at_startup()
        except Exception as e:  # noqa: BLE001
            log.critical("startup reconciliation failed (%s) — HALTING; "
                         "cannot trade without knowing broker state", e)
            audit.halt(reason=f"reconciliation failed: {e}")
            time.sleep(600)   # gentle halt: no restart storm
            return
        audit.reconcile(adopted=sorted(broker.positions), orphans=orphans,
                        profile=sorted(ENABLED_SYSTEMS))
        if orphans:
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
