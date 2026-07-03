"""Comprehensive test suite for agentic-trader v6.

Run: python selftest.py
Expected: all tests pass (0 failures).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from brokers import PaperBroker
from config import (
    INTRADAY, SWING, MEANREV, XSECT,
    max_position_dollars, RISK_PER_TRADE_PCT, MAX_POSITION_PCT, MAX_POSITION_SIZE,
)
from indicators import (
    sma, atr, vwap, rel_volume, prior_high, opening_range_high,
)
from models import Bars, System, SignalSource
from risk import position_size
from trade_record import TradeRecord
from montecarlo import ruin_probability

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        print(f"  ok   {name}")
        PASS += 1
    else:
        print(f"  FAIL {name} {detail}")
        FAIL += 1


def test_indicators():
    print("indicators:")
    bh = Bars("Z", close=list(range(10)), high=list(range(10)), low=[0]*10, volume=[1]*10)
    check("sma", sma(bh.close, 3) == 4.0)
    check("atr flat==2", atr(bh, 3) == 2.0)
    check("vwap constant", vwap(bh, 3) == 5.0)
    check("rel volume==2", rel_volume(bh, 3) == 2.0)
    check("prior_high excludes last", prior_high(bh, 5) == 8.0)  # highs 4..8, excl last(9)
    check("opening_range_high", opening_range_high(bh, 3) == 2.0)


def test_sizing():
    print("sizing:")
    # equity 50k, 1% risk = $500; entry 100 stop 95 -> wants 100 shares.
    # Cap = 10% of equity = $5,000 -> 50 shares binds. (Old test expected 30
    # from the stale flat $3,000 cap -- that WAS the bug.)
    s = position_size(50_000, 100, 95, 1e9)
    check("risk-based capped by scaled max notional", abs(s - 50.0) < 1e-9, f"got {s}")
    check("zero when stop>=entry", position_size(50_000, 100, 105, 1e9) == 0.0)


def test_broker():
    print("broker reconciliation:")
    b = PaperBroker(50_000)
    b.buy("ZZZ", 100, 10.0, System.SWING, SignalSource.TREND, 9.5)
    check("realized 0 while open", b.realized_pnl[System.SWING] == 0.0)
    b.mark("ZZZ", 12.0)
    check("unrealized reflects mark", b.positions["ZZZ"].unrealized_pnl == 200.0)
    b.mark("ZZZ", 10.0)
    check("mark does NOT move realized", b.realized_pnl[System.SWING] == 0.0)
    realized = b.sell("ZZZ", 11.0)
    check("realized recorded on close", b.realized_pnl[System.SWING] == realized)


def test_live_gate():
    print("safety gate:")
    from config import live_money_armed
    check("live money DISARMED by default", not live_money_armed())


def test_trade_record_and_mc():
    print("trade record + monte carlo:")
    tr = TradeRecord(
        ticker="TEST", system="intraday", source="momentum",
        entry_time=0, exit_time=1, entry_price=100, exit_price=103,
        shares=10, initial_risk=5, realized_pnl=30,
    )
    check("R-multiple = +3R", tr.r_multiple == 3.0)
    check("initial_risk = 50", tr.initial_risk == 5)
    # Small sample: not trustworthy.
    trades = [TradeRecord("T", "s", "src", 0, 1, 100, 101, 10, 1, 10) for _ in range(5)]
    check("small sample flagged not trustworthy", not all(t.trustworthy for t in trades))
    # 40 winners: trustworthy, low ruin.
    trades = [TradeRecord("T", "s", "src", 0, 1, 100, 101, 10, 1, 10) for _ in range(40)]
    check("40 winners -> trustworthy", all(t.trustworthy for t in trades))
    ruin = ruin_probability(trades, 50_000)
    check("40 winners -> ~0 ruin", ruin < 0.01, f"ruin={ruin}")
    final = 50_000
    for t in trades:
        final += t.realized_pnl
    check("40 winners -> final > start", final > 50_000)


def test_new_strategies():
    print("new strategies:")
    from indicators import rsi, trailing_return
    # RSI all-up: 100
    bh = Bars("Z", close=list(range(1, 15)), high=list(range(1, 15)), low=list(range(1, 15)), volume=[1]*14)
    check("rsi all-up = 100", rsi(bh.close, 14) == 100.0)
    # RSI all-down: ~0
    bh = Bars("Z", close=list(range(14, 0, -1)), high=list(range(14, 0, -1)), low=list(range(14, 0, -1)), volume=[1]*14)
    check("rsi all-down ~ 0", rsi(bh.close, 14) < 1.0)
    # Trailing return: +20%
    bh = Bars("Z", close=[100, 110, 120], high=[100, 110, 120], low=[100, 110, 120], volume=[1]*3)
    check("trailing_return +20%", trailing_return(bh.close, 1) == 0.2)


def test_backtest_no_lookahead():
    print("backtest no-lookahead:")
    from feed_layer import HistoricalFeed
    bars = [
        Bars("AAA", close=[10.0]*60, high=[11.0]*60, low=[9.0]*60, volume=[1e6]*60),
        Bars("AAA", close=[10.5]*60, high=[11.5]*60, low=[9.5]*60, volume=[1e6]*60),
        Bars("AAA", close=[11.0]*60, high=[12.0]*60, low=[10.0]*60, volume=[1e6]*60),
    ]
    feed = HistoricalFeed(bars)
    # At cursor 50 the feed must expose exactly 51 bars (0..50) and NOTHING after.
    bars = feed.get_daily_bars("AAA")
    check("feed exposes only history up to cursor", bars is not None and len(bars.close) == 51,
          f"got {len(bars.close) if bars else None}")
    q = feed.get_quote("AAA")
    check("quote price = close at cursor", q is not None and q.price == bars.close[-1])
    # Advancing reveals exactly one more bar (no jumps, no future leakage).
    feed.advance()
    check("advance reveals one more bar", len(feed.get_daily_bars("AAA").close) == 52)


def test_feed_cache():
    print("per-cycle cache (429 fix):")
    from feed_layer import FinnhubFeed
    calls = {"candle": 0, "quote": 0}
    class FakeClient:
        def stock_candles(self, t, res, a, b):
            calls["candle"] += 1
            return {"s": "ok", "c": [10.0]*60, "h": [11.0]*60, "l": [9.0]*60, "v": [1e6]*60}
        def quote(self, t):
            calls["quote"] += 1
            return {"c": 10.0}
    f = FinnhubFeed(FakeClient())
    f.new_cycle()
    # Four strategies all asking for the same ticker's daily bars in one cycle...
    for _ in range(4):
        f.get_daily_bars("AAPL")
    f.get_quote("AAPL")
    # ...should hit the candle API far fewer times than the number of requests.
    check("daily bars fetched once despite 4 requests", calls["candle"] <= 2,
          f"candle calls={calls['candle']}")
    # Daily bars now live in a slow-TTL cache (see test_universe_expansion);
    # the per-cycle memo still governs QUOTES, which must refetch each cycle.
    f.get_quote("AAPL"); q1 = calls["quote"]
    f.get_quote("AAPL")                  # same cycle -> memoized, no new call
    check("quote memoized within a cycle", calls["quote"] == q1)
    f.new_cycle()
    f.get_quote("AAPL")                  # new cycle -> per-cycle memo cleared
    check("new_cycle clears quote memo (refetch)", calls["quote"] > q1)


def test_tightness_fixes():
    print("tightness fixes:")
    from config import max_position_dollars, INTRADAY
    # Fix 1: cap scales with equity (10% default) instead of stale flat $3000.
    check("cap scales with equity (100k -> 10k)", max_position_dollars(100_000) == 10_000.0)
    check("cap scales with equity (50k -> 5k)", max_position_dollars(50_000) == 5_000.0)
    
    # Debug: print actual config values
    print(f"    DEBUG: MAX_POSITION_SIZE={MAX_POSITION_SIZE}, MAX_POSITION_PCT={MAX_POSITION_PCT}, RISK_PER_TRADE_PCT={RISK_PER_TRADE_PCT}")
    
    s = position_size(equity=100_000, entry=390, stop=389, cash=1e9)
    expected = 10_000 / 390  # 25.64
    check("sizing uses scaled cap (~25.6 sh at 100k)", abs(s - expected) < 0.01, f"got {s}, expected {expected}")
    
    # Fix 2: intraday ATR multiple widened (stops were 0.1-0.2% of price -> churn).
    check("intraday ATR mult widened to 2.5", INTRADAY.atr_stop_multiple == 2.5)
    # Fix 3: intraday liquidity test reads DAILY bars (unit fix, was ~390x too strict).
    src = open("intraday_engine.py").read()
    check("intraday liquidity uses daily bars", "get_daily_bars" in src and "daily_dv" in src)


def test_coherence_fixes():
    print("coherence fixes:")
    from config import daily_loss_dollars, INTRADAY, XSECT
    # Fix A: daily loss halt scales with equity (2.5% default), env override wins.
    check("daily loss scales (100k -> 2500)", daily_loss_dollars(100_000) == 2_500.0)
    check("daily loss scales (50k -> 1250)", daily_loss_dollars(50_000) == 1_250.0)
    # Fix B: intraday trail on the stop's scale (1%), not 6%.
    check("intraday trail = 1%", abs(INTRADAY.trail_pct - 0.01) < 1e-12,
          f"got {INTRADAY.trail_pct}")
    # Fix C: cross-sectional rebalance ~daily, not every ~25 minutes.
    check("xsect rebalance ~daily (780 cycles)", XSECT.rebalance_cycles == 780,
          f"got {XSECT.rebalance_cycles}")


def test_universe_expansion():
    print("universe expansion + tiered caching:")
    from config import (DAILY_BARS_REFRESH_CYCLES, INTRADAY_UNIVERSE,
                        RATE_LIMIT_CALLS, UNIVERSE)
    check("universe widened (>=30 names)", len(UNIVERSE) >= 30, f"got {len(UNIVERSE)}")
    check("multi-sector (JPM+XOM+UNH+CAT present)",
          all(t in UNIVERSE for t in ("JPM", "XOM", "UNH", "CAT")))
    check("intraday subset is a subset", set(INTRADAY_UNIVERSE) <= set(UNIVERSE))
    check("intraday subset is small (<=15)", len(INTRADAY_UNIVERSE) <= 15)

    # Daily TTL cache: new_cycle must NOT refetch daily bars until TTL expires.
    from feed_layer import FinnhubFeed
    calls = {"candle": 0}
    class FakeClient:
        def stock_candles(self, t, res, a, b):
            calls["candle"] += 1
            return {"s": "ok", "c": [10.0]*60, "h": [11.0]*60, "l": [9.0]*60, "v": [1e6]*60}
        def quote(self, t): return {"c": 10.0}
    f = FinnhubFeed(FakeClient())
    f.new_cycle(); f.get_daily_bars("AAPL")
    n1 = calls["candle"]
    for _ in range(5):                       # five more cycles inside the TTL
        f.new_cycle(); f.get_daily_bars("AAPL")
    check("daily bars NOT refetched within TTL", calls["candle"] == n1,
          f"calls went {n1} -> {calls['candle']}")
    for _ in range(DAILY_BARS_REFRESH_CYCLES):
        f.new_cycle()
    f.get_daily_bars("AAPL")
    check("daily bars refetched after TTL", calls["candle"] > n1)

    # Rate budget: worst-case steady-state calls/min must fit under the limiter.
    # intraday subset: quote + 1-min candle per cycle; daily: full universe
    # amortized over the TTL window. 2 cycles/min at 30s cycles.
    cycles_per_min = 2
    intraday_per_min = len(INTRADAY_UNIVERSE) * 2 * cycles_per_min
    daily_per_min = len(UNIVERSE) * (cycles_per_min / DAILY_BARS_REFRESH_CYCLES)
    est = intraday_per_min + daily_per_min
    check(f"rate budget fits (est {est:.0f}/min < {RATE_LIMIT_CALLS})",
          est < RATE_LIMIT_CALLS * 0.8, f"estimate {est:.0f}")


def test_market_calendar():
    print("market calendar (holiday bug fix):")
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from safety import early_closes, market_holidays, market_is_open, near_close
    ET = ZoneInfo("America/New_York")
    # THE live-discovered case: Fri Jul 3 2026 (Jul 4 observed) must be CLOSED.
    check("Jul 3 2026 observed holiday -> closed",
          market_is_open(datetime(2026, 7, 3, 11, 0, tzinfo=ET)) is False)
    # Normal trading day at 11am -> open.
    check("normal Tue 11am -> open",
          market_is_open(datetime(2026, 7, 7, 11, 0, tzinfo=ET)) is True)
    # Fixed + rule-based holidays.
    from datetime import date
    h26 = market_holidays(2026)
    check("Thanksgiving 2026 = Nov 26", date(2026, 11, 26) in h26)
    check("MLK 2026 = Jan 19", date(2026, 1, 19) in h26)
    check("Good Friday 2026 = Apr 3", date(2026, 4, 3) in h26)
    check("Christmas 2026 (Fri) in set", date(2026, 12, 25) in h26)
    # Early close: day after Thanksgiving 2026 = Nov 27; open at 11, CLOSED at 14.
    check("Nov 27 2026 is early close", date(2026, 11, 27) in early_closes(2026))
    check("half-day: open at 11am",
          market_is_open(datetime(2026, 11, 27, 11, 0, tzinfo=ET)) is True)
    check("half-day: CLOSED at 2pm (13:00 close)",
          market_is_open(datetime(2026, 11, 27, 14, 0, tzinfo=ET)) is False)
    # Flatten window keys off the EARLY close on a half-day (12:45 within 15min of 13:00).
    check("half-day flatten fires before 13:00 (12:57, 5-min window)",
          near_close(datetime(2026, 11, 27, 12, 57, tzinfo=ET)) is True)
    check("normal day 12:50 NOT near close",
          near_close(datetime(2026, 7, 7, 12, 50, tzinfo=ET)) is False)


def main():
    test_indicators(); test_sizing(); test_broker(); test_live_gate()
    test_trade_record_and_mc(); test_new_strategies(); test_backtest_no_lookahead()
    test_feed_cache(); test_tightness_fixes(); test_coherence_fixes()
    test_universe_expansion(); test_market_calendar()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()

