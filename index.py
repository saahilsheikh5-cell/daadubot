import os
import json
import telebot
from telebot import types
from binance.client import Client
import pandas as pd
import ta
import datetime
import time
import threading

# ==== ENVIRONMENT VARS ====
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")

if not TELEGRAM_TOKEN or not BINANCE_API_KEY or not BINANCE_API_SECRET:
    raise RuntimeError(
        "Please set TELEGRAM_TOKEN, BINANCE_API_KEY, BINANCE_API_SECRET env vars."
    )

# ==== INIT BOT AND BINANCE CLIENT ====
bot = telebot.TeleBot(TELEGRAM_TOKEN)
bot.remove_webhook()
client = Client(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)

COINS_FILE = "my_coins.json"
INTERVALS = ["1m", "5m", "15m", "1h", "1d"]
MIN_PRICE_FILTER = 0.5  # Avoid very small coins
ALERTED_SIGNALS = set()  # To avoid duplicate alerts

# ==== HELPERS ====
def load_coins():
    if not os.path.exists(COINS_FILE):
        return []
    with open(COINS_FILE, "r") as f:
        return json.load(f)


def save_coins(coins):
    with open(COINS_FILE, "w") as f:
        json.dump(coins, f)


def get_strong_signal(symbol, interval="5m", lookback=100):
    try:
        klines = client.get_klines(symbol=symbol, interval=interval, limit=lookback)
        df = pd.DataFrame(
            klines,
            columns=[
                "time", "o", "h", "l", "c", "v", "ct", "qav", "ntr", "tbbav", "tbqav", "ignore"
            ],
        )
        df["c"] = df["c"].astype(float)
        df["h"] = df["h"].astype(float)
        df["l"] = df["l"].astype(float)

        if df["c"].iloc[-1] < MIN_PRICE_FILTER:
            return None  # Skip small coins

        df["rsi"] = ta.momentum.RSIIndicator(df["c"], window=14).rsi()
        macd = ta.trend.MACD(df["c"])
        df["macd"] = macd.macd()
        df["macd_signal"] = macd.macd_signal()
        df["ma50"] = df["c"].rolling(50).mean()
        last = df.iloc[-1]

        decision = None
        explanation = []

        if last["rsi"] < 30 and last["macd"] > last["macd_signal"] and last["c"] > last["ma50"]:
            decision = "✅ Strong BUY"
            explanation.append("RSI oversold + MACD bullish + Above MA50")
        elif last["rsi"] > 70 and last["macd"] < last["macd_signal"] and last["c"] < last["ma50"]:
            decision = "❌ Strong SELL"
            explanation.append("RSI overbought + MACD bearish + Below MA50")

        if not decision:
            return None  # Only strong signals

        signal_text = f"""
📊 Signal for {symbol} [{interval}]
Decision: {decision}
RSI: {round(last['rsi'],2)}
MACD: {round(last['macd'],4)} / Signal: {round(last['macd_signal'],4)}
Price: {last['c']}

Entry: {round(last['c'],4)}
TP1: {round(last['c']*1.01,4)}
TP2: {round(last['c']*1.02,4)}
SL: {round(last['c']*0.99,4)}
Suggested Leverage: x10
Notes: {" | ".join(explanation)}
        """
        return signal_text
    except Exception as e:
        return f"⚠️ Error fetching data for {symbol} {interval}: {e}"


# ==== MENUS ====
def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("📈 Signals", "➕ Add Coin", "➖ Remove Coin")
    return kb


def signals_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("💼 My Coins", "🌍 All Coins")
    kb.add("🔎 Particular Coin", "🚀 Top Movers")
    kb.add("⬅️ Back")
    return kb


# ==== HANDLERS ====
@bot.message_handler(commands=["start"])
def start(message):
    bot.send_message(message.chat.id, "🤖 Welcome to Ultra Signals Bot!", reply_markup=main_menu())


@bot.message_handler(func=lambda msg: msg.text == "⬅️ Back")
def back_btn(message):
    bot.send_message(message.chat.id, "🔙 Main Menu", reply_markup=main_menu())


@bot.message_handler(func=lambda msg: msg.text == "📈 Signals")
def signals(message):
    bot.send_message(message.chat.id, "Choose a signal option:", reply_markup=signals_menu())


