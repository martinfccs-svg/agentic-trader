"""Persistent audit trail for agentic-trader.

Why this exists: Railway purges deployment logs on every redeploy (the
2026-07-07 boot/reconcile/EOD-flatten logs were lost this way), and the
container filesystem is ephemeral. This module appends structured events to
a JSONL file on a persistent volume and mirrors the critical ones to
ntfy.sh, so the audit trail survives redeploys and total log loss.

Events: boot, reconcile, halt, fill, close, flatten, scorecard, crash.

Design rules:
  - record() NEVER raises. A broken audit trail must never break trading.
  - ntfy posts run on a daemon thread with a short timeout: never blocks
    the trading loop, failures are swallowed (the JSONL file is the record
    of truth; ntfy is a best-effort mirror).
  - If the volume path is unwritable (no volume mounted), falls back to
    ./audit.jsonl and warns once — degraded, but never silent.

Configuration (Railway variables):
  AUDIT_LOG_PATH   default /data/audit.jsonl  (mount a volume at /data)
  NTFY_TOPIC       your existing ntfy.sh topic; empty disables mirroring
  NTFY_SERVER      default https://ntfy.sh
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.request
from datetime import datetime, timezone

log = logging.getLogger("audit")

AUDIT_LOG_PATH = os.getenv("AUDIT_LOG_PATH", "/data/audit.jsonl")
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()

# Events mirrored to ntfy.sh by default. 'fill' is deliberately included:
# every real order deserves a phone notification.
_NOTIFY_EVENTS = {"boot", "reconcile", "halt", "fill", "close", "flatten",
                  "scorecard", "crash"}
_PRIORITY = {"halt": "urgent", "crash": "urgent", "flatten": "high",
             "close": "high", "fill": "high"}
_TAGS = {"boot": "rocket", "reconcile": "mag", "halt": "octagonal_sign",
         "fill": "arrow_up", "close": "arrow_down", "flatten": "broom",
         "scorecard": "bar_chart", "crash": "boom"}

_lock = threading.Lock()
_effective_path: str | None = None


def _resolve_path() -> str:
    """Pick the audit file path once; fall back if the volume is missing."""
    global _effective_path
    if _effective_path:
        return _effective_path
    path = AUDIT_LOG_PATH
    try:
        d = os.path.dirname(path) or "."
        os.makedirs(d, exist_ok=True)
        with open(path, "a", encoding="utf-8"):
            pass
        _effective_path = path
    except OSError as e:
        fallback = "./audit.jsonl"
        log.error("audit: %s not writable (%s) — falling back to %s. "
                  "Mount a Railway volume at /data or set AUDIT_LOG_PATH, "
                  "or this trail will NOT survive redeploys.",
                  path, e, fallback)
        _effective_path = fallback
    return _effective_path


def _post_ntfy(title: str, body: str, priority: str, tags: str) -> None:
    try:
        req = urllib.request.Request(
            f"{NTFY_SERVER}/{NTFY_TOPIC}",
            data=body.encode("utf-8"),
            headers={"Title": title, "Priority": priority, "Tags": tags},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:  # noqa: BLE001 — mirror is best-effort
        log.warning("audit: ntfy post failed (%s) — JSONL still written", e)


def _summarize(event: str, fields: dict) -> str:
    """One phone-readable line per event."""
    f = fields
    if event == "boot":
        return (f"mode={f.get('mode')} equity=${f.get('equity', 0):,.2f} "
                f"deploy={f.get('deployment', '?')[:8]}")
    if event == "reconcile":
        return (f"adopted={f.get('adopted') or 'none'} "
                f"orphans={f.get('orphans') or 'none'}")
    if event == "halt":
        return str(f.get("reason", ""))
    if event == "fill":
        return (f"BUY {f.get('ticker')} x{f.get('qty')} @ {f.get('price')} "
                f"stop={f.get('stop')} [{f.get('system')}]")
    if event == "close":
        return (f"CLOSE {f.get('ticker')} x{f.get('qty')} @ {f.get('price')} "
                f"realized=${f.get('realized', 0):+,.2f} [{f.get('system')}]")
    if event == "flatten":
        s = (f"{f.get('reason')}: closed={f.get('closed')}")
        if f.get("failed"):
            s += f" FAILED={f['failed']}"
        return s
    if event == "scorecard":
        return (f"equity=${f.get('equity', 0):,.2f} "
                f"realized_today=${f.get('realized_today', 0):+,.2f}")
    if event == "crash":
        return str(f.get("error", ""))[:180]
    return json.dumps(fields, default=str)[:180]


def record(event: str, notify: bool | None = None, **fields) -> None:
    """Append one event to the audit trail. Never raises."""
    entry = {"ts": datetime.now(timezone.utc).isoformat(),
             "event": event, **fields}
    try:
        line = json.dumps(entry, default=str)
    except Exception as e:  # noqa: BLE001
        line = json.dumps({"ts": entry["ts"], "event": event,
                           "serialization_error": str(e)})
    try:
        path = _resolve_path()
        with _lock, open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    except Exception as e:  # noqa: BLE001 — never break trading
        log.error("audit: write failed: %s | lost entry: %s", e, line[:200])

    if notify is None:
        notify = event in _NOTIFY_EVENTS
    if notify and NTFY_TOPIC:
        threading.Thread(
            target=_post_ntfy,
            args=(f"trader: {event}", _summarize(event, fields),
                  _PRIORITY.get(event, "default"),
                  _TAGS.get(event, "robot")),
            daemon=True,
        ).start()


# ---------------------------------------------------------------- wrappers
def boot(**f):        record("boot", **f)
def reconcile(**f):   record("reconcile", **f)
def halt(**f):        record("halt", **f)
def fill(**f):        record("fill", **f)
def close(**f):       record("close", **f)
def flatten(**f):     record("flatten", **f)
def scorecard(**f):   record("scorecard", **f)
def crash(**f):       record("crash", **f)
