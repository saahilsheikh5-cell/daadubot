from flask import Flask, request
import telebot
import os

BOT_TOKEN = os.environ.get("BOT_TOKEN")
bot = telebot.TeleBot(BOT_TOKEN)

app = Flask(__name__)

@app.route(f'/{BOT_TOKEN}', methods=['POST'])
def webhook():
    json_str = request.get_data().decode('utf-8')
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "OK", 200

@app.route("/")
def index():
    return "Bot is running! ✅"

# Example command
@bot.message_handler(commands=['start'])
def start(message):
    bot.send_message(message.chat.id, "👋 Welcome!\nUse /addcoin to track a coin")

if __name__ == "__main__":
    # Set webhook automatically
    bot.remove_webhook()
    bot.set_webhook(url=f"https://daadubot.onrender.com/{BOT_TOKEN}")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

