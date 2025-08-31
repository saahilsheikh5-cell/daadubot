import os
import json
import time
import threading
import traceback
from flask import Flask, request
import telebot
from telebot import types
from binance.client import Client

# ================= CONFIG =================
API_KEY = os.getenv("BINANCE_API_KEY", "")
API_SECRET = os.getenv("BINANCE_API_SECRET", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "1263295916"))

bot = telebot.TeleBot(BOT_TOKEN)
client = Client(API_KEY, API_SECRET)
app = Flask(__name__)

DATA_FILE = "user_data.json"

# ================= HELPERS =================
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=4)

user_data = load_data()

# ================= TECHNICAL ANALYSIS =================
import pandas as pd
import numpy as np

def fetch_klines(symbol, interval="5m", limit=100):
    try:
        klines = client.get_klines(symbol=symbol, interval=interval, limit=limit)
        df = pd.DataFrame(klines, columns=["time", "o", "h", "l", "c", "v", "ct", "qv", "n", "tbv", "tbqv", "i"])
        df["c"] = df["c"].astype(float)
        return df
    except Exception as e:
        print(f"Error fetching klines: {e}")
        return None

def analyze(symbol, interval="5m"):
    df = fetch_klines(symbol, interval)
    if df is None or df.empty:
        return f"⚠️ Error fetching data for {symbol}", None

    close = df["c"]

    # RSI
    delta = close.diff()
    gain = np.where(delta > 0, delta, 0)
    loss = np.where(delta < 0, -delta, 0)
    avg_gain = pd.Series(gain).rolling(14).mean()
    avg_loss = pd.Series(loss).rolling(14).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    last_rsi = rsi.iloc[-1]

    # EMA
    ema20 = close.ewm(span=20).mean().iloc[-1]
    ema50 = close.ewm(span=50).mean().iloc[-1]

    # MACD
    ema12 = close.ewm(span=12).mean()
    ema26 = close.ewm(span=26).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9).mean()
    last_macd = macd.iloc[-1]
    last_signal = signal.iloc[-1]

    # Bollinger Bands
    sma20 = close.rolling(window=20).mean()
    std20 = close.rolling(window=20).std()
    upper = sma20 + (std20 * 2)
    lower = sma20 - (std20 * 2)
    last_price = close.iloc[-1]

    desc = []
    if last_rsi < 30:
        desc.append("Oversold → possible bounce")
    elif last_rsi > 70:
        desc.append("Overbought → possible drop")

    if last_macd > last_signal:
        desc.append("Bullish MACD crossover")
    else:
        desc.append("Bearish MACD crossover")

    if last_price > ema20 > ema50:
        desc.append("Strong uptrend above EMA20 & EMA50")
    elif last_price < ema20 < ema50:
        desc.append("Strong downtrend below EMA20 & EMA50")

    if last_price >= upper.iloc[-1]:
        desc.append("Price at upper Bollinger Band → overbought")
    elif last_price <= lower.iloc[-1]:
        desc.append("Price at lower Bollinger Band → oversold")

    summary = "; ".join(desc[:3]) if desc else "No strong signals."
    
    return f"📊 {symbol} [{interval}]\nRSI: {last_rsi:.2f}\nMACD: {last_macd:.4f} vs Signal: {last_signal:.4f}\nEMA20: {ema20:.2f}, EMA50: {ema50:.2f}\nPrice: {last_price:.2f}\n👉 {summary}", summary

# ================= BOT COMMANDS =================
@bot.message_handler(commands=["start"])
def start(message):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("➕ Add Coin", "➖ Remove Coin")
    markup.add("📈 Signals", "🚀 Top Movers")
    markup.add("⏹ Stop Auto Signals")
    bot.send_message(message.chat.id, "🤖 Welcome to Ultra Signals Bot!", reply_markup=markup)

