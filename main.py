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

Safety architecture (each interlock maps to a real incident):
  startup:  HALT latch file          - a breach yesterday must not be re-traded
                                       today just because Railway restarted us
            reconcile_at_startup()   - broker is source of truth; orphan
                                       positions OR resting orders => HALT
  per-cycle: equity circuit breaker  - BROKER equity vs persisted baseline;
                                       2026-07-07 the bot ran 17 min at
                                       negative equity reporting open=0
             book-vs-broker audit    - broker holding something we don't
                                       track => flatten-safe HALT
             contained cycle errors  - one bad symbol backs off, never kills
                                       the process (2026-07-06 crash loop)
  shutdown:  flatten + scorecard each wrapped; shutdown always completes
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import datetime, timezone

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

# ---------------------------------------------------------------------------
# Persistent safety state. Railway restarts the container on exit, so any
# halt decision must survive the restart — otherwise "halt" just means
# "trade again in 30 seconds with amnesia" (the 2026-07-07 failure shape).
# ---------------------------------------------------------------------------
STATE_DIR = os.environ.get("STATE_DIR", "state")
HALT_FILE = os.path.join(STATE_DIR, "HALTED")
BASELINE_FILE = os.path.join(STATE_DIR, "equity_baseline.json")

# Circuit breaker: halt everything if broker-reported equity falls this far
# below the persisted baseline. Override via config if defined there.
try:
    from config import EQUITY_FLOOR_FRACTION  # type: ignore
except ImportError:
    EQUITY_FLOOR_FRACTION = 0.90

# Book-vs-broker audit cadence (cycles). ~1/min at a 5s scan interval.
AUDIT_EVERY_N_CYCLES = 12


def engage_halt_latch(reason: str) -> None:
    """Write the halt latch. The bot refuses to trade on any subsequent boot
    until a human deletes the file. This is deliberate friction: whatever
    tripped it lost (or nearly lost) money, and a restart loop must not be
    able to resume trading on its own."""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(HALT_FILE, "w") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()}\n{reason}\n")
    except Exception as e:  # noqa: BLE001
        log.critical("could not write halt latch (%s) — reason was: %s", e, reason)
    log.critical("HALT LATCH ENGAGED: %s — delete %s to re-enable trading",
                 reason, HALT_FILE)


def halt_latched() -> str | None:
    """Return the latch contents if a previous run halted, else None."""
    try:
        with open(HALT_FILE) as f:
            return f.read().strip()
    except FileNotFoundError:
        return None
    except Exception as e:  # noqa: BLE001
        return f"(unreadable halt file: {e})"


def load_or_set_equity_baseline(current_equity: float) -> float:
    """The circuit breaker must compare against a PERSISTED baseline. Using
    broker.start_equity alone re-baselines after every restart, so a crash
    that followed a big loss would quietly redefine the loss as 'normal'."""
    try:
        with open(BASELINE_FILE) as f:
            return float(json.load(f)["baseline"])
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        log.error("could not read equity baseline (%s); re-seeding", e)
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(BASELINE_FILE, "w") as f:
            json.dump({"baseline": current_equity,
                       "set_at": datetime.now(timezone.utc).isoformat()}, f)
    except Exception as e:  # noqa: BLE001
        log.error("could not persist equity baseline: %s", e)
    return current_equity


class SafetyHalt(SystemExit):
    """Raised when a safety interlock trips. Subclasses SystemExit so the
    per-cycle `except Exception` retry wrapper can NEVER swallow it — it
    propagates straight to the shutdown flatten in run()'s finally block."""


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


def audit_book(broker, intraday, equity_baseline: float, n: int) -> None:
    """Runtime interlocks, checked every cycle.

    1. EQUITY CIRCUIT BREAKER (every cycle): broker-reported equity below the
       persisted floor => flatten intraday, engage latch, halt. On 2026-07-07
       the account went from +98.6k to -3k and the bot cycled happily for 17
       minutes because every internal book said flat.
    2. BOOK-VS-BROKER AUDIT (every AUDIT_EVERY_N_CYCLES): compare symbols.
       - broker holds something we don't track  => DANGEROUS (untracked
         exposure, the 07-07 mechanism) => latch + halt.
       - we track something the broker doesn't  => usually benign: a bracket
         leg filled between cycles; manage_open_positions()' 404 path will
         reconcile it. Log and continue.
    """
    if not hasattr(broker, "broker_position_symbols"):
        # PaperBroker: internal books ARE the broker; only the floor applies.
        eq = broker.equity
    else:
        eq = broker.equity   # AlpacaBroker: broker-reported (or loud-stale)

    floor = equity_baseline * EQUITY_FLOOR_FRACTION
    if eq < floor:
        log.critical("equity %.2f below floor %.2f (baseline %.2f) — "
                     "flattening intraday and HALTING", eq, floor, equity_baseline)
        try:
            intraday.flatten_all("equity floor breach")
        except Exception as e:  # noqa: BLE001
            log.critical("flatten during equity halt raised: %s — check the "
                         "Alpaca dashboard NOW", e)
        engage_halt_latch(f"equity {eq:.2f} < floor {floor:.2f}")
        raise SafetyHalt("equity floor breached")

    if hasattr(broker, "broker_position_symbols") and n % AUDIT_EVERY_N_CYCLES == 0:
        try:
            broker_syms = broker.broker_position_symbols()
        except Exception as e:  # noqa: BLE001
            log.error("book audit skipped this cycle (broker API error): %s", e)
            return
        local_syms = set(broker.positions)
        untracked = broker_syms - local_syms
        stale = local_syms - broker_syms
        if untracked:
            log.critical("BOOK DESYNC: broker holds %s not in local book — "
                         "untracked exposure, HALTING", sorted(untracked))
            engage_halt_latch(f"untracked broker positions: {sorted(untracked)}")
            raise SafetyHalt("book desync: untracked broker positions")
        if stale:
            log.warning("book audit: local tracks %s but broker is flat "
                        "(bracket leg likely filled) — manage will reconcile",
                        sorted(stale))


