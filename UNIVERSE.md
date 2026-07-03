# Trading Universe - 63+ Stocks Across 14 Sectors

**Last Updated**: July 3, 2026
**Total Stocks**: 63+
**Total Sectors**: 14
**Intraday Subset**: 12 most liquid stocks

---

## 📊 Universe Breakdown by Sector

### 1. Technology / Semiconductors (8 stocks)
- AAPL - Apple Inc.
- MSFT - Microsoft Corporation
- NVDA - NVIDIA Corporation
- AMD - Advanced Micro Devices
- AVGO - Broadcom Inc.
- CRM - Salesforce Inc.
- INTC - Intel Corporation
- PLTR - Palantir Technologies

### 2. Consumer / Retail (8 stocks)
- AMZN - Amazon.com Inc.
- TSLA - Tesla Inc.
- WMT - Walmart Inc.
- COST - Costco Wholesale
- HD - The Home Depot
- MCD - McDonald's Corporation
- NKE - Nike Inc.
- DIS - The Walt Disney Company

### 3. Communication / Media (4 stocks)
- GOOGL - Alphabet Inc. (Google)
- META - Meta Platforms Inc. (Facebook)
- NFLX - Netflix Inc.
- T - AT&T Inc.

### 4. Financials (5 stocks)
- JPM - JPMorgan Chase & Co.
- BAC - Bank of America
- GS - Goldman Sachs Group
- V - Visa Inc.
- MA - Mastercard Inc.

### 5. Healthcare (4 stocks)
- UNH - UnitedHealth Group
- JNJ - Johnson & Johnson
- LLY - Eli Lilly and Company
- PFE - Pfizer Inc.

### 6. Energy (3 stocks)
- XOM - Exxon Mobil Corporation
- CVX - Chevron Corporation
- COP - ConocoPhillips

### 7. Industrials (4 stocks)
- CAT - Caterpillar Inc.
- BA - The Boeing Company
- GE - General Electric
- UPS - United Parcel Service

### 8. Defense (8 stocks)
- LMT - Lockheed Martin Corporation
- RTX - Raytheon Technologies
- NOC - Northrop Grumman
- GD - General Dynamics
- LHX - L3Harris Technologies
- HII - Huntington Ingalls Industries
- TDG - TransDigm Group
- HWM - Howmet Aerospace

### 9. Utilities (3 stocks)
- NEE - NextEra Energy
- DUK - Duke Energy
- SO - Southern Company

### 10. Consumer Staples (3 stocks)
- PG - Procter & Gamble
- KO - The Coca-Cola Company
- PEP - PepsiCo Inc.

### 11. Materials (3 stocks)
- LIN - Linde plc
- FCX - Freeport-McMoRan Inc.
- NEM - Newmont Corporation

### 12. REITs (2 stocks)
- PLD - Prologis Inc.
- AMT - American Tower Corporation

### 13. Logistics / Transportation (2 stocks)
- FDX - FedEx Corporation
- UNP - Union Pacific Corporation

### 14. Emerging Tech (6 stocks)
- SMCI - Super Micro Computer Inc.
- ARM - Arm Holdings plc
- MU - Micron Technology
- IONQ - IonQ Inc.
- AVAV - AeroVironment Inc.
- KTOS - Kratos Defense & Security Solutions

---

## 🎯 Intraday Subset (12 Most Liquid Stocks)

Used for intraday strategy (1-minute candles):
1. AAPL - Apple Inc.
2. MSFT - Microsoft Corporation
3. NVDA - NVIDIA Corporation
4. AMD - Advanced Micro Devices
5. AMZN - Amazon.com Inc.
6. TSLA - Tesla Inc.
7. META - Meta Platforms Inc.
8. GOOGL - Alphabet Inc.
9. NFLX - Netflix Inc.
10. AVGO - Broadcom Inc.
11. PLTR - Palantir Technologies
12. JPM - JPMorgan Chase & Co.

---

## 📈 Strategy Coverage

### Swing Strategy (All 63+ Stocks)
- Scans daily candles for breakouts above 20-day highs
- Requires uptrend (price > 50-day SMA)
- Volume spike confirmation (1.3x average)
- Max 4 concurrent positions

### Intraday Strategy (12 Most Liquid Stocks)
- Scans 1-minute candles for opening range breakouts
- Requires price above VWAP
- Volume spike confirmation (1.3x average)
- Max 4 concurrent positions
- 1% trailing stop

### Mean Reversion Strategy (All 63+ Stocks)
- Scans for oversold conditions (RSI < 30)
- Requires uptrend (price > 200-day SMA)
- Max 4 concurrent positions
- 2.0x ATR stop

### Cross-Sectional Momentum Strategy (All 63+ Stocks)
- Ranks all 63+ stocks by 126-day momentum
- Holds top 3 performers
- Rebalances daily (~780 cycles at 30s intervals)
- 3.0x ATR stop (wide protective stop)

---

## 🌍 Sector Diversification

| Sector | Count | % of Universe |
|--------|-------|---------------|
| Technology/Semis | 8 | 12.7% |
| Consumer/Retail | 8 | 12.7% |
| Defense | 8 | 12.7% |
| Financials | 5 | 7.9% |
| Healthcare | 4 | 6.3% |
| Communication/Media | 4 | 6.3% |
| Industrials | 4 | 6.3% |
| Utilities | 3 | 4.8% |
| Consumer Staples | 3 | 4.8% |
| Materials | 3 | 4.8% |
| Emerging Tech | 6 | 9.5% |
| Energy | 3 | 4.8% |
| Logistics | 2 | 3.2% |
| REITs | 2 | 3.2% |

---

## 💡 Why This Universe?

### Diversification
- 14 sectors reduce correlation
- No single sector dominates
- Better risk-adjusted returns

### Liquidity
- All stocks are large-cap, highly liquid
- Tight bid-ask spreads
- High daily volume for reliable fills

### Emerging Tech Addition
- SMCI: AI infrastructure (data centers)
- ARM: Semiconductor design
- MU: Memory chips
- IONQ: Quantum computing
- AVAV: Autonomous drones
- KTOS: Defense tech

These emerging tech stocks provide exposure to future growth areas while maintaining liquidity.

---

## 🔄 Daily Refresh Strategy

- **Swing/Mean Rev/XSect**: Full universe scanned every 30 cycles (~15 minutes)
- **Intraday**: 12-stock subset scanned every cycle (30 seconds)
- **Rate Budget**: 52 calls/min (under 140 limit)
- **Caching**: Daily bars cached to reduce API calls

---

## 📊 Universe Statistics

- **Total Stocks**: 63+
- **Total Sectors**: 14
- **Intraday Subset**: 12
- **Market Cap Range**: Large-cap only ($50B+)
- **Minimum Price**: $5
- **Minimum Daily Volume**: $5M
- **Average Daily Volume**: $100M+

---

## 🎯 Universe Expansion Timeline

| Date | Stocks | Sectors | Notes |
|------|--------|---------|-------|
| Initial | 16 | 3 | Tech-heavy, correlated |
| v5 | 36 | 8 | Added financials, healthcare, energy |
| v6 (7/3/26) | 63+ | 14 | Added defense, utilities, staples, materials, REITs, logistics, emerging tech |

---

## 🚀 Future Expansion Opportunities

Potential additions (if needed):
- Small-cap growth stocks
- International stocks
- Cryptocurrency-related stocks
- Biotech/Pharma specialists
- Clean energy stocks
- Semiconductor equipment manufacturers

---

**Last Updated**: July 3, 2026
**Status**: ✅ VERIFIED & TESTED
**Ready for Trading**: ✅ YES - JULY 7, 2026

