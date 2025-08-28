from flask import Flask, request
import telebot
import os
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
PUBLIC_URL = os.getenv("PUBLIC_URL", "https://daadubot.onrender.com")
WEBHOOK_PATH = f"/webhook"  # simpler path, separate from token

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# ===== COMMAND HANDLER =====
@bot.message_handler(commands=["start", "help"])
def start(message):
    chat_id = message.chat.id
    bot.send_message(chat_id, "✅ Bot is live and working on Render!")
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("➕ Add Coin","📊 My Coins")
    markup.add("➖ Remove Coin","📈 Top Movers")
    bot.send_message(chat_id,"🤖 Main Menu:", reply_markup=markup)

# ===== FLASK ROUTES =====
@app.route("/", methods=["GET"])
def home():
    return "Bot is alive ✅", 200

@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    update_json = request.get_json(force=True)
    logger.info(f"Incoming update: {update_json}")
    try:
        update = telebot.types.Update.de_json(update_json)
        bot.process_new_updates([update])
    except Exception as e:
        logger.error(f"Failed processing update: {e}")
    return "ok", 200

# ===== WEBHOOK SETUP =====
def setup_webhook():
    import requests
    logger.info("Resetting webhook...")
    requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook")
    url = f"{PUBLIC_URL}{WEBHOOK_PATH}"
    r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?url={url}")
    logger.info(f"Webhook set: {r.json()}")

# ===== MAIN =====
if __name__ == "__main__":
    setup_webhook()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

