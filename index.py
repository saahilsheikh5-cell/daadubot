import os
import json
import time
import threading
import pandas as pd
import numpy as np
from flask import Flask, request
import telebot
from telebot import types
from binance.client import Client
from binance.exceptions import BinanceAPIException

# -------------------------
# ENV
# -------------------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = int(os.environ.get("TELEGRAM_CHAT_ID", 0))
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY")
BINANCE_API_SECRET = os.environ.get("BINANCE_API_SECRET")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")  # https://yourdomain.com

bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=True)
app = Flask(__name__)
client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)

MY_COINS_FILE = "my_coins.json"
TIMEFRAMES = ["1m", "5m", "15m", "1h", "1d"]
SEND_SLEEP = 1

auto_flag = False
auto_thread = None
auto_tf = "5m"
movers_flag = False
movers_thread = None

# -------------------------
# Persistence
# -------------------------
def load_my_coins():
    if os.path.exists(MY_COINS_FILE):
        with open(MY_COINS_FILE, "r") as f:
            try:
                return json.load(f)
            except:
                return []
    return []

def save_my_coins_atomic(coins):
    tmp = MY_COINS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(coins, f)
    os.replace(tmp, MY_COINS_FILE)

# -------------------------
# Signal scoring (stub)
# -------------------------
def interval_to_minutes(interval):
    mapping = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "1d": 1440}
    return mapping.get(interval, 5)

def build_signal(symbol, interval):
    try:
        price = float(client.get_symbol_ticker(symbol=symbol)["price"])
    except Exception as e:
        return None

    # Dummy TA scoring (replace with real)
    rsi = np.random.uniform(20, 80)
    macd = round(np.random.uniform(-2, 2), 2)
    boll = "Tight" if np.random.rand() > 0.5 else "Wide"

    strength = "❌ SELL"
    if rsi < 35 and macd > 0:
        strength = "✅ BUY"
    elif rsi > 65 and macd < 0:
        strength = "❌ SELL"
    else:
        return None  # skip weak signals

    # Dynamic leverage
    lev = "x5"
    if abs(rsi - 50) > 20 and abs(macd) > 1.0:
        lev = "x20"
    elif abs(rsi - 50) > 10:
        lev = "x10"

    tp1 = round(price * (1.01 if "BUY" in strength else 0.99), 4)
    tp2 = round(price * (1.02 if "BUY" in strength else 0.98), 4)
    sl = round(price * (0.99 if "BUY" in strength else 1.01), 4)

    return f"""{strength} {symbol} ({interval})
Leverage: {lev}
Entry: {price}
Stop Loss: {sl}
TP1: {tp1} | TP2: {tp2}
Valid for: {interval_to_minutes(interval)} mins
RSI: {round(rsi,2)} | MACD: {macd} | Bollinger: {boll}
➡ Suggestion: Based on RSI, MACD & Bollinger it is suggested to {strength.replace('❌','SELL').replace('✅','BUY')}"""

# -------------------------
# Bot menus
# -------------------------
def main_menu():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("➕ Add Coin", callback_data="add_coin"))
    kb.add(types.InlineKeyboardButton("➖ Remove Coin", callback_data="remove_coin"))
    kb.add(types.InlineKeyboardButton("💼 My Coins", callback_data="my_coins"))
    kb.add(types.InlineKeyboardButton("📈 Signals", callback_data="signals"))
    kb.add(types.InlineKeyboardButton("🕑 Auto Signals Start", callback_data="start_auto"))
    kb.add(types.InlineKeyboardButton("⏹ Stop Auto Signals", callback_data="stop_auto"))
    kb.add(types.InlineKeyboardButton("🚀 Top Movers Auto", callback_data="start_movers"))
    kb.add(types.InlineKeyboardButton("⏹ Stop Movers", callback_data="stop_movers"))
    return kb

@bot.message_handler(commands=["start"])
def handle_start(message):
    bot.send_message(
        message.chat.id,
        "🤖 Welcome to Ultra Signals Bot!",
        reply_markup=main_menu()
    )

