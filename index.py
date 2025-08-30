import os
import json
import threading
import time
import telebot
from telebot import types
from binance.client import Client
import pandas as pd
import ta

# ==== ENVIRONMENT VARS ====
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_SECRET")  # Note: use same key name as Render env var

MIN_VOLUME_USDT = 100000  # Minimum 24h volume filter for top movers

if not TELEGRAM_TOKEN or not BINANCE_API_KEY or not BINANCE_API_SECRET:
    raise RuntimeError("Please set TELEGRAM_TOKEN, BINANCE_API_KEY, BINANCE_API_SECRET env vars.")

bot = telebot.TeleBot(TELEGRAM_TOKEN)
client = Client(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)

COINS_FILE = "my_coins.json"

# ==== HELPERS ====
def load_coins():
    if not os.path.exists(COINS_FILE):
        return []
    with open(COINS_FILE, "r") as f:
        return json.load(f)


def save_coins(coins):
    with open(COINS_FILE, "w") as f:
        json.dump(coins, f)


def get_signal(symbol, interval="5m", lookback=100):
    try:
        klines = client.get_klines(symbol=symbol, interval=interval, limit=lookback)
        df = pd.DataFrame(klines, columns=[
            "time", "o", "h", "l", "c", "v", "ct", "qav", "ntr", "tbbav", "tbqav", "ignore"
        ])
        df["c"] = df["c"].astype(float)
        df["h"] = df["h"].astype(float)
        df["l"] = df["l"].astype(float)
        df["v"] = df["v"].astype(float)

        # Indicators
        df["rsi"] = ta.momentum.RSIIndicator(df["c"], window=14).rsi()
        macd = ta.trend.MACD(df["c"])
        df["macd"] = macd.macd()
        df["macd_signal"] = macd.macd_signal()
        df["ma50"] = df["c"].rolling(50).mean()
        df["bb_upper"] = ta.volatility.BollingerBands(df["c"], window=20).bollinger_hband()
        df["bb_lower"] = ta.volatility.BollingerBands(df["c"], window=20).bollinger_lband()
        df["stoch"] = ta.momentum.StochasticOscillator(df["h"], df["l"], df["c"]).stoch()
        last = df.iloc[-1]

        decision = "Neutral"
        explanation = []

        # Strong signal conditions
        if last["rsi"] < 30 and last["macd"] > last["macd_signal"] and last["c"] > last["ma50"]:
            decision = "✅ Ultra BUY"
            explanation.append("RSI oversold + MACD bullish + Above MA50")
        elif last["rsi"] > 70 and last["macd"] < last["macd_signal"] and last["c"] < last["ma50"]:
            decision = "❌ Ultra SELL"
            explanation.append("RSI overbought + MACD bearish + Below MA50")

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
Notes: {" | ".join(explanation) if explanation else "Mixed signals"}
        """
        return signal_text, decision
    except Exception as e:
        return f"⚠️ Error fetching data for {symbol} {interval}: {e}", "Neutral"


def get_top_movers(limit=5):
    tickers = client.get_ticker()
    filtered = [t for t in tickers if float(t['quoteVolume']) > MIN_VOLUME_USDT and t['symbol'].endswith("USDT")]
    sorted_tickers = sorted(filtered, key=lambda x: abs(float(x['priceChangePercent'])), reverse=True)
    return [t['symbol'] for t in sorted_tickers[:limit]]


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


def top_movers_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("1️⃣ 1 Min", "5️⃣ 5 Min", "1️⃣5️⃣ 15 Min")
    kb.add("⬅️ Back")
    return kb


# ==== TELEGRAM HANDLERS ====
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
        for interval in ["1m", "5m", "15m", "1h", "1d"]:
            txt, decision = get_signal(c, interval)
            if decision != "Neutral":
                bot.send_message(message.chat.id, txt)


@bot.message_handler(func=lambda msg: msg.text == "🌍 All Coins")
def all_coins(message):
    tickers = [s["symbol"] for s in client.get_all_tickers() if s["symbol"].endswith("USDT")]
    for c in tickers[:10]:
        txt, decision = get_signal(c, "5m")
        if decision != "Neutral":
            bot.send_message(message.chat.id, txt)


@bot.message_handler(func=lambda msg: msg.text == "🔎 Particular Coin")
def ask_coin(message):
    bot.send_message(message.chat.id, "Enter coin symbol (e.g., BTCUSDT):")
    bot.register_next_step_handler(message, particular_coin)


def particular_coin(message):
    symbol = message.text.upper()
    for interval in ["1m", "5m", "15m", "1h", "1d"]:
        txt, decision = get_signal(symbol, interval)
        if decision != "Neutral":
            bot.send_message(message.chat.id, txt)


@bot.message_handler(func=lambda msg: msg.text == "🚀 Top Movers")
def top_movers_handler(message):
    bot.send_message(message.chat.id, "Select timeframe for Top Movers signals:", reply_markup=top_movers_menu())


@bot.message_handler(func=lambda msg: msg.text in ["1️⃣ 1 Min", "5️⃣ 5 Min", "1️⃣5️⃣ 15 Min"])
def top_movers_signals(message):
    interval_map = {"1️⃣ 1 Min": "1m", "5️⃣ 5 Min": "5m", "1️⃣5️⃣ 15 Min": "15m"}
    interval = interval_map[message.text]
    movers = get_top_movers(limit=5)
    for c in movers:
        txt, decision = get_signal(c, interval)
        if decision != "Neutral":
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


# ==== BACKGROUND SCALPING ALERTS ====
def continuous_alerts():
    while True:
        coins = load_coins()
        for c in coins:
            for interval in ["1m", "5m"]:
                txt, decision = get_signal(c, interval)
                if decision != "Neutral":
                    bot.send_message(chat_id=os.getenv("CHAT_ID"), text=txt)
        time.sleep(30)  # check every 30 seconds


threading.Thread(target=continuous_alerts, daemon=True).start()

# ==== RUN ====
print("🚀 Bot is running...")
bot.infinity_polling()

