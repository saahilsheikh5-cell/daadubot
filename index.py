import os
import telebot
from flask import Flask, request
import threading
import time
import pandas as pd
import numpy as np

# ===== CONFIG =====
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
APP_URL = os.environ.get("APP_URL", "https://daadubot.onrender.com")
bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# ===== DATA STORAGE =====
# In-memory storage, replace with DB for production
user_coins = {}  # {chat_id: [coin1, coin2, ...]}

# ===== TELEGRAM HANDLERS =====
@bot.message_handler(commands=['start'])
def start_command(message):
    bot.send_message(
        message.chat.id,
        "Welcome to DaaduBot! 🤖\n\nUse the buttons below to access signals and analysis."
    )
    show_main_menu(message)

def show_main_menu(message):
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(
        telebot.types.KeyboardButton("My Coins"),
        telebot.types.KeyboardButton("Add/Remove Coin"),
    )
    markup.add(
        telebot.types.KeyboardButton("Auto Signals"),
        telebot.types.KeyboardButton("Top Movers"),
    )
    bot.send_message(message.chat.id, "Main Menu:", reply_markup=markup)

# ===== MENU HANDLER =====
@bot.message_handler(func=lambda message: True)
def menu_handler(message):
    chat_id = message.chat.id
    text = message.text

    if text == "My Coins":
        coins = user_coins.get(chat_id, [])
        if coins:
            bot.send_message(chat_id, "Your coins: " + ", ".join(coins))
        else:
            bot.send_message(chat_id, "You have no coins added yet.")
    elif text == "Add/Remove Coin":
        markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
        markup.add("Add Coin", "Remove Coin", "Back to Menu")
        bot.send_message(chat_id, "Choose an action:", reply_markup=markup)
    elif text == "Add Coin":
        msg = bot.send_message(chat_id, "Send the coin symbol to add (e.g., BTCUSDT):")
        bot.register_next_step_handler(msg, add_coin)
    elif text == "Remove Coin":
        coins = user_coins.get(chat_id, [])
        if coins:
            markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
            for c in coins:
                markup.add(c)
            markup.add("Back to Menu")
            msg = bot.send_message(chat_id, "Select coin to remove:", reply_markup=markup)
            bot.register_next_step_handler(msg, remove_coin)
        else:
            bot.send_message(chat_id, "No coins to remove.")
    elif text == "Auto Signals":
        bot.send_message(chat_id, "Auto Signals feature coming soon...")
    elif text == "Top Movers":
        bot.send_message(chat_id, "Top Movers feature coming soon...")
    elif text == "Back to Menu":
        show_main_menu(message)
    else:
        bot.send_message(chat_id, "Please select a valid option from the menu.")

# ===== ADD / REMOVE COIN FUNCTIONS =====
def add_coin(message):
    chat_id = message.chat.id
    coin = message.text.upper()
    user_coins.setdefault(chat_id, [])
    if coin not in user_coins[chat_id]:
        user_coins[chat_id].append(coin)
        bot.send_message(chat_id, f"{coin} added to your list.")
    else:
        bot.send_message(chat_id, f"{coin} is already in your list.")
    show_main_menu(message)

def remove_coin(message):
    chat_id = message.chat.id
    coin = message.text.upper()
    if coin in user_coins.get(chat_id, []):
        user_coins[chat_id].remove(coin)
        bot.send_message(chat_id, f"{coin} removed from your list.")
    else:
        bot.send_message(chat_id, "Coin not found.")
    show_main_menu(message)

# ===== FLASK WEBHOOK =====
@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def telegram_webhook():
    json_str = request.get_data().decode("utf-8")
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "!", 200

@app.route("/")
def health_check():
    return "DaaduBot is live! ✅", 200

# ===== SET WEBHOOK =====
if __name__ == "__main__":
    bot.remove_webhook()
    bot.set_webhook(url=f"{APP_URL}/{BOT_TOKEN}")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
