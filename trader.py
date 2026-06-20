"""
Agentic Trading Agent v3.1
==========================
Scans Reddit, StockTwits, STOCK Act, and SEC Form 4 for trading signals
across the US market.

Runs 24/7 on Railway. Sends alerts; trades are executed by a human in chat
(via Robinhood Agentic MCP through Claude).

v3.1 changes:
  - Live price feed (get_price)
  - Take-profit / stop-loss now actually enforced every scan (monitor_positions)
  - Daily loss limit now actually halts new buys
  - Realized P&L + win/loss tracking -> a real (estimated) win rate

NOTE: the agent only alerts; you place the trade. So recorded entry prices are
the price seen at signal time, not your real fill. P&L is therefore an ESTIMATE.
Your brokerage account is the source of truth.
"""

import time
import schedule
import requests
import json
import os
import pytz
from datetime import datetime, timedelta

# ── CONFIG (set these in Railway environment variables) ────────────────────
TAKE_PROFIT     = float(os.getenv("TAKE_PROFIT_PCT", "0.03"))       # 3%
STOP_LOSS       = float(os.getenv("STOP_LOSS_PCT", "0.05"))         # 5%
MAX_POSITION    = float(os.getenv("MAX_POSITION_SIZE", "3000"))      # $3,000
MIN_CONFIDENCE  = float(os.getenv("MIN_CONFIDENCE", "70"))           # 70%
SOCIAL_WEIGHT   = float(os.getenv("SOCIAL_WEIGHT", "0.40"))         # 40%
DAILY_LOSS_LIM  = float(os.getenv("DAILY_LOSS_LIMIT", "2500"))      # $2,500
EOD_HOUR        = int(os.getenv("EOD_CLOSE_HOUR", "15"))            # 3 PM ET
EOD_MIN         = int(os.getenv("EOD_CLOSE_MIN", "50"))             # 3:50 PM ET
SCAN_INTERVAL   = int(os.getenv("SCAN_INTERVAL_SECS", "30"))        # 30 seconds
MAX_POSITIONS   = int(os.getenv("MAX_CONCURRENT_POSITIONS", "8"))   # 8 max open
WEBHOOK_URL     = os.getenv("CLAUDE_WEBHOOK_URL", "")               # optional

# FREE Push Alert config (ntfy.sh - no signup, no app passwords)
NTFY_TOPIC      = os.getenv("NTFY_TOPIC", "")  # e.g. martin-trading-alerts-8821
NTFY_SERVER     = os.getenv("NTFY_SERVER", "https://ntfy.sh")

ET = pytz.timezone("America/New_York")

# ── STATE ──────────────────────────────────────────────────────────────────
positions        = {}   # {ticker: {shares, entry_price, sector, source, ...}}
daily_realized   = 0.0
total_trades     = 0
scan_count       = 0
signals_fired    = 0
discovered       = set()
wins             = 0
losses           = 0
_last_day        = None  # tracks trading day for daily P&L reset

def log(msg, level="INFO"):
    now = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET")
    prefix = {"INFO": "ℹ️ ", "BUY": "🟢", "SELL": "🔴", "TP": "💰",
              "SL": "🛑", "WARN": "⚠️ ", "ERROR": "❌", "SCAN": "📡",
              "SOCIAL": "📱", "INSIDER": "🏛️", "NEWS": "📰"}.get(level, "  ")
    print(f"[{now}] {prefix} {msg}", flush=True)


# ── MARKET HOURS ──────────────────────────────────────────────────────────
def market_is_open():
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return False  # Weekend
    if now.hour < 9 or (now.hour == 9 and now.minute < 30):
        return False  # Pre-market
    if now.hour > 16 or (now.hour == 16 and now.minute > 0):
        return False  # After hours
    return True

def is_eod():
    now = datetime.now(ET)
    return now.hour == EOD_HOUR and now.minute >= EOD_MIN


# ── SOCIAL SIGNAL SOURCES ─────────────────────────────────────────────────

