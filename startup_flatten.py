"""startup_flatten.py — close named positions automatically, no typing.

Operator-ordered liquidation: set FLATTEN_TICKERS and the bot closes those
positions itself at the next market open, using the broker path the engines
already use (which cancels the resting bracket leg before selling).

WHY THIS IS THE MOST DANGEROUS FILE IN THE REPO
It is the only component whose sole purpose is to destroy positions, and it
runs unattended. Every design choice below is a guard:

  * MARKET HOURS ONLY. It will not sell into a closed market. Firing a market
    order after the bell is what dumped UNH/INTC/MU at stale quote-estimates
    on 2026-07-16; queued orders then fill at the next open against stops
    computed from prices that no longer exist. If the market is shut, this
    waits and retries on the next cycle.
  * SELF-LIMITING. It only acts on tickers currently held. Once they are
    closed there is nothing left to close, so a forgotten variable cannot
    liquidate anything later by surprise.
  * LOUD WHILE ARMED. Every boot with FLATTEN_TICKERS set logs CRITICAL,
    naming the tickers, so a stale instruction cannot sit unnoticed in
    Railway for weeks. Remove the variable once the closes are confirmed.
  * PER-TICKER ISOLATION. One failure never abandons the rest half-done
    (the 2026-07-06 flatten lesson).
  * NOT GATED BY THE KILL SWITCH OR REGIME. Reducing risk is always allowed;
    those gates exist to stop new entries, never exits.

It does NOT decide anything. It executes a decision the operator already
made, and says so in every line it writes.

Env:
  FLATTEN_TICKERS   comma-separated, e.g. "UNP,PLD,UNH,UPS". Empty = inert.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("startup_flatten")

_done: set[str] = set()      # closed by THIS process; avoids double attempts
_announced = False


def _wanted() -> list[str]:
    raw = os.getenv("FLATTEN_TICKERS", "")
    return [t.strip().upper() for t in raw.split(",") if t.strip()]


def announce() -> None:
    """Call once at boot so an armed instruction is impossible to miss."""
    global _announced
    tickers = _wanted()
    if not tickers or _announced:
        return
    _announced = True
    log.critical("FLATTEN_TICKERS IS ARMED: %s — the bot will CLOSE these "
                 "positions at the next market open, without further "
                 "confirmation. This is an operator instruction, not a "
                 "strategy decision. REMOVE the variable once the closes are "
                 "confirmed.", ", ".join(tickers))


def run(broker, feed, engines, market_is_open: bool) -> int:
    """Attempt the ordered closes. Returns how many were closed this call.
    Never raises: a liquidation helper must not be able to break a cycle."""
    tickers = _wanted()
    if not tickers:
        return 0
    outstanding = [t for t in tickers
                   if t in getattr(broker, "positions", {}) and t not in _done]
    if not outstanding:
        return 0
    if not market_is_open:
        log.warning("FLATTEN pending for %s — market is CLOSED, waiting for "
                    "the open rather than selling at a stale quote",
                    ", ".join(outstanding))
        return 0

    # The trade logger lives on the engines, not in the cycle signature;
    # take it from any engine so realized P&L still reaches the scorecard.
    logger = None
    for e in (engines or []):
        logger = getattr(e, "_log", None)
        if logger is not None:
            break

    closed = 0
    for ticker in outstanding:
        try:
            pos = broker.positions.get(ticker)
            if pos is None:
                continue
            q = feed.get_quote(ticker)
            if q is None or not q.price:
                log.warning("FLATTEN %s: no quote this cycle — retrying next",
                            ticker)
                continue
            entry, shares, system = pos.entry_price, pos.shares, pos.system
            realized = broker.sell(ticker, q.price)
            _done.add(ticker)
            closed += 1
            if logger is not None:
                try:
                    logger.record_close(system, realized)
                except Exception as e:  # noqa: BLE001
                    log.error("FLATTEN %s: sold but record_close failed (%s)",
                              ticker, e)
            log.critical("FLATTEN CLOSED %s x%.2f @ %.2f (entry %.2f) "
                         "realized=%s — operator-ordered via FLATTEN_TICKERS",
                         ticker, shares, q.price, entry,
                         f"{realized:+.2f}" if realized is not None else "n/a")
            try:
                import audit
                audit.record("operator_flatten", notify=True, ticker=ticker,
                             shares=round(shares, 2), price=round(q.price, 2),
                             entry=round(entry, 2),
                             realized=(round(realized, 2)
                                       if realized is not None else None))
            except Exception:  # noqa: BLE001 — mirror is best-effort
                pass
        except Exception as e:  # noqa: BLE001 — one failure must not stop the rest
            log.error("FLATTEN %s FAILED (%s) — other tickers continue, this "
                      "one retries next cycle", ticker, e)

    still = [t for t in tickers
             if t in getattr(broker, "positions", {}) and t not in _done]
    if closed and not still:
        log.critical("FLATTEN COMPLETE: %s all closed. REMOVE FLATTEN_TICKERS "
                     "from Railway now — leaving it set means any future "
                     "position in these names is closed on sight.",
                     ", ".join(tickers))
    return closed
