import os
import telebot
import requests
import pandas as pd
import numpy as np
from flask import Flask, request
from telebot import types

# =========================
# CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not set in environment variables!")

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

RENDER_URL = "https://daadubot.onrender.com"

# =========================
# COIN STORAGE
# =========================
user_coins = {}

# =========================
# HELPER: TECHNICAL SIGNALS
# =========================
def get_signal(prices):
    if len(prices) < 14:
        return "Neutral"

    sma = np.mean(prices[-14:])
    last = prices[-1]

    if last > sma * 1.03:
        return "Ultra Buy ✅"
    elif last > sma * 1.01:
        return "Strong Buy 🟢"
    elif last > sma:
        return "Buy 🔼"
    elif last < sma * 0.97:
        return "Ultra Sell ❌"
    elif last < sma * 0.99:
        return "Strong Sell 🔴"
    elif last < sma:
        return "Sell 🔽"
    else:
        return "Neutral ⚪"

def get_technical_analysis(symbol, timeframe="1h"):
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol.upper()}USDT&interval={timeframe}&limit=50"
        data = requests.get(url, timeout=10).json()
        closes = [float(c[4]) for c in data]
        signal = get_signal(closes)
        return f"{symbol.upper()} ({timeframe}) → {signal}"
    except Exception as e:
        return f"Error fetching {symbol} ({timeframe}): {e}"

# =========================
# MENU HANDLERS
# =========================
@bot.message_handler(commands=["start"])
def start(message):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("➕ Add Coin", "📂 My Coins")
    markup.row("📊 Technical Analysis", "🚀 Movers")
    markup.row("⚡ Auto Signals", "🔧 Settings")
    bot.send_message(message.chat.id, "👋 Welcome to DaaduBot! Select an option:", reply_markup=markup)

@bot.message_handler(func=lambda msg: msg.text == "➕ Add Coin")
def add_coin(message):
    bot.send_message(message.chat.id, "Send me the coin symbol (e.g., BTC, ETH):")
    bot.register_next_step_handler(message, save_coin)

def save_coin(message):
    coin = message.text.strip().upper()
    user_coins.setdefault(message.chat.id, []).append(coin)
    bot.send_message(message.chat.id, f"✅ {coin} added to your list.")

@bot.message_handler(func=lambda msg: msg.text == "📂 My Coins")
def my_coins(message):
    coins = user_coins.get(message.chat.id, [])
    if not coins:
        bot.send_message(message.chat.id, "You have no coins added yet.")
        return

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for coin in coins:
        markup.row(coin)
    markup.row("⬅️ Back")
    bot.send_message(message.chat.id, "📂 Your coins:", reply_markup=markup)

@bot.message_handler(func=lambda msg: msg.text in user_coins.get(msg.chat.id, []))
def coin_analysis(message):
    coin = message.text
    timeframes = ["1m", "5m", "15m", "1h", "4h", "1d"]
    response = [get_technical_analysis(coin, tf) for tf in timeframes]
    bot.send_message(message.chat.id, "\n".join(response))

@bot.message_handler(func=lambda msg: msg.text == "📊 Technical Analysis")
def tech_analysis(message):
    bot.send_message(message.chat.id, "Send me the coin symbol for analysis:")
    bot.register_next_step_handler(message, tech_analysis_run)

def tech_analysis_run(message):
    coin = message.text.strip().upper()
    timeframes = ["1m", "5m", "15m", "1h", "4h", "1d"]
    response = [get_technical_analysis(coin, tf) for tf in timeframes]
    bot.send_message(message.chat.id, "\n".join(response))

@bot.message_handler(func=lambda msg: msg.text == "🚀 Movers")
def movers(message):
    try:
        url = "https://api.binance.com/api/v3/ticker/24hr"
        data = requests.get(url, timeout=10).json()
        usdt_pairs = [x for x in data if x["symbol"].endswith("USDT")]
        movers = sorted(usdt_pairs, key=lambda x: float(x["priceChangePercent"]), reverse=True)[:5]

        response = "🚀 Top 5 Movers (24h):\n"
        for m in movers:
            response += f"{m['symbol']}: {m['priceChangePercent']}%\n"
        bot.send_message(message.chat.id, response)
    except Exception as e:
        bot.send_message(message.chat.id, f"Error fetching movers: {e}")

@bot.message_handler(func=lambda msg: msg.text == "⚡ Auto Signals")
def auto_signals(message):
    bot.send_message(message.chat.id, "⚡ Auto signals will scan your coins soon... (feature placeholder).")

@bot.message_handler(func=lambda msg: msg.text == "⬅️ Back")
def go_back(message):
    start(message)

# =========================
# FLASK ROUTES (WEBHOOK)
# =========================
@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    update = request.stream.read().decode("utf-8")
    bot.process_new_updates([telebot.types.Update.de_json(update)])
    return "OK", 200

@app.route("/health", methods=["GET"])
def health():
    return "Bot is alive ✅", 200

# =========================
# AUTO SET WEBHOOK
# =========================
with app.app_context():
    wh_url = f"{RENDER_URL}/{BOT_TOKEN}"
    r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?url={wh_url}")
    print("Webhook set:", r.json())

# =========================
# MAIN ENTRY
# =========================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
