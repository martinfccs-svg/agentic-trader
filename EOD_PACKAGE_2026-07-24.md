# EOD PACKAGE — 2026-07-24 (regime allocation ship)
# Theme: the CIO layer goes live. One commit, then one variable.

## COMMIT SET — 6 code files (ALL IN ONE COMMIT) + 1 doc
  NEW     repo/regime_allocation.py   regime classifier + allocator:
                                      - trend: EMA50/EMA200 >=1% separation,
                                        price vs EMA200, tolerant slope, ADX>=20
                                        (gates strength BOTH directions)
                                      - volatility: SPY ATR% vs 100d median,
                                        must PERSIST (1-day spike = SPIKE, not
                                        a regime)
                                      - breadth: % >EMA50, % >EMA200, and share
                                        of SECTORS leading (one pass, cached
                                        bars, zero extra API calls)
                                      - confidence 0-100 -> multipliers blended
                                        toward 1.00 when evidence is weak
                                      - PERSISTENCE: a label needs 3 consecutive
                                        sessions before it takes effect
                                        (stateless: prior sessions re-classified
                                        from the same bars, redeploy-proof)
                                      - apply_to_shares(): re-clamps to the
                                        10%-of-equity notional cap so leaning IN
                                        can never breach a hard risk limit
  EDIT    repo/main.py                classifies regime once per cycle;
                                      REGIME_ALLOC added to the GATES banner
  EDIT    repo/swing_engine.py        sizing via apply_to_shares (clamped)
  EDIT    repo/meanrev_engine.py      sizing via apply_to_shares (clamped)
  EDIT    repo/intraday_engine.py     sizing via apply_to_shares (clamped)
  EDIT    repo/xsection.py            rotation sizing via apply_to_shares
  DOC     docs/system_blueprint.html  whole-machine architecture, six layers

  WHY ONE COMMIT: the five EDIT files import regime_allocation. A partial push
  fails at import and the bot will not boot.

## VERIFY THESE ARE ALREADY IN THE REPO (from earlier today)
  portfolio_risk.py    NEW  — heat measurement (main.py imports it)
  meanrev_scoring.py   EDIT — volatility exit, risk_multiplier, and the single
                              ADX implementation regime_allocation reuses
  scanner.py           EDIT — scorecard attached to meanrev signals
  If any are missing, add them to the same commit.

## RAILWAY — ONE VARIABLE CHANGE
  REGIME_ALLOC=live          (default is 'shadow' = classify + log only)

## DO NOT SET (all correct by default)
  REGIME_PERSIST_DAYS      3      sessions needed to confirm a regime change
  REGIME_PERSIST_LOOKBACK  12     how far back to search for a confirmed run
  REGIME_ADX_MIN           20     trend-strength floor
  REGIME_ALLOC_FLOOR       0.25   a regime may shrink a desk, never halt one
  REGIME_ALLOC_CONF_BLEND  on     lean scales with evidence strength
  REGIME_ALLOC_TTL_SECS    3600   classification cache
  PORTFOLIO_HEAT_MAX       0      heat = measure-only until you set a ceiling
  MEANREV_SCORING          shadow
  Existing seven (SWING_*, INTRADAY_*, XSECT_SECTOR_CAP, ENABLED_SYSTEMS) unchanged.

## ALLOCATION TABLE (multipliers on sizing the engine already computed)
  regime        swing  pullback  meanrev  intraday  xsectmom
  STRONG_BULL    1.25      1.35     0.50      0.75      1.40
  WEAK_BULL      1.00      1.00     1.00      1.00      1.00   <- baseline
  SIDEWAYS       0.50      0.50     1.50      1.25      0.75
  BEAR           0.25      0.25     0.75      0.75      0.50
  HIGH_VOL       0.25      0.25     0.50      0.50      0.50
  WEAK_BULL is 1.00 everywhere, so going live in an ordinary market is a
  near no-op. Current read is likely WEAK_BULL (uptrend, ~50% breadth).

## POST-PUSH CHECKS
  1. GATES line ends with: REGIME_ALLOC=live
  2. New line within a cycle or two:
     REGIME <LABEL> [LIVE] conf=NN% trend=... vol=... breadth=(x%>EMA50,
       y%>EMA200, z% sectors leading) | ... | multipliers ...
  3. New line: portfolio heat N.NN% of equity ($N at risk across M positions)
  4. If a lean applies: "<engine> regime sizing TICKER: x1.35 -> shares=N"
  5. If a lean would breach the cap: "would breach the notional cap ... clamped"
  6. Watch label STABILITY across days. Flip-flopping = tell me, we slow it.

## ROLLBACK
  REGIME_ALLOC=shadow      (classify + log, apply nothing) — next boot.

## THE LEDGER — unchanged, and now the only thing left
  1. autopsy:  cat /data/audit.jsonl > audit.jsonl
     python autopsy.py audit.jsonl --system swing --exclude-dates 2026-07-16
  2. python backtest_swing_v2.py --symbols-file universe.txt --days 730
     python backtest_swing_v2.py --symbols-file universe.txt --days 365
     ^ THIS is the trend-pullback verdict. swing_v2 IS the pullback engine.
  3. python backtest_xsect.py --symbols-file universe.txt --days 730 --regime
  4. The 4 legacy swing positions: close vs retrofit.
  5. Deferred infrastructure: partial-sell support in brokers.py (unblocks
     layered exits for meanrev + intraday; prerequisite for any short side).
  DECLINED with reasons on record: statistical arbitrage (a 14-sector universe
  yields too few cointegrated pairs to justify shorting + multi-leg positions),
  earnings drift (no fundamental-data pipeline), a second pullback engine
  (swing_v2 already is one), adaptive multipliers (needs the backtests first).
