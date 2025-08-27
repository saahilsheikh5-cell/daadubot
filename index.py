import os
from flask import Flask, request
import telebot
import requests
import threading
import time

# === CONFIG ===
BOT_TOKEN = os.getenv("BOT_TOKEN")  # set in Render
WEBHOOK_URL = f"https://daadubot.onrender.com/{BOT_TOKEN}"

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# === DATA STORAGE ===
user_coins = {}  # {chat_id: [coins]}
top_movers_cache = []  # list of top coins, can refresh periodically

# === BOT COMMANDS ===
@bot.message_handler(commands=['start'])
def start_message(message):
    bot.send_message(message.chat.id, "✅ Welcome! Bot is running.\nUse /help to see commands.")

@bot.message_handler(commands=['help'])
def help_message(message):
    help_text = """
Available commands:
/start - Start the bot
/help - Show this message
/addcoin <symbol> - Add a coin to your watchlist
/mycoins - Show your coins
/topmovers - Show top 50 Binance coins
/signals <symbol> - Show signals for a coin
"""
    bot.send_message(message.chat.id, help_text)

@bot.message_handler(commands=['addcoin'])
def add_coin(message):
    chat_id = message.chat.id
    try:
        symbol = message.text.split()[1].upper()
    except IndexError:
        bot.send_message(chat_id, "Please provide a coin symbol. Example: /addcoin BTCUSDT")
        return
    user_coins.setdefault(chat_id, [])
    if symbol in user_coins[chat_id]:
        bot.send_message(chat_id, f"{symbol} is already in your list.")
    else:
        user_coins[chat_id].append(symbol)
        bot.send_message(chat_id, f"{symbol} added to your watchlist.")

@bot.message_handler(commands=['mycoins'])
def my_coins(message):
    chat_id = message.chat.id
    coins = user_coins.get(chat_id, [])
    if coins:
        bot.send_message(chat_id, "Your coins:\n" + "\n".join(coins))
    else:
        bot.send_message(chat_id, "You have no coins added. Use /addcoin <symbol>.")

@bot.message_handler(commands=['topmovers'])
def top_movers(message):
    chat_id = message.chat.id
    # Fetch top 50 Binance coins by 24h change
    try:
        resp = requests.get("https://api.binance.com/api/v3/ticker/24hr")
        data = resp.json()
        data = sorted(data, key=lambda x: float(x['priceChangePercent']), reverse=True)
        top_50 = [f"{d['symbol']}: {d['priceChangePercent']}%" for d in data[:50]]
        bot.send_message(chat_id, "📈 Top 50 Binance movers:\n" + "\n".join(top_50))
    except Exception as e:
        bot.send_message(chat_id, "Error fetching top movers.")

@bot.message_handler(commands=['signals'])
def signals(message):
    chat_id = message.chat.id
    try:
        symbol = message.text.split()[1].upper()
    except IndexError:
        bot.send_message(chat_id, "Provide a coin symbol. Example: /signals BTCUSDT")
        return
    # Dummy signal example; replace with real logic
    bot.send_message(chat_id, f"Signals for {symbol}:\nUltra: Buy\nStrong: Neutral\nWeak: Sell")

# === WEBHOOK ROUTE ===
@app.route(f"/{BOT_TOKEN}", methods=['POST'])
def telegram_webhook():
    json_str = request.get_data().decode('utf-8')
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "ok", 200

# === INDEX ROUTE ===
@app.route("/")
def index():
    return "Bot service is live!", 200

# === SET WEBHOOK ===
bot.remove_webhook()
bot.set_webhook(url=WEBHOOK_URL)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