def scan_reddit():
    """Scan WallStreetBets, stocks, investing, smallcaps subreddits."""
    signals = []
    subreddits = [
        "wallstreetbets", "stocks", "investing",
        "smallcaps", "SecurityAnalysis", "options"
    ]
    headers = {"User-Agent": "AgenticTrader/3.1 (trading signal scanner)"}

    for sub in subreddits:
        try:
            url = f"https://www.reddit.com/r/{sub}/hot.json?limit=15"
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code != 200:
                continue
            data = r.json()
            for post in data.get("data", {}).get("children", []):
                p = post["data"]
                title = p.get("title", "")
                score = p.get("score", 0)
                ratio = p.get("upvote_ratio", 0)
                comments = p.get("num_comments", 0)

                # Only high-conviction posts
                if score < 200 or ratio < 0.80:
                    continue

                # Extract tickers (words starting with $)
                import re
                tickers = re.findall(r'\$([A-Z]{2,5})', title)

                # Calculate confidence from engagement
                conf = min(95, 50 + (score // 100) + (comments // 20))

                for ticker in tickers:
                    signals.append({
                        "ticker": ticker,
                        "source": f"r/{sub}",
                        "platform": "reddit",
                        "headline": title[:120],
                        "confidence": conf,
                        "action": "BUY" if ratio > 0.85 else "WATCH",
                        "score": score,
                    })

            time.sleep(1)  # Be polite to Reddit API

        except Exception as e:
            log(f"Reddit r/{sub} error: {e}", "ERROR")

    log(f"Reddit scan: {len(signals)} signals found", "SOCIAL")
    return signals


def scan_stocktwits(ticker):
    """Get StockTwits bull/bear sentiment for a specific ticker."""
    try:
        url = f"https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json"
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        messages = data.get("messages", [])
        if not messages:
            return None

        bulls = sum(
            1 for m in messages
            if m.get("entities", {}).get("sentiment", {}).get("basic") == "Bullish"
        )
        bears = sum(
            1 for m in messages
            if m.get("entities", {}).get("sentiment", {}).get("basic") == "Bearish"
        )
        total = len(messages)
        bull_pct = (bulls / total * 100) if total > 0 else 50

        return {
            "ticker": ticker,
            "platform": "stocktwits",
            "bull_pct": bull_pct,
            "bear_pct": (bears / total * 100) if total > 0 else 50,
            "message_count": total,
            "confidence": min(95, int(bull_pct)),
            "action": "BUY" if bull_pct > 70 else "SELL" if bull_pct < 30 else "WATCH",
        }
    except Exception as e:
        log(f"StockTwits {ticker} error: {e}", "ERROR")
        return None


def scan_sec_stock_act():
    """Fetch recent Congressional STOCK Act trade disclosures (House)."""
    signals = []
    try:
        url = "https://house-stock-watcher-data.s3-us-east-2.amazonaws.com/data/all_transactions.json"
        r = requests.get(url, timeout=15)
        if r.status_code != 200:
            return signals

        data = r.json()
        cutoff = datetime.now(ET) - timedelta(days=3)

        for trade in data[:200]:
            try:
                ticker = trade.get("ticker", "").strip().upper()
                t_type = trade.get("type", "").lower()
                amount = trade.get("amount", "")
                member = trade.get("representative", "Unknown")
                date_str = trade.get("transaction_date", "")

                if not ticker or ticker in ("--", "N/A"):
                    continue
                if t_type not in ("purchase", "sale"):
                    continue

                # Confidence higher for purchases, lower for sales
                conf = 85 if t_type == "purchase" else 60

                signals.append({
                    "ticker": ticker,
                    "source": "STOCK Act",
                    "platform": "stockact",
                    "headline": f"{member} {'bought' if t_type=='purchase' else 'sold'} {amount} of {ticker}",
                    "confidence": conf,
                    "action": "BUY" if t_type == "purchase" else "SELL",
                })
            except:
                continue

        log(f"STOCK Act scan: {len(signals)} recent filings", "INSIDER")

    except Exception as e:
        log(f"STOCK Act scan error: {e}", "ERROR")

    return signals[:10]  # Top 10 most recent


def scan_sec_form4():
    """Scan SEC Form 4 insider filings via OpenInsider."""
    signals = []
    try:
        # OpenInsider latest cluster buys — free, no auth needed
        url = "http://openinsider.com/screener?s=&o=&pl=&ph=&ll=&lh=&fd=1&fdr=&td=0&tdr=&fdlyl=&fdlyh=&daysago=1&xp=1&xs=1&vl=25&vh=&ocl=&och=&sic1=-1&sicl=100&sich=9999&grp=0&nfl=&nfh=&nil=&nih=&nol=&noh=&v2l=&v2h=&oc2l=&oc2h=&sortcol=0&cnt=20&page=1"
        r = requests.get(url, timeout=15, headers={"User-Agent": "AgenticTrader/3.1"})

        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.text, "html.parser")
        table = soup.find("table", {"class": "tinytable"})
        if not table:
            return signals

        rows = table.find_all("tr")[1:]  # Skip header
        for row in rows[:15]:
            cols = row.find_all("td")
            if len(cols) < 8:
                continue
            try:
                ticker = cols[3].get_text(strip=True).upper()
                insider = cols[5].get_text(strip=True)
                trade_type = cols[6].get_text(strip=True)
                value = cols[9].get_text(strip=True) if len(cols) > 9 else "Unknown"

                if "P" in trade_type:  # Purchase
                    signals.append({
                        "ticker": ticker,
                        "source": "SEC Form 4",
                        "platform": "form4",
                        "headline": f"Insider BUY: {insider} purchased {value} of {ticker}",
                        "confidence": 88,
                        "action": "BUY",
                    })
            except:
                continue

        log(f"Form 4 scan: {len(signals)} insider purchases found", "INSIDER")

    except Exception as e:
        log(f"Form 4 scan error: {e}", "ERROR")

    return signals


# ── PRICE FEED & P&L ───────────────────────────────────────────────────────

def get_price(ticker):
    """Last trade price via Yahoo's unofficial chart endpoint.
    Returns a float, or None if unavailable. Unofficial API — can break."""
    try:
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/"
               f"{ticker}?interval=1m&range=1d")
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        if r.status_code != 200:
            return None
        meta = r.json()["chart"]["result"][0]["meta"]
        price = meta.get("regularMarketPrice")
        return float(price) if price else None
    except Exception as e:
        log(f"Price fetch {ticker} error: {e}", "ERROR")
        return None


