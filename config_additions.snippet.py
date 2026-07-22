# ============================================================================
# config.py additions — UPDATED against the REAL config.py (2026-07-22).
# Verified: config UNIVERSE = exactly 63, zero duplicates, matches the
# 14-sector table, full sector_map coverage. Pre-condition CLEARED: 63+5=68.
#
# Your config builds UNIVERSE from env var `UNIVERSE` if set, else the
# default string. Two equivalent ways to add the five — pick ONE:
# ============================================================================

# --- OPTION A (recommended): edit the default string in config.py ----------
# Slot each ticker into its sector line so the file stays self-documenting:
#
#   # financials
#   "JPM,BAC,GS,V,MA,PGR,CME,"          # + PGR (insurer), CME (exchange)
#   # healthcare
#   "UNH,JNJ,LLY,PFE,ABBV,MRK,"         # + ABBV, MRK (pharma depth)
#   # consumer / retail
#   "AMZN,TSLA,WMT,COST,HD,MCD,NKE,DIS,DHI,"   # + DHI (homebuilder)
#
# (Just append to those three lines; no other line changes. Result: 68.)

# --- OPTION B: Railway env var, no code push -------------------------------
# Set UNIVERSE to the full 68-name comma-separated list. CAUTION: the env
# var REPLACES the default entirely — an incomplete list silently shrinks
# the universe. Option A is safer for a permanent change.

# --- Gates before either option ships (unchanged) --------------------------
#   1. backtest_xsect.py --with-additions approves them on both windows
#   2. Ship after hours; additions change xsect rankings on day one

# ============================================================================
# OPTIONAL config tidy — nothing below is required; noted from review:
# ============================================================================
#   1. Stale comment: "~36 liquid large-caps across 8 sectors" — it's 63
#      across 14. Update when convenient so the file matches reality.
#   2. DAILY_LOOKBACK_DAYS default is 120; feed_layer floors it to 500 with
#      a boot warning every start. Set the default (or env) to 500 to
#      silence the warning: the floor is doing the real work either way.
#   3. XSectParams.rebalance_cycles (XS_REBAL_CYCLES=780) is dead code —
#      xsection.py uses DailyRebalanceGate since Jul 9. Harmless; delete
#      whenever the dataclass is next touched.
