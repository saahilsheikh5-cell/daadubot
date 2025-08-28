import os
import json
import time
import threading
import requests
import logging
import telebot
import numpy as np
import pandas as pd
from flask import Flask, request
from telebot import types

# ===== LOGGING =====
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ===== CONFIG =====
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is not set!")

PUBLIC_URL = os.getenv("PUBLIC_URL", "https://daadubot.onrender.com")
WEBHOOK_URL_PATH = "/webhook"
CHAT_ID = int(os.getenv("CHAT_ID", 0))  # optional for signals

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# ===== STORAGE FILES =====
USER_COINS_FILE = "user_coins.json"
SETTINGS_FILE = "settings.json"
LAST_SIGNAL_FILE = "last_signals.json"
MUTED_COINS_FILE = "muted_coins.json"
COIN_INTERVALS_FILE = "coin_intervals.json"

def load_json(file, default):
    if not os.path.exists(file):
        return default
    with open(file, "r") as f:
        return json.load(f)

def save_json(file, data):
    with open(file, "w") as f:
        json.dump(data, f, indent=4)

# ===== DATA =====
coins = load_json(USER_COINS_FILE, [])
settings = load_json(SETTINGS_FILE, {"rsi_buy": 20, "rsi_sell": 80, "signal_validity_min": 15})
last_signals = load_json(LAST_SIGNAL_FILE, {})
muted_coins = load_json(MUTED_COINS_FILE, [])
coin_intervals = load_json(COIN_INTERVALS_FILE, {})

# ===== USER STATE =====
user_state = {}
selected_coin = {}
selected_interval = {}

# ===== FLASK ROUTES =====
@app.route("/", methods=["GET"])
def home():
    logger.info("Health check received at /")
    return "Bot is alive ✅", 200

@app.route(WEBHOOK_URL_PATH, methods=["POST"])
def webhook():
    update = request.get_json(force=True)
    logger.info(f"Incoming update: {update}")

    if update and "message" in update:
        chat_id = update["message"]["chat"]["id"]
        text = update["message"].get("text", "")
        try:
            if text.startswith("/start") or text.startswith("/help"):
                bot.send_message(chat_id, "🤖 Bot is live and ready!")
                main_menu(chat_id)
            else:
                handle_text(chat_id, text)
        except Exception as e:
            logger.error(f"Failed to process message: {e}")

    return "ok", 200

# ===== WEBHOOK SETUP =====
def setup_webhook():
    logger.info("Resetting Telegram webhook...")
    requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook")
    url = f"{PUBLIC_URL}{WEBHOOK_URL_PATH}"
    r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?url={url}")
    logger.info(f"Webhook set response: {r.json()}")

# ===== MENU HANDLERS =====
def main_menu(chat_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("➕ Add Coin", "📊 My Coins")
    markup.add("➖ Remove Coin", "📈 Top Movers")
    markup.add("📡 Signals", "🛑 Stop Signals")
    markup.add("🔄 Reset Settings", "⚙️ Signal Settings", "🔍 Preview Signal")
    bot.send_message(chat_id, "🤖 Main Menu:", reply_markup=markup)
    user_state[chat_id] = None

def handle_text(chat_id, text):
    state = user_state.get(chat_id)
    
    # ----- Add Coin -----
    if text == "➕ Add Coin":
        bot.send_message(chat_id, "Type coin symbol (e.g., BTCUSDT):")
        user_state[chat_id] = "adding_coin"
    elif state == "adding_coin":
        coin = text.upper()
        if not coin.isalnum():
            bot.send_message(chat_id, "❌ Invalid coin symbol.")
        elif coin not in coins:
            coins.append(coin)
            save_json(USER_COINS_FILE, coins)
            bot.send_message(chat_id, f"✅ {coin} added.")
        else:
            bot.send_message(chat_id, f"{coin} already exists.")
        user_state[chat_id] = None
        main_menu(chat_id)

    # ----- Remove Coin -----
    elif text == "➖ Remove Coin":
        if not coins:
            bot.send_message(chat_id, "⚠️ No coins to remove.")
            main_menu(chat_id)
            return
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        for c in coins:
            markup.add(c)
        markup.add("🔙 Back")
        bot.send_message(chat_id, "Select coin to remove:", reply_markup=markup)
        user_state[chat_id] = "removing_coin"
    elif state == "removing_coin":
        coin = text.upper()
        if coin in coins:
            coins.remove(coin)
            save_json(USER_COINS_FILE, coins)
            bot.send_message(chat_id, f"✅ {coin} removed.")
        elif text == "🔙 Back":
            bot.send_message(chat_id, "Going back.")
        else:
            bot.send_message(chat_id, "❌ Coin not in list.")
        user_state[chat_id] = None
        main_menu(chat_id)

    # ----- My Coins -----
    elif text == "📊 My Coins":
        if not coins:
            bot.send_message(chat_id, "⚠️ No coins added yet.")
            main_menu(chat_id)
            return
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        for c in coins:
            markup.add(c)
        markup.add("🔙 Back")
        bot.send_message(chat_id, "Select a coin to view:", reply_markup=markup)
        user_state[chat_id] = "view_coin"

    elif state == "view_coin":
        if text == "🔙 Back":
            main_menu(chat_id)
            user_state[chat_id] = None
            return
        coin = text.upper()
        if coin not in coins:
            bot.send_message(chat_id, "❌ Coin not in your list.")
            return
        # Show intervals for selected coin
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        intervals = ["1m", "5m", "15m", "1h", "4h", "1d"]
        for i in intervals:
            markup.add(i)
        markup.add("🔙 Back")
        selected_coin[chat_id] = coin
        bot.send_message(chat_id, f"Select interval for {coin}:", reply_markup=markup)
        user_state[chat_id] = "view_coin_interval"

    elif state == "view_coin_interval":
        if text == "🔙 Back":
            main_menu(chat_id)
            user_state[chat_id] = None
            return
        coin = selected_coin.get(chat_id)
        interval = text
        # Placeholder for technical analysis
        bot.send_message(chat_id, f"⚪ Neutral / No signal for {coin} | {interval}")
        user_state[chat_id] = "view_coin_interval"  # stay in interval selection

    # Other buttons placeholders
    elif text in ["📈 Top Movers", "📡 Signals", "🛑 Stop Signals", "🔄 Reset Settings", "⚙️ Signal Settings", "🔍 Preview Signal"]:
        bot.send_message(chat_id, f"✅ {text} clicked — feature in progress.")
    else:
        bot.send_message(chat_id, f"You said: {text}")

# ===== MAIN =====
if __name__ == "__main__":
    setup_webhook()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
else:
    setup_webhook()


