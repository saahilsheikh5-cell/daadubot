from flask import Flask, request
import telebot
import os
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
PUBLIC_URL = os.getenv("PUBLIC_URL", "https://daadubot.onrender.com")
WEBHOOK_PATH = f"/{BOT_TOKEN}"  # include bot token in path

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# COMMANDS
@bot.message_handler(commands=["start","help"])
def start(message):
    bot.send_message(message.chat.id, "✅ Bot is live and ready!")
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("➕ Add Coin","📊 My Coins")
    markup.add("➖ Remove Coin","📈 Top Movers")
    bot.send_message(message.chat.id,"🤖 Main Menu:", reply_markup=markup)

# FLASK ROUTES
@app.route("/", methods=["GET"])
def healthcheck():
    return "Bot is alive ✅", 200

@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    json_str = request.get_data().decode("utf-8")
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "ok", 200

# WEBHOOK SETUP
def setup_webhook():
    import requests
    requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook")
    url = f"{PUBLIC_URL}{WEBHOOK_PATH}"
    r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?url={url}")
    logger.info(f"Webhook set: {r.json()}")

if __name__ == "__main__":
    setup_webhook()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
