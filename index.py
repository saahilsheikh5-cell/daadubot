# index.py
import os
import json
import time
import threading
import logging
import requests
import pandas as pd
import numpy as np
from flask import Flask, request
import telebot
from telebot import types

# ===== LOGGING =====
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ===== CONFIG =====
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable required")

PUBLIC_URL = os.getenv("PUBLIC_URL", "https://daadubot.onrender.com")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = PUBLIC_URL + WEBHOOK_PATH

# optional: single chat id to push global signals (string or not used)
GLOBAL_CHAT_ID = os.getenv("CHAT_ID")  # keep None if you don't want global alerts

# Binance futures endpoints (no python-binance dependency; we use requests)
FAPI_KLINES = "https://fapi.binance.com/fapi/v1/klines"
FAPI_24HR = "https://fapi.binance.com/fapi/v1/ticker/24hr"

# ===== FLASK & TELEBOT =====
app = Flask(__name__)
bot = telebot.TeleBot(BOT_TOKEN, threaded=False)

# ===== STORAGE FILES =====
USER_COINS_FILE = "user_coins.json"
SETTINGS_FILE = "settings.json"
LAST_SIGNAL_FILE = "last_signals.json"
MUTED_COINS_FILE = "muted_coins.json"
# maps coin -> list of intervals user prefers (optional)
COIN_INTERVALS_FILE = "coin_intervals.json"

def load_json(path, default):
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r") as f:
            return json.load(f)
    except Exception as e:
        logger.error("Failed loading JSON %s: %s", path, e)
        return default

def save_json(path, data):
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.error("Failed saving JSON %s: %s", path, e)

# ===== Persistent state =====
coins = load_json(USER_COINS_FILE, [])                 # list of saved coins, e.g. ["BTCUSDT","ETHUSDT"]
settings = load_json(SETTINGS_FILE, {
    "rsi_buy": 25,
    "rsi_sell": 75,
    "signal_validity_min": 15,
    "default_leverage": 10,   # baseline suggested leverage
})
last_signals = load_json(LAST_SIGNAL_FILE, {})         # { "BTCUSDT_15m": timestamp }
muted_coins = load_json(MUTED_COINS_FILE, [])          # list of coin symbols muted
coin_intervals = load_json(COIN_INTERVALS_FILE, {})    # optional per-coin intervals

# ===== Simple TA functions (pandas + numpy) =====
def get_klines_futures(symbol, interval="15m", limit=200):
    """Return closes (list) and volumes (list). Uses Binance futures endpoint."""
    try:
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        r = requests.get(FAPI_KLINES, params=params, timeout=10)
        r.raise_for_status()
        raw = r.json()
        # each kline: [openTime, open, high, low, close, volume, ...]
        closes = [float(c[4]) for c in raw]
        volumes = [float(c[5]) for c in raw]
        highs = [float(c[2]) for c in raw]
        lows = [float(c[3]) for c in raw]
        return closes, volumes, highs, lows
    except Exception as e:
        logger.error("get_klines_futures error for %s %s: %s", symbol, interval, e)
        return [], [], [], []

def rsi_from_list(closes, period=14):
    if len(closes) < period+1:
        return None
    series = pd.Series(closes)
    delta = series.diff().dropna()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1])

def ema_from_list(closes, period=14):
    if len(closes) < period:
        return None
    return float(pd.Series(closes).ewm(span=period, adjust=False).mean().iloc[-1])

