"""
Simple HTTP API to dump current trading state as JSON.
Run alongside trader.py in separate threads.
"""
import json
import os
from flask import Flask, jsonify
from datetime import datetime
import pytz

app = Flask(__name__)

# Import trader state (shared globals)
import trader

ET = pytz.timezone("America/New_York")

@app.route("/trades", methods=["GET"])
def get_trades():
    """Return all paper trades as JSON."""
    return jsonify({
        "timestamp": datetime.now(ET).isoformat(),
        "mode": trader.TRADING_MODE,
        "equity": trader.equity,
        "start_equity": trader.START_EQUITY,
        "open_positions": len(trader.positions),
        "max_positions": trader.MAX_POSITIONS,
        "signals_fired": trader.signals_fired,
        "discovered_tickers": list(trader.discovered),
        "daily_realized_pnl": trader.daily_realized,
        "wins": trader.wins,
        "losses": trader.losses,
        "paper_trades": trader.paper_trades,
        "positions": {
            ticker: {
                "entry": pos.get("entry"),
                "shares": pos.get("shares"),
                "init_stop": pos.get("init_stop"),
                "trail_stop": pos.get("trail_stop"),
                "take_profit": pos.get("take_profit"),
                "source": pos.get("source"),
                "opened_at": pos.get("opened_at"),
            }
            for ticker, pos in trader.positions.items()
        }
    })

@app.route("/status", methods=["GET"])
def get_status():
    """Return brief status."""
    closed = trader.wins + trader.losses
    wr = (trader.wins / closed * 100) if closed else 0.0
    return jsonify({
        "timestamp": datetime.now(ET).isoformat(),
        "mode": trader.TRADING_MODE,
        "market_open": trader.market_is_open(),
        "open_positions": len(trader.positions),
        "closed_trades": closed,
        "win_rate": f"{wr:.1f}%",
        "equity": f"${trader.equity:,.0f}",
        "daily_pnl": f"${trader.daily_realized:+.0f}",
        "signals_fired": trader.signals_fired,
    })

if __name__ == "__main__":
    port = int(os.getenv("API_PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)

