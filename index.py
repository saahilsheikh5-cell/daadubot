import os
import telebot
from flask import Flask, request

# ===== BOT CONFIG =====
BOT_TOKEN = "7638935379:AAEmLD7JHLZ36Ywh5tvmlP1F8xzrcNrym_Q"
bot = telebot.TeleBot(BOT_TOKEN)

# ===== FLASK APP =====
app = Flask(__name__)

# ===== TELEGRAM COMMANDS =====
@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.send_message(message.chat.id, "Hello! Welcome to DaaduBot.\nUse /help to see commands.")

@bot.message_handler(commands=['help'])
def send_help(message):
    help_text = "/start - Welcome message\n/help - This help message"
    bot.send_message(message.chat.id, help_text)

# ===== FLASK ROUTE FOR WEBHOOK =====
@app.route(f"/{BOT_TOKEN}", methods=['POST'])
def webhook():
    json_str = request.get_data().decode('utf-8')
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "OK", 200

# ===== ROOT =====
@app.route("/", methods=['GET'])
def index():
    return "Bot is running!", 200

# ===== MAIN =====
if __name__ == "__main__":
    # Detect environment
    RUN_MODE = os.environ.get("RUN_MODE", "polling")  # default is polling

    if RUN_MODE == "polling":
        print("Starting bot in polling mode...")
        bot.infinity_polling()
    else:
        print("Starting Flask server for webhook...")
        app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
