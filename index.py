import os
import json
import threading
import time
from flask import Flask, request
import telebot
from telebot import types
from binance.client import Client
import pandas as pd
import ta

# ==== ENVIRONMENT CHECK ====
required_env_vars = ["TELEGRAM_TOKEN", "BINANCE_API_KEY", "BINANCE_API_SECRET", "PORT", "TELEGRAM_CHAT_ID"]
missing_vars = [var for var in required_env_vars if not os.getenv(var)]
if missing_vars:
    print(f"❌ Missing environment variables: {', '.join(missing_vars)}")
    exit(1)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
PORT = int(os.getenv("PORT", 5000))
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID"))

# ==== INIT BOT & BINANCE CLIENT ====
bot = telebot.TeleBot(TELEGRAM_TOKEN)
client = Client(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)

# ==== FLASK SERVER FOR WEBHOOK ====
app = Flask(__name__)
WEBHOOK_URL = f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME')}/{TELEGRAM_TOKEN}"

bot.remove_webhook()
bot.set_webhook(url=WEBHOOK_URL)
print(f"🚀 Webhook set: {WEBHOOK_URL}")

@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def webhook():
    json_str = request.get_data().decode("utf-8")
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "OK", 200

@app.route("/")
def home():
    return "Bot is running"

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

# ==== SIGNAL GENERATOR ====
def calculate_signal(symbol, interval="5m", lookback=100):
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

        decision = None
        tp1 = tp2 = sl = last["c"]
        if last["rsi"] < 30 and last["macd"] > last["macd_signal"] and last["c"] > last["ma50"]:
            decision = "✅ Ultra BUY"
            tp1 = last["c"] * 1.01
            tp2 = last["c"] * 1.02
            sl  = last["c"] * 0.99
        elif last["rsi"] < 40 and last["macd"] > last["macd_signal"] and last["c"] > last["ma50"]:
            decision = "✅ Strong BUY"
            tp1 = last["c"] * 1.008
            tp2 = last["c"] * 1.015
            sl  = last["c"] * 0.995
        elif last["rsi"] > 70 and last["macd"] < last["macd_signal"] and last["c"] < last["ma50"]:
            decision = "❌ Ultra SELL"
            tp1 = last["c"] * 0.99
            tp2 = last["c"] * 0.98
            sl  = last["c"] * 1.01
        elif last["rsi"] > 60 and last["macd"] < last["macd_signal"] and last["c"] < last["ma50"]:
            decision = "❌ Strong SELL"
            tp1 = last["c"] * 0.992
            tp2 = last["c"] * 0.985
            sl  = last["c"] * 1.005
        else:
            return None  # skip neutral

        return {
            "decision": decision,
            "entry": last["c"],
            "tp1": tp1,
            "tp2": tp2,
            "sl": sl,
            "rsi": last["rsi"],
            "macd": last["macd"],
            "macd_signal": last["macd_signal"]
        }
    except:
        return None

def multi_tf_signal(symbol):
    signals = {}
    for tf in ["5m", "1h", "1d"]:
        sig = calculate_signal(symbol, tf)
        if sig:
            signals[tf] = sig

    # Only send if at least 2 timeframes agree
    buy_count = sum(1 for s in signals.values() if "BUY" in s["decision"])
    sell_count = sum(1 for s in signals.values() if "SELL" in s["decision"])

    if buy_count >= 2 or sell_count >= 2:
        primary_tf = max(signals.keys(), key=lambda x: ["5m","1h","1d"].index(x))
        s = signals[primary_tf]
        return f"""
📊 Signal for {symbol} [{primary_tf}] (Confirmed)
Decision: {s['decision']}
RSI: {round(s['rsi'],2)}
MACD: {round(s['macd'],4)} / Signal: {round(s['macd_signal'],4)}
Price: {round(s['entry'],4)}

Entry: {round(s['entry'],4)}
TP1: {round(s['tp1'],4)}
TP2: {round(s['tp2'],4)}
SL: {round(s['sl'],4)}
Suggested Leverage: x10
        """
    return None

# ==== MENUS ====
def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("📈 Signals", "➕ Add Coin", "➖ Remove Coin", "⏹ Stop Auto Signals")
    return kb

