import os
import telebot
from flask import Flask, request
from telebot import types

# ================== CONFIG ==================
BOT_TOKEN = "7638935379:AAEmLD7JHLZ36Ywh5tvmlP1F8xzrcNrym_Q"
bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# Example coins list (top 50 Binance coins can be added here)
TOP_COINS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "ADAUSDT"]  # Extend as needed
user_coins = {}  # dictionary to store user-added coins

# ================== COMMANDS ==================
@bot.message_handler(commands=['start'])
def send_welcome(message):
    markup = types.ReplyKeyboardMarkup(row_width=2, resize_keyboard=True)
    btn1 = types.KeyboardButton("/mycoins")
    btn2 = types.KeyboardButton("/addcoin")
    btn3 = types.KeyboardButton("/topcoins")
    markup.add(btn1, btn2, btn3)
    bot.send_message(message.chat.id, "Welcome! Choose an option:", reply_markup=markup)

@bot.message_handler(commands=['mycoins'])
def my_coins(message):
    coins = user_coins.get(message.chat.id, [])
    if not coins:
        bot.send_message(message.chat.id, "You have no coins added yet.")
    else:
        bot.send_message(message.chat.id, "Your coins: " + ", ".join(coins))

@bot.message_handler(commands=['addcoin'])
def add_coin(message):
    msg = bot.send_message(message.chat.id, "Send the coin symbol you want to add (e.g., BTCUSDT):")
    bot.register_next_step_handler(msg, process_add_coin)

def process_add_coin(message):
    coin = message.text.upper()
    if message.chat.id not in user_coins:
        user_coins[message.chat.id] = []
    if coin not in user_coins[message.chat.id]:
        user_coins[message.chat.id].append(coin)
        bot.send_message(message.chat.id, f"{coin} added to your coins.")
    else:
        bot.send_message(message.chat.id, f"{coin} is already in your coins.")

@bot.message_handler(commands=['topcoins'])
def top_coins(message):
    bot.send_message(message.chat.id, "Top coins:\n" + ", ".join(TOP_COINS))

# ================== WEBHOOK ROUTE ==================
@app.route(f"/{BOT_TOKEN}", methods=['POST'])
def webhook():
    json_str = request.get_data().decode('utf-8')
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "OK", 200

# Health check
@app.route("/")
def health():
    return "Bot is running", 200

# ================== MAIN ==================
if __name__ == "__main__":
    # Set webhook for Telegram
    WEBHOOK_URL = f"https://daadubot.onrender.com/{BOT_TOKEN}"
    bot.remove_webhook()
    bot.set_webhook(url=WEBHOOK_URL)
    print(f"Webhook set: {WEBHOOK_URL}")
    
    # Run Flask app
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

