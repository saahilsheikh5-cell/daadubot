import os
import sqlite3
import threading
import time
import math
import requests
import numpy as np
import pandas as pd
from flask import Flask, request
import telebot
from apscheduler.schedulers.background import BackgroundScheduler

# ------------------------
# ENV
# ------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("❌ BOT_TOKEN not found in environment variables!")

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET = os.getenv("BINANCE_SECRET")

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# ------------------------
# DB
# ------------------------
DB_FILE = "settings.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT NOT NULL,
        coin TEXT NOT NULL,
        timeframe TEXT NOT NULL,
        interval_seconds INTEGER NOT NULL DEFAULT 60,
        score_threshold INTEGER NOT NULL DEFAULT 70
    )
    """)
    conn.commit()
    conn.close()

init_db()

# ------------------------
# Scheduler
# ------------------------
scheduler = BackgroundScheduler()
scheduler.start()

lock = threading.Lock()

# ------------------------
# Binance klines helper
# ------------------------
def fetch_klines(symbol, interval, limit=300):
    # Binance public API
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    df = pd.DataFrame(data, columns=[
        "open_time","open","high","low","close","volume","close_time","qav","num_trades","taker_base_vol","taker_quote_vol","ignore"
    ])
    df["close"] = df["close"].astype(float)
    df["open"] = df["open"].astype(float)
    df["high"] = df["high"].astype(float)
    df["low"] = df["low"].astype(float)
    df["volume"] = df["volume"].astype(float)
    return df

# ------------------------
# Technicals & scoring
# ------------------------
def compute_indicators(df):
    close = df["close"]

    # RSI (14)
    delta = close.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    ma_up = up.rolling(14).mean()
    ma_down = down.rolling(14).mean()
    rs = ma_up / ma_down
    rsi = 100 - (100 / (1 + rs))

    # EMA
    ema_fast = close.ewm(span=12, adjust=False).mean()
    ema_slow = close.ewm(span=26, adjust=False).mean()

    # MACD
    macd = ema_fast - ema_slow
    macd_sig = macd.ewm(span=9, adjust=False).mean()
    macd_hist = macd - macd_sig

    # ATR (14)
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - close.shift()).abs()
    low_close = (df["low"] - close.shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    atr = tr.rolling(14).mean()

    # Bollinger Bands (20,2)
    ma20 = close.rolling(20).mean()
    std20 = close.rolling(20).std()
    upper_bb = ma20 + 2 * std20
    lower_bb = ma20 - 2 * std20

    # Stochastic (14,3)
    lowest14 = df["low"].rolling(14).min()
    highest14 = df["high"].rolling(14).max()
    stoch_k = 100 * (close - lowest14) / (highest14 - lowest14)
    stoch_d = stoch_k.rolling(3).mean()

    # Volume
    vol20 = df["volume"].rolling(20).mean()

    return {
        "close": close,
        "rsi": rsi,
        "ema_fast": ema_fast,
        "ema_slow": ema_slow,
        "macd_hist": macd_hist,
        "atr": atr,
        "upper_bb": upper_bb,
        "lower_bb": lower_bb,
        "stoch_k": stoch_k,
        "stoch_d": stoch_d,
        "vol20": vol20
    }

def score_signal(latest, inds):
    # latest: index (int) for last candle
    s = 0

    rsi = inds["rsi"].iloc[latest]
    macd_h = inds["macd_hist"].iloc[latest]
    ema_fast = inds["ema_fast"].iloc[latest]
    ema_slow = inds["ema_slow"].iloc[latest]
    close = inds["close"].iloc[latest]
    upper_bb = inds["upper_bb"].iloc[latest]
    lower_bb = inds["lower_bb"].iloc[latest]
    stoch_k = inds["stoch_k"].iloc[latest]
    stoch_d = inds["stoch_d"].iloc[latest]
    vol = inds["vol20"].iloc[latest]
    cur_vol = inds["close"].index  # not used, we use df volume separately in caller

    # RSI signal weight
    if rsi < 20:
        s += 25  # very strong buy
    elif rsi < 30:
        s += 15
    elif rsi > 80:
        s -= 20  # strong sell
    elif rsi > 70:
        s -= 10

    # EMA crossover
    if ema_fast > ema_slow:
        s += 15
    else:
        s -= 10

    # MACD histogram
    if macd_h > 0:
        s += min(15, (macd_h / (abs(macd_h) + 1)) * 15)
    else:
        s -= min(15, (abs(macd_h) / (abs(macd_h) + 1)) * 15)

    # Bollinger breakout
    if close > upper_bb:
        s += 8
    elif close < lower_bb:
        s += 8

    # Stochastic (oversold/overbought)
    if stoch_k < 20 and stoch_d < 20:
        s += 8
    elif stoch_k > 80 and stoch_d > 80:
        s -= 8

    # Normalize to 0-100
    s = max(-100, min(100, s))
    final = (s + 100) / 2  # map -100..100 to 0..100
    return round(final)

# ------------------------
# Trade plan generator
# ------------------------
def suggest_trade_from_inds(df, inds, latest_index, rscore):
    price = inds["close"].iloc[latest_index]
    atr = inds["atr"].iloc[latest_index]

    # direction by score >50 => buy bias, <50 sell bias
    direction = "BUY" if rscore >= 50 else "SELL"

    # suggested leverage (conservative depending on score)
    if rscore >= 85:
        lev = 20
    elif rscore >= 70:
        lev = 10
    elif rscore >= 55:
        lev = 5
    else:
        lev = 2

    # SL/TP using ATR multiples
    if direction == "BUY":
        sl = round(price - 1.5 * atr, 4)
        tp1 = round(price + 2 * atr, 4)
        tp2 = round(price + 3.5 * atr, 4)
    else:
        sl = round(price + 1.5 * atr, 4)
        tp1 = round(price - 2 * atr, 4)
        tp2 = round(price - 3.5 * atr, 4)

    return {
        "direction": direction,
        "price": round(price, 8),
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "leverage": lev
    }

# ------------------------
# Job runner for subscription
# ------------------------
def subscription_job(sub_id):
    with lock:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("SELECT id, chat_id, coin, timeframe, interval_seconds, score_threshold FROM subscriptions WHERE id=?", (sub_id,))
        row = cur.fetchone()
        conn.close()

    if not row:
        return

    _, chat_id, coin, timeframe, interval_seconds, threshold = row

    try:
        df = fetch_klines(coin, timeframe, limit=300)
        inds = compute_indicators(df)
        latest = len(df) - 1
        score = score_signal(latest, inds)

        # only send "ultra" signals above threshold
        if score >= threshold:
            trade = suggest_trade_from_inds(df, inds, latest, score)

            # step messages
            bot.send_message(chat_id, f"📌 {coin} | {timeframe} | Score: {score}%\nPrice: {trade['price']}\nBias: {trade['direction']}")
            bot.send_message(chat_id, f"RSI: {inds['rsi'].iloc[latest]:.2f} | EMA_fast: {inds['ema_fast'].iloc[latest]:.4f} | EMA_slow: {inds['ema_slow'].iloc[latest]:.4f}")
            bot.send_message(chat_id, f"MACD hist: {inds['macd_hist'].iloc[latest]:.6f} | ATR: {inds['atr'].iloc[latest]:.6f}")
            bot.send_message(chat_id, f"Volume vs MA20: {df['volume'].iloc[latest]:.2f} vs {inds['vol20'].iloc[latest]:.2f}")
            bot.send_message(chat_id, f"🎯 Trade Plan\nEntry: {trade['price']}\nSL: {trade['sl']}\nTP1: {trade['tp1']}\nTP2: {trade['tp2']}\nSuggested Leverage: x{trade['leverage']}")

    except Exception as e:
        print("subscription job error:", e)

# ------------------------
# Utility: schedule all jobs from DB
# ------------------------
def schedule_all_subscriptions():
    with lock:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("SELECT id, interval_seconds FROM subscriptions")
        rows = cur.fetchall()
        conn.close()

    # remove existing jobs not in DB
    existing = set(j.id for j in scheduler.get_jobs())
    db_ids = set(str(r[0]) for r in rows)

    for job_id in existing:
        if job_id not in db_ids:
            try:
                scheduler.remove_job(job_id)
            except Exception:
                pass

    for r in rows:
        sub_id, interval_seconds = r
        job_id = str(sub_id)
        if job_id in existing:
            continue
        scheduler.add_job(subscription_job, 'interval', seconds=interval_seconds, args=[sub_id], id=job_id, replace_existing=True)

# Run scheduler initial load
schedule_all_subscriptions()

# ------------------------
# Bot commands (add, setinterval, mycoins, stop, reset)
# ------------------------
@bot.message_handler(commands=['start'])
def start_cmd(message):
    bot.reply_to(message, "Welcome — use /add COIN TIMEFRAME [interval_seconds] to subscribe. Example: /add BTCUSDT 15m 300\nUse /setinterval COIN TIMEFRAME SECONDS to change per-subscription interval.\nUltra signals only sent if score >= threshold (default 70). Use /setthreshold COIN TIMEFRAME 80 to change.)")

@bot.message_handler(commands=['add'])
def add_cmd(message):
    try:
        parts = message.text.split()
        coin = parts[1].upper()
        timeframe = parts[2]
        interval_seconds = int(parts[3]) if len(parts) > 3 else 60
        chat_id = str(message.chat.id)

        with lock:
            conn = sqlite3.connect(DB_FILE)
            cur = conn.cursor()
            cur.execute("INSERT INTO subscriptions (chat_id, coin, timeframe, interval_seconds) VALUES (?,?,?,?)", (chat_id, coin, timeframe, interval_seconds))
            sub_id = cur.lastrowid
            conn.commit()
            conn.close()

        # schedule job
        scheduler.add_job(subscription_job, 'interval', seconds=interval_seconds, args=[sub_id], id=str(sub_id), replace_existing=True)

        bot.reply_to(message, f"✅ Subscribed {coin} {timeframe} every {interval_seconds}s (id={sub_id}).")
    except Exception as e:
        bot.reply_to(message, f"❌ Usage: /add COIN TIMEFRAME [interval_seconds]\nExample: /add BTCUSDT 15m 300\nError: {e}")

@bot.message_handler(commands=['setinterval'])
def set_interval_cmd(message):
    try:
        parts = message.text.split()
        coin = parts[1].upper()
        timeframe = parts[2]
        interval_seconds = int(parts[3])
        chat_id = str(message.chat.id)

        with lock:
            conn = sqlite3.connect(DB_FILE)
            cur = conn.cursor()
            cur.execute("UPDATE subscriptions SET interval_seconds=? WHERE chat_id=? AND coin=? AND timeframe=?", (interval_seconds, chat_id, coin, timeframe))
            conn.commit()
            cur.execute("SELECT id FROM subscriptions WHERE chat_id=? AND coin=? AND timeframe=?", (chat_id, coin, timeframe))
            row = cur.fetchone()
            conn.close()

        if row:
            sub_id = row[0]
            scheduler.add_job(subscription_job, 'interval', seconds=interval_seconds, args=[sub_id], id=str(sub_id), replace_existing=True)
            bot.reply_to(message, f"✅ Interval updated for {coin} {timeframe} -> {interval_seconds}s")
        else:
            bot.reply_to(message, "❌ Subscription not found.")
    except Exception as e:
        bot.reply_to(message, f"❌ Usage: /setinterval COIN TIMEFRAME SECONDS\nError: {e}")

@bot.message_handler(commands=['setthreshold'])
def set_threshold_cmd(message):
    try:
        parts = message.text.split()
        coin = parts[1].upper()
        timeframe = parts[2]
        threshold = int(parts[3])
        chat_id = str(message.chat.id)

        with lock:
            conn = sqlite3.connect(DB_FILE)
            cur = conn.cursor()
            cur.execute("UPDATE subscriptions SET score_threshold=? WHERE chat_id=? AND coin=? AND timeframe=?", (threshold, chat_id, coin, timeframe))
            conn.commit()
            conn.close()

        bot.reply_to(message, f"✅ Threshold set to {threshold}% for {coin} {timeframe}.")
    except Exception as e:
        bot.reply_to(message, "❌ Usage: /setthreshold COIN TIMEFRAME PERCENT")

@bot.message_handler(commands=['mycoins'])
def mycoins_cmd(message):
    chat_id = str(message.chat.id)
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT coin, timeframe, interval_seconds, score_threshold FROM subscriptions WHERE chat_id=?", (chat_id,))
    rows = cur.fetchall()
    conn.close()

    if not rows:
        bot.reply_to(message, "No active subscriptions.")
        return

    msg = "Your subscriptions:\n"
    for r in rows:
        msg += f"{r[0]} {r[1]} | every {r[2]}s | threshold {r[3]}%\n"
    bot.reply_to(message, msg)

@bot.message_handler(commands=['stop'])
def stop_cmd(message):
    try:
        parts = message.text.split()
        coin = parts[1].upper()
        timeframe = parts[2]
        chat_id = str(message.chat.id)

        with lock:
            conn = sqlite3.connect(DB_FILE)
            cur = conn.cursor()
            cur.execute("SELECT id FROM subscriptions WHERE chat_id=? AND coin=? AND timeframe=?", (chat_id, coin, timeframe))
            row = cur.fetchone()
            if not row:
                conn.close()
                bot.reply_to(message, "Subscription not found.")
                return
            sub_id = row[0]
            cur.execute("DELETE FROM subscriptions WHERE id=?", (sub_id,))
            conn.commit()
            conn.close()

        # remove scheduled job
        try:
            scheduler.remove_job(str(sub_id))
        except Exception:
            pass

        bot.reply_to(message, f"Stopped {coin} {timeframe} (id={sub_id}).")
    except Exception as e:
        bot.reply_to(message, "❌ Usage: /stop COIN TIMEFRAME")

@bot.message_handler(commands=['reset'])
def reset_cmd(message):
    chat_id = str(message.chat.id)
    with lock:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("SELECT id FROM subscriptions WHERE chat_id=?", (chat_id,))
        rows = cur.fetchall()
        cur.execute("DELETE FROM subscriptions WHERE chat_id=?", (chat_id,))
        conn.commit()
        conn.close()

    for r in rows:
        try:
            scheduler.remove_job(str(r[0]))
        except Exception:
            pass

    bot.reply_to(message, "All subscriptions cleared.")

# ------------------------
# Flask webhook (optional)
# ------------------------
@app.route("/" + BOT_TOKEN, methods=["POST"])
def webhook():
    json_str = request.stream.read().decode("UTF-8")
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "ok", 200

@app.route("/")
def index():
    return "Bot running"

# ------------------------
# On start: ensure DB jobs scheduled
# ------------------------
schedule_all_subscriptions()

if __name__ == '__main__':
    bot.remove_webhook()
    bot.polling(none_stop=True)




