import os
from flask import Flask, request
import telebot

# ===== CONFIG =====
BOT_TOKEN = os.environ.get("BOT_TOKEN")  # Set your bot token as an env variable
HEROKU_URL = os.environ.get("HEROKU_URL")  # or your Render URL: https://daadubot.onrender.com

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# ===== Bot Commands =====
@bot.message_handler(commands=['start'])
def start(message):
    bot.send_message(message.chat.id, "👋 Welcome! Your bot is working ✅")

@bot.message_handler(commands=['help'])
def help_command(message):
    bot.send_message(message.chat.id, "Available commands:\n/start - Welcome message\n/help - This message")

# ===== Flask Routes =====
@app.route("/", methods=["GET"])
def index():
    return "Bot is running! ✅"

@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def webhook():
    json_str = request.get_data().decode("utf-8")
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "OK", 200

# ===== Start Webhook =====
if __name__ == "__main__":
    bot.remove_webhook()
    bot.set_webhook(url=f"{HEROKU_URL}/{BOT_TOKEN}")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))


