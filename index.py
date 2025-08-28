import os
import telebot
from flask import Flask, request

# ===== CONFIG =====
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL_PATH = "/webhook"   # fixed endpoint
PUBLIC_URL = os.getenv("PUBLIC_URL", "https://daadubot.onrender.com")

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# ===== COMMAND HANDLERS =====
@bot.message_handler(commands=["start", "help"])
def send_welcome(message):
    bot.reply_to(message, "✅ Bot is live and working on Render!")

# ===== FLASK ROUTES =====
@app.route("/", methods=["GET"])
def index():
    return "Bot is running!", 200

@app.route(WEBHOOK_URL_PATH, methods=["POST"])
def webhook():
    update = request.get_json(force=True)
    if update:
        bot.process_new_updates([telebot.types.Update.de_json(update)])
    return "ok", 200

# ===== SET WEBHOOK =====
def setup_webhook():
    import requests
    # remove old webhook
    requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook")
    # set new webhook to Render’s /webhook endpoint
    url = f"{PUBLIC_URL}{WEBHOOK_URL_PATH}"
    r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?url={url}")
    print("Webhook set:", r.json())

# ===== MAIN =====
if __name__ == "__main__":
    setup_webhook()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
else:
    setup_webhook()
