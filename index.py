import os
import json
import logging
import threading
import time
from flask import Flask, request
import telebot
from telebot import types

# ===== LOGGING =====
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ===== CONFIG =====
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = int(os.getenv("CHAT_ID", 0))  # optional, for testing signals
WEBHOOK_URL_PATH = "/webhook"
PUBLIC_URL = os.getenv("PUBLIC_URL", "https://daadubot.onrender.com")

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# ===== STORAGE =====
USER_COINS_FILE = "user_coins.json"
SETTINGS_FILE = "settings.json"
LAST_SIGNAL_FILE = "last_signals.json"
MUTED_COINS_FILE = "muted_coins.json"

def load_json(file, default):
    if not os.path.exists(file):
        return default
    with open(file, "r") as f:
        return json.load(f)

def save_json(file, data):
    with open(file, "w") as f:
        json.dump(data, f, indent=4)

coins = load_json(USER_COINS_FILE, [])
settings = load_json(SETTINGS_FILE, {"rsi_buy":20,"rsi_sell":80,"signal_validity_min":15})
last_signals = load_json(LAST_SIGNAL_FILE, {})
muted_coins = load_json(MUTED_COINS_FILE, [])

# ===== USER STATE =====
user_state = {}
selected_coin = {}
selected_interval = {}

# ===== MENU HELPERS =====
def main_menu(msg):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("➕ Add Coin","📊 My Coins")
    markup.add("➖ Remove Coin","📈 Top Movers")
    markup.add("📡 Signals","🛑 Stop Signals")
    markup.add("🔄 Reset Settings","⚙️ Signal Settings","🔍 Preview Signal")
    bot.send_message(msg.chat.id,"🤖 Main Menu:", reply_markup=markup)
    user_state[msg.chat.id] = None

def back_to_main(chat_id, text="Back to Main Menu"):
    bot.send_message(chat_id, text)
    main_menu(types.SimpleNamespace(chat=types.SimpleNamespace(id=chat_id)))

# ===== COMMAND HANDLERS =====
@bot.message_handler(commands=["start","help"])
def start(msg):
    bot.send_message(msg.chat.id,"✅ Bot is live and ready!")
    main_menu(msg)

# ----- ADD COIN -----
@bot.message_handler(func=lambda m: m.text=="➕ Add Coin")
def add_coin_menu(msg):
    chat_id = msg.chat.id
    bot.send_message(chat_id,"Type coin symbol (e.g., BTCUSDT):")
    user_state[chat_id] = "adding_coin"

@bot.message_handler(func=lambda m: user_state.get(m.chat.id)=="adding_coin")
def process_add_coin(msg):
    chat_id = msg.chat.id
    coin = msg.text.upper()
    if not coin.isalnum():
        bot.send_message(chat_id,"❌ Invalid coin symbol.")
    elif coin not in coins:
        coins.append(coin)
        save_json(USER_COINS_FILE, coins)
        bot.send_message(chat_id,f"✅ {coin} added.")
    else:
        bot.send_message(chat_id,f"{coin} already exists.")
    user_state[chat_id] = None
    main_menu(msg)

# ----- REMOVE COIN -----
@bot.message_handler(func=lambda m: m.text=="➖ Remove Coin")
def remove_coin_menu(msg):
    chat_id = msg.chat.id
    if not coins:
        back_to_main(chat_id,"⚠️ No coins to remove.")
        return
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for c in coins:
        markup.add(c)
    markup.add("🔙 Back")
    bot.send_message(chat_id,"Select coin to remove:", reply_markup=markup)
    user_state[chat_id] = "removing_coin"

@bot.message_handler(func=lambda m: user_state.get(m.chat.id)=="removing_coin")
def process_remove_coin(msg):
    chat_id = msg.chat.id
    if msg.text=="🔙 Back":
        user_state[chat_id]=None
        back_to_main(chat_id)
        return
    coin = msg.text.upper()
    if coin in coins:
        coins.remove(coin)
        save_json(USER_COINS_FILE, coins)
        bot.send_message(chat_id,f"✅ {coin} removed.")
    else:
        bot.send_message(chat_id,"❌ Coin not in list.")
    user_state[chat_id] = None
    main_menu(msg)

# ----- MY COINS -----
@bot.message_handler(func=lambda m: m.text=="📊 My Coins")
def my_coins_menu(msg):
    chat_id = msg.chat.id
    if not coins:
        back_to_main(chat_id,"⚠️ No coins added yet.")
        return
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for c in coins:
        markup.add(c)
    markup.add("🔙 Back")
    bot.send_message(chat_id,"Select a coin to view:", reply_markup=markup)
    user_state[chat_id] = "view_coin"

@bot.message_handler(func=lambda m: user_state.get(m.chat.id)=="view_coin")
def coin_timeframe_menu(msg):
    chat_id = msg.chat.id
    if msg.text=="🔙 Back":
        user_state[chat_id]=None
        main_menu(msg)
        return
    selected_coin[chat_id] = msg.text.upper()
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for tf in ["1m","5m","15m","1h","4h","1d"]:
        markup.add(tf)
    markup.add("🔙 Back")
    bot.send_message(chat_id,f"Select interval for {selected_coin[chat_id]}:", reply_markup=markup)
    user_state[chat_id] = "coin_interval"

@bot.message_handler(func=lambda m: user_state.get(m.chat.id)=="coin_interval")
def show_coin_signal(msg):
    chat_id = msg.chat.id
    if msg.text=="🔙 Back":
        user_state[chat_id]="view_coin"
        my_coins_menu(msg)
        return
    interval = msg.text
    coin = selected_coin.get(chat_id)
    # Placeholder analysis
    bot.send_message(chat_id,f"⚪ Neutral / No signal for {coin} | {interval}")
    # stay in same menu
    coin_timeframe_menu(msg)

# ----- PLACEHOLDER HANDLERS -----
@bot.message_handler(func=lambda m: m.text in ["📈 Top Movers","📡 Signals","🛑 Stop Signals","🔄 Reset Settings","⚙️ Signal Settings","🔍 Preview Signal"])
def placeholder_handler(msg):
    chat_id = msg.chat.id
    bot.send_message(chat_id,f"✅ {msg.text} menu clicked (placeholder). Back button not implemented yet.")

# ===== FLASK ROUTES =====
@app.route("/", methods=["GET"])
def home():
    logger.info("Health check received at /")
    return "Bot is alive ✅", 200

@app.route(WEBHOOK_URL_PATH, methods=["POST"])
def webhook():
    update_json = request.get_json(force=True)
    logger.info(f"Incoming update: {update_json}")
    if "message" in update_json:
        chat_id = update_json["message"]["chat"]["id"]
        text = update_json["message"].get("text", "")
        try:
            bot.process_new_updates([telebot.types.Update.de_json(update_json)])
        except Exception as e:
            logger.error(f"Failed to process update: {e}")
    return "ok", 200

# ===== WEBHOOK SETUP =====
def setup_webhook():
    logger.info("Resetting Telegram webhook...")
    import requests
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


