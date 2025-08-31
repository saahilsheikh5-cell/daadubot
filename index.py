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

# ==== REMOVE EXISTING WEBHOOK ====
bot.remove_webhook()
print("✅ Webhook removed. Bot ready for polling.")

# ==== FLASK SERVER TO BIND PORT ====
app = Flask("")

@app.route("/")
def home():
    return "Ultra Signals Bot is running!"

def run_flask():
    app.run(host="0.0.0.0", port=PORT)

threading.Thread(target=run_flask).start()

# ==== COINS FILE ====
COINS_FILE = "my_coins.json"

# ==== AUTO SIGNAL CONTROL ====
auto_signal = True

# ==== HELPERS ====
def load_coins():
    if not os.path.exists(COINS_FILE):
        return []
    with open(COINS_FILE, "r") as f:
        return json.load(f)

def save_coins(coins):
    with open(COINS_FILE, "w") as f:
        json.dump(coins, f)

def calculate_atr(df, period=14):
    high = df["h"]
    low = df["l"]
    close = df["c"]
    tr1 = high - low
    tr2 = abs(high - close.shift())
    tr3 = abs(low - close.shift())
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(period).mean()
    return atr

def calculate_vwap(df):
    return (df['v'] * (df['h'] + df['l'] + df['c'])/3).cumsum() / df['v'].cumsum()

def ultra_signal(symbol, interval="5m", lookback=100):
    try:
        klines = client.get_klines(symbol=symbol, interval=interval, limit=lookback)
        df = pd.DataFrame(klines, columns=[
            "time","o","h","l","c","v","ct","qav","ntr","tbbav","tbqav","ignore"
        ])
        df["c"] = df["c"].astype(float)
        df["h"] = df["h"].astype(float)
        df["l"] = df["l"].astype(float)
        df["o"] = df["o"].astype(float)
        df["v"] = df["v"].astype(float)

        atr = calculate_atr(df).iloc[-1]
        vwap = calculate_vwap(df).iloc[-1]

        # Indicators
        df["rsi"] = ta.momentum.RSIIndicator(df["c"], window=14).rsi()
        macd = ta.trend.MACD(df["c"])
        df["macd"] = macd.macd()
        df["macd_signal"] = macd.macd_signal()
        df["ema50"] = df["c"].ewm(span=50, adjust=False).mean()
        df["ema200"] = df["c"].ewm(span=200, adjust=False).mean()
        df["adx"] = ta.trend.ADXIndicator(df["h"], df["l"], df["c"], window=14).adx()
        df["bb_high"] = ta.volatility.BollingerBands(df["c"], window=20, window_dev=2).bollinger_hband()
        df["bb_low"] = ta.volatility.BollingerBands(df["c"], window=20, window_dev=2).bollinger_lband()
        df["stoch_k"] = ta.momentum.StochasticOscillator(df["h"], df["l"], df["c"], window=14, smooth_window=3).stoch()
        df["stoch_d"] = ta.momentum.StochasticOscillator(df["h"], df["l"], df["c"], window=14, smooth_window=3).stoch_signal()
        df["obv"] = ta.volume.OnBalanceVolumeIndicator(df["c"], df["v"]).on_balance_volume()

        last = df.iloc[-1]
        decision = None
        notes = []

        # Ultra-Pro logic combining all indicators
        if (last["rsi"] < 30 and last["macd"] > last["macd_signal"] and last["c"] > last["ema50"]
            and last["adx"] > 25 and last["stoch_k"] < 20 and last["obv"] > df["obv"].mean() and last["c"] > vwap):
            decision = "✅ Ultra BUY"
            notes.append("Ultra bullish conditions: RSI+MACD+EMA+ADX+Stoch+OBV+VWAP")
        elif (last["rsi"] > 70 and last["macd"] < last["macd_signal"] and last["c"] < last["ema50"]
              and last["adx"] > 25 and last["stoch_k"] > 80 and last["obv"] < df["obv"].mean() and last["c"] < vwap):
            decision = "❌ Ultra SELL"
            notes.append("Ultra bearish conditions: RSI+MACD+EMA+ADX+Stoch+OBV+VWAP")
        elif last["rsi"] < 40 and last["macd"] > last["macd_signal"] and last["c"] > last["ema50"]:
            decision = "✅ Strong BUY"
            notes.append("Strong bullish conditions")
        elif last["rsi"] > 60 and last["macd"] < last["macd_signal"] and last["c"] < last["ema50"]:
            decision = "❌ Strong SELL"
            notes.append("Strong bearish conditions")
        else:
            return None

        # TP/SL based on ATR
        if "BUY" in decision:
            entry = last["c"]
            tp1 = entry + 0.5 * atr
            tp2 = entry + 1.0 * atr
            sl = entry - 0.5 * atr
            summary = f"Market is bullish; price above EMA50/EMA200 with positive trend."
        else:
            entry = last["c"]
            tp1 = entry - 0.5 * atr
            tp2 = entry - 1.0 * atr
            sl = entry + 0.5 * atr
            summary = f"Market is bearish; price below EMA50/EMA200 with negative trend."

        text = f"""
📊 Signal for {symbol} [{interval}]
Decision: {decision}
RSI: {round(last['rsi'],2)}
MACD: {round(last['macd'],4)} / Signal: {round(last['macd_signal'],4)}
Price: {round(last['c'],4)}

Entry: {round(entry,4)}
TP1: {round(tp1,4)}
TP2: {round(tp2,4)}
SL: {round(sl,4)}
Suggested Leverage: x10
Notes: {" | ".join(notes)}
Summary: {summary}
        """
        return text

    except Exception as e:
        return f"⚠️ Error fetching data for {symbol} [{interval}]: {e}"

