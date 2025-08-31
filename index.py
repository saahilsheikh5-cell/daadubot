# index.py
import os
import sys
import json
import threading
import time
import re
from queue import Queue, Empty
from flask import Flask
import telebot
from telebot import types
from binance.client import Client
import pandas as pd
import numpy as np
import ta

# ===== CONFIG / ENV CHECK =====
REQUIRED = ["TELEGRAM_TOKEN", "BINANCE_API_KEY", "BINANCE_API_SECRET", "PORT"]
missing = [v for v in REQUIRED if not os.getenv(v)]
if missing:
    print("❌ Missing env vars:", missing)
    sys.exit(1)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
PORT = int(os.getenv("PORT", 5000))

# Admin chat id (detailed errors go here). Prefer ADMIN_CHAT_ID, fallback to TELEGRAM_CHAT_ID, fallback to provided default
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID") or 1263295916)

# Thresholds / tuning
TOP_N = 100                 # top N coins for "All Coins" and auto scanning
AUTO_SCAN_INTERVAL = 60     # seconds between auto scans of top100 (24x7) — 60s = 1 minute
TOP_MOVERS_CHECK_INTERVAL = 20  # seconds for checking rapid moves
TOP_MOVER_PERCENT = 0.02    # 2% move (0.02) threshold for top mover alert
MOVERS_ALERT_COOLDOWN = 300 # seconds per symbol cooldown so we don't spam

# Timeframes allowed (for manual selection)
ALLOWED_TFS = ["1m", "5m", "15m", "1h", "1d"]

# ===== INIT BOT & BINANCE CLIENT =====
bot = telebot.TeleBot(TELEGRAM_TOKEN)
client = Client(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)

# remove any webhook to allow polling
try:
    bot.remove_webhook()
except Exception as e:
    print("Warning: remove_webhook:", e)

# ===== FLASK APP (healthcheck / port binding) =====
app = Flask("UltraSignalsBot")

@app.route("/")
def home():
    return "Ultra Signals Bot is running."

def run_flask():
    app.run(host="0.0.0.0", port=PORT)

threading.Thread(target=run_flask, daemon=True).start()

# ===== STORAGE =====
COINS_FILE = "my_coins.json"
def load_coins():
    if not os.path.exists(COINS_FILE):
        return []
    with open(COINS_FILE, "r") as f:
        try:
            return json.load(f)
        except:
            return []

def save_coins(coins):
    with open(COINS_FILE, "w") as f:
        json.dump(coins, f)

# ===== UTILITIES =====
SYMBOL_RE = re.compile(r"^[A-Z0-9\-_\.]{1,20}$")
def clean_symbol(s: str) -> str:
    return s.strip().upper().replace(" ", "")

def is_valid_symbol(s: str) -> bool:
    if not s: return False
    return SYMBOL_RE.match(s) is not None

def send_admin(msg: str):
    try:
        bot.send_message(ADMIN_CHAT_ID, f"✅ Admin log:\n{msg}")
    except Exception:
        print("Failed to send admin msg:", msg)

def safe_binance_klines(symbol, interval, limit=200):
    """Wrap Binance klines with try/except — returns DataFrame or raises."""
    try:
        raw = client.get_klines(symbol=symbol, interval=interval, limit=limit)
        df = pd.DataFrame(raw, columns=["time","o","h","l","c","v","ct","qav","ntr","tbbav","tbqav","ignore"])
        # convert types
        df["c"] = df["c"].astype(float)
        df["h"] = df["h"].astype(float)
        df["l"] = df["l"].astype(float)
        df["o"] = df["o"].astype(float)
        df["v"] = df["v"].astype(float)
        return df
    except Exception as e:
        # raise up so caller can handle; but include symbol for admin info
        raise RuntimeError(f"Binance klines error for {symbol} {interval}: {repr(e)}")

def calculate_atr(df, period=14):
    high = df["h"]; low = df["l"]; close = df["c"]
    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(period).mean()

