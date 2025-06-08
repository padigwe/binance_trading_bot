import os
import json
import ssl
import smtplib
from email.mime.text import MIMEText
from flask import Flask, request, jsonify
from binance.client import Client
from binance.enums import (
    SIDE_BUY, SIDE_SELL, ORDER_TYPE_MARKET,
    POSITION_SIDE_LONG, POSITION_SIDE_SHORT
)
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import date
from zoneinfo import ZoneInfo

# CONFIGURATION FROM ENVIRONMENT
API_KEY         = os.getenv("BINANCE_API_KEY")
API_SECRET      = os.getenv("BINANCE_API_SECRET")
WEBHOOK_PASSPHRASE = os.getenv("WEBHOOK_PASSPHRASE", "your_webhook_passphrase")
SYMBOL          = os.getenv("SYMBOL", "SOLUSDT.P")
LEVERAGE        = int(os.getenv("LEVERAGE", "5"))
STOP_LOSS_PCT   = float(os.getenv("STOP_LOSS_PCT", "0.20"))

# Email settings
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")
EMAIL_FROM = os.getenv("EMAIL_FROM", EMAIL_USER)
EMAIL_TO   = os.getenv("EMAIL_TO", EMAIL_FROM)

# Reporting settings
DAILY_REPORT_TIME = os.getenv("DAILY_REPORT_TIME", "07:00")  # HH:MM London
REPORT_TZ         = ZoneInfo("Europe/London")
BASELINE_FILE     = os.getenv("BASELINE_FILE_PATH", "baseline.json")

# Initialize Binance Futures client
tmp = Client(API_KEY, API_SECRET)
tmp.FUTURES_URL = 'https://fapi.binance.com'
client = tmp
client.futures_change_leverage(symbol=SYMBOL, leverage=LEVERAGE)

app = Flask(__name__)

# State: last confirmed signals
timeframe_state = {"SUPER": None, "MACD": None}
# Buffer: hold the latest MACD until next bar arrives
pending_macd = None

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
sched = BackgroundScheduler(timezone=REPORT_TZ)
hr, mn = map(int, DAILY_REPORT_TIME.split(':'))
sched.add_job(daily_report, trigger=CronTrigger(hour=hr, minute=mn))
sched.start()

@app.route('/webhook', methods=['POST'])
def webhook():
    global pending_macd
    data = request.get_json()
    if data.get('passphrase') != WEBHOOK_PASSPHRASE:
        return jsonify({'error': 'Invalid passphrase'}), 403

    ind = data.get('indicator')   # "SUPER" or "MACD"
    sig = data.get('signal')      # "BUY" or "SELL"
    if ind not in timeframe_state or sig not in ['BUY', 'SELL']:
        return jsonify({'error': 'Invalid payload'}), 400

    # For MACD, buffer one bar: only confirm previous bar's signal on new bar
    if ind == 'MACD':
        if pending_macd is None:
            pending_macd = sig
            return jsonify({'status': 'pending macd'}), 200
        else:
            # new bar started: confirm last bar's transition
            timeframe_state['MACD'] = pending_macd
            pending_macd = sig
    else:
        # SUPER is instantaneous
        timeframe_state['SUPER'] = sig

    # Trade logic
    try:
        result = None
        sup = timeframe_state['SUPER']
        mac = timeframe_state['MACD']
        # Current position
        pos_info = client.futures_position_information(symbol=SYMBOL)
        pos = next(p for p in pos_info if p['symbol']==SYMBOL)
        amt = float(pos['positionAmt'])
        side = 'LONG' if amt>0 else ('SHORT' if amt<0 else None)

        # Entry: both agree and not already in that side
        if sup == mac and sup:
            buy = sup=='BUY'
            tgt = POSITION_SIDE_LONG if buy else POSITION_SIDE_SHORT
            if side != tgt:
                # close opposite
                if side:
                    client.futures_create_order(
                        symbol=SYMBOL,
                        side=SIDE_SELL if side=='LONG' else SIDE_BUY,
                        type=ORDER_TYPE_MARKET,
                        quantity=abs(amt)
                    )
                bal = float(client.futures_account_balance()[0]['balance'])
                price = float(client.futures_mark_price(symbol=SYMBOL)['markPrice'])
                qty = round((bal * LEVERAGE)/price, 3)
                ord = client.futures_create_order(
                    symbol=SYMBOL,
                    side=SIDE_BUY if buy else SIDE_SELL,
                    type=ORDER_TYPE_MARKET,
                    quantity=qty
                )
                send_email(f"Entry {sup}", json.dumps(ord, indent=2))
                result = ord

        # Exit: any flip against position
        if side:
            flip = 'SELL' if side=='LONG' else 'BUY'
            if sup==flip or mac==flip:
                cls = client.futures_create_order(
                    symbol=SYMBOL,
                    side=SIDE_SELL if side=='LONG' else SIDE_BUY,
                    type=ORDER_TYPE_MARKET,
                    quantity=abs(amt)
                )
                send_email(f"Exit {flip}", json.dumps(cls, indent=2))
                result = cls

        return jsonify({'status':'ok','result':result}), 200
    except Exception as e:
        send_email("Bot Error", str(e))
        return jsonify({'error':str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