def cycle(feed, broker, kill, swing, intraday, meanrev, xsect, router, scanner, engines,
          n: int = 0, force_market_open=False, equity_baseline: float | None = None):
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

    # Safety interlocks AFTER manage (so a leg fill this cycle is already
    # reconciled) and BEFORE the equity log (so the number we print is the
    # number we judged). SafetyHalt bypasses the retry wrapper by design.
    if equity_baseline is not None:
        audit_book(broker, intraday, equity_baseline, n)

    # Honest P&L: realized and unrealized logged separately, per system.
    for system in System:
        n_open = sum(1 for p in broker.positions.values() if p.system is system)
        log.info("  %-8s realized=%.2f unrealized=%.2f open=%d",
                 system.value, broker.realized_pnl[system],
                 broker.unrealized_pnl(system), n_open)
    log.info("  equity=%.2f | === cycle %d complete ===", broker.equity, n)


def run(loop: bool, cycles: int = 40):
    # ---- Halt latch: a breach in a previous run blocks THIS run. ----------
    latched = halt_latched()
    if latched:
        log.critical("HALT LATCH is set — refusing to trade.\n%s\n"
                     "Resolve the cause, then delete %s to re-enable.",
                     latched, HALT_FILE)
        return

    feed, broker, logger, kill, swing, intraday, meanrev, xsect, router, scanner, engines = build()
    startup_banner()
    log.info("agentic-trader v6 | mode=%s | broker live-armed=%s",
             TRADING_MODE, live_money_armed())

    # Broker is the source of truth at boot. After each 2026-07-06 crash the
    # bot restarted with an empty tracker while Alpaca still held shares and
    # live bracket legs -> re-bought TSLA (72 shares) and 404'd on manage.
    # Reconcile now also sweeps RESTING ORDERS: on 2026-07-07 an unfilled
    # entry survived a crash, filled after the restart, and took the account
    # negative while the bot reported open=0. Re-adopt bot-created positions;
    # cancel stale bot entries; HALT on anything unrecognized.
    if hasattr(broker, "reconcile_at_startup"):
        try:
            orphans = broker.reconcile_at_startup()
        except Exception as e:  # noqa: BLE001
            log.critical("startup reconciliation failed (%s) — HALTING; "
                         "cannot trade without knowing broker state", e)
            return
        if orphans:
            log.critical("ORPHANs at broker: %s — HALTING. Resolve in the "
                         "Alpaca dashboard, then restart.", orphans)
            engage_halt_latch(f"orphans at broker: {orphans}")
            return

    # Circuit-breaker baseline persists across restarts; see the helper's
    # docstring for why broker.start_equity alone is not safe.
    equity_baseline = load_or_set_equity_baseline(broker.equity)
    log.info("equity baseline=%.2f floor=%.2f (%.0f%%)",
             equity_baseline, equity_baseline * EQUITY_FLOOR_FRACTION,
             EQUITY_FLOOR_FRACTION * 100)

    sim = isinstance(feed, SimulatedFeed)
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
                # SafetyHalt subclasses SystemExit, so it is NOT caught here:
                # interlock trips go straight to the shutdown flatten.
                try:
                    cycle(feed, broker, kill, swing, intraday, meanrev, xsect, router, scanner, engines,
                          n=i, force_market_open=sim, equity_baseline=equity_baseline)
                    consecutive_failures = 0
                except KeyboardInterrupt:
                    raise
                except Exception as e:  # noqa: BLE001
                    consecutive_failures += 1
                    backoff = min(2 ** consecutive_failures, 60)
                    log.error("cycle %d failed (%d consecutive): %s — "
                              "backing off %ds", i, consecutive_failures, e,
                              backoff, exc_info=True)
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        log.critical("%d consecutive cycle failures — "
                                     "something is structurally broken; "
                                     "latching halt and exiting to shutdown "
                                     "flatten", consecutive_failures)
                        engage_halt_latch(
                            f"{consecutive_failures} consecutive cycle failures")
                        break
                    time.sleep(backoff)
                if sim:
                    feed.step_prices()
                time.sleep(SCAN_INTERVAL_SECS)
        else:
            for i in range(1, cycles + 1):
                cycle(feed, broker, kill, swing, intraday, meanrev, xsect, router, scanner, engines,
                      n=i, force_market_open=sim, equity_baseline=equity_baseline)
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


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true")
    run(loop=ap.parse_args().loop)
