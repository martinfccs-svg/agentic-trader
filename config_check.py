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

import ast
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
    "AUDIT_LOG_PATH", "FUNNEL_EMIT_SECS", "LIVE_TRADING_CONFIRMED",
    "MEANREV_LOOKBACK_BARS", "MEANREV_REVERSAL", "NTFY_SERVER", "NTFY_TOKEN",
    "REBALANCE_GATE_STATE", "SWING_LOOKBACK_BARS", "TRADES_LOG_PATH",
    "XSECT_LOOKBACK_BARS", "XSECT_REBALANCE_ET",
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
    "DAILY_LOOKBACK_DAYS", "DAILY_LOSS_LIMIT", "DAILY_LOSS_PCT", "DATA_DIR", "DEGRADED_AFTER_CYCLES", "SAME_TICKER_MAX_PCT", "DESK_BUDGET_PCT",
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
    "PORTFOLIO_HEAT_MAX", "PORTFOLIO_HEAT_TAPER", "POSITION_STATE_PATH", "PROMOTION_DIR", "QUARANTINE_FAILURES",
    "QUARANTINE_MINUTES",
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
    "SWING_V2_STAGED_LOCK", "SWING_V2_PROFIT_LOCK", "SWING_V2_ADX_TRAIL", "SWING_V2_RS_EXIT",
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
    "DEGRADED_AFTER_CYCLES": (1, 60, "consecutive failures before escalating"),
    "DESK_BUDGET_PCT": (0.0, 1.0, "fraction of equity PER DESK; 0 = MEASURE ONLY"),
    "SAME_TICKER_MAX_PCT": (0.0, 1.0, "combined exposure to ONE ticker across desks; 0 = MEASURE ONLY"),
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
    "QUARANTINE_FAILURES": (1, 20, "routing failures before a ticker is benched"),
    "QUARANTINE_MINUTES": (1, 240, "minutes benched"),
    "SWING_MAX_POS": (1, 20, None),
    "INTRADAY_MAX_POS": (1, 20, None),
    "MR_MAX_POS": (1, 20, None),
    "XS_TOP_N": (1, 20, None),
}

