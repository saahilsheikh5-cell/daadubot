import os
import json
import requests
import pandas as pd
import numpy as np
from flask import Flask, request
import telebot
from telebot import types
from datetime import datetime

API_KEY = os.getenv("TELEGRAM_API_KEY")
bot = telebot.TeleBot(API_KEY)
app = Flask(__name__)

COINS_FILE = "coins.json"

# ---------- Utility: persistent coin storage ----------
def load_coins():
    if not os.path.exists(COINS_FILE):
        return []
    with open(COINS_FILE, "r") as f:
        try:
            return json.load(f)
        except:
            return []

def save_coins(coins):
    with open(COINS_FILE, "w") as f:
        json.dump(coins, f)

# ---------- Binance Kline Fetch ----------
def get_klines(symbol, interval, limit=100):
    url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}"
    r = requests.get(url)
    data = r.json()
    df = pd.DataFrame(data, columns=[
        "time","o","h","l","c","v","ct","qv","tn","tb","tq","ig"
    ])
    df["c"] = df["c"].astype(float)
    df["h"] = df["h"].astype(float)
    df["l"] = df["l"].astype(float)
    df["o"] = df["o"].astype(float)
    df["v"] = df["v"].astype(float)
    return df

# ---------- Indicators ----------
def rsi(series, period=14):
    delta = series.diff()
    gain = np.where(delta > 0, delta, 0)
    loss = np.where(delta < 0, -delta, 0)
    avg_gain = pd.Series(gain).rolling(period).mean()
    avg_loss = pd.Series(loss).rolling(period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def ema(series, period=20):
    return series.ewm(span=period).mean()

def macd(series, fast=12, slow=26, signal=9):
    fast_ema = series.ewm(span=fast).mean()
    slow_ema = series.ewm(span=slow).mean()
    macd_line = fast_ema - slow_ema
    signal_line = macd_line.ewm(span=signal).mean()
    return macd_line, signal_line

def atr(df, period=14):
    high_low = df["h"] - df["l"]
    high_close = np.abs(df["h"] - df["c"].shift())
    low_close = np.abs(df["l"] - df["c"].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = ranges.max(axis=1)
    return true_range.rolling(period).mean()

# ---------- Analysis ----------
def analyze(symbol, interval):
    try:
        df = get_klines(symbol, interval, 100)
        price = df["c"].iloc[-1]
        rsi_val = rsi(df["c"]).iloc[-1]
        ema20 = ema(df["c"], 20).iloc[-1]
        ema50 = ema(df["c"], 50).iloc[-1]
        macd_line, signal_line = macd(df["c"])
        macd_val = macd_line.iloc[-1]
        signal_val = signal_line.iloc[-1]
        atr_val = atr(df).iloc[-1]
        vol = df["v"].iloc[-1]

        # Action logic
        action = "⚠️ Hold"
        entry = price
        sl = None
        tp1 = None
        tp2 = None
        leverage = "5x-10x"

        if rsi_val < 30 and macd_val > signal_val and ema20 > ema50:
            action = "✅ Long"
            sl = round(price - 2 * atr_val, 4)
            tp1 = round(price + 2 * atr_val, 4)
            tp2 = round(price + 4 * atr_val, 4)
            leverage = "10x-20x"
        elif rsi_val > 70 and macd_val < signal_val and ema20 < ema50:
            action = "🔻 Short"
            sl = round(price + 2 * atr_val, 4)
            tp1 = round(price - 2 * atr_val, 4)
            tp2 = round(price - 4 * atr_val, 4)
            leverage = "10x-20x"

        msg = f"""📊 {symbol} | {interval}
Price: {price:.4f}
RSI(14): {rsi_val:.2f}
EMA20/50: {"Bullish" if ema20>ema50 else "Bearish"}  (20={ema20:.4f}, 50={ema50:.4f})
MACD: {"Bullish" if macd_val>signal_val else "Bearish"}  (MACD={macd_val:.5f}, Signal={signal_val:.5f})
ATR(14): {atr_val:.6f}
Vol(last): {vol:.2f}

🎯 Action: {action}
Entry: {entry:.4f}
Stop Loss: {sl}
TP1: {tp1}
TP2: {tp2}
Leverage: {leverage}
"""
        return msg
    except Exception as e:
        return f"❌ Error analyzing {symbol}: {e}"

# ---------- Bot Handlers ----------
def main_menu(chat_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("➕ Add Coin", "➖ Remove Coin")
    markup.add("📊 My Coins", "📈 Top Movers")
    markup.add("📡 Signals", "⚙️ Signal Settings")
    markup.add("♻️ Reset Settings")
    bot.send_message(chat_id, "Main Menu:", reply_markup=markup)

@bot.message_handler(commands=["start"])
def start(message):
    bot.send_message(message.chat.id, "✅ Bot is live and working on Render!")
    main_menu(message.chat.id)

@bot.message_handler(func=lambda msg: True)
def handle_message(message):
    text = message.text
    chat_id = message.chat.id
    coins = load_coins()

    # ---- Menu Handling ----
    if text == "➕ Add Coin":
        msg = bot.send_message(chat_id, "Send symbol (e.g. BTCUSDT):")
        bot.register_next_step_handler(msg, add_coin)
    elif text == "➖ Remove Coin":
        if not coins:
            bot.send_message(chat_id, "⚠️ No coins added.")
            return
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        for c in coins: markup.add(c)
        msg = bot.send_message(chat_id, "Choose coin to remove:", reply_markup=markup)
        bot.register_next_step_handler(msg, remove_coin)
    elif text == "📊 My Coins":
        if not coins:
            bot.send_message(chat_id, "⚠️ No coins added.")
        else:
            markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
            for c in coins: markup.add(c)
            msg = bot.send_message(chat_id, "Select coin:", reply_markup=markup)
            bot.register_next_step_handler(msg, show_timeframes)
    elif text == "📈 Top Movers":
        bot.send_message(chat_id, "🚀 Feature under dev: Top movers list here.")
    elif text == "📡 Signals":
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add("📊 My Coins Signals", "📈 All Coins Signals", "🔎 Any Coin")
        bot.send_message(chat_id, "Choose signal type:", reply_markup=markup)
    elif text == "📊 My Coins Signals":
        bot.send_message(chat_id, "📡 Signals started for your coins...")
        # Future: add scheduling loop
    elif text == "📈 All Coins Signals":
        bot.send_message(chat_id, "📡 Signals started for Binance top coins...")
    elif text == "🔎 Any Coin":
        msg = bot.send_message(chat_id, "Send coin symbol (e.g. ETHUSDT):")
        bot.register_next_step_handler(msg, any_coin)
    elif text == "⚙️ Signal Settings":
        bot.send_message(chat_id, "Current Settings\nRSI Buy: 30\nRSI Sell: 70\nValidity: 15 min\nActive subscription: all | - | 1m")
    elif text == "♻️ Reset Settings":
        save_coins([])
        bot.send_message(chat_id, "✅ All settings reset.")
    else:
        bot.send_message(chat_id, f"You said: {text}")

def add_coin(message):
    coin = message.text.upper()
    coins = load_coins()
    if coin not in coins:
        coins.append(coin)
        save_coins(coins)
        bot.send_message(message.chat.id, f"✅ {coin} added.")
    else:
        bot.send_message(message.chat.id, f"{coin} already exists.")

def remove_coin(message):
    coin = message.text.upper()
    coins = load_coins()
    if coin in coins:
        coins.remove(coin)
        save_coins(coins)
        bot.send_message(message.chat.id, f"❌ {coin} removed.")
    else:
        bot.send_message(message.chat.id, f"{coin} not found.")

def show_timeframes(message):
    coin = message.text.upper()
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for tf in ["1m","5m","15m","1h","4h","1d"]:
        markup.add(tf)
    msg = bot.send_message(message.chat.id, f"Select timeframe for {coin}:", reply_markup=markup)
    bot.register_next_step_handler(msg, lambda m: show_analysis(m, coin))

def show_analysis(message, coin):
    tf = message.text
    result = analyze(coin, tf)
    bot.send_message(message.chat.id, result)

def any_coin(message):
    coin = message.text.upper()
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for tf in ["1m","5m","15m","1h","4h","1d"]:
        markup.add(tf)
    msg = bot.send_message(message.chat.id, f"Select timeframe for {coin}:", reply_markup=markup)
    bot.register_next_step_handler(msg, lambda m: show_analysis(m, coin))

# ---------- Flask webhook ----------
@app.route("/" + API_KEY, methods=["POST"])
def webhook():
    json_str = request.get_data().decode("UTF-8")
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "ok", 200

@app.route("/")
def index():
    return "Bot is running!", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))




