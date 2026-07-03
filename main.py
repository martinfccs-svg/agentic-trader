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
import time

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


def run(loop: bool, cycles: int = 40):
    feed, broker, logger, kill, swing, intraday, meanrev, xsect, router, scanner, engines = build()
    startup_banner()
    log.info("agentic-trader v6 | mode=%s | broker live-armed=%s",
             TRADING_MODE, live_money_armed())

    sim = isinstance(feed, SimulatedFeed)
    i = 0
    try:
        if loop:
            while True:
                i += 1
                cycle(feed, broker, kill, swing, intraday, meanrev, xsect, router, scanner, engines,
                      n=i, force_market_open=sim)
                if sim:
                    feed.step_prices()
                time.sleep(SCAN_INTERVAL_SECS)
        else:
            for i in range(1, cycles + 1):
                cycle(feed, broker, kill, swing, intraday, meanrev, xsect, router, scanner, engines,
                      n=i, force_market_open=sim)
                if sim:
                    feed.step_prices()
    except KeyboardInterrupt:
        log.warning("interrupt -> flattening intraday before exit")
    finally:
        intraday.flatten_all("shutdown")     # never leave intraday hanging
        logger.print_scorecard(broker)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true")
    run(loop=ap.parse_args().loop)
