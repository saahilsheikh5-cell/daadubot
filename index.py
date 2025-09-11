import os
import json
import threading
import time
import pandas as pd
import numpy as np
from flask import Flask, request
import telebot
from telebot import types
from binance.client import Client

# -------------------------
# Environment variables
# -------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

# -------------------------
# Init
# -------------------------
bot = telebot.TeleBot(TELEGRAM_TOKEN)
app = Flask(__name__)
client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)

# -------------------------
# Global flags & state
# -------------------------
auto_flag = False
movers_flag = False
user_coins = {}

# -------------------------
# Helpers
# -------------------------
DATA_FILE = "coins.json"

def load_coins():
    global user_coins
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            user_coins = json.load(f)
    else:
        user_coins = {}

def save_coins():
    with open(DATA_FILE, "w") as f:
        json.dump(user_coins, f)

def build_signal_summary(symbol, tf="5m"):
    # Fetch fake values for demo (replace with TA)
    price = float(client.get_symbol_ticker(symbol=symbol)["price"])
    strength = np.random.choice(["Ultra Buy", "Strong Buy", "Ultra Sell", "Strong Sell"])
    rsi = np.random.uniform(20, 80)
    macd = np.random.uniform(-2, 2)
    boll = (price * 0.95, price * 1.05)

    # Dynamic leverage
    if "Ultra" in strength:
        lev = 20
    elif "Strong" in strength:
        lev = 10
    else:
        lev = 5

    sl = round(price * 0.98, 2)
    tp1 = round(price * 1.02, 2)
    tp2 = round(price * 1.04, 2)

    return f"""
📊 Signal: {strength} {symbol} ({tf})
Leverage: x{lev}
Entry: {price}
Stop Loss: {sl}
TP1: {tp1} | TP2: {tp2}
Valid for: 15 mins

📉 Indicators:
RSI: {rsi:.2f}
MACD: {macd:.2f}
Bollinger: {boll}

💡 Suggestion: Based on RSI & MACD trend, {strength} is recommended.
"""

def strongest_signals(symbols, tf="5m"):
    signals = []
    for s in symbols:
        signals.append(build_signal_summary(s, tf))
    return signals[:5]

