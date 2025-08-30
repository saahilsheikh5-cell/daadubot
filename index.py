 import os
import json
import time
import threading
import telebot
from telebot import types
from binance.client import Client
import pandas as pd
import numpy as np
import ta

# ==== ENVIRONMENT VARS ====
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_SECRET")  # must match env var
CHAT_ID = os.getenv("CHAT_ID")

if not TELEGRAM_TOKEN or not BINANCE_API_KEY or not BINANCE_API_SECRET or not CHAT_ID:
    raise RuntimeError("Please set TELEGRAM_TOKEN, BINANCE_API_KEY, BINANCE_SECRET, CHAT_ID env vars.")

bot = telebot.TeleBot(TELEGRAM_TOKEN)
client = Client(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)

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

# ==== STRONG SIGNAL LOGIC ====
def get_signal(symbol, interval="5m", lookback=100):
    try:
        klines = client.get_klines(symbol=symbol, interval=interval, limit=lookback)
        df = pd.DataFrame(klines, columns=[
            "time", "o", "h", "l", "c", "v", "ct", "qav", "ntr", "tbbav", "tbqav", "ignore"
        ])
        df["c"] = df["c"].astype(float)
        df["h"] = df["h"].astype(float)
        df["l"] = df["l"].astype(float)
        df["v"] = df["v"].astype(float)

        # === Indicators ===
        df["rsi"] = ta.momentum.RSIIndicator(df["c"], window=14).rsi()
        macd = ta.trend.MACD(df["c"])
        df["macd"] = macd.macd()
        df["macd_signal"] = macd.macd_signal()
        df["adx"] = ta.trend.ADXIndicator(df["h"], df["l"], df["c"]).adx()
        df["stoch_k"] = ta.momentum.StochasticOscillator(df["h"], df["l"], df["c"]).stoch()
        df["ema20"] = df["c"].ewm(span=20, adjust=False).mean()
        df["ema50"] = df["c"].ewm(span=50, adjust=False).mean()
        bb = ta.volatility.BollingerBands(df["c"], window=20, window_dev=2)
        df["bb_high"] = bb.bollinger_hband()
        df["bb_low"] = bb.bollinger_lband()
        df["ma200"] = df["c"].rolling(200).mean()
        df["vol_mean"] = df["v"].rolling(20).mean()
        last = df.iloc[-1]

        # === Volume filter for small coins ===
        if last["v"] < 1000:  # filter tiny volumes
            return None

        # === Strong signal conditions ===
        strong_buy = (last["rsi"] < 30 and last["macd"] > last["macd_signal"]
                      and last["adx"] > 25 and last["c"] > last["ema20"]
                      and last["c"] > last["bb_low"])
        strong_sell = (last["rsi"] > 70 and last["macd"] < last["macd_signal"]
                       and last["adx"] > 25 and last["c"] < last["ema20"]
                       and last["c"] < last["bb_high"])

        decision = "Neutral"
        explanation = []

        if strong_buy:
            decision = "✅ Strong BUY"
            explanation.append("RSI oversold + MACD bullish + ADX trending + Above EMA20 + Above BB low")
        elif strong_sell:
            decision = "❌ Strong SELL"
            explanation.append("RSI overbought + MACD bearish + ADX trending + Below EMA20 + Below BB high")
        else:
            explanation.append("Mixed signals")

        signal_text = f"""
📊 Signal for {symbol} [{interval}]
Decision: {decision}
RSI: {round(last['rsi'],2)}
MACD: {round(last['macd'],4)} / Signal: {round(last['macd_signal'],4)}
ADX: {round(last['adx'],2)}
Stochastic K: {round(last['stoch_k'],2)}
EMA20: {round(last['ema20'],2)} | EMA50: {round(last['ema50'],2)}
BB High/Low: {round(last['bb_high'],2)} / {round(last['bb_low'],2)}
Price: {round(last['c'],4)}
Volume: {round(last['v'],2)}

Entry: {round(last['c'],4)}
TP1: {round(last['c']*1.01,4)}
TP2: {round(last['c']*1.02,4)}
SL: {round(last['c']*0.99,4)}
Suggested Leverage: x10
Notes: {" | ".join(explanation)}
        """
        return signal_text
    except Exception as e:
        return f"⚠️ Error fetching data for {symbol} {interval}: {e}"

# ==== CONTINUOUS BACKGROUND SIGNALS (only strong alerts) ====
def continuous_signals():
    while True:
        coins = load_coins()
        for c in coins:
            txt = get_signal(c, "5m")
            if txt and ("Strong BUY" in txt or "Strong SELL" in txt):
                bot.send_message(CHAT_ID, txt)
        time.sleep(60)  # check every minute

threading.Thread(target=continuous_signals, daemon=True).start()

# ==== TELEGRAM MENUS ====
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
        txt = get_signal(c, "5m")
        if txt:
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

# ==== RUN BOT ====
print("🚀 Bot is running with strong signal alerts...")
bot.infinity_polling()