def macd_from_list(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow + signal:
        return None, None, None
    series = pd.Series(closes)
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    return float(macd_line.iloc[-1]), float(signal_line.iloc[-1]), float(hist.iloc[-1])

def atr_from_lists(highs, lows, closes, period=14):
    if len(closes) < period+1:
        return None
    high = pd.Series(highs)
    low = pd.Series(lows)
    close = pd.Series(closes)
    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(window=period).mean().iloc[-1]
    return float(atr)

# ===== Signal generation for futures (entry/sl/sl/TPs/leverage) =====
def generate_futures_signal(symbol, interval):
    closes, volumes, highs, lows = get_klines_futures(symbol, interval, limit=200)
    if not closes or len(closes) < 30:
        return None

    last_price = float(closes[-1])
    rsi_val = rsi_from_list(closes, period=14)
    macd_val, macd_signal, macd_hist = macd_from_list(closes)
    ema20 = ema_from_list(closes, 20)
    ema50 = ema_from_list(closes, 50)
    atr = atr_from_lists(highs, lows, closes, period=14) or 0.0

    # Volatility relative to price
    vol_ratio = (atr / last_price) if last_price>0 else 0.0
    # Suggest leverage: the smaller the volatility, the higher the leverage allowed.
    # Basic heuristic: target_risk_per_trade = 0.02 (2%) => leverage ≈ 0.02 / vol_ratio, clamp [1,125]
    if vol_ratio <= 0:
        suggested_leverage = settings.get("default_leverage", 10)
    else:
        raw = 0.02 / vol_ratio
        suggested_leverage = int(max(1, min(125, round(raw))))
        # cap to reasonable default if insane value
        if suggested_leverage > 1000:
            suggested_leverage = settings.get("default_leverage", 10)

    # Determine direction
    buy_conditions = []
    sell_conditions = []

    if rsi_val is not None:
        if rsi_val < settings.get("rsi_buy", 25):
            buy_conditions.append(f"RSI {rsi_val:.1f} < {settings.get('rsi_buy')}")
        if rsi_val > settings.get("rsi_sell", 75):
            sell_conditions.append(f"RSI {rsi_val:.1f} > {settings.get('rsi_sell')}")

    if macd_val is not None and macd_signal is not None:
        if macd_val > macd_signal:
            buy_conditions.append("MACD bullish")
        else:
            sell_conditions.append("MACD bearish")

    if ema20 is not None and ema50 is not None:
        if ema20 > ema50:
            buy_conditions.append("EMA20>EMA50")
        else:
            sell_conditions.append("EMA20<EMA50")

    direction = None
    strength = "Medium"
    if buy_conditions and not sell_conditions:
        direction = "BUY"
        if (rsi_val is not None and rsi_val < settings.get("rsi_buy") - 5) and macd_hist is not None and macd_hist > 0:
            strength = "ULTRA STRONG"
        elif len(buy_conditions) >= 2:
            strength = "STRONG"
    elif sell_conditions and not buy_conditions:
        direction = "SELL"
        if (rsi_val is not None and rsi_val > settings.get("rsi_sell") + 5) and macd_hist is not None and macd_hist < 0:
            strength = "ULTRA STRONG"
        elif len(sell_conditions) >= 2:
            strength = "STRONG"
    else:
        # conflicted or neutral
        direction = None

    # Build entry/sl/tps using ATR
    sl = None
    tp1 = None
    tp2 = None
    entry = last_price
    if atr and atr > 0:
        # Conservative SL at 1*ATR, TP1 1.5*ATR, TP2 3*ATR
        if direction == "BUY":
            sl = entry - atr
            tp1 = entry + atr * 1.5
            tp2 = entry + atr * 3
        elif direction == "SELL":
            sl = entry + atr
            tp1 = entry - atr * 1.5
            tp2 = entry - atr * 3
    else:
        # fallback: percentage-based SL/TP if ATR not available
        pct = 0.01  # 1%
        if direction == "BUY":
            sl = entry * (1 - pct)
            tp1 = entry * (1 + pct * 1.5)
            tp2 = entry * (1 + pct * 3)
        elif direction == "SELL":
            sl = entry * (1 + pct)
            tp1 = entry * (1 - pct * 1.5)
            tp2 = entry * (1 - pct * 3)

    # Confidence
    confidence = strength if direction else "Neutral"

    # summary text
    if not direction:
        return f"⚪ Neutral / No clear futures signal for {symbol} | {interval}\nPrice: {entry:.4f}\nRSI: {rsi_val if rsi_val is not None else 'N/A'}"

    sig_text = (
        f"{'🟢' if direction=='BUY' else '🔴'} {confidence} {direction} | {symbol} | {interval}\n"
        f"Price (Entry): {entry:.4f}\n"
        f"SL: {sl:.4f}\n"
        f"TP1: {tp1:.4f}\n"
        f"TP2: {tp2:.4f}\n"
        f"Suggested Leverage: {suggested_leverage}x\n"
        f"Indicators: RSI={rsi_val:.1f if rsi_val is not None else 'N/A'}, MACD_hist={macd_hist if macd_hist is not None else 'N/A'}\n"
    )
    return sig_text

# ===== Signal throttle / de-duplication =====
def send_signal_if_new(chat_id, coin, interval, sig):
    global last_signals, muted_coins
    if coin in muted_coins:
        logger.info("Coin %s is muted, skipping signal", coin)
        return
    key = f"{chat_id}_{coin}_{interval}"
    now_ts = time.time()
    validity = settings.get("signal_validity_min", 15) * 60
    if key not in last_signals or now_ts - last_signals[key] > validity:
        try:
            bot.send_message(chat_id, f"⚡ {sig}")
            last_signals[key] = now_ts
            save_json(LAST_SIGNAL_FILE, last_signals)
        except Exception as e:
            logger.error("Failed to send signal to %s: %s", chat_id, e)

# ===== Background scanner (per-user) =====
scanner_running = True

def scanner_thread_function():
    logger.info("Scanner thread started")
    while scanner_running:
        try:
            # iterate over users — we will track known chat_ids that used /start recently
            # For simplicity, read saved chat ids from last_signals keys or persist a user list
            # We'll keep a local in-memory subscribers list
            # We'll use a file to persist subscribers
            subscribers = load_json("subscribers.json", [])
            for chat_id in list(subscribers):
                # ensure chat_id int
                try:
                    chat_i = int(chat_id)
                except:
                    continue
                # if user has coins
                user_coins = load_json(USER_COINS_FILE, [])
                # For now, assume global coins list are the watched ones per user (improvement: per-user lists)
                for coin in user_coins:
                    intervals = coin_intervals.get(coin, ["1m", "5m", "15m"])  # check quick intervals
                    for interval in intervals:
                        try:
                            sig = generate_futures_signal(coin, interval)
                            if sig and not sig.startswith("⚪ Neutral"):
                                send_signal_if_new(chat_i, coin, interval, sig)
                        except Exception as e:
                            logger.debug("scanner generate error %s %s: %s", coin, interval, e)
            # Sleep short time
            time.sleep(60)
        except Exception as e:
            logger.error("Scanner loop error: %s", e)
            time.sleep(5)

# Start scanner thread (daemon)
threading.Thread(target=scanner_thread_function, daemon=True).start()

# ===== User state & subscriptions =====
user_state = {}   # chat_id -> state string (e.g., "adding_coin", "removing_coin", "select_interval_for_coin")
selected_coin = {}  # chat_id -> symbol

def add_subscriber(chat_id):
    subs = load_json("subscribers.json", [])
    if str(chat_id) not in subs:
        subs.append(str(chat_id))
        save_json("subscribers.json", subs)

def remove_subscriber(chat_id):
    subs = load_json("subscribers.json", [])
    if str(chat_id) in subs:
        subs.remove(str(chat_id))
        save_json("subscribers.json", subs)

# ===== Menu helpers =====
def main_menu(chat_id):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("➕ Add Coin", "📊 My Coins")
    markup.row("➖ Remove Coin", "📈 Top Movers")
    markup.row("📡 Signals", "🛑 Stop Signals")
    markup.row("🔄 Reset Settings", "⚙️ Signal Settings", "🔍 Preview Signal")
    try:
        bot.send_message(chat_id, "🤖 Main Menu:", reply_markup=markup)
    except Exception as e:
        logger.error("main_menu send error: %s", e)

# ===== Bot handlers =====
@bot.message_handler(commands=["start", "help"])
def handle_start(message):
    chat_id = message.chat.id
    add_subscriber(chat_id)
    bot.send_message(chat_id, "✅ Bot is live and configured for BINANCE FUTURES signals.")
    main_menu(chat_id)

# Add Coin
@bot.message_handler(func=lambda m: m.text == "➕ Add Coin")
def handle_add_coin_request(message):
    chat_id = message.chat.id
    bot.send_message(chat_id, "Type coin symbol to add (Futures pair, e.g., BTCUSDT).")
    user_state[str(chat_id)] = "adding_coin"

@bot.message_handler(func=lambda m: user_state.get(str(m.chat.id)) == "adding_coin")
def handle_add_coin(message):
    chat_id = message.chat.id
    symbol = message.text.strip().upper()
    # Basic validation: symbol should end with USDT or BUSD etc. Verify via 24hr endpoint
    try:
        r = requests.get(FAPI_24HR, params={"symbol": symbol}, timeout=8)
        if r.status_code != 200:
            bot.send_message(chat_id, f"❌ Symbol {symbol} not found on futures API.")
        else:
            if symbol not in coins:
                coins.append(symbol)
                save_json(USER_COINS_FILE, coins)
                bot.send_message(chat_id, f"✅ {symbol} added to watchlist.")
            else:
                bot.send_message(chat_id, f"⚠️ {symbol} already in watchlist.")
    except Exception as e:
        logger.error("add coin check error: %s", e)
        bot.send_message(chat_id, "⚠️ Error checking symbol. Try again.")
    user_state.pop(str(chat_id), None)
    main_menu(chat_id)

# Remove Coin
@bot.message_handler(func=lambda m: m.text == "➖ Remove Coin")
def handle_remove_coin_request(message):
    chat_id = message.chat.id
    if not coins:
        bot.send_message(chat_id, "⚠️ No coins to remove.")
        main_menu(chat_id)
        return
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for c in coins:
        markup.add(c)
    markup.add("🔙 Back")
    bot.send_message(chat_id, "Select coin to remove:", reply_markup=markup)
    user_state[str(chat_id)] = "removing_coin"

@bot.message_handler(func=lambda m: user_state.get(str(m.chat.id)) == "removing_coin")
def handle_remove_coin(message):
    chat_id = message.chat.id
    sym = message.text.strip().upper()
    if sym == "🔙" or sym == "🔙 BACK":
        user_state.pop(str(chat_id), None)
        main_menu(chat_id)
        return
    if sym in coins:
        coins.remove(sym)
        save_json(USER_COINS_FILE, coins)
        bot.send_message(chat_id, f"✅ {sym} removed.")
    else:
        bot.send_message(chat_id, "❌ That coin is not in the watchlist.")
    user_state.pop(str(chat_id), None)
    main_menu(chat_id)

# My Coins -> show coins and allow selecting timeframe
@bot.message_handler(func=lambda m: m.text == "📊 My Coins")
def handle_my_coins(message):
    chat_id = message.chat.id
    if not coins:
        bot.send_message(chat_id, "⚠️ No coins in your watchlist.")
        main_menu(chat_id)
        return
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for c in coins:
        markup.add(c)
    markup.add("🔙 Back")
    bot.send_message(chat_id, "Select a coin to view analysis:", reply_markup=markup)
    user_state[str(chat_id)] = "select_coin_to_view"

@bot.message_handler(func=lambda m: user_state.get(str(m.chat.id)) == "select_coin_to_view")
def handle_select_coin_to_view(message):
    chat_id = message.chat.id
    sym = message.text.strip().upper()
    if sym in ["🔙", "🔙 BACK"]:
        user_state.pop(str(chat_id), None)
        main_menu(chat_id)
        return
    if sym not in coins:
        bot.send_message(chat_id, "❌ Coin not in your watchlist.")
        return
    selected_coin[str(chat_id)] = sym
    # timeframe keyboard
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for tf in ["1m", "5m", "15m", "1h", "4h", "1d"]:
        markup.add(tf)
    markup.add("🔙 Back")
    bot.send_message(chat_id, f"Select timeframe for {sym}:", reply_markup=markup)
    user_state[str(chat_id)] = "select_interval_for_coin"

@bot.message_handler(func=lambda m: user_state.get(str(m.chat.id)) == "select_interval_for_coin")
def handle_select_interval_for_coin(message):
    chat_id = message.chat.id
    interval = message.text.strip()
    if interval in ["🔙", "🔙 BACK"]:
        user_state.pop(str(chat_id), None)
        main_menu(chat_id)
        return
    sym = selected_coin.get(str(chat_id))
    if not sym:
        bot.send_message(chat_id, "❌ No coin selected.")
        main_menu(chat_id)
        return
    try:
        sig = generate_futures_signal(sym, interval)
        bot.send_message(chat_id, sig)
    except Exception as e:
        logger.error("Error generating signal for %s %s: %s", sym, interval, e)
        bot.send_message(chat_id, "⚠️ Error generating signal. See logs.")
    # keep in same menu
    # show timeframe keyboard again
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for tf in ["1m", "5m", "15m", "1h", "4h", "1d"]:
        markup.add(tf)
    markup.add("🔙 Back")
    bot.send_message(chat_id, f"Select another timeframe for {sym} or go back:", reply_markup=markup)

# Top Movers (futures 24h)
@bot.message_handler(func=lambda m: m.text == "📈 Top Movers")
def handle_top_movers(message):
    chat_id = message.chat.id
    try:
        r = requests.get(FAPI_24HR, timeout=10)
        r.raise_for_status()
        data = r.json()
        # sort by priceChangePercent
        data_sorted = sorted(data, key=lambda d: float(d.get("priceChangePercent", 0)), reverse=True)
        top_gainers = data_sorted[:5]
        top_losers = data_sorted[-5:]
        out = "📈 Top 5 Gainers (24h):\n"
        for t in top_gainers:
            out += f"{t['symbol']}: {t['priceChangePercent']}%\n"
        out += "\n📉 Top 5 Losers (24h):\n"
        for t in top_losers:
            out += f"{t['symbol']}: {t['priceChangePercent']}%\n"
        bot.send_message(chat_id, out)
    except Exception as e:
        logger.error("Top movers error: %s", e)
        bot.send_message(chat_id, "⚠️ Failed to fetch top movers.")

# Signals start/stop (per-chat)
@bot.message_handler(func=lambda m: m.text == "📡 Signals")
def handle_signals_start(message):
    chat_id = message.chat.id
    add_subscriber(chat_id)
    bot.send_message(chat_id, "📡 Signals enabled for your chat. We'll send futures signals for your watchlist.")

@bot.message_handler(func=lambda m: m.text == "🛑 Stop Signals")
def handle_signals_stop(message):
    chat_id = message.chat.id
    remove_subscriber(chat_id)
    bot.send_message(chat_id, "🛑 Signals disabled for your chat.")

# Reset Settings
@bot.message_handler(func=lambda m: m.text == "🔄 Reset Settings")
def handle_reset_settings(message):
    chat_id = message.chat.id
    # reset local persisted files
    save_json(USER_COINS_FILE, [])
    save_json(SETTINGS_FILE, {
        "rsi_buy": 25,
        "rsi_sell": 75,
        "signal_validity_min": 15,
        "default_leverage": 10,
    })
    save_json(LAST_SIGNAL_FILE, {})
    save_json(MUTED_COINS_FILE, [])
    bot.send_message(chat_id, "♻️ Settings and lists reset to defaults.")
    main_menu(chat_id)

# Signal Settings (simple edit RSI thresholds)
@bot.message_handler(func=lambda m: m.text == "⚙️ Signal Settings")
def handle_signal_settings(message):
    chat_id = message.chat.id
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("Set RSI Buy", "Set RSI Sell")
    markup.add("Set Signal Validity (minutes)")
    markup.add("🔙 Back")
    bot.send_message(chat_id, "⚙️ Signal Settings menu:", reply_markup=markup)
    user_state[str(chat_id)] = "signal_settings_menu"

@bot.message_handler(func=lambda m: user_state.get(str(m.chat.id)) == "signal_settings_menu")
def handle_signal_settings_menu(message):
    chat_id = message.chat.id
    text = message.text.strip()
    if text == "Set RSI Buy":
        bot.send_message(chat_id, "Send RSI BUY threshold (e.g., 25):")
        user_state[str(chat_id)] = "set_rsi_buy"
    elif text == "Set RSI Sell":
        bot.send_message(chat_id, "Send RSI SELL threshold (e.g., 75):")
        user_state[str(chat_id)] = "set_rsi_sell"
    elif text == "Set Signal Validity (minutes)":
        bot.send_message(chat_id, "Send signal validity in minutes (e.g., 15):")
        user_state[str(chat_id)] = "set_validity"
    elif text in ["🔙", "🔙 Back"]:
        user_state.pop(str(chat_id), None)
        main_menu(chat_id)
    else:
        bot.send_message(chat_id, "⚠️ Unknown settings command.")

@bot.message_handler(func=lambda m: user_state.get(str(m.chat.id)) in ["set_rsi_buy","set_rsi_sell","set_validity"])
def handle_signal_settings_values(message):
    chat_id = message.chat.id
    state = user_state.get(str(chat_id))
    try:
        val = int(message.text.strip())
        if state == "set_rsi_buy":
            settings["rsi_buy"] = val
            save_json(SETTINGS_FILE, settings)
            bot.send_message(chat_id, f"✅ RSI buy set to {val}")
        elif state == "set_rsi_sell":
            settings["rsi_sell"] = val
            save_json(SETTINGS_FILE, settings)
            bot.send_message(chat_id, f"✅ RSI sell set to {val}")
        elif state == "set_validity":
            settings["signal_validity_min"] = val
            save_json(SETTINGS_FILE, settings)
            bot.send_message(chat_id, f"✅ Signal validity set to {val} minutes")
    except Exception as e:
        bot.send_message(chat_id, "⚠️ Invalid number.")
    user_state.pop(str(chat_id), None)
    main_menu(chat_id)

# Preview Signal
@bot.message_handler(func=lambda m: m.text == "🔍 Preview Signal" or m.text == "👀 Preview Signal")
def handle_preview_signal(message):
    chat_id = message.chat.id
    if not coins:
        bot.send_message(chat_id, "⚠️ No coins saved to preview.")
        return
    for sym in coins:
        try:
            txt = generate_futures_signal(sym, "15m")
            bot.send_message(chat_id, f"🔎 Preview {sym} (15m):\n{txt}")
        except Exception as e:
            logger.error("preview error %s: %s", sym, e)
            bot.send_message(chat_id, f"⚠️ Error for {sym}")

# Mute/unmute via Stop Signals submenu per coin (quick implementation: accept "MUTE <SYMBOL>" and "UNMUTE <SYMBOL>")
@bot.message_handler(func=lambda m: m.text and m.text.strip().upper().startswith("MUTE "))
def handle_mute(message):
    chat_id = message.chat.id
    sym = message.text.strip().upper().split()[1]
    if sym not in muted_coins:
        muted_coins.append(sym)
        save_json(MUTED_COINS_FILE, muted_coins)
        bot.send_message(chat_id, f"🔇 {sym} muted.")
    else:
        bot.send_message(chat_id, f"⚠️ {sym} already muted.")

@bot.message_handler(func=lambda m: m.text and m.text.strip().upper().startswith("UNMUTE "))
def handle_unmute(message):
    chat_id = message.chat.id
    sym = message.text.strip().upper().split()[1]
    if sym in muted_coins:
        muted_coins.remove(sym)
        save_json(MUTED_COINS_FILE, muted_coins)
        bot.send_message(chat_id, f"🔊 {sym} unmuted.")
    else:
        bot.send_message(chat_id, f"⚠️ {sym} not muted.")

# Catch all fallback (also allows entering a coin directly to get full multi-timeframe analysis)
@bot.message_handler(func=lambda m: True)
def fallback(message):
    chat_id = message.chat.id
    txt = message.text.strip().upper()
    # direct request: if user types a symbol that's in coins, show multi-timeframe analysis
    if txt in coins:
        out = f"📊 Multi-timeframe for {txt}:\n"
        for tf in ["1m","5m","15m","1h","4h","1d"]:
            try:
                s = generate_futures_signal(txt, tf)
                out += f"\n⏱ {tf}:\n{s}\n"
            except Exception as e:
                out += f"\n⏱ {tf}: error\n"
        bot.send_message(chat_id, out)
        return
    # allow direct add by entering symbol (quick add)
    if txt.endswith("USDT") and len(txt) >= 5:
        # try to add
        if txt not in coins:
            try:
                r = requests.get(FAPI_24HR, params={"symbol": txt}, timeout=8)
                if r.status_code == 200:
                    coins.append(txt)
                    save_json(USER_COINS_FILE, coins)
                    bot.send_message(chat_id, f"✅ {txt} added to watchlist.")
                else:
                    bot.send_message(chat_id, "⚠️ Symbol not found on futures.")
            except Exception as e:
                bot.send_message(chat_id, "⚠️ Error checking symbol.")
        else:
            bot.send_message(chat_id, "⚠️ Symbol already in watchlist.")
        main_menu(chat_id)
        return

    # fallback generic reply
    bot.send_message(chat_id, "⚠️ Unknown command. Use /start to open menu or type a futures symbol (e.g., BTCUSDT) to quick add/view.")

# ===== Webhook route (we use telebot process_new_updates so decorators fire) =====
@app.route(WEBHOOK_PATH, methods=["POST"])
def webhook():
    try:
        data = request.get_data().decode("utf-8")
        update = telebot.types.Update.de_json(data)
        bot.process_new_updates([update])
    except Exception as e:
        logger.error("Webhook processing error: %s", e)
    return "OK", 200

@app.route("/", methods=["GET"])
def home():
    return "Bot is alive (Futures edition) ✅", 200

# ===== set webhook on start (Render) =====
def setup_webhook():
    try:
        bot.remove_webhook()
    except Exception:
        pass
    try:
        bot.set_webhook(url=WEBHOOK_URL)
        logger.info("Webhook set to %s", WEBHOOK_URL)
    except Exception as e:
        logger.error("Failed to set webhook: %s", e)

if __name__ == "__main__":
    setup_webhook()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))