# ===== SIGNAL LOGIC (ULTRA/STRONG) =====
def compute_indicators(df):
    """Add indicators needed and return df with indicators columns."""
    df = df.copy()
    df["rsi"] = ta.momentum.RSIIndicator(df["c"], window=14).rsi()
    macd = ta.trend.MACD(df["c"])
    df["macd"] = macd.macd()
    df["macd_signal"] = macd.macd_signal()
    df["ema9"] = df["c"].ewm(span=9, adjust=False).mean()
    df["ema21"] = df["c"].ewm(span=21, adjust=False).mean()
    df["ma50"] = df["c"].rolling(50).mean()
    df["adx"] = ta.trend.ADXIndicator(df["h"], df["l"], df["c"], window=14).adx()
    df["bb_high"] = ta.volatility.BollingerBands(df["c"], window=20, window_dev=2).bollinger_hband()
    df["bb_low"] = ta.volatility.BollingerBands(df["c"], window=20, window_dev=2).bollinger_lband()
    df["vol_mean20"] = df["v"].rolling(20).mean()
    return df

def decide_signal_from_df(df):
    """
    Given df with indicators, decide Ultra/Strong BUY/SELL or None.
    Ultra requires stronger conditions; Strong slightly relaxed.
    """
    last = df.iloc[-1]
    decision = None
    notes = []

    # Ultra BUY
    if (last["rsi"] < 30
        and last["macd"] > last["macd_signal"]
        and last["c"] > last["ma50"]
        and last["adx"] > 25
        and last["v"] > 1.3 * last["vol_mean20"]):
        decision = "✅ Ultra BUY"
        notes.append("Ultra: RSI oversold + MACD bullish + Above MA50 + ADX>25 + volume spike")
    # Strong BUY
    elif (last["rsi"] < 40 and last["macd"] > last["macd_signal"] and last["c"] > last["ema9"]):
        decision = "✅ Strong BUY"
        notes.append("Strong: RSI low + MACD bullish + Above EMA9")
    # Ultra SELL
    elif (last["rsi"] > 70
        and last["macd"] < last["macd_signal"]
        and last["c"] < last["ma50"]
        and last["adx"] > 25
        and last["v"] > 1.3 * last["vol_mean20"]):
        decision = "❌ Ultra SELL"
        notes.append("Ultra: RSI overbought + MACD bearish + Below MA50 + ADX>25 + volume spike")
    # Strong SELL
    elif (last["rsi"] > 60 and last["macd"] < last["macd_signal"] and last["c"] < last["ema9"]):
        decision = "❌ Strong SELL"
        notes.append("Strong: RSI high + MACD bearish + Below EMA9")
    return decision, notes

def format_signal_text(symbol, interval, df, decision, notes):
    last = df.iloc[-1]
    entry = float(last["c"])
    # ATR for TP/SL if available (fallback to percent targets)
    atr_series = calculate_atr(df)
    atr = float(atr_series.iloc[-1]) if not atr_series.isna().all() else None

    if atr and atr > 0:
        if "BUY" in decision:
            tp1 = entry + 0.5 * atr
            tp2 = entry + 1.0 * atr
            sl  = entry - 0.5 * atr
        else:
            tp1 = entry - 0.5 * atr
            tp2 = entry - 1.0 * atr
            sl  = entry + 0.5 * atr
    else:
        # fallback percentages
        if "BUY" in decision:
            tp1 = entry * 1.01; tp2 = entry * 1.02; sl = entry * 0.99
        else:
            tp1 = entry * 0.99; tp2 = entry * 0.98; sl = entry * 1.01

    summary = ("Market shows bullish momentum: price above MA50/EMA9; look for continuation."
               if "BUY" in decision else
               "Market shows bearish momentum: price below MA50/EMA9; look for continuation.")

    text = (
        f"📊 Signal for {symbol} [{interval}]\n"
        f"Decision: {decision}\n"
        f"RSI: {round(last['rsi'],2)}\n"
        f"MACD: {round(last['macd'],4)} / Signal: {round(last['macd_signal'],4)}\n"
        f"Price: {round(entry,6)}\n\n"
        f"Entry: {round(entry,6)}\n"
        f"TP1: {round(tp1,6)}\n"
        f"TP2: {round(tp2,6)}\n"
        f"SL: {round(sl,6)}\n"
        f"Suggested Leverage: x10\n"
        f"Notes: {' | '.join(notes)}\n\n"
        f"💡 Summary: {summary}"
    )
    return text

def ultra_signal(symbol, interval="5m", lookback=200):
    """Main wrapper: returns text or None. Exceptions are propagated as RuntimeError."""
    symbol = clean_symbol(symbol)
    if not is_valid_symbol(symbol):
        return None

    try:
        df = safe_binance_klines(symbol, interval, limit=lookback)
    except Exception as e:
        raise

    try:
        df = compute_indicators(df)
        decision, notes = decide_signal_from_df(df)
        if not decision:
            return None
        return format_signal_text(symbol, interval, df, decision, notes)
    except Exception as e:
        raise RuntimeError(f"Signal generation failed for {symbol} {interval}: {repr(e)}")

