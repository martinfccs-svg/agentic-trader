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

from config import TRADING_MODE, UNIVERSE, live_money_armed
from brokers import build_broker
from feed_layer import SimulatedFeed, build_feed
from intraday_engine import IntradayRiskEngine
from kill_switch import KillSwitch
from models import System
from router import SignalRouter
from safety import market_is_open, near_close, startup_banner
from scanner import PriceActionScanner
from swing_engine import SwingRiskEngine
from trade_logger import TradeLogger

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("main")


def build():
    feed = build_feed(UNIVERSE)
    broker = build_broker()
    logger = TradeLogger()
    kill = KillSwitch(feed, broker)
    swing = SwingRiskEngine(feed, broker, kill, logger)
    intraday = IntradayRiskEngine(feed, broker, kill, logger)
    kill.register_price_loss_handler(System.INTRADAY, intraday.flatten_all)
    router = SignalRouter({System.SWING: swing, System.INTRADAY: intraday})
    scanner = PriceActionScanner(feed, UNIVERSE)
    return feed, broker, logger, kill, swing, intraday, router, scanner


def cycle(feed, kill, swing, intraday, router, scanner, force_market_open=False):
    kill.check_emergencies()
    is_open = force_market_open or market_is_open()

    # Swing scans daily structure (fine any time the market is open).
    for sig in scanner.scan_swing():
        router.route(sig)
    # Intraday only when the market is open and not in the flatten window.
    if is_open and not near_close():
        for sig in scanner.scan_intraday():
            router.route(sig)
    # Manage both books every cycle.
    swing.manage_open_positions()
    intraday.manage_open_positions()
    # Hard EOD flatten for intraday.
    if is_open and near_close():
        intraday.flatten_all("near close")


def run(loop: bool, cycles: int = 40):
    feed, broker, logger, kill, swing, intraday, router, scanner = build()
    startup_banner()
    log.info("agentic-trader v6 | mode=%s | broker live-armed=%s",
             TRADING_MODE, live_money_armed())

    sim = isinstance(feed, SimulatedFeed)
    try:
        n = 10_000_000 if loop else cycles
        for _ in range(n):
            # In sim we force "market open" so the demo actually trades.
            cycle(feed, kill, swing, intraday, router, scanner, force_market_open=sim)
            if sim:
                feed.step_prices()
            if loop:
                time.sleep(5)
    except KeyboardInterrupt:
        log.warning("interrupt -> flattening intraday before exit")
    finally:
        intraday.flatten_all("shutdown")     # never leave intraday hanging
        logger.print_scorecard(broker)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true")
    run(loop=ap.parse_args().loop)
