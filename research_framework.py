"""research_framework.py — the research machinery both harnesses share.

LOCAL TOOL. Never deployed.

backtest_swing_v2 grew Monte Carlo, bootstrap intervals, promotion rules and
structured experiment records. backtest_xsect has none of them. The obvious
fix — copy them across — is the mistake this codebase has spent a week
undoing: seven EMA implementations, four ATR trails, two exit ladders. Copies
start identical and do not stay that way.

So the machinery lives here and the harnesses import it. What stays local to
each harness is the part that genuinely differs: WHAT question it asks and
WHAT thresholds answer it.

    swing  : "should this exit be promoted?"   -> Sharpe gain, drawdown, trades
    xsect  : "does the sector cap help?"       -> monoculture %, drawdown, Sharpe cost

Those are different questions and deserve different rules. They are not
different bootstrap implementations.

ONE THING THAT IS NOT SHARED, deliberately: Monte Carlo comes in two forms.

    monte_carlo_trades()   discrete trades, resampled independently
    monte_carlo_returns()  a daily return SERIES, resampled in BLOCKS

Swing produces discrete trades; xsect produces a continuous equity curve.
Resampling daily returns independently destroys autocorrelation — momentum
strategies trend and mean-revert in runs — and a bootstrap that ignores that
reports drawdowns far shallower than anything the strategy can actually
experience. Block resampling preserves the runs. Using the wrong one would
produce a confident number that is wrong in the dangerous direction.
"""

from __future__ import annotations

import json
import os
import random
import subprocess
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# UNCERTAINTY
# ---------------------------------------------------------------------------

def _mean(v):
    return sum(v) / len(v) if v else 0.0


def bootstrap_ci(values, iters=5000, seed=7, pct=0.95, stat=None):
    """(lo, hi) around any STATISTIC of the resample, mean by default.

    `stat` takes the resampled list and returns a number, so the same
    machinery answers "is the mean different from zero?" and "how uncertain
    is the Sharpe?" — different questions that were previously served by one
    hardcoded answer. A median CI is not a mean CI, and a Sharpe CI is
    neither.

        bootstrap_ci(rs)                       mean
        bootstrap_ci(rs, stat=statistics.median)
        bootstrap_ci(rs, stat=lambda v: _mean(v) / (pstdev(v) or 1e-12))
    """
    if len(values) < 3:
        return (float("nan"), float("nan"))
    f = stat or _mean
    rng = random.Random(seed)
    n = len(values)
    out = []
    for _ in range(iters):
        sample = [rng.choice(values) for _ in range(n)]
        try:
            out.append(f(sample))
        except Exception:  # noqa: BLE001 — a bad stat must not kill the run
            continue
    if not out:
        return (float("nan"), float("nan"))
    out.sort()
    lo_i = int((1 - pct) / 2 * len(out))
    hi_i = int((1 - (1 - pct) / 2) * len(out))
    return out[lo_i], out[min(hi_i, len(out) - 1)]


def monte_carlo_trades(trades, iters=2000, slip_bps=3.0, miss_rate=0.05,
                       gap_rate=0.03, gap_extra=0.5, seed=17):
    """Resample DISCRETE trades. For harnesses that produce a trade list.

    Perturbs ordering, slippage, missed fills and gap losses — four ways
    reality differs from replay. The 5th percentile is the number to survive.
    """
    pnls = [t["pnl"] if isinstance(t, dict) else float(t) for t in trades]
    if len(pnls) < 10:
        return None
    rng = random.Random(seed)
    n = len(pnls)
    finals, dds = [], []
    for _ in range(iters):
        seq = []
        for _ in range(n):
            v = rng.choice(pnls)
            if rng.random() < miss_rate:
                continue
            v -= abs(v) * (slip_bps / 10000.0) * rng.uniform(0.5, 1.5)
            if v < 0 and rng.random() < gap_rate:
                v *= (1.0 + gap_extra)
            seq.append(v)
        if len(seq) < 5:
            continue
        finals.append(sum(seq))
        eq = peak = mdd = 0.0
        for v in seq:
            eq += v
            peak = max(peak, eq)
            mdd = min(mdd, eq - peak)
        dds.append(mdd)
    return _summarise(finals, dds)