def reset_daily_pnl():
    """Reset realized P&L at the start of a trading day."""
    global daily_realized, _last_day
    daily_realized = 0.0
    _last_day = datetime.now(ET).date()
    log("Daily realized P&L reset to $0", "INFO")


def monitor_positions():
    """Check every open position against take-profit and stop-loss."""
    for ticker in list(positions.keys()):
        pos = positions[ticker]
        entry = pos.get("entry_price")
        if not entry:
            continue
        price = get_price(ticker)
        if not price:
            continue
        change = (price - entry) / entry   # e.g. +0.034 = +3.4%
        if change >= TAKE_PROFIT:
            execute_sell(ticker, f"Take-profit +{change*100:.1f}%")
        elif change <= -STOP_LOSS:
            execute_sell(ticker, f"Stop-loss {change*100:.1f}%")


# ── SIGNAL PROCESSING ─────────────────────────────────────────────────────

def blend_confidence(fundamental_conf, ticker):
    """Blend fundamental signal confidence with social sentiment."""
    # Get StockTwits sentiment for this ticker
    st = scan_stocktwits(ticker)
    social_conf = st["confidence"] if st else 50

    blended = (fundamental_conf * (1 - SOCIAL_WEIGHT)) + (social_conf * SOCIAL_WEIGHT)
    return round(blended, 1), social_conf


