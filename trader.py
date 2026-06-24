"""
Agentic Trading Agent v4.1 (FIXED + diagnostic logging)
=======================================================
Scans Reddit, StockTwits, STOCK Act, and SEC Form 4 for signals, with robust
error handling and API stability.

This build is tuned for DIAGNOSIS — every scanner reports its result count on
every cycle (even 0), so a quiet-but-working feed is clearly distinguishable
from a failed one in the logs.

Key features:
  - Circuit breaker for API failures (fail 3x -> skip that source 5 min)
  - Per-call logging with HTTP status + timing for every source
  - Reddit now ALWAYS logs its count (the previous build hid 0-result scans)
  - Single reliable scan interval; rate-limiting handled by the circuit
    breaker + per-source pacing
  - Graceful error handling (no crashes on API failures), GC every 100 scans

NOTE: this diagnostic build uses plain liquidity screening (the v4.1 trend /
volume / confirmation filters are not included here, to maximise visibility
while we confirm the data feeds work). They can be re-added afterwards.

Shares engine + scorecard are imported from strategy_lab.py (same package).
Not financial advice. You are responsible for every trade you approve.
"""

import time
import json
import os
import schedule
import requests
import pytz
import gc
import logging
from datetime import datetime, timedelta
from collections import defaultdict

import strategy_lab as lab   # CostModel, RiskConfig, fetch_history, report, Trade

# -- LOGGING SETUP --
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# -- MODE --
TRADING_MODE = os.getenv("TRADING_MODE", "PAPER").upper()   # PAPER | LIVE

# -- STRATEGY CONFIG (set in Railway env vars) --
INIT_STOP_PCT   = float(os.getenv("STOP_LOSS_PCT", "0.05"))
TRAIL_PCT       = float(os.getenv("TRAIL_PCT", "0.06"))
_tp             = os.getenv("TAKE_PROFIT_PCT", "")
TAKE_PROFIT_PCT = float(_tp) if _tp.strip() else None
RISK_PER_TRADE  = float(os.getenv("RISK_PER_TRADE_PCT", "0.01"))
MAX_POSITION    = float(os.getenv("MAX_POSITION_SIZE", "3000"))
START_EQUITY    = float(os.getenv("START_EQUITY", "50000"))
MIN_CONFIDENCE  = float(os.getenv("MIN_CONFIDENCE", "70"))
SOCIAL_WEIGHT   = float(os.getenv("SOCIAL_WEIGHT", "0.40"))
DAILY_LOSS_LIM  = float(os.getenv("DAILY_LOSS_LIMIT", "2500"))
MAX_POSITIONS   = int(os.getenv("MAX_CONCURRENT_POSITIONS", "8"))
SCAN_INTERVAL   = int(os.getenv("SCAN_INTERVAL_SECS", "30"))
MIN_PRICE       = float(os.getenv("MIN_PRICE", "5"))
MIN_DOLLAR_VOL  = float(os.getenv("MIN_DOLLAR_VOL", "5000000"))
# Reddit post-quality thresholds (relaxed from the old 200 / 0.80 / 0.85, and
# now tunable from Railway env without a code change).
REDDIT_MIN_SCORE = int(os.getenv("REDDIT_MIN_SCORE", "100"))
REDDIT_MIN_RATIO = float(os.getenv("REDDIT_MIN_RATIO", "0.70"))
REDDIT_BUY_RATIO = float(os.getenv("REDDIT_BUY_RATIO", "0.80"))
# Reddit OAuth (app-only). Register a "script" app at reddit.com/prefs/apps to
# get the id/secret. Without these, Reddit is skipped cleanly (no public-endpoint
# hammering). User agent must be descriptive incl. your username per Reddit rules.
REDDIT_CLIENT_ID     = os.getenv("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET", "")
REDDIT_USER_AGENT    = os.getenv(
    "REDDIT_USER_AGENT", "python:agentic-trader:v4.1 (by /u/your_username)")
