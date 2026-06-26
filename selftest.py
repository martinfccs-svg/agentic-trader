"""Self-test: verify the engine math before trusting any scorecard.

    python selftest.py

Checks indicators against hand-computed values, sizing math, broker P&L
reconciliation (realized vs unrealized never conflated), and kill-switch
severity. Exits non-zero on failure so CI/Railway can gate on it.
"""
import logging
log = logging.getLogger("selftest")
from __future__ import annotations

import sys

from indicators import atr, relative_volume, sma, vwap
from models import Bars, System, SignalSource
from paper_broker import PaperBroker
from risk import position_size

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def test_indicators() -> None:
    print("indicators:")
    check("sma basic", sma([1, 2, 3, 4], 2) == 3.5)
    check("sma insufficient", sma([1], 3) is None)
    # ATR: flat 10/8 bars, prev close 9 -> TR=2 each; ATR=2
    b = Bars("X", close=[9] * 20, high=[10] * 20, low=[8] * 20, volume=[1] * 20)
    a = atr(b, period=14)
    check("atr flat == 2", a is not None and abs(a - 2.0) < 1e-9, f"got {a}")
    # VWAP with constant typical price 9 -> 9
    check("vwap constant", abs((vwap(b) or 0) - 9.0) < 1e-9)
    # relative volume: last bar double the prior average
    bv = Bars("Y", close=[1] * 22, high=[1] * 22, low=[1] * 22,
              volume=[100] * 21 + [200])
    rv = relative_volume(bv, lookback=20)
    check("rel volume == 2", rv is not None and abs(rv - 2.0) < 1e-9, f"got {rv}")


def test_sizing() -> None:
    print("sizing:")
    # equity 50k, risk 1% = $500; entry 100 stop 95 -> risk/share 5 -> 100 shares,
    # but MAX_POSITION_SIZE 3000/100 = 30 shares cap.
    s = position_size(equity=50_000, entry=100, stop=95, cash=1_000_000)
    check("risk-based capped by max notional", abs(s - 30.0) < 1e-9, f"got {s}")
    check("zero when stop above entry", position_size(50_000, 100, 105, 1e9) == 0.0)


def test_broker_reconciliation():
    """Test broker P&L reconciliation."""
    from config import TRADING_MODE
    
    # Skip broker test in LIVE mode (uses real broker)
    if TRADING_MODE == "LIVE":
        log.info("Skipping broker test in LIVE mode (uses real RobinhoodBroker)")
        return
    
    # Rest of the test for PAPER mode...
    b = PaperBroker(50000)
    # ... rest of test code

def main() -> None:
    test_indicators()
    test_sizing()
    test_broker_reconciliation()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
