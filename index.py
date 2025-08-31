import os
import sys
import json
import threading
import time
from flask import Flask, request
import telebot
from telebot import types
from binance.client import Client
import pandas as pd
import numpy as np
import ta

# ==== ENVIRONMENT CHECK ====
required_env_vars = ["TELEGRAM_TOKEN", "BINANCE_API_KEY", "BINANCE_API_SECRET", "PORT", "CHAT_ID"]
missing_vars = [var for var in required_env_vars if not os.getenv(var)]
if missing_vars:
    print(f"❌ Missing environment variables: {', '.join(missing_vars)}")
    sys.exit(1)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
PORT = int(os.getenv("PORT", 5000))
CHAT_ID = int(os.getenv("CHAT_ID"))

AUTO_POLL_INTERVAL = 300  # 5 minutes
MOVER_ALERT_THRESHOLD = 5  # 5% move

# ==== INIT BOT & BINANCE CLIENT ====
bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=True)
client = Client(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)

# ==== FLASK SERVER FOR WEBHOOK ====
app = Flask("")

@app.route("/")
def home():
    return "Bot is running"

@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def webhook():
    json_str = request.get_data().decode("utf-8")
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "!", 200

def run_flask():
    app.run(host="0.0.0.0", port=PORT)

threading.Thread(target=run_flask).start()

WEBHOOK_URL = f"https://YOUR_RENDER_APP_NAME.onrender.com/{TELEGRAM_TOKEN}"
bot.remove_webhook()
bot.set_webhook(url=WEBHOOK_URL)
print(f"🚀 Webhook set: {WEBHOOK_URL}")

# ==== COINS FILE ====
COINS_FILE = "my_coins.json"

# ==== HELPERS ====
def load_coins():
    if not os.path.exists(COINS_FILE):
        return []
    with open(COINS_FILE, "r") as f:
        return json.load(f)

def save_coins(coins):
    with open(COINS_FILE, "w") as f:
        json.dump(coins, f)

def get_signal(symbol, interval="5m", lookback=100):
    try:
        klines = client.get_klines(symbol=symbol, interval=interval, limit=lookback)
        df = pd.DataFrame(klines, columns=[
            "time","o","h","l","c","v","ct","qav","ntr","tbbav","tbqav","ignore"
        ])
        df["c"] = df["c"].astype(float)
        df["h"] = df["h"].astype(float)
        df["l"] = df["l"].astype(float)

        # Indicators
        df["rsi"] = ta.momentum.RSIIndicator(df["c"], window=14).rsi()
        macd = ta.trend.MACD(df["c"])
        df["macd"] = macd.macd()
        df["macd_signal"] = macd.macd_signal()
        df["ma50"] = df["c"].rolling(50).mean()
        bb = ta.volatility.BollingerBands(df["c"], window=20, window_dev=2)
        df["bb_h"] = bb.bollinger_hband()
        df["bb_l"] = bb.bollinger_lband()
        stoch = ta.momentum.StochasticOscillator(df["h"], df["l"], df["c"])
        df["stoch_rsi"] = stoch.stoch()

        last = df.iloc[-1]

        decision = "Neutral"
        explanation = []

        if last["rsi"] < 30 and last["macd"] > last["macd_signal"] and last["c"] > last["ma50"]:
            decision = "✅ Ultra BUY"
            explanation.append("RSI oversold + MACD bullish + Above MA50")
        elif last["rsi"] > 70 and last["macd"] < last["macd_signal"] and last["c"] < last["ma50"]:
            decision = "❌ Ultra SELL"
            explanation.append("RSI overbought + MACD bearish + Below MA50")

        signal_text = f"""
📊 Signal for {symbol} [{interval}]
Decision: {decision}
RSI: {round(last['rsi'],2)}
MACD: {round(last['macd'],4)} / Signal: {round(last['macd_signal'],4)}
Stoch RSI: {round(last['stoch_rsi'],2)}
Price: {last['c']}

Entry: {round(last['c'],4)}
TP1: {round(last['c']*1.01,4)}
TP2: {round(last['c']*1.02,4)}
SL: {round(last['c']*0.99,4)}
Suggested Leverage: x10
Notes: {" | ".join(explanation) if explanation else "Mixed signals"}
        """
        return signal_text
    except Exception as e:
        return f"⚠️ Error fetching data for {symbol} {interval}: {e}"

# ==== MENUS ====
def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("📈 Signals", "➕ Add Coin", "➖ Remove Coin")
    return kb

def signals_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("💼 My Coins", "🌍 All Coins")
    kb.add("🔎 Particular Coin", "🚀 Top Movers")
    kb.add("⬅️ Back")
    return kb

# ==== BOT HANDLERS ====
@bot.message_handler(commands=["start"])
def start(message):
    bot.send_message(message.chat.id, "🤖 Welcome to Ultra Signals Bot!", reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text == "⬅️ Back")