EOD_HOUR        = int(os.getenv("EOD_CLOSE_HOUR", "15"))
EOD_MIN         = int(os.getenv("EOD_CLOSE_MIN", "50"))
WEBHOOK_URL     = os.getenv("CLAUDE_WEBHOOK_URL", "")
NTFY_TOPIC      = os.getenv("NTFY_TOPIC", "")
NTFY_SERVER     = os.getenv("NTFY_SERVER", "https://ntfy.sh")
PAPER_LOG       = os.getenv("PAPER_LOG", "paper_trades.jsonl")

# -- API CIRCUIT BREAKER --
API_FAILURE_THRESHOLD = 3  # Fail 3 times, then skip for 5 minutes
api_failures = defaultdict(int)
api_skip_until = defaultdict(lambda: None)

ET = pytz.timezone("America/New_York")
COST = lab.CostModel()

# -- STATE --
positions      = {}
paper_trades   = []
equity         = START_EQUITY
daily_realized = 0.0
scan_count     = 0
signals_fired  = 0
discovered     = set()
wins = losses  = 0
_last_day      = None


def log(msg, level="INFO"):
    """Enhanced logging with ET timezone and level tags."""
    now = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET")
    tag = {
        "INFO": "[i]", "BUY": "[BUY]", "SELL": "[SELL]", "WARN": "[!]",
        "ERROR": "[X]", "SCAN": "[scan]", "SOCIAL": "[soc]",
        "INSIDER": "[ins]", "PAPER": "[paper]", "API": "[api]"
    }.get(level, "   ")
    print(f"[{now}] {tag} {msg}", flush=True)
    if level == "ERROR":
        logger.error(msg)


# -- MARKET HOURS --
def market_is_open():
    """Check if US stock market is currently open (9:30 AM - 4:00 PM ET, Mon-Fri)."""
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return False
    if now.hour < 9 or (now.hour == 9 and now.minute < 30):
        return False
    if now.hour >= 16:           # market closes at 4:00 PM ET
        return False
    return True


def is_eod():
    """End-of-day close window (3:50 PM - 4:00 PM ET)."""
    now = datetime.now(ET)
    return now.hour == EOD_HOUR and now.minute >= EOD_MIN


# -- API CIRCUIT BREAKER --
def should_skip_api(source_name):
    """Skip a source if its circuit breaker is open."""
    if api_skip_until[source_name] is None:
        return False
    if datetime.now(ET) < api_skip_until[source_name]:
        return True
    api_failures[source_name] = 0          # reset after the skip window
    api_skip_until[source_name] = None
    return False


def record_api_failure(source_name):
    """Count a failure; open the breaker once the threshold is hit."""
    api_failures[source_name] += 1
    if api_failures[source_name] >= API_FAILURE_THRESHOLD:
        api_skip_until[source_name] = datetime.now(ET) + timedelta(minutes=5)
        log(f"Circuit breaker activated for {source_name} "
            f"(failed {api_failures[source_name]} times). Skipping for 5 minutes.", "WARN")


def record_api_success(source_name):
    """Reset failure counter on a successful call."""
    if api_failures[source_name] > 0:
        log(f"{source_name} recovered after {api_failures[source_name]} failures", "API")
    api_failures[source_name] = 0


_reddit_token_cache = {"value": None, "expires": 0}


def _reddit_oauth_token():
    """App-only OAuth bearer token (client_credentials grant), cached until it
    nears expiry. Returns None if credentials aren't configured."""
    if not (REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET):
        return None
    now = time.time()
    if _reddit_token_cache["value"] and now < _reddit_token_cache["expires"]:
        return _reddit_token_cache["value"]
    try:
        r = requests.post(
            "https://www.reddit.com/api/v1/access_token",
            auth=(REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET),
            data={"grant_type": "client_credentials"},
            headers={"User-Agent": REDDIT_USER_AGENT},
            timeout=8,
        )
        if r.status_code != 200:
            log(f"Reddit OAuth: HTTP {r.status_code}", "WARN")
            return None
        tok = r.json()
        _reddit_token_cache["value"] = tok["access_token"]
        _reddit_token_cache["expires"] = now + tok.get("expires_in", 3600) - 60
        return _reddit_token_cache["value"]
    except Exception as e:
        log(f"Reddit OAuth error: {type(e).__name__}", "WARN")
        return None


