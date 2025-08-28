import os
import random
import logging
import requests
import telebot
from flask import Flask, request
from telebot import types

# ===== LOGGING =====
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ===== CONFIG =====
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is not set!")

WEBHOOK_URL_PATH = "/webhook"
PUBLIC_URL = os.getenv("PUBLIC_URL", "https://daadubot.onrender.com")

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# ===== USER DATA =====
user_data = {}

# ===== HELPER: Fake Technical Analysis =====
def fake_analysis(symbol, interval):
    signals = ["🟢 Strong Buy", "🔴 Strong Sell", "🟡 Neutral"]
    return (
        f"📊 Technical Analysis for {symbol} ({interval})\n"
        f"RSI: {random.randint(20, 80)}\n"
        f"MACD: {random.choice(['Bullish', 'Bearish'])}\n"
        f"Signal: {random.choice(signals)}"
    )

# ===== MAIN MENU =====
def main_menu(chat_id, text="🤖 Main Menu:"):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("➕ Add Coin", "➖ Remove Coin")
    markup.add("📊 My Coins", "📈 Top Movers")
    markup.add("📡 Signals", "🛑 Stop Signals")
    markup.add("🔄 Reset Settings", "⚙️ Signal Settings")
    markup.add("🔍 Preview Signal")
    bot.send_message(chat_id, text, reply_markup=markup)

# ===== COMMANDS =====
@bot.message_handler(commands=["start", "help"])
def send_welcome(message):
    chat_id = message.chat.id
    if chat_id not in user_data:
        user_data[chat_id] = {"coins": []}
    main_menu(chat_id, "✅ Bot is live! Welcome.")

# ===== ADD COIN =====
@bot.message_handler(func=lambda msg: msg.text == "➕ Add Coin")
def add_coin(message):
    bot.send_message(message.chat.id, "Type coin symbol (e.g., BTCUSDT):")
    bot.register_next_step_handler(message, process_add_coin)

def process_add_coin(message):
    coin = message.text.strip().upper()
    chat_id = message.chat.id
    if coin not in user_data[chat_id]["coins"]:
        user_data[chat_id]["coins"].append(coin)
        bot.send_message(chat_id, f"✅ {coin} added.")
    else:
        bot.send_message(chat_id, f"⚠️ {coin} already in list.")
    main_menu(chat_id)

# ===== REMOVE COIN =====
@bot.message_handler(func=lambda msg: msg.text == "➖ Remove Coin")
def remove_coin(message):
    chat_id = message.chat.id
    coins = user_data.get(chat_id, {}).get("coins", [])
    if not coins:
        bot.send_message(chat_id, "⚠️ No coins to remove.")
        return main_menu(chat_id)

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for c in coins:
        markup.add(c)
    markup.add("🔙 Back")
    bot.send_message(chat_id, "Select coin to remove:", reply_markup=markup)

@bot.message_handler(func=lambda msg: msg.text in ["🔙 Back"])
def go_back(message):
    main_menu(message.chat.id)

@bot.message_handler(func=lambda msg: True)
def handle_text(message):
    chat_id = message.chat.id
    text = message.text.strip()

    # --- My Coins ---
    if text == "📊 My Coins":
        coins = user_data.get(chat_id, {}).get("coins", [])
        if not coins:
            bot.send_message(chat_id, "⚠️ No coins added yet.")
            return main_menu(chat_id)
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        for c in coins:
            markup.add(c)
        markup.add("🔙 Back")
        return bot.send_message(chat_id, "Select a coin to view:", reply_markup=markup)

    if text in user_data.get(chat_id, {}).get("coins", []):
        user_data[chat_id]["selected_coin"] = text
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        for tf in ["1m", "5m", "15m", "1h", "1d"]:
            markup.add(tf)
        markup.add("🔙 Back")
        return bot.send_message(chat_id, f"Select interval for {text}:", reply_markup=markup)

    if text in ["1m", "5m", "15m", "1h", "1d"]:
        coin = user_data[chat_id].get("selected_coin")
        if coin:
            analysis = fake_analysis(coin, text)
            return bot.send_message(chat_id, analysis)

    # --- Top Movers ---
    if text == "📈 Top Movers":
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add("5m Movers", "1h Movers", "24h Movers")
        markup.add("🔙 Back")
        return bot.send_message(chat_id, "Select timeframe for movers:", reply_markup=markup)

    if text in ["5m Movers", "1h Movers", "24h Movers"]:
        return bot.send_message(chat_id, "🚧 Movers feature in progress.")

    # --- Signals ---
    if text == "📡 Signals":
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add("My Coins Signals", "All Coins Signals", "Particular Coin Signals")
        markup.add("🔙 Back")
        return bot.send_message(chat_id, "Select signal type:", reply_markup=markup)

    if text in ["My Coins Signals", "All Coins Signals", "Particular Coin Signals"]:
        return bot.send_message(chat_id, "🚧 Signals feature in progress.")

    # --- Stop Signals ---
    if text == "🛑 Stop Signals":
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add("Stop My Coins", "Stop All Coins", "Stop Particular Coin")
        markup.add("🔙 Back")
        return bot.send_message(chat_id, "Select stop option:", reply_markup=markup)

    if text in ["Stop My Coins", "Stop All Coins", "Stop Particular Coin"]:
        return bot.send_message(chat_id, "🚧 Stop Signals feature in progress.")

    # --- Reset Settings ---
    if text == "🔄 Reset Settings":
        return bot.send_message(chat_id, "🚧 Reset Settings feature in progress.")

    # --- Signal Settings ---
    if text == "⚙️ Signal Settings":
        return bot.send_message(chat_id, "🚧 Signal Settings feature in progress.")

    # --- Preview Signal ---
    if text == "🔍 Preview Signal":
        return bot.send_message(chat_id, "🚧 Preview Signal feature in progress.")

    # Default
    bot.send_message(chat_id, "❓ Unknown command. Use menu buttons.")

# ===== FLASK ROUTES =====
@app.route("/", methods=["GET"])
def home():
    return "Bot is alive ✅", 200

@app.route(WEBHOOK_URL_PATH, methods=["POST"])
def webhook():
    update_json = request.get_json(force=True)
    logger.info(f"Incoming update: {update_json}")
    bot.process_new_updates([telebot.types.Update.de_json(update_json)])
    return "ok", 200

# ===== WEBHOOK SETUP =====
def setup_webhook():
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