# ===== TOP 100 BY VOLUME (helper + cache) =====
_top100_cache = {"list": [], "ts": 0}
CACHE_TTL = 30  # seconds

def get_top_100_by_volume():
    now = time.time()
    if _top100_cache["list"] and now - _top100_cache["ts"] < CACHE_TTL:
        return _top100_cache["list"]
    try:
        tickers = client.get_ticker_24hr()
        usdt_tickers = [t for t in tickers if t["symbol"].endswith("USDT")]
        # prefer quoteVolume (trading volume in quote asset)
        sorted_by_vol = sorted(usdt_tickers, key=lambda x: float(x.get("quoteVolume", 0)), reverse=True)
        top100 = [t["symbol"] for t in sorted_by_vol[:TOP_N]]
        _top100_cache["list"] = top100
        _top100_cache["ts"] = now
        return top100
    except Exception as e:
        raise RuntimeError(f"Failed to fetch top tickers: {repr(e)}")

# ===== NON-BLOCKING HANDLERS =====
def run_async(fn, *args, **kwargs):
    t = threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True)
    t.start()
    return t

# ===== HANDLERS: menus & flows =====
def main_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("📈 Signals", "➕ Add Coin", "➖ Remove Coin")
    kb.add("⏹ Stop Auto Signals")
    return kb

def signals_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("💼 My Coins", "🌍 All Coins")
    kb.add("🔎 Particular Coin", "🚀 Top Movers")
    kb.add("⬅️ Back")
    return kb

def timeframe_menu():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("1m", "5m", "15m")
    kb.add("1h", "1d", "⬅️ Back")
    return kb

@bot.message_handler(commands=["start"])
def handle_start(msg):
    bot.send_message(msg.chat.id, "🤖 Welcome to Ultra Signals Bot!", reply_markup=main_menu())

@bot.message_handler(func=lambda m: m.text == "⬅️ Back")
def handle_back(msg):
    bot.send_message(msg.chat.id, "🔙 Main Menu", reply_markup=main_menu())

@bot.message_handler(func=lambda m: m.text == "📈 Signals")
def handle_signals_menu(msg):
    bot.send_message(msg.chat.id, "Choose a signal option:", reply_markup=signals_menu())

# --- My Coins (manual) ask timeframe ---
@bot.message_handler(func=lambda m: m.text == "💼 My Coins")
def handle_my_coins(msg):
    bot.send_message(msg.chat.id, "Choose timeframe for My Coins signals:", reply_markup=timeframe_menu())
    bot.register_next_step_handler(msg, handle_my_coins_timeframe)

def handle_my_coins_timeframe(msg):
    tf = msg.text
    if tf not in ALLOWED_TFS:
        bot.send_message(msg.chat.id, f"Invalid timeframe. Choose one of {ALLOWED_TFS}", reply_markup=signals_menu())
        return
    # process in background
    run_async(process_my_coins_manual, msg.chat.id, tf)

def process_my_coins_manual(chat_id, timeframe):
    coins = load_coins()
    if not coins:
        bot.send_message(chat_id, "❌ No coins in My Coins. Use ➕ Add Coin.")
        return
    bot.send_message(chat_id, f"🔎 Scanning {len(coins)} My Coins on {timeframe} ... (this runs in background)")
    for c in coins:
        try:
            txt = ultra_signal(c, timeframe)
            if txt:
                bot.send_message(chat_id, txt)
            else:
                # we skip neutral; optionally send a small summary
                pass
        except Exception as e:
            # user-friendly message + admin detailed error
            bot.send_message(chat_id, f"⚠️ Couldn't fetch {c} ({timeframe}) right now. Skipping.")
            send_admin(f"Error while processing MyCoins {c} {timeframe}: {repr(e)}")

# --- All Coins (manual) ask timeframe; uses top100 by volume ---
@bot.message_handler(func=lambda m: m.text == "🌍 All Coins")
def handle_all_coins(msg):
    bot.send_message(msg.chat.id, "Choose timeframe for All Coins (top 100 by volume):", reply_markup=timeframe_menu())
    bot.register_next_step_handler(msg, handle_all_coins_timeframe)

def handle_all_coins_timeframe(msg):
    tf = msg.text
    if tf not in ALLOWED_TFS:
        bot.send_message(msg.chat.id, f"Invalid timeframe. Choose one of {ALLOWED_TFS}", reply_markup=signals_menu())
        return
    run_async(process_all_coins_manual, msg.chat.id, tf)

