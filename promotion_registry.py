"""promotion_registry.py — production reads what research approved.

    research  ->  backtest + sweep + reality check
              ->  promotion artifact (versioned JSON)
              ->  active.json says which version is live
    production ->  loads it, trades it, never questions it

The point of the split: production stops asking "should I promote this?" and
asks only "what has already been promoted?". Rolling back becomes editing one
line of active.json instead of a redeploy.

WHY THIS FILE IS MORE DANGEROUS THAN IT LOOKS

It lets a JSON file on disk change trading behaviour without a code deploy.
That is the feature, and it is also the hazard: anything that can write /data
can now size your positions. This project has already deleted one small file
with outsized authority (startup_flatten.py) and hardened another that was a
bare {"approved": true} away from authorising real capital.

So the registry is built the same way:

  1. VALIDATED, NOT TRUSTED. Every value is range- and type-checked against
     a schema declared HERE, in deployed code. An artifact proposing
     sector_cap=0 or risk_pct=0.5 is rejected, not applied.
  2. ENV WINS. An explicitly-set environment variable always beats the
     artifact, and the override is logged. An operator reaching for a switch
     during a bad morning must not be silently overruled by a file written
     on Saturday.
  3. FAIL SAFE, NOT FAIL OPEN. A missing, corrupt or invalid artifact falls
     back to code defaults and says so loudly. Trading continues on known
     values; it never continues on garbage.
  4. STATED, NOT INFERRED. What loaded, from which version, appears in the
     boot banner — the same rule as every other flag in this system.

WHAT IT DOES NOT DO
It does not approve anything. Approval happens in research, against rules
fixed before the numbers existed. This file only carries the decision across
the boundary.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

log = logging.getLogger("promotion")

REGISTRY_DIR = os.getenv("PROMOTION_DIR", "/data/promotion")
ACTIVE_FILE = "active.json"

# The schema lives in DEPLOYED CODE, not in the artifact. An artifact cannot
# widen its own limits — that is the difference between configuration and
# authority. (min, max, type)
SCHEMA: dict[str, dict[str, tuple]] = {
    "xsection": {
        "lookback": (20, 400, int),
        "skip": (0, 20, int),
        "sector_cap": (1, 10, int),      # 0 would UNCAP; never from a file
        "max_positions": (1, 10, int),
        "regime_filter": (0, 1, bool),
    },
    "swing": {
        "risk_pct": (0.001, 0.02, float),
        "trail_atr": (0.0, 5.0, float),
        "trail_after_r": (0.0, 5.0, float),
        "vol_exit_mult": (0.0, 5.0, float),
        "adx_decay_frac": (0.0, 1.0, float),
        "rs_exit_lag": (0.0, 0.5, float),
        "time_stop_days": (3, 60, int),
        "staged_lock": (0, 1, bool),
        "adx_trail": (0, 1, bool),
        "percent_lock": (0, 1, bool),
    },
    "meanrev": {
        "score_min": (0, 6, int),
        "time_stop_days": (3, 40, int),
        "max_positions": (1, 10, int),
    },
    "intraday": {
        "score_min": (0.0, 1.0, float),
        "rv_gate": (1.0, 10.0, float),
        "max_positions": (1, 10, int),
    },
}

# Which env var, if explicitly set, overrides each key. Operator beats file.
ENV_OVERRIDE: dict[str, dict[str, str]] = {
    "xsection": {"sector_cap": "XSECT_SECTOR_CAP", "lookback": "XS_LOOKBACK",
                 "skip": "XS_SKIP", "max_positions": "XS_TOP_N"},
    "swing": {"risk_pct": "SWING_RISK_PCT", "trail_atr": "SWING_V2_TRAIL_ATR",
              "percent_lock": "SWING_V2_PROFIT_LOCK",
              "staged_lock": "SWING_V2_STAGED_LOCK",
              "adx_trail": "SWING_V2_ADX_TRAIL",
              "vol_exit_mult": "SWING_V2_VOL_EXIT",
              "adx_decay_frac": "SWING_V2_ADX_DECAY",
              "rs_exit_lag": "SWING_V2_RS_EXIT",
              "time_stop_days": "SWING_V2_TIME_STOP_DAYS"},
    "meanrev": {"score_min": "MEANREV_SCORE_MIN", "max_positions": "MR_MAX_POS"},
    "intraday": {"score_min": "INTRADAY_SCORE_MIN",
                 "max_positions": "INTRADAY_MAX_POS"},
}

_loaded: dict[str, dict] = {}
_source: dict[str, str] = {}


def _coerce(value: Any, kind) -> Any:
    if kind is bool:
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "on", "yes")
        return bool(value)
    return kind(value)


def _validate(desk: str, raw: dict) -> tuple[dict, list]:
    """Keep only keys that exist in the schema AND pass their range.

    Unknown keys are DROPPED with a warning rather than passed through: an
    artifact that can introduce arbitrary settings is an artifact that can
    introduce a typo nobody notices.
    """
    schema = SCHEMA.get(desk, {})
    clean, problems = {}, []
    for k, v in (raw or {}).items():
        if k in ("version", "status", "approved_by", "approval_id", "notes"):
            continue
        if k not in schema:
            problems.append(f"{k}: not a known {desk} setting — dropped")
            continue
        lo, hi, kind = schema[k]
        try:
            val = _coerce(v, kind)
        except (TypeError, ValueError):
            problems.append(f"{k}={v!r}: not a {kind.__name__} — dropped")
            continue
        num = float(val)
        if not (lo <= num <= hi):
            problems.append(f"{k}={val}: outside [{lo}, {hi}] — dropped")
            continue
        clean[k] = val
    return clean, problems


def audit_schema(policy_fields: dict = None) -> dict:
    """Which swing exit options can the SWEEP promote that the registry
    cannot carry?

    The registry is the only path from a research decision to production. A
    setting missing from SCHEMA is silently DROPPED — validated away as "not
    a known setting" — so a promoted improvement would never reach the desk
    and the only evidence would be one log line. That already happened once:
    percent_lock shipped as sweep variant 9 while the schema knew nothing
    about it.

    Compares SCHEMA["swing"] against SwingExitConfig's fields, since those
    are exactly what the sweep can turn on.
    """
    if policy_fields is None:
        try:
            import dataclasses
            import swing_exit_policy
            policy_fields = {f.name for f in
                             dataclasses.fields(swing_exit_policy.SwingExitConfig)}
        except Exception:  # noqa: BLE001
            return {}
    known = set(SCHEMA.get("swing", {}))
    # grace/require fields are mechanics, not promotable knobs
    skip = {"ema20_grace_days", "rs_exit_min_days", "time_stop_require_r"}
    missing = sorted((policy_fields - known) - skip)
    extra = sorted(known - policy_fields)
    return {"missing_from_schema": missing, "in_schema_not_in_policy": extra}


def load(desk: str) -> dict:
    """Approved settings for one desk. Never raises; never returns garbage.

    Precedence, highest first:
        1. an explicitly-set environment variable
        2. the approved artifact
        3. nothing — the caller's own default applies
    """
    if desk in _loaded:
        return _loaded[desk]
    cfg, src = {}, "defaults (no artifact)"
    try:
        active_path = os.path.join(REGISTRY_DIR, ACTIVE_FILE)
        with open(active_path, encoding="utf-8") as fh:
            active = json.load(fh)
        version = active.get(desk)
        if not version:
            src = f"defaults ({desk} not listed in {ACTIVE_FILE})"
        else:
            with open(os.path.join(REGISTRY_DIR, f"{version}.json"),
                      encoding="utf-8") as fh:
                raw = json.load(fh)
            if str(raw.get("status", "")).lower() != "approved":
                log.error("promotion: %s.json status=%r, not 'approved' — "
                          "IGNORED. Falling back to defaults.", version,
                          raw.get("status"))
                src = f"defaults ({version} not approved)"
            else:
                cfg, problems = _validate(desk, raw)
                for p in problems:
                    log.error("promotion %s/%s: %s", desk, version, p)
                src = f"{version}.json"
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001 — a bad file must not stop trading
        log.error("promotion: could not read the registry for %s (%s) — "
                  "using defaults", desk, e)
        src = "defaults (registry unreadable)"

    # Operator overrides last, so a switch thrown by hand always wins.
    for key, env_name in ENV_OVERRIDE.get(desk, {}).items():
        raw_env = os.getenv(env_name)
        if raw_env is None or raw_env.strip() == "":
            continue
        lo, hi, kind = SCHEMA[desk][key]
        try:
            val = _coerce(raw_env, kind)
            if not (lo <= float(val) <= hi):
                raise ValueError("out of range")
        except (TypeError, ValueError):
            log.error("promotion %s: %s=%r is invalid — ignoring the "
                      "override", desk, env_name, raw_env)
            continue
        if key in cfg and cfg[key] != val:
            log.warning("promotion %s: %s=%s OVERRIDES the approved %s=%s "
                        "from %s", desk, env_name, val, key, cfg[key], src)
        cfg[key] = val

    _loaded[desk], _source[desk] = cfg, src
    return cfg


def get(desk: str, key: str, default):
    """One approved value, or the caller's default. The default stays in the
    calling module so a missing registry changes nothing."""
    return load(desk).get(key, default)


def banner() -> str:
    """One line for the boot banner. What loaded, from where — stated."""
    if not _loaded:
        return "PROMOTION: nothing loaded yet"
    parts = []
    for desk in sorted(_loaded):
        n = len(_loaded[desk])
        parts.append(f"{desk}={_source.get(desk, '?')}({n} setting"
                     f"{'' if n == 1 else 's'})")
    return "PROMOTION: " + " ".join(parts)


def describe() -> list[str]:
    """Full detail for the boot log — every value and where it came from."""
    out = []
    for desk in sorted(_loaded):
        out.append(f"  {desk}: {_source.get(desk, '?')}")
        for k, v in sorted(_loaded[desk].items()):
            out.append(f"      {k} = {v}")
        if not _loaded[desk]:
            out.append("      (none — every value is a code default)")
    return out
