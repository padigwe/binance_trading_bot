import os
import json
import ssl
import smtplib
from email.mime.text import MIMEText
from flask import Flask, request, jsonify
from binance.client import Client
from binance.enums import (
    SIDE_BUY, SIDE_SELL, ORDER_TYPE_MARKET, ORDER_TYPE_STOP_MARKET,
    POSITION_SIDE_LONG, POSITION_SIDE_SHORT
)
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import date
from zoneinfo import ZoneInfo

# CONFIGURATION FROM ENVIRONMENT
API_KEY           = os.getenv("BINANCE_API_KEY")
API_SECRET        = os.getenv("BINANCE_API_SECRET")
WEBHOOK_PASSPHRASE = os.getenv("WEBHOOK_PASSPHRASE", "your_webhook_passphrase")
SYMBOL            = os.getenv("SYMBOL", "SOLUSDT.P")
LEVERAGE          = int(os.getenv("LEVERAGE", "5"))
STOP_LOSS_PCT     = float(os.getenv("STOP_LOSS_PCT", "0.10"))  # 10% capital loss

# Email settings
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")
EMAIL_FROM = os.getenv("EMAIL_FROM", EMAIL_USER)
EMAIL_TO   = os.getenv("EMAIL_TO", EMAIL_FROM)

# Reporting settings
DAILY_REPORT_TIME = os.getenv("DAILY_REPORT_TIME", "07:00")
REPORT_TZ         = ZoneInfo("Europe/London")
BASELINE_FILE     = os.getenv("BASELINE_FILE_PATH", "baseline.json")

# Initialize Binance Futures client
client = Client(API_KEY, API_SECRET)
client.FUTURES_URL = 'https://fapi.binance.com'
client.futures_change_leverage(symbol=SYMBOL, leverage=LEVERAGE)

app = Flask(__name__)

# State: last SuperTrend signal
last_super_signal = None

# Email helper
def send_email(subject: str, body: str):
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    context = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls(context=context)
        server.login(EMAIL_USER, EMAIL_PASS)
        server.sendmail(EMAIL_FROM, [EMAIL_TO], msg.as_string())

# Baseline helpers
def load_baseline():
    if os.path.exists(BASELINE_FILE):
        with open(BASELINE_FILE) as f:
            data = json.load(f)
            if data.get('date') == date.today().isoformat():
                return float(data.get('balance', 0))
    bal = float(client.futures_account_balance()[0]['balance'])
    with open(BASELINE_FILE, 'w') as f:
        json.dump({'date': date.today().isoformat(), 'balance': bal}, f)
    return bal


def update_baseline(new_balance: float):
    with open(BASELINE_FILE, 'w') as f:
        json.dump({'date': date.today().isoformat(), 'balance': new_balance}, f)

# Daily P&L report
def daily_report():
    try:
        curr = float(client.futures_account_balance()[0]['balance'])
        base = load_baseline()
        pnl = curr - base
        subject = f"Daily P&L Report - {date.today()}"
        body = (
            f"Date: {date.today()}\n"
            f"Start: {base}\n"
            f"Current: {curr}\n"
            f"P&L: {pnl:+.8f}\n"
        )
        send_email(subject, body)
        update_baseline(curr)
    except Exception as e:
        send_email("Daily Report Error", str(e))

# Schedule report
scheduler = BackgroundScheduler(timezone=REPORT_TZ)
hr, mn = map(int, DAILY_REPORT_TIME.split(':'))
scheduler.add_job(daily_report, trigger=CronTrigger(hour=hr, minute=mn))
scheduler.start()

@app.route('/webhook', methods=['POST'])
def webhook():
    global last_super_signal
    data = request.get_json()
    if data.get('passphrase') != WEBHOOK_PASSPHRASE:
        return jsonify({'error': 'Invalid passphrase'}), 403

    ind = data.get('indicator')   # should be "SUPER"
    sig = data.get('signal')      # "BUY" or "SELL"
    if ind != 'SUPER' or sig not in ['BUY', 'SELL']:
        return jsonify({'error': 'Invalid payload'}), 400

    # No change -> nothing to do
    if sig == last_super_signal:
        return jsonify({'status': 'no change'}), 200

    # Determine current position
    positions = client.futures_position_information(symbol=SYMBOL)
    pos = next((p for p in positions if p['symbol']==SYMBOL), None)
    amt = float(pos['positionAmt'])
    current_side = 'LONG' if amt > 0 else ('SHORT' if amt < 0 else None)

    result = None
    # On BUY signal
    if sig == 'BUY':
        # Close short if open
        if current_side == 'SHORT':
            client.futures_create_order(
                symbol=SYMBOL,
                side=SIDE_BUY,
                type=ORDER_TYPE_MARKET,
                quantity=abs(amt)
            )
        # Open long
        bal = float(client.futures_account_balance()[0]['balance'])
        price = float(client.futures_mark_price(symbol=SYMBOL)['markPrice'])
        qty = round((bal * LEVERAGE) / price, 3)
        entry = client.futures_create_order(
            symbol=SYMBOL,
            side=SIDE_BUY,
            type=ORDER_TYPE_MARKET,
            quantity=qty
        )
        # Place stop-market for 10% capital loss
        stop_price = price * (1 - STOP_LOSS_PCT / LEVERAGE)
        client.futures_create_order(
            symbol=SYMBOL,
            side=SIDE_SELL,
            type=ORDER_TYPE_STOP_MARKET,
            stopPrice=round(stop_price, 2),
            closePosition=True
        )
        result = entry
        send_email("Entry BUY", json.dumps(entry, indent=2))

    # On SELL signal
    if sig == 'SELL':
        # Close long if open
        if current_side == 'LONG':
            client.futures_create_order(
                symbol=SYMBOL,
                side=SIDE_SELL,
                type=ORDER_TYPE_MARKET,
                quantity=abs(amt)
            )
        # Open short
        bal = float(client.futures_account_balance()[0]['balance'])
        price = float(client.futures_mark_price(symbol=SYMBOL)['markPrice'])
        qty = round((bal * LEVERAGE) / price, 3)
        entry = client.futures_create_order(
            symbol=SYMBOL,
            side=SIDE_SELL,
            type=ORDER_TYPE_MARKET,
            quantity=qty
        )
        # Place stop-market for 10% capital loss
        stop_price = price * (1 + STOP_LOSS_PCT / LEVERAGE)
        client.futures_create_order(
            symbol=SYMBOL,
            side=SIDE_BUY,
            type=ORDER_TYPE_STOP_MARKET,
            stopPrice=round(stop_price, 2),
            closePosition=True
        )
        result = entry
        send_email("Entry SELL", json.dumps(entry, indent=2))

    last_super_signal = sig
    return jsonify({'status': 'ok', 'result': result}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
