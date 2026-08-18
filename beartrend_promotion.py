"""beartrend_promotion.py — turn research evidence into a durable decision.

    beartrend_scoring.py    detect, score, log observations
    beartrend_review.py     analyse them
    beartrend_promotion.py  convert the analysis into an approval artifact  <-

The production system should not re-run months of statistics every morning.
It should read one small file that says whether the evidence supports going
further, and be able to tell whether that file is still trustworthy.

WHY THIS FILE IS MORE DANGEROUS THAN IT LOOKS

An approval artifact is a permission slip. A bare {"approved": true} can be
written by hand, by a script, or by an assistant trying to be helpful — and
nothing downstream would know. This project has already deleted one small
file with outsized authority (startup_flatten.py) for exactly that reason.
So the artifact carries four properties a plain flag does not:

  1. IT IS DERIVED, NEVER ASSERTED. approved=true is only ever written by
     this module after all conditions pass on real data. There is no flag,
     env var or argument that can force it.
  2. IT CARRIES ITS EVIDENCE. Every condition is recorded with the value
     that satisfied it, so a reader can see WHY rather than trusting a bool.
  3. IT IS FINGERPRINTED. The observations file is hashed and counted. An
     artifact that does not match the data it claims to summarise is
     detectable — whether it went stale or was fabricated.
  4. IT EXPIRES. Evidence gathered in one bear market does not authorise
     trading in a different one two years later.

WHAT APPROVAL ACTUALLY AUTHORISES — and this matters:

    approved=true means "the research supports BUILDING the short-side
    execution stack". It does NOT mean "start shorting". brokers.py cannot
    short; there is no execution path; and when one exists it should require
    its own explicit enable, not merely the presence of this file.

    approved=false is not a verdict on the strategy. It says the evidence
    does not yet justify a month of engineering.

Usage:
    python beartrend_promotion.py                     # evaluate, print, write
    python beartrend_promotion.py --verify            # check an existing one
    python beartrend_promotion.py --out /data/beartrend_promotion.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import sys
import tool_guard
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from beartrend_review import (_bootstrap_ci, _spearman, fetch_bars)
except ImportError as e:  # noqa: BLE001
    sys.exit(f"beartrend_promotion needs beartrend_review alongside it ({e})")

# The bar. Deliberately duplicated here rather than imported, so a change to
# the review tool cannot silently move the promotion threshold.
MIN_OBSERVATIONS = 50
MIN_EPISODES = 2
MIN_EDGE_OVER_BASELINE = 0.05
VALID_DAYS = int(os.getenv("BEARTREND_APPROVAL_VALID_DAYS", "180"))
ARTIFACT_VERSION = 1

# Namespaced under /data/research/beartrend/ so research artifacts do not sit
# beside live trading state. Env overrides keep it portable for local runs.
RESEARCH_DIR = os.getenv("BEARTREND_RESEARCH_DIR", "/data/research/beartrend")
DEFAULT_ARTIFACT = os.path.join(RESEARCH_DIR, "beartrend_promotion.json")
HISTORY_PATH = os.path.join(RESEARCH_DIR, "promotion_history.jsonl")


def append_history(body: dict, path: str = None) -> None:
    """Append-only ledger of every promotion run, approved or not.

    Answers a question the current artifact cannot: "which research authorised
    this?" — and, just as importantly, "what did we decide LAST time, and did
    the answer change?" A single overwritten file loses the sequence, and the
    sequence is where you would see an edge appearing and then evaporating.

    Rejections are recorded too. A history containing only approvals is a
    history that hides how many times the bar was missed.
    """
    path = path or HISTORY_PATH
    row = {
        "approval_id": body.get("approval_id"),
        "approved": body.get("approved"),
        "evaluated_on": body.get("evaluated_on"),
        "valid_until": body.get("valid_until"),
        "reason": body.get("reason"),
        "metrics": body.get("metrics"),
        "config_hash": (body.get("evaluation_config") or {}).get("hash"),
        "source_fingerprint": body.get("source_fingerprint"),
        "failed_conditions": [c["name"] for c in body.get("conditions", [])
                              if not c.get("pass")],
    }
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except Exception as e:  # noqa: BLE001 — the ledger must never block a run
        print(f"  WARNING: could not append history ({e})")


def load_approval(path: str = None, obs_file: str = None,
                  cost_bps: float = None, borrow_apr: float = None) -> dict:
    """THE ONLY SUPPORTED WAY for a consumer to read an approval.

    The obvious consumer code is:

        promotion = json.load(open(path))
        if promotion["approved"]: ...

    and it is wrong. That reads the flag while skipping every safeguard: the
    artifact could be expired, describe data that has since changed, have been
    written under thresholds that have since moved, or have been edited by
    hand. This helper verifies FIRST and returns approved=False with a reason
    whenever verification fails, so a consumer cannot accidentally trust a
    bad artifact by taking the simplest path.

    Returns {"approved": bool, "approval_id": str|None, "reason": str}.
    """
    path = path or DEFAULT_ARTIFACT
    if not os.path.exists(path):
        return {"approved": False, "approval_id": None,
                "reason": f"no approval artifact at {path}"}
    ok, why = verify(path, obs_file or "", cost_bps=cost_bps,
                     borrow_apr=borrow_apr)
    if not ok:
        return {"approved": False, "approval_id": None,
                "reason": f"artifact REJECTED by verification: {why}"}
    try:
        art = json.load(open(path, encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"approved": False, "approval_id": None,
                "reason": f"unreadable: {e}"}
    return {"approved": bool(art.get("approved")),
            "approval_id": art.get("approval_id"),
            "reason": art.get("reason", "")}


# Bootstrap seeds are FIXED, so a promotion decision is reproducible. They
# are recorded in the artifact rather than merely set, because "we used a
# seed" is only checkable if the seed is written down.
BOOTSTRAP_SEED = 7


def _source_fingerprint() -> dict:
    """Hash the modules that produced this decision.

    An artifact says what the evidence was; it should also say what CODE read
    that evidence. beartrend_review.py evolving is not a bug, but an approval
    generated by one version and interpreted under another is unreproducible,
    and nobody would know.
    """
    out = {}
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("beartrend_promotion.py", "beartrend_review.py",
                 "beartrend_scoring.py"):
        p = os.path.join(here, name)
        try:
            with open(p, "rb") as fh:
                out[name] = hashlib.sha256(fh.read()).hexdigest()[:16]
        except OSError:
            out[name] = None
    return out


def _config_fingerprint(horizons, cost_bps, borrow_apr, iters) -> dict:
    """Fingerprint the EVALUATION SETTINGS, not just the data.

    The hole this closes: change borrow_apr from 3% to 0% and the
    observations hash is byte-identical, so a stale artifact would still
    verify while describing a materially different calculation. Costs are
    part of the evidence, not a display preference.
    """
    cfg = {
        "horizons": list(horizons), "cost_bps": cost_bps,
        "borrow_apr": borrow_apr, "bootstrap_iters": iters,
        "bootstrap_seed": BOOTSTRAP_SEED,
        "thresholds": {
            "min_observations": MIN_OBSERVATIONS,
            "min_episodes": MIN_EPISODES,
            "min_edge_over_baseline": MIN_EDGE_OVER_BASELINE,
            "ci_must_exclude_zero": True,
            "score_corr_must_be_positive": True,
            "valid_days": VALID_DAYS,
        },
    }
    blob = json.dumps(cfg, sort_keys=True).encode()
    cfg["hash"] = hashlib.sha256(blob).hexdigest()[:16]
    return cfg


def _approval_id(obs_fp: dict, cfg_fp: dict, src_fp: dict,
                 metrics: dict, day: str) -> str:
    """Immutable, traceable identifier: BT-YYYY-MM-DD-XXXXXX.

    Derived from the data, the settings, the source and the results — so two
    approvals can never share an id unless every one of those is identical.
    Downstream execution and audit logs can then record WHICH research
    approval a trade was taken under, which is the norm in regulated systems
    and cheap to add now rather than retrofit later.
    """
    blob = json.dumps({"obs": obs_fp.get("sha256"), "cfg": cfg_fp.get("hash"),
                       "src": src_fp, "m": metrics, "day": day},
                      sort_keys=True).encode()
    return f"BT-{day}-{hashlib.sha256(blob).hexdigest()[:6].upper()}"


def _fingerprint(path: str) -> dict:
    h = hashlib.sha256()
    n = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    with open(path, encoding="utf-8") as fh:
        n = sum(1 for line in fh if line.strip())
    return {"sha256": h.hexdigest(), "lines": n,
            "bytes": os.path.getsize(path)}


def evaluate(obs_file: str, horizons, cost_bps: float, borrow_apr: float,
             iters: int) -> dict:
    """Compute the promotion conditions from real data. Returns the artifact
    body. Never writes anything; never invents a positive."""
    # Refuse to decide on data that cannot be fully read. The review tool
    # skips malformed lines because a report on 95% of the data is still
    # useful; an APPROVAL on 95% of the data is not — the missing 5% could
    # be the losses. Fail loudly with the line number instead.
    obs = []
    try:
        with open(obs_file, encoding="utf-8") as fh:
            for i, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    obs.append(json.loads(line))
                except json.JSONDecodeError as e:
                    sys.exit(f"\n{obs_file}:{i} is malformed ({e}).\n"
                             f"A promotion decision must read EVERY "
                             f"observation — a partial read could omit "
                             f"exactly the losses that matter. Fix or "
                             f"re-pull the file:\n"
                             f"    railway run cat "
                             f"/data/beartrend_observations.jsonl > "
                             f"{obs_file}\n")
    except FileNotFoundError:
        sys.exit(f"{obs_file} not found:\n    railway run cat "
                 f"/data/beartrend_observations.jsonl > {obs_file}")

    now = datetime.now(timezone.utc)
    cfg_fp = _config_fingerprint(horizons, cost_bps, borrow_apr, iters)
    src_fp = _source_fingerprint()
    body = {
        "artifact_version": ARTIFACT_VERSION,
        "approval_id": None,          # set once metrics exist
        "approved": False,
        "evaluation_config": cfg_fp,
        "source_fingerprint": src_fp,
        "evaluated_on": now.strftime("%Y-%m-%d"),
        "valid_until": (now + timedelta(days=VALID_DAYS)).strftime("%Y-%m-%d"),
        "source_file": os.path.basename(obs_file),
        "fingerprint": _fingerprint(obs_file),
        "authorises": ("BUILDING the short-side execution stack — NOT live "
                       "shorting. Execution requires its own explicit enable."),
        "conditions": [],
        "metrics": {},
    }
    if not obs:
        body["conditions"] = [{"name": "observations exist", "pass": False,
                               "value": 0}]
        body["reason"] = ("no observations recorded — beartrend only produces "
                          "them in a confirmed SPY downtrend")
        return body

    days = sorted({o["scan_date"] for o in obs})
    syms = sorted({o["ticker"] for o in obs})
    lo = days[0]
    hi = (datetime.strptime(days[-1], "%Y-%m-%d")
          + timedelta(days=max(horizons) * 2 + 15)).strftime("%Y-%m-%d")
    bars = fetch_bars(syms + ["SPY"], lo, hi)

    net, scores = [], []
    for o in obs:
        b = [x for x in bars.get(o["ticker"], [])
             if x["t"][:10] > o["scan_date"]]
        entry, risk, stop = o["price"], o["risk_per_share"], o["stop"]
        if risk <= 0 or not b:
            continue
        full = b[:max(horizons)]
        stop_day = next((i for i, x in enumerate(full, 1)
                         if x["h"] >= stop), None)
        if stop_day is not None:
            r = -1.0
        elif len(full) >= max(horizons):
            r = (entry - full[-1]["c"]) / risk
        else:
            continue                       # still maturing — excluded
        held = stop_day or max(horizons)
        cost = ((entry * (cost_bps / 10000.0) * 2) / risk
                + (entry * (borrow_apr / 100.0) * (held / 252.0)) / risk)
        net.append(r - cost)
        scores.append(o.get("score", 0.0))

    n = len(net)
    episodes = len({d[:7] for d in days})
    lo_ci, hi_ci = _bootstrap_ci(net, iters=iters,
                                 seed=BOOTSTRAP_SEED) if n >= 3 else (
        float("nan"), float("nan"))
    exp = statistics.mean(net) if net else 0.0
    rho = _spearman(scores, net) if n >= 4 else None

    body["metrics"] = {
        "observations_resolved": n,
        "observations_total": len(obs),
        "scan_days": len(days),
        "episodes": episodes,
        "date_range": [days[0], days[-1]],
        "expectancy_net_R": round(exp, 4),
        "expectancy_ci": [round(lo_ci, 4), round(hi_ci, 4)],
        "score_rank_correlation": round(rho, 4) if rho is not None else None,
        "cost_bps": cost_bps, "borrow_apr": borrow_apr,
    }
    # Each condition records its MARGIN — how far past the bar it cleared.
    # A dashboard number without weights: "187 against a minimum of 50" is
    # more informative than "+15 points", and inventing a weighted composite
    # would be inventing the answer, the same objection that ruled out the
    # composite momentum score and the weighted portfolio risk score.
    body["conditions"] = [
        {"name": f"resolved observations >= {MIN_OBSERVATIONS}",
         "pass": n >= MIN_OBSERVATIONS, "value": n,
         "threshold": MIN_OBSERVATIONS,
         "margin": f"{n / MIN_OBSERVATIONS:.1f}x minimum"},
        {"name": f"distinct bearish episodes >= {MIN_EPISODES}",
         "pass": episodes >= MIN_EPISODES, "value": episodes,
         "threshold": MIN_EPISODES,
         "margin": f"{episodes / MIN_EPISODES:.1f}x minimum"},
        {"name": "net expectancy CI excludes zero",
         "pass": bool(lo_ci > 0), "value": [round(lo_ci, 3), round(hi_ci, 3)],
         "threshold": 0.0,
         "margin": f"lower bound {lo_ci:+.3f}"},
        {"name": "score rank correlation > 0",
         "pass": bool(rho is not None and rho > 0),
         "value": round(rho, 3) if rho is not None else None,
         "threshold": 0.0,
         "margin": (f"rho {rho:+.3f}" if rho is not None else "not computable")},
    ]
    # DERIVED, never asserted. There is no path to True except this line.
    body["approved"] = all(c["pass"] for c in body["conditions"])
    body["approval_id"] = _approval_id(body["fingerprint"], cfg_fp, src_fp,
                                       body["metrics"], body["evaluated_on"])
    if not body["approved"]:
        failed = [c["name"] for c in body["conditions"] if not c["pass"]]
        body["reason"] = "conditions not met: " + "; ".join(failed)
    else:
        body["reason"] = ("all conditions met on real data — the short-side "
                          "build has an evidence case")
    return body


def verify(path: str, obs_file: str, horizons=None, cost_bps=None,
           borrow_apr=None, iters=None) -> tuple[bool, str]:
    """Is this artifact still trustworthy?

    Four independent ways an approval can silently stop meaning what it says:
    it expires, the DATA changes, the THRESHOLDS change, or the CODE changes.
    Checking only the first two would let a bar raised from 50 to 100
    observations leave old approvals standing.
    """
    try:
        art = json.load(open(path, encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return False, f"unreadable: {e}"
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if art.get("valid_until", "") < today:
        return False, (f"EXPIRED on {art.get('valid_until')} — evidence from "
                       f"one bear market does not authorise the next")
    if os.path.exists(obs_file):
        fp = _fingerprint(obs_file)
        old = art.get("fingerprint", {})
        if fp["sha256"] != old.get("sha256"):
            return False, (f"STALE: the observations file has changed "
                           f"({old.get('lines')} -> {fp['lines']} rows). "
                           f"Re-evaluate before relying on this.")
    if not art.get("conditions"):
        return False, "no conditions recorded — this artifact was not derived"
    if art.get("approved") and not all(c.get("pass")
                                       for c in art["conditions"]):
        return False, ("INCONSISTENT: approved=true but a condition failed. "
                       "This file was edited by hand.")

    # THRESHOLD DRIFT: the bar moved after the artifact was written.
    stored = (art.get("evaluation_config") or {}).get("thresholds") or {}
    current = _config_fingerprint([1], 0, 0, 1)["thresholds"]
    drift = {k: (stored.get(k), current[k]) for k in current
             if k in stored and stored[k] != current[k]}
    if drift:
        return False, ("THRESHOLDS CHANGED since this was written: "
                       + "; ".join(f"{k} {o} -> {n}"
                                   for k, (o, n) in drift.items())
                       + ". Re-evaluate — an old approval cannot clear a "
                         "new bar.")

    # CONFIG MISMATCH: verifying under different costs than were evaluated.
    if cost_bps is not None or borrow_apr is not None:
        cfg = art.get("evaluation_config") or {}
        for key, val in (("cost_bps", cost_bps), ("borrow_apr", borrow_apr)):
            if val is not None and cfg.get(key) != val:
                return False, (f"CONFIG MISMATCH: artifact used {key}="
                               f"{cfg.get(key)}, verifying with {val}. Costs "
                               f"are part of the evidence, not a display "
                               f"setting.")

    # SOURCE DRIFT: warn, do not reject — code may change harmlessly.
    src_now = _source_fingerprint()
    src_then = art.get("source_fingerprint") or {}
    changed = [k for k in src_now
               if k in src_then and src_now[k] != src_then[k]]
    suffix = (f"  (NOTE: {', '.join(changed)} changed since evaluation — "
              f"results may not reproduce exactly)" if changed else "")

    return True, ("valid" + (" and APPROVED" if art.get("approved")
                             else " — not approved")
                  + f" [{art.get('approval_id')}]" + suffix)


def main():
    # A research tool must never be a container entrypoint: it exits,
    # and Railway restarts anything that exits. See tool_guard.
    tool_guard.guard_entrypoint("beartrend_promotion.py")
    ap = argparse.ArgumentParser()
    ap.add_argument("obs_file", nargs="?", default="beartrend_obs.jsonl")
    ap.add_argument("--out", default=None,
                    help=f"default: {DEFAULT_ARTIFACT}")
    ap.add_argument("--horizons", default="1,5,10,20")
    ap.add_argument("--cost-bps", type=float, default=5.0)
    ap.add_argument("--borrow-apr", type=float, default=3.0)
    ap.add_argument("--iters", type=int, default=10000)
    ap.add_argument("--verify", action="store_true")
    a = ap.parse_args()
    if a.out is None:
        a.out = (DEFAULT_ARTIFACT if os.path.isdir(RESEARCH_DIR)
                 else "beartrend_promotion.json")

    if a.verify:
        ok, why = verify(a.out, a.obs_file,
                         cost_bps=a.cost_bps, borrow_apr=a.borrow_apr)
        print(f"{a.out}: {'OK' if ok else 'REJECTED'} — {why}")
        sys.exit(0 if ok else 1)

    body = evaluate(a.obs_file, [int(x) for x in a.horizons.split(",")],
                    a.cost_bps, a.borrow_apr, a.iters)

    print("=" * 74)
    print("BEARTREND PROMOTION DECISION")
    print("=" * 74)
    for c in body["conditions"]:
        print(f"  [{'PASS' if c['pass'] else 'FAIL'}]  {c['name']:<42} "
              f"{str(c['value']):<18} {c.get('margin','')}")
    print(f"\n  APPROVAL ID: {body['approval_id']}")
    print(f"  APPROVED: {body['approved']}")
    print(f"  {body['reason']}")
    print(f"\n  This authorises: {body['authorises']}")
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(body, fh, indent=2)
    hist = (HISTORY_PATH if os.path.isdir(os.path.dirname(a.out) or ".")
            and a.out == DEFAULT_ARTIFACT else "promotion_history.jsonl")
    append_history(body, hist)
    print(f"\n  written -> {a.out}  (valid until {body['valid_until']})")
    print(f"  ledger  -> {hist}  (append-only; rejections recorded too)")


if __name__ == "__main__":
    main()