# -------------------------
# Callbacks
# -------------------------
@bot.callback_query_handler(func=lambda c: True)
def handle_callbacks(call):
    if call.data == "add_coin":
        msg = bot.send_message(call.message.chat.id, "Enter coin symbol to add (e.g., BTCUSDT):")
        bot.register_next_step_handler(msg, process_add_coin)
    elif call.data == "remove_coin":
        coins = load_my_coins()
        if not coins:
            bot.send_message(call.message.chat.id, "No coins to remove.")
            return
        kb = types.InlineKeyboardMarkup()
        for c in coins:
            kb.add(types.InlineKeyboardButton(f"❌ {c}", callback_data=f"rm_{c}"))
        bot.send_message(call.message.chat.id, "Select coin to remove:", reply_markup=kb)
    elif call.data.startswith("rm_"):
        sym = call.data[3:]
        coins = load_my_coins()
        if sym in coins:
            coins.remove(sym)
            save_my_coins_atomic(coins)
            bot.send_message(call.message.chat.id, f"✅ {sym} removed.")
    elif call.data == "my_coins":
        coins = load_my_coins()
        if not coins:
            bot.send_message(call.message.chat.id, "No coins added yet.")
        else:
            bot.send_message(call.message.chat.id, "My Coins:\n" + "\n".join(coins))
    elif call.data == "signals":
        kb = types.InlineKeyboardMarkup()
        for tf in TIMEFRAMES:
            kb.add(types.InlineKeyboardButton(tf, callback_data=f"sig_{tf}"))
        kb.add(types.InlineKeyboardButton("⬅ Back", callback_data="back"))
        bot.send_message(call.message.chat.id, "Choose timeframe:", reply_markup=kb)
    elif call.data.startswith("sig_"):
        tf = call.data.split("_", 1)[1]
        coins = load_my_coins()
        if not coins:
            bot.send_message(call.message.chat.id, "No coins in list. Add some first.")
            return
        msgs = []
        for c in coins:
            sig = build_signal(c, tf)
            if sig:
                msgs.append(sig)
        if not msgs:
            bot.send_message(call.message.chat.id, f"No strong signals for {tf}.")
        else:
            for m in msgs:
                bot.send_message(call.message.chat.id, m)
                time.sleep(SEND_SLEEP)
    elif call.data == "back":
        bot.edit_message_text("🤖 Back to menu", call.message.chat.id, call.message.id, reply_markup=main_menu())
    elif call.data == "start_auto":
        global auto_flag, auto_thread, auto_tf
        if auto_flag:
            bot.send_message(call.message.chat.id, "Auto already running.")
            return
        auto_flag = True
        auto_tf = "5m"
        auto_thread = threading.Thread(target=auto_loop, daemon=True)
        auto_thread.start()
        bot.send_message(call.message.chat.id, "🟢 Auto signals started (5m).")
    elif call.data == "stop_auto":
        global auto_flag
        auto_flag = False
        bot.send_message(call.message.chat.id, "⏹ Auto signals stopped.")
    elif call.data == "start_movers":
        global movers_flag, movers_thread
        if movers_flag:
            bot.send_message(call.message.chat.id, "Movers already running.")
            return
        movers_flag = True
        movers_thread = threading.Thread(target=movers_loop, daemon=True)
        movers_thread.start()
        bot.send_message(call.message.chat.id, "🟢 Top Movers Auto started.")
    elif call.data == "stop_movers":
        global movers_flag
        movers_flag = False
        bot.send_message(call.message.chat.id, "⏹ Top Movers Auto stopped.")

# -------------------------
# Loops
# -------------------------
def auto_loop():
    global auto_flag, auto_tf
    while auto_flag:
        coins = load_my_coins()
        if not coins:
            time.sleep(30)
            continue
        for c in coins:
            sig = build_signal(c, auto_tf)
            if sig:
                bot.send_message(CHAT_ID, sig)
                time.sleep(SEND_SLEEP)
        time.sleep(interval_to_minutes(auto_tf) * 60)

def movers_loop():
    global movers_flag
    while movers_flag:
        try:
            tickers = client.get_ticker()
            df = pd.DataFrame(tickers)
            df["priceChangePercent"] = df["priceChangePercent"].astype(float)
            top = df.sort_values("priceChangePercent", ascending=False).head(5)
            for _, row in top.iterrows():
                msg = f"🚀 {row['symbol']} {row['priceChangePercent']:.2f}%"
                bot.send_message(CHAT_ID, msg)
            time.sleep(60)
        except Exception as e:
            print(f"Movers loop error: {e}")
            time.sleep(30)

# -------------------------
# Webhook endpoints & boot
# -------------------------
@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def webhook():
    update = telebot.types.Update.de_json(request.data.decode("utf-8"))
    bot.process_new_updates([update])
    return "!", 200

if __name__ == "__main__":
    if CHAT_ID == 0:
        print("⚠️ TELEGRAM_CHAT_ID not set")
    bot.remove_webhook()
    bot.set_webhook(url=f"{WEBHOOK_URL}/{TELEGRAM_TOKEN}")
    print("✅ Bot started (webhook, TA signals)")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))