def process_all_coins_manual(chat_id, timeframe):
    bot.send_message(chat_id, f"🔎 Scanning top {TOP_N} coins by volume on {timeframe} ... (background)")
    try:
        top100 = get_top_100_by_volume()
    except Exception as e:
        bot.send_message(chat_id, "⚠️ Couldn't fetch top coins right now. Try again later.")
        send_admin(f"Error fetching top100: {repr(e)}")
        return
    count = 0
    for s in top100:
        try:
            txt = ultra_signal(s, timeframe)
            if txt:
                bot.send_message(chat_id, txt)
                count += 1
        except Exception as e:
            send_admin(f"Error processing all_coins {s} {timeframe}: {repr(e)}")
    bot.send_message(chat_id, f"✅ Done. Sent {count} strong/ultra signals from top{TOP_N} on {timeframe}.")

# --- Particular coin manual ---
@bot.message_handler(func=lambda m: m.text == "🔎 Particular Coin")
def handle_particular(msg):
    bot.send_message(msg.chat.id, "Enter coin symbol (e.g., BTCUSDT):")
    bot.register_next_step_handler(msg, handle_particular_symbol)

def handle_particular_symbol(msg):
    s = clean_symbol(msg.text)
    if not is_valid_symbol(s):
        bot.send_message(msg.chat.id, "⚠️ Invalid symbol format.")
        return
    bot.send_message(msg.chat.id, "Choose timeframe:", reply_markup=timeframe_menu())
    # store symbol on message for next handler using closure via register_next_step_handler trick:
    bot.register_next_step_handler(msg, handle_particular_timeframe, s)

def handle_particular_timeframe(msg, symbol):
    tf = msg.text
    if tf not in ALLOWED_TFS:
        bot.send_message(msg.chat.id, f"Invalid timeframe. Choose one of {ALLOWED_TFS}")
        return
    run_async(process_particular, msg.chat.id, symbol, tf)

def process_particular(chat_id, symbol, timeframe):
    try:
        txt = ultra_signal(symbol, timeframe)
        if txt:
            bot.send_message(chat_id, txt)
        else:
            bot.send_message(chat_id, f"⚠️ No strong/ultra signals for {symbol} on {timeframe}.")
    except Exception as e:
        bot.send_message(chat_id, f"⚠️ Could not fetch {symbol} now.")
        send_admin(f"Error processing particular {symbol} {timeframe}: {repr(e)}")

# --- Top Movers manual (quick) ---
@bot.message_handler(func=lambda m: m.text == "🚀 Top Movers")
def handle_top_movers(msg):
    # run in background because it can be slow
    run_async(process_top_movers_manual, msg.chat.id)

def process_top_movers_manual(chat_id):
    try:
        tickers = client.get_ticker_24hr()
        # sort by absolute percent change desc
        movers_sorted = sorted(tickers, key=lambda t: abs(float(t.get("priceChangePercent", 0))), reverse=True)
        top = movers_sorted[:20]
        text = "🚀 Top Movers (all Binance symbols):\n"
        for t in top:
            text += f"{t['symbol']}: {round(float(t.get('priceChangePercent',0)),2)}% change\n"
        bot.send_message(chat_id, text)
    except Exception as e:
        bot.send_message(chat_id, "⚠️ Couldn't fetch top movers right now. Try again later.")
        send_admin(f"TopMovers fetch error: {repr(e)}")

# --- Add/Remove coin ---
@bot.message_handler(func=lambda m: m.text == "➕ Add Coin")
def handle_add_coin(msg):
    bot.send_message(msg.chat.id, "Enter coin symbol to add (e.g., BTCUSDT):")
    bot.register_next_step_handler(msg, add_coin_step)

def add_coin_step(msg):
    s = clean_symbol(msg.text)
    if not is_valid_symbol(s):
        bot.send_message(msg.chat.id, "⚠️ Invalid symbol format.")
        return
    coins = load_coins()
    if s in coins:
        bot.send_message(msg.chat.id, f"⚠️ {s} already in My Coins.")
        return
    coins.append(s)
    save_coins(coins)
    bot.send_message(msg.chat.id, f"✅ {s} added to My Coins.")