def process_signal(sig):
    """Evaluate a signal and decide whether to trade."""
    global signals_fired

    ticker  = sig.get("ticker", "").upper()
    action  = sig.get("action", "WATCH")
    raw_conf = sig.get("confidence", 50)
    source  = sig.get("source", "Unknown")
    headline = sig.get("headline", "")

    # Skip if already holding
    if ticker in positions and action == "BUY":
        return

    # Skip if at max positions
    if len(positions) >= MAX_POSITIONS and action == "BUY":
        log(f"Max positions reached ({MAX_POSITIONS}) — skipping {ticker}", "WARN")
        return

    # Blend with social sentiment
    blended, social = blend_confidence(raw_conf, ticker)

    log(f"Signal: {ticker} | {action} | Fund:{raw_conf}% Social:{social}% Blended:{blended}% | {source}", "SCAN")

    if action == "BUY" and blended >= MIN_CONFIDENCE:
        signals_fired += 1
        execute_buy(ticker, sig.get("sector", "Unknown"), source, headline, blended)

    elif action == "SELL" and ticker in positions:
        signals_fired += 1
        execute_sell(ticker, f"Signal sell: {source}")

    # Track discovered tickers
    if ticker not in discovered:
        discovered.add(ticker)
        log(f"NEW TICKER DISCOVERED: {ticker} via {source}", "SCAN")


# ── TRADE EXECUTION ───────────────────────────────────────────────────────

def execute_buy(ticker, sector, source, reason, confidence):
    """Place a buy order via Robinhood Agentic (alert-only here)."""
    global positions

    # HALT new buys if the daily loss limit is already breached
    if daily_realized <= -DAILY_LOSS_LIM:
        log(f"Daily loss limit reached (${daily_realized:.0f}) — skipping {ticker}", "WARN")
        return

    price = get_price(ticker)
    if not price or price <= 0:
        log(f"Skip BUY {ticker}: no live price available", "WARN")
        return

    shares = MAX_POSITION / price

    log(f"BUY {ticker} | ${MAX_POSITION:.0f} @ ~${price:.2f} | Conf:{confidence}% | {source}", "BUY")
    log(f"  Reason: {reason[:80]}", "BUY")

    # Notify Claude webhook (if configured) to execute via Robinhood MCP
    notify_claude("BUY", ticker, MAX_POSITION, reason, confidence)

    # Send push alert so you can confirm execution in Claude chat
    sms_msg = (
        f"Amount: ${MAX_POSITION:.0f} (~{shares:.2f} sh @ ${price:.2f})\n"
        f"Confidence: {confidence}%\n"
        f"Source: {source}\n"
        f"Reason: {reason[:80]}\n\n"
        f'Reply to Claude: "buy {ticker} ${MAX_POSITION:.0f}"'
    )
    send_sms_alert(sms_msg, title=f"\U0001F7E2 BUY SIGNAL: {ticker}", priority="high")

    positions[ticker] = {
        "ticker": ticker,
        "sector": sector,
        "source": source,
        "confidence": confidence,
        "entry_time": datetime.now(ET).isoformat(),
        "entry_price": price,
        "shares": shares,
        "notional": MAX_POSITION,
    }


def execute_sell(ticker, reason):
    """Close a position and record estimated realized P&L."""
    global positions, daily_realized, total_trades, wins, losses

    if ticker not in positions:
        return

    pos = positions[ticker]
    exit_price = get_price(ticker)
    pnl = None

    if exit_price and pos.get("entry_price"):
        pnl = (exit_price - pos["entry_price"]) * pos["shares"]
        daily_realized += pnl
        total_trades += 1
        if pnl >= 0:
            wins += 1
        else:
            losses += 1

    pnl_str = f"{pnl:+.2f}" if pnl is not None else "unknown"
    log(f"SELL {ticker} | est P&L ${pnl_str} | {reason}", "SELL")
    log(f"  Daily realized: ${daily_realized:+.2f}", "INFO")

    notify_claude("SELL", ticker, 0, reason, 0)

    sms_msg = (
        f"Est P&L: ${pnl_str}\n"
        f"Reason: {reason}\n\n"
        f'Reply to Claude: "sell {ticker}"'
    )
    send_sms_alert(sms_msg, title=f"\U0001F534 SELL SIGNAL: {ticker}", priority="high")

    del positions[ticker]


