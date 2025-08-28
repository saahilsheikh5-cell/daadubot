import os
import json
import time
import threading
import requests
import telebot
from flask import Flask, request
from telebot import types
import pandas as pd
import numpy as np

# ===== CONFIG =====
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = int(os.getenv("CHAT_ID", "0"))
PUBLIC_URL = os.getenv("PUBLIC_URL", "https://daadubot.onrender.com")
WEBHOOK_PATH = f"/webhook"

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET = os.getenv("BINANCE_SECRET")
KLINES_URL = "https://api.binance.com/api/v3/klines"

bot = telebot.TeleBot(BOT_TOKEN)
app = Flask(__name__)

# ===== STORAGE =====
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

coins = load_json(USER_COINS_FILE, [])
settings = load_json(SETTINGS_FILE, {"rsi_buy":20,"rsi_sell":80,"signal_validity_min":15})
last_signals = load_json(LAST_SIGNAL_FILE, {})
muted_coins = load_json(MUTED_COINS_FILE, [])
coin_intervals = load_json(COIN_INTERVALS_FILE, {})

# ===== TECHNICAL ANALYSIS PLACEHOLDER =====
def get_klines(symbol, interval="15m", limit=100):
    try:
        data = requests.get(f"{KLINES_URL}?symbol={symbol}&interval={interval}&limit={limit}", timeout=10).json()
        if not isinstance(data, list) or len(data) == 0: return [], []
        closes = [float(c[4]) for c in data]
        volumes = [float(c[5]) for c in data]
        return closes, volumes
    except:
        return [], []

def rsi(data, period=14):
    if len(data) < period + 1: return pd.Series()
    delta = np.diff(data)
    gain = np.maximum(delta,0)
    loss = -np.minimum(delta,0)
    avg_gain = pd.Series(gain).rolling(period).mean()
    avg_loss = pd.Series(loss).rolling(period).mean()
    rs = avg_gain/avg_loss
    return 100-(100/(1+rs))

def ema(data, period=14):
    if len(data) < period: return []
    return pd.Series(data).ewm(span=period, adjust=False).mean().tolist()

def macd(data, fast=12, slow=26, signal=9):
    if len(data) < slow: return [], []
    fast_ema = pd.Series(data).ewm(span=fast, adjust=False).mean()
    slow_ema = pd.Series(data).ewm(span=slow, adjust=False).mean()
    macd_line = fast_ema - slow_ema
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line.tolist(), signal_line.tolist()

def calculate_atr(closes, period=14):
    if len(closes) < period + 1: return 0
    high_low = np.diff(closes)
    return np.mean(np.abs(high_low[-period:]))

def ultra_signal(symbol, interval):
    closes, volumes = get_klines(symbol, interval)
    if not closes or len(closes) < 26: return None
    last_close = closes[-1]
    last_vol = volumes[-1] if volumes else 0
    r = rsi(closes).iloc[-1] if not rsi(closes).empty else 50
    m, s = macd(closes)
    e_list = ema(closes, 20)
    e = e_list[-1] if e_list else last_close
    atr = calculate_atr(closes, 14)
    leverage = min(50, max(1, int(100/atr))) if atr>0 else 5
    entry = last_close
    sl = entry - atr if r < settings["rsi_buy"] else entry + atr
    tp1 = entry + atr*1.5 if r < settings["rsi_buy"] else entry - atr*1.5
    tp2 = entry + atr*3 if r < settings["rsi_buy"] else entry - atr*3
    confidence = "High" if (r<settings["rsi_buy"] and m[-1]>s[-1] and last_close>e) or (r>settings["rsi_sell"] and m[-1]<s[-1] and last_close<e) else "Medium"
    if (r < settings["rsi_buy"] and m[-1] > s[-1]): return f"🟢 ULTRA BUY {symbol} {interval}\nEntry:{entry}\nSL:{sl}\nTP1:{tp1}\nTP2:{tp2}\nConf:{confidence}"
    if (r > settings["rsi_sell"] and m[-1] < s[-1]): return f"🔴 ULTRA SELL {symbol} {interval}\nEntry:{entry}\nSL:{sl}\nTP1:{tp1}\nTP2:{tp2}\nConf:{confidence}"
    return f"⚪ Neutral / No signal for {symbol} | {interval}"

# ===== SIGNAL MANAGEMENT =====
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
                if sig: send_signal_if_new(c, interval, sig)
        time.sleep(60)

threading.Thread(target=signal_scanner, daemon=True).start()

