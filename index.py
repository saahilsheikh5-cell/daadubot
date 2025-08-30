import os
import json
import time
import threading
import telebot
from telebot import types
from binance.client import Client
import pandas as pd
import ta

# ==== ENVIRONMENT VARS ====
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_SECRET")  # Use your env var name here
CHAT_ID = os.getenv("CHAT_ID")

if not TELEGRAM_TOKEN or not BINANCE_API_KEY or not BINANCE_API_SECRET or not CHAT_ID:
    raise RuntimeError("❌ Missing environment variables: TELEGRAM_TOKEN, BINANCE_API_KEY, BINANCE_API_SECRET, CHAT_ID")

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

# ==== SIGNAL FUNCTION ====
def get_signal(symbol, interval="5m", lookback=100):
    try:
        klines = client.get_klines(symbol=symbol, interval=interval, limit=lookback)
        df = pd.DataFrame(klines, columns=[
            "time","o","h","l","c","v","ct","qav","ntr","tbbav","tbqav","ignore"
        ])
        df["c"] = df["c"].astype(float)
        df["h"] = df["h"].astype(float)
        df["l"] = df["l"].astype(float)
        df["v"] = df["v"].astype(float)
        last = df.iloc[-1]

        # Indicators
        df["rsi"] = ta.momentum.RSIIndicator(df["c"], window=14).rsi()
        macd = ta.trend.MACD(df["c"])
        df["macd"] = macd.macd()
        df["macd_signal"] = macd.macd_signal()
        df["ema20"] = df["c"].ewm(span=20, adjust=False).mean()
        df["ema50"] = df["c"].ewm(span=50, adjust=False).mean()
        bb = ta.volatility.BollingerBands(df["c"], window=20, window_dev=2)
        df["bb_upper"] = bb.bollinger_hband()
        df["bb_lower"] = bb.bollinger_lband()
        df["adx"] = ta.trend.ADXIndicator(df["h"], df["l"], df["c"], window=14).adx()
        df["stoch_k"] = ta.momentum.StochasticOscillator(df["h"], df["l"], df["c"], window=14, smooth_window=3).stoch()
        df["stoch_d"] = df["stoch_k"].rolling(3).mean()

        score = 0
        notes = []

        # RSI
        if last["rsi"] < 35:
            score += 2; notes.append("RSI oversold")
        elif last["rsi"] > 65:
            score -= 2; notes.append("RSI overbought")

        # MACD
        if last["macd"] > last["macd_signal"]:
            score += 1; notes.append("MACD bullish")
        else:
            score -= 1; notes.append("MACD bearish")

        # EMA trend
        if last["ema20"] > last["ema50"]:
            score += 1; notes.append("EMA20 > EMA50")
        else:
            score -= 1; notes.append("EMA20 < EMA50")

        # Bollinger Bands
        if last["c"] < last["bb_lower"]:
            score += 1; notes.append("Near lower BB")
        elif last["c"] > last["bb_upper"]:
            score -= 1; notes.append("Near upper BB")

        # ADX
        if last["adx"] > 25:
            notes.append("Strong trend")

        # Stochastic
        if last["stoch_k"] < 20 and last["stoch_d"] < 20:
            score += 1; notes.append("Stochastic oversold")
        elif last["stoch_k"] > 80 and last["stoch_d"] > 80:
            score -= 1; notes.append("Stochastic overbought")

        # Volume spike
        avg_volume = df["v"].rolling(20).mean().iloc[-1]
        if last["v"] > 1.5 * avg_volume:
            score += 1; notes.append("Volume spike")

        # Candle pattern
        if df["c"].iloc[-2] < df["o"].iloc[-2] and last["c"] > last["o"]:
            score += 1; notes.append("Bullish engulfing")
        elif df["c"].iloc[-2] > df["o"].iloc[-2] and last["c"] < last["o"]:
            score -= 1; notes.append("Bearish engulfing")

        # Decision
        if score >=5:
            decision = "✅ STRONG BUY"
        elif score <=-5:
            decision = "❌ STRONG SELL"
        elif score>0:
            decision = "BUY"
        elif score<0:
            decision = "SELL"
        else:
            decision = "Neutral"

        entry = last["c"]
        tp1 = entry*1.01
        tp2 = entry*1.02
        sl = entry*0.99
        leverage = 10

        signal_text = f"""
📊 Signal for {symbol} [{interval}]
Decision: {decision}
RSI: {round(last['rsi'],2)}
MACD: {round(last['macd'],4)} / Signal: {round(last['macd_signal'],4)}
Price: {entry}

Entry: {round(entry,4)}
TP1: {round(tp1,4)}
TP2: {round(tp2,4)}
SL: {round(sl,4)}
Suggested Leverage: x{leverage}
Notes: {" | ".join(notes) if notes else "Mixed signals"}
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

# ==== BACKGROUND SIGNAL RUNNER ====
def background_signal_runner():
    while True:
        coins = load_coins()
        for coin in coins:
            signal_text = get_signal(coin, "5m")
            if "STRONG BUY" in signal_text or "STRONG SELL" in signal_text:
                bot.send_message(CHAT_ID, signal_text)
        time.sleep(60)  # every minute

threading.Thread(target=background_signal_runner, daemon=True).start()

# ==== RUN BOT ====
print("🚀 Bot is running...")
bot.infinity_polling()

