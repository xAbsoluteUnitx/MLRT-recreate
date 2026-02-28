import os
import csv
import io
import json
import sqlite3
from datetime import datetime, timedelta

import requests
import yfinance as yf
from flask import Flask, jsonify, render_template, request, Response
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)
DB_PATH = os.path.join(os.path.dirname(__file__), "stock_alerts.db")

# ---------------------------------------------------------------------------
# ntfy.sh configuration
#   1. Install the ntfy app on your iPhone from the App Store.
#   2. Subscribe to a unique topic (e.g. "my-stock-alerts-xyz123").
#   3. Set that same topic name here or via the NTFY_TOPIC env var.
# ---------------------------------------------------------------------------
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "my-stock-alerts-change-me")
NTFY_URL = f"https://ntfy.sh/{NTFY_TOPIC}"
ALERT_THRESHOLD_PCT = float(os.environ.get("ALERT_THRESHOLD_PCT", "1.0"))
CHECK_INTERVAL_SEC = int(os.environ.get("CHECK_INTERVAL_SEC", "60"))


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tickers (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol      TEXT    NOT NULL,
            base_price  REAL,
            last_price  REAL,
            change_pct  REAL    DEFAULT 0,
            added_at    TEXT    NOT NULL,
            expires_at  TEXT    NOT NULL,
            alerted     INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alert_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol      TEXT    NOT NULL,
            base_price  REAL,
            alert_price REAL,
            change_pct  REAL,
            alerted_at  TEXT    NOT NULL
        )
    """)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Push notification via ntfy.sh
# ---------------------------------------------------------------------------
def send_push(title: str, message: str, priority: str = "high"):
    try:
        requests.post(
            NTFY_URL,
            data=message.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": priority,
                "Tags": "chart_with_upwards_trend",
            },
            timeout=10,
        )
    except Exception as e:
        print(f"[ntfy] Failed to send notification: {e}")


# ---------------------------------------------------------------------------
# Price fetching
# ---------------------------------------------------------------------------
def fetch_price(symbol: str):
    """Return the latest price for *symbol* using yfinance."""
    try:
        ticker = yf.Ticker(symbol)
        data = ticker.history(period="1d", interval="1m")
        if data.empty:
            return None
        return round(float(data["Close"].iloc[-1]), 4)
    except Exception as e:
        print(f"[price] Error fetching {symbol}: {e}")
        return None


# ---------------------------------------------------------------------------
# Scheduled jobs
# ---------------------------------------------------------------------------
def check_prices():
    """Runs every CHECK_INTERVAL_SEC seconds. Checks all active tickers."""
    now = datetime.utcnow()
    conn = get_db()

    # Remove expired tickers (older than 8 hours)
    conn.execute("DELETE FROM tickers WHERE expires_at <= ?", (now.isoformat(),))
    conn.commit()

    rows = conn.execute("SELECT * FROM tickers").fetchall()
    for row in rows:
        symbol = row["symbol"]
        base_price = row["base_price"]
        if base_price is None or base_price == 0:
            continue

        price = fetch_price(symbol)
        if price is None:
            continue

        change_pct = round(((price - base_price) / base_price) * 100, 4)

        conn.execute(
            "UPDATE tickers SET last_price = ?, change_pct = ? WHERE id = ?",
            (price, change_pct, row["id"]),
        )

        if abs(change_pct) >= ALERT_THRESHOLD_PCT and not row["alerted"]:
            direction = "UP" if change_pct > 0 else "DOWN"
            title = f"🚨 {symbol} {direction} {abs(change_pct):.2f}%"
            body = (
                f"{symbol} moved {direction} {abs(change_pct):.2f}%\n"
                f"Base: ${base_price:.2f}  →  Now: ${price:.2f}"
            )
            send_push(title, body)

            conn.execute("UPDATE tickers SET alerted = 1 WHERE id = ?", (row["id"],))
            conn.execute(
                "INSERT INTO alert_log (symbol, base_price, alert_price, change_pct, alerted_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (symbol, base_price, price, change_pct, now.isoformat()),
            )

    conn.commit()
    conn.close()


def daily_cleanup():
    """Delete all tickers once per day at midnight UTC."""
    conn = get_db()
    conn.execute("DELETE FROM tickers")
    conn.commit()
    conn.close()
    print("[cleanup] All tickers cleared for the new day.")


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html", ntfy_topic=NTFY_TOPIC)


@app.route("/api/tickers", methods=["GET"])
def list_tickers():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM tickers ORDER BY added_at DESC"
    ).fetchall()
    conn.close()
    tickers = [dict(r) for r in rows]
    return jsonify(tickers)


@app.route("/api/tickers", methods=["POST"])
def add_ticker():
    data = request.get_json(force=True)
    symbol = data.get("symbol", "").strip().upper()
    if not symbol:
        return jsonify({"error": "Symbol is required"}), 400

    # Check for duplicates
    conn = get_db()
    existing = conn.execute(
        "SELECT id FROM tickers WHERE symbol = ?", (symbol,)
    ).fetchone()
    if existing:
        conn.close()
        return jsonify({"error": f"{symbol} is already being monitored"}), 409

    price = fetch_price(symbol)
    if price is None:
        conn.close()
        return jsonify({"error": f"Could not fetch price for {symbol}. Invalid ticker?"}), 400

    now = datetime.utcnow()
    expires = now + timedelta(hours=8)

    conn.execute(
        "INSERT INTO tickers (symbol, base_price, last_price, change_pct, added_at, expires_at) "
        "VALUES (?, ?, ?, 0, ?, ?)",
        (symbol, price, price, now.isoformat(), expires.isoformat()),
    )
    conn.commit()
    conn.close()

    return jsonify({"symbol": symbol, "base_price": price, "expires_at": expires.isoformat()}), 201


@app.route("/api/tickers/<symbol>", methods=["DELETE"])
def delete_ticker(symbol):
    symbol = symbol.upper()
    conn = get_db()
    conn.execute("DELETE FROM tickers WHERE symbol = ?", (symbol,))
    conn.commit()
    conn.close()
    return jsonify({"deleted": symbol})


@app.route("/api/tickers/clear", methods=["POST"])
def clear_tickers():
    conn = get_db()
    conn.execute("DELETE FROM tickers")
    conn.commit()
    conn.close()
    return jsonify({"status": "all tickers cleared"})


@app.route("/api/export")
def export_csv():
    conn = get_db()
    rows = conn.execute("SELECT * FROM tickers ORDER BY symbol").fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Symbol", "Base Price", "Last Price", "Change %", "Added (UTC)", "Expires (UTC)", "Alerted"])
    for r in rows:
        writer.writerow([
            r["symbol"],
            r["base_price"],
            r["last_price"],
            r["change_pct"],
            r["added_at"],
            r["expires_at"],
            "Yes" if r["alerted"] else "No",
        ])

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=stock_alerts.csv"},
    )


@app.route("/api/alerts")
def alert_history():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM alert_log ORDER BY alerted_at DESC LIMIT 50"
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


# ---------------------------------------------------------------------------
# App startup
# ---------------------------------------------------------------------------
init_db()

scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(check_prices, "interval", seconds=CHECK_INTERVAL_SEC, id="price_checker")
scheduler.add_job(daily_cleanup, "cron", hour=0, minute=0, id="daily_cleanup")
scheduler.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
