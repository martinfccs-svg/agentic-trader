"""volume_check.py — does /data actually survive a redeploy?

Everything this bot knows across restarts lives on the Railway volume: the
audit trail (the evidence stream for every shadow experiment), the position
registry (system attribution, true entry_time, stops), swing_v2's shadow book,
the rebalance gate, and the loss-cooldown state. If the volume is not
persisting, all of that silently resets on every deploy and several features
become permanently inert rather than visibly broken:

  * meanrev's time stop counts trading days from entry_time -> reset to boot
    time -> never fires
  * the per-ticker loss cooldown -> forgets every loss -> never arms
  * swing_v2's shadow book -> no accumulated evidence, ever
  * the audit trail -> weeks of fills and closes lost, which is why the
    broker showed 63 closed trades while audit.jsonl held ~900 bytes

None of those announce themselves. That is what makes this worth a canary.

On every boot this writes a marker containing first-seen time and a boot
count, then reports what it found. Three readings tell the whole story:

  boot #1, age 0s        -> first boot with this volume (or NOT persisting)
  boot #7, age 4d        -> persisting correctly
  boot #1 every restart  -> NOT PERSISTING. The count can only stay at 1 if
                            the file is gone each time.
"""

from __future__ import annotations

import json
import logging
import os
import time

log = logging.getLogger("volume_check")

DATA_DIR = os.getenv("DATA_DIR", "/data")
CANARY = os.path.join(DATA_DIR, "volume_canary.json")


def check() -> dict:
    """Read/update the canary and log the verdict. Never raises."""
    info = {"ok": None, "boots": None, "age_days": None}
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except Exception as e:  # noqa: BLE001
        log.critical("volume_check: cannot create %s (%s) — the volume is "
                     "NOT WRITABLE. Audit trail, position registry, shadow "
                     "books and loss cooldowns are all disabled in effect.",
                     DATA_DIR, e)
        info["ok"] = False
        return info

    prior = None
    try:
        with open(CANARY, encoding="utf-8") as fh:
            prior = json.load(fh)
    except FileNotFoundError:
        prior = None
    except Exception as e:  # noqa: BLE001
        log.error("volume_check: canary unreadable (%s) — treating as absent", e)
        prior = None

    now = time.time()
    if prior and prior.get("first_seen"):
        boots = int(prior.get("boots", 1)) + 1
        first = float(prior["first_seen"])
        age_d = (now - first) / 86400
        info.update(ok=True, boots=boots, age_days=round(age_d, 2))
        log.warning("volume_check: /data PERSISTING — boot #%d, volume first "
                    "seen %.2f days ago", boots, age_d)
    else:
        boots, first, age_d = 1, now, 0.0
        info.update(ok=None, boots=1, age_days=0.0)
        log.critical("volume_check: NO CANARY FOUND — this is either the very "
                     "first boot with this volume, or /data is NOT "
                     "PERSISTING. If this line appears on every deploy, the "
                     "volume is not surviving: the audit trail, position "
                     "registry (true entry_time, stops), swing_v2 shadow "
                     "book and loss cooldowns are all resetting, and the "
                     "meanrev time stop can never fire.")

    try:
        with open(CANARY + ".tmp", "w", encoding="utf-8") as fh:
            json.dump({"first_seen": first, "boots": boots,
                       "last_boot": now}, fh)
        os.replace(CANARY + ".tmp", CANARY)
    except Exception as e:  # noqa: BLE001
        log.critical("volume_check: cannot WRITE the canary (%s) — /data is "
                     "read-only or unmounted", e)
        info["ok"] = False

    # inventory of what should be accumulating
    for name in ("audit.jsonl", "trades.jsonl", "position_state.json",
                 "swing_v2_state.json", "loss_cooldown.json"):
        p = os.path.join(DATA_DIR, name)
        try:
            size = os.path.getsize(p)
            log.warning("volume_check:   %-22s %9d bytes", name, size)
        except OSError:
            log.warning("volume_check:   %-22s ABSENT", name)
    return info
