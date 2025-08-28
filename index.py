import os
from flask import Flask, request
import telebot
import logging

# ===== CONFIG =====
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is not set!")

PUBLIC_URL = os.environ.get("PUBLIC_URL", "https://daadubot.onrender.com")
WEBHOOK_PATH = "/webhook"

# ===== LOGGING =====
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ===== TELEBOT =====
bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# ===== FLASK ROUTES =====
@app.route("/", methods=["GET"])
def home():
    logger.info("Health check received at /")
    return "Bot is alive ✅", 200

@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    update = request.get_json(force=True)
    logger.info(f"Incoming update: {update}")
    
    try:
        # Convert incoming JSON to Telebot Update object
        t_update = telebot.types.Update.de_json(update)
        bot.process_new_updates([t_update])
    except Exception as e:
        logger.error(f"Error processing update: {e}")

    return "ok", 200

# ===== COMMAND HANDLER =====
@bot.message_handler(commands=["start", "help"])
def send_welcome(message):
    chat_id = message.chat.id
    bot.send_message(chat_id, "✅ Bot is live and working on Render!\n🤖 Main Menu will appear here soon.")

# ===== WEBHOOK SETUP =====
def setup_webhook():
    logger.info("Resetting Telegram webhook...")
    # Delete previous webhook
    bot.remove_webhook()
    # Set new webhook
    webhook_url = f"{PUBLIC_URL}{WEBHOOK_PATH}"
    success = bot.set_webhook(url=webhook_url)
    logger.info(f"Webhook set to {webhook_url}, success: {success}")

# ===== MAIN =====
if __name__ == "__main__":
    setup_webhook()
    # Run Flask server for Render
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))