# Common ALL-CAPS words/acronyms that are not tickers (or are words that
# collide with tickers). Used to filter bare-word matches in Reddit titles.
# Bare matches are WATCH-only, so this list being imperfect is low-stakes.
REDDIT_TICKER_STOPWORDS = frozenset({
    "CEO", "CFO", "COO", "CTO", "IPO", "ETF", "ATH", "ATL", "YOLO", "FOMO",
    "FUD", "WSB", "USA", "USD", "EUR", "GBP", "GDP", "CPI", "PPI", "FED",
    "SEC", "IRS", "FBI", "CIA", "EPS", "PEG", "EOD", "IMO", "IMHO", "TLDR",
    "EDIT", "NSFW", "FYI", "LOL", "LMAO", "IRA", "HODL", "BTFD", "NFA", "API",
    "OTC", "SPAC", "CAGR", "ROI", "ROE", "GAAP", "RIP", "YES", "NOT", "NEW",
    "NOW", "BIG", "TOP", "LOW", "BUY", "SELL", "HOLD", "CALL", "PUT", "CALLS",
    "PUTS", "GAIN", "LOSS", "BULL", "BEAR", "MOON", "RED", "HIGH", "NYSE",
    "FAANG", "DCA", "EOY", "YTD", "WTF", "OMG", "DAY", "CASH", "DEBT", "BANK",
    "RATE", "DATA", "NEWS", "THE", "AND", "FOR", "ARE", "WILL", "JUST", "ALL",
    "KEY", "CAR", "GPS", "PLAY", "FUN", "OPEX", "EARN", "PUMP", "DUMP",
})


