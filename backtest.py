"""Backtest harness — replay the real strategy code over history.

Reuses the live scanner, engines, broker, and risk logic against a
HistoricalFeed (no lookahead), and writes results in the SAME TradeRecord
format the live bot uses — so montecarlo.py and correlation.py consume the
output unchanged.

    python backtest.py --synthetic                  # runs on synthetic history
    python backtest.py --data ./history             # CSVs: TICKER.csv per name
    python backtest.py --synthetic --split 2023-07-01   # in-sample / out-of-sample

The --split flag is the anti-overfitting check: it reports performance before
and after the split date separately. If the strategy only works in-sample, you
have a curve-fit, not an edge.

Honest limitations (stated, not hidden):
  - Daily strategies only (trend, mean-reversion, cross-sectional). Intraday
    needs 1-min replay — a separate build.
  - Fills/stops evaluated at the daily CLOSE (no intrabar). A reasonable daily
    convention, but real fills will differ.
"""

from __future__ import annotations

import argparse
import logging

from brokers import PaperBroker
from config import UNIVERSE
from historical_feed import HistoricalFeed, load_csv_dir, make_synthetic
from intraday_engine import IntradayRiskEngine
from kill_switch import KillSwitch
from meanrev_engine import MeanReversionEngine
from models import System
from notifier import Notifier
from router import SignalRouter
from scanner import PriceActionScanner
from swing_engine import SwingRiskEngine
from trade_logger import TradeLogger
from trade_record import TradeRecord, TradeRecorder
from xsection import CrossSectionalMomentumEngine

logging.basicConfig(level=logging.WARNING)   # quiet; backtest prints its own report


def run_backtest(feed: HistoricalFeed, out_path: str = "backtest_trades.jsonl"):
    recorder = TradeRecorder(out_path)
    import datetime as _dt
    def _sim_clock():
        d = feed.current_date()
        if not d:
            return 0.0
        return _dt.datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=_dt.timezone.utc).timestamp()
    broker = PaperBroker(recorder=recorder, clock=_sim_clock)
    logger = TradeLogger(path="backtest_signals.jsonl")
    notifier = Notifier()
    kill = KillSwitch(feed, broker)
    swing = SwingRiskEngine(feed, broker, kill, logger, notifier)
    intraday = IntradayRiskEngine(feed, broker, kill, logger, notifier)   # no-op (no 1-min bars)
    meanrev = MeanReversionEngine(feed, broker, kill, logger, notifier)
    xsect = CrossSectionalMomentumEngine(feed, broker, kill, logger, notifier, UNIVERSE)
    router = SignalRouter({System.SWING: swing, System.INTRADAY: intraday,
                           System.MEANREV: meanrev, System.XSECTMOM: xsect})
    scanner = PriceActionScanner(feed, UNIVERSE)
    engines = [swing, meanrev, xsect]

    feed.set_cursor(30)                      # warm-up for indicators
    days = 0
    while feed.has_next():
        broker.reset_daily()                 # daily-loss limit is per simulated day
        for sig in scanner.scan_swing() + scanner.scan_meanrev():
            router.route(sig)
        # One backtest cycle = one DAY, so the live cycle-based cadence (tuned
        # for 30s cycles) doesn't apply here. Rebalance weekly in day units.
        if days % 5 == 0:
            xsect.rebalance()
        for e in engines:
            e.manage_open_positions()
        feed.advance()
        days += 1

    # Close out everything at the final close.
    for ticker in list(broker.positions):
        q = feed.get_quote(ticker)
        if q:
            broker.sell(ticker, q.price)
    return out_path, days


# ----------------------------------------------------------------------------
# Reporting
# ----------------------------------------------------------------------------

def _metrics(trades: list[TradeRecord]) -> dict:
    if not trades:
        return {"trades": 0}
    pnls = [t.realized_pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gl = abs(sum(losses))
    curve = peak = mdd = 0.0
    for p in pnls:
        curve += p; peak = max(peak, curve); mdd = min(mdd, curve - peak)
    return {
        "trades": len(trades),
        "win_rate": len(wins) / len(trades),
        "profit_factor": (sum(wins) / gl) if gl else float("inf"),
        "expectancy": sum(pnls) / len(trades),
        "total": sum(pnls),
        "max_dd": mdd,
    }


def _print_block(title: str, trades: list[TradeRecord]) -> None:
    m = _metrics(trades)
    print(f"\n  {title}")
    if m["trades"] == 0:
        print("    (no trades)"); return
    print(f"    trades={m['trades']}  win={m['win_rate']:.0%}  "
          f"PF={m['profit_factor']:.2f}  exp={m['expectancy']:+.2f}  "
          f"total={m['total']:+.2f}  maxDD={m['max_dd']:.2f}")


def report(out_path: str, days: int, split: str | None) -> None:
    trades = TradeRecorder.load(out_path)
    print("\n==================== BACKTEST ====================")
    print(f"days simulated: {days}   |   trades: {len(trades)}   ->  {out_path}")

    print("\n  -- per strategy (full period) --")
    for system in System:
        sub = [t for t in trades if t.system == system.value]
        if sub:
            _print_block(system.value, sub)

    if split:
        import datetime
        def before(t):
            d = datetime.datetime.fromtimestamp(t.exit_time, datetime.timezone.utc).strftime("%Y-%m-%d")
            return d < split
        ins = [t for t in trades if before(t)]
        oos = [t for t in trades if not before(t)]
        print(f"\n  -- IN-SAMPLE  (before {split}) --")
        _print_block("all strategies", ins)
        print(f"\n  -- OUT-OF-SAMPLE (on/after {split}) --")
        _print_block("all strategies", oos)
        print("\n  Read: if OUT-OF-SAMPLE is much worse than in-sample, the result")
        print("  is likely curve-fit, not a real edge. Similar = more believable.")

    print("\n  Next: feed this file to the other tools ->")
    print(f"    python montecarlo.py {out_path}")
    print(f"    python correlation.py {out_path}")
    print("  Caveat: daily-close fills, daily strategies only. Not financial advice.")
    print("==================================================\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--synthetic", action="store_true", help="run on synthetic history")
    src.add_argument("--data", help="directory of TICKER.csv daily files")
    ap.add_argument("--out", default="backtest_trades.jsonl")
    ap.add_argument("--split", help="YYYY-MM-DD in-sample/out-of-sample boundary")
    ap.add_argument("--days", type=int, default=800, help="synthetic history length")
    args = ap.parse_args()

    feed = make_synthetic(UNIVERSE, days=args.days) if args.synthetic else load_csv_dir(args.data)
    out_path, days = run_backtest(feed, args.out)
    report(out_path, days, args.split)


if __name__ == "__main__":
    main()
