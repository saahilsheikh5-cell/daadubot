import os
import json
import time
import threading
import logging
import requests
import telebot
import numpy as np
import pandas as pd
from flask import Flask, request
from telebot import types

# ===== LOGGING =====
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ===== CONFIG =====
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is not set!")

PUBLIC_URL = os.getenv("PUBLIC_URL", "https://daadubot.onrender.com")
WEBHOOK_URL_PATH = f"/webhook"
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET = os.getenv("BINANCE_SECRET")
KLINES_URL = "https://api.binance.com/api/v3/klines"

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# ===== STORAGE FILES =====
USER_COINS_FILE = "user_coins.json"
SETTINGS_FILE = "settings.json"
LAST_SIGNAL_FILE = "last_signals.json"
MUTED_COINS_FILE = "muted_coins.json"
COIN_INTERVALS_FILE = "coin_intervals.json"

def load_json(file, default):
    if not os.path.exists(file):
        return default
    with open(file, "r") as f:
        return json.load(f)

def save_json(file, data):
    with open(file, "w") as f:
        json.dump(data, f, indent=4)

# Safe loading
coins = load_json(USER_COINS_FILE, [])
if not isinstance(coins, list):
    coins = []

settings = load_json(SETTINGS_FILE, {"rsi_buy":20,"rsi_sell":80,"signal_validity_min":15})
last_signals = load_json(LAST_SIGNAL_FILE, {})
muted_coins = load_json(MUTED_COINS_FILE, [])
if not isinstance(muted_coins, list):
    muted_coins = []

coin_intervals = load_json(COIN_INTERVALS_FILE, {})

# ===== TECHNICAL ANALYSIS =====
def get_klines(symbol, interval="15m", limit=100):
    try:
        data = requests.get(f"{KLINES_URL}?symbol={symbol}&interval={interval}&limit={limit}", timeout=10).json()
        if not isinstance(data, list) or len(data) == 0:
            return [], []
        closes = [float(c[4]) for c in data]
        volumes = [float(c[5]) for c in data]
        return closes, volumes
    except:
        return [], []

def rsi(data, period=14):
    if len(data) < period + 1:
        return pd.Series()
    delta = np.diff(data)
    gain = np.maximum(delta,0)
    loss = -np.minimum(delta,0)
    avg_gain = pd.Series(gain).rolling(period).mean()
    avg_loss = pd.Series(loss).rolling(period).mean()
    rs = avg_gain/avg_loss
    return 100-(100/(1+rs))

def ema(data, period=14):
    if len(data) < period:
        return []
    return pd.Series(data).ewm(span=period, adjust=False).mean().tolist()

def macd(data, fast=12, slow=26, signal=9):
    if len(data) < slow:
        return [], []
    fast_ema = pd.Series(data).ewm(span=fast, adjust=False).mean()
    slow_ema = pd.Series(data).ewm(span=slow, adjust=False).mean()
    macd_line = fast_ema - slow_ema
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line.tolist(), signal_line.tolist()

def calculate_atr(closes, period=14):
    if len(closes) < period + 1:
        return 0
    high_low = np.diff(closes)
    return np.mean(np.abs(high_low[-period:]))

