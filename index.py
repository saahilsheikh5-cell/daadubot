import os
from flask import Flask, request
import telebot

BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"
bot = telebot.TeleBot(BOT_TOKEN)

app = Flask(__name__)

# --- Start command ---
@bot.message_handler(commands=['start'])
def start(message):
    bot.send_message(message.chat.id, "👋 Welcome to DaaduBot! Your bot is working ✅")

# --- Webhook route ---
@app.route(f"/{BOT_TOKEN}", methods=['POST'])
def webhook():
    json_str = request.get_data().decode('utf-8')
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "!", 200

# --- Health check ---
@app.route("/")
def health():
    return "Bot is running.", 200

# --- Set webhook (only run once) ---
if __name__ == "__main__":
    bot.remove_webhook()
    bot.set_webhook(url=f"https://daadubot.onrender.com/{BOT_TOKEN}")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

