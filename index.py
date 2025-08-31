import os
from flask import Flask, request
import telebot

# Get environment variables
TOKEN = os.getenv("TELEGRAM_TOKEN")
URL = os.getenv("RENDER_EXTERNAL_URL")  # Render sets this automatically
if not TOKEN:
    raise ValueError("❌ TELEGRAM_TOKEN not set in environment variables")

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

# Root check
@app.route("/", methods=["GET"])
def index():
    return "🤖 Bot is running via webhook!", 200

# Telegram webhook receiver
@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    update = request.get_data().decode("utf-8")
    bot.process_new_updates([telebot.types.Update.de_json(update)])
    return "OK", 200

# Bot command
@bot.message_handler(commands=["start"])
def start_handler(message):
    bot.reply_to(message, "✅ Webhook mode active! Your bot is running on Render.")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    # Remove old webhook before setting a new one
    bot.remove_webhook()
    webhook_url = f"{URL}/{TOKEN}"
    bot.set_webhook(url=webhook_url)
    print(f"🚀 Webhook set: {webhook_url}")
    app.run(host="0.0.0.0", port=port)





