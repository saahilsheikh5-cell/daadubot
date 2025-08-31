import os
import time
import threading
from flask import Flask, request
import telebot
from telebot import types
from binance.client import Client

# === ENVIRONMENT VARIABLES ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

bot = telebot.TeleBot(TELEGRAM_TOKEN)
app = Flask(__name__)
client = Client(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)

# === STATE STORAGE ===
user_coins = {}
auto_signals_running = {}
top_movers_running = {}
selected_timeframe = {}

# === UTILITIES ===
def get_signal(symbol, timeframe="5m"):
    """Mock signal generator (replace with real TA logic)."""
    import random
    outcome = random.choice(["BUY", "SELL", "NEUTRAL"])
    if outcome == "NEUTRAL":
        return None
    return {
        "symbol": symbol,
        "action": outcome,
        "timeframe": timeframe,
        "leverage": random.choice([5, 10, 20]),
        "entry": round(random.uniform(1, 100), 3),
        "sl": round(random.uniform(1, 100), 3),
        "tp1": round(random.uniform(1, 100), 3),
        "tp2": round(random.uniform(1, 100), 3),
        "valid_for": 15  # minutes
    }

def format_signal(sig):
    return (
        f"{'✅ BUY' if sig['action']=='BUY' else '❌ SELL'} {sig['symbol']} ({sig['timeframe']})\n"
        f"Leverage: x{sig['leverage']}\n"
        f"Entry: {sig['entry']}\n"
        f"Stop Loss: {sig['sl']}\n"
        f"TP1: {sig['tp1']} | TP2: {sig['tp2']}\n"
        f"Valid for: {sig['valid_for']} mins"
    )

# === MAIN MENU ===
def main_menu():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("➕ Add Coin", "📈 Signals")
    markup.add("🚀 Top Movers Auto", "⏹ Stop Top Movers Auto")
    markup.add("🕑 Auto Signals Start", "⏹ Stop Auto Signals")
    return markup

@bot.message_handler(commands=['start'])
def start(message):
    bot.send_message(message.chat.id, "🤖 Welcome to Ultra Signals Bot!", reply_markup=main_menu())

# === ADD COIN ===
@bot.message_handler(func=lambda msg: msg.text == "➕ Add Coin")
def add_coin(message):
    bot.send_message(message.chat.id, "Enter coin symbol to add (e.g., BTCUSDT):")
    bot.register_next_step_handler(message, save_coin)

def save_coin(message):
    symbol = message.text.strip().upper()
    user_coins.setdefault(message.chat.id, [])
    if symbol not in user_coins[message.chat.id]:
        user_coins[message.chat.id].append(symbol)
        bot.send_message(message.chat.id, f"✅ {symbol} added to My Coins.")
    else:
        bot.send_message(message.chat.id, f"⚠️ {symbol} already in My Coins.")

# === SIGNALS MENU ===
@bot.message_handler(func=lambda msg: msg.text == "📈 Signals")
def signals_menu(message):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("💼 My Coins", "🌍 All Coins", "🔎 Particular Coin")
    markup.add("⬅️ Back")
    bot.send_message(message.chat.id, "Choose a signal option:", reply_markup=markup)

@bot.message_handler(func=lambda msg: msg.text in ["💼 My Coins", "🌍 All Coins", "🔎 Particular Coin"])
def signal_timeframe(message):
    markup = types.InlineKeyboardMarkup()
    for tf in ["1m", "5m", "15m", "1h", "1d"]:
        markup.add(types.InlineKeyboardButton(tf, callback_data=f"tf|{message.text}|{tf}"))
    markup.add(types.InlineKeyboardButton("⬅️ Back", callback_data="back_main"))
    bot.send_message(message.chat.id, "Choose timeframe:", reply_markup=markup)

# === CALLBACK HANDLER ===
@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    if call.data.startswith("tf|"):
        _, source, tf = call.data.split("|")
        chat_id = call.message.chat.id
        if source == "💼 My Coins":
            coins = user_coins.get(chat_id, [])
        elif source == "🌍 All Coins":
            coins = [s["symbol"] for s in client.get_ticker() if s["symbol"].endswith("USDT")][:100]
        else:
            bot.send_message(chat_id, "Enter coin symbol (e.g., BTCUSDT):")
            return
        for sym in coins:
            sig = get_signal(sym, tf)
            if sig:
                bot.send_message(chat_id, format_signal(sig))
    elif call.data == "back_main":
        bot.send_message(call.message.chat.id, "🔙 Main Menu", reply_markup=main_menu())

# === AUTO SIGNALS ===
def auto_signal_worker(chat_id, timeframe):
    while auto_signals_running.get(chat_id):
        coins = [s["symbol"] for s in client.get_ticker() if s["symbol"].endswith("USDT")][:100]
        for sym in coins:
            sig = get_signal(sym, timeframe)
            if sig:
                bot.send_message(chat_id, format_signal(sig))
        time.sleep(60)

@bot.message_handler(func=lambda msg: msg.text == "🕑 Auto Signals Start")
def start_auto_signals(message):
    markup = types.InlineKeyboardMarkup()
    for tf in ["1m", "5m", "15m", "1h", "1d"]:
        markup.add(types.InlineKeyboardButton(tf, callback_data=f"auto|{tf}"))
    bot.send_message(message.chat.id, "Select timeframe for Auto Signals:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("auto|"))
def start_auto_with_tf(call):
    _, tf = call.data.split("|")
    chat_id = call.message.chat.id
    auto_signals_running[chat_id] = True
    threading.Thread(target=auto_signal_worker, args=(chat_id, tf), daemon=True).start()
    bot.send_message(chat_id, f"✅ Auto signals started for {tf}.")

@bot.message_handler(func=lambda msg: msg.text == "⏹ Stop Auto Signals")
def stop_auto_signals(message):
    auto_signals_running[message.chat.id] = False
    bot.send_message(message.chat.id, "⏹ Auto signals stopped.")

# === TOP MOVERS ===
def top_movers_worker(chat_id):
    while top_movers_running.get(chat_id):
        movers = sorted(client.get_ticker(), key=lambda x: abs(float(x["priceChangePercent"])), reverse=True)[:5]
        txt = "🚀 Top Movers:\n" + "\n".join([f"{m['symbol']}: {m['priceChangePercent']}%" for m in movers])
        bot.send_message(chat_id, txt)
        time.sleep(120)

@bot.message_handler(func=lambda msg: msg.text == "🚀 Top Movers Auto")
def start_top_movers(message):
    top_movers_running[message.chat.id] = True
    threading.Thread(target=top_movers_worker, args=(message.chat.id,), daemon=True).start()
    bot.send_message(message.chat.id, "🚀 Top Movers monitor started.")

@bot.message_handler(func=lambda msg: msg.text == "⏹ Stop Top Movers Auto")
def stop_top_movers(message):
    top_movers_running[message.chat.id] = False
    bot.send_message(message.chat.id, "⏹ Top Movers Auto stopped.")

# === BACK HANDLER ===
@bot.message_handler(func=lambda msg: msg.text == "⬅️ Back")
def back_to_main(message):
    bot.send_message(message.chat.id, "🔙 Main Menu", reply_markup=main_menu())

# === FLASK WEBHOOK ===
@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def webhook():
    update = request.stream.read().decode("utf-8")
    bot.process_new_updates([telebot.types.Update.de_json(update)])
    return "OK", 200

if __name__ == "__main__":
    bot.remove_webhook()
    time.sleep(1)
    bot.set_webhook(url=f"{WEBHOOK_URL}/{TELEGRAM_TOKEN}")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))