@bot.message_handler(func=lambda m: m.text == "➕ Add Coin")
def add_coin(message):
    bot.send_message(message.chat.id, "Enter coin symbol to add (e.g., BTCUSDT):")
    bot.register_next_step_handler(message, save_coin)

def save_coin(message):
    coin = message.text.strip().upper()
    uid = str(message.chat.id)
    if uid not in user_data:
        user_data[uid] = {"coins": []}
    if coin not in user_data[uid]["coins"]:
        user_data[uid]["coins"].append(coin)
    save_data(user_data)
    bot.send_message(message.chat.id, f"✅ {coin} added to My Coins.")

@bot.message_handler(func=lambda m: m.text == "➖ Remove Coin")
def remove_coin(message):
    uid = str(message.chat.id)
    if uid not in user_data or not user_data[uid]["coins"]:
        bot.send_message(message.chat.id, "⚠️ No coins to remove.")
        return
    coins = user_data[uid]["coins"]
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for c in coins:
        markup.add(c)
    bot.send_message(message.chat.id, "Select coin to remove:", reply_markup=markup)
    bot.register_next_step_handler(message, confirm_remove)

def confirm_remove(message):
    uid = str(message.chat.id)
    coin = message.text.strip().upper()
    if uid in user_data and coin in user_data[uid]["coins"]:
        user_data[uid]["coins"].remove(coin)
        save_data(user_data)
        bot.send_message(message.chat.id, f"❌ {coin} removed.")

@bot.message_handler(func=lambda m: m.text == "📈 Signals")
def signals_menu(message):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("💼 My Coins", "🌍 All Coins", "🔎 Particular Coin")
    markup.add("⬅️ Back")
    bot.send_message(message.chat.id, "Choose a signal option:", reply_markup=markup)

@bot.message_handler(func=lambda m: m.text == "⬅️ Back")
def back_menu(message):
    start(message)

# ================= AUTO SCAN =================
def auto_scan():
    bot.send_message(ADMIN_CHAT_ID, "✅ Admin log:\nAuto-scan started for top100 (every 60s).")
    while True:
        try:
            tickers = client.get_ticker()[:100]
            for t in tickers:
                symbol = t["symbol"]
                if symbol.endswith("USDT"):
                    msg, _ = analyze(symbol, "5m")
                    if "No strong signals" not in msg:
                        bot.send_message(ADMIN_CHAT_ID, msg)
        except Exception as e:
            bot.send_message(ADMIN_CHAT_ID, f"❌ Auto-scan error: {e}")
        time.sleep(60)

# ================= TOP MOVERS =================
def top_movers():
    bot.send_message(ADMIN_CHAT_ID, "✅ Admin log:\nTop Movers monitor started.")
    while True:
        try:
            tickers = client.get_ticker()
            sorted_tickers = sorted(tickers, key=lambda x: float(x['priceChangePercent']), reverse=True)
            top = sorted_tickers[:5] + sorted_tickers[-5:]
            msg = "🚀 Top Movers:\n" + "\n".join([f"{t['symbol']}: {t['priceChangePercent']}%" for t in top if t['symbol'].endswith("USDT")])
            bot.send_message(ADMIN_CHAT_ID, msg)
        except Exception as e:
            bot.send_message(ADMIN_CHAT_ID, f"❌ Top Movers error: {e}")
        time.sleep(120)

# ================= STOP AUTO =================
@bot.message_handler(func=lambda m: m.text == "⏹ Stop Auto Signals")
def stop_auto(message):
    bot.send_message(message.chat.id, "⏹ Auto signals stopped.")

# ================= FLASK WEBHOOK =================
@app.route("/" + BOT_TOKEN, methods=["POST"])
def webhook():
    json_str = request.get_data().decode("UTF-8")
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "", 200

@app.route("/")
def index():
    return "Bot running!"

# ================= MAIN =================
if __name__ == "__main__":
    threading.Thread(target=auto_scan, daemon=True).start()
    threading.Thread(target=top_movers, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)








