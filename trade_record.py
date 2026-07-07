"""The standard trade record — the one ingredient everything else consumes.

Every source of closed trades emits this same shape: the live/paper broker now,
and the future backtest harness later. Monte Carlo, cost analysis, and the
scorecard all read THIS and nothing else, so a new trade source plugs in with
zero downstream changes.

The key normalized field is `r_multiple`: realized P&L expressed in units of the
risk you originally took (entry-to-initial-stop). +3R = made three times the
planned risk; -1R = lost the full planned risk. R-multiples make trades of
different sizes and prices comparable, which is exactly what Monte Carlo needs.

Persistence (post 2026-07-07): Railway's container filesystem is ephemeral and
deployment logs are purged on redeploy — a trades.jsonl written to the working
directory dies with every deploy, taking the Monte Carlo history with it. The
path now defaults to the persistent volume and is configurable:

    TRADES_LOG_PATH   default /data/trades.jsonl  (mount a volume at /data)

If the volume isn't mounted, we fall back to ./trades.jsonl and log an ERROR —
degraded but never silent. Write failures are also logged loudly: the old
`except OSError: pass` silently discarded trade records.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, fields

log = logging.getLogger("trade_record")

TRADES_LOG_PATH = os.getenv("TRADES_LOG_PATH", "/data/trades.jsonl")


@dataclass
class TradeRecord:
    ticker: str
    system: str                 # "swing" | "intraday"
    source: str                 # signal source that opened it
    entry_time: float
    exit_time: float
    entry_price: float
    exit_price: float
    shares: float
    realized_pnl: float
    initial_risk: float         # dollars risked at entry = (entry - initial_stop) * shares
    r_multiple: float           # realized_pnl / initial_risk

    @staticmethod
    def build(ticker, system, source, entry_time, exit_time, entry_price,
              exit_price, shares, entry_stop, realized_pnl) -> "TradeRecord":
        risk_per_share = max(entry_price - entry_stop, 0.0)
        initial_risk = risk_per_share * shares
        r = (realized_pnl / initial_risk) if initial_risk > 0 else 0.0
        return TradeRecord(
            ticker=ticker, system=system, source=source,
            entry_time=entry_time, exit_time=exit_time,
            entry_price=round(entry_price, 4), exit_price=round(exit_price, 4),
            shares=round(shares, 4), realized_pnl=round(realized_pnl, 2),
            initial_risk=round(initial_risk, 2), r_multiple=round(r, 4),
        )


_FIELD_NAMES = {f.name for f in fields(TradeRecord)}


def _resolve_path(preferred: str) -> str:
    """Ensure the trades file is writable; fall back loudly if not."""
    try:
        d = os.path.dirname(preferred) or "."
        os.makedirs(d, exist_ok=True)
        with open(preferred, "a", encoding="utf-8"):
            pass
        return preferred
    except OSError as e:
        fallback = "./trades.jsonl"
        log.error("trade_record: %s not writable (%s) — falling back to %s. "
                  "Mount a Railway volume at /data or set TRADES_LOG_PATH, or "
                  "the Monte Carlo trade history will NOT survive redeploys.",
                  preferred, e, fallback)
        return fallback


class TradeRecorder:
    """Appends TradeRecords to a JSONL file as trades close. One line per trade.
    This file is the bridge: the bot writes it live; Monte Carlo reads it."""

    def __init__(self, path: str | None = None) -> None:
        self._path = _resolve_path(path or TRADES_LOG_PATH)
        log.info("trade_record: writing closed trades to %s", self._path)

    def record(self, tr: TradeRecord) -> None:
        """Append one closed trade. Never raises (a recording failure must not
        break trading) but failures are LOUD — silent loss corrupted the
        history before."""
        line = json.dumps(asdict(tr))
        try:
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as e:
            log.error("trade_record: WRITE FAILED (%s) — lost record: %s",
                      e, line)

    @staticmethod
    def load(path: str | None = None) -> list[TradeRecord]:
        """Read the trade history. Skips corrupt lines with a warning instead
        of aborting the whole load, and ignores unknown fields so records
        written by a future schema still load here."""
        path = path or TRADES_LOG_PATH
        out: list[TradeRecord] = []
        try:
            with open(path, encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        raw = json.loads(line)
                        out.append(TradeRecord(
                            **{k: v for k, v in raw.items()
                               if k in _FIELD_NAMES}))
                    except (json.JSONDecodeError, TypeError) as e:
                        log.warning("trade_record: skipping corrupt line %d "
                                    "in %s: %s", lineno, path, e)
        except FileNotFoundError:
            pass
        return out
