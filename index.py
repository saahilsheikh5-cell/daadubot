import os
import logging
from flask import Flask, request
import telebot
import requests

# ----------------------------
# CONFIG
# ----------------------------
BOT_TOKEN = "7638935379:AAEmLD7JHLZ36Ywh5tvmlP1F8xzrcNrym_Q"
PUBLIC_URL = "https://daadubot.onrender.com"

WEBHOOK_PATH = f"/{BOT_TOKEN}"
WEBHOOK_URL = f"{PUBLIC_URL}{WEBHOOK_PATH}"

# ----------------------------
# Logging
# ----------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("daadubot")

# ----------------------------
# Flask app
# ----------------------------
app = Flask(__name__)

# ----------------------------
# Telegram bot
# ----------------------------
bot = telebot.TeleBot(BOT_TOKEN)

# ----------------------------
# Setup webhook function
# ----------------------------
def setup_webhook():
    # Delete any existing webhook
    r1 = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook")
    logger.info(f"Delete webhook response: {r1.json()}")

    # Set new webhook
    r2 = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?url={WEBHOOK_URL}")
    logger.info(f"Set webhook response: {r2.json()}")

# ----------------------------
# Routes
# ----------------------------
@app.route("/")
def index():
    return "Bot is running!"

@app.route(WEBHOOK_PATH, methods=['POST'])
def webhook():
    json_string = request.get_data().decode('utf-8')
    update = telebot.types.Update.de_json(json_string)
    bot.process_new_updates([update])
    return "", 200

# ----------------------------
# Bot commands
# ----------------------------
@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.send_message(message.chat.id, "Hello! Your bot is live 🚀")

# ----------------------------
# Run app
# ----------------------------
if __name__ == "__main__":
    setup_webhook()  # <-- call webhook setup manually
    port = int(os.environ.get('PORT', 5000))
    app.run(host="0.0.0.0", port=port)