# -- SIGNAL SOURCES (robust error handling) --
def scan_reddit():
    """Scan Reddit. ALWAYS logs the result count (even 0) so a quiet scan is
    distinguishable from a failed one."""
    if should_skip_api("reddit"):
        log("Reddit skipped (circuit breaker active)", "API")
        return []

    signals = []
    ok_count = 0
    subreddits = ["wallstreetbets", "stocks", "investing", "smallcaps",
                  "SecurityAnalysis", "options"]

    # OAuth is required: the public hot.json endpoint gets 403'd from cloud IPs.
    # No credentials -> skip cleanly rather than hammer and trip the breaker.
    token = _reddit_oauth_token()
    if not token:
        log("Reddit skipped: no OAuth credentials (set REDDIT_CLIENT_ID/SECRET)", "SOCIAL")
        return signals
    headers = {"Authorization": f"bearer {token}", "User-Agent": REDDIT_USER_AGENT}

    for sub in subreddits:
        try:
            start = time.time()
            url = f"https://oauth.reddit.com/r/{sub}/hot?limit=15"
            r = requests.get(url, headers=headers, timeout=8)
            elapsed = time.time() - start

            if r.status_code != 200:
                log(f"Reddit r/{sub}: HTTP {r.status_code} ({elapsed:.1f}s)", "WARN")
                record_api_failure("reddit")
                continue

            ok_count += 1
            for post in r.json().get("data", {}).get("children", []):
                p = post["data"]
                title, score = p.get("title", ""), p.get("score", 0)
                ratio, comments = p.get("upvote_ratio", 0), p.get("num_comments", 0)

                if score < REDDIT_MIN_SCORE or ratio < REDDIT_MIN_RATIO:
                    continue

                import re
                base = min(95, 50 + (score // 100) + (comments // 20))
                # $CASHTAGs are explicit ticker intent -> eligible to BUY.
                cashtags = set(re.findall(r'\$([A-Z]{2,5})', title))
                for ticker in cashtags:
                    signals.append({
                        "ticker": ticker, "source": f"r/{sub}",
                        "headline": title[:120], "confidence": base,
                        "action": "BUY" if ratio > REDDIT_BUY_RATIO else "WATCH"
                    })
                # Bare ALL-CAPS words (3-5 letters) are ambiguous -> surface for
                # discovery only (WATCH), never auto-BUY. Stopwords + the
                # downstream liquidity check keep noise out of the trade path.
                bare = set(re.findall(r'\b([A-Z]{3,5})\b', title)) - cashtags
                for ticker in bare - REDDIT_TICKER_STOPWORDS:
                    signals.append({
                        "ticker": ticker, "source": f"r/{sub}",
                        "headline": title[:120], "confidence": base,
                        "action": "WATCH"
                    })

            time.sleep(0.5)   # be polite between subreddits

        except requests.Timeout:
            log(f"Reddit r/{sub}: TIMEOUT (>8s)", "ERROR")
            record_api_failure("reddit")
            continue
        except requests.ConnectionError as e:
            log(f"Reddit r/{sub}: CONNECTION ERROR - {str(e)[:50]}", "ERROR")
            record_api_failure("reddit")
            continue
        except Exception as e:
            log(f"Reddit r/{sub}: {type(e).__name__} - {str(e)[:50]}", "ERROR")
            record_api_failure("reddit")
            continue

    # ALWAYS report the outcome — this is the key diagnostic line.
    log(f"Reddit scan: {len(signals)} signals from {ok_count}/{len(subreddits)} subreddits", "SOCIAL")
    if ok_count > 0:
        record_api_success("reddit")

    return signals


def scan_stocktwits(ticker):
    """StockTwits sentiment for a ticker, with timeout and error handling."""
    if should_skip_api("stocktwits"):
        return None
    try:
        start = time.time()
        url = f"https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json"
        r = requests.get(url, timeout=8)
        elapsed = time.time() - start

        if r.status_code != 200:
            log(f"StockTwits {ticker}: HTTP {r.status_code} ({elapsed:.1f}s)", "WARN")
            record_api_failure("stocktwits")
            return None

        messages = r.json().get("messages", [])
        if not messages:
            record_api_success("stocktwits")   # call worked, just no messages
            return None

        bulls = sum(1 for m in messages
                    if m.get("entities", {}).get("sentiment", {}).get("basic") == "Bullish")
        total = len(messages)
        bull_pct = bulls / total * 100

        record_api_success("stocktwits")
        return {
            "ticker": ticker, "bull_pct": bull_pct,
            "confidence": min(95, int(bull_pct)),
            "action": "BUY" if bull_pct > 70 else "SELL" if bull_pct < 30 else "WATCH"
        }

    except requests.Timeout:
        log(f"StockTwits {ticker}: TIMEOUT (>8s)", "ERROR")
        record_api_failure("stocktwits")
        return None
    except requests.ConnectionError as e:
        log(f"StockTwits {ticker}: CONNECTION ERROR - {str(e)[:50]}", "ERROR")
        record_api_failure("stocktwits")
        return None
    except Exception as e:
        log(f"StockTwits {ticker}: {type(e).__name__} - {str(e)[:50]}", "ERROR")
        record_api_failure("stocktwits")
        return None


def scan_sec_stock_act():
    """Congressional STOCK Act disclosures, with timeout and error handling."""
    if should_skip_api("sec_stock_act"):
        log("SEC STOCK Act skipped (circuit breaker active)", "API")
        return []

    signals = []
    try:
        start = time.time()
        url = "https://house-stock-watcher-data.s3-us-east-2.amazonaws.com/data/all_transactions.json"
        r = requests.get(url, timeout=12)
        elapsed = time.time() - start

        if r.status_code != 200:
            log(f"SEC STOCK Act: HTTP {r.status_code} ({elapsed:.1f}s)", "WARN")
            record_api_failure("sec_stock_act")
            return signals

        for trade in r.json()[:200]:
            try:
                ticker = trade.get("ticker", "").strip().upper()
                t_type = trade.get("type", "").lower()
                if not ticker or ticker in ("--", "N/A") or t_type not in ("purchase", "sale"):
                    continue
                signals.append({
                    "ticker": ticker, "source": "STOCK Act",
                    "headline": f"{trade.get('representative','?')} {t_type} {ticker}",
                    "confidence": 85 if t_type == "purchase" else 60,
                    "action": "BUY" if t_type == "purchase" else "SELL"
                })
            except:
                continue

        record_api_success("sec_stock_act")
        log(f"SEC STOCK Act scan: {len(signals)} filings ({elapsed:.1f}s)", "INSIDER")

    except requests.Timeout:
        log("SEC STOCK Act: TIMEOUT (>12s)", "ERROR")
        record_api_failure("sec_stock_act")
    except requests.ConnectionError as e:
        log(f"SEC STOCK Act: CONNECTION ERROR - {str(e)[:50]}", "ERROR")
        record_api_failure("sec_stock_act")
    except Exception as e:
        log(f"SEC STOCK Act: {type(e).__name__} - {str(e)[:50]}", "ERROR")
        record_api_failure("sec_stock_act")

    return signals[:10]


def scan_sec_form4():
    """SEC Form 4 insider buys via OpenInsider, with timeout and error handling."""
    if should_skip_api("sec_form4"):
        log("SEC Form 4 skipped (circuit breaker active)", "API")
        return []

    signals = []
    try:
        start = time.time()
        url = (
            "https://openinsider.com/screener?s=&o=&pl=&ph=&ll=&lh=&fd=1&fdr=&td=0"
            "&tdr=&daysago=1&xp=1&xs=1&vl=25&cnt=20&page=1&sortcol=0"
        )
        r = requests.get(url, timeout=10, headers={"User-Agent": "AgenticTrader/4.1"})
        elapsed = time.time() - start

        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.text, "html.parser")
        table = soup.find("table", {"class": "tinytable"})

        if not table:
            log(f"SEC Form 4: No table found ({elapsed:.1f}s)", "WARN")
            record_api_failure("sec_form4")
            return signals

        for row in table.find_all("tr")[1:16]:
            cols = row.find_all("td")
            if len(cols) < 8:
                continue
            try:
                ticker = cols[3].get_text(strip=True).upper()
                if "P" in cols[6].get_text(strip=True):
                    signals.append({
                        "ticker": ticker, "source": "SEC Form 4",
                        "headline": f"Insider buy {ticker}",
                        "confidence": 88, "action": "BUY"
                    })
            except:
                continue

        record_api_success("sec_form4")
        log(f"SEC Form 4 scan: {len(signals)} insider buys ({elapsed:.1f}s)", "INSIDER")

    except requests.Timeout:
        log("SEC Form 4: TIMEOUT (>10s)", "ERROR")
        record_api_failure("sec_form4")
    except requests.ConnectionError as e:
        log(f"SEC Form 4: CONNECTION ERROR - {str(e)[:50]}", "ERROR")
        record_api_failure("sec_form4")
    except Exception as e:
        log(f"SEC Form 4: {type(e).__name__} - {str(e)[:50]}", "ERROR")
        record_api_failure("sec_form4")

    return signals


# -- PRICE & LIQUIDITY --
def get_price(ticker):
    """Latest price from the most recent 1-minute bar, falling back to meta.
    Reading the bar avoids the stale-ish meta.regularMarketPrice that the
    previous version returned."""
    try:
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/"
            f"{ticker}?interval=1m&range=1d"
        )
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
        if r.status_code != 200:
            return None
        result = r.json()["chart"]["result"][0]
        closes = result.get("indicators", {}).get("quote", [{}])[0].get("close") or []
        for c in reversed(closes):          # most recent non-null bar close
            if c is not None:
                return float(c)
        price = result.get("meta", {}).get("regularMarketPrice")   # fallback
        return float(price) if price else None
    except requests.Timeout:
        log(f"Yahoo Finance {ticker}: TIMEOUT", "WARN")
        return None
    except Exception:
        return None


