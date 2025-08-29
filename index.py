import os
import time
import logging
import sqlite3
import threading
import requests
import pandas as pd
import pandas_ta as ta
from apscheduler.schedulers.background import BackgroundScheduler
import telebot
from logging.handlers import TimedRotatingFileHandler

# ---------------------------
# ENVIRONMENT VARIABLES
# ---------------------------
API_KEY = os.getenv("TELEGRAM_TOKEN")
bot = telebot.TeleBot(API_KEY)

# ---------------------------
# LOGGING SETUP
# ---------------------------
log_handler = TimedRotatingFileHandler("bot.log", when="midnight", interval=1, backupCount=7)
log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[log_handler])

# ---------------------------
# DATABASE SETUP
# ---------------------------
DB_FILE = "settings.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT,
        coin TEXT,
        interval_sec INTEGER,
        threshold INTEGER
    )
    """)
    conn.commit()
    conn.close()

def add_subscription(chat_id, coin, interval_sec, threshold):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT INTO subscriptions (chat_id, coin, interval_sec, threshold) VALUES (?,?,?,?)",
                (chat_id, coin, interval_sec, threshold))
    conn.commit()
    conn.close()

def remove_subscription(chat_id, coin):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("DELETE FROM subscriptions WHERE chat_id=? AND coin=?", (chat_id, coin))
    conn.commit()
    conn.close()

def list_subscriptions(chat_id=None, all_chats=False):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    if all_chats:
        cur.execute("SELECT chat_id, coin, interval_sec, threshold FROM subscriptions")
    else:
        cur.execute("SELECT coin, interval_sec, threshold FROM subscriptions WHERE chat_id=?", (chat_id,))
    rows = cur.fetchall()
    conn.close()
    return rows

def reset_settings(chat_id):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("DELETE FROM subscriptions WHERE chat_id=?", (chat_id,))
    conn.commit()
    conn.close()

def test_db_connection():
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.execute("SELECT 1")
        conn.close()
        return True
    except:
        return False

# ---------------------------
# SIGNAL CALCULATION
# ---------------------------
def fetch_klines(symbol, interval='1m', limit=200):
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    data = requests.get(url).json()
    df = pd.DataFrame(data, columns=[
        "time","open","high","low","close","volume","c1","c2","c3","c4","c5","ignore"
    ])
    df["close"] = df["close"].astype(float)
    df["high"] = df["high"].astype(float)
    df["low"] = df["low"].astype(float)
    df["volume"] = df["volume"].astype(float)
    return df

def calculate_indicators(df):
    indicators = {}
    df['rsi'] = ta.rsi(df['close'], length=14)
    df['ema_short'] = ta.ema(df['close'], length=9)
    df['ema_long'] = ta.ema(df['close'], length=21)
    macd = ta.macd(df['close'])
    df['macd'] = macd['MACD_12_26_9']
    df['macd_signal'] = macd['MACDs_12_26_9']
    df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=14)
    indicators['rsi'] = df['rsi'].iloc[-1]
    indicators['ema_short'] = df['ema_short'].iloc[-1]
    indicators['ema_long'] = df['ema_long'].iloc[-1]
    indicators['macd'] = df['macd'].iloc[-1]
    indicators['macd_signal'] = df['macd_signal'].iloc[-1]
    indicators['atr'] = df['atr'].iloc[-1]
    indicators['close'] = df['close'].iloc[-1]
    indicators['volume'] = df['volume'].iloc[-1]
    return indicators

def suggest_signal(ind):
    # Ultra signal logic
    score = 0
    # RSI
    if ind['rsi'] < 30:
        score += 2
    elif ind['rsi'] > 70:
        score -= 2
    # EMA crossover
    if ind['ema_short'] > ind['ema_long']:
        score += 1
    elif ind['ema_short'] < ind['ema_long']:
        score -= 1
    # MACD
    if ind['macd'] > ind['macd_signal']:
        score += 1
    else:
        score -= 1
    # Decide
    if score >= 3:
        signal = "BUY ✅"
        leverage = 20
    elif score <= -3:
        signal = "SELL ❌"
        leverage = 20
    elif score == 2:
        signal = "BUY ⚡"
        leverage = 10
    elif score == -2:
        signal = "SELL ⚡"
        leverage = 10
    else:
        signal = "NEUTRAL ⚖️"
        leverage = 3
    return signal, leverage

def analyze_and_signal(bot, chat_id, coin, threshold):
    try:
        df = fetch_klines(coin)
        ind = calculate_indicators(df)
        signal, leverage = suggest_signal(ind)
        sl = round(ind['atr'] * 2, 2)
        tp = round(ind['atr'] * 4, 2)
        msgs = [
            f"📊 {coin} Signal Update\nSignal: {signal}\nRSI: {round(ind['rsi'],2)}",
            f"EMA Trend: Short({round(ind['ema_short'],2)}) / Long({round(ind['ema_long'],2)})",
            f"MACD: {round(ind['macd'],2)} / Signal: {round(ind['macd_signal'],2)}",
            f"ATR (SL suggestion): {sl}",
            f"Volume: {ind['volume']}",
            f"💰 Trade Plan:\nEntry: {ind['close']}\nSL: {sl}\nTP: {tp}\nSuggested Leverage: x{leverage}"
        ]
        for m in msgs:
            bot.send_message(chat_id, m)
            time.sleep(1)
    except Exception as e:
        logging.error(f"Error sending signal for {coin}: {e}")

# ---------------------------
# SCHEDULER
# ---------------------------
scheduler = BackgroundScheduler()
scheduler.start()

def restore_jobs():
    all_subs = list_subscriptions(all_chats=True)
    for chat_id, coin, interval_sec, threshold in all_subs:
        scheduler.add_job(analyze_and_signal, 'interval', seconds=interval_sec,
                          args=[bot, chat_id, coin, threshold],
                          id=f"{chat_id}_{coin}", replace_existing=True)
        logging.info(f"Restored job: {chat_id} {coin} ({interval_sec}s)")

# ---------------------------
# TELEGRAM BOT COMMANDS
# ---------------------------
@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, "🤖 Welcome! Use /add SYMBOL INTERVAL_SECONDS THRESHOLD to start ultra signals.\nExample: /add BTCUSDT 60 70")
    logging.info(f"/start called by {message.chat.id}")

@bot.message_handler(commands=['add'])
def add_coin(message):
    try:
        _, symbol, interval, threshold = message.text.split()
        interval = int(interval)
        threshold = int(threshold)
        add_subscription(message.chat.id, symbol.upper(), interval, threshold)
        scheduler.add_job(analyze_and_signal, 'interval', seconds=interval,
                          args=[bot, message.chat.id, symbol.upper(), threshold],
                          id=f"{message.chat.id}_{symbol}", replace_existing=True)
        bot.reply_to(message, f"✅ Subscribed to {symbol.upper()} every {interval}s with threshold {threshold}")
        logging.info(f"Added subscription for {message.chat.id}: {symbol.upper()}, {interval}s, threshold {threshold}")
    except Exception as e:
        bot.reply_to(message, f"❌ Usage: /add BTCUSDT 60 70 — Error: {e}")
        logging.error(f"Failed to add subscription: {e}")

@bot.message_handler(commands=['stop'])
def stop_coin(message):
    try:
        _, symbol = message.text.split()
        remove_subscription(message.chat.id, symbol.upper())
        scheduler.remove_job(f"{message.chat.id}_{symbol.upper()}")
        bot.reply_to(message, f"🛑 Stopped {symbol.upper()}")
        logging.info(f"Stopped subscription for {message.chat.id}: {symbol.upper()}")
    except Exception as e:
        bot.reply_to(message, f"❌ Usage: /stop BTCUSDT — Error: {e}")
        logging.error(f"Failed to stop subscription: {e}")

@bot.message_handler(commands=['mycoins'])
def mycoins(message):
    subs = list_subscriptions(message.chat.id)
    if not subs:
        bot.reply_to(message, "📭 No active subscriptions")
    else:
        msg = "📊 Your subscriptions:\n"
        for s in subs:
            msg += f"- {s[0]} (interval {s[1]}s, threshold {s[2]})\n"
        bot.reply_to(message, msg)

@bot.message_handler(commands=['reset'])
def reset(message):
    reset_settings(message.chat.id)
    bot.reply_to(message, "♻️ All settings reset")
    logging.info(f"Reset settings for {message.chat.id}")

@bot.message_handler(commands=['logs'])
def send_logs(message):
    try:
        if os.path.exists("bot.log"):
            with open("bot.log", "rb") as f:
                bot.send_document(message.chat.id, f)
        else:
            bot.reply_to(message, "⚠️ No log file found yet.")
    except Exception as e:
        bot.reply_to(message, f"❌ Failed to send logs: {e}")
        logging.error(f"Failed to send logs: {e}")

@bot.message_handler(commands=['health'])
def health_check(message):
    db_ok = test_db_connection()
    sched_jobs = len(scheduler.get_jobs())
    msg = f"🩺 Health Check:\n- DB Connection: {'✅' if db_ok else '❌'}\n- Scheduled Jobs: {sched_jobs}\n- Bot: ✅ Running"
    bot.reply_to(message, msg)

# ---------------------------
# MAIN
# ---------------------------
if __name__ == "__main__":
    init_db()
    restore_jobs()
    while True:
        try:
            logging.info("Bot polling started...")
            bot.infinity_polling(timeout=60, long_polling_timeout=60)
        except Exception as e:
            logging.error(f"Polling error: {e}. Restarting in 5s...")
            time.sleep(5)