def monte_carlo_returns(daily_returns, iters=2000, block=10, seed=17,
                        slip_bps_per_day=0.0):
    """Resample a daily RETURN SERIES in BLOCKS. For curve-based harnesses.

    `block` is the whole point. Momentum strategies trend and reverse in
    runs; resampling days independently breaks those runs and reports
    drawdowns shallower than the strategy can actually produce — a confident
    number, wrong in the dangerous direction. Ten trading days is roughly
    two weeks, long enough to keep a rotation intact.
    """
    r = [x for x in daily_returns if x is not None]
    if len(r) < block * 5:
        return None
    rng = random.Random(seed)
    n = len(r)
    finals, dds = [], []
    for _ in range(iters):
        seq = []
        while len(seq) < n:
            start = rng.randrange(0, max(1, n - block))
            seq.extend(r[start:start + block])
        seq = seq[:n]
        eq, peak, mdd = 1.0, 1.0, 0.0
        for x in seq:
            eq *= (1.0 + x - slip_bps_per_day / 10000.0)
            peak = max(peak, eq)
            mdd = min(mdd, eq / peak - 1.0)
        finals.append(eq - 1.0)
        dds.append(mdd)
    return _summarise(finals, dds)


def _summarise(finals, dds):
    if not finals:
        return None
    finals.sort(); dds.sort()

    def pct(v, p):
        return v[int(p * (len(v) - 1))]
    return {"n": len(finals),
            "p05": pct(finals, 0.05), "p50": pct(finals, 0.50),
            "p95": pct(finals, 0.95),
            "dd_p05": pct(dds, 0.05), "dd_p50": pct(dds, 0.50),
            "prob_loss": sum(1 for f in finals if f < 0) / len(finals)}


# ---------------------------------------------------------------------------
# MULTIPLE HYPOTHESES
# ---------------------------------------------------------------------------

def reality_check(baseline, candidates, iters=5000, seed=23, block=1):
    """White's Reality Check: is the BEST of N variants better than luck?

    THE PROBLEM THIS SOLVES. The exit sweep now tests eight variants against
    a baseline. Judging each in isolation at 5% gives a 34% chance that at
    least one clears the bar on noise alone — so "the ATR trail passed
    promotion" is close to a coin flip away from meaning nothing, and the
    current rules cannot tell the difference because they never look at how
    many things were tried.

    THE TEST. Take each variant's per-period outperformance over baseline.
    Under the null that NO variant is genuinely better, those series have
    mean zero — so recentre them, resample, and record the MAXIMUM mean
    across variants each time. That builds the distribution of "best of
    eight, by chance". The p-value is how often chance beat what was
    actually observed.

    A variant clearing its own promotion rules AND a Reality Check p-value
    below 0.05 has survived the fact that it was cherry-picked. One clearing
    only the former has not.

        baseline   : [per-period return, ...]
        candidates : {name: [per-period return, ...], ...}

    `block` > 1 resamples in blocks, for autocorrelated series.
    """
    if not candidates or len(baseline) < 20:
        return None
    names = [n for n, v in candidates.items() if len(v) == len(baseline)]
    if not names:
        return None
    diffs = {n: [candidates[n][i] - baseline[i] for i in range(len(baseline))]
             for n in names}
    observed = {n: _mean(d) for n, d in diffs.items()}
    best_name = max(observed, key=observed.get)
    best_obs = observed[best_name]

    # Recentre to mean zero: this IS the null hypothesis, made concrete.
    centred = {n: [x - observed[n] for x in d] for n, d in diffs.items()}

    rng = random.Random(seed)
    m = len(baseline)
    worse = 0
    for _ in range(iters):
        if block > 1:
            idx = []
            while len(idx) < m:
                st = rng.randrange(0, max(1, m - block))
                idx.extend(range(st, min(st + block, m)))
            idx = idx[:m]
        else:
            idx = [rng.randrange(m) for _ in range(m)]
        best_boot = max(_mean([centred[n][i] for i in idx]) for n in names)
        if best_boot >= best_obs:
            worse += 1
    p = worse / iters
    return {"best": best_name, "best_mean_edge": best_obs, "p_value": p,
            "n_variants": len(names), "iters": iters,
            "verdict": ("survives being cherry-picked" if p < 0.05
                        else "indistinguishable from the best of "
                             f"{len(names)} random draws")}