def close_all_positions(reason="EOD auto-close"):
    """Close every open position."""
    if not positions:
        log("No positions to close", "INFO")
        return

    log(f"Closing all {len(positions)} positions — {reason}", "WARN")
    for ticker in list(positions.keys()):
        execute_sell(ticker, reason)


# ── CLAUDE WEBHOOK ────────────────────────────────────────────────────────

def send_sms_alert(message, title="Trading Agent Alert", priority="default"):
    """Send a free push notification via ntfy.sh - no signup, no app password."""
    if not NTFY_TOPIC:
        log("Push alerts not configured \u2014 set NTFY_TOPIC env var", "WARN")
        return
    try:
        url = f"{NTFY_SERVER}/{NTFY_TOPIC}"
        resp = requests.post(
            url,
            data=message.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": priority,   # "default", "high", or "urgent"
                "Tags": "chart_with_upwards_trend",
            },
            timeout=10,
        )
        if resp.status_code == 200:
            log("Push alert sent (free, via ntfy.sh) \u2705", "INFO")
        else:
            log(f"Push send failed: {resp.status_code}", "ERROR")
    except Exception as e:
        log(f"Push send error: {e}", "ERROR")


def notify_claude(action, ticker, amount_usd, reason, confidence):
    """
    Send trade instruction to Claude via webhook.
    Claude executes the actual Robinhood MCP trade.

    Set CLAUDE_WEBHOOK_URL in Railway environment variables.
    Leave blank to just log the trade (manual execution mode).
    """
    if not WEBHOOK_URL:
        log(f"[MANUAL MODE] Would {action} {ticker} ${amount_usd:.0f} — set CLAUDE_WEBHOOK_URL to automate", "WARN")
        return

    payload = {
        "action": action,
        "ticker": ticker,
        "amount_usd": amount_usd,
        "reason": reason,
        "confidence": confidence,
        "timestamp": datetime.now(ET).isoformat(),
        "agent": "AgenticTrader/3.1",
    }
    try:
        r = requests.post(WEBHOOK_URL, json=payload, timeout=10)
        if r.status_code == 200:
            log(f"Claude notified: {action} {ticker} ✅", "INFO")
        else:
            log(f"Claude webhook error: {r.status_code}", "ERROR")
    except Exception as e:
        log(f"Claude webhook failed: {e}", "ERROR")


# ── MAIN SCAN CYCLE ───────────────────────────────────────────────────────

def run_scan():
    """Full market scan — runs every 30 seconds."""
    global scan_count
    scan_count += 1

    now_str = datetime.now(ET).strftime("%H:%M:%S")

    if not market_is_open():
        if scan_count % 20 == 1:  # Log every ~10 mins after hours
            log(f"Market closed ({now_str} ET) — watching social signals only", "INFO")
        return

    # Reset realized P&L on a new trading day
    if _last_day != datetime.now(ET).date():
        reset_daily_pnl()

    log(f"── Scan #{scan_count} | {now_str} ET | Positions:{len(positions)}/{MAX_POSITIONS} | P&L:${daily_realized:+.0f} ──", "SCAN")

    # EOD check — close all positions before market close
    if is_eod():
        close_all_positions("3:50 PM ET auto-close")
        return

    # Enforce take-profit / stop-loss on everything we hold, every cycle
    monitor_positions()

    # Stop opening new trades once the daily loss limit is hit
    if daily_realized <= -DAILY_LOSS_LIM:
        if scan_count % 10 == 1:
            log(f"Daily loss limit hit (${daily_realized:.0f}) — monitoring only, no new buys", "WARN")
        return

    all_signals = []

    # 1. Reddit scan (every scan)
    reddit_sigs = scan_reddit()
    all_signals.extend(reddit_sigs)

    # 2. STOCK Act (every 5 scans = ~2.5 min)
    if scan_count % 5 == 0:
        stock_act_sigs = scan_sec_stock_act()
        all_signals.extend(stock_act_sigs)

    # 3. Form 4 insider trades (every 10 scans = ~5 min)
    if scan_count % 10 == 0:
        form4_sigs = scan_sec_form4()
        all_signals.extend(form4_sigs)

    # 4. Process all signals
    seen = set()
    for sig in all_signals:
        ticker = sig.get("ticker", "").upper()
        # Deduplicate same ticker in one cycle
        key = f"{ticker}:{sig.get('action')}"
        if key in seen or not ticker or len(ticker) > 5:
            continue
        seen.add(key)
        process_signal(sig)

    log(f"Scan #{scan_count} complete | {len(all_signals)} signals | {len(discovered)} tickers discovered", "SCAN")


