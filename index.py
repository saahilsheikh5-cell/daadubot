import os
from flask import Flask, request
import logging
import telebot
import requests

# ===== LOGGING =====
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ===== CONFIG =====
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is not set!")

PUBLIC_URL = os.getenv("PUBLIC_URL", "https://daadubot.onrender.com")
WEBHOOK_URL_PATH = "/webhook"

# ===== FLASK & TELEBOT =====
app = Flask(__name__)
bot = telebot.TeleBot(BOT_TOKEN)

# ===== ROUTES =====
@app.route("/", methods=["GET"])
def home():
    logger.info("Health check received at /")
    return "Bot is alive ✅", 200

@app.route(WEBHOOK_URL_PATH, methods=["POST"])
def webhook():
    update_json = request.get_json(force=True)
    logger.info(f"Incoming update: {update_json}")

    if "message" in update_json:
        chat_id = update_json["message"]["chat"]["id"]
        text = update_json["message"].get("text", "")
        try:
            if text.startswith("/start") or text.startswith("/help"):
                bot.send_message(chat_id, "✅ Bot is live and ready!")
        except Exception as e:
            logger.error(f"Failed to send message: {e}")

    return "ok", 200

# ===== WEBHOOK SETUP =====
def setup_webhook():
    logger.info("Resetting Telegram webhook...")
    requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook")
    url = f"{PUBLIC_URL}{WEBHOOK_URL_PATH}"
    r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?url={url}")
    logger.info(f"Webhook set response: {r.json()}")

# ===== MAIN =====
if __name__ == "__main__":
    setup_webhook()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))


               



