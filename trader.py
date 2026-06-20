"""
Agentic Trading Agent v4.0
==========================
Scans Reddit, StockTwits, STOCK Act, and SEC Form 4 for signals, then applies
a measured strategy framework:

  - Trailing-stop exits (let winners run, cut losers) instead of the old,
    math-hostile +3% / -5% fixed targets.
  - Liquidity filter so it only trades names where costs don't eat the edge.
  - Risk-based position sizing (risk a fixed % of equity), not flat $3,000.
  - No fabricated confidence: if data is missing, it skips rather than guessing.

It runs in PAPER mode by default: it logs HYPOTHETICAL fills with realistic
costs and prints a performance scorecard, risking $0. Switch to LIVE only after
the paper track record shows a real edge.

  TRADING_MODE = PAPER   -> simulate + score, no real money, no alerts to act on
  TRADING_MODE = LIVE    -> alert you to confirm each trade in chat (you execute)

Shares engine + scorecard are imported from strategy_lab.py (same package).

Not financial advice. You are responsible for every trade you approve.
"""

import time
import json
import os
import schedule
import requests
import pytz
from datetime import datetime, timedelta

import strategy_lab as lab   # CostModel, RiskConfig, fetch_history, report, Trade

# -- MODE --
TRADING_MODE = os.getenv("TRADING_MODE", "PAPER").upper()   # PAPER | LIVE

# -- STRATEGY CONFIG (set in Railway env vars) --
INIT_STOP_PCT   = float(os.getenv("STOP_LOSS_PCT", "0.05"))        # initial stop
TRAIL_PCT       = float(os.getenv("TRAIL_PCT", "0.06"))            # trailing stop
_tp             = os.getenv("TAKE_PROFIT_PCT", "")                  # blank = trail only
TAKE_PROFIT_PCT = float(_tp) if _tp.strip() else None
RISK_PER_TRADE  = float(os.getenv("RISK_PER_TRADE_PCT", "0.01"))   # risk 1% / trade
MAX_POSITION    = float(os.getenv("MAX_POSITION_SIZE", "3000"))     # $ cap per position
START_EQUITY    = float(os.getenv("START_EQUITY", "50000"))         # paper sizing base
MIN_CONFIDENCE  = float(os.getenv("MIN_CONFIDENCE", "70"))
SOCIAL_WEIGHT   = float(os.getenv("SOCIAL_WEIGHT", "0.40"))
DAILY_LOSS_LIM  = float(os.getenv("DAILY_LOSS_LIMIT", "2500"))
MAX_POSITIONS   = int(os.getenv("MAX_CONCURRENT_POSITIONS", "8"))
SCAN_INTERVAL   = int(os.getenv("SCAN_INTERVAL_SECS", "30"))
# Liquidity filter
MIN_PRICE       = float(os.getenv("MIN_PRICE", "5"))
MIN_DOLLAR_VOL  = float(os.getenv("MIN_DOLLAR_VOL", "5000000"))
# Plumbing
EOD_HOUR        = int(os.getenv("EOD_CLOSE_HOUR", "15"))
EOD_MIN         = int(os.getenv("EOD_CLOSE_MIN", "50"))
WEBHOOK_URL     = os.getenv("CLAUDE_WEBHOOK_URL", "")
NTFY_TOPIC      = os.getenv("NTFY_TOPIC", "")
NTFY_SERVER     = os.getenv("NTFY_SERVER", "https://ntfy.sh")
PAPER_LOG       = os.getenv("PAPER_LOG", "paper_trades.jsonl")

ET = pytz.timezone("America/New_York")
COST = lab.CostModel()   # spread + slippage applied to every fill

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
    now = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET")
    tag = {"INFO": "[i]", "BUY": "[BUY]", "SELL": "[SELL]", "WARN": "[!]",
           "ERROR": "[X]", "SCAN": "[scan]", "SOCIAL": "[soc]",
           "INSIDER": "[ins]", "PAPER": "[paper]"}.get(level, "   ")
    print(f"[{now}] {tag} {msg}", flush=True)


# -- MARKET HOURS --
def market_is_open():
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return False
    if now.hour < 9 or (now.hour == 9 and now.minute < 30):
        return False
    if now.hour > 16 or (now.hour == 16 and now.minute > 0):
        return False
    return True

def is_eod():
    now = datetime.now(ET)
    return now.hour == EOD_HOUR and now.minute >= EOD_MIN


