from flask import Flask, request
import logging
import requests
import telebot

# ===== LOGGING =====
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ===== CONFIG =====
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is not set!")

WEBHOOK_URL_PATH = "/webhook"
PUBLIC_URL = os.getenv("PUBLIC_URL", "https://daadubot.onrender.com")

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# ===== TELEBOT HANDLERS =====
@bot.message_handler(commands=["start", "help"])
def send_welcome(message):
    bot.reply_to(message, "🤖 Bot is live and working on Render!")

@bot.message_handler(func=lambda m: True)
def echo(message):
    bot.reply_to(message, f"You said: {message.text}")

# ===== FLASK ROUTES =====
@app.route("/", methods=["GET"])
def home():
    logger.info("Health check received at /")
    return "Bot is alive ✅", 200

@app.route(WEBHOOK_URL_PATH, methods=["POST"])
def webhook():
    try:
        update_json = request.get_data().decode("utf-8")
        logger.info(f"Raw update: {update_json}")

        update = telebot.types.Update.de_json(update_json)
        bot.process_new_updates([update])   # Let telebot handle update
    except Exception as e:
        logger.error(f"Error processing update: {e}")

    return "ok", 200

# ===== WEBHOOK SETUP =====
def setup_webhook():
    logger.info("Resetting Telegram webhook...")
    requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook")
    url = f"{PUBLIC_URL}{WEBHOOK_URL_PATH}"
    r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?url={url}")
    logger.info(

