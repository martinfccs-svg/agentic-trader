"""portfolio_manager.py — one decision point for every candidate trade.

Each desk answers "is this a good trade?". This answers the only question
that matters once you already own things: "is it still a good trade GIVEN
what I hold?" — once, for every desk, in one place.

WHY CONSOLIDATE (the real motivation, 2026-08-02)
The checks already existed but were called separately inside each engine:
seven call sites across four files, each pair of lines slightly different.
That is how double-application bugs happen — and one nearly did: adding the
regime multiplier to swing_v2 while swing_engine already applied it would
have produced 0.845 x 0.845 = 0.714, a position 16% smaller than intended,
silently. One authority, one log line, one audit record.

GATES, NOT A WEIGHTED SCORE — a deliberate rejection of the design proposed
alongside this. A "portfolio risk score" of heat 30% + correlation 30% +
sector 20% + concentration 20% lets a severe breach in one dimension be
outvoted by comfort in the others: a candidate correlating 0.95 with an
existing holding (literally the same trade twice) would score ~63 and be
"reduced 50%" instead of rejected. It also adds four weights plus four
thresholds — eight free parameters on a system whose measured Sharpe already
spans -0.05 to 1.62 from the parameters it has. Each check keeps its own
authority and its own veto.

ORDER, and why: cheapest and most absolute first, so an expensive correlation
computation is never done for a trade a sector budget was going to reject.

    1. heat          how much is at risk across ALL desks right now
    2. sector budget would this breach a sector's share of equity
    3. correlation   does this duplicate something already held
    4. regime        how aggressive should this desk be at all
    5. notional cap  final re-clamp, so nothing above can breach it

MEASURE-FIRST. Heat and sector budgets ship OFF (threshold 0) and log what
they WOULD have done. Nobody knows what sector concentration this book
actually runs at; a limit chosen before the measurement is a guess that
presents as "no signals" rather than "misconfigured". Correlation carries its
own measure mode. Regime is already live.

FAILS OPEN: any error returns the shares unchanged, loudly. A risk overlay
must never become a new way to halt trading.

Env:
  PORTFOLIO_HEAT_MAX      halt new entries above this (0 = measure only)
  PORTFOLIO_HEAT_TAPER    start reducing size above this (0 = off)
  SECTOR_MAX_PCT          max share of equity per sector (0 = measure only)
  (correlation and regime read their own vars)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

log = logging.getLogger("portfolio_manager")

HEAT_MAX = float(os.getenv("PORTFOLIO_HEAT_MAX", "0"))
HEAT_TAPER = float(os.getenv("PORTFOLIO_HEAT_TAPER", "0"))
SECTOR_MAX_PCT = float(os.getenv("SECTOR_MAX_PCT", "0"))

# CONCENTRATION (2026-08-02). The SINGLE-position cap already exists as the
# 10%-of-equity notional clamp. What did not: the top-N aggregate. Five
# positions each at a perfectly legal 9% is 45% of the book in five names,
# and no gate in this system noticed. Measured against equity, not against
# invested capital, so cash held is not mistaken for diversification.
TOP_N = int(os.getenv("CONCENTRATION_TOP_N", "5"))
TOP_N_MAX_PCT = float(os.getenv("CONCENTRATION_TOP_N_MAX", "0"))   # 0 = measure

# DRAWDOWN SCALING (2026-08-02). A daily loss limit exists (kill switch,
# 2.5%); peak-to-trough did not, and it was already listed as a gap in the
# schematic. Peak equity persists on /data — which only became reliable on
# 2026-07-28, so this could not have worked before then. Only ever REDUCES.
DD_STATE = os.getenv("DRAWDOWN_STATE_PATH", "/data/peak_equity.json")
DD_SCALE = os.getenv("DRAWDOWN_SCALING", "measure").strip().lower()

# LIQUIDITY (2026-08-02). Cap the order as a share of average daily dollar
# volume. Honest note: at ~$95k equity and a 10% notional cap, orders run
# ~$9.5k against large-cap ADV in the hundreds of millions — participation
# near 0.01%, so this will not bind on THIS universe at THIS size. It is
# built because it becomes real the moment either grows, and because it is
# cheap; it is not built because it is currently binding.
MAX_PARTICIPATION = float(os.getenv("MAX_PARTICIPATION_PCT", "0"))  # 0 = measure

ACCEPT, REDUCE, REJECT = "accept", "reduce", "reject"


@dataclass
class PortfolioDecision:
    ticker: str
    shares_in: float
    shares_out: float = 0.0
    decision: str = ACCEPT
    stages: list = field(default_factory=list)   # (name, action, detail)

    def note(self, name, action, detail):
        self.stages.append((name, action, detail))
        if action == REJECT:
            self.decision = REJECT
        elif action == REDUCE and self.decision != REJECT:
            self.decision = REDUCE

    def line(self) -> str:
        parts = " | ".join(f"{n}:{a}({d})" for n, a, d in self.stages) or "no checks"
        return (f"{self.ticker} {self.shares_in:.2f} -> {self.shares_out:.2f} "
                f"[{self.decision.upper()}] {parts}")


def _heat_now(broker) -> float | None:
    """Current portfolio heat as a fraction of equity. None if unavailable.

    DELEGATES to portfolio_risk.heat(), which already owns the definition —
    including the rule that an unprotected position counts at FULL notional
    because unbounded risk is not small risk. Reimplementing that here would
    give the system two answers to one question, which is how the two ADX
    implementations nearly happened."""
    try:
        import portfolio_risk
        return float(portfolio_risk.heat(broker))
    except Exception as e:  # noqa: BLE001 — measured, never required
        log.error("portfolio: heat unavailable (%s)", e)
        return None


def _peak_equity(equity: float) -> float:
    """Highest equity ever seen, persisted. Returns the peak INCLUDING now."""
    import json
    peak = equity
    try:
        with open(DD_STATE, encoding="utf-8") as fh:
            peak = max(equity, float(json.load(fh).get("peak", equity)))
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        log.error("drawdown: cannot read %s (%s) — treating now as the peak",
                  DD_STATE, e)
    if peak <= equity:
        try:
            os.makedirs(os.path.dirname(DD_STATE) or ".", exist_ok=True)
            tmp = DD_STATE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"peak": equity}, fh)
            os.replace(tmp, DD_STATE)
        except Exception as e:  # noqa: BLE001
            log.error("drawdown: cannot persist peak (%s)", e)
    return peak


def _drawdown_multiplier(equity: float) -> tuple[float, float]:
    """(multiplier, drawdown_fraction). Ladder only ever reduces."""
    peak = _peak_equity(equity)
    dd = 0.0 if peak <= 0 else max(0.0, (peak - equity) / peak)
    if dd < 0.05:
        return 1.0, dd
    if dd < 0.10:
        return 0.75, dd
    if dd < 0.15:
        return 0.50, dd
    return 0.25, dd


def _top_n_pct(broker, equity: float, n: int) -> float:
    """Share of equity held in the n largest positions."""
    if equity <= 0:
        return 0.0
    vals = []
    for t, p in (getattr(broker, "positions", {}) or {}).items():
        try:
            price = getattr(p, "last_price", None) or p.entry_price
            vals.append(price * p.shares)
        except Exception:  # noqa: BLE001
            continue
    vals.sort(reverse=True)
    return sum(vals[:n]) / equity


def _adv_dollars(feed, ticker: str, days: int = 20) -> float | None:
    """Average daily dollar volume over `days` sessions, from cached bars."""
    try:
        bars = feed.get_daily_bars(ticker)
        if bars is None or not bars.close or not bars.volume:
            return None
        c, v = bars.close[-days:], bars.volume[-days:]
        n = min(len(c), len(v))
        if n < 5:
            return None
        return sum(c[-n:][i] * v[-n:][i] for i in range(n)) / n
    except Exception:  # noqa: BLE001
        return None


def _sector_exposure(broker, equity: float) -> dict:
    """{sector: fraction of equity} from current holdings."""
    out: dict[str, float] = {}
    if equity <= 0:
        return out
    try:
        from sector_map import sector_of
    except Exception:  # noqa: BLE001
        return out
    for t, p in (getattr(broker, "positions", {}) or {}).items():
        try:
            price = getattr(p, "last_price", None) or p.entry_price
            sec = sector_of(t)
            out[sec] = out.get(sec, 0.0) + (price * p.shares) / equity
        except Exception:  # noqa: BLE001 — one bad position must not blind us
            continue
    return out


def evaluate(shares: float, feed, broker, ticker: str, system,
             price: float, stop: float, equity: float) -> PortfolioDecision:
    """The single decision. Never raises; fails open to the input size."""
    d = PortfolioDecision(ticker=ticker, shares_in=shares, shares_out=shares)
    if shares <= 0 or price <= 0:
        return d
    tag = system.value if hasattr(system, "value") else str(system)

    # Clamp to the notional cap on ENTRY as well as exit. position_size
    # already applies it, so this is normally a no-op — but without it the
    # sector stage reasons about a size that could never be taken and logs
    # nonsense like "healthcare 0.0%+842.1%". Every stage should see the
    # size that would actually be traded.
    try:
        from config import max_position_dollars
        _cap0 = max_position_dollars(equity) / price
        if d.shares_out > _cap0:
            d.shares_out = _cap0
    except Exception:  # noqa: BLE001
        pass

    # ---- 0. DRAWDOWN ---------------------------------------------------
    # First because it is the cheapest check (one equity read) and the most
    # global: if the account is deep in a drawdown, nothing below should be
    # sized as though it were not.
    try:
        dd_mult, dd = _drawdown_multiplier(equity)
        if DD_SCALE in ("on", "live", "true", "1", "yes") and dd_mult < 1.0:
            d.shares_out *= dd_mult
            d.note("drawdown", REDUCE, f"{dd:.1%} from peak -> x{dd_mult:.2f}")
        else:
            d.note("drawdown", ACCEPT,
                   f"{dd:.1%} from peak"
                   + (f" (would be x{dd_mult:.2f})" if dd_mult < 1.0 else "")
                   + ("" if DD_SCALE in ("on", "live") else " measure-only"))
    except Exception as e:  # noqa: BLE001
        log.error("portfolio: drawdown check failed (%s) — failing open", e)

    # ---- 1. HEAT -------------------------------------------------------
    try:
        heat = _heat_now(broker)
        if heat is not None:
            if HEAT_MAX > 0 and heat >= HEAT_MAX:
                d.shares_out = 0.0
                d.note("heat", REJECT, f"{heat:.2%} >= max {HEAT_MAX:.2%}")
                log.warning("PORTFOLIO %s", d.line())
                return d
            if HEAT_TAPER > 0 and heat >= HEAT_TAPER:
                # Linear taper between taper and max: risk already committed
                # buys down the size of the next trade rather than blocking it.
                span = max(HEAT_MAX - HEAT_TAPER, 1e-9) if HEAT_MAX > 0 else 1.0
                frac = max(0.25, 1.0 - (heat - HEAT_TAPER) / span)
                d.shares_out *= frac
                d.note("heat", REDUCE, f"{heat:.2%} -> x{frac:.2f}")
            else:
                d.note("heat", ACCEPT,
                       f"{heat:.2%}" + ("" if HEAT_MAX > 0 else " measure-only"))
    except Exception as e:  # noqa: BLE001
        log.error("portfolio: heat check failed (%s) — failing open", e)

    # ---- 2. SECTOR BUDGET ----------------------------------------------
    try:
        from sector_map import sector_of
        sec = sector_of(ticker)
        exposure = _sector_exposure(broker, equity)
        have = exposure.get(sec, 0.0)
        adding = (d.shares_out * price) / equity if equity > 0 else 0.0
        if SECTOR_MAX_PCT > 0:
            room = SECTOR_MAX_PCT - have
            if room <= 0:
                d.shares_out = 0.0
                d.note("sector", REJECT,
                       f"{sec} at {have:.1%} >= {SECTOR_MAX_PCT:.1%}")
                log.warning("PORTFOLIO %s", d.line())
                return d
            if adding > room:
                # Size to FIT the budget rather than rejecting outright — the
                # trade is not wrong, only its size is.
                d.shares_out = (room * equity) / price
                d.note("sector", REDUCE,
                       f"{sec} {have:.1%}+{adding:.1%} > {SECTOR_MAX_PCT:.1%},"
                       f" trimmed to fit")
            else:
                d.note("sector", ACCEPT, f"{sec} {have:.1%}+{adding:.1%}")
        else:
            d.note("sector", ACCEPT,
                   f"{sec} {have:.1%}+{adding:.1%} measure-only")
    except Exception as e:  # noqa: BLE001
        log.error("portfolio: sector check failed (%s) — failing open", e)

    # ---- 2b. CONCENTRATION (top-N aggregate) ----------------------------
    # The single-position cap is the notional clamp; this is the one it
    # cannot see: five positions each at a legal 9% is 45% in five names.
    try:
        have_top = _top_n_pct(broker, equity, TOP_N)
        adding_pct = (d.shares_out * price) / equity if equity > 0 else 0.0
        # Adding a new name can only displace the smallest of the current
        # top-N, so the ceiling case is have_top + adding.
        would = have_top + adding_pct
        if TOP_N_MAX_PCT > 0:
            room = TOP_N_MAX_PCT - have_top
            if room <= 0:
                d.shares_out = 0.0
                d.note("concentration", REJECT,
                       f"top{TOP_N} already {have_top:.1%} >= "
                       f"{TOP_N_MAX_PCT:.1%}")
                log.warning("PORTFOLIO %s", d.line())
                return d
            if adding_pct > room:
                d.shares_out = (room * equity) / price
                d.note("concentration", REDUCE,
                       f"top{TOP_N} {have_top:.1%}+{adding_pct:.1%} > "
                       f"{TOP_N_MAX_PCT:.1%}, trimmed to fit")
            else:
                d.note("concentration", ACCEPT,
                       f"top{TOP_N} {would:.1%}")
        else:
            d.note("concentration", ACCEPT,
                   f"top{TOP_N} {would:.1%} measure-only")
    except Exception as e:  # noqa: BLE001
        log.error("portfolio: concentration failed (%s) — failing open", e)

    # ---- 2c. LIQUIDITY (participation vs average daily dollar volume) ---
    try:
        adv = _adv_dollars(feed, ticker)
        if adv and adv > 0:
            order = d.shares_out * price
            part = order / adv
            if MAX_PARTICIPATION > 0 and part > MAX_PARTICIPATION:
                d.shares_out = (MAX_PARTICIPATION * adv) / price
                d.note("liquidity", REDUCE,
                       f"{part:.3%} of ADV > {MAX_PARTICIPATION:.3%}, trimmed")
            else:
                d.note("liquidity", ACCEPT,
                       f"{part:.3%} of ${adv/1e6:.0f}M ADV"
                       + ("" if MAX_PARTICIPATION > 0 else " measure-only"))
    except Exception as e:  # noqa: BLE001
        log.error("portfolio: liquidity check failed (%s) — failing open", e)

    # ---- 3. CORRELATION (delegates; carries its own measure mode) -------
    try:
        import correlation_manager
        if tag == "xsectmom":
            # Excluded on purpose: the sector cap already enforces
            # diversification for this desk, and two filters on three slots
            # could leave it holding cash instead of the ranking's choices.
            d.note("correlation", ACCEPT, "skipped — sector cap covers xsect")
            raise StopIteration
        before = d.shares_out
        d.shares_out, corr_dec = correlation_manager.apply(
            d.shares_out, feed, broker, ticker, system)
        if d.shares_out <= 0:
            d.note("correlation", REJECT, "duplicates existing exposure")
            log.warning("PORTFOLIO %s", d.line())
            return d
        d.note("correlation",
               REDUCE if d.shares_out < before else ACCEPT,
               f"{corr_dec}")
    except StopIteration:
        pass
    except Exception as e:  # noqa: BLE001
        log.error("portfolio: correlation failed (%s) — failing open", e)

    # ---- 4. REGIME (delegates; re-clamps to notional itself) ------------
    try:
        import regime_allocation
        before = d.shares_out
        d.shares_out, mult = regime_allocation.apply_to_shares(
            d.shares_out, feed, tag if tag in
            ("swing", "meanrev", "intraday", "xsectmom") else "swing",
            equity, price)
        d.note("regime",
               REDUCE if d.shares_out < before else ACCEPT, f"x{mult:.2f}")
    except Exception as e:  # noqa: BLE001
        log.error("portfolio: regime failed (%s) — failing open", e)

    # ---- 5. FINAL NOTIONAL RE-CLAMP -------------------------------------
    # Last, so no stage above can breach it — the ordering point the review
    # made, and correct: safety should not depend on every later multiplier
    # happening to be <= 1.0.
    try:
        from config import max_position_dollars
        cap = max_position_dollars(equity) / price
        if d.shares_out > cap:
            d.note("notional", REDUCE,
                   f"{d.shares_out:.2f} -> {cap:.2f} shares")
            d.shares_out = cap
    except Exception as e:  # noqa: BLE001
        log.error("portfolio: notional clamp failed (%s)", e)

    if d.decision == ACCEPT:
        log.info("portfolio %s", d.line())
    else:
        log.warning("PORTFOLIO %s", d.line())
    try:
        import audit
        audit.record("portfolio_decision", notify=False, ticker=ticker,
                     system=tag, decision=d.decision,
                     shares_in=round(d.shares_in, 2),
                     shares_out=round(d.shares_out, 2),
                     stages=[{"stage": n, "action": a, "detail": det}
                             for n, a, det in d.stages])
    except Exception:  # noqa: BLE001 — mirror is best-effort
        pass
    return d


def apply(shares: float, feed, broker, ticker: str, system,
          price: float, stop: float, equity: float) -> tuple[float, str]:
    """Engine entry point: (shares, decision)."""
    d = evaluate(shares, feed, broker, ticker, system, price, stop, equity)
    return d.shares_out, d.decision