def print_status():
    """Print agent status every 5 minutes."""
    closed = wins + losses
    win_rate = (wins / closed * 100) if closed else 0.0
    log("──────────────────────────────────────────────", "INFO")
    log(f"Status Report @ {datetime.now(ET).strftime('%H:%M ET')}", "INFO")
    log(f"  Open positions   : {len(positions)}", "INFO")
    log(f"  Closed trades    : {closed} ({wins}W / {losses}L)", "INFO")
    log(f"  Win rate (est)   : {win_rate:.1f}%", "INFO")
    log(f"  Realized P&L     : ${daily_realized:+.2f} (today, est)", "INFO")
    log(f"  Tickers found    : {len(discovered)}", "INFO")
    log(f"  Signals fired    : {signals_fired}", "INFO")
    log(f"  Scans run        : {scan_count}", "INFO")
    log(f"  Market open      : {market_is_open()}", "INFO")
    if positions:
        log(f"  Holdings         : {', '.join(positions.keys())}", "INFO")
    log("──────────────────────────────────────────────", "INFO")


# ── ENTRY POINT ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    log("🚀 Agentic Market Scanner v3.1 starting...", "INFO")
    log(f"  Take profit    : +{TAKE_PROFIT*100:.1f}%", "INFO")
    log(f"  Stop loss      : -{STOP_LOSS*100:.1f}%", "INFO")
    log(f"  Max position   : ${MAX_POSITION:,.0f}", "INFO")
    log(f"  Min confidence : {MIN_CONFIDENCE}%", "INFO")
    log(f"  Social weight  : {SOCIAL_WEIGHT*100:.0f}%", "INFO")
    log(f"  Max positions  : {MAX_POSITIONS}", "INFO")
    log(f"  Daily loss lim : -${DAILY_LOSS_LIM:,.0f}", "INFO")
    log(f"  EOD close      : {EOD_HOUR}:{EOD_MIN:02d} ET", "INFO")
    log(f"  Scan interval  : {SCAN_INTERVAL}s", "INFO")
    log(f"  Claude webhook : {'SET ✅' if WEBHOOK_URL else 'NOT SET — manual mode'}", "INFO")
    log("📡 Sources: Reddit · StockTwits · STOCK Act · SEC Form 4", "INFO")
    log("🌍 Scanning ENTIRE US equity market — no ticker restrictions", "INFO")
    log("", "INFO")

    # Initialize the trading day so P&L tracking starts clean
    reset_daily_pnl()

    # Run immediately on startup
    run_scan()

    # Schedule recurring scan
    schedule.every(SCAN_INTERVAL).seconds.do(run_scan)
    schedule.every(5).minutes.do(print_status)
    schedule.every().day.at("09:30").do(reset_daily_pnl)
    schedule.every().day.at("15:50").do(lambda: close_all_positions("Scheduled EOD"))
    schedule.every().day.at("09:31").do(lambda: log("🔔 Market OPEN — agent active", "INFO"))
    schedule.every().day.at("16:01").do(lambda: log("🔕 Market CLOSED — monitoring only", "INFO"))

    # Main loop — runs forever on Railway
    while True:
        schedule.run_pending()
        time.sleep(1)
