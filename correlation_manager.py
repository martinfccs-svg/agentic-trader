"""correlation_manager.py — the portfolio question, asked once for every desk.

Each engine answers "is this a good trade?". Nothing has been asking "does
this improve the PORTFOLIO?". Swing can hold RTX and LMT at once — two
defence names, one trade — and no component notices. Xsect has a sector cap;
swing, meanrev and intraday have no cross-position awareness at all.

This is a shared service, not engine logic: it sits between any engine's
sizing step and the broker, so every desk is judged by the same rule.

SCOPE, deliberately narrow:
  * candidate vs CURRENT HOLDINGS only — with 5-6 open positions that is
    5-6 pairs per signal, computed from daily closes already cached. A full
    68x68 matrix would be 2,278 pairs of work to answer the same question.
  * correlation of DAILY RETURNS over CORR_WINDOW sessions (default 60),
    which is what "these move together" actually means. Correlating price
    LEVELS would report 0.9+ for any two rising stocks and be useless.
  * NOT built: portfolio VaR, marginal risk contribution, factor overlap.
    Those need a covariance matrix, distributional assumptions and factor
    definitions this system does not have. Institutional vocabulary attached
    to machinery that would answer questions nobody can act on yet.
  * sector overlap is already handled by sector_map + the xsect cap.

THREE-WAY DECISION (accept / reduce / reject) rather than a binary gate: a
correlated but strong candidate is often worth half a position, not none.

MEASURE-ONLY BY DEFAULT. CORRELATION_MAX=0 logs every reading and blocks
nothing. This matters more than usual here: nobody knows what correlations
this book actually runs at. Long-only equities in a bull market commonly sit
at 0.3-0.6 cross-sector and 0.6-0.85 same-sector, so a threshold set blind
could reject nearly every candidate and look like "no signals" rather than
"misconfigured". Watch the numbers for a week, then choose a threshold that
means something. Same discipline as portfolio heat and the sector cap.

FAILS OPEN: any data problem returns ACCEPT with a loud log. A diversification
overlay must never become a new way to halt trading.

Env:
  CORRELATION_MAX     reject above this (0 = MEASURE ONLY, default)
  CORRELATION_REDUCE  halve size above this (0 = disabled, default)
  CORRELATION_WINDOW  sessions of daily returns (default 60)
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("correlation_manager")

# MODE is deliberately separate from the THRESHOLDS (2026-07-29 review).
# An earlier build used CORRELATION_MAX=0 to mean "measure only", which meant
# the only way to learn what a 0.80 limit would do was to set it — and
# thereby enforce it. Separating them turns measure mode into a DRY RUN: the
# thresholds you are considering are evaluated and logged on every candidate,
# and nothing is blocked until MODE=enforce. Choosing a threshold then rests
# on what it would actually have done, not on a guess.
MODE = os.getenv("CORRELATION_MODE", "measure").strip().lower()
WARN_CORR = float(os.getenv("CORRELATION_WARN", "0.60"))
BLOCK_CORR = float(os.getenv("CORRELATION_BLOCK", "0.80"))
WINDOW = int(os.getenv("CORRELATION_LOOKBACK", "60"))
MIN_OVERLAP = int(os.getenv("CORRELATION_MIN_OBS", "40"))

ACCEPT, REDUCE, REJECT = "accept", "reduce", "reject"


@dataclass
class CorrelationDecision:
    """Structured result so this can be extended without changing callers."""
    candidate: str
    decision: str = ACCEPT
    max_corr: float = 0.0
    against: Optional[str] = None
    avg_corr: float = 0.0
    beta: Optional[float] = None      # vs the worst-correlated holding
    observations: int = 0
    t_stat: Optional[float] = None    # significance of max_corr
    enforced: bool = False            # did this actually change anything?
    reason: str = ""

    def line(self) -> str:
        b = f" beta={self.beta:+.2f}" if self.beta is not None else ""
        t = f" t={self.t_stat:.1f}" if self.t_stat is not None else ""
        return (f"{self.candidate}: max_corr={self.max_corr:+.2f} vs "
                f"{self.against or '-'}{b}{t} avg={self.avg_corr:+.2f} "
                f"n={self.observations} -> {self.decision.upper()}"
                f"{'' if self.enforced else ' [MEASURE — no action taken]'}"
                f" | {self.reason}")


def _returns(closes: list[float], n: int) -> list[float]:
    """Daily returns over the last n+1 closes. Returns, not levels: two
    unrelated stocks in an uptrend both have rising PRICES and would show
    a spurious 0.9+ correlation on levels."""
    tail = closes[-(n + 1):]
    if len(tail) < MIN_OVERLAP + 1:
        return []
    return [tail[i] / tail[i - 1] - 1 for i in range(1, len(tail))
            if tail[i - 1]]


def correlation(a: list[float], b: list[float]) -> Optional[float]:
    n = min(len(a), len(b))
    if n < MIN_OVERLAP:
        return None
    a, b = a[-n:], b[-n:]
    ma, mb = sum(a) / n, sum(b) / n
    num = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((x - mb) ** 2 for x in b))
    if not da or not db:
        return None
    return num / (da * db)


def beta(candidate: list[float], holding: list[float]) -> Optional[float]:
    """Slope of candidate returns on holding returns.

    Correlation says two names MOVE TOGETHER; beta says BY HOW MUCH. 0.86
    correlation with beta 1.04 is a near substitute; 0.86 with beta 0.30
    shares direction but a third of the amplitude. The distinction matters
    for whether a position is redundant or merely related.
    """
    n = min(len(candidate), len(holding))
    if n < MIN_OVERLAP:
        return None
    a, b = candidate[-n:], holding[-n:]
    mb = sum(b) / n
    var = sum((x - mb) ** 2 for x in b)
    if not var:
        return None
    ma = sum(a) / n
    return sum((a[i] - ma) * (b[i] - mb) for i in range(n)) / var


def _t_stat(r: float, n: int) -> Optional[float]:
    """Significance of a correlation: t = r*sqrt(n-2)/sqrt(1-r^2).
    Guards against acting on a large-looking r from a short window."""
    if n < 3 or abs(r) >= 1:
        return None
    return r * math.sqrt(n - 2) / math.sqrt(1 - r * r)


def evaluate(feed, broker, ticker: str, system=None
             ) -> tuple[str, float, str]:
    """(decision, worst_correlation, human-readable reason).

    Never raises. Compares the candidate against every CURRENT holding,
    including those owned by other desks — the portfolio is one book even
    though the engines are separate.
    """
    try:
        positions = dict(getattr(broker, "positions", {}) or {})
        positions.pop(ticker, None)
        d = CorrelationDecision(candidate=ticker)
        if not positions:
            d.reason = "no existing holdings to correlate against"
            return d

        bars = feed.get_daily_bars(ticker)
        if bars is None or not bars.close:
            log.warning("correlation: no bars for %s — FAILING OPEN", ticker)
            d.reason = "no bars for candidate (failed open)"
            return d
        cand = _returns(bars.close, WINDOW)
        if not cand:
            d.reason = "insufficient history for candidate"
            return d

        pairs = []
        for held in positions:
            hb = feed.get_daily_bars(held)
            if hb is None or not hb.close:
                continue
            hr = _returns(hb.close, WINDOW)
            r = correlation(cand, hr)
            if r is not None:
                pairs.append((r, held, hr))
        if not pairs:
            d.reason = "no comparable holdings (failed open)"
            return d

        pairs.sort(reverse=True)
        d.max_corr, d.against, worst_returns = pairs[0]
        d.avg_corr = sum(p[0] for p in pairs) / len(pairs)
        d.observations = min(len(cand), len(worst_returns))
        d.beta = beta(cand, worst_returns)
        d.t_stat = _t_stat(d.max_corr, d.observations)
        detail = ", ".join(f"{t} {c:+.2f}" for c, t, _ in pairs[:4])

        # Thresholds are ALWAYS evaluated, in both modes. In measure mode the
        # recommendation is logged and discarded — that is the dry run.
        if d.max_corr >= BLOCK_CORR:
            d.decision = REJECT
            d.reason = (f"correlates {d.max_corr:+.2f} with {d.against} "
                        f"(block >= {BLOCK_CORR:.2f}) | {detail}")
        elif d.max_corr >= WARN_CORR:
            d.decision = REDUCE
            d.reason = (f"correlates {d.max_corr:+.2f} with {d.against} "
                        f"(reduce >= {WARN_CORR:.2f}) | {detail}")
        else:
            d.decision = ACCEPT
            d.reason = f"below {WARN_CORR:.2f} | {detail}"
        d.enforced = (MODE == "enforce" and d.decision != ACCEPT)
        return d
    except Exception as e:  # noqa: BLE001 — an overlay must never break a cycle
        log.error("correlation: evaluation failed for %s (%s) — FAILING OPEN",
                  ticker, e)
        return CorrelationDecision(candidate=ticker,
                                   reason=f"evaluation failed: {e}")


def apply(shares: float, feed, broker, ticker: str, system=None
          ) -> tuple[float, str]:
    """Engine entry point: returns (shares, decision).

    In MEASURE mode the recommendation is logged and the share count is
    returned UNCHANGED — the dry run. In ENFORCE mode REJECT -> 0 shares and
    REDUCE -> half size.
    """
    d = evaluate(feed, broker, ticker, system)
    tag = system.value if hasattr(system, "value") else (system or "?")
    if d.decision == ACCEPT:
        log.info("correlation [%s] %s", tag, d.line())
        return shares, d.decision
    log.warning("CORRELATION %s [%s] %s", d.decision.upper(), tag, d.line())
    if MODE != "enforce":
        return shares, ACCEPT          # measured only; nothing changes
    return (0.0 if d.decision == REJECT else shares * 0.5), d.decision
