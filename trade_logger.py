"""Per-system logging and scorecard.

Signal funnel (source -> system -> action) to a JSONL file, plus a per-system
scorecard computed from closed trades: win rate, profit factor, expectancy,
and max drawdown. Realized and unrealized are kept separate; the scorecard is
built from realized closes only.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field

from models import Action, Signal, System

log = logging.getLogger("logger")


@dataclass
class _Closed:
    realized: float
    at: float = field(default_factory=time.time)


class TradeLogger:
    def __init__(self, path: str = "signal_log.jsonl") -> None:
        self._path = path
        self._closed: dict[System, list[_Closed]] = {s: [] for s in System}

    def record(self, signal: Signal, system: System, action: Action, detail: str = "") -> None:
        rec = {"ts": time.time(), "ticker": signal.ticker, "source": signal.source.value,
               "system": system.value, "action": action.value, "detail": detail}
        try:
            with open(self._path, "a") as fh:
                fh.write(json.dumps(rec) + "\n")
        except OSError:
            pass
        log.debug("log %s", rec)

    def record_close(self, system: System, realized: float) -> None:
        self._closed[system].append(_Closed(realized))

    # ----- scorecard ------------------------------------------------------

    def scorecard(self, system: System) -> dict:
        trades = [c.realized for c in self._closed[system]]
        n = len(trades)
        if n == 0:
            return {"system": system.value, "trades": 0}
        wins = [t for t in trades if t > 0]
        losses = [t for t in trades if t <= 0]
        gross_win = sum(wins)
        gross_loss = abs(sum(losses))
        # max drawdown over the realized equity curve
        curve, peak, mdd = 0.0, 0.0, 0.0
        for t in trades:
            curve += t
            peak = max(peak, curve)
            mdd = min(mdd, curve - peak)
        return {
            "system": system.value,
            "trades": n,
            "win_rate": round(len(wins) / n, 3),
            "profit_factor": round(gross_win / gross_loss, 3) if gross_loss else float("inf"),
            "expectancy": round(sum(trades) / n, 2),
            "total_realized": round(sum(trades), 2),
            "max_drawdown": round(mdd, 2),
        }

    def print_scorecard(self, broker) -> None:
        print("\n================ SCORECARD (per system) ================")
        for system in System:
            sc = self.scorecard(system)
            unreal = broker.unrealized_pnl(system)
            if sc["trades"] == 0:
                print(f"[{system.value:8}] no closed trades | unrealized {unreal:+.2f}")
                continue
            print(f"[{system.value:8}] trades={sc['trades']} winrate={sc['win_rate']:.0%} "
                  f"PF={sc['profit_factor']} exp={sc['expectancy']:+.2f} "
                  f"realized={sc['total_realized']:+.2f} maxDD={sc['max_drawdown']:.2f} "
                  f"| unrealized {unreal:+.2f}")
        print(f"Equity: {broker.equity:,.2f}  (start {broker.start_equity:,.0f})")
        print("Small samples are noise; read this over dozens of trades, not three.")
        print("========================================================\n")
