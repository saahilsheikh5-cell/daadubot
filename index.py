import os
import telebot
import requests
import threading
import time
import logging
from flask import Flask, request
from telebot import types
from tradingview_ta import TA_Handler, Interval, Exchange

# ================= CONFIG =================
BOT_TOKEN = os.getenv("BOT_TOKEN", "7638935379:AAEmLD7JHLZ36Ywh5tvmlP1F8xzrcNrym_Q")
APP_URL = f"https://daadubot.onrender.com/{BOT_TOKEN}"

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
server = Flask(__name__)

# ====== Logging ======
logging.basicConfig(level=logging.INFO)

# ====== Data Storage ======
user_coins = {}  # user_id -> list of coins
tracked_signals = {}  # coin -> last signal

# ====== Utility Functions ======
def get_signal(symbol, interval=Interval.INTERVAL_1_HOUR):
    try:
        handler = TA_Handler(
            symbol=symbol,
            screener="crypto",
            exchange="BINANCE",
            interval=interval
        )
        analysis = handler.get_analysis()
        return analysis.summary, analysis.indicators
    except Exception as e:
        return {"RECOMMENDATION": "ERROR"}, {"error": str(e)}

def format_ta(symbol, summary, indicators):
    rec = summary.get("RECOMMENDATION", "N/A")
    text = f"<b>📊 Technical Analysis for {symbol}</b>\n"
    text += f"Recommendation: <b>{rec}</b>\n\n"
    text += "📌 Indicators:\n"
    for key in ["RSI", "MACD.macd", "EMA10", "EMA20", "EMA50"]:
        if key in indicators:
            text += f"{key}: {indicators[key]}\n"
    return text

# ====== Commands ======
@bot.message_handler(commands=['start'])
def start(message):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("➕ Add Coin", "📂 My Coins")
    kb.add("📈 Signals", "🔥 Movers")
    kb.add("⚡ Auto Signals")
    bot.send_message(message.chat.id, "👋 Welcome! Choose an option:", reply_markup=kb)

@bot.message_handler(func=lambda m: m.text == "➕ Add Coin")
def add_coin(message):
    bot.send_message(message.chat.id, "Send me coin symbol (e.g., BTCUSDT):")
    bot.register_next_step_handler(message, save_coin)

def save_coin(message):
    uid = message.chat.id
    coin = message.text.upper()
    user_coins.setdefault(uid, []).append(coin)
    bot.send_message(uid, f"✅ {coin} added to your list!")

@bot.message_handler(func=lambda m: m.text == "📂 My Coins")
def my_coins(message):
    uid = message.chat.id
    coins = user_coins.get(uid, [])
    if not coins:
        bot.send_message(uid, "No coins added. Use ➕ Add Coin.")
        return
    kb = types.InlineKeyboardMarkup()
    for c in coins:
        kb.add(types.InlineKeyboardButton(c, callback_data=f"coin_{c}"))
    bot.send_message(uid, "📂 Your coins:", reply_markup=kb)

@bot.message_handler(func=lambda m: m.text == "📈 Signals")
def signals_menu(message):
    bot.send_message(message.chat.id, "Send coin symbol (e.g., BTCUSDT):")
    bot.register_next_step_handler(message, show_signals)

def show_signals(message):
    coin = message.text.upper()
    summary, indicators = get_signal(coin, Interval.INTERVAL_1_HOUR)
    text = format_ta(coin, summary, indicators)
    bot.send_message(message.chat.id, text)

@bot.message_handler(func=lambda m: m.text == "🔥 Movers")
def movers(message):
    url = "https://api.binance.com/api/v3/ticker/24hr"
    data = requests.get(url).json()
    sorted_data = sorted(data, key=lambda x: float(x["priceChangePercent"]), reverse=True)[:10]
    msg = "🔥 Top 10 Movers (24h):\n"
    for d in sorted_data:
        msg += f"{d['symbol']}: {d['priceChangePercent']}%\n"
    bot.send_message(message.chat.id, msg)

@bot.message_handler(func=lambda m: m.text == "⚡ Auto Signals")
def auto_signals(message):
    bot.send_message(message.chat.id, "⚡ Auto signal alerts enabled for your coins.")
    threading.Thread(target=signal_watcher, args=(message.chat.id,), daemon=True).start()

def signal_watcher(chat_id):
    while True:
        coins = user_coins.get(chat_id, [])
        for coin in coins:
            summary, indicators = get_signal(coin)
            rec = summary.get("RECOMMENDATION")
            if tracked_signals.get(coin) != rec:
                tracked_signals[coin] = rec
                text = format_ta(coin, summary, indicators)
                bot.send_message(chat_id, f"⚡ Signal update for {coin}:\n{text}")
        time.sleep(60)

# ====== Callback ======
@bot.callback_query_handler(func=lambda call: call.data.startswith("coin_"))
def coin_analysis(call):
    coin = call.data.split("_")[1]
    summary, indicators = get_signal(coin)
    text = format_ta(coin, summary, indicators)
    bot.send_message(call.message.chat.id, text)

# ====== Flask Routes ======
@server.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    bot.process_new_updates([telebot.types.Update.de_json(request.data.decode("utf-8"))])
    return "!", 200

@server.route("/health", methods=["GET"])
def health():
    return "Bot is running!", 200

@server.route("/", methods=["GET"])
def index():
    return "Hello, this is DaaduBot!", 200

# ====== Start Bot ======
def run():
    bot.remove_webhook()
    bot.set_webhook(url=APP_URL)
    server.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

if __name__ == "__main__":
    run()

