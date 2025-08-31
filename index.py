import os
import sys
import json
import threading
from flask import Flask, request
import telebot
from telebot import types
from binance.client import Client
import pandas as pd
import ta

# ==== ENVIRONMENT CHECK ====
required_env_vars = ["TELEGRAM_TOKEN", "BINANCE_API_KEY", "BINANCE_API_SECRET", "PORT"]
missing_vars = [var for var in required_env_vars if not os.getenv(var)]
if missing_vars:
    print(f"❌ Missing environment variables: {', '.join(missing_vars)}")
    sys.exit(1)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
PORT = int(os.getenv("PORT", 5000))

# ==== INIT BOT & BINANCE CLIENT ====
bot = telebot.TeleBot(TELEGRAM_TOKEN)
client = Client(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)

# ==== REMOVE ANY EXISTING WEBHOOK ====
bot.remove_webhook()
WEBHOOK_URL = f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME')}/{TELEGRAM_TOKEN}"
bot.set_webhook(url=WEBHOOK_URL)
print(f"🚀 Webhook set: {WEBHOOK_URL}")

# ==== FLASK SERVER ====
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is running"

@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def webhook():
    json_str = request.get_data().decode("utf-8")
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "ok", 200

# ==== COINS FILE ====
COINS_FILE = "my_coins.json"

def load_coins():
    if not os.path.exists(COINS_FILE):
        return []
    with open(COINS_FILE, "r") as f:
        return json.load(f)

def save_coins(coins):
    with open(COINS_FILE, "w") as f:
        json.dump(coins, f)

# ==== SIGNAL CALCULATION ====
def get_signal(symbol, interval="5m", lookback=100):
    try:
        klines = client.get_klines(symbol=symbol, interval=interval, limit=lookback)
        df = pd.DataFrame(klines, columns=[
            "time", "o", "h", "l", "c", "v", "ct", "qav", "ntr", "tbbav", "tbqav", "ignore"
        ])
        df["c"] = df["c"].astype(float)
        df["h"] = df["h"].astype(float)
        df["l"] = df["l"].astype(float)

        df["rsi"] = ta.momentum.RSIIndicator(df["c"], window=14).rsi()
        macd = ta.trend.MACD(df["c"])
        df["macd"] = macd.macd()
        df["macd_signal"] = macd.macd_signal()
        df["ma50"] = df["c"].rolling(50).mean()
        last = df.iloc[-1]

        # ==== TREND SCORING ====
        trend_score = 0
        trend_score += 1 if last["rsi"] < 30 else -1 if last["rsi"] > 70 else 0
        trend_score += 1 if last["macd"] > last["macd_signal"] else -1
        trend_score += 1 if last["c"] > last["ma50"] else -1

        if trend_score >= 2:
            decision = "✅ Strong BUY"
        elif trend_score <= -2:
            decision = "❌ Strong SELL"
        else:
            decision = "🔄 Neutral / Mixed"

        signal_text = f"""
📊 Signal for {symbol} [{interval}]
Decision: {decision}
RSI: {round(last['rsi'],2)}
MACD: {round(last['macd'],4)} / Signal: {round(last['macd_signal'],4)}
Price: {last['c']}

Entry: {round(last['c'],4)}
TP1: {round(last['c']*1.01,4)}
TP2: {round(last['c']*1.02,4)}
SL: {round(last['c']*0.99,4)}
Suggested Leverage: x10
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
        txt = "\n\n".join([get_signal(c, tf) for tf in ["5m","1h","1d"]])
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
    txt = "\n\n".join([get_signal(symbol, tf) for tf in ["5m","1h","1d"]])
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

# ==== RUN FLASK SERVER ====
threading.Thread(target=lambda: app.run(host="0.0.0.0", port=PORT)).start()
print("🚀 Bot is running via webhook...")