# -- SIGNAL SOURCES --
def scan_reddit():
    signals = []
    subreddits = ["wallstreetbets", "stocks", "investing", "smallcaps",
                  "SecurityAnalysis", "options"]
    headers = {"User-Agent": "AgenticTrader/4.0 (signal scanner)"}
    for sub in subreddits:
        try:
            url = f"https://www.reddit.com/r/{sub}/hot.json?limit=15"
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code != 200:
                continue
            for post in r.json().get("data", {}).get("children", []):
                p = post["data"]
                title, score = p.get("title", ""), p.get("score", 0)
                ratio, comments = p.get("upvote_ratio", 0), p.get("num_comments", 0)
                if score < 200 or ratio < 0.80:
                    continue
                import re
                for ticker in re.findall(r'\$([A-Z]{2,5})', title):
                    conf = min(95, 50 + (score // 100) + (comments // 20))
                    signals.append({"ticker": ticker, "source": f"r/{sub}",
                                    "headline": title[:120], "confidence": conf,
                                    "action": "BUY" if ratio > 0.85 else "WATCH"})
            time.sleep(1)
        except Exception as e:
            log(f"Reddit r/{sub} error: {e}", "ERROR")
    log(f"Reddit scan: {len(signals)} signals", "SOCIAL")
    return signals


def scan_stocktwits(ticker):
    try:
        url = f"https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json"
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            return None
        messages = r.json().get("messages", [])
        if not messages:
            return None
        bulls = sum(1 for m in messages
                    if m.get("entities", {}).get("sentiment", {}).get("basic") == "Bullish")
        total = len(messages)
        bull_pct = bulls / total * 100
        return {"ticker": ticker, "bull_pct": bull_pct,
                "confidence": min(95, int(bull_pct)),
                "action": "BUY" if bull_pct > 70 else "SELL" if bull_pct < 30 else "WATCH"}
    except Exception as e:
        log(f"StockTwits {ticker} error: {e}", "ERROR")
        return None


def scan_sec_stock_act():
    signals = []
    try:
        url = "https://house-stock-watcher-data.s3-us-east-2.amazonaws.com/data/all_transactions.json"
        r = requests.get(url, timeout=15)
        if r.status_code != 200:
            return signals
        for trade in r.json()[:200]:
            try:
                ticker = trade.get("ticker", "").strip().upper()
                t_type = trade.get("type", "").lower()
                if not ticker or ticker in ("--", "N/A") or t_type not in ("purchase", "sale"):
                    continue
                signals.append({"ticker": ticker, "source": "STOCK Act",
                                "headline": f"{trade.get('representative','?')} {t_type} {ticker}",
                                "confidence": 85 if t_type == "purchase" else 60,
                                "action": "BUY" if t_type == "purchase" else "SELL"})
            except:
                continue
        log(f"STOCK Act scan: {len(signals)} filings", "INSIDER")
    except Exception as e:
        log(f"STOCK Act error: {e}", "ERROR")
    return signals[:10]


def scan_sec_form4():
    signals = []
    try:
        url = ("http://openinsider.com/screener?s=&o=&pl=&ph=&ll=&lh=&fd=1&fdr=&td=0"
               "&tdr=&daysago=1&xp=1&xs=1&vl=25&cnt=20&page=1&sortcol=0")
        r = requests.get(url, timeout=15, headers={"User-Agent": "AgenticTrader/4.0"})
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.text, "html.parser")
        table = soup.find("table", {"class": "tinytable"})
        if not table:
            return signals
        for row in table.find_all("tr")[1:16]:
            cols = row.find_all("td")
            if len(cols) < 8:
                continue
            try:
                ticker = cols[3].get_text(strip=True).upper()
                if "P" in cols[6].get_text(strip=True):
                    signals.append({"ticker": ticker, "source": "SEC Form 4",
                                    "headline": f"Insider buy {ticker}",
                                    "confidence": 88, "action": "BUY"})
            except:
                continue
        log(f"Form 4 scan: {len(signals)} insider buys", "INSIDER")
    except Exception as e:
        log(f"Form 4 error: {e}", "ERROR")
    return signals


# -- PRICE & LIQUIDITY --
def get_price(ticker):
    try:
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/"
               f"{ticker}?interval=1m&range=1d")
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        if r.status_code != 200:
            return None
        price = r.json()["chart"]["result"][0]["meta"].get("regularMarketPrice")
        return float(price) if price else None
    except Exception:
        return None