# ==== TOP MOVERS ====
def top_movers(interval="5m", top_n=5):
    try:
        tickers = [t["symbol"] for t in client.get_all_tickers() if t["symbol"].endswith("USDT")]
        movers = []
        for s in tickers:
            df = pd.DataFrame(client.get_klines(symbol=s, interval=interval, limit=2),
                              columns=["time","o","h","l","c","v","ct","qav","ntr","tbbav","tbqav","ignore"])
            df["c"] = df["c"].astype(float)
            change = (df["c"].iloc[-1] - df["c"].iloc[-2]) / df["c"].iloc[-2] * 100
            movers.append((s, change))
        movers.sort(key=lambda x: abs(x[1]), reverse=True)
        text = "🚀 Top Movers:\n"
        for s, ch in movers[:top_n]:
            text += f"{s}: {round(ch,2)}% change\n"
        return text
    except:
        return "⚠️ Unable to fetch top movers."

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

@bot.message_handler(func=lambda msg: msg.text == "➕ Add Coin")
def add_coin(message):
    msg = bot.send_message(message.chat.id, "Enter coin symbol to add (e.g., BTCUSDT):")
    bot.register_next_step_handler(msg, save_coin)

def save_coin(msg):
    coin = msg.text.strip().upper()
    coins = load_coins()
    if coin not in coins:
        coins.append(coin)
        save_coins(coins)
        bot.send_message(msg.chat.id, f"✅ {coin} added to My Coins")
    else:
        bot.send_message(msg.chat.id, f"⚠️ {coin} already in My Coins.")

@bot.message_handler(func=lambda msg: msg.text == "➖ Remove Coin")
def remove_coin_btn(message):
    msg = bot.send_message(message.chat.id, "Enter coin symbol to remove:")
    bot.register_next_step_handler(msg, remove_coin)

def remove_coin(msg):
    coin = msg.text.strip().upper()
    coins = load_coins()
    if coin in coins:
        coins.remove(coin)
        save_coins(coins)
        bot.send_message(msg.chat.id, f"✅ {coin} removed from My Coins")
    else:
        bot.send_message(msg.chat.id, f"⚠️ {coin} not found in My Coins.")

@bot.message_handler(func=lambda msg: msg.text == "📈 Signals")
def show_signals_menu(message):
    bot.send_message(message.chat.id, "Choose a signal option:", reply_markup=signals_menu())

@bot.message_handler(func=lambda msg: msg.text in ["💼 My Coins", "🌍 All Coins", "🔎 Particular Coin"])
def signal_handler(message):
    coins = load_coins()
    if message.text == "💼 My Coins":
        if not coins:
            bot.send_message(message.chat.id, "⚠️ No coins added.")
            return
        for c in coins:
            txt = ultra_signal(c, "5m")
            if txt:
                bot.send_message(message.chat.id, txt)
    elif message.text == "🌍 All Coins":
        tickers = [t["symbol"] for t in client.get_all_tickers() if t["symbol"].endswith("USDT")]
        for c in tickers:
            txt = ultra_signal(c, "5m")
            if txt:
                bot.send_message(message.chat.id, txt)
    elif message.text == "🔎 Particular Coin":
        msg = bot.send_message(message.chat.id, "Enter coin symbol (e.g., BTCUSDT):")
        bot.register_next_step_handler(msg, particular_coin_signal)

def particular_coin_signal(msg):
    c = msg.text.strip().upper()
    txt = ultra_signal(c, "5m")
    if txt:
        bot.send_message(msg.chat.id, txt)
    else:
        bot.send_message(msg.chat.id, f"⚠️ No strong signals for {c}.")

@bot.message_handler(func=lambda msg: msg.text == "🚀 Top Movers")
def top_movers_btn(message):
    txt = top_movers("5m")
    bot.send_message(message.chat.id, txt)

@bot.message_handler(func=lambda msg: msg.text == "⏹ Stop Auto Signals")
def stop_auto(message):
    global auto_signal
    auto_signal = False
    bot.send_message(message.chat.id, "⏹ Auto signals stopped.")

# ==== AUTO SIGNALS THREAD ====
def auto_signal_thread(chat_id):
    global auto_signal
    while auto_signal:
        coins = load_coins()
        for c in coins:
            txt = ultra_signal(c, "5m")
            if txt:
                bot.send_message(chat_id, txt)
        time.sleep(300)  # Every 5 mins

@bot.message_handler(commands=["autosignal"])
def start_auto(message):
    global auto_signal
    auto_signal = True
    bot.send_message(message.chat.id, "🚀 Auto signals started.")
    threading.Thread(target=auto_signal_thread, args=(message.chat.id,)).start()

# ==== RUN BOT ====
bot.infinity_polling()