def check_liquidity(ticker):
    """Return (ok, price, avg_dollar_vol). Uses real history; no data => not ok."""
    try:
        bars = lab.fetch_history(ticker, 30)
        if len(bars) < 5:
            return False, None, 0.0
        price = bars[-1]["c"]
        window = bars[-20:]
        advol = sum(b["c"] * b["v"] for b in window) / len(window)
        return (price >= MIN_PRICE and advol >= MIN_DOLLAR_VOL), price, advol
    except Exception as e:
        log(f"Liquidity check {ticker}: {type(e).__name__}", "WARN")
        return False, None, 0.0


def size_position(entry, init_stop, eq):
    """Fixed-fractional risk sizing, capped at MAX_POSITION and no-leverage."""
    per_share_risk = entry - init_stop
    if per_share_risk <= 0:
        return 0.0
    shares = (eq * RISK_PER_TRADE) / per_share_risk
    return max(0.0, min(shares, MAX_POSITION / entry, eq / entry))


# -- SIGNAL PROCESSING --
def blend_confidence(fundamental_conf, ticker):
    """Blend fundamental + social. If social data is MISSING, do not fabricate."""
    st = scan_stocktwits(ticker)
    if st is None:
        return float(fundamental_conf), False
    social = st["confidence"]
    blended = fundamental_conf * (1 - SOCIAL_WEIGHT) + social * SOCIAL_WEIGHT
    return round(blended, 1), True


