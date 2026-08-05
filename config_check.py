"""config_check.py — validate the environment BEFORE the trading day starts.

Every failure this catches has actually happened in this project. None of
them raised an error; each cost a session or a debugging afternoon:

  * DAILY_LOOKBACK_DAYS=120 was silently floored to 500 by the feed, and
    logged a warning at error severity on every boot for a week
  * MR_MAX_POS is the real name; MEANREV_MAX_POS is the one you reach for.
    Setting the second does nothing at all, silently
  * SWING_V2_ROUTE was set but the running process never saw it — a variable
    only reaches a container on the deploy that follows saving it
  * PORTFOLIO_HEAT_MAX=0 means MEASURE-ONLY, not "no heat permitted" — the
    same digit means opposite things in different systems

The philosophy is the same as the GATES banner: a configuration fact should
be STATED, not inferred. This runs once at boot, prints what it found, and
never blocks startup — a validator that halts trading over a warning would
be a worse failure than the ones it prevents.

Severity:
  ERROR    the setting cannot do what it looks like it does
  WARN     legal but surprising, or a known trap
  INFO     measure-only / disabled features, listed so nothing is assumed live
"""

from __future__ import annotations

import difflib
import logging
import os
import re

log = logging.getLogger("config_check")

# Every variable the codebase actually reads. Anything set that is NOT here is
# either a typo or dead config — both worth knowing about.
KNOWN = {
    "ABSENT_CONFIRM_SECS", "AFTER_HOURS_INTERVAL_SECS", "ALPACA_API_KEY",
    "ALPACA_PAPER", "ALPACA_SECRET_KEY", "APCA_API_BASE_URL",
    "BEARTREND_ADX_MIN", "BEARTREND_APPROVAL_VALID_DAYS",
    "BEARTREND_BREAKDOWN_DAYS", "BEARTREND_EMA_SLOPE_DAYS",
    "BEARTREND_LOG_SCORE_MIN", "BEARTREND_MODE", "BEARTREND_OBS_PATH",
    "BEARTREND_REFRESH_SECONDS", "BEARTREND_RESEARCH_DIR",
    "BEARTREND_RS_LOOKBACK", "BEARTREND_RSI_FLOOR", "BEARTREND_ATR_STOP",
    "BEARTREND_VERSION", "BEARTREND_VOL_MULT",
    "APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "BROKER",
    "COMMISSION_PER_TRADE", "CONCENTRATION_TOP_N", "CONCENTRATION_TOP_N_MAX",
    "CORRELATION_BLOCK", "CORRELATION_LOOKBACK", "CORRELATION_MIN_OBS",
    "CORRELATION_MODE", "CORRELATION_WARN", "DAILY_BARS_REFRESH_CYCLES",
    "DAILY_LOOKBACK_DAYS", "DAILY_LOSS_LIMIT", "DAILY_LOSS_PCT", "DATA_DIR", "DESK_BUDGET_PCT",
    "DRAWDOWN_SCALING", "DRAWDOWN_STATE_PATH", "ENABLED_SYSTEMS",
    "ENTRY_SETTLE_GRACE_SECS", "FINNHUB_API_KEY", "FLATTEN_BEFORE_CLOSE_MIN",
    "INTRADAY_ATR_MULT", "INTRADAY_BREAK_END", "INTRADAY_BREAK_START",
    "INTRADAY_SESSION_CLOSE", "INTRADAY_SESSION_OPEN", "INTRADAY_COOLDOWN_MIN", "INTRADAY_ENTRIES",
    "INTRADAY_LOOKBACK_MIN", "INTRADAY_MAX_POS", "INTRADAY_REQUIRE_VWAP",
    "INTRADAY_RESOLUTION", "INTRADAY_RV_GATE", "INTRADAY_SCORE_MIN",
    "INTRADAY_TRAIL_PCT", "INTRADAY_UNIVERSE", "INTRADAY_V2_GATE",
    "LIVE_CONFIRM", "LOSS_COOLDOWN_PATH", "MARKET_CLOSE", "MARKET_OPEN",
    "MAX_PARTICIPATION_PCT", "MAX_POSITION_PCT", "MAX_POSITION_SIZE",
    "MAX_SLIPPAGE_BPS", "MEANREV_ADX_MAX", "MEANREV_ADX_PERIOD",
    "MEANREV_BB_K", "MEANREV_BB_PERIOD", "MEANREV_LOSS_COOLDOWN_DAYS",
    "MEANREV_RS_LOOKBACK", "MEANREV_SCORE_MIN", "MEANREV_SCORING",
    "MEANREV_TIME_STOP_DAYS", "MEANREV_TRAIL_ATR", "MEANREV_VOL_DRY_RATIO",
    "MEANREV_VOL_EXIT_MULT", "MIN_DAILY_LOOKBACK_CALENDAR_DAYS",
    "MIN_DOLLAR_VOL", "MIN_PRICE", "MR_ATR_MULT", "MR_MAX_POS",
    "MR_RSI_EXIT", "MR_RSI_OVERSOLD", "MR_RSI_PERIOD", "MR_TREND_SMA",
    "NTFY_TOPIC", "OPENING_RANGE_MIN", "PORTFOLIO_HEAT_LOG",
    "PORTFOLIO_HEAT_MAX", "PORTFOLIO_HEAT_TAPER", "POSITION_STATE_PATH",
    "RANK_SIGNALS", "RATE_LIMIT_CALLS", "REGIME_ADX_MIN", "REGIME_ALLOC",
    "REGIME_ALLOC_CONF_BLEND", "REGIME_ALLOC_FAIL_TTL_SECS",
    "REGIME_ALLOC_FLOOR", "REGIME_ALLOC_TTL_SECS", "REGIME_FAIL_TTL_SECS",
    "REGIME_FILTER", "REGIME_PERSIST_DAYS", "REGIME_PERSIST_LOOKBACK",
    "REGIME_SMA_DAYS", "REGIME_SYMBOL", "REGIME_TTL_SECS", "REQUIRE_UPTREND",
    "RISK_PER_TRADE_PCT", "MEANREV_RISK_PCT", "INTRADAY_RISK_PCT",
    "XSECT_RISK_PCT", "SCAN_INTERVAL_SECS", "SECTOR_MAX_PCT",
    "SLIPPAGE_BPS", "START_EQUITY", "STOP_LOSS_PCT", "SWING_ATR_MULT",
    "SWING_BREAKOUT_DAYS", "SWING_ENTRIES", "SWING_LOSS_COOLDOWN_DAYS",
    "SWING_MAX_POS", "SWING_RISK_PCT", "SWING_V2_ADX",
    "SWING_V2_ADX_DECAY", "SWING_V2_ADX_MIN", "SWING_V2_ENTRY",
    "SWING_V2_MODE", "SWING_V2_RISK_PCT", "SWING_V2_ROUTE", "SWING_V2_RS",
    "SWING_V2_RS_LOOKBACK", "SWING_V2_STATE", "SWING_V2_TIME_STOP_DAYS",
    "SWING_V2_TRAIL_AFTER_R", "SWING_V2_TRAIL_ATR", "SWING_V2_VOL_EXIT",
    "SWING_V2_STAGED_LOCK", "SWING_V2_ADX_TRAIL", "SWING_V2_RS_EXIT",
    "SWING_VOL_MULT", "TAKE_PROFIT_R", "TRADING_MODE", "TRAIL_PCT",
    "TREND_SMA_DAYS", "UNIVERSE", "USE_BRACKET_ORDERS", "VOL_SPIKE_MULT",
    "XSECT_ABS_MOMENTUM", "XSECT_ENTRY_PERIODS", "XSECT_EXIT_RANK_PAD",
    "XSECT_MIN_RANKABLE", "XSECT_PERSIST_PATH", "XSECT_SECTOR_CAP",
    "XSECT_TRAIL", "XSECT_TRAIL_T1_ATR", "XSECT_TRAIL_T1_GAIN",
    "XSECT_TRAIL_T2_ATR", "XSECT_TRAIL_T2_GAIN", "XS_ATR_MULT",
    "XS_LOOKBACK", "XS_SKIP", "XS_TOP_N",
}