def back_btn(message):
    bot.send_message(message.chat.id, "🔙 Main Menu", reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text == "📈 Signals")
def signals(message):
    bot.send_message(message.chat.id, "Choose a signal option:", reply_markup=signals_menu())

@bot.message_handler(func=lambda msg: msg.text == "💼 My Coins")
def my_coins(message):
    coins = load_coins()
    if not coins:
        bot.send_message(message.chat.id, "❌ No coins added yet. Use ➕ Add Coin.")
        return
    for c in coins:
        txt = get_signal(c, "5m") + "\n" + get_signal(c, "1h") + "\n" + get_signal(c, "1d")
        bot.send_message(message.chat.id, txt)

@bot.message_handler(func=lambda msg: msg.text == "🌍 All Coins")
def all_coins(message):
    tickers = [s["symbol"] for s in client.get_all_tickers() if s["symbol"].endswith("USDT")]
    for c in tickers[:10]:
        txt = get_signal(c, "5m")
        bot.send_message(message.chat.id, txt)

@bot.message_handler(func=lambda msg: msg.text == "🔎 Particular Coin")
def ask_coin(message):
    bot.send_message(message.chat.id, "Enter coin symbol (e.g., BTCUSDT):")
    bot.register_next_step_handler(message, particular_coin)

def particular_coin(message):
    symbol = message.text.upper()
    txt = get_signal(symbol, "5m") + "\n" + get_signal(symbol, "1h") + "\n" + get_signal(symbol, "1d")
    bot.send_message(message.chat.id, txt)

@bot.message_handler(func=lambda msg: msg.text == "🚀 Top Movers")
def top_movers(message):
    tickers = client.get_ticker_24hr()
    sorted_tickers = sorted(tickers, key=lambda x: abs(float(x["priceChangePercent"])), reverse=True)
    top = [t["symbol"] for t in sorted_tickers if t["symbol"].endswith("USDT")][:5]
    for c in top:
        txt = get_signal(c, "5m")
        bot.send_message(message.chat.id, txt)

@bot.message_handler(func=lambda msg: msg.text == "➕ Add Coin")
def add_coin(message):
    bot.send_message(message.chat.id, "Enter coin symbol to add (e.g., BTCUSDT):")
    bot.register_next_step_handler(message, save_new_coin)

def save_new_coin(message):
    symbol = message.text.upper()
    coins = load_coins()
    if symbol not in coins:
        coins.append(symbol)
        save_coins(coins)
        bot.send_message(message.chat.id, f"✅ {symbol} added to My Coins")
    else:
        bot.send_message(message.chat.id, "⚠️ Coin already in list.")

@bot.message_handler(func=lambda msg: msg.text == "➖ Remove Coin")
def remove_coin(message):
    bot.send_message(message.chat.id, "Enter coin symbol to remove (e.g., BTCUSDT):")
    bot.register_next_step_handler(message, delete_coin)

def delete_coin(message):
    symbol = message.text.upper()
    coins = load_coins()
    if symbol in coins:
        coins.remove(symbol)
        save_coins(coins)
        bot.send_message(message.chat.id, f"🗑 {symbol} removed from My Coins")
    else:
        bot.send_message(message.chat.id, "⚠️ Coin not found in list.")

# ==== AUTO POLL MY COINS ====
def auto_poll_my_coins(interval=AUTO_POLL_INTERVAL):
    while True:
        try:
            coins = load_coins()
            if coins:
                for c in coins:
                    txt = get_signal(c, "5m") + "\n" + get_signal(c, "1h") + "\n" + get_signal(c, "1d")
                    bot.send_message(CHAT_ID, txt)
        except Exception as e:
            print(f"Error in auto-polling My Coins: {e}")
        time.sleep(interval)

# ==== REAL-TIME MOVER ALERTS ====
def mover_alerts(interval="1h", threshold=MOVER_ALERT_THRESHOLD):
    sent_alerts = set()
    while True:
        try:
            tickers = client.get_ticker_24hr()
            for t in tickers:
                if not t["symbol"].endswith("USDT"):
                    continue
                symbol = t["symbol"]
                change_pct = float(t["priceChangePercent"])
                if abs(change_pct) >= threshold and symbol not in sent_alerts:
                    alert = f"🚨 Sudden Mover Alert: {symbol}\nChange in last 24h: {round(change_pct,2)}%"
                    bot.send_message(CHAT_ID, alert)
                    sent_alerts.add(symbol)
            # Reset alerts every hour
            if len(sent_alerts) > 100:
                sent_alerts.clear()
        except Exception as e:
            print(f"Error in mover alerts: {e}")
        time.sleep(60)

threading.Thread(target=auto_poll_my_coins, daemon=True).start()
threading.Thread(target=mover_alerts, daemon=True).start()

print("🚀 Bot is live with webhook, auto-polling, and real-time mover alerts...")






