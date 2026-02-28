# Stock Alert Monitor

A web app that monitors stock tickers and sends **push notifications to your iPhone** when a stock moves 1% or more. Tickers are automatically monitored for 8 hours and cleared daily.

## Features

- Add/remove stock tickers via a web dashboard
- Real-time price monitoring using Yahoo Finance (free, no API key needed)
- Push notifications to iPhone via [ntfy.sh](https://ntfy.sh) (free)
- 1% move threshold triggers an alert (configurable)
- Each ticker expires after 8 hours; all tickers clear at midnight UTC
- Export monitored tickers to CSV
- Alert history log

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Set up iPhone notifications

1. Install the **ntfy** app from the [App Store](https://apps.apple.com/app/ntfy/id1625396347).
2. Open the app and subscribe to a unique topic name (e.g. `my-stock-alerts-abc123`).
3. Set the same topic when launching the server:

```bash
export NTFY_TOPIC="my-stock-alerts-abc123"
```

### 3. Run the app

```bash
python app.py
```

Open [http://localhost:5000](http://localhost:5000) in your browser.

## Configuration (environment variables)

| Variable             | Default                      | Description                        |
|----------------------|------------------------------|------------------------------------|
| `NTFY_TOPIC`         | `my-stock-alerts-change-me`  | Your ntfy.sh topic name            |
| `ALERT_THRESHOLD_PCT`| `1.0`                        | Price change % to trigger alert    |
| `CHECK_INTERVAL_SEC` | `60`                         | Seconds between price checks       |

## How It Works

1. **Add a ticker** — the app records the current price as the baseline.
2. **Every 60 seconds**, the backend fetches the latest price and computes the % change from the baseline.
3. **If the change hits 1%**, a push notification is sent to your phone via ntfy.sh and the ticker is marked as "alerted".
4. **After 8 hours**, the ticker automatically expires and is removed.
5. **At midnight UTC**, all remaining tickers are cleared for the new day.

## Project Structure

```
app.py                 # Flask backend + scheduler
templates/index.html   # Web dashboard (single-page app)
requirements.txt       # Python dependencies
stock_alerts.db        # SQLite database (auto-created)
```