@bot.message_handler(func=lambda msg: msg.text == "💼 My Coins")
def my_coins(message):
    coins = load_coins()
    if not coins:
        bot.send_message(message.chat.id, "❌ No coins added yet. Use ➕ Add Coin.")
        return
    for c in coins:
        for interval in INTERVALS:
            txt = get_strong_signal(c, interval)
            if txt:
                bot.send_message(message.chat.id, txt)


@bot.message_handler(func=lambda msg: msg.text == "🌍 All Coins")
def all_coins(message):
    tickers = [s["symbol"] for s in client.get_all_tickers() if s["symbol"].endswith("USDT")]
    for c in tickers[:10]:
        for interval in INTERVALS:
            txt = get_strong_signal(c, interval)
            if txt:
                bot.send_message(message.chat.id, txt)


@bot.message_handler(func=lambda msg: msg.text == "🔎 Particular Coin")
def ask_coin(message):
    bot.send_message(message.chat.id, "Enter coin symbol (e.g., BTCUSDT):")
    bot.register_next_step_handler(message, particular_coin)


def particular_coin(message):
    symbol = message.text.upper()
    for interval in INTERVALS:
        txt = get_strong_signal(symbol, interval)
        if txt:
            bot.send_message(message.chat.id, txt)


@bot.message_handler(func=lambda msg: msg.text == "🚀 Top Movers")
def top_movers(message):
    tickers = client.get_ticker_24hr()
    sorted_tickers = sorted(tickers, key=lambda x: float(x["priceChangePercent"]), reverse=True)
    top = [t["symbol"] for t in sorted_tickers if t["symbol"].endswith("USDT")][:5]
    for c in top:
        for interval in INTERVALS:
            txt = get_strong_signal(c, interval)
            if txt:
                bot.send_message(message.chat.id, txt)


@bot.message_handler(func=lambda msg: msg.text == "➕ Add Coin")
def add_coin(message):
    bot.send_message(message.chat.id, "Enter coin symbol to add (e.g., BTCUSDT):")
    bot.register_next_step_handler(message, save_new_coin)


def save_new_coin(message):
    symbol = message.text.upper()
    coins = load_coins()
    if symbol not in coins:
        coins.append(symbol)
        save_coins(coins)
        bot.send_message(message.chat.id, f"✅ {symbol} added to My Coins")
    else:
        bot.send_message(message.chat.id, "⚠️ Coin already in list.")


@bot.message_handler(func=lambda msg: msg.text == "➖ Remove Coin")
def remove_coin(message):
    bot.send_message(message.chat.id, "Enter coin symbol to remove (e.g., BTCUSDT):")
    bot.register_next_step_handler(message, delete_coin)


def delete_coin(message):
    symbol = message.text.upper()
    coins = load_coins()
    if symbol in coins:
        coins.remove(symbol)
        save_coins(coins)
        bot.send_message(message.chat.id, f"🗑 {symbol} removed from My Coins")
    else:
        bot.send_message(message.chat.id, "⚠️ Coin not found in list.")


# ==== BACKGROUND SIGNAL CHECKER ====
def background_checker():
    while True:
        coins = load_coins()
        tickers = client.get_ticker_24hr()
        top = sorted(tickers, key=lambda x: float(x["priceChangePercent"]), reverse=True)
        top_coins = [t["symbol"] for t in top if t["symbol"].endswith("USDT")][:5]

        symbols_to_check = set(coins + top_coins)

        for sym in symbols_to_check:
            for interval in INTERVALS:
                signal_text = get_strong_signal(sym, interval)
                if signal_text and (sym, interval) not in ALERTED_SIGNALS:
                    ALERTED_SIGNALS.add((sym, interval))
                    # Broadcast to all users in coins file (or implement your chat list)
                    # For simplicity, sending to one chat: replace with your chat ID
                    bot.send_message(os.getenv("CHAT_ID"), signal_text)
        time.sleep(60)  # Check every minute


# ==== RUN BOT AND BACKGROUND THREAD ====
if __name__ == "__main__":
    print("🚀 Bot is running...")
    # Start background checker in a separate thread
    threading.Thread(target=background_checker, daemon=True).start()
    while True:
        try:
            bot.infinity_polling(timeout=10, long_polling_timeout=5)
        except Exception as e:
            print("⚠️ Bot crashed, restarting in 5s:", e)
            time.sleep(5)


