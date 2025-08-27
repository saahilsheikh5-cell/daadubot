import os
from flask import Flask, request
import telebot

# === CONFIG ===
BOT_TOKEN = "7638935379:AAEmLD7JHLZ36Ywh5tvmlP1F8xzrcNrym_Q"
WEBHOOK_URL = f"https://daadubot.onrender.com/{BOT_TOKEN}"

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# === Telegram handlers ===
@bot.message_handler(commands=['start'])
def start_message(message):
    bot.send_message(message.chat.id, "Hello! Bot is working ✅")

# === Flask route for webhook ===
@app.route(f"/{BOT_TOKEN}", methods=['POST'])
def webhook():
    json_str = request.get_data().decode('utf-8')
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "!", 200

# === Set webhook on startup ===
@app.before_first_request
def setup_webhook():
    bot.remove_webhook()
    bot.set_webhook(url=WEBHOOK_URL)
    print(f"Webhook set: {WEBHOOK_URL}")

# === Optional root route ===
@app.route("/")
def index():
    return "Bot service is live 🎉", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
