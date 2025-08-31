import os
import json
import time
import threading
import traceback
from flask import Flask, request
import telebot
from telebot import types
from binance.client import Client

# --- ENVIRONMENT VARIABLES ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY")
BINANCE_API_SECRET = os.environ.get("BINANCE_API_SECRET")
CHAT_ID = int(os.environ.get("TELEGRAM_CHAT_ID", "0"))

bot = telebot.TeleBot(TELEGRAM_TOKEN)
client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)

app = Flask(__name__)

# --- JSON Storage ---
SETTINGS_FILE = "settings.json"
if not os.path.exists(SETTINGS_FILE):
    with open(SETTINGS_FILE, "w") as f:
        json.dump({"coins": [], "auto_mode": False}, f)

def load_settings():
    with open(SETTINGS_FILE, "r") as f:
        return json.load(f)

def save_settings(data):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(data, f)

# --- SIGNAL GENERATOR ---
def get_signal(symbol, interval="5m"):
    try:
        klines = client.get_klines(symbol=symbol, interval=interval, limit=50)
        closes = [float(k[4]) for k in klines]
        if len(closes) < 14:
            return f"⚠️ Not enough data for {symbol}"

        sma = sum(closes[-14:]) / 14
        last_price = closes[-1]

        if last_price > sma * 1.01:
            return f"✅ BUY {symbol} ({interval}) — Strong bullish momentum."
        elif last_price < sma * 0.99:
            return f"❌ SELL {symbol} ({interval}) — Bearish pressure."
        else:
            return f"⚠️ Neutral {symbol} ({interval}) — Sideways."
    except Exception as e:
        return f"⚠️ Error fetching data for {symbol}: {e}"

# --- TELEGRAM MENUS ---
def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("➕ Add Coin", "➖ Remove Coin")
    kb.add("📈 Signals", "🚀 Top Movers")
    kb.add("▶ Start Auto Mode", "⏹ Stop Auto Mode")
    return kb

def signal_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("💼 My Coins", "🌍 All Coins", "🔎 Particular Coin")
    kb.add("⬅️ Back")
    return kb

def timeframe_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("1m", "5m", "15m", "1h", "1d")
    kb.add("⬅️ Back")
    return kb

# --- BOT COMMANDS ---
@bot.message_handler(commands=["start"])
def start(message):
    bot.send_message(message.chat.id, "🤖 Welcome to Ultra Signals Bot!", reply_markup=main_menu())

@bot.message_handler(func=lambda m: m.text == "➕ Add Coin")
def add_coin(message):
    bot.send_message(message.chat.id, "Enter coin symbol to add (e.g., BTCUSDT):")
    bot.register_next_step_handler(message, process_add_coin)

def process_add_coin(message):
    symbol = message.text.strip().upper()
    settings = load_settings()
    if symbol not in settings["coins"]:
        settings["coins"].append(symbol)
        save_settings(settings)
        bot.send_message(message.chat.id, f"✅ {symbol} added to My Coins.")
    else:
        bot.send_message(message.chat.id, f"⚠️ {symbol} already in My Coins.")

@bot.message_handler(func=lambda m: m.text == "➖ Remove Coin")
def remove_coin(message):
    bot.send_message(message.chat.id, "Enter coin symbol to remove:")
    bot.register_next_step_handler(message, process_remove_coin)

def process_remove_coin(message):
    symbol = message.text.strip().upper()
    settings = load_settings()
    if symbol in settings["coins"]:
        settings["coins"].remove(symbol)
        save_settings(settings)
        bot.send_message(message.chat.id, f"❌ {symbol} removed.")
    else:
        bot.send_message(message.chat.id, f"⚠️ {symbol} not found.")

@bot.message_handler(func=lambda m: m.text == "📈 Signals")
def signals_menu(message):
    bot.send_message(message.chat.id, "Choose a signal option:", reply_markup=signal_menu())

@bot.message_handler(func=lambda m: m.text == "💼 My Coins")
def my_coins(message):
    settings = load_settings()
    if not settings["coins"]:
        bot.send_message(message.chat.id, "⚠️ No coins added.")
        return
    bot.send_message(message.chat.id, "Select timeframe:", reply_markup=timeframe_menu())
    bot.register_next_step_handler(message, lambda msg: send_signals(msg, settings["coins"]))

@bot.message_handler(func=lambda m: m.text == "🌍 All Coins")
def all_coins(message):
    tickers = client.get_ticker()
    symbols = [t["symbol"] for t in tickers if t["symbol"].endswith("USDT")][:100]
    bot.send_message(message.chat.id, "Select timeframe:", reply_markup=timeframe_menu())
    bot.register_next_step_handler(message, lambda msg: send_signals(msg, symbols))

@bot.message_handler(func=lambda m: m.text == "🔎 Particular Coin")
def particular_coin(message):
    bot.send_message(message.chat.id, "Enter coin symbol (e.g., BTCUSDT):")
    bot.register_next_step_handler(message, ask_timeframe)

def ask_timeframe(message):
    symbol = message.text.strip().upper()
    bot.send_message(message.chat.id, "Select timeframe:", reply_markup=timeframe_menu())
    bot.register_next_step_handler(message, lambda msg: send_signals(msg, [symbol]))

def send_signals(message, symbols):
    interval = message.text
    if interval not in ["1m", "5m", "15m", "1h", "1d"]:
        bot.send_message(message.chat.id, "⚠️ Invalid timeframe.", reply_markup=main_menu())
        return
    for sym in symbols[:5]:  # limit batch
        bot.send_message(message.chat.id, get_signal(sym, interval))

# --- AUTO MODE ---
def auto_mode_loop():
    while True:
        settings = load_settings()
        if settings.get("auto_mode"):
            try:
                tickers = client.get_ticker()
                symbols = [t["symbol"] for t in tickers if t["symbol"].endswith("USDT")][:100]
                for sym in symbols:
                    signal = get_signal(sym, "5m")
                    bot.send_message(CHAT_ID, signal)
            except Exception as e:
                bot.send_message(CHAT_ID, f"⚠️ AutoMode error: {e}")
        time.sleep(60)

@bot.message_handler(func=lambda m: m.text == "▶ Start Auto Mode")
def start_auto(message):
    settings = load_settings()
    settings["auto_mode"] = True
    save_settings(settings)
    bot.send_message(message.chat.id, "▶ Auto Mode started 24×7.")

@bot.message_handler(func=lambda m: m.text == "⏹ Stop Auto Mode")
def stop_auto(message):
    settings = load_settings()
    settings["auto_mode"] = False
    save_settings(settings)
    bot.send_message(message.chat.id, "⏹ Auto Mode stopped.")

# --- FLASK WEBHOOK ---
@app.route("/" + TELEGRAM_TOKEN, methods=["POST"])
def webhook():
    update = request.stream.read().decode("utf-8")
    bot.process_new_updates([telebot.types.Update.de_json(update)])
    return "OK", 200

@app.route("/")
def index():
    return "Bot is running!", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    bot.remove_webhook()
    webhook_url = f"https://{os.environ.get('RENDER_EXTERNAL_HOSTNAME')}/{TELEGRAM_TOKEN}"
    bot.set_webhook(url=webhook_url)
    threading.Thread(target=auto_mode_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=port)