def process_signal(sig):
    global signals_fired
    ticker = sig.get("ticker", "").upper()
    action = sig.get("action", "WATCH")
    if not ticker or len(ticker) > 5:
        return
    if action == "BUY":
        if ticker in positions or len(positions) >= MAX_POSITIONS:
            return
        blended, social_ok = blend_confidence(sig.get("confidence", 50), ticker)
        if blended >= MIN_CONFIDENCE:
            signals_fired += 1
            execute_buy(ticker, sig.get("source", "?"), sig.get("headline", ""),
                        blended, social_ok)
    elif action == "SELL" and ticker in positions:
        signals_fired += 1
        execute_sell(ticker, f"Signal sell: {sig.get('source','?')}")
    if ticker not in discovered:
        discovered.add(ticker)


# -- EXECUTION (mode-aware) --
def execute_buy(ticker, source, reason, confidence, social_ok):
    global positions
    if daily_realized <= -DAILY_LOSS_LIM:
        log(f"Daily loss limit hit -- skipping {ticker}", "WARN")
        return

    ok, price, advol = check_liquidity(ticker)
    if not ok or not price:
        log(f"Skip {ticker}: failed liquidity/data filter (price={price})", "WARN")
        return

    init_stop = price * (1 - INIT_STOP_PCT)
    shares = size_position(price, init_stop, equity)
    if shares <= 0:
        log(f"Skip {ticker}: position sizes to zero", "WARN")
        return

    pos = {
        "entry_raw": price, "entry_fill": COST.buy_fill(price),
        "init_stop": init_stop, "high": price, "shares": shares,
        "source": source, "confidence": confidence, "social_ok": social_ok,
        "entry_time": datetime.now(ET),
    }
    positions[ticker] = pos
    notional = shares * price
    q = "" if social_ok else " [social data missing]"

    if TRADING_MODE == "LIVE":
        log(f"LIVE BUY {ticker} ~{shares:.1f}sh @ ${price:.2f} (~${notional:.0f}) "
            f"conf {confidence}%{q}", "BUY")
        notify_claude("BUY", ticker, notional, reason, confidence)
        send_alert(f"~{shares:.1f} sh @ ${price:.2f}\nConf {confidence}%\n{reason[:70]}\n\n"
                   f'Reply to Claude: "buy {ticker} ${notional:.0f}"',
                   title=f"BUY SIGNAL: {ticker}")
    else:
        log(f"PAPER BUY {ticker} {shares:.2f}sh @ ${price:.2f} (fill ${pos['entry_fill']:.2f}) "
            f"conf {confidence}%{q}", "PAPER")


