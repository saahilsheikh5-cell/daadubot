import os
import telebot
import requests
import pandas as pd
import numpy as np
import json
import time
import threading
from flask import Flask, request
from telebot import types

# ================= CONFIG =================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHAT_ID = int(os.environ.get("CHAT_ID"))
WEBHOOK_URL = f"https://solbot.onrender.com/{BOT_TOKEN}"
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY")
BINANCE_SECRET = os.environ.get("BINANCE_SECRET")
KLINES_URL = "https://api.binance.com/api/v3/klines"

bot = telebot.TeleBot(BOT_TOKEN)

# ================= STORAGE =================
USER_COINS_FILE = "user_coins.json"
SETTINGS_FILE = "settings.json"
LAST_SIGNAL_FILE = "last_signals.json"
MUTED_COINS_FILE = "muted_coins.json"
COIN_INTERVALS_FILE = "coin_intervals.json"

def load_json(file, default):
    if not os.path.exists(file):
        return default
    with open(file,"r") as f:
        return json.load(f)

def save_json(file,data):
    with open(file,"w") as f:
        json.dump(data,f, indent=4)

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

# ================= TECHNICAL ANALYSIS =================
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
        return None

# ================= SIGNAL MANAGEMENT =================
def send_signal_if_new(coin, interval, sig):
    global last_signals, muted_coins
    if coin in muted_coins: return
    key = f"{coin}_{interval}"
    now_ts = time.time()
    if key not in last_signals or now_ts - last_signals[key] > settings["signal_validity_min"]*60:
        bot.send_message(CHAT_ID,f"⚡ {sig}")
        last_signals[key] = now_ts
        save_json(LAST_SIGNAL_FILE,last_signals)

# Auto signal scanner
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

# ================= USER STATE & MENUS =================
user_state = {}
selected_coin = {}
selected_interval = {}

# ----- MAIN MENU -----
def main_menu(msg):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("➕ Add Coin","📊 My Coins")
    markup.add("➖ Remove Coin","📈 Top Movers")
    markup.add("📡 Signals","🛑 Stop Signals")
    markup.add("🔄 Reset Settings","⚙️ Signal Settings","🔍 Preview Signal")
    bot.send_message(msg.chat.id,"🤖 Main Menu:", reply_markup=markup)
    user_state[msg.chat.id]=None

@bot.message_handler(commands=["start"])
def start(msg):
    bot.send_message(msg.chat.id,"✅ Bot deployed and running!")
    main_menu(msg)

# ---------------- ADD / REMOVE COIN ------------------
@bot.message_handler(func=lambda m: m.text=="➕ Add Coin")
def add_coin_menu(msg):
    chat_id = msg.chat.id
    bot.send_message(chat_id,"Type coin symbol (e.g., BTCUSDT):")
    user_state[chat_id] = "adding_coin"

@bot.message_handler(func=lambda m: user_state.get(m.chat.id)=="adding_coin")
def process_add_coin(msg):
    chat_id = msg.chat.id
    coin = msg.text.upper()
    if not coin.isalnum():
        bot.send_message(chat_id,"❌ Invalid coin symbol.")
    elif coin not in coins:
        coins.append(coin)
        save_json(USER_COINS_FILE, coins)
        bot.send_message(chat_id,f"✅ {coin} added.")
    else:
        bot.send_message(chat_id,f"{coin} already exists.")
    user_state[chat_id] = None
    main_menu(msg)

@bot.message_handler(func=lambda m: m.text=="➖ Remove Coin")
def remove_coin_menu(msg):
    chat_id = msg.chat.id
    if not coins:
        bot.send_message(chat_id,"⚠️ No coins to remove.")
        main_menu(msg)
        return
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for c in coins:
        markup.add(c)
    markup.add("🔙 Back")
    bot.send_message(chat_id,"Select coin to remove:", reply_markup=markup)
    user_state[chat_id] = "removing_coin"

@bot.message_handler(func=lambda m: user_state.get(m.chat.id)=="removing_coin")
def process_remove_coin(msg):
    chat_id = msg.chat.id
    coin = msg.text.upper()
    if coin in coins:
        coins.remove(coin)
        save_json(USER_COINS_FILE, coins)
        bot.send_message(chat_id,f"✅ {coin} removed.")
    else:
        bot.send_message(chat_id,"❌ Coin not in list.")
    user_state[chat_id] = None
    main_menu(msg)

# ---------------- FLASK WEBHOOK ------------------
app = Flask(__name__)

bot.remove_webhook()
bot.set_webhook(url=WEBHOOK_URL)

@app.route("/" + BOT_TOKEN, methods=["POST"])
def webhook():
    json_str = request.get_data().decode("utf-8")
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "!", 200

@app.route("/")
def index():
    return "Bot is running!", 200

# ================= HEALTH CHECK =================
def health_check():
    while True:
        try:
            requests.get(os.environ.get("HEALTH_URL","http://localhost:5000/"))
        except:
            pass
        time.sleep(300)

threading.Thread(target=health_check, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

