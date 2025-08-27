import os
import requests
import telebot
from flask import Flask, request

# ================== CONFIG ==================
BOT_TOKEN = os.environ.get('BOT_TOKEN')  # Your Telegram bot token in Render env
bot = telebot.TeleBot(BOT_TOKEN)

GITHUB_NAME = "daadubot"
APP_URL = f"https://{GITHUB_NAME}.onrender.com"  # Your Render app URL

# ================== FLASK APP ==================
app = Flask(__name__)

# Health check endpoint
@app.route('/')
def index():
    return "Bot is running! ✅"

# Telegram webhook endpoint
@app.route(f'/{BOT_TOKEN}', methods=['POST'])
def webhook():
    json_str = request.get_data().decode('utf-8')
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return 'OK', 200

# ================== TELEGRAM COMMANDS ==================
@bot.message_handler(commands=['start'])
def start_message(message):
    bot.send_message(message.chat.id,
                     "👋 Welcome!\n\n"
                     "Use /addcoin to track a coin\n"
                     "Use /topcoins to see top 50 coins\n"
                     "Use /track <symbol> to track a particular coin")

# Example: add coin command
@bot.message_handler(commands=['addcoin'])
def add_coin(message):
    bot.send_message(message.chat.id, "Send the symbol of the coin you want to add (e.g., BTC, ETH):")

# Example: top coins command
@bot.message_handler(commands=['topcoins'])
def top_coins(message):
    bot.send_message(message.chat.id, "Top 50 Binance coins feature coming soon!")

# Example: track a coin command
@bot.message_handler(commands=['track'])
def track_coin(message):
    try:
        symbol = message.text.split()[1].upper()
        bot.send_message(message.chat.id, f"Now tracking {symbol} ✅")
    except IndexError:
        bot.send_message(message.chat.id, "Usage: /track <symbol> (e.g., /track BTC)")

# ================== SET WEBHOOK ==================
def set_webhook():
    webhook_url = f"{APP_URL}/{BOT_TOKEN}"
    r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?url={webhook_url}")
    print("Webhook set:", r.json())

# ================== START APP ==================
if __name__ == "__main__":
    set_webhook()
    # Use Flask's default port, Render sets $PORT automatically
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

