import os
from flask import Flask, request
import telebot
import logging

BOT_TOKEN = os.environ.get("BOT_TOKEN")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "https://daadubot.onrender.com")
WEBHOOK_PATH = "/webhook"

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@app.route("/", methods=["GET"])
def home():
    return "Bot is alive ✅", 200

@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    update_json = request.get_json(force=True)
    logger.info(f"Incoming update: {update_json}")
    if "message" in update_json:
        chat_id = update_json["message"]["chat"]["id"]
        text = update_json["message"].get("text", "")
        try:
            if text == "/start":
                bot.send_message(chat_id, "✅ Bot is live and working on Render!")
            else:
                bot.send_message(chat_id, f"You said: {text}")
        except Exception as e:
            logger.error(f"Failed to send message: {e}")
    return "ok", 200

def setup_webhook():
    bot.remove_webhook()
    url = f"{PUBLIC_URL}{WEBHOOK_PATH}"
    success = bot.set_webhook(url=url)
    logger.info(f"Webhook set to {url}, success: {success}")

if __name__ == "__main__":
    setup_webhook()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))