# The name you would naturally reach for -> the name the code actually reads.
# Setting the left-hand one does NOTHING, silently. Every entry here is a
# real trap someone has hit or would hit.
# Confirmed dead on the 2026-08-03 boot — set in Railway, read by nothing.
# Kept as explicit entries so the message names the replacement instead of
# guessing, and so removing them from Railway is an informed decision.
DEAD = {
    "TAKE_PROFIT_PCT": "TAKE_PROFIT_R (the code works in R multiples, not %)",
    "MAX_CONCURRENT_POSITIONS": "per-desk caps: SWING_MAX_POS, "
                                "INTRADAY_MAX_POS, MR_MAX_POS, XS_TOP_N",
    "MEANREV_USE_TAKE_PROFIT": "removed when the exit ladder replaced the "
                               "fixed take-profit (2026-07-23)",
    "XS_REBAL_CYCLES": "removed when rotation moved to a daily 10:00 gate",
}

ALIASES = {
    "MEANREV_MAX_POS": "MR_MAX_POS",
    "MEANREV_ATR_MULT": "MR_ATR_MULT",
    "MEANREV_RSI_OVERSOLD": "MR_RSI_OVERSOLD",
    "MEANREV_RSI_PERIOD": "MR_RSI_PERIOD",
    "MEANREV_RSI_EXIT": "MR_RSI_EXIT",
    "MEANREV_TREND_SMA": "MR_TREND_SMA",
    "XSECT_TOP_N": "XS_TOP_N",
    "XSECT_LOOKBACK": "XS_LOOKBACK",
    "XSECT_SKIP": "XS_SKIP",
    "XSECT_ATR_MULT": "XS_ATR_MULT",
    "SWING_V2_ENABLED": "SWING_V2_ROUTE",
    "PORTFOLIO_HEAT": "PORTFOLIO_HEAT_MAX",
    "CORRELATION_MAX": "CORRELATION_BLOCK",
}

