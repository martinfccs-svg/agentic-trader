# agentic-trader v6 — Strategy Reference & Holding Rules
**Purpose:** authoritative reference for what each system does, which positions
are held overnight, and how position ownership is determined. Written
2026-07-10 to resolve a misattribution (see "The META case" below).

---

## The one-line answer

**Three of the four systems hold positions overnight and through weekends by
design.** Only INTRADAY flattens at the close — and INTRADAY has been benched
(disabled) since Jul 8 and cannot open positions at all. **A position that is
open after the market closes is normal and expected** if it belongs to swing,
meanrev, or xsectmom.

---

## The four systems

### 1. SWING (trend breakout) — ACTIVE · holds overnight, multi-day
- **Universe:** all 63 names, daily bars
- **Entry:** close breaks the prior 20-day high, price above the 50-day SMA,
  volume ≥ 1.3× average
- **Exit:** broker-side protective stop (bracket order, lives at Alpaca) and
  the engine's trailing-stop management each cycle
- **Holding period:** days to weeks. **Positions are held overnight and over
  weekends — this is the strategy, not an error.**
- **Max positions:** 4

### 2. MEAN REVERSION — ACTIVE · holds overnight, multi-day
- **Universe:** all 63 names, daily bars
- **Entry:** RSI(14) < 30 while price remains above the 200-day SMA
  (oversold within a long-term uptrend)
- **Exit:** 2.0× ATR broker-side stop; reversion exit per engine
- **Holding period:** days. **Held overnight.**
- **Max positions:** 4 (deliberately sized smaller per Jul 8 strategy decision)

### 3. CROSS-SECTIONAL MOMENTUM (xsectmom) — ACTIVE · holds overnight, days-to-weeks
- **Universe:** all 63 names, ranked by 126-day trailing return (5-day skip)
- **Portfolio:** holds the **top 3** by relative strength
- **Rebalance:** exactly once per trading day at 10:00 ET (wall-clock gate,
  state persisted to /data — a redeploy does NOT re-fire it). A rotation
  requires minimum ranking coverage; on degraded data it skips entirely
  (no exits, no entries) and retries later.
- **Exit:** a name falls out of the top 3 at a rebalance, or its 3.0× ATR
  broker-side stop fills
- **Holding period:** by design, as long as a name stays in the top 3 —
  **held overnight and over weekends.**

### 4. INTRADAY (opening-range momentum) — **BENCHED since Jul 8** · never overnight
- **Universe:** 12-name liquid subset, 1-minute bars
- **Entry:** opening-range breakout above VWAP with 1.3× relative volume
- **Exit:** 1% trailing stop; **hard flatten before every close** — this is
  the ONLY system that never holds overnight
- **Current status:** benched via `ENABLED_SYSTEMS` — the engine is not
  built, its scanner never runs, it **cannot open positions**. Any position
  observed in the account since Jul 8 is, by construction, NOT intraday's.

---

## Weekends & after-hours behavior

**The bot never trades outside regular market hours (9:30 AM–4:00 PM ET,
Mon–Fri, per the market calendar).** The process itself runs 24/7 — cycle
counters keep climbing on weekends — but while the market is closed:

- `market_is_open()` blocks all entries; scanners produce no actionable signals
- the loop drops to a slow after-hours cadence (~60s vs ~5s) to save API budget
- the xsectmom gate, if it opens on a closed day (weekend/holiday), logs once
  and marks the day done — no rotation is attempted
- positions held through the weekend (swing / meanrev / xsectmom) sit at the
  broker with their protective stop orders attached; those stops can only
  execute when the market reopens Monday

So on a weekend the correct observations are: service ONLINE, slow cycles,
zero signals, zero orders, equity static, overnight positions unchanged.
None of that is an anomaly.

---

## How position ownership is determined (do not guess from the ticker)

1. **Persistent registry (primary):** every open/close writes
   `/data/position_state.json` — ticker → {system, entry, stop}. Survives
   redeploys and weekends.
2. **Order-ID prefix (fallback):** every bot entry order carries
   `client_order_id = bot-{system}-{hash}`.
3. **Neither found → ORPHAN → the bot HALTS at boot.** This is deliberate:
   unknown holdings are unknown risk, and the system never auto-liquidates
   what it cannot attribute. A human resolves it. Auto-flattening at boot is
   explicitly rejected as a design (it would trade blind into a possibly
   closed market — the same failure class that caused the Jul 6 and Jul 9
   incidents).

---

## The META case (Jul 10) — resolution

- META was opened by **SWING** on Jul 9 (signal: 20-day-high breakout, >50-SMA,
  volume confirmed; the funnel line and the `bot-swing-…` order ID both attest).
- The first META position stopped out Jul 10 morning (−$300.67 realized);
  swing re-entered the same day.
- META being open over the weekend is **swing behaving exactly as designed**,
  protected by its broker-side stop the entire time.
- It was never an intraday position; intraday has been unable to trade since
  Jul 8. **No action is needed. Do not close it. Do not re-enable intraday
  to "manage" it.**

---

## Quick rules of thumb for monitoring

| Observation | Verdict |
|---|---|
| Position open after 4 PM ET, system ∈ {swing, meanrev, xsectmom} | **Normal.** Designed behavior. |
| Position open after 4 PM ET, system = intraday | Genuine anomaly (impossible while benched) — flag it. |
| Boot halts with ORPHAN | Deliberate safety stop — a human attributes/handles the position, then restarts. |
| xsectmom book empty mid-day | Possible (take-profit legs or degraded-data skip) — check the rebalance log line, which states its own reason. |
| Zero signals on a quiet day | Normal — the funnel lines (`funnel[swing]: …`) show gate-by-gate why. |