ENUMS = {
    "BEARTREND_MODE": {"off", "shadow"},
    "MEANREV_REVERSAL": {"off", "shadow", "live"},
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


def fatal_invariants() -> list:
    """Conditions under which the system must NOT open new positions.

    config_check has always refused to block boot, and that stays right: a
    validator that halts a trading process over a typo is a worse failure
    than the typo. But there is a class where continuing is worse than
    stopping — trading with no credentials, or in a mode nobody confirmed.

    So the escalation is not "block boot", it is "block ENTRIES". The process
    still boots, still reconciles, still MANAGES AND EXITS open positions.
    Refusing to boot would strand an open book with no manager, which is the
    one outcome worse than not trading.
    """
    bad = []
    key = os.getenv("ALPACA_API_KEY") or os.getenv("APCA_API_KEY_ID")
    sec = os.getenv("ALPACA_SECRET_KEY") or os.getenv("APCA_API_SECRET_KEY")
    if not key or not sec:
        bad.append("no broker credentials — cannot place or verify orders")
    base = (os.getenv("APCA_API_BASE_URL") or "").lower()
    paper_flag = (os.getenv("ALPACA_PAPER", "true").strip().lower()
                  not in ("false", "0", "no", "off"))
    if base and "paper" not in base and paper_flag:
        bad.append(f"APCA_API_BASE_URL={base!r} is a LIVE endpoint while "
                   f"ALPACA_PAPER says paper — the two disagree about whether "
                   f"this is real money")
    if not paper_flag and os.getenv("LIVE_TRADING_CONFIRMED", "").strip() != "yes":
        bad.append("ALPACA_PAPER=false but LIVE_TRADING_CONFIRMED is not "
                   "'yes' — real-money trading requires an explicit second "
                   "confirmation")
    enabled = (os.getenv("ENABLED_SYSTEMS") or "").strip()
    if enabled and not any(d in enabled for d in
                           ("swing", "intraday", "meanrev", "xsectmom")):
        bad.append(f"ENABLED_SYSTEMS={enabled!r} names no known desk")
    return bad


_HASH_CACHE = None


def active_config_report() -> list:
    """What each desk is ACTUALLY running, grouped and resolved.

    The GATES banner is one dense line covering 24 settings across four
    desks, which is fine for spotting a change and poor for answering "what
    is meanrev doing right now?". This prints the resolved value of every
    setting that governs a desk, grouped by desk — the value in force, not
    the code default, so an operator reading the log sees what the machine
    sees.

    Built from the module constants rather than re-reading the environment,
    so a variable that is set but IGNORED (wrong name, invalid enum) shows
    its real effect here instead of its intended one.
    """
    out = ["", "=" * 66, "ACTIVE STRATEGY CONFIG", "=" * 66]

    def sect(name, pairs):
        out.append(f"\n  {name}")
        for k, v in pairs:
            out.append(f"    {k:<16} = {v}")

    try:
        import swing_v2 as _sv2
        import position_sizing as _ps
        from config import SWING
        sect("SWING", [
            ("route", "ON (swing runs swing_v2)" if _sv2.ROUTE_LIVE else "off"),
            ("risk", f"{_ps.risk_pct('swing', 'v2' if _sv2.ROUTE_LIVE else None):.4%}"),
            ("max_pos", SWING.max_positions),
            ("adx_gate", f">={_sv2.ADX_MIN:g}" if _sv2.ADX_REQUIRED else "off"),
            ("rel_strength", "on" if _sv2.RS_REQUIRED else "off"),
            ("time_stop", f"{_sv2.TIME_STOP_DAYS}d without +1R"),
            ("setup_expiry", f"{_sv2.SETUP_EXPIRY_DAYS}d"),
        ])
    except Exception as e:  # noqa: BLE001
        out.append(f"\n  SWING (unavailable: {e})")

    try:
        import meanrev_scoring as _mrs
        from config import MEANREV
        sect("MEANREV", [
            ("scoring", _mrs.SCORING_MODE),
            ("score_min", f"{_mrs.SCORE_MIN}/6"),
            ("reversal", _mrs.REVERSAL_MODE),
            ("rsi_oversold", MEANREV.rsi_oversold),
            ("max_pos", MEANREV.max_positions),
        ])
    except Exception as e:  # noqa: BLE001
        out.append(f"\n  MEANREV (unavailable: {e})")

    try:
        import intraday_scoring as _isc
        from config import INTRADAY
        sect("INTRADAY", [
            ("score_min", _isc.SCORE_MIN),
            ("rv_gate", _isc.RV_GATE),
            ("session", _isc.schedule_text()),
            ("cooldown", f"{_isc.COOLDOWN_MIN}min after a loss"),
            ("max_pos", INTRADAY.max_positions),
            ("trail", f"{INTRADAY.trail_pct:.1%}"),
        ])
    except Exception as e:  # noqa: BLE001
        out.append(f"\n  INTRADAY (unavailable: {e})")

    try:
        import portfolio_manager as _pm

        def _mode(v, fmt="{:.0%}"):
            return fmt.format(v) if v > 0 else "0 (MEASURE ONLY)"
        sect("PORTFOLIO", [
            ("heat_max", _mode(_pm.HEAT_MAX)),
            ("sector_max", _mode(_pm.SECTOR_MAX_PCT)),
            ("desk_budget", _mode(_pm.DESK_BUDGET_PCT)),
            ("same_ticker", _mode(_pm.SAME_TICKER_MAX_PCT)),
            ("top_n_max", _mode(_pm.TOP_N_MAX_PCT)),
            ("participation", _mode(_pm.MAX_PARTICIPATION, "{:.2%}")),
            ("drawdown", _pm.DD_SCALE),
        ])
    except Exception as e:  # noqa: BLE001
        out.append(f"\n  PORTFOLIO (unavailable: {e})")

    out.append("=" * 66)
    return out


def active_hash() -> str:
    """The config fingerprint, cached — the ONE accessor engines should use.

    swing_engine and intraday_engine each grew a private _config_hash() with
    its own cache. A third and fourth copy for meanrev and xsect is how four
    ATR trails happened. One definition, cached here.
    """
    global _HASH_CACHE
    if _HASH_CACHE is None:
        try:
            _HASH_CACHE = config_hash()[0]
        except Exception:  # noqa: BLE001
            _HASH_CACHE = "unknown"
    return _HASH_CACHE


def config_hash() -> tuple:
    """(hash, [the settings it covers]) — a fingerprint of the ACTIVE config.

    Answers a question the logs could not: six weeks from now, looking at
    trade #1842, what configuration produced it? Boot banners scroll away and
    Railway variables change without leaving a mark in the trade record.

    Hashes the RESOLVED value of every KNOWN variable — the value in force,
    not the code default — so two boots with the same hash ran the same
    machine. A changed hash is the signal to go look at what moved.

    Excludes secrets by name so the fingerprint can be logged and pasted
    freely; rotating an API key must not look like a config change.
    """
    import hashlib
    secret_ish = ("KEY", "SECRET", "TOKEN", "PASSWORD", "WEBHOOK", "DSN")
    pairs = []
    for name in sorted(KNOWN):
        if any(s in name for s in secret_ish):
            continue
        raw = os.getenv(name)
        if raw is None or raw.strip() == "":
            continue                     # unset = the code default
        pairs.append(f"{name}={raw.strip()}")
    blob = "\n".join(pairs).encode()
    return hashlib.sha256(blob).hexdigest()[:8].upper(), pairs


def audit_known(repo_files: list = None) -> list:
    """Which variables does the DEPLOYED code read that KNOWN does not list?

    The validator's whole value is telling you a setting does nothing. That
    only works if KNOWN is complete — and KNOWN is hand-maintained, so it
    drifts the moment a module adds a variable and nobody updates it here.
    Three had already slipped through (PROMOTION_DIR, QUARANTINE_FAILURES,
    QUARANTINE_MINUTES) before this check existed.

    Deliberately scoped to the LIVE import graph. Local tools and archived
    code read variables that production never sees, and listing those would
    make KNOWN claim authority over settings it does not govern.
    """
    import os as _os
    import re as _re
    here = _os.path.dirname(_os.path.abspath(__file__))
    if repo_files is None:
        try:
            # Walk main.py's imports from SOURCE — no `import main` needed.
            # Importing it to decide whether to audit would make the audit
            # silently skip in any environment where main cannot import,
            # which is exactly where a stale KNOWN would go unnoticed.
            seen, todo = set(), ["main"]
            local = {f[:-3] for f in _os.listdir(here) if f.endswith(".py")}
            while todo:
                mod = todo.pop()
                path = _os.path.join(here, mod + ".py")
                if mod in seen or not _os.path.exists(path):
                    continue
                seen.add(mod)
                tree = ast.parse(open(path, encoding="utf-8").read())
                for n in ast.walk(tree):
                    if isinstance(n, ast.Import):
                        todo += [a.name.split(".")[0] for a in n.names
                                 if a.name.split(".")[0] in local]
                    elif isinstance(n, ast.ImportFrom) and n.module:
                        if n.module.split(".")[0] in local:
                            todo.append(n.module.split(".")[0])
            repo_files = [_os.path.join(here, m + ".py") for m in seen]
        except Exception:  # noqa: BLE001
            return []
    read = set()
    pat = _re.compile(r'(?:getenv|environ\.get)\(\s*["\']([A-Z_0-9]+)["\']')
    for p in repo_files:
        try:
            src = open(p, encoding="utf-8").read()
        except OSError:
            continue
        read |= set(pat.findall(src))
        read |= set(_re.findall(r'_[fisb]\(\s*["\']([A-Z_0-9]+)["\']', src))
    return sorted(read - KNOWN - set(ALIASES) - set(DEAD)
                  - {"RAILWAY_DEPLOYMENT_ID"})


ENTRIES_BLOCKED: list = []      # set by validate(); read by main.py


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
    _bad_enums = []
    for name, allowed in ENUMS.items():
        v = os.getenv(name)
        if v and v.strip().lower() not in {a.lower() for a in allowed}:
            # BLOCKS ENTRIES (2026-08-13). A typo'd enum used to fall back to
            # the code default and keep trading — so SWING_V2_MODE=liv would
            # run the DEFAULT mode while the operator believed they had set
            # something. The operator's intent is unknown and the fallback is
            # a guess; guessing about which strategy mode to run is not a
            # safe default. Existing positions are still managed.
            _bad_enums.append(f"{name}={v!r}")
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

    # ---- does the validator still know every LIVE variable? -------------
    try:
        _unknown = audit_known()
        if _unknown:
            warns.append("config_check does not know these variables that "
                         "live code READS: " + ", ".join(_unknown)
                         + " — they cannot be range-checked or typo-detected "
                           "until they are added to KNOWN.")
    except Exception:  # noqa: BLE001 — a self-audit must never block boot
        pass

    # ---- FATAL INVARIANTS: block ENTRIES, never boot ---------------------
    _fatal = []
    try:
        _fatal = fatal_invariants()
    except Exception:  # noqa: BLE001
        pass
    # An invalid enum joins the fatal set: the process boots and manages the
    # book, but opens nothing new until the value is corrected.
    if _bad_enums:
        _fatal = list(_fatal) + [
            "invalid enum value(s): " + ", ".join(_bad_enums)
            + " — the intended mode is unknowable, so entries are blocked "
              "rather than run under a guessed default"]
    ENTRIES_BLOCKED[:] = _fatal
    for f in _fatal:
        errors.append(f"FATAL: {f}")
    if _fatal:
        log.critical("ENTRIES BLOCKED — %d fatal invariant(s) failed. The "
                     "process will boot, reconcile and MANAGE OPEN POSITIONS, "
                     "but will open nothing new until these are fixed: %s",
                     len(_fatal), "; ".join(_fatal))

    # ---- CONFIG FINGERPRINT ---------------------------------------------
    try:
        _h, _pairs = config_hash()
        infos.append(f"CONFIG_HASH={_h} ({len(_pairs)} variables set; unset "
                     f"variables use code defaults and are excluded)")
    except Exception:  # noqa: BLE001
        pass

    # ---- can the registry CARRY everything the sweep can promote? -------
    try:
        import promotion_registry as _prs
        _gap = _prs.audit_schema()
        if _gap.get("missing_from_schema"):
            warns.append("promotion registry cannot carry these swing exit "
                         "options: " + ", ".join(_gap["missing_from_schema"])
                         + " — if research promotes one it will be silently "
                           "DROPPED. Add it to promotion_registry.SCHEMA.")
    except Exception:  # noqa: BLE001
        pass

    # ---- promotion registry: what is approved, and is anything using it? -
    try:
        import promotion_registry as _pr
        # DISTINGUISH an approved artifact from an env override. load()
        # returns a non-empty dict when an operator has set a variable, even
        # with NO artifact on disk — so a truthiness test reported
        # "PROMOTION artifacts loaded for: xsection (defaults (no artifact))",
        # which contradicts itself in a single line. Anyone reading it would
        # conclude research had promoted something. Nothing had.
        # load() FIRST — it is what populates _source. Reading _source before
        # calling it returns "" for every desk, which does not start with
        # "defaults", so every desk looked like it had an artifact. The same
        # evaluation-order mistake the message was meant to fix.
        _loaded = {d: bool(_pr.load(d)) for d in _pr.SCHEMA}
        _with_artifact = [d for d, ok in _loaded.items() if ok and not
                          _pr._source.get(d, "").startswith("defaults")]
        _env_only = [d for d, ok in _loaded.items()
                     if ok and d not in _with_artifact]
        if _with_artifact:
            infos.append("PROMOTION artifacts loaded for: " + ", ".join(
                f"{d} ({_pr._source.get(d, '?')})" for d in _with_artifact))
        if _env_only:
            infos.append("PROMOTION: no artifacts; these desks take settings "
                         "from ENVIRONMENT VARIABLES only: "
                         + ", ".join(_env_only))
        if not _with_artifact:
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
                        ("SAME_TICKER_MAX_PCT", "same-ticker across desks"),
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