# name -> (low, high, note). Inclusive.
RANGES = {
    "RISK_PER_TRADE_PCT": (0.0001, 0.05, "fraction of equity, not percent"),
    "SWING_RISK_PCT": (0.0001, 0.05, "fraction of equity, not percent"),
    "XSECT_RISK_PCT": (0.0001, 0.05, "fraction of equity, not percent"),
    "INTRADAY_RISK_PCT": (0.0001, 0.05, "fraction of equity, not percent"),
    "MEANREV_RISK_PCT": (0.0001, 0.05, "fraction of equity, not percent"),
    "SWING_V2_RISK_PCT": (0.0001, 0.05, "fraction of equity, not percent"),
    "MAX_POSITION_PCT": (0.01, 1.0, "fraction of equity"),
    "PORTFOLIO_HEAT_MAX": (0.0, 1.0, "fraction; 0 = MEASURE ONLY"),
    "PORTFOLIO_HEAT_TAPER": (0.0, 1.0, "fraction; 0 = off"),
    "SECTOR_MAX_PCT": (0.0, 1.0, "fraction; 0 = MEASURE ONLY"),
    "DESK_BUDGET_PCT": (0.0, 1.0, "fraction of equity PER DESK; 0 = MEASURE ONLY"),
    "CONCENTRATION_TOP_N_MAX": (0.0, 1.0, "fraction; 0 = MEASURE ONLY"),
    "MAX_PARTICIPATION_PCT": (0.0, 1.0, "fraction; 0 = MEASURE ONLY"),
    "CORRELATION_WARN": (0.0, 1.0, "correlation coefficient"),
    "CORRELATION_BLOCK": (0.0, 1.0, "correlation coefficient"),
    "INTRADAY_SCORE_MIN": (0.0, 1.0, "score is 0-1"),
    "MEANREV_SCORE_MIN": (0, 6, "score is 0-6"),
    "REGIME_ALLOC_FLOOR": (0.0, 1.0, "multiplier floor"),
    "REGIME_ADX_MIN": (0, 100, "ADX is 0-100"),
    "SWING_V2_ADX_MIN": (0, 100, "ADX is 0-100"),
    "XSECT_SECTOR_CAP": (0, 10, "names per sector; 0 = uncapped"),
    "SWING_MAX_POS": (1, 20, None),
    "INTRADAY_MAX_POS": (1, 20, None),
    "MR_MAX_POS": (1, 20, None),
    "XS_TOP_N": (1, 20, None),
}

ENUMS = {
    "BEARTREND_MODE": {"off", "shadow"},
    "CORRELATION_MODE": {"measure", "enforce"},
    "MEANREV_SCORING": {"off", "shadow", "live"},
    "REGIME_ALLOC": {"off", "shadow", "live"},
    "SWING_V2_MODE": {"off", "shadow", "live"},
    "SWING_V2_ENTRY": {"A", "B"},
    "DRAWDOWN_SCALING": {"measure", "on", "off", "live", "true", "false"},
    "TRADING_MODE": {"paper", "live"},
}

_TRUE = {"1", "true", "on", "yes"}


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in _TRUE


def _num(name):
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return None
    try:
        return float(v)
    except ValueError:
        return "NaN"


