import os
import time
import threading
import requests
import telebot
from flask import Flask, request
from telebot import types

# ===== CONFIG =====
BOT_TOKEN = os.environ.get("BOT_TOKEN")
BINANCE_KEY = os.environ.get("BINANCE_KEY")
BINANCE_SECRET = os.environ.get("BINANCE_SECRET")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")  # e.g., https://yourrenderapp.onrender.com

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# In-memory storage (replace with DB if needed)
user_coins = {}
timeframes = ["1m", "5m", "15m", "1h", "4h", "1d"]

# ===== FLASK ROUTES =====
@app.route("/", methods=["GET"])
def health():
    return "Bot service is live!", 200

@app.route("/" + BOT_TOKEN, methods=["POST"])
def webhook():
    json_str = request.get_data().decode("utf-8")
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "", 200

# ===== TELEGRAM HANDLERS =====
@bot.message_handler(commands=["start", "help"])
def start_handler(message):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("My Coins", "Add Coin", "Remove Coin")
    markup.row("Auto Signals", "Top Movers", "Technical Analysis")
    bot.send_message(message.chat.id, "Welcome! Choose an option:", reply_markup=markup)

@bot.message_handler(func=lambda m: m.text == "My Coins")
def my_coins(message):
    chat_id = message.chat.id
    coins = user_coins.get(chat_id, [])
    if not coins:
        bot.send_message(chat_id, "You have no coins added yet.")
        return
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for coin in coins:
        markup.add(coin)
    markup.add("Back")
    bot.send_message(chat_id, "Select a coin for analysis:", reply_markup=markup)

@bot.message_handler(func=lambda m: m.text == "Add Coin")
def add_coin(message):
    msg = bot.send_message(message.chat.id, "Send the coin symbol to add (e.g., BTCUSDT):")
    bot.register_next_step_handler(msg, add_coin_step)

def add_coin_step(message):
    chat_id = message.chat.id
    coin = message.text.upper()
    user_coins.setdefault(chat_id, []).append(coin)
    bot.send_message(chat_id, f"{coin} added!")

@bot.message_handler(func=lambda m: m.text == "Remove Coin")
def remove_coin(message):
    chat_id = message.chat.id
    coins = user_coins.get(chat_id, [])
    if not coins:
        bot.send_message(chat_id, "You have no coins to remove.")
        return
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for coin in coins:
        markup.add(coin)
    msg = bot.send_message(chat_id, "Select a coin to remove:", reply_markup=markup)
    bot.register_next_step_handler(msg, remove_coin_step)

def remove_coin_step(message):
    chat_id = message.chat.id
    coin = message.text.upper()
    coins = user_coins.get(chat_id, [])
    if coin in coins:
        coins.remove(coin)
        bot.send_message(chat_id, f"{coin} removed!")
    else:
        bot.send_message(chat_id, f"{coin} not found in your list.")

@bot.message_handler(func=lambda m: m.text == "Technical Analysis")
def technical_analysis(message):
    chat_id = message.chat.id
    coins = user_coins.get(chat_id, [])
    if not coins:
        bot.send_message(chat_id, "Add coins first.")
        return
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for coin in coins:
        markup.add(coin)
    markup.add("Back")
    msg = bot.send_message(chat_id, "Select a coin for technical analysis:", reply_markup=markup)
    bot.register_next_step_handler(msg, analysis_coin_step)

def analysis_coin_step(message):
    coin = message.text.upper()
    chat_id = message.chat.id
    for tf in timeframes:
        # Placeholder for actual analysis (replace with Binance API logic)
        signal = f"{coin} - {tf}: Neutral"
        bot.send_message(chat_id, signal)

@bot.message_handler(func=lambda m: m.text == "Top Movers")
def top_movers(message):
    chat_id = message.chat.id
    # Placeholder for top movers logic
    bot.send_message(chat_id, "Top Movers: BTC, ETH, SOL")

@bot.message_handler(func=lambda m: m.text == "Auto Signals")
def auto_signals(message):
    chat_id = message.chat.id
    bot.send_message(chat_id, "Auto Signals started. You will receive updates automatically.")
    threading.Thread(target=send_auto_signals, args=(chat_id,), daemon=True).start()

def send_auto_signals(chat_id):
    while True:
        # Placeholder logic for auto signals (replace with real API logic)
        for coin in user_coins.get(chat_id, []):
            bot.send_message(chat_id, f"Auto Signal for {coin}: BUY")
        time.sleep(60)

# ===== START BOT WITH WEBHOOK =====
def set_webhook():
    url = f"{WEBHOOK_URL}/{BOT_TOKEN}"
    bot.remove_webhook()
    bot.set_webhook(url=url)

if __name__ == "__main__":
    set_webhook()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
