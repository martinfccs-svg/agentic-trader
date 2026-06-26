"""Orchestration for agentic-trader v5.

Runs out of the box: with no FINNHUB_API_KEY it uses the SimulatedFeed so you
can watch the full machine work end to end. With a key it uses live Finnhub
data (after you've confirmed access via verify_endpoints.py).

    python main.py              # one paper session against whichever feed
    python main.py --loop       # continuous (real deployment shape)
"""

from __future__ import annotations

import sys
sys.stderr.write("=== MAIN.PY STARTED ===\n")
sys.stderr.flush()

import argparse
sys.stderr.write("DEBUG: argparse imported\n")
sys.stderr.flush()

import logging
sys.stderr.write("DEBUG: logging imported\n")
sys.stderr.flush()

import time
sys.stderr.write("DEBUG: time imported\n")
sys.stderr.flush()

sys.stderr.write("DEBUG: About to import from config\n")
sys.stderr.flush()
from config import START_EQUITY, TRADING_MODE
sys.stderr.write(f"DEBUG: config imported - START_EQUITY={START_EQUITY}, TRADING_MODE={TRADING_MODE}\n")
sys.stderr.flush()

sys.stderr.write("DEBUG: About to import from feed_layer\n")
sys.stderr.flush()
from feed_layer import SimulatedFeed, build_feed
sys.stderr.write("DEBUG: feed_layer imported\n")
sys.stderr.flush()

sys.stderr.write("DEBUG: About to import from intraday_engine\n")
sys.stderr.flush()
from intraday_engine import IntradayRiskEngine
sys.stderr.write("DEBUG: intraday_engine imported\n")
sys.stderr.flush()

sys.stderr.write("DEBUG: About to import from kill_switch\n")
sys.stderr.flush()
from kill_switch import KillSwitch
sys.stderr.write("DEBUG: kill_switch imported\n")
sys.stderr.flush()

sys.stderr.write("DEBUG: About to import from paper_broker\n")
sys.stderr.flush()
from paper_broker import PaperBroker
sys.stderr.write("DEBUG: paper_broker imported\n")
sys.stderr.flush()

sys.stderr.write("DEBUG: About to import from router\n")
sys.stderr.flush()
from router import SignalRouter
sys.stderr.write("DEBUG: router imported\n")
sys.stderr.flush()

sys.stderr.write("DEBUG: About to import from swing_engine\n")
sys.stderr.flush()
from swing_engine import SwingRiskEngine
sys.stderr.write("DEBUG: swing_engine imported\n")
sys.stderr.flush()

sys.stderr.write("DEBUG: About to import from trade_logger\n")
sys.stderr.flush()
from trade_logger import TradeLogger
sys.stderr.write("DEBUG: trade_logger imported\n")
sys.stderr.flush()

sys.stderr.write("DEBUG: About to import from models\n")
sys.stderr.flush()
from models import System
sys.stderr.write("DEBUG: models imported\n")
sys.stderr.flush()

sys.stderr.write("=== ALL IMPORTS COMPLETE ===\n")
sys.stderr.flush()

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("main")

# Your scan universe. In production this comes from your discovery scan.
UNIVERSE = ["HNRG", "TPL", "IX", "KARD", "AAPL", "MSFT", "NVDA", "AMD", "F", "T"]


def build():
    sys.stderr.write("DEBUG: build() called\n")
    sys.stderr.flush()
    
    try:
        sys.stderr.write("DEBUG: Calling build_feed()\n")
        sys.stderr.flush()
        feed = build_feed(UNIVERSE)
        sys.stderr.write(f"DEBUG: feed created: {type(feed).__name__}\n")
        sys.stderr.flush()
    except Exception as e:
        sys.stderr.write(f"ERROR in build_feed: {e}\n")
        sys.stderr.flush()
        import traceback
        traceback.print_exc(file=sys.stderr)
        raise
    
    try:
        sys.stderr.write("DEBUG: Creating PaperBroker\n")
        sys.stderr.flush()
        broker = PaperBroker(START_EQUITY)
        sys.stderr.write("DEBUG: PaperBroker created\n")
        sys.stderr.flush()
    except Exception as e:
        sys.stderr.write(f"ERROR in PaperBroker: {e}\n")
        sys.stderr.flush()
        import traceback
        traceback.print_exc(file=sys.stderr)
        raise
    
    try:
        sys.stderr.write("DEBUG: Creating TradeLogger\n")
        sys.stderr.flush()
        logger = TradeLogger()
        sys.stderr.write("DEBUG: TradeLogger created\n")
        sys.stderr.flush()
    except Exception as e:
        sys.stderr.write(f"ERROR in TradeLogger: {e}\n")
        sys.stderr.flush()
        import traceback
        traceback.print_exc(file=sys.stderr)
        raise
    
    try:
        sys.stderr.write("DEBUG: Creating KillSwitch\n")
        sys.stderr.flush()
        kill = KillSwitch(feed, broker)
        sys.stderr.write("DEBUG: KillSwitch created\n")
        sys.stderr.flush()
    except Exception as e:
        sys.stderr.write(f"ERROR in KillSwitch: {e}\n")
        sys.stderr.flush()
        import traceback
        traceback.print_exc(file=sys.stderr)
        raise
    
    try:
        sys.stderr.write("DEBUG: Creating SwingRiskEngine\n")
        sys.stderr.flush()
        swing = SwingRiskEngine(feed, broker, kill, logger)
        sys.stderr.write("DEBUG: SwingRiskEngine created\n")
        sys.stderr.flush()
    except Exception as e:
        sys.stderr.write(f"ERROR in SwingRiskEngine: {e}\n")
        sys.stderr.flush()
        import traceback
        traceback.print_exc(file=sys.stderr)
        raise
    
    try:
        sys.stderr.write("DEBUG: Creating IntradayRiskEngine\n")
        sys.stderr.flush()
        intraday = IntradayRiskEngine(feed, broker, kill, logger)
        sys.stderr.write("DEBUG: IntradayRiskEngine created\n")
        sys.stderr.flush()
    except Exception as e:
        sys.stderr.write(f"ERROR in IntradayRiskEngine: {e}\n")
        sys.stderr.flush()
        import traceback
        traceback.print_exc(file=sys.stderr)
        raise
    
    try:
        sys.stderr.write("DEBUG: Registering price loss handler\n")
        sys.stderr.flush()
        kill.register_price_loss_handler(System.INTRADAY, intraday.flatten_all)
        sys.stderr.write("DEBUG: Price loss handler registered\n")
        sys.stderr.flush()
    except Exception as e:
        sys.stderr.write(f"ERROR in register_price_loss_handler: {e}\n")
        sys.stderr.flush()
        import traceback
        traceback.print_exc(file=sys.stderr)
        raise
    
    try:
        sys.stderr.write("DEBUG: Creating SignalRouter\n")
        sys.stderr.flush()
        router = SignalRouter({System.SWING: swing, System.INTRADAY: intraday})
        sys.stderr.write("DEBUG: SignalRouter created\n")
        sys.stderr.flush()
    except Exception as e:
        sys.stderr.write(f"ERROR in SignalRouter: {e}\n")
        sys.stderr.flush()
        import traceback
        traceback.print_exc(file=sys.stderr)
        raise
    
    sys.stderr.write("DEBUG: build() complete, returning all components\n")
    sys.stderr.flush()
    return feed, broker, logger, kill, swing, intraday, router


