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


def test_trade_record_and_mc():
    print("trade record + monte carlo:")
    from trade_record import TradeRecord
    from montecarlo import run_monte_carlo, MIN_TRUSTWORTHY
    # entry 100, stop 95 -> risk/share 5; 10 shares -> $50 risk. Exit 115 -> +150 pnl -> +3R
    tr = TradeRecord.build("X", "swing", "trend", 0, 1, 100, 115, 10, 95, 150.0)
    check("R-multiple = +3R", abs(tr.r_multiple - 3.0) < 1e-9, f"got {tr.r_multiple}")
    check("initial_risk = 50", abs(tr.initial_risk - 50.0) < 1e-9)
    # small sample must be flagged untrustworthy
    few = [TradeRecord.build("X","swing","trend",0,1,100,110,10,95,100.0) for _ in range(5)]
    r_few = run_monte_carlo(few, start_equity=50_000, runs=200)
    check("small sample flagged not trustworthy", r_few.trustworthy is False)
    # all-winning trades -> zero ruin
    wins = [TradeRecord.build("X","swing","trend",0,1,100,110,10,95,100.0) for _ in range(40)]
    r_win = run_monte_carlo(wins, start_equity=50_000, runs=500)
    check("40 winners -> trustworthy", r_win.trustworthy is True)
    check("40 winners -> ~0 ruin", r_win.prob_ruin == 0.0)
    check("40 winners -> final > start", r_win.median_final > 50_000)


def test_new_strategies():
    print("new strategies:")
    from indicators import rsi, trailing_return
    # RSI of a strictly rising series -> 100 (no losses)
    check("rsi all-up = 100", rsi(list(range(1, 40)), 14) == 100.0)
    # RSI of a strictly falling series -> ~0
    r_down = rsi(list(range(40, 1, -1)), 14)
    check("rsi all-down ~ 0", r_down is not None and r_down < 1.0, f"got {r_down}")
    # trailing return: 100 -> 120 over lookback = +20%
    check("trailing_return +20%",
          abs((trailing_return([100]*5 + [100, 110, 120], 2, 0) or 0) - 0.20) < 1e-9)
    # correlation: identical series -> r = +1
    from correlation import _pearson
    check("pearson identical = +1", abs((_pearson([1,2,3,4],[1,2,3,4]) or 0) - 1.0) < 1e-9)
    check("pearson opposite = -1", abs((_pearson([1,2,3,4],[4,3,2,1]) or 0) + 1.0) < 1e-9)


def test_backtest_no_lookahead():
    print("backtest no-lookahead:")
    from historical_feed import make_synthetic
    feed = make_synthetic(["AAA", "BBB"], days=200, seed=3)
    feed.set_cursor(50)
    bars = feed.get_daily_bars("AAA")
    # At cursor 50 the feed must expose exactly 51 bars (0..50) and NOTHING after.
    check("feed exposes only history up to cursor", bars is not None and len(bars.close) == 51,
          f"got {len(bars.close) if bars else None}")
    q = feed.get_quote("AAA")
    check("quote price = close at cursor", q is not None and q.price == bars.close[-1])
    # Advancing reveals exactly one more bar (no jumps, no future leakage).
    feed.advance()
    check("advance reveals one more bar", len(feed.get_daily_bars("AAA").close) == 52)


def main():
    test_indicators(); test_sizing(); test_broker(); test_live_gate()
    test_trade_record_and_mc(); test_new_strategies(); test_backtest_no_lookahead()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
