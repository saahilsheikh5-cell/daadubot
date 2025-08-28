from flask import Flask, request
import telebot
import os
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "https://daadubot.onrender.com")
WEBHOOK_PATH = "/webhook"

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# ================= COMMAND HANDLERS =================
@bot.message_handler(commands=['start', 'help'])
def start(message):
    bot.send_message(message.chat.id, "✅ Bot is live and ready!")
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("➕ Add Coin", "📊 My Coins")
    markup.add("➖ Remove Coin", "📈 Top Movers")
    markup.add("📡 Signals", "🛑 Stop Signals")
    markup.add("🔄 Reset Settings", "⚙️ Signal Settings", "🔍 Preview Signal")
    bot.send_message(message.chat.id, "🤖 Main Menu:", reply_markup=markup)

# ================= FLASK ROUTES =================
@app.route("/", methods=["GET"])
def home():
    return "Bot is alive ✅", 200

@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    update_json = request.get_json()
    logger.info(f"Incoming update: {update_json}")
    try:
        bot.process_new_updates([telebot.types.Update.de_json(update_json)])
    except Exception as e:
        logger.error(f"Error processing update: {e}")
    return "ok", 200

# ================= WEBHOOK SETUP =================
def setup_webhook():
    import requests
    logger.info("Resetting Telegram webhook...")
    requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook")
    url = f"{PUBLIC_URL}{WEBHOOK_PATH}"
    r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?url={url}")
    logger.info(f"Webhook set response: {r.json()}")

# ================= MAIN =================
if __name__ == "__main__":
    setup_webhook()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))


