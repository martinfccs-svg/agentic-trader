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
from meanrev_engine import MeanReversionEngine
from xsection import CrossSectionalMomentumEngine
from trade_logger import TradeLogger
from trade_record import TradeRecorder

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("main")

# After the close nothing can trade, but the loop was still scanning the
# full universe every ~5s (observed 2026-07-07, 164 cycles in 15 min after
# hours) — pure API burn. Slow the cadence when the market is closed.
AFTER_HOURS_INTERVAL_SECS = int(os.getenv("AFTER_HOURS_INTERVAL_SECS", "60"))


def build():
    feed = build_feed(UNIVERSE)
    recorder = TradeRecorder()          # writes trades.jsonl for Monte Carlo
    broker = build_broker(recorder=recorder)
    logger = TradeLogger()
    kill = KillSwitch(feed, broker)
    swing = SwingRiskEngine(feed, broker, kill, logger)
    intraday = IntradayRiskEngine(feed, broker, kill, logger)
    meanrev = MeanReversionEngine(feed, broker, kill, logger)
    xsect = CrossSectionalMomentumEngine(feed, broker, kill, logger, UNIVERSE)
    kill.register_price_loss_handler(System.INTRADAY, intraday.flatten_all)
    router = SignalRouter({
        System.SWING: swing, System.INTRADAY: intraday,
        System.MEANREV: meanrev, System.XSECTMOM: xsect,
    })
    scanner = PriceActionScanner(feed, UNIVERSE, INTRADAY_UNIVERSE)
    engines = [swing, intraday, meanrev, xsect]
    return feed, broker, logger, kill, swing, intraday, meanrev, xsect, router, scanner, engines


def cycle(feed, broker, kill, swing, intraday, meanrev, xsect, router, scanner, engines,
          n: int = 0, force_market_open=False):
    log.info("=== cycle %d start ===", n)
    feed.new_cycle()                     # one fetch per ticker this cycle (rate-limit fix)
    kill.check_emergencies()
    is_open = force_market_open or market_is_open()

    # Scan each strategy. Swing/mean-reversion run on daily structure; intraday
    # only while open and outside the flatten window.
    swing_sigs = scanner.scan_swing()
    meanrev_sigs = scanner.scan_meanrev()
    intraday_sigs = scanner.scan_intraday() if (is_open and not near_close()) else []
    log.info("scan: %d trend, %d meanrev, %d intraday (market_open=%s)",
             len(swing_sigs), len(meanrev_sigs), len(intraday_sigs), is_open)
    for sig in swing_sigs + meanrev_sigs + intraday_sigs:
        router.route(sig)

    # Cross-sectional momentum rebalances on its own cadence (not per signal).
    xsect.maybe_rebalance()

    # Manage every book each cycle (even when entries are halted).
    for e in engines:
        e.manage_open_positions()

    # Hard EOD flatten for the intraday book only.
    if is_open and near_close():
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
            return
        audit.reconcile(adopted=sorted(broker.positions), orphans=orphans)
        if orphans:
            log.critical("ORPHAN positions at broker: %s — HALTING. Resolve "
                         "in the Alpaca dashboard, then restart.", orphans)
            audit.halt(reason=f"orphan positions at broker: {orphans}")
            return

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
            intraday.flatten_all("shutdown")     # never leave intraday hanging
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
