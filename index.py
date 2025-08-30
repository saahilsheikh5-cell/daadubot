import os
import telebot
from telebot import types
import pandas as pd
import requests
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

# ==============================
# BOT SETUP
# ==============================
TOKEN = os.environ.get("TELEGRAM_TOKEN")
bot = telebot.TeleBot(TOKEN)
bot.remove_webhook()  # Prevent 409 error on Render

# ==============================
# USER STATE STORAGE
# ==============================
user_coins = {}        # {chat_id: [coin1, coin2, ...]}
user_settings = {}     # {chat_id: {"timeframe": "15m", "auto": False}}

# ==============================
# HELPER FUNCTIONS
# ==============================

def get_klines(symbol, interval, limit=100):
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol.upper()}USDT&interval={interval}&limit={limit}"
    data = requests.get(url).json()
    df = pd.DataFrame(data, columns=[
        "time", "open", "high", "low", "close", "volume",
        "close_time", "quote_asset_volume", "number_of_trades",
        "taker_buy_base", "taker_buy_quote", "ignore"
    ])
    df["close"] = df["close"].astype(float)
    return df

def technical_analysis(symbol, interval):
    try:
        df = get_klines(symbol, interval)
        close = df["close"]

        ema20 = close.ewm(span=20).mean().iloc[-1]
        ema50 = close.ewm(span=50).mean().iloc[-1]
        rsi = compute_rsi(close).iloc[-1]
        macd_val, signal_val = compute_macd(close)

        # Simple support/resistance
        support = min(close.tail(20))
        resistance = max(close.tail(20))

        # Leverage suggestion logic (basic)
        leverage = suggest_leverage(rsi)

        return (
            f"📊 Technical Analysis for {symbol} ({interval})\n\n"
            f"EMA20: {ema20:.2f}\n"
            f"EMA50: {ema50:.2f}\n"
            f"RSI: {rsi:.2f}\n"
            f"MACD: {macd_val:.2f}, Signal: {signal_val:.2f}\n"
            f"Support: {support:.2f}, Resistance: {resistance:.2f}\n"
            f"⚡ Suggested Leverage: {leverage}x"
        )
    except Exception as e:
        return f"❌ Error fetching data: {e}"

def compute_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def compute_macd(series):
    ema12 = series.ewm(span=12).mean()
    ema26 = series.ewm(span=26).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9).mean()
    return macd.iloc[-1], signal.iloc[-1]

def suggest_leverage(rsi):
    if rsi < 30:
        return 10
    elif rsi > 70:
        return 5
    else:
        return 20

# ==============================
# BOT COMMANDS
# ==============================

@bot.message_handler(commands=['start'])
def start_message(message):
    chat_id = message.chat.id
    if chat_id not in user_coins:
        user_coins[chat_id] = []
    if chat_id not in user_settings:
        user_settings[chat_id] = {"timeframe": "15m", "auto": False}

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("📈 My Coins", "➕ Add Coin", "➖ Remove Coin")
    markup.row("⚙️ Auto Signals", "🛑 Stop Signals", "🔄 Reset Settings")
    markup.row("🚀 Top Movers", "📊 Signal Settings")

    bot.send_message(chat_id, "🤖 Bot is live!\nChoose an option:", reply_markup=markup)

# ==============================
# MENU HANDLERS
# ==============================

@bot.message_handler(func=lambda m: m.text == "📈 My Coins")
def my_coins(message):
    chat_id = message.chat.id
    coins = user_coins.get(chat_id, [])
    if not coins:
        bot.send_message(chat_id, "❌ No coins added yet.")
        return

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for c in coins:
        markup.row(f"{c} - Analysis")
    markup.row("⬅️ Back")
    bot.send_message(chat_id, "📌 Select a coin for analysis:", reply_markup=markup)

@bot.message_handler(func=lambda m: "Analysis" in m.text)
def coin_analysis(message):
    chat_id = message.chat.id
    symbol = message.text.split(" - ")[0]

    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for tf in ["1m", "5m", "15m", "1h", "1d"]:
        markup.row(f"{symbol} ({tf})")
    markup.row("⬅️ Back")
    bot.send_message(chat_id, f"⏳ Choose timeframe for {symbol}:", reply_markup=markup)