# ===== USER STATE =====
user_state = {}
selected_coin = {}
selected_interval = {}

# ===== MAIN MENU =====
def main_menu(msg):
    chat_id = msg.chat.id
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("➕ Add Coin","📊 My Coins")
    markup.add("➖ Remove Coin","📈 Top Movers")
    markup.add("📡 Signals","🛑 Stop Signals")
    markup.add("🔄 Reset Settings","⚙️ Signal Settings","🔍 Preview Signal")
    bot.send_message(chat_id,"🤖 Main Menu:", reply_markup=markup)
    user_state[chat_id]=None

# ===== /START =====
@bot.message_handler(commands=["start"])
def start(msg):
    bot.send_message(msg.chat.id,"✅ Bot live and working on Render!")
    main_menu(msg)

# ===== ADD / REMOVE COIN =====
@bot.message_handler(func=lambda m: m.text=="➕ Add Coin")
def add_coin_menu(msg):
    chat_id = msg.chat.id
    bot.send_message(chat_id,"Type coin symbol (e.g., BTCUSDT):")
    user_state[chat_id] = "adding_coin"

@bot.message_handler(func=lambda m: user_state.get(m.chat.id)=="adding_coin")
def process_add_coin(msg):
    chat_id = msg.chat.id
    coin = msg.text.upper()
    if not coin.isalnum(): bot.send_message(chat_id,"❌ Invalid coin symbol.")
    elif coin not in coins:
        coins.append(coin)
        save_json(USER_COINS_FILE, coins)
        bot.send_message(chat_id,f"✅ {coin} added.")
    else: bot.send_message(chat_id,f"{coin} already exists.")
    user_state[chat_id] = None
    main_menu(msg)

@bot.message_handler(func=lambda m: m.text=="➖ Remove Coin")
def remove_coin_menu(msg):
    chat_id = msg.chat.id
    if not coins: bot.send_message(chat_id,"⚠️ No coins to remove."); main_menu(msg); return
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for c in coins: markup.add(c)
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
    else: bot.send_message(chat_id,"❌ Coin not in list.")
    user_state[chat_id] = None
    main_menu(msg)

# ===== MY COINS MENU WITH TIMEFRAMES =====
@bot.message_handler(func=lambda m: m.text=="📊 My Coins")
def my_coins_menu(msg):
    chat_id = msg.chat.id
    if not coins:
        bot.send_message(chat_id,"⚠️ No coins added."); main_menu(msg); return
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for c in coins: markup.add(c)
    markup.add("🔙 Back")
    bot.send_message(chat_id,"Select a coin to view:", reply_markup=markup)
    user_state[chat_id]="selecting_coin"

@bot.message_handler(func=lambda m: user_state.get(m.chat.id)=="selecting_coin")
def select_coin(msg):
    chat_id = msg.chat.id
    coin = msg.text.upper()
    if coin=="🔙 BACK": main_menu(msg); return
    if coin not in coins: bot.send_message(chat_id,"❌ Coin not in your list."); my_coins_menu(msg); return
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for tf in ["1m","5m","15m","1h","4h","1d"]: markup.add(tf)
    markup.add("🔙 Back")
    bot.send_message(chat_id,f"Select interval for {coin}:", reply_markup=markup)
    user_state[chat_id]="select_interval"
    selected_coin[chat_id]=coin

@bot.message_handler(func=lambda m: user_state.get(m.chat.id)=="select_interval")
def select_interval(msg):
    chat_id = msg.chat.id
    interval = msg.text.lower()
    coin = selected_coin.get(chat_id)
    if interval=="🔙 back": my_coins_menu(msg); return
    if interval not in ["1m","5m","15m","1h","4h","1d"]: bot.send_message(chat_id,"❌ Invalid interval."); return
    sig = ultra_signal(coin, interval)
    bot.send_message(chat_id,sig)
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for tf in ["1m","5m","15m","1h","4h","1d"]: markup.add(tf)
    markup.add("🔙 Back")
    bot.send_message(chat_id,f"Select another interval for {coin} or go back:", reply_markup=markup)

# ===== FLASK WEBHOOK =====
@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    update = request.get_json(force=True)
    bot.process_new_updates([telebot.types.Update.de_json(update)])
    return "ok",200

@app.route("/", methods=["GET"])
def home():
    return "Bot is alive ✅",200

# ===== SET WEBHOOK =====
def setup_webhook():
    requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook")
    requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook?url={PUBLIC_URL}{WEBHOOK_PATH}")

if __name__=="__main__":
    setup_webhook()
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",5000)))




