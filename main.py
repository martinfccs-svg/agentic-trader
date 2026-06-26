"""Orchestration for agentic-trader v5.

Runs out of the box: with no FINNHUB_API_KEY it uses the SimulatedFeed so you
can watch the full machine work end to end. With a key it uses live Finnhub
data (after you've confirmed access via verify_endpoints.py).

    python main.py              # one paper session against whichever feed
    python main.py --loop       # continuous (real deployment shape)
"""

from __future__ import annotations

import argparse
import logging
import time

from config import START_EQUITY, TRADING_MODE
from feed_layer import SimulatedFeed, build_feed
from intraday_engine import IntradayRiskEngine
from kill_switch import KillSwitch
from paper_broker import PaperBroker
from router import SignalRouter
from swing_engine import SwingRiskEngine
from trade_logger import TradeLogger
from models import System

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("main")

# Your scan universe. In production this comes from your discovery scan.
UNIVERSE = ["HNRG", "TPL", "IX", "KARD", "AAPL", "MSFT", "NVDA", "AMD", "F", "T"]


def build():
    feed = build_feed(UNIVERSE)
    broker = PaperBroker(START_EQUITY)
    logger = TradeLogger()
    kill = KillSwitch(feed, broker)
    swing = SwingRiskEngine(feed, broker, kill, logger)
    intraday = IntradayRiskEngine(feed, broker, kill, logger)
    kill.register_price_loss_handler(System.INTRADAY, intraday.flatten_all)
    router = SignalRouter({System.SWING: swing, System.INTRADAY: intraday})
    return feed, broker, logger, kill, swing, intraday, router


def one_cycle(feed, kill, swing, intraday, router) -> None:
    kill.check_emergencies()
    for sig in feed.get_signals():
        router.route(sig)
    intraday.evaluate_watchlist()
    swing.manage_open_positions()
    intraday.manage_open_positions()


def run(loop: bool, cycles: int = 40) -> None:
    feed, broker, logger, kill, swing, intraday, router = build()
    log.info("agentic-trader v5 starting in %s mode", TRADING_MODE)

    sim = isinstance(feed, SimulatedFeed)
    n = 10_000_000 if loop else cycles
    for i in range(n):
        one_cycle(feed, kill, swing, intraday, router)
        if sim:
            feed.step_prices()      # advance synthetic market
        if loop:
            time.sleep(5)
    # End of (simulated) session: flatten intraday, keep swing overnight.
    intraday.flatten_all("session end")
    logger.print_scorecard(broker)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true", help="run continuously (deploy shape)")
    args = ap.parse_args()
    run(loop=args.loop)