def validate() -> tuple[int, int]:
    """Log every finding. Returns (errors, warnings). NEVER raises, never
    blocks boot — a config validator that halts trading is a worse bug than
    the ones it catches."""
    errors, warns, infos = [], [], []

    # ---- typos and dead config ----------------------------------------
    for k in sorted(os.environ):
        if k in DEAD:
            errors.append(f"{k} is set but nothing reads it — {DEAD[k]}. "
                          f"Remove it from Railway.")
        elif k in ALIASES:
            errors.append(f"{k} is set but the code reads {ALIASES[k]} — "
                          f"this setting does NOTHING. Rename it.")
        elif k not in KNOWN:
            # Fuzzy match first: a transposition like SWNIG_MAX_POS shares no
            # prefix with anything known, so a prefix test alone misses the
            # most common kind of typo.
            near = difflib.get_close_matches(k, KNOWN, n=1, cutoff=0.85)
            if near:
                errors.append(f"{k} is set but is not read by any module — "
                              f"did you mean {near[0]}? As written it does "
                              f"NOTHING.")
            elif k.startswith(("SWING", "INTRADAY", "MEANREV", "MR_", "XS",
                               "REGIME", "PORTFOLIO", "CORRELATION",
                               "DRAWDOWN", "CONCENTRATION", "TRADE", "RISK",
                               "MAX_", "DAILY", "FLATTEN", "SECTOR")):
                warns.append(f"{k} looks like a trading setting but no module "
                             f"reads it — typo, or left over from a removed "
                             f"feature?")

    # ---- ranges --------------------------------------------------------
    for name, (lo, hi, note) in RANGES.items():
        v = _num(name)
        if v is None:
            continue
        if v == "NaN":
            errors.append(f"{name}={os.getenv(name)!r} is not a number")
        elif not (lo <= v <= hi):
            errors.append(f"{name}={v} is outside [{lo}, {hi}]"
                          + (f" — {note}" if note else ""))

    # ---- enums ---------------------------------------------------------
    for name, allowed in ENUMS.items():
        v = os.getenv(name)
        if v and v.strip().lower() not in {a.lower() for a in allowed}:
            errors.append(f"{name}={v!r} is not one of "
                          f"{sorted(allowed)} — the code will fall back to "
                          f"its default, silently")

    # ---- contradictions (each one has bitten this project) -------------
    if _truthy(os.getenv("SWING_V2_ROUTE")) and \
            os.getenv("SWING_ENTRIES", "true").strip().lower() not in _TRUE:
        errors.append("SWING_V2_ROUTE=on but SWING_ENTRIES is off — swing_v2 "
                      "will generate signals that the engine then refuses. "
                      "The desk looks live and trades nothing.")

    taper, hmax = _num("PORTFOLIO_HEAT_TAPER"), _num("PORTFOLIO_HEAT_MAX")
    if taper and taper > 0 and (not hmax or hmax == 0):
        warns.append("PORTFOLIO_HEAT_TAPER is set while PORTFOLIO_HEAT_MAX=0 "
                     "(measure-only) — the taper has no ceiling to taper "
                     "toward and will use a degenerate span.")
    if taper and hmax and 0 < hmax <= taper:
        errors.append(f"PORTFOLIO_HEAT_TAPER={taper} >= PORTFOLIO_HEAT_MAX="
                      f"{hmax} — tapering would start at or after the halt.")

    db = _num("DESK_BUDGET_PCT")
    if db and db > 0:
        n_desks = len([d for d in (os.getenv("ENABLED_SYSTEMS") or
                                   "swing,intraday,meanrev,xsectmom").split(",")
                       if d.strip()])
        if db * n_desks < 0.5:
            warns.append(f"DESK_BUDGET_PCT={db:.0%} x {n_desks} desks = "
                         f"{db*n_desks:.0%} of equity — the book cannot get "
                         f"meaningfully invested. Intended?")
        if db * n_desks > 1.5:
            infos.append(f"DESK_BUDGET_PCT={db:.0%} x {n_desks} desks = "
                         f"{db*n_desks:.0%} — budgets overlap, so cash still "
                         f"binds first. That is fine, but the budget is not "
                         f"the constraint you think it is.")

    warn_c, block_c = _num("CORRELATION_WARN"), _num("CORRELATION_BLOCK")
    if warn_c and block_c and warn_c > block_c:
        errors.append(f"CORRELATION_WARN={warn_c} > CORRELATION_BLOCK="
                      f"{block_c} — reduce would trigger above reject, so "
                      f"REDUCE can never fire.")

    if os.getenv("BEARTREND_MODE", "").strip().lower() not in \
            ("", "off", "shadow"):
        errors.append("BEARTREND_MODE only supports 'off' and 'shadow' — the "
                      "module RAISES on anything else at import, so the bot "
                      "will not boot. There is no short execution path.")

    if (os.getenv("SWING_V2_MODE", "").strip().lower() == "live"):
        warns.append("SWING_V2_MODE=live is REFUSED in code (v2 orders would "
                     "orphan at reconcile). Use SWING_V2_ROUTE=on to route "
                     "its signals through swing_engine instead.")

    # The intraday break is now configurable, so a fat-fingered HH:MM can
    # silently widen or delete it. The module already reverts to defaults on
    # a nonsense schedule; say so here too, at boot, where it is read.
    for _v in ("INTRADAY_BREAK_START", "INTRADAY_BREAK_END",
               "INTRADAY_SESSION_OPEN", "INTRADAY_SESSION_CLOSE"):
        _raw = os.getenv(_v, "").strip()
        if _raw and not re.match(r"^([01]?\d|2[0-3]):[0-5]\d$", _raw):
            errors.append(f"{_v}={_raw!r} is not HH:MM — intraday will fall "
                          f"back to its default schedule and this setting "
                          f"will do NOTHING.")
    try:
        import intraday_scoring as _isc
        _b = ((_isc.BREAK_END_ET[0] * 60 + _isc.BREAK_END_ET[1])
              - (_isc.BREAK_START_ET[0] * 60 + _isc.BREAK_START_ET[1]))
        infos.append(f"INTRADAY schedule: trades "
                     f"{_isc.SESSION_OPEN_ET[0]:02d}:{_isc.SESSION_OPEN_ET[1]:02d}"
                     f"-{_isc.SESSION_CLOSE_ET[0]:02d}:{_isc.SESSION_CLOSE_ET[1]:02d} ET "
                     f"with a {_b}-minute break "
                     f"{_isc.BREAK_START_ET[0]:02d}:{_isc.BREAK_START_ET[1]:02d}"
                     f"-{_isc.BREAK_END_ET[0]:02d}:{_isc.BREAK_END_ET[1]:02d}")
    except Exception:  # noqa: BLE001
        pass

    look = _num("DAILY_LOOKBACK_DAYS")
    floor = _num("MIN_DAILY_LOOKBACK_CALENDAR_DAYS") or 500
    if look and look < floor:
        warns.append(f"DAILY_LOOKBACK_DAYS={look:.0f} is below the feed's "
                     f"floor of {floor:.0f} and will be silently raised. Set "
                     f"it to {floor:.0f} to stop the boot warning.")

    enabled = (os.getenv("ENABLED_SYSTEMS") or "").lower()
    if enabled:
        for sysname, gate in (("swing", "SWING_ENTRIES"),
                              ("intraday", "INTRADAY_ENTRIES")):
            if sysname in enabled and \
                    os.getenv(gate, "true").strip().lower() not in _TRUE:
                infos.append(f"{sysname} is in ENABLED_SYSTEMS but {gate} is "
                             f"off — it will scan and log, not trade.")

    # ---- promotion registry: what is approved, and is anything using it? -
    try:
        import promotion_registry as _pr
        _desks = [d for d in _pr.SCHEMA if _pr.load(d)]
        if _desks:
            infos.append("PROMOTION artifacts loaded for: " + ", ".join(
                f"{d} ({_pr._source.get(d, '?')})" for d in _desks))
        else:
            infos.append("PROMOTION: no approved artifacts — every desk is "
                         "running code defaults and environment variables. "
                         "Expected until research promotes something.")
    except Exception as e:  # noqa: BLE001 — a validator must never block boot
        warns.append(f"promotion registry unreadable ({e}) — desks will use "
                     f"code defaults")

    # ---- measure-only inventory, so nothing is assumed live -------------
    measure = []
    for name, label in (("PORTFOLIO_HEAT_MAX", "portfolio heat"),
                        ("SECTOR_MAX_PCT", "sector budget"),
                        ("DESK_BUDGET_PCT", "desk capital budget"),
                        ("CONCENTRATION_TOP_N_MAX", "top-N concentration"),
                        ("MAX_PARTICIPATION_PCT", "liquidity participation")):
        v = _num(name)
        if not v:
            measure.append(label)
    if os.getenv("CORRELATION_MODE", "measure").lower() != "enforce":
        measure.append("correlation")
    if os.getenv("DRAWDOWN_SCALING", "measure").lower() not in ("on", "live"):
        measure.append("drawdown scaling")
    if measure:
        infos.append("MEASURE-ONLY (logging, not enforcing): "
                     + ", ".join(measure))

    # ---- report --------------------------------------------------------
    for m in errors:
        log.error("CONFIG ERROR: %s", m)
    for m in warns:
        log.warning("CONFIG WARN: %s", m)
    for m in infos:
        log.warning("CONFIG INFO: %s", m)
    log.warning("CONFIG CHECK: %d error(s), %d warning(s) — startup "
                "continues either way", len(errors), len(warns))
    return len(errors), len(warns)
