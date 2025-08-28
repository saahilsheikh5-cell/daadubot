import os
import json
import time
import threading
from flask import Flask, request
import telebot
import logging

# ===== LOGGING =====
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ===== CONFIG =====
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = int(os.getenv("CHAT_ID", "0"))  # optional default
WEBHOOK_URL_PATH = "/webhook"
PUBLIC_URL = os.getenv("PUBLIC_URL", "https://daadubot.onrender.com")

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# ===== STORAGE =====
USER_COINS_FILE = "user_coins.json"

def load_json(file, default):
    if not os.path.exists(file):
        return default
    with open(file, "r") as f:
        return json.load(f)

def save_json(file, data):
    with open(file, "w") as f:
        json.dump(data, f, indent=4)

coins = load_json(USER_COINS_FILE, [])

# ===== USER STATE =====
user_state = {}

# ===== MENU =====
from telebot import types

def main_menu(chat_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("➕ Add Coin","📊 My Coins")
    markup.add("➖ Remove Coin","📈 Top Movers")
    markup.add("📡 Signals","🛑 Stop Signals")
    markup.add("🔄 Reset Settings","⚙️ Signal Settings","🔍 Preview Signal")
    bot.send_message(chat_id, "🤖 Main Menu:", reply_markup=markup)
    user_state[chat_id] = None

# ===== HANDLERS =====
@bot.message_handler(commands=["start"])
def start(msg):
    bot.send_message(msg.chat.id, "✅ Bot is live and ready!")
    main_menu(msg.chat.id)

@bot.message_handler(func=lambda m: m.text=="➕ Add Coin")
def add_coin_menu(msg):
    bot.send_message(msg.chat.id, "Type coin symbol (e.g., BTCUSDT):")
    user_state[msg.chat.id] = "adding_coin"

@bot.message_handler(func=lambda m: user_state.get(m.chat.id)=="adding_coin")
def process_add_coin(msg):
    coin = msg.text.upper()
    if coin not in coins:
        coins.append(coin)
        save_json(USER_COINS_FILE, coins)
        bot.send_message(msg.chat.id, f"✅ {coin} added.")
    else:
        bot.send_message(msg.chat.id, f"{coin} already exists.")
    user_state[msg.chat.id] = None
    main_menu(msg.chat.id)

@bot.message_handler(func=lambda m: m.text=="📊 My Coins")
def my_coins_menu(msg):
    chat_id = msg.chat.id
    if not coins:
        bot.send_message(chat_id, "⚠️ No coins added yet.")
        main_menu(chat_id)
        return
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for c in coins:
        markup.add(c)
    markup.add("🔙 Back")
    bot.send_message(chat_id, "Select a coin to view:", reply_markup=markup)
    user_state[chat_id] = "viewing_coin"

@bot.message_handler(func=lambda m: user_state.get(m.chat.id)=="viewing_coin")
def process_view_coin(msg):
    chat_id = msg.chat.id
    coin = msg.text.upper()
    if coin == "🔙":
        main_menu(chat_id)
        return
    if coin not in coins:
        bot.send_message(chat_id, "❌ Coin not in list.")
        main_menu(chat_id)
        return
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    intervals = ["1m","5m","15m","1h","1d"]
    for i in intervals:
        markup.add(i)
    markup.add("🔙 Back")
    bot.send_message(chat_id, f"Select interval for {coin}:", reply_markup=markup)
    user_state[chat_id] = f"interval_{coin}"

@bot.message_handler(func=lambda m: user_state.get(m.chat.id, "").startswith("interval_"))
def process_coin_interval(msg):
    chat_id = msg.chat.id
    state = user_state.get(chat_id)
    coin = state.replace("interval_", "")
    interval = msg.text
    if interval == "🔙":
        my_coins_menu(msg)
        return
    bot.send_message(chat_id, f"⚪ Neutral / No signal for {coin} | {interval}")
    # stay in interval selection
    my_coins_menu(msg)

# ===== FLASK ROUTES =====
@app.route("/", methods=["GET"])
def home():
    logger.info("Health check received at /")
    return "Bot is alive ✅", 200

@app.route(WEBHOOK_URL_PATH, methods=["POST"])
def webhook():
    update_json = request.get_json(force=True)
    logger.info(f"Incoming update: {update_json}")
    if update_json:
        try:
            bot.process_new_updates([telebot.types.Update.de_json(update_json)])
        except Exception as e:
            logger.error(f"Failed to process update: {e}")
    return "ok", 200

# ===== WEBHOOK SETUP =====
def setup_webhook():
    import requests
    logger.info("Resetting Telegram webhook...")
    requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook")
    url = f"{PUBLIC_URL}{WEBHOOK_URL_PATH}"
    r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?url={url}")
    logger.info(f"Webhook set response: {r.json()}")

# ===== MAIN =====
if __name__ == "__main__":
    setup_webhook()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
else:
    setup_webhook()

               