def check_liquidity(ticker):
    """Return (ok, price, avg_dollar_vol). Uses real history; no data => not ok."""
    bars = lab.fetch_history(ticker, 30)
    if len(bars) < 5:
        return False, None, 0.0
    price = bars[-1]["c"]
    window = bars[-20:]
    advol = sum(b["c"] * b["v"] for b in window) / len(window)
    return (price >= MIN_PRICE and advol >= MIN_DOLLAR_VOL), price, advol


def size_position(entry, init_stop, eq):
    """Fixed-fractional risk sizing, capped at MAX_POSITION and no-leverage."""
    per_share_risk = entry - init_stop
    if per_share_risk <= 0:
        return 0.0
    shares = (eq * RISK_PER_TRADE) / per_share_risk
    return max(0.0, min(shares, MAX_POSITION / entry, eq / entry))


# -- SIGNAL PROCESSING --
def blend_confidence(fundamental_conf, ticker):
    """Blend fundamental + social. If social data is MISSING, do not fabricate
    a neutral 50 -- drop the social term and flag lower data quality."""
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
        "entry_raw": price,
        "entry_fill": COST.buy_fill(price),
        "init_stop": init_stop,
        "high": price,
        "shares": shares,
        "source": source,
        "confidence": confidence,
        "social_ok": social_ok,
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
    trade = lab.Trade(ticker=ticker,
                      entry_date=pos["entry_time"].strftime("%Y-%m-%d %H:%M"),
                      exit_date=datetime.now(ET).strftime("%Y-%m-%d %H:%M"),
                      entry=round(pos["entry_fill"], 4), exit=round(exit_fill, 4),
                      shares=round(pos["shares"], 3), ret_pct=ret, pnl=pnl,
                      reason=reason, held_days=held)
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
        requests.post(f"{NTFY_SERVER}/{NTFY_TOPIC}", data=message.encode("utf-8"),
                      headers={"Title": title, "Priority": priority,
                               "Tags": "chart_with_upwards_trend"}, timeout=10)
    except Exception as e:
        log(f"Alert error: {e}", "ERROR")


def notify_claude(action, ticker, amount_usd, reason, confidence):
    if not WEBHOOK_URL:
        log(f"[no webhook] would {action} {ticker} ${amount_usd:.0f}", "WARN")
        return
    try:
        requests.post(WEBHOOK_URL, json={"action": action, "ticker": ticker,
                      "amount_usd": amount_usd, "reason": reason,
                      "confidence": confidence,
                      "timestamp": datetime.now(ET).isoformat()}, timeout=10)
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
    log("Agentic Market Scanner v4.0 starting...", "INFO")
    log(f"  MODE           : {TRADING_MODE}", "INFO")
    log(f"  Initial stop   : -{INIT_STOP_PCT*100:.1f}%  |  Trailing: -{TRAIL_PCT*100:.1f}%", "INFO")
    log(f"  Take-profit    : {'trailing only' if TAKE_PROFIT_PCT is None else f'+{TAKE_PROFIT_PCT*100:.1f}%'}", "INFO")
    log(f"  Risk/trade     : {RISK_PER_TRADE*100:.1f}% of equity (cap ${MAX_POSITION:,.0f})", "INFO")
    log(f"  Liquidity gate : price>=${MIN_PRICE:.0f}, avg $vol>=${MIN_DOLLAR_VOL:,.0f}", "INFO")
    log(f"  Min confidence : {MIN_CONFIDENCE}%  |  Social weight: {SOCIAL_WEIGHT*100:.0f}%", "INFO")
    log(f"  Daily loss lim : -${DAILY_LOSS_LIM:,.0f}", "INFO")
    if TRADING_MODE == "LIVE":
        log("  LIVE MODE -- real-money trade alerts. Only run this after PAPER", "WARN")
        log("  shows a positive scorecard. You confirm every trade in chat.", "WARN")
    else:
        log("  PAPER MODE -- $0 at risk. Logs hypothetical fills to " + PAPER_LOG, "INFO")
    log("Sources: Reddit, StockTwits, STOCK Act, SEC Form 4", "INFO")

    reset_daily()
    run_scan()
    schedule.every(SCAN_INTERVAL).seconds.do(run_scan)
    schedule.every(5).minutes.do(print_status)
    schedule.every().day.at("09:30").do(reset_daily)
    schedule.every().day.at("15:50").do(lambda: close_all_positions("Scheduled EOD"))

    while True:
        schedule.run_pending()
        time.sleep(1)
