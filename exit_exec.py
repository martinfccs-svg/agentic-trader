"""exit_exec.py — the MECHANICS of closing a position, shared by every desk.

Deliberately NOT an "exit manager". The distinction is the whole design:

    POLICY  — WHEN to exit. Genuinely different per desk and stays there.
              meanrev runs a six-rung ladder (breakeven, ATR trail, volatility
              emergency, trend failure, time, RSI); swing_v2 exits on an EMA20
              close plus a time stop; xsect exits on RANK, not price at all;
              intraday exits on the clock. Merging those behind one manager
              would mean inventing a common shape they do not have — the same
              objection that ruled out the composite momentum score and the
              weighted portfolio risk score.

    MECHANICS — HOW to exit safely. Identical everywhere, and currently
              copy-pasted into four engines: sell, book the close, notify,
              arm the loss cooldown, and contain failure so one bad exit does
              not abandon the others (the 2026-07-06 flatten lesson).

This module owns the second and touches none of the first.

Every ordering rule here was learned from an incident:
  * capture entry_price and shares BEFORE selling — the position object is
    gone afterwards, and the notifier needs both
  * the loss cooldown arms on ANY losing exit, including broker-side bracket
    fills, because the autopsy showed nearly all positions close that way
  * a failure logs and returns False rather than raising: the caller moves on
    to the next ticker and retries this one next cycle
  * reducing risk is never gated by kill switch or regime — those exist to
    stop new entries
"""

from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger("exit_exec")


def close_position(broker, logger, ticker: str, price: float, reason: str,
                   system, notifier=None, cooldown_system: Optional[str] = None
                   ) -> tuple[bool, Optional[float]]:
    """Close one position and record it everywhere it needs recording.

    Returns (succeeded, realized_pnl). Never raises.

    cooldown_system: the key loss_cooldown should use ("swing", "meanrev").
    None disables the cooldown for this desk — intraday has its own
    minute-scale cooldown and must not also take a 5-day one.
    """
    pos = (getattr(broker, "positions", {}) or {}).get(ticker)
    if pos is None:
        log.warning("exit %s: no position to close (already gone?)", ticker)
        return False, None

    # Captured BEFORE the sell: the position object does not survive it.
    entry_price = getattr(pos, "entry_price", None)
    shares = getattr(pos, "shares", None)

    try:
        realized = broker.sell(ticker, price)
    except Exception as e:  # noqa: BLE001 — one failure must not stop the rest
        log.error("exit %s FAILED (%s) — position retained, retries next "
                  "cycle", ticker, e)
        return False, None

    try:
        logger.record_close(system, realized)
    except Exception as e:  # noqa: BLE001
        log.error("exit %s: sold but record_close failed (%s) — the trade is "
                  "closed at the broker; only the scorecard is short",
                  ticker, e)

    if cooldown_system and realized is not None and realized < 0:
        try:
            import loss_cooldown
            loss_cooldown.note_loss(cooldown_system, ticker)
        except Exception as e:  # noqa: BLE001
            log.error("exit %s: cooldown not armed (%s)", ticker, e)

    log.warning("EXIT %s [%s] %s @ %.2f realized=%s", ticker,
                getattr(system, "value", system), reason, price,
                f"{realized:+.2f}" if realized is not None else "n/a")

    if notifier is not None and realized is not None and \
            entry_price is not None and shares is not None:
        try:
            notifier.notify_exit(ticker=ticker, shares=shares,
                                 exit_price=price, entry_price=entry_price,
                                 pnl=realized,
                                 system=getattr(system, "value", str(system)))
        except Exception as e:  # noqa: BLE001 — a phone alert is never critical
            log.error("exit %s: notify failed (%s)", ticker, e)

    try:
        import audit
        audit.record("position_exit", notify=False, ticker=ticker,
                     system=getattr(system, "value", str(system)),
                     reason=reason, price=round(price, 2),
                     realized=(round(realized, 2)
                               if realized is not None else None))
    except Exception:  # noqa: BLE001 — mirror is best-effort
        pass

    return True, realized