def one_cycle(feed, kill, swing, intraday, router) -> None:
    kill.check_emergencies()
    for sig in feed.get_signals():
        router.route(sig)
    intraday.evaluate_watchlist()
    swing.manage_open_positions()
    intraday.manage_open_positions()


def run(loop: bool, cycles: int = 40) -> None:
    sys.stderr.write("DEBUG: run() called\n")
    sys.stderr.flush()
    
    try:
        sys.stderr.write("DEBUG: Calling build()\n")
        sys.stderr.flush()
        feed, broker, logger, kill, swing, intraday, router = build()
        sys.stderr.write("DEBUG: build() returned successfully\n")
        sys.stderr.flush()
    except Exception as e:
        sys.stderr.write(f"FATAL: Failed to build components: {e}\n")
        sys.stderr.flush()
        import traceback
        traceback.print_exc(file=sys.stderr)
        raise
    
    log.info("agentic-trader v5 starting in %s mode", TRADING_MODE)
    sys.stderr.write(f"DEBUG: Starting main loop - loop={loop}, TRADING_MODE={TRADING_MODE}\n")
    sys.stderr.flush()

    sim = isinstance(feed, SimulatedFeed)
    n = 10_000_000 if loop else cycles
    sys.stderr.write(f"DEBUG: Loop config - sim={sim}, n={n}\n")
    sys.stderr.flush()
    
    for i in range(n):
        try:
            sys.stderr.write(f"DEBUG: Cycle {i} starting\n")
            sys.stderr.flush()
            one_cycle(feed, kill, swing, intraday, router)
            sys.stderr.write(f"DEBUG: Cycle {i} complete\n")
            sys.stderr.flush()
        except Exception as e:
            sys.stderr.write(f"ERROR: Cycle {i} failed: {e}\n")
            sys.stderr.flush()
            import traceback
            traceback.print_exc(file=sys.stderr)
            log.error("Cycle %d failed: %s", i, e, exc_info=True)
            if not loop:
                raise
            time.sleep(5)  # backoff before retry
            continue
        
        if sim:
            feed.step_prices()      # advance synthetic market
        if loop:
            time.sleep(5)
    
    sys.stderr.write("DEBUG: Loop complete, flattening positions\n")
    sys.stderr.flush()
    # End of (simulated) session: flatten intraday, keep swing overnight.
    intraday.flatten_all("session end")
    logger.print_scorecard(broker)
    sys.stderr.write("DEBUG: Session complete\n")
    sys.stderr.flush()


if __name__ == "__main__":
    sys.stderr.write("DEBUG: Entering main block\n")
    sys.stderr.flush()
    
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true", help="run continuously (deploy shape)")
    args = ap.parse_args()
    
    sys.stderr.write(f"DEBUG: Args parsed - loop={args.loop}\n")
    sys.stderr.flush()
    
    try:
        sys.stderr.write("DEBUG: Calling run()\n")
        sys.stderr.flush()
        run(loop=args.loop)
        sys.stderr.write("DEBUG: run() completed successfully\n")
        sys.stderr.flush()
    except Exception as e:
        sys.stderr.write(f"FATAL: {e}\n")
        sys.stderr.flush()
        import traceback
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
    
    sys.stderr.write("DEBUG: Main block complete\n")
    sys.stderr.flush()
