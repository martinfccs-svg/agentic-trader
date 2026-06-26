"""Self-test: verify engine math before trusting any scorecard or going live.

    python selftest.py

Indicators, sizing, broker P&L reconciliation, price-action scanner logic, and
the live-money gate (must be DISARMED by default). Exits non-zero on failure so
the Docker startup gates on it.
"""

from __future__ import annotations

import sys

from indicators import atr, opening_range_high, prior_high, relative_volume, sma, vwap
from models import Bars, System, SignalSource
from brokers import PaperBroker
from risk import position_size

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ok   {name}")
    else:
        FAIL += 1; print(f"  FAIL {name} {detail}")


def test_indicators():
    print("indicators:")
    check("sma", sma([1, 2, 3, 4], 2) == 3.5)
    b = Bars("X", close=[9]*20, high=[10]*20, low=[8]*20, volume=[1]*20)
    check("atr flat==2", abs((atr(b, 14) or 0) - 2.0) < 1e-9)
    check("vwap constant", abs((vwap(b) or 0) - 9.0) < 1e-9)
    bv = Bars("Y", close=[1]*22, high=[1]*22, low=[1]*22, volume=[100]*21 + [200])
    check("rel volume==2", abs((relative_volume(bv, 20) or 0) - 2.0) < 1e-9)
    bh = Bars("Z", close=list(range(10)), high=list(range(10)), low=[0]*10, volume=[1]*10)
    check("prior_high excludes last", prior_high(bh, 5) == 8.0)  # highs 4..8, excl last(9)
    check("opening_range_high", opening_range_high(bh, 3) == 2.0)


def test_sizing():
    print("sizing:")
    s = position_size(50_000, 100, 95, 1e9)
    check("risk-based capped by max notional", abs(s - 30.0) < 1e-9, f"got {s}")
    check("zero when stop>=entry", position_size(50_000, 100, 105, 1e9) == 0.0)


def test_broker():
    print("broker reconciliation:")
    b = PaperBroker(50_000)
    b.buy("ZZZ", 100, 10.0, System.SWING, SignalSource.TREND, 9.5)
    check("realized 0 while open", b.realized_pnl[System.SWING] == 0.0)
    b.mark("ZZZ", 12.0)
    check("unrealized reflects mark", abs(b.unrealized_pnl(System.SWING) - 199.5) < 1.0)
    check("mark does NOT move realized", b.realized_pnl[System.SWING] == 0.0)
    r = b.sell("ZZZ", 12.0)
    check("realized recorded on close", r > 0 and b.realized_pnl[System.SWING] == r)
    check("no unrealized after close", b.unrealized_pnl(System.SWING) == 0.0)


def test_live_gate():
    print("safety gate:")
    from config import live_money_armed
    # With no env set, real money MUST be disarmed.
    check("live money DISARMED by default", live_money_armed() is False)


def main():
    test_indicators(); test_sizing(); test_broker(); test_live_gate()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
