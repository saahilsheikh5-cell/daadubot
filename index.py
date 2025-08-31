import os
import json
import traceback
import pandas as pd
import numpy as np
from flask import Flask, request
import telebot
from telebot import types
from binance.client import Client

# =========================
# ENVIRONMENT VARIABLES
# =========================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")  # Correct case
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")   # Your Render service URL
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY")
BINANCE_API_SECRET = os.environ.get("BINANCE_API_SECRET")

bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=True)
app = Flask(__name__)
client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)

# =========================
# JSON STORAGE
# =========================
SETTINGS_FILE = "settings.json"

def load_settings():
    if not os.path.exists(SETTINGS_FILE):
        return {"coins": []}
    with open(SETTINGS_FILE, "r") as f:
        return json.load(f)

def save_settings(settings):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f)

# =========================
# BOT COMMANDS
# =========================
@bot.message_handler(commands=["start"])
def start(message):
    markup = types.InlineKeyboardMarkup()
    markup.row(types.InlineKeyboardButton("➕ Add Coin", callback_data="add_coin"))
    markup.row(types.InlineKeyboardButton("➖ Remove Coin", callback_data="remove_coin"))
    markup.row(types.InlineKeyboardButton("💼 My Coins", callback_data="my_coins"))
    markup.row(types.InlineKeyboardButton("📈 Signals", callback_data="signals"))
    markup.row(types.InlineKeyboardButton("🚀 Top Movers", callback_data="top_movers"))
    markup.row(types.InlineKeyboardButton("🕑 Auto Signals Start", callback_data="auto_signals"))
    markup.row(types.InlineKeyboardButton("⏹ Stop Auto Signals", callback_data="stop_auto"))
    markup.row(types.InlineKeyboardButton("🚀 Top Movers Auto", callback_data="auto_movers"))
    markup.row(types.InlineKeyboardButton("⏹ Stop Top Movers Auto", callback_data="stop_auto_movers"))
    bot.send_message(message.chat.id, "🤖 Welcome to Ultra Signals Bot!", reply_markup=markup)

# =========================
# CALLBACK HANDLERS
# =========================
@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    settings = load_settings()

    if call.data == "add_coin":
        msg = bot.send_message(call.message.chat.id, "Enter coin symbol to add (e.g., BTCUSDT):")
        bot.register_next_step_handler(msg, add_coin_step)
    
    elif call.data == "remove_coin":
        if not settings["coins"]:
            bot.send_message(call.message.chat.id, "❌ No coins to remove.")
            return
        markup = types.InlineKeyboardMarkup()
        for c in settings["coins"]:
            markup.row(types.InlineKeyboardButton(f"❌ {c}", callback_data=f"remove_{c}"))
        markup.row(types.InlineKeyboardButton("⬅ Back", callback_data="back_main"))
        bot.send_message(call.message.chat.id, "Select coin to remove:", reply_markup=markup)
    
    elif call.data.startswith("remove_"):
        coin = call.data.replace("remove_", "")
        if coin in settings["coins"]:
            settings["coins"].remove(coin)
            save_settings(settings)
            bot.send_message(call.message.chat.id, f"✅ {coin} removed from My Coins.")
    
    elif call.data == "my_coins":
        coins = settings.get("coins", [])
        if not coins:
            bot.send_message(call.message.chat.id, "💼 No coins added yet.")
        else:
            bot.send_message(call.message.chat.id, "💼 My Coins:\n" + "\n".join(coins))

    elif call.data == "signals":
        markup = types.InlineKeyboardMarkup()
        markup.row(types.InlineKeyboardButton("💼 My Coins", callback_data="signals_my"))
        markup.row(types.InlineKeyboardButton("🌍 All Coins", callback_data="signals_all"))
        markup.row(types.InlineKeyboardButton("⬅ Back", callback_data="back_main"))
        bot.send_message(call.message.chat.id, "Choose a signal option:", reply_markup=markup)

    elif call.data == "signals_my":
        show_timeframes(call, "my")

    elif call.data == "signals_all":
        show_timeframes(call, "all")

    elif call.data.startswith("timeframe_my_"):
        tf = call.data.replace("timeframe_my_", "")
        show_signals(call.message.chat.id, tf, "my")

    elif call.data.startswith("timeframe_all_"):
        tf = call.data.replace("timeframe_all_", "")
        show_signals(call.message.chat.id, tf, "all")

    elif call.data == "back_main":
        start(call.message)

def add_coin_step(message):
    coin = message.text.strip().upper()
    settings = load_settings()
    if coin not in settings["coins"]:
        settings["coins"].append(coin)
        save_settings(settings)
        bot.send_message(message.chat.id, f"✅ {coin} added to My Coins.")
    else:
        bot.send_message(message.chat.id, f"⚠ {coin} already in My Coins.")

def show_timeframes(call, mode):
    markup = types.InlineKeyboardMarkup()
    for tf in ["1m", "5m", "15m", "1h", "1d"]:
        markup.row(types.InlineKeyboardButton(tf, callback_data=f"timeframe_{mode}_{tf}"))
    markup.row(types.InlineKeyboardButton("⬅ Back", callback_data="signals"))
    bot.send_message(call.message.chat.id, "Choose timeframe:", reply_markup=markup)

def show_signals(chat_id, timeframe, mode):
    settings = load_settings()
    coins = settings["coins"] if mode == "my" else ["BTCUSDT","ETHUSDT","BNBUSDT","XRPUSDT","SOLUSDT"]
    signals = []
    for c in coins:
        try:
            df = fetch_klines(c, timeframe)
            sig = generate_signal(c, df, timeframe)
            if sig:
                signals.append(sig)
        except Exception as e:
            print(f"Error fetching {c}: {e}")
    if not signals:
        bot.send_message(chat_id, f"No strong signals found for {timeframe}.")
    else:
        for s in signals[:10]:  # limit max 10
            bot.send_message(chat_id, s)

# =========================
# SIGNAL GENERATOR (SIMPLE)
# =========================
def fetch_klines(symbol, interval):
    klines = client.get_klines(symbol=symbol, interval=interval, limit=50)
    df = pd.DataFrame(klines, columns=[
        "time","o","h","l","c","v","ct","qv","n","tbv","tqv","ig"
    ])
    df["c"] = df["c"].astype(float)
    return df

def generate_signal(symbol, df, timeframe):
    prices = df["c"].values
    last = prices[-1]
    ma = np.mean(prices[-10:])
    trend = "✅ BUY" if last > ma else "❌ SELL"
    strength = "Ultra" if abs(last - ma)/ma > 0.02 else "Strong"
    if strength not in ["Ultra","Strong"]:
        return None
    leverage = {"1m":5, "5m":10, "15m":20, "1h":30, "1d":50}.get(timeframe, 5)
    entry = round(last,3)
    stop = round(last*0.98,3)
    tp1 = round(last*1.02,3)
    tp2 = round(last*1.04,3)
    return f"""{trend} {symbol} ({timeframe})
Leverage: x{leverage}
Entry: {entry}
Stop Loss: {stop}
TP1: {tp1} | TP2: {tp2}
Valid for: 15 mins"""

# =========================
# FLASK WEBHOOK
# =========================
@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def webhook():
    try:
        json_str = request.get_data().decode("UTF-8")
        update = telebot.types.Update.de_json(json_str)
        bot.process_new_updates([update])
    except Exception as e:
        print("Webhook Exception:", e)
        traceback.print_exc()
    return "OK", 200

if __name__ == "__main__":
    bot.remove_webhook()
    bot.set_webhook(url=f"{WEBHOOK_URL}/{TELEGRAM_TOKEN}")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))









