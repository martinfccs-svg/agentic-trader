"""sector_map.py — sector classification for the equity universe.

Source of truth: the operator's 14-sector universe table (2026-07-22), plus
five additions chosen to widen genuinely-different correlation streams:
healthcare breadth (ABBV, MRK), an insurer (PGR — financials were all banks
and payment rails), an exchange (CME — one of the few equities that likes
volatility), and a rate-cyclical (DHI — homebuilder rate-cut torque).

Consumers:
  xsection.py     sector cap in the top-N rotation (the reason this exists:
                  with 14 sectors available, uncapped relative-strength
                  ranking still produced AMD/ARM/MU — one semi bet, three
                  tickers)
  backtest_xsect.py  replays capped vs uncapped rotation history

Design notes:
  - "Tech/semis" and "Emerging tech" are DELIBERATELY MERGED for capping
    purposes: NVDA/AMD/AVGO/SMCI/ARM/MU trade as one cluster; keeping the
    operator's finer labels would let the cap hold two of them at once.
    Same logic merges Defense with the defense-adjacent emerging names
    (AVAV, KTOS) and Logistics with Industrials-freight overlap kept apart
    only where the correlation is genuinely different.
  - Unknown tickers map to their own name as a sector ("unmapped:<T>") —
    fail-open: an unmapped name can always be selected, but can never
    crowd out others, and it logs so the map gets fixed.
"""

from __future__ import annotations

import logging

log = logging.getLogger("sector_map")

SECTOR_MAP: dict[str, str] = {
    # Tech / semis + emerging-tech compute (merged cluster — see notes)
    "AAPL": "tech", "MSFT": "tech", "NVDA": "tech", "AMD": "tech",
    "AVGO": "tech", "CRM": "tech", "INTC": "tech", "PLTR": "tech",
    "SMCI": "tech", "ARM": "tech", "MU": "tech", "IONQ": "tech",
    # Consumer / retail
    "AMZN": "consumer", "TSLA": "consumer", "WMT": "consumer",
    "COST": "consumer", "HD": "consumer", "MCD": "consumer",
    "NKE": "consumer", "DIS": "consumer",
    # Communication / media
    "GOOGL": "comms", "META": "comms", "NFLX": "comms", "T": "comms",
    # Financials (banks + payment rails)
    "JPM": "financials", "BAC": "financials", "GS": "financials",
    "V": "financials", "MA": "financials",
    # Healthcare
    "UNH": "healthcare", "JNJ": "healthcare", "LLY": "healthcare",
    "PFE": "healthcare",
    # Energy
    "XOM": "energy", "CVX": "energy", "COP": "energy",
    # Industrials
    "CAT": "industrials", "BA": "industrials", "GE": "industrials",
    "UPS": "industrials",
    # Defense (incl. defense-adjacent emerging tech)
    "LMT": "defense", "RTX": "defense", "NOC": "defense", "GD": "defense",
    "LHX": "defense", "HII": "defense", "TDG": "defense", "HWM": "defense",
    "AVAV": "defense", "KTOS": "defense",
    # Utilities
    "NEE": "utilities", "DUK": "utilities", "SO": "utilities",
    # Consumer staples
    "PG": "staples", "KO": "staples", "PEP": "staples",
    # Materials (incl. gold)
    "LIN": "materials", "FCX": "materials", "NEM": "materials",
    # REITs
    "PLD": "reits", "AMT": "reits",
    # Logistics / transports
    "FDX": "transports", "UNP": "transports",
    # ---- additions (2026-07-22) ----
    "ABBV": "healthcare", "MRK": "healthcare",
    "PGR": "financials",          # insurance: different stream than banks
    "CME": "financials",          # exchange: likes volatility
    "DHI": "consumer",            # homebuilder: rate-cut cyclical
}

# Names to append to config.UNIVERSE when adopting the expanded universe.
# Backtest with backtest_xsect.py --with-additions BEFORE deploying: new
# names change xsect rankings and can trigger rotations on day one.
UNIVERSE_ADDITIONS = ["ABBV", "MRK", "PGR", "CME", "DHI"]

_warned: set[str] = set()


def sector_of(ticker: str) -> str:
    s = SECTOR_MAP.get(ticker)
    if s is None:
        if ticker not in _warned:
            _warned.add(ticker)
            log.warning("sector_map: %s unmapped — treated as its own "
                        "sector (fail-open); add it to SECTOR_MAP", ticker)
        return f"unmapped:{ticker}"
    return s