@bot.message_handler(func=lambda m: "(" in m.text and ")" in m.text)
def timeframe_analysis(message):
    chat_id = message.chat.id
    text = message.text
    symbol = text.split(" (")[0]
    tf = text.split("(")[1].replace(")", "")
    result = technical_analysis(symbol, tf)
    bot.send_message(chat_id, result)

@bot.message_handler(func=lambda m: m.text == "➕ Add Coin")
def add_coin(message):
    bot.send_message(message.chat.id, "✍️ Send me the coin symbol (e.g., BTC, ETH):")
    bot.register_next_step_handler(message, save_coin)

def save_coin(message):
    chat_id = message.chat.id
    coin = message.text.upper()
    user_coins.setdefault(chat_id, []).append(coin)
    bot.send_message(chat_id, f"✅ {coin} added!")

@bot.message_handler(func=lambda m: m.text == "➖ Remove Coin")
def remove_coin(message):
    chat_id = message.chat.id
    coins = user_coins.get(chat_id, [])
    if not coins:
        bot.send_message(chat_id, "❌ You have no coins added.")
        return
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for c in coins:
        markup.row(f"Remove {c}")
    markup.row("⬅️ Back")
    bot.send_message(chat_id, "🗑 Select coin to remove:", reply_markup=markup)

@bot.message_handler(func=lambda m: m.text.startswith("Remove "))
def confirm_remove(message):
    chat_id = message.chat.id
    coin = message.text.replace("Remove ", "")
    if coin in user_coins.get(chat_id, []):
        user_coins[chat_id].remove(coin)
        bot.send_message(chat_id, f"✅ {coin} removed!")

@bot.message_handler(func=lambda m: m.text == "⚙️ Auto Signals")
def auto_signals(message):
    chat_id = message.chat.id
    user_settings[chat_id]["auto"] = True
    bot.send_message(chat_id, "✅ Auto signals enabled!")

@bot.message_handler(func=lambda m: m.text == "🛑 Stop Signals")
def stop_signals(message):
    chat_id = message.chat.id
    user_settings[chat_id]["auto"] = False
    bot.send_message(chat_id, "🛑 Auto signals stopped.")

@bot.message_handler(func=lambda m: m.text == "🔄 Reset Settings")
def reset_settings(message):
    chat_id = message.chat.id
    user_coins[chat_id] = []
    user_settings[chat_id] = {"timeframe": "15m", "auto": False}
    bot.send_message(chat_id, "♻️ Settings reset to default.")

@bot.message_handler(func=lambda m: m.text == "🚀 Top Movers")
def top_movers(message):
    # For now, just placeholder
    bot.send_message(message.chat.id, "🚀 Top movers feature coming soon!")

@bot.message_handler(func=lambda m: m.text == "📊 Signal Settings")
def signal_settings(message):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for tf in ["1m", "5m", "15m", "1h", "1d"]:
        markup.row(f"Set timeframe {tf}")
    markup.row("⬅️ Back")
    bot.send_message(message.chat.id, "⚙️ Choose default signal timeframe:", reply_markup=markup)

@bot.message_handler(func=lambda m: m.text.startswith("Set timeframe"))
def set_timeframe(message):
    chat_id = message.chat.id
    tf = message.text.replace("Set timeframe ", "")
    user_settings[chat_id]["timeframe"] = tf
    bot.send_message(chat_id, f"✅ Default timeframe set to {tf}")

# ==============================
# AUTO SIGNAL LOOP
# ==============================
def auto_signal_loop():
    while True:
        for chat_id, settings in user_settings.items():
            if settings.get("auto") and user_coins.get(chat_id):
                tf = settings.get("timeframe", "15m")
                for coin in user_coins[chat_id]:
                    result = technical_analysis(coin, tf)
                    bot.send_message(chat_id, result)
        time.sleep(60)  # check every 1 min

threading.Thread(target=auto_signal_loop, daemon=True).start()

# ==============================
# THREAD SAFE POLLING
# ==============================
def start_bot():
    bot.infinity_polling()

threading.Thread(target=start_bot, daemon=True).start()

# ==============================
# DUMMY SERVER FOR RENDER
# ==============================
PORT = int(os.environ.get("PORT", 5000))

class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running on Render!")

server = HTTPServer(("0.0.0.0", PORT), DummyHandler)
print(f"✅ Dummy HTTP server running on port {PORT}...")
server.serve_forever()



