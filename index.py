# final index.py
import os
import time
import json
import sqlite3
import threading
from datetime import datetime, timezone
from flask import Flask, request, jsonify

import requests
import pandas as pd
import numpy as np
from ta.momentum import RSIIndicator
from ta.trend import MACD, EMAIndicator, SMAIndicator
from ta.volatility import AverageTrueRange

from binance.client import Client
import telebot

# -----------------------
# Environment & Clients
# -----------------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")  # e.g. https://your-service.onrender.com
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")  # optional default chat
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY")
BINANCE_API_SECRET = os.environ.get("BINANCE_API_SECRET")

if not TELEGRAM_TOKEN or not WEBHOOK_URL or not BINANCE_API_KEY or not BINANCE_API_SECRET:
    raise RuntimeError("Please set TELEGRAM_TOKEN, WEBHOOK_URL, BINANCE_API_KEY, BINANCE_API_SECRET in env")

bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=True)
app = Flask(__name__)
binance = Client(BINANCE_API_KEY, BINANCE_API_SECRET)

# -----------------------
# Database (SQLite)
# -----------------------
DB_FILE = "bot.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT NOT NULL,
        symbol TEXT NOT NULL,
        timeframe TEXT NOT NULL,
        interval_seconds INTEGER NOT NULL,
        UNIQUE(chat_id, symbol, timeframe)
    )
    """)
    conn.commit()
    conn.close()

init_db()

def add_subscription(chat_id, symbol, timeframe, interval_seconds):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO subscriptions (chat_id,symbol,timeframe,interval_seconds) VALUES (?,?,?,?)",
                (str(chat_id), symbol.upper(), timeframe, interval_seconds))
    conn.commit()
    conn.close()

def remove_subscription(chat_id, symbol, timeframe):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("DELETE FROM subscriptions WHERE chat_id=? AND symbol=? AND timeframe=?",
                (str(chat_id), symbol.upper(), timeframe))
    changed = cur.rowcount
    conn.commit()
    conn.close()
    return changed

def list_subscriptions(chat_id):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT symbol, timeframe, interval_seconds FROM subscriptions WHERE chat_id=?", (str(chat_id),))
    rows = cur.fetchall()
    conn.close()
    return rows

def get_all_subscriptions():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT id, chat_id, symbol, timeframe, interval_seconds FROM subscriptions")
    rows = cur.fetchall()
    conn.close()
    return rows

# -----------------------
# Utilities - intervals map
# -----------------------
TF_TO_SECONDS = {
    "1m": 60,
    "5m": 5*60,
    "15m": 15*60,
    "1h": 60*60,
    "4h": 4*60*60,
    "1d": 24*60*60
}

def tf_to_limit(tf):
    # choose a sensible kline limit for indicators
    return 500 if tf in ["1m","5m","15m"] else 300

# -----------------------
# Binance Data helpers
# -----------------------
def fetch_klines(symbol, interval, limit=None):
    if limit is None:
        limit = tf_to_limit(interval)
    klines = binance.get_klines(symbol=symbol, interval=interval, limit=limit)
    df = pd.DataFrame(klines, columns=[
        "open_time","open","high","low","close","volume","close_time","qav","trades","tbav","tqav","ignore"
    ])
    df["open"] = df["open"].astype(float)
    df["high"] = df["high"].astype(float)
    df["low"] = df["low"].astype(float)
    df["close"] = df["close"].astype(float)
    df["volume"] = df["volume"].astype(float)
    return df

def fetch_orderbook(symbol, limit=50):
    return binance.get_order_book(symbol=symbol, limit=limit)

# -----------------------
# Indicator calculations
# -----------------------
def calculate_indicators(df):
    # must have columns 'open','high','low','close','volume'
    close = df['close']
    high = df['high']
    low = df['low']

    df = df.copy()
    df['rsi'] = RSIIndicator(close, window=14).rsi()
    macd_obj = MACD(close, window_slow=26, window_fast=12, window_sign=9)
    df['macd'] = macd_obj.macd()
    df['macd_signal'] = macd_obj.macd_signal()
    df['ema9'] = EMAIndicator(close, window=9).ema_indicator()
    df['ema21'] = EMAIndicator(close, window=21).ema_indicator()
    df['sma50'] = SMAIndicator(close, window=50).sma_indicator()
    df['sma200'] = SMAIndicator(close, window=200).sma_indicator()
    df['atr'] = AverageTrueRange(high, low, close, window=14).average_true_range()
    df['vol_ma20'] = df['volume'].rolling(20).mean()
    return df

def detect_candlestick(df):
    last = df.iloc[-1]
    prev = df.iloc[-2]
    body = abs(last['close'] - last['open'])
    high = last['high']
    low = last['low']
    lower_shadow = min(last['open'], last['close']) - low
    upper_shadow = high - max(last['open'], last['close'])
    # simple patterns
    if lower_shadow >= 2*body and upper_shadow <= body:
        return "hammer"
    if upper_shadow >= 2*body and lower_shadow <= body:
        return "shooting_star"
    if last['close'] > last['open'] and prev['close'] < prev['open'] and last['close'] > prev['open']:
        return "bullish_engulfing"
    if last['close'] < last['open'] and prev['close'] > prev['open'] and last['close'] < prev['open']:
        return "bearish_engulfing"
    if body <= 0.1 * (last['high'] - last['low']):
        return "doji"
    return None

# -----------------------
# Signal generation (multi-indicator + orderbook)
# -----------------------
CONFIDENCE_THRESHOLD = 4  # number of positive indicator conditions needed
MAX_LEVERAGE = 50
RISK_PERCENT = float(os.environ.get("RISK_PER_TRADE", 1.0))  # percent of capital per trade
TOTAL_CAPITAL = float(os.environ.get("TOTAL_CAPITAL", 10000.0))

def suggest_leverage(df, orderbook):
    # ATR-based base leverage, modified by orderbook liquidity & spread
    last = df.iloc[-1]
    atr = last['atr'] if not np.isnan(last['atr']) else (last['high'] - last['low'])
    price = last['close']
    if atr <= 0:
        return 1
    # base leverage idea: risk_percent * price / atr
    base = (RISK_PERCENT/100)*price / atr
    # compute liquidity factor: depth on near price relative to one contract size
    bids = orderbook.get('bids', [])
    asks = orderbook.get('asks', [])
    # compute best depth near top 5 levels
    def depth(side, levels=5):
        s = 0.0
        for i, lv in enumerate(side[:levels]):
            qty = float(lv[1])
            s += qty
        return s
    bid_depth = depth(bids, levels=5)
    ask_depth = depth(asks, levels=5)
    depth_factor = min(3.0, (bid_depth + ask_depth)/10.0 + 1.0)  # heuristic
    leverage = int(max(1, min(MAX_LEVERAGE, base * depth_factor)))
    return leverage

def compute_position_size(entry, sl, leverage):
    # risk amount
    risk_amount = TOTAL_CAPITAL * (RISK_PERCENT/100.0)
    stop_dist = abs(entry - sl)
    if stop_dist == 0:
        return 0.0
    raw_qty = risk_amount / stop_dist
    qty_leveraged = raw_qty * leverage
    return round(qty_leveraged, 6)

def generate_signal_for_symbol(symbol, timeframe):
    try:
        df = fetch_klines(symbol, timeframe, limit=tf_to_limit(timeframe))
    except Exception as e:
        return {"error": f"klines fetch error: {e}"}
    df = calculate_indicators(df)
    last = df.iloc[-1]

    # indicator checks
    score = 0
    reasons = []

    # RSI
    if last['rsi'] < 30:
        score += 1; reasons.append("RSI oversold")
    elif last['rsi'] > 70:
        score += 1; reasons.append("RSI overbought")

    # MACD
    if last['macd'] > last['macd_signal']:
        score += 1; reasons.append("MACD bullish")
    elif last['macd'] < last['macd_signal']:
        score += 1; reasons.append("MACD bearish")

    # EMA cross
    if last['ema9'] > last['ema21']:
        score += 1; reasons.append("EMA9>EMA21")
    elif last['ema9'] < last['ema21']:
        score += 1; reasons.append("EMA9<EMA21")

    # SMA cross
    if last['sma50'] > last['sma200']:
        score += 1; reasons.append("SMA50>SMA200")
    elif last['sma50'] < last['sma200']:
        score += 1; reasons.append("SMA50<SMA200")

    # volume
    if last['volume'] > (df['vol_ma20'].iloc[-1] if not np.isnan(df['vol_ma20'].iloc[-1]) else 0):
        score += 1; reasons.append("Volume above MA20")

    # candle
    pattern = detect_candlestick(df)
    if pattern in ("hammer", "bullish_engulfing"):
        score += 1; reasons.append(pattern)
    if pattern in ("shooting_star", "bearish_engulfing"):
        score += 1; reasons.append(pattern)

    # Decide signal direction using majority of directional indicators
    # Count bullish vs bearish reasons
    bullish_tags = {"RSI oversold","MACD bullish","EMA9>EMA21","SMA50>SMA200","Volume above MA20","hammer","bullish_engulfing"}
    bearish_tags = {"RSI overbought","MACD bearish","EMA9<EMA21","SMA50<SMA200","Volume above MA20","shooting_star","bearish_engulfing"}
    bullish_count = sum(1 for r in reasons if r in bullish_tags)
    bearish_count = sum(1 for r in reasons if r in bearish_tags)

    signal_type = None
    if score >= CONFIDENCE_THRESHOLD:
        if bullish_count > bearish_count:
            signal_type = "BUY"
        elif bearish_count > bullish_count:
            signal_type = "SELL"
        else:
            # fallback to RSI/MACD direction
            if last['rsi'] < 40 and last['macd'] > last['macd_signal']:
                signal_type = "BUY"
            elif last['rsi'] > 60 and last['macd'] < last['macd_signal']:
                signal_type = "SELL"

    if signal_type is None:
        return {"signal": None, "score": score, "reasons": reasons}

    # orderbook for precision
    try:
        ob = fetch_orderbook(symbol, limit=50)
    except Exception:
        ob = {"bids": [], "asks": []}

    # suggested levels
    price = last['close']
    atr = last['atr'] if not np.isnan(last['atr']) and last['atr']>0 else (last['high']-last['low'])
    sl_buffer = 1.5  # ATR multiplier for SL
    if signal_type == "BUY":
        entry = price
        sl = price - sl_buffer * atr
        tp1 = entry + 1.0 * atr
        tp2 = entry + 2.0 * atr
    else:
        entry = price
        sl = price + sl_buffer * atr
        tp1 = entry - 1.0 * atr
        tp2 = entry - 2.0 * atr

    leverage = suggest_leverage(df, ob)
    position_size = compute_position_size(entry, sl, leverage)

    result = {
        "signal": signal_type,
        "score": score,
        "reasons": reasons,
        "entry": round(entry, 8),
        "sl": round(sl, 8),
        "tp1": round(tp1, 8),
        "tp2": round(tp2, 8),
        "leverage": leverage,
        "position_size": position_size,
        "orderbook_spread_pct": None
    }

    # compute spread %
    try:
        if ob and ob.get('bids') and ob.get('asks'):
            best_bid = float(ob['bids'][0][0])
            best_ask = float(ob['asks'][0][0])
            spread_pct = (best_ask - best_bid) / ((best_ask+best_bid)/2) * 100.0
            result['orderbook_spread_pct'] = round(spread_pct, 4)
    except Exception:
        pass

    return result

# -----------------------
# Telegram command handlers
# -----------------------
HELP = (
    "Commands:\n"
    "/start - help\n"
    "/add SYMBOL TIMEFRAME - subscribe (e.g. /add BTCUSDT 5m)\n"
    "/remove SYMBOL TIMEFRAME - unsubscribe\n"
    "/mycoins - list your subscriptions\n"
    "/signal SYMBOL TIMEFRAME - one-time signal now\n"
    "/topmovers - show top 10 movers 24h\n"
)

@bot.message_handler(commands=['start','help'])
def cmd_start(m):
    bot.send_message(m.chat.id, "Precision Signals Bot — ready.\n" + HELP)

@bot.message_handler(commands=['add'])
def cmd_add(m):
    try:
        parts = m.text.strip().split()
        symbol = parts[1].upper()
        timeframe = parts[2]
        if timeframe not in TF_TO_SECONDS:
            raise ValueError("Invalid timeframe")
        add_subscription(m.chat.id, symbol, timeframe, TF_TO_SECONDS[timeframe])
        bot.send_message(m.chat.id, f"✅ Subscribed to {symbol} {timeframe}")
    except Exception as e:
        bot.send_message(m.chat.id, "Usage: /add SYMBOL TIMEFRAME  e.g. /add BTCUSDT 5m")

@bot.message_handler(commands=['remove'])
def cmd_remove(m):
    try:
        parts = m.text.strip().split()
        symbol = parts[1].upper()
        timeframe = parts[2]
        removed = remove_subscription(m.chat.id, symbol, timeframe)
        if removed:
            bot.send_message(m.chat.id, f"🛑 Unsubscribed {symbol} {timeframe}")
        else:
            bot.send_message(m.chat.id, "No such subscription found.")
    except Exception:
        bot.send_message(m.chat.id, "Usage: /remove SYMBOL TIMEFRAME")

@bot.message_handler(commands=['mycoins'])
def cmd_mycoins(m):
    rows = list_subscriptions(m.chat.id)
    if not rows:
        bot.send_message(m.chat.id, "You have no subscriptions. Use /add to subscribe.")
        return
    lines = [f"{r[0]} {r[1]} (every {r[2]}s)" for r in rows]
    bot.send_message(m.chat.id, "Your subscriptions:\n" + "\n".join(lines))

@bot.message_handler(commands=['signal'])
def cmd_signal(m):
    try:
        parts = m.text.strip().split()
        symbol = parts[1].upper()
        timeframe = parts[2]
        res = generate_signal_for_symbol(symbol, timeframe)
        if res.get("error"):
            bot.send_message(m.chat.id, f"Error: {res['error']}")
            return
        if not res.get("signal"):
            bot.send_message(m.chat.id, f"No strong signal for {symbol} {timeframe} (score {res.get('score')})")
            return
        # send detailed message
        msg = (
            f"🔔 {res['signal']} — {symbol} {timeframe}\n"
            f"Score: {res['score']}\n"
            f"Reasons: {', '.join(res['reasons'])}\n"
            f"Entry: {res['entry']}\nSL: {res['sl']}\nTP1: {res['tp1']} | TP2: {res['tp2']}\n"
            f"Suggested Leverage: x{res['leverage']}\n"
            f"Position Size (units): {res['position_size']}\n"
            f"Spread%: {res.get('orderbook_spread_pct')}"
        )
        bot.send_message(m.chat.id, msg)
    except Exception as e:
        bot.send_message(m.chat.id, "Usage: /signal SYMBOL TIMEFRAME")

@bot.message_handler(commands=['topmovers'])
def cmd_topmovers(m):
    try:
        url = "https://api.binance.com/api/v3/ticker/24hr"
        data = requests.get(url, timeout=10).json()
        df = pd.DataFrame(data)
        df['priceChangePercent'] = df['priceChangePercent'].astype(float)
        df = df[df['symbol'].str.endswith("USDT")]
        top = df.sort_values("priceChangePercent", ascending=False).head(10)
        lines = [f"{r.symbol}: {r.priceChangePercent:.2f}%" for r in top.itertuples()]
        bot.send_message(m.chat.id, "Top movers (24h):\n" + "\n".join(lines))
    except Exception as e:
        bot.send_message(m.chat.id, f"Top movers error: {e}")

# -----------------------
# Background scheduler - processes subscriptions and sends auto signals
# -----------------------
SENT_CACHE = set()  # to avoid duplicates: (chat,symbol,tf,last_signal_type)

def subscription_worker():
    while True:
        try:
            subs = get_all_subscriptions()
            for sub in subs:
                _id, chat_id, symbol, timeframe, interval_seconds = sub
                res = generate_signal_for_symbol(symbol, timeframe)
                if res.get("signal"):
                    key = (chat_id, symbol, timeframe, res['signal'])
                    if key in SENT_CACHE:
                        continue
                    # build message
                    msg = (
                        f"🔔 Auto Signal: {res['signal']} — {symbol} {timeframe}\n"
                        f"Score: {res['score']}\n"
                        f"Reasons: {', '.join(res['reasons'])}\n"
                        f"Entry: {res['entry']}\nSL: {res['sl']}\nTP1: {res['tp1']} | TP2: {res['tp2']}\n"
                        f"Suggested Leverage: x{res['leverage']}\n"
                        f"Position Size: {res['position_size']}\n"
                        f"Spread%: {res.get('orderbook_spread_pct')}"
                    )
                    try:
                        bot.send_message(chat_id, msg)
                        SENT_CACHE.add(key)
                        # keep cache bounded
                        if len(SENT_CACHE) > 2000:
                            SENT_CACHE.clear()
                    except Exception as e:
                        print("Failed to send auto signal:", e)
                # small sleep to avoid API rate hits
                time.sleep(1)
            # Sleep small amount (scan every 30s). Subscriptions store interval but simplest to do periodic loop.
            time.sleep(30)
        except Exception as e:
            print("Subscription worker error:", e)
            time.sleep(10)

# -----------------------
# Webhook endpoints (Flask)
# -----------------------
@app.route("/")
def health():
    return "OK", 200

@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def telegram_webhook():
    try:
        update = request.get_json(force=True)
        bot.process_new_updates([telebot.types.Update.de_json(update)])
    except Exception as e:
        print("Webhook error:", e)
    return "", 200

# -----------------------
# Start webhook & worker on startup
# -----------------------
def set_telegram_webhook():
    webhook_target = f"{WEBHOOK_URL.rstrip('/')}/{TELEGRAM_TOKEN}"
    # delete previous webhook first (safety)
    try:
        requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook", timeout=10)
    except Exception:
        pass
    # set webhook
    res = bot.set_webhook(url=webhook_target)
    if not res:
        print("Failed to set webhook to:", webhook_target)
    else:
        print("Webhook set to:", webhook_target)

# -----------------------
# Run App (Flask) + worker + telebot (no polling)
# -----------------------
if __name__ == "__main__":
    # set webhook
    set_telegram_webhook()

    # start subscription worker
    t = threading.Thread(target=subscription_worker, daemon=True)
    t.start()

    # start flask app (Render will run this)
    port = int(os.environ.get("PORT", 5000))
    print("Starting Flask on port", port)
    app.run(host="0.0.0.0", port=port)



