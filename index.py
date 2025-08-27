import os
import logging
from flask import Flask, request
import telebot

# ---------------- CONFIG ----------------
BOT_TOKEN = "7638935379:AAEmLD7JHLZ36Ywh5tvmlP1F8xzrcNrym_Q"  # include your token
WEBHOOK_PATH = f"/{BOT_TOKEN}"
WEBHOOK_URL = f"https://daadubot.onrender.com{WEBHOOK_PATH}"

# ---------------- INIT ----------------
app = Flask(__name__)
bot = telebot.TeleBot(BOT_TOKEN)

# Enable logging
logging.basicConfig(level=logging.INFO)

# ---------------- TELEGRAM HANDLERS ----------------
@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "Hello! Your bot is live and working.")

@bot.message_handler(func=lambda message: True)
def echo_all(message):
    bot.reply_to(message, f"You said: {message.text}")

# ---------------- FLASK ROUTE ----------------
@app.route(WEBHOOK_PATH, methods=['POST'])
def webhook():
    json_str = request.get_data().decode('utf-8')
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "OK", 200

@app.route("/", methods=['GET'])
def index():
    return "Bot is running!", 200

# ---------------- SET WEBHOOK ----------------
def setup_webhook():
    if bot.get_webhook_info().url != WEBHOOK_URL:
        bot.remove_webhook()
        bot.set_webhook(url=WEBHOOK_URL)
        logging.info(f"Webhook set to {WEBHOOK_URL}")

# ---------------- MAIN ----------------
if __name__ == "__main__":
    setup_webhook()
    # Run Flask in default mode for local testing
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
