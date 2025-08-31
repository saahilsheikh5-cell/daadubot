import os
import sys
import json
import threading
import time
from flask import Flask
import telebot
from telebot import types
from binance.client import Client
import pandas as pd
import ta
import numpy as np

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
print("✅ Webhook removed. Bot ready for polling.")

# ==== FLASK SERVER ====
app = Flask("UltraSignalsBot")

@app.route("/")
def home():
    return "Ultra Signals Bot is running."

def run_flask():
    app.run(host="0.0.0.0", port=PORT)

threading.Thread(target=run_flask).start()

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

# ==== SIGNAL CALCULATION ====
def ultra_signal(symbol, interval="5m", lookback=100):
    """
    Returns a formatted ultra/strong signal or None if neutral
    """
    try:
        klines = client.get_klines(symbol=symbol, interval=interval, limit=lookback)
        df = pd.DataFrame(klines, columns=[
            "time", "o", "h", "l", "c", "v", "ct", "qav", "ntr", "tbbav", "tbqav", "ignore"
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
        df["ma200"] = df["c"].rolling(200).mean()
        df["ema20"] = df["c"].ewm(span=20, adjust=False).mean()
        last = df.iloc[-1]

        decision = None
        explanation = []

        # Ultra / Strong Buy Conditions
        if last["rsi"] < 30 and last["macd"] > last["macd_signal"] and last["c"] > last["ma50"]:
            decision = "✅ Ultra BUY"
            explanation.append("RSI oversold + MACD bullish + Above MA50")
        elif last["rsi"] < 40 and last["macd"] > last["macd_signal"] and last["c"] > last["ema20"]:
            decision = "✅ Strong BUY"
            explanation.append("RSI low + MACD bullish + Above EMA20")

        # Ultra / Strong Sell Conditions
        elif last["rsi"] > 70 and last["macd"] < last["macd_signal"] and last["c"] < last["ma50"]:
            decision = "❌ Ultra SELL"
            explanation.append("RSI overbought + MACD bearish + Below MA50")
        elif last["rsi"] > 60 and last["macd"] < last["macd_signal"] and last["c"] < last["ema20"]:
            decision = "❌ Strong SELL"
            explanation.append("RSI high + MACD bearish + Below EMA20")

        if not decision:
            return None  # ignore neutral

        # TP/SL calculation
        entry = last["c"]
        if "BUY" in decision:
            tp1 = round(entry * 1.01, 4)
            tp2 = round(entry * 1.02, 4)
            sl = round(entry * 0.99, 4)
        else:
            tp1 = round(entry * 0.99, 4)
            tp2 = round(entry * 0.98, 4)
            sl = round(entry * 1.01, 4)

        # Summary lines
        summary = "Market shows bullish momentum." if "BUY" in decision else "Market shows bearish momentum."

        signal_text = f"""
📊 Signal for {symbol} [{interval}]
Decision: {decision}
RSI: {round(last['rsi'],2)}
MACD: {round(last['macd'],4)} / Signal: {round(last['macd_signal'],4)}
Price: {last['c']}

Entry: {entry}
TP1: {tp1}
TP2: {tp2}
SL: {sl}
Suggested Leverage: x10
Notes: {" | ".join(explanation)}

💡 Summary: {summary}
        """
        return signal_text
    except Exception as e:
        return f"⚠️ Error fetching data for {symbol} {interval}: {e}"

# ==== MENUS ====
def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("📈 Signals", "➕ Add Coin", "➖ Remove Coin")
    kb.add("⏹ Stop Auto Signals")
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
        for tf in ["1m","5m","15m","1h","1d"]:
            txt = ultra_signal(c, tf)
            if txt:
                bot.send_message(message.chat.id, txt)

@bot.message_handler(func=lambda msg: msg.text == "🌍 All Coins")
def all_coins(message):
    tickers = [s for s in client.get_ticker_24hr() if s['symbol'].endswith('USDT')]
    tickers_sorted = sorted(tickers, key=lambda x: float(x['quoteVolume']), reverse=True)
    top_100 = [t['symbol'] for t in tickers_sorted[:100]]
    for coin in top_100:
        for tf in ["1m","5m","15m","1h"]:
            txt = ultra_signal(coin, tf)
            if txt:
                bot.send_message(message.chat.id, txt)

@bot.message_handler(func=lambda msg: msg.text == "🔎 Particular Coin")
def ask_coin(message):
    bot.send_message(message.chat.id, "Enter coin symbol (e.g., BTCUSDT):")
    bot.register_next_step_handler(message, particular_coin)

def particular_coin(message):
    symbol = message.text.upper()
    for tf in ["1m","5m","15m","1h","1d"]:
        txt = ultra_signal(symbol, tf)
        if txt:
            bot.send_message(message.chat.id, txt)

@bot.message_handler(func=lambda msg: msg.text == "🚀 Top Movers")
def top_movers(message):
    tickers = client.get_ticker_24hr()
    movers_sorted = sorted(tickers, key=lambda x: abs(float(x['priceChangePercent'])), reverse=True)
    top = movers_sorted[:20]
    txt = "🚀 Top Movers:\n"
    for t in top:
        txt += f"{t['symbol']}: {round(float(t['priceChangePercent']),2)}% change\n"
    bot.send_message(message.chat.id, txt)

@bot.message_handler(func=lambda msg: msg.text == "➕ Add Coin")
def add_coin(message):
    bot.send_message(message.chat.id, "Enter coin symbol to add (e.g., BTCUSDT):")
    bot.register_next_step_handler(message, save_new_coin)

def save_new_coin(message):
    symbol = message.text.upper()
    coins = load_coins()
    if symbol in coins:
        bot.send_message(message.chat.id, f"⚠️ {symbol} already exists.")
    else:
        coins.append(symbol)
        save_coins(coins)
        bot.send_message(message.chat.id, f"✅ {symbol} added to My Coins.")

@bot.message_handler(func=lambda msg: msg.text == "➖ Remove Coin")
def remove_coin(message):
    coins = load_coins()
    if not coins:
        bot.send_message(message.chat.id, "❌ No coins to remove.")
        return
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for c in coins:
        kb.add(c)
    kb.add("⬅️ Back")
    bot.send_message(message.chat.id, "Select coin to remove:", reply_markup=kb)
    bot.register_next_step_handler(message, remove_coin_step)

def remove_coin_step(message):
    symbol = message.text.upper()
    coins = load_coins()
    if symbol in coins:
        coins.remove(symbol)
        save_coins(coins)
        bot.send_message(message.chat.id, f"✅ {symbol} removed.", reply_markup=main_menu())
    else:
        bot.send_message(message.chat.id, "❌ Invalid coin.", reply_markup=main_menu())

# ==== AUTO SIGNALS THREAD ====
auto_signal_running = True

def auto_signals():
    while auto_signal_running:
        coins = load_coins()
        for coin in coins:
            txt = ultra_signal(coin, "1m")
            if txt:
                try:
                    bot.send_message(chat_id=os.getenv("TELEGRAM_CHAT_ID"), text=txt)
                except:
                    pass
        time.sleep(60)

threading.Thread(target=auto_signals, daemon=True).start()

@bot.message_handler(func=lambda msg: msg.text == "⏹ Stop Auto Signals")
def stop_auto(message):
    global auto_signal_running
    auto_signal_running = False
    bot.send_message(message.chat.id, "⏹ Auto signals stopped.", reply_markup=main_menu())

# ==== POLLING ====
print("✅ Bot polling started.")
bot.infinity_polling()