def execute_sell(ticker, reason):
    global positions, equity, daily_realized, wins, losses
    if ticker not in positions:
        return

    pos = positions[ticker]
    exit_raw = get_price(ticker)

    if TRADING_MODE == "LIVE":
        log(f"LIVE SELL {ticker} | {reason}", "SELL")
        notify_claude("SELL", ticker, 0, reason, 0)
        send_alert(f"{reason}\n\nReply to Claude: \"sell {ticker}\"",
                   title=f"SELL SIGNAL: {ticker}")
        del positions[ticker]
        return

    if not exit_raw:
        log(f"PAPER SELL {ticker}: no price, closing at entry", "WARN")
        exit_raw = pos["entry_raw"]

    exit_fill = COST.sell_fill(exit_raw)
    pnl = (exit_fill - pos["entry_fill"]) * pos["shares"]
    ret = (exit_fill - pos["entry_fill"]) / pos["entry_fill"]
    equity += pnl
    daily_realized += pnl
    if ret > 0:
        wins += 1
    else:
        losses += 1

    held = max(0, (datetime.now(ET) - pos["entry_time"]).days)
    trade = lab.Trade(
        ticker=ticker,
        entry_date=pos["entry_time"].strftime("%Y-%m-%d %H:%M"),
        exit_date=datetime.now(ET).strftime("%Y-%m-%d %H:%M"),
        entry=round(pos["entry_fill"], 4), exit=round(exit_fill, 4),
        shares=round(pos["shares"], 3), ret_pct=ret, pnl=pnl,
        reason=reason, held_days=held
    )
    paper_trades.append(trade)
    _append_log(trade)
    log(f"PAPER SELL {ticker} {ret*100:+.2f}% (${pnl:+.0f}) | {reason} | equity ${equity:,.0f}", "PAPER")
    del positions[ticker]


def monitor_positions():
    """Trailing-stop enforcement, every scan."""
    for ticker in list(positions.keys()):
        pos = positions[ticker]
        price = get_price(ticker)
        if not price:
            continue
        pos["high"] = max(pos["high"], price)
        stop = max(pos["init_stop"], pos["high"] * (1 - TRAIL_PCT))
        change = (price - pos["entry_raw"]) / pos["entry_raw"] * 100
        if price <= stop:
            execute_sell(ticker, f"Trailing stop {change:+.1f}%")
        elif TAKE_PROFIT_PCT is not None and price >= pos["entry_raw"] * (1 + TAKE_PROFIT_PCT):
            execute_sell(ticker, f"Take-profit {change:+.1f}%")


def close_all_positions(reason="EOD auto-close"):
    if not positions:
        return
    log(f"Closing all {len(positions)} positions -- {reason}", "WARN")
    for ticker in list(positions.keys()):
        execute_sell(ticker, reason)


def _append_log(trade):
    try:
        with open(PAPER_LOG, "a") as f:
            f.write(json.dumps(trade.__dict__) + "\n")
    except Exception as e:
        log(f"Paper log write error: {e}", "ERROR")


# -- ALERTS / WEBHOOK (LIVE only) --
def send_alert(message, title="Trading Agent", priority="high"):
    if not NTFY_TOPIC:
        return
    try:
        requests.post(
            f"{NTFY_SERVER}/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": priority, "Tags": "chart_with_upwards_trend"},
            timeout=10
        )
    except Exception as e:
        log(f"Alert error: {e}", "ERROR")


def notify_claude(action, ticker, amount_usd, reason, confidence):
    if not WEBHOOK_URL:
        log(f"[no webhook] would {action} {ticker} ${amount_usd:.0f}", "WARN")
        return
    try:
        requests.post(
            WEBHOOK_URL,
            json={"action": action, "ticker": ticker, "amount_usd": amount_usd,
                  "reason": reason, "confidence": confidence,
                  "timestamp": datetime.now(ET).isoformat()},
            timeout=10
        )
    except Exception as e:
        log(f"Webhook error: {e}", "ERROR")


# -- SCAN CYCLE --
def run_scan():
    global scan_count
    scan_count += 1
    now_str = datetime.now(ET).strftime("%H:%M:%S")

    if not market_is_open():
        if scan_count % 20 == 1:
            log(f"Market closed ({now_str} ET) -- idle", "INFO")
        return

    if _last_day != datetime.now(ET).date():
        reset_daily()

    log(f"-- Scan #{scan_count} | {now_str} | mode:{TRADING_MODE} | "
        f"pos:{len(positions)}/{MAX_POSITIONS} | P&L:${daily_realized:+.0f} --", "SCAN")

    if is_eod():
        close_all_positions("3:50 PM ET auto-close")
        return

    monitor_positions()

    if daily_realized <= -DAILY_LOSS_LIM:
        if scan_count % 10 == 1:
            log("Daily loss limit reached -- monitoring only", "WARN")
        return

    # Scan with error handling
    signals = scan_reddit()
    if scan_count % 5 == 0:
        signals += scan_sec_stock_act()
    if scan_count % 10 == 0:
        signals += scan_sec_form4()

    seen = set()
    for sig in signals:
        key = f"{sig.get('ticker','').upper()}:{sig.get('action')}"
        if key in seen:
            continue
        seen.add(key)
        process_signal(sig)

    if scan_count % 100 == 0:
        gc.collect()
        log(f"Garbage collection run (scan #{scan_count})", "INFO")