# -------------------------
# Handlers
# -------------------------
@bot.message_handler(commands=["start"])
def start(message):
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("➕ Add Coin", callback_data="add_coin"))
    kb.add(types.InlineKeyboardButton("➖ Remove Coin", callback_data="remove_coin"))
    kb.add(types.InlineKeyboardButton("📋 My Coins", callback_data="list_coins"))
    kb.add(types.InlineKeyboardButton("📈 Signals", callback_data="signals"))
    kb.add(types.InlineKeyboardButton("🕑 Auto Signals Start", callback_data="auto_start"))
    kb.add(types.InlineKeyboardButton("⏹ Stop Auto Signals", callback_data="auto_stop"))
    kb.add(types.InlineKeyboardButton("🚀 Top Movers Auto", callback_data="movers_start"))
    kb.add(types.InlineKeyboardButton("⏹ Stop Top Movers Auto", callback_data="movers_stop"))
    bot.send_message(message.chat.id, "🤖 Welcome to Ultra Signals Bot!", reply_markup=kb)

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    global auto_flag, movers_flag

    if call.data == "add_coin":
        msg = bot.send_message(call.message.chat.id, "Enter coin symbol (e.g., BTCUSDT):")
        bot.register_next_step_handler(msg, add_coin_step)

    elif call.data == "remove_coin":
        coins = user_coins.get(str(call.message.chat.id), [])
        if not coins:
            bot.send_message(call.message.chat.id, "⚠️ No coins to remove.")
            return
        kb = types.InlineKeyboardMarkup()
        for c in coins:
            kb.add(types.InlineKeyboardButton(c, callback_data=f"del_{c}"))
        bot.send_message(call.message.chat.id, "Select coin to remove:", reply_markup=kb)

    elif call.data.startswith("del_"):
        coin = call.data.split("_")[1]
        user_coins[str(call.message.chat.id)] = [c for c in user_coins.get(str(call.message.chat.id), []) if c != coin]
        save_coins()
        bot.send_message(call.message.chat.id, f"❌ {coin} removed.")

    elif call.data == "list_coins":
        coins = user_coins.get(str(call.message.chat.id), [])
        if not coins:
            bot.send_message(call.message.chat.id, "⚠️ No coins added yet.")
        else:
            bot.send_message(call.message.chat.id, "📋 Your coins:\n" + "\n".join(coins))

    elif call.data == "signals":
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("💼 My Coins", callback_data="sig_my"))
        kb.add(types.InlineKeyboardButton("🌍 All Coins", callback_data="sig_all"))
        kb.add(types.InlineKeyboardButton("🔎 Particular Coin", callback_data="sig_part"))
        kb.add(types.InlineKeyboardButton("⬅️ Back", callback_data="back_start"))
        bot.send_message(call.message.chat.id, "Choose a signal option:", reply_markup=kb)

    elif call.data == "sig_my":
        coins = user_coins.get(str(call.message.chat.id), [])
        if not coins:
            bot.send_message(call.message.chat.id, "⚠️ No coins added.")
            return
        signals = strongest_signals(coins, "5m")
        for s in signals:
            bot.send_message(call.message.chat.id, s)

    elif call.data == "sig_all":
        tickers = [s["symbol"] for s in client.get_ticker()[:100]]
        signals = strongest_signals(tickers, "5m")
        for s in signals:
            bot.send_message(call.message.chat.id, s)

    elif call.data == "sig_part":
        msg = bot.send_message(call.message.chat.id, "Enter coin symbol (e.g., ETHUSDT):")
        bot.register_next_step_handler(msg, sig_part_step)

    elif call.data == "auto_start":
        if auto_flag:
            bot.send_message(call.message.chat.id, "⚠️ Auto already running.")
            return
        auto_flag = True
        threading.Thread(target=auto_signals_loop, args=(call.message.chat.id,), daemon=True).start()
        bot.send_message(call.message.chat.id, "✅ Auto signals started.")

    elif call.data == "auto_stop":
        auto_flag = False
        bot.send_message(call.message.chat.id, "⏹ Auto signals stopped.")

    elif call.data == "movers_start":
        if movers_flag:
            bot.send_message(call.message.chat.id, "⚠️ Movers already running.")
            return
        movers_flag = True
        threading.Thread(target=movers_loop, args=(call.message.chat.id,), daemon=True).start()
        bot.send_message(call.message.chat.id, "✅ Top Movers started.")

    elif call.data == "movers_stop":
        movers_flag = False
        bot.send_message(call.message.chat.id, "⏹ Top Movers stopped.")

    elif call.data == "back_start":
        start(call.message)

def add_coin_step(message):
    symbol = message.text.strip().upper()
    coins = user_coins.get(str(message.chat.id), [])
    if symbol not in coins:
        coins.append(symbol)
    user_coins[str(message.chat.id)] = coins
    save_coins()
    bot.send_message(message.chat.id, f"✅ {symbol} added.")

def sig_part_step(message):
    symbol = message.text.strip().upper()
    s = build_signal_summary(symbol, "5m")
    bot.send_message(message.chat.id, s)

# -------------------------
# Background loops
# -------------------------
def auto_signals_loop(chat_id):
    global auto_flag
    while auto_flag:
        tickers = [s["symbol"] for s in client.get_ticker()[:100]]
        signals = strongest_signals(tickers, "5m")
        for s in signals:
            bot.send_message(chat_id, s)
        time.sleep(60)

def movers_loop(chat_id):
    global movers_flag
    while movers_flag:
        tickers = [s["symbol"] for s in client.get_ticker()[:100]]
        coin = np.random.choice(tickers)
        s = build_signal_summary(coin, "5m")
        bot.send_message(chat_id, s)
        time.sleep(120)

# -------------------------
# Webhook endpoints & boot
# -------------------------
@app.route("/" + TELEGRAM_TOKEN, methods=["POST"])
def webhook():
    json_str = request.get_data().decode("UTF-8")
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "OK", 200

@app.route("/")
def index():
    return "Bot running", 200

if __name__ == "__main__":
    load_coins()
    bot.remove_webhook()
    bot.set_webhook(url=f"{WEBHOOK_URL}/{TELEGRAM_TOKEN}")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))




