import os
from flask import Flask, request
import telebot

# ---------------- CONFIG ----------------
BOT_TOKEN = "7638935379:AAEmLD7JHLZ36Ywh5tvmlP1F8xzrcNrym_Q"
WEBHOOK_URL_BASE = "https://daadubot.onrender.com"
WEBHOOK_URL_PATH = f"/{BOT_TOKEN}"

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# ---------------- TELEGRAM COMMANDS ----------------
@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "Hello! Bot is running successfully 🚀")

# Example: add more commands here
@bot.message_handler(commands=['help'])
def send_help(message):
    bot.reply_to(message, "This is your help message.")

# ---------------- FLASK ROUTES ----------------
@app.route("/")
def index():
    return "Bot server is running!"

@app.route(WEBHOOK_URL_PATH, methods=['POST'])
def webhook():
    json_str = request.get_data().decode('utf-8')
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "!", 200

# ---------------- WEBHOOK SETUP ----------------
def setup_webhook():
    # Remove old webhook
    bot.remove_webhook()
    # Set new webhook
    bot.set_webhook(url=WEBHOOK_URL_BASE + WEBHOOK_URL_PATH)
    print(f"Webhook set to {WEBHOOK_URL_BASE + WEBHOOK_URL_PATH}")

# ---------------- RUN ON START ----------------
if __name__ == "__main__":
    setup_webhook()
    # Only use Flask built-in server for local testing
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
else:
    # If running with Gunicorn, set webhook once
    setup_webhook()