def reset_daily():
    global daily_realized, _last_day
    daily_realized = 0.0
    _last_day = datetime.now(ET).date()
    log("Daily realized P&L reset", "INFO")


def print_status():
    closed = wins + losses
    wr = (wins / closed * 100) if closed else 0.0
    log("-" * 48, "INFO")
    log(f"STATUS @ {datetime.now(ET).strftime('%H:%M ET')} | mode: {TRADING_MODE}", "INFO")
    log(f"  Open positions : {len(positions)}", "INFO")
    log(f"  Closed (paper) : {closed} ({wins}W/{losses}L) win rate {wr:.1f}%", "INFO")
    log(f"  Equity (paper) : ${equity:,.0f}  (start ${START_EQUITY:,.0f})", "INFO")
    log(f"  Signals fired  : {signals_fired} | tickers seen: {len(discovered)}", "INFO")
    log("-" * 48, "INFO")
    if TRADING_MODE == "PAPER" and len(paper_trades) >= 5:
        cfg = lab.RiskConfig(init_stop_pct=INIT_STOP_PCT, trail_pct=TRAIL_PCT,
                             risk_per_trade_pct=RISK_PER_TRADE)
        lab.report(paper_trades, cfg, start_equity=START_EQUITY)


if __name__ == "__main__":
    log("Agentic Market Scanner v4.1 (diagnostic logging) starting...", "INFO")
    log(f"  MODE           : {TRADING_MODE}", "INFO")
    log(f"  Initial stop   : -{INIT_STOP_PCT*100:.1f}%  |  Trailing: -{TRAIL_PCT*100:.1f}%", "INFO")
    log(f"  Take-profit    : {'trailing only' if TAKE_PROFIT_PCT is None else f'+{TAKE_PROFIT_PCT*100:.1f}%'}", "INFO")
    log(f"  Risk/trade     : {RISK_PER_TRADE*100:.1f}% of equity (cap ${MAX_POSITION:,.0f})", "INFO")
    log(f"  Liquidity gate : price>=${MIN_PRICE:.0f}, avg $vol>=${MIN_DOLLAR_VOL:,.0f}", "INFO")
    log(f"  Min confidence : {MIN_CONFIDENCE}%  |  Social weight: {SOCIAL_WEIGHT*100:.0f}%", "INFO")
    log(f"  Daily loss lim : -${DAILY_LOSS_LIM:,.0f}", "INFO")
    log(f"  Scan interval  : {SCAN_INTERVAL}s", "INFO")

    if TRADING_MODE == "LIVE":
        log("  LIVE MODE -- real-money trade alerts. Only run this after PAPER", "WARN")
        log("  shows a positive scorecard. You confirm every trade in chat.", "WARN")
    else:
        log("  PAPER MODE -- $0 at risk. Logs hypothetical fills to " + PAPER_LOG, "INFO")

    log("Sources: Reddit, StockTwits, STOCK Act, SEC Form 4", "INFO")
    log("API Circuit Breaker: Fail 3x, skip 5 min", "INFO")

    reset_daily()
    run_scan()

    schedule.every(SCAN_INTERVAL).seconds.do(run_scan)
    schedule.every(5).minutes.do(print_status)
    schedule.every().day.at("09:30").do(reset_daily)
    schedule.every().day.at("15:50").do(lambda: close_all_positions("Scheduled EOD"))

    while True:
        schedule.run_pending()
        time.sleep(1)