def ultra_signal(symbol, interval):
    closes, volumes = get_klines(symbol, interval)
    if not closes or len(closes) < 26:
        return None

    last_close = closes[-1]
    last_vol = volumes[-1] if volumes else 0

    r_series = rsi(closes)
    if r_series.empty:
        return None
    r = r_series.iloc[-1]

    m, s = macd(closes)
    if len(m) == 0 or len(s) == 0:
        return None

    e_list = ema(closes, 20)
    if not e_list:
        return None
    e = e_list[-1]

    atr = calculate_atr(closes, 14)
    leverage = min(50, max(1, int(100/atr))) if atr>0 else 5
    entry = last_close
    sl = entry - atr if r < settings["rsi_buy"] else entry + atr
    tp1 = entry + atr*1.5 if r < settings["rsi_buy"] else entry - atr*1.5
    tp2 = entry + atr*3 if r < settings["rsi_buy"] else entry - atr*3
    confidence = "High" if (r<settings["rsi_buy"] and m[-1]>s[-1] and last_close>e and last_vol>np.mean(volumes)) or \
                       (r>settings["rsi_sell"] and m[-1]<s[-1] and last_close<e and last_vol>np.mean(volumes)) else "Medium"

    strong_buy = r < settings["rsi_buy"] and m[-1] > s[-1] and last_close > e and last_vol > np.mean(volumes)
    strong_sell = r > settings["rsi_sell"] and m[-1] < s[-1] and last_close < e and last_vol > np.mean(volumes)

    if strong_buy:
        return f"🟢 ULTRA STRONG BUY | {symbol} | {interval}\nEntry: {entry:.4f}\nSL: {sl:.4f}\nTP1: {tp1:.4f}\nTP2: {tp2:.4f}\nLeverage: {leverage}x\nConfidence: {confidence}"
    elif strong_sell:
        return f"🔴 ULTRA STRONG SELL | {symbol} | {interval}\nEntry: {entry:.4f}\nSL: {sl:.4f}\nTP1: {tp1:.4f}\nTP2: {tp2:.4f}\nLeverage: {leverage}x\nConfidence: {confidence}"
    else:
        return f"⚪ Neutral / No signal for {symbol} | {interval}"

# ===== SIGNAL MANAGEMENT =====
def send_signal_if_new(coin, interval, sig):
    global last_signals, muted_coins
    if coin in muted_coins: return
    key = f"{coin}_{interval}"
    now_ts = time.time()
    if key not in last_signals or now_ts - last_signals[key] > settings["signal_validity_min"]*60:
        bot.send_message(chat_id=CHAT_ID, text=f"⚡ {sig}")
        last_signals[key] = now_ts
        save_json(LAST_SIGNAL_FILE, last_signals)

def signal_scanner():
    while True:
        active_coins = coins if coins else ["BTCUSDT","ETHUSDT","SOLUSDT"]
        for c in active_coins:
            intervals = coin_intervals.get(c, ["1m","5m","15m","1h","4h","1d"])
            for interval in intervals:
                sig = ultra_signal(c, interval)
                if sig:
                    send_signal_if_new(c, interval, sig)
        time.sleep(60)

threading.Thread(target=signal_scanner, daemon=True).start()

# ===== USER STATE =====
user_state = {}
selected_coin = {}
selected_interval = {}