# ---------------------------------------------------------------------------
# PROMOTION
# ---------------------------------------------------------------------------

def check_promotion(conditions):
    """conditions: [(name, passed, value, threshold), ...] -> (ok, lines).

    The RULES stay in the caller — swing's question ("promote this exit?")
    and xsect's ("does the cap help?") are not the same question and should
    not share thresholds. What is shared is the discipline: every condition
    named, every value shown, ALL required.
    """
    lines, ok = [], True
    for name, passed, value, threshold in conditions:
        ok &= bool(passed)
        lines.append(f"    [{'PASS' if passed else 'FAIL'}]  {name:<44} "
                     f"{value}  (need {threshold})")
    return ok, lines


# ---------------------------------------------------------------------------
# REPRODUCIBILITY
# ---------------------------------------------------------------------------

def git_state():
    """(short hash, dirty). None if this is not a git checkout."""
    try:
        h = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True,
                           timeout=1).stdout.strip() or None
        d = bool(subprocess.run(["git", "status", "--porcelain"],
                                capture_output=True, text=True,
                                timeout=1).stdout.strip())
        return h, d
    except Exception:  # noqa: BLE001
        return None, None


def environment():
    """Interpreter and platform, recorded with every experiment.

    Rarely matters and occasionally matters enormously: floating-point and
    sort-stability differences across versions have moved backtest results
    before. A result that cannot be reproduced is an anecdote, and knowing
    WHICH interpreter produced it is part of reproducing it.
    """
    import platform
    import sys as _sys
    env = {"python": _sys.version.split()[0],
           "platform": platform.platform(),
           "machine": platform.machine()}
    for mod in ("numpy", "pandas", "scipy"):
        try:
            env[mod] = __import__(mod).__version__
        except Exception:  # noqa: BLE001
            env[mod] = None
    return env


def write_experiment(name: str, payload: dict, biases: list,
                     seeds: dict = None, out_dir: str = ".") -> str | None:
    """Structured record beside the prose report.

    The markdown is for reading; this is for COMPARING. After twenty runs the
    question stops being "what did this one say?" and becomes "has the answer
    moved, and what changed when it did?" — which prose cannot answer.

    `biases` is required, not optional. A result whose limitations are only
    in someone's head is a result that will eventually be quoted without
    them.
    """
    h, dirty = git_state()
    now = datetime.now(timezone.utc)
    # SECONDS, not just the date. Two runs on one afternoon — which is the
    # normal case while tuning — used to overwrite each other, so the record
    # of what you tried first simply vanished. An experiment archive that
    # loses experiments is not an archive.
    stamp = now.strftime("%Y%m%dT%H%M%S")
    body = {
        "experiment": name,
        "run_at": now.isoformat(timespec="seconds"),
        "git_hash": h, "git_dirty": dirty,
        "environment": environment(),
        "seeds": seeds or {},
        "known_biases": biases,
        **payload,
    }
    path = os.path.join(out_dir, f"BACKTEST_EXPERIMENT_{name}_{stamp}.json")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(body, fh, indent=2, default=str)
        return path
    except Exception:  # noqa: BLE001
        return None


# Biases that apply to EVERY replay in this project. Stated once, imported
# everywhere, so no harness can quietly omit them.
UNIVERSE_BIASES = [
    "universe is TODAY'S 68 names — a list assembled in 2026 cannot contain "
    "a company that failed in 2024. Selection bias, bounded by window length "
    "but not zero.",
    "no delisted names: real universes contain survivors AND failures; this "
    "one contains only survivors.",
    "strategy prices are SPLIT-ADJUSTED to match live signal generation; "
    "dividends earned while holding are excluded (~0.3%/yr), a residual that "
    "runs AGAINST the strategy.",
]