def signals_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("💼 My Coins", "🌍 All Coins")
    kb.add("🔎 Particular Coin", "🚀 Top Movers")
    kb.add("⬅️ Back")
    return kb

# ==== BOT HANDLERS ====
AUTO_SIGNAL = True
TOP_MOVER_CACHE = {}

@bot.message_handler(commands=["start"])
def start(message):
    bot.send_message(message.chat.id, "🤖 Welcome to Ultra Signals Bot!", reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text == "⬅️ Back")
def back_btn(message):
    bot.send_message(message.chat.id, "🔙 Main Menu", reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text == "📈 Signals")
def signals_btn(message):
    bot.send_message(message.chat.id, "Choose a signal option:", reply_markup=signals_menu())

@bot.message_handler(func=lambda msg: msg.text == "💼 My Coins")
def my_coins(message):
    coins = load_coins()
    if not coins:
        bot.send_message(message.chat.id, "❌ No coins added yet. Use ➕ Add Coin.")
        return
    for c in coins:
        txt = multi_tf_signal(c)
        if txt:
            bot.send_message(message.chat.id, txt)

@bot.message_handler(func=lambda msg: msg.text == "🌍 All Coins")
def all_coins(message):
    tickers = [s["symbol"] for s in client.get_all_tickers() if s["symbol"].endswith("USDT")]
    for c in tickers[:10]:
        txt = multi_tf_signal(c)
        if txt:
            bot.send_message(message.chat.id, txt)

@bot.message_handler(func=lambda msg: msg.text == "🔎 Particular Coin")
def ask_coin(message):
    bot.send_message(message.chat.id, "Enter coin symbol (e.g., BTCUSDT):")
    bot.register_next_step_handler(message, particular_coin)

def particular_coin(message):
    symbol = message.text.upper()
    txt = multi_tf_signal(symbol)
    if txt:
        bot.send_message(message.chat.id, txt)
    else:
        bot.send_message(message.chat.id, "⚠️ No strong signals for this coin.")

@bot.message_handler(func=lambda msg: msg.text == "🚀 Top Movers")
def top_movers(message):
    tickers = client.get_ticker_24hr()
    sorted_tickers = sorted(tickers, key=lambda x: abs(float(x["priceChangePercent"])), reverse=True)
    top = [t["symbol"] for t in sorted_tickers if t["symbol"].endswith("USDT")][:5]
    for c in top:
        txt = multi_tf_signal(c)
        if txt:
            bot.send_message(message.chat.id, txt)

# ==== ADD / REMOVE COINS ====
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
def remove_coin_btn(message):
    coins = load_coins()
    if not coins:
        bot.send_message(message.chat.id, "❌ No coins to remove.")
        return
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for c in coins:
        kb.add(c)
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
        bot.send_message(message.chat.id, "❌ Coin not in list.", reply_markup=main_menu())

@bot.message_handler(func=lambda msg: msg.text == "⏹ Stop Auto Signals")
def stop_auto_signals(message):
    global AUTO_SIGNAL
    AUTO_SIGNAL = False
    bot.send_message(message.chat.id, "⏹ Auto signals stopped.", reply_markup=main_menu())

# ==== AUTO SIGNAL THREAD ====
def auto_signal_worker():
    global TOP_MOVER_CACHE
    while AUTO_SIGNAL:
        coins = load_coins()
        # Regular signals
        for c in coins:
            txt = multi_tf_signal(c)
            if txt:
                bot.send_message(TELEGRAM_CHAT_ID, txt)

        # Real-time top movers (>1% change in 5m)
        tickers = client.get_ticker_24hr()
        for t in tickers:
            sym = t["symbol"]
            if not sym.endswith("USDT"):
                continue
            price_change = float(t["priceChangePercent"])
            prev_change = TOP_MOVER_CACHE.get(sym, 0)
            if abs(price_change - prev_change) >= 1:
                TOP_MOVER_CACHE[sym] = price_change
                txt = multi_tf_signal(sym)
                if txt:
                    bot.send_message(TELEGRAM_CHAT_ID, f"🚨 Top Mover Alert:\n{txt}")
        time.sleep(300)  # every 5 minutes

threading.Thread(target=auto_signal_worker, daemon=True).start()

# ==== RUN FLASK ====
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)