# ===== MENU HELPERS =====
def main_menu(chat_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("➕ Add Coin","📊 My Coins")
    markup.add("➖ Remove Coin","📈 Top Movers")
    markup.add("📡 Signals","🛑 Stop Signals")
    markup.add("🔄 Reset Settings","⚙️ Signal Settings","🔍 Preview Signal")
    bot.send_message(chat_id, "🤖 Main Menu:", reply_markup=markup)
    user_state[chat_id] = None

# ===== FLASK ROUTES =====
@app.route("/", methods=["GET"])
def home():
    logger.info("Health check received at /")
    return "Bot is alive ✅", 200

@app.route(WEBHOOK_URL_PATH, methods=["POST"])
def webhook():
    update = request.get_json(force=True)
    logger.info(f"Incoming update: {update}")

    if "message" in update:
        chat_id = update["message"]["chat"]["id"]
        text = update["message"].get("text", "")

        if text.startswith("/start") or text.startswith("/help"):
            bot.send_message(chat_id, "✅ Bot is live and working on Render!")
            main_menu(chat_id)
            return "ok", 200

        # --- Handle Add Coin ---
        if user_state.get(chat_id) == "adding_coin":
            coin = text.upper()
            if not coin.isalnum():
                bot.send_message(chat_id,"❌ Invalid coin symbol.")
            elif coin not in coins:
                coins.append(coin)
                save_json(USER_COINS_FILE, coins)
                bot.send_message(chat_id,f"✅ {coin} added.")
            else:
                bot.send_message(chat_id,f"{coin} already exists.")
            user_state[chat_id] = None
            main_menu(chat_id)
            return "ok", 200

        if text == "➕ Add Coin":
            bot.send_message(chat_id,"Type coin symbol (e.g., BTCUSDT):")
            user_state[chat_id] = "adding_coin"
            return "ok", 200

        if text == "➖ Remove Coin":
            if not coins:
                bot.send_message(chat_id,"No coins to remove.")
                main_menu(chat_id)
            else:
                markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
                for c in coins:
                    markup.add(c)
                markup.add("🔙 Back")
                bot.send_message(chat_id,"Select coin to remove:", reply_markup=markup)
                user_state[chat_id] = "removing_coin"
            return "ok", 200

        if user_state.get(chat_id) == "removing_coin":
            coin = text.upper()
            if coin in coins:
                coins.remove(coin)
                save_json(USER_COINS_FILE, coins)
                bot.send_message(chat_id,f"✅ {coin} removed.")
            elif coin == "🔙 Back":
                pass
            else:
                bot.send_message(chat_id,"❌ Coin not in list.")
            user_state[chat_id] = None
            main_menu(chat_id)
            return "ok", 200

        # --- My Coins ---
        if text == "📊 My Coins":
            if not coins:
                bot.send_message(chat_id,"No coins added yet.")
                main_menu(chat_id)
            else:
                markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
                for c in coins:
                    markup.add(c)
                markup.add("🔙 Back")
                bot.send_message(chat_id,"Select a coin to view:", reply_markup=markup)
                user_state[chat_id] = "my_coins"
            return "ok", 200

        if user_state.get(chat_id) == "my_coins":
            coin = text.upper()
            if coin in coins:
                markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
                for interval in ["1m","5m","15m","1h","4h","1d"]:
                    markup.add(interval)
                markup.add("🔙 Back")
                bot.send_message(chat_id,f"Select interval for {coin}:", reply_markup=markup)
                selected_coin[chat_id] = coin
                user_state[chat_id] = "coin_interval"
            elif text == "🔙 Back":
                main_menu(chat_id)
            return "ok", 200

        if user_state.get(chat_id) == "coin_interval":
            interval = text
            coin = selected_coin.get(chat_id)
            if interval in ["1m","5m","15m","1h","4h","1d"]:
                sig = ultra_signal(coin, interval)
                bot.send_message(chat_id, sig)
            elif interval == "🔙 Back":
                main_menu(chat_id)
            return "ok", 200

        # --- Top Movers ---
        if text == "📈 Top Movers":
            markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
            markup.add("5m","1h","24h","🔙 Back")
            bot.send_message(chat_id,"Select timeframe for Top Movers:", reply_markup=markup)
            user_state[chat_id] = "top_movers"
            return "ok", 200

        if user_state.get(chat_id) == "top_movers":
            tf = text
            if tf in ["5m","1h","24h"]:
                bot.send_message(chat_id,f"Top movers for {tf} timeframe (mock): BTC, ETH, SOL")
            main_menu(chat_id)
            user_state[chat_id] = None
            return "ok", 200

        # --- Signals / Stop Signals / Reset / Settings / Preview --- TODO: Similar pattern
        # For brevity, you can replicate the same pattern as My Coins

    return "ok", 200

# ===== WEBHOOK SETUP =====
def setup_webhook():
    logger.info("Resetting Telegram webhook...")
    requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook")
    url = f"{PUBLIC_URL}{WEBHOOK_URL_PATH}"
    r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?url={url}")
    logger.info(f"Webhook set response: {r.json()}")

# ===== MAIN =====
if __name__ == "__main__":
    setup_webhook()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
else:
    setup_webhook()


               



