import os
from flask import Flask, request
import telebot

BOT_TOKEN = os.getenv("BOT_TOKEN")  # make sure this is set in Render env
bot = telebot.TeleBot(BOT_TOKEN)

app = Flask(__name__)

# Example /start handler
@bot.message_handler(commands=['start'])
def start_message(message):
    bot.send_message(message.chat.id, "Welcome! Bot is running.")

# Flask route to receive Telegram webhook
@app.route(f"/{BOT_TOKEN}", methods=['POST'])
def webhook():
    json_str = request.get_data().decode('utf-8')
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "ok", 200

# Optional root route
@app.route("/")
def index():
    return "Bot service is live!", 200

# Set webhook (run once)
bot.set_webhook(url=f"https://daadubot.onrender.com/{BOT_TOKEN}")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