@bot.message_handler(func=lambda m: m.text == "➖ Remove Coin")
def handle_remove_coin(msg):
    coins = load_coins()
    if not coins:
        bot.send_message(msg.chat.id, "❌ No coins to remove.")
        return
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for c in coins:
        kb.add(c)
    kb.add("⬅️ Back")
    bot.send_message(msg.chat.id, "Select a coin to remove:", reply_markup=kb)
    bot.register_next_step_handler(msg, remove_coin_step)

def remove_coin_step(msg):
    s = clean_symbol(msg.text)
    coins = load_coins()
    if s in coins:
        coins.remove(s)
        save_coins(coins)
        bot.send_message(msg.chat.id, f"✅ {s} removed.")
    else:
        bot.send_message(msg.chat.id, f"⚠️ {s} not found.")

# --- Stop Auto Signals (for admin/user) ---
auto_scan_running = False
def stop_auto_scan(msg):
    global auto_scan_running
    auto_scan_running = False
    bot.send_message(msg.chat.id, "⏹ Auto scanning stopped (top100).", reply_markup=main_menu())

@bot.message_handler(func=lambda m: m.text == "⏹ Stop Auto Signals")
def handle_stop_auto(msg):
    stop_auto_scan(msg)

# ===== AUTO SCAN (24x7 for top100) =====
_last_mover_alert = {}  # symbol -> last alert timestamp to cooldown movers

def auto_scan_loop():
    """Continuously scan top100 and send ultra/strong signals when they occur."""
    global auto_scan_running
    auto_scan_running = True
    send_admin("Auto-scan started for top100 (every {}s).".format(AUTO_SCAN_INTERVAL))
    while auto_scan_running:
        try:
            top100 = get_top_100_by_volume()
        except Exception as e:
            send_admin(f"Auto-scan: failed to fetch top100: {repr(e)}")
            time.sleep(60)
            continue

        for sym in top100:
            try:
                # check 1m and 5m (scalping + short-term)
                for tf in ("1m","5m"):
                    try:
                        txt = ultra_signal(sym, tf)
                    except Exception as e:
                        send_admin(f"Auto-scan signal error {sym} {tf}: {repr(e)}")
                        txt = None
                    if txt:
                        try:
                            bot.send_message(ADMIN_CHAT_ID, txt)  # send to admin (you). If you want to broadcast to channel/users, change.
                        except Exception:
                            pass
                        # small sleep to respect rate limits
                        time.sleep(0.2)
            except Exception as e:
                send_admin(f"Auto-scan per-symbol fatal {sym}: {repr(e)}")
        time.sleep(AUTO_SCAN_INTERVAL)

# start auto scan thread on startup
run_async(auto_scan_loop)

# ===== REAL-TIME TOP MOVERS MONITOR (continuous) =====
def top_movers_monitor():
    """Continuously watch last prices and alert if rapid moves occur AND ultra/strong criteria satisfied."""
    send_admin("Top Movers monitor started.")
    last_prices = {}
    while True:
        try:
            tickers = client.get_all_tickers()  # returns symbol/price
        except Exception as e:
            send_admin(f"Top-movers monitor fetch tickers failed: {repr(e)}")
            time.sleep(10)
            continue
        now = time.time()
        for t in tickers:
            sym = t["symbol"]
            if not sym.endswith("USDT"):
                continue
            try:
                price = float(t["price"])
            except:
                continue
            prev = last_prices.get(sym)
            last_prices[sym] = price
            if prev:
                pct = (price - prev) / prev
                if abs(pct) >= TOP_MOVER_PERCENT:
                    # cooldown per symbol
                    last_alert = _last_mover_alert.get(sym, 0)
                    if now - last_alert < MOVERS_ALERT_COOLDOWN:
                        continue
                    # Check if ultra/strong criteria satisfied (quick 5m check)
                    try:
                        txt = ultra_signal(sym, "5m")
                    except Exception as e:
                        send_admin(f"TopMovers signal check error {sym}: {repr(e)}")
                        txt = None
                    if txt:
                        try:
                            bot.send_message(ADMIN_CHAT_ID, f"🚨 Top Mover {sym}: {round(pct*100,2)}% in last check\n{txt}")
                        except Exception:
                            pass
                        _last_mover_alert[sym] = now
        time.sleep(TOP_MOVERS_CHECK_INTERVAL)

# start movers monitor thread
run_async(top_movers_monitor)

# ===== ERROR HANDLING note =====
# All heavy operations run in background threads; user-facing handlers report friendly messages while admin receives full errors.

# ===== START POLLING =====
print("✅ Bot ready. Polling...")
bot.infinity_polling()









