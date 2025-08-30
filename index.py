# final index.py - persistent My Coins, robust signal generation, webhook-safe, Render-ready
import os
import time
import sqlite3
import threading
import requests
import pandas as pd
from flask import Flask
import telebot
from telebot import types
from ta.momentum import RSIIndicator
from ta.trend import MACD, EMAIndicator, SMAIndicator
from ta.volatility import AverageTrueRange

# -----------------------
# Config / Env
# -----------------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")  # optional, used by auto-send
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.environ.get("BINANCE_API_SECRET", "")

if not TELEGRAM_TOKEN:
    raise RuntimeError("Environment variable TELEGRAM_TOKEN is required and not set.")

# Signal configuration
TIMEFRAMES = ["1m", "5m", "15m", "1h", "1d"]
CONFIDENCE_THRESHOLD = 4   # number of indicator votes required
MAX_LEVERAGE = 20
MIN_ROWS_REQUIRED = 50     # ensure enough history for indicators
DB_FILE = "bot_settings.db"

# -----------------------
# Telebot + webhook cleanup
# -----------------------
bot = telebot.TeleBot(TELEGRAM_TOKEN)
# remove webhook so polling is allowed
try:
    bot.remove_webhook()
    print("Removed any existing Telegram webhook.")
except Exception as e:
    print("Warning removing webhook:", e)

# -----------------------
# Flask app for Render port binding
# -----------------------
app = Flask("keepalive")
@app.route("/")
def home():
    return "Bot is running ✅"

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    # use_reloader=False prevents double-start in some hosts
    app.run(host="0.0.0.0", port=port, use_reloader=False)

# -----------------------
# SQLite persistence
# -----------------------
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT NOT NULL,
        coin TEXT NOT NULL,
        timeframe TEXT NOT NULL,
        auto INTEGER DEFAULT 0,
        last_sent INTEGER DEFAULT 0
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        chat_id TEXT PRIMARY KEY,
        default_timeframe TEXT DEFAULT '15m',
        auto_global INTEGER DEFAULT 0
    );
    """)
    conn.commit()
    conn.close()

def add_subscription(chat_id, coin, timeframe, auto=0):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT INTO subscriptions (chat_id, coin, timeframe, auto) VALUES (?,?,?,?)",
                (str(chat_id), coin.upper(), timeframe, int(auto)))
    conn.commit()
    conn.close()

def remove_subscription(chat_id, coin, timeframe):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("DELETE FROM subscriptions WHERE chat_id=? AND coin=? AND timeframe=?",
                (str(chat_id), coin.upper(), timeframe))
    conn.commit()
    conn.close()

def list_subscriptions(chat_id):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT id, coin, timeframe, auto, last_sent FROM subscriptions WHERE chat_id=?", (str(chat_id),))
    rows = cur.fetchall()
    conn.close()
    return rows

def get_all_auto_subscriptions():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT id, chat_id, coin, timeframe, last_sent FROM subscriptions WHERE auto=1")
    rows = cur.fetchall()
    conn.close()
    return rows

def set_subscription_auto(sub_id, auto):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("UPDATE subscriptions SET auto=? WHERE id=?", (int(auto), int(sub_id)))
    conn.commit()
    conn.close()

def update_last_sent(sub_id, ts):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("UPDATE subscriptions SET last_sent=? WHERE id=?", (int(ts), int(sub_id)))
    conn.commit()
    conn.close()

def get_or_create_settings(chat_id):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT default_timeframe, auto_global FROM settings WHERE chat_id=?", (str(chat_id),))
    row = cur.fetchone()
    if row:
        conn.close()
        return {"default_timeframe": row[0], "auto_global": bool(row[1])}
    else:
        cur.execute("INSERT INTO settings (chat_id) VALUES (?)", (str(chat_id),))
        conn.commit()
        conn.close()
        return {"default_timeframe": "15m", "auto_global": False}

def set_default_timeframe(chat_id, tf):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO settings(chat_id, default_timeframe, auto_global) VALUES(?,?,COALESCE((SELECT auto_global FROM settings WHERE chat_id=?),0))",
                (str(chat_id), tf, str(chat_id)))
    conn.commit()
    conn.close()

# init DB
init_db()

# -----------------------
# Binance data (public)
# -----------------------
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"

def fetch_klines_public(symbol, interval, limit=200):
    symbol = symbol.upper()
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    try:
        r = requests.get(BINANCE_KLINES_URL, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        if not data:
            return None
        df = pd.DataFrame(data, columns=[
            "open_time","open","high","low","close","volume","close_time","quote_asset_volume",
            "num_trades","taker_buy_base","taker_buy_quote","ignore"
        ])
        # convert numeric cols
        for c in ["open","high","low","close","volume"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        # convert open_time to int
        df["open_time"] = df["open_time"].astype("int64")
        return df
    except Exception as e:
        print("fetch_klines_public error:", e)
        return None

# -----------------------
# Indicator calculations (safe)
# -----------------------
def compute_indicators(df):
    # df must have columns: open, high, low, close, volume
    df = df.copy()
    # safety
    if df is None or df.empty or len(df) < MIN_ROWS_REQUIRED:
        return None

    try:
        df["rsi"] = RSIIndicator(df["close"], window=14).rsi()
        macd_obj = MACD(df["close"], window_slow=26, window_fast=12, window_sign=9)
        df["macd"] = macd_obj.macd()
        df["macd_signal"] = macd_obj.macd_signal()
        df["ema9"] = EMAIndicator(df["close"], window=9).ema_indicator()
        df["ema21"] = EMAIndicator(df["close"], window=21).ema_indicator()
        df["sma50"] = SMAIndicator(df["close"], window=50).sma_indicator()
        df["sma200"] = SMAIndicator(df["close"], window=200).sma_indicator()
        atr_obj = AverageTrueRange(df["high"], df["low"], df["close"], window=14)
        df["atr"] = atr_obj.average_true_range()
        df["vol_ma20"] = df["volume"].rolling(20).mean()
        return df
    except Exception as e:
        print("compute_indicators error:", e)
        return None

# -----------------------
# Candle pattern helpers (simple)
# -----------------------
def detect_simple_candles(df):
    last = df.iloc[-1]
    prev = df.iloc[-2]
    body = abs(last["close"] - last["open"])
    rng = last["high"] - last["low"]
    if rng == 0:
        return None
    lower_shadow = min(last["open"], last["close"]) - last["low"]
    upper_shadow = last["high"] - max(last["open"], last["close"])
    if lower_shadow > 2 * body and upper_shadow < body:
        return "hammer"
    if upper_shadow > 2 * body and lower_shadow < body:
        return "shooting_star"
    if last["close"] > last["open"] and prev["close"] < prev["open"] and last["close"] > prev["open"]:
        return "bullish_engulfing"
    if last["close"] < last["open"] and prev["close"] > prev["open"] and last["close"] < prev["open"]:
        return "bearish_engulfing"
    return None

# -----------------------
# Suggest leverage & levels
# -----------------------
def suggest_leverage_from_score(score):
    # map score to leverage (example)
    if score >= 6:
        return min(MAX_LEVERAGE, 20)
    if score == 5:
        return 12
    if score == 4:
        return 8
    return 3

def build_trade_plan(df, signal_type):
    last = df.iloc[-1]
    price = float(last["close"])
    atr = float(last["atr"]) if not pd.isna(last["atr"]) else max( (df["close"].pct_change().std() or 0.001) * price, price*0.001 )
    # SL based on atr
    if signal_type == "BUY":
        sl = price - 1.5 * atr
        tp1 = price + (price - sl) * 1.2
        tp2 = price + (price - sl) * 2
    else:
        sl = price + 1.5 * atr
        tp1 = price - (sl - price) * 1.2
        tp2 = price - (sl - price) * 2
    # make sure positive
    return {"entry": round(price, 8), "sl": round(sl, 8), "tp1": round(tp1, 8), "tp2": round(tp2, 8), "atr": round(atr,8)}

# -----------------------
# Generate multi-indicator signal (returns dict or None)
# -----------------------
def generate_multi_signal(symbol, timeframe):
    df = fetch_klines_public(symbol, timeframe, limit=300)
    if df is None or df.empty or len(df) < MIN_ROWS_REQUIRED:
        return {"error": f"No sufficient data for {symbol} {timeframe}"}
    df = compute_indicators(df)
    if df is None:
        return {"error": f"Indicator calculation failed for {symbol} {timeframe}"}

    last = df.iloc[-1]
    buy_score = 0
    sell_score = 0
    reasons = []

    # RSI
    rsi = last["rsi"]
    if rsi < 30:
        buy_score += 1
        reasons.append("RSI oversold")
    elif rsi > 70:
        sell_score += 1
        reasons.append("RSI overbought")
    else:
        # neutral doesn't add points
        pass

    # MACD
    if last["macd"] > last["macd_signal"]:
        buy_score += 1
        reasons.append("MACD bullish")
    elif last["macd"] < last["macd_signal"]:
        sell_score += 1
        reasons.append("MACD bearish")

    # EMA crossover
    if last["ema9"] > last["ema21"]:
        buy_score += 1
        reasons.append("EMA9>EMA21")
    elif last["ema9"] < last["ema21"]:
        sell_score += 1
        reasons.append("EMA9<EMA21")

    # SMA trend (50 vs 200)
    if last["sma50"] > last["sma200"]:
        buy_score += 1
        reasons.append("SMA50>SMA200")
    elif last["sma50"] < last["sma200"]:
        sell_score += 1
        reasons.append("SMA50<SMA200")

    # Candle pattern
    pat = detect_simple_candles(df)
    if pat in ("hammer", "bullish_engulfing"):
        buy_score += 1
        reasons.append(pat)
    elif pat in ("shooting_star", "bearish_engulfing"):
        sell_score += 1
        reasons.append(pat)

    # Volume confirmation
    vol_ok = (last["volume"] > last.get("vol_ma20", 0))
    if vol_ok:
        # small tiebreaker vote to direction of price move
        if last["close"] > df["close"].iloc[-2]:
            buy_score += 1
            reasons.append("Volume increased (buy)")
        else:
            sell_score += 1
            reasons.append("Volume increased (sell)")

    # Final decision
    if buy_score >= CONFIDENCE_THRESHOLD and buy_score > sell_score:
        signal = "BUY"
        score = buy_score
    elif sell_score >= CONFIDENCE_THRESHOLD and sell_score > buy_score:
        signal = "SELL"
        score = sell_score
    else:
        signal = None
        score = max(buy_score, sell_score)

    # Build response dict
    trade_plan = build_trade_plan(df, signal) if signal else None
    suggested_leverage = suggest_leverage_from_score(score)
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "signal": signal,
        "score": score,
        "reasons": reasons,
        "rsi": round(float(rsi),2),
        "ema9": round(float(last["ema9"]),6),
        "ema21": round(float(last["ema21"]),6),
        "macd": round(float(last["macd"]),6),
        "macd_signal": round(float(last["macd_signal"]),6),
        "atr": round(float(last["atr"]),8),
        "vol_ok": bool(vol_ok),
        "trade_plan": trade_plan,
        "last_open_time": int(df["open_time"].iloc[-1])
    }

# -----------------------
# Messaging helpers: send modular messages
# -----------------------
def send_signal_messages(chat_id, sig_dict):
    if "error" in sig_dict:
        bot.send_message(chat_id, sig_dict["error"])
        return
    if not sig_dict["signal"]:
        bot.send_message(chat_id, f"No strong signal for {sig_dict['symbol']} on {sig_dict['timeframe']} (score {sig_dict['score']}).")
        return

    # 6 messages: RSI, EMA, MACD, ATR-SL, Volume, Final Trade Plan
    # 1) RSI
    bot.send_message(chat_id, f"📊 RSI: {sig_dict['rsi']} ({'oversold' if sig_dict['rsi']<30 else ('overbought' if sig_dict['rsi']>70 else 'neutral')})")

    # 2) EMA trend
    bot.send_message(chat_id, f"📈 EMA9: {sig_dict['ema9']} | EMA21: {sig_dict['ema21']} → {'bullish' if sig_dict['ema9']>sig_dict['ema21'] else 'bearish'}")

    # 3) MACD
    bot.send_message(chat_id, f"⚡ MACD: {sig_dict['macd']}  Signal: {sig_dict['macd_signal']} → {'bull' if sig_dict['macd']>sig_dict['macd_signal'] else 'bear'}")

    # 4) ATR-based stop loss suggestion
    bot.send_message(chat_id, f"🛡 ATR: {sig_dict['atr']} → suggested SL distance ~ {round(sig_dict['atr'],8)}")

    # 5) Volume confirmation
    bot.send_message(chat_id, f"🔊 Volume confirmed: {sig_dict['vol_ok']}")

    # 6) Final trade plan
    tp = sig_dict["trade_plan"]
    msg = (
        f"🚀 FINAL SIGNAL: {sig_dict['signal']} {sig_dict['symbol']} [{sig_dict['timeframe']}]\n"
        f"Score: {sig_dict['score']}\n"
        f"Entry: {tp['entry']}\nSL: {tp['sl']}\nTP1: {tp['tp1']} | TP2: {tp['tp2']}\n"
        f"Suggested Leverage: x{suggest_leverage_from_score(sig_dict['score'])}\n"
        f"Reasons: {', '.join(sig_dict['reasons'])}"
    )
    bot.send_message(chat_id, msg)

# -----------------------
# Auto worker: process auto subscriptions
# -----------------------
def auto_worker():
    while True:
        try:
            subs = get_all_auto_subscriptions()
            for sub in subs:
                sub_id, chat_id, coin, timeframe, last_sent = sub
                result = generate_multi_signal(coin, timeframe)
                if "error" in result:
                    # skip if not enough data
                    continue
                # dedupe: if last candle open_time equals last_sent -> skip
                if int(result.get("last_open_time", 0)) > int(last_sent or 0):
                    # send messages (step-by-step)
                    send_signal_messages(chat_id, result)
                    update_last_sent(sub_id, result["last_open_time"])
                    # small sleep to avoid spamming API
                    time.sleep(1.0)
            # loop interval
            time.sleep(10)
        except Exception as e:
            print("Auto worker error:", e)
            time.sleep(5)

# -----------------------
# UI / Commands: Menus & Flows
# -----------------------
def main_menu_markup():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("📈 Signals", "📌 My Coins")
    markup.row("➕ Add Coin", "➖ Remove Coin")
    markup.row("🚀 Top Movers", "⚙️ Settings")
    return markup

def signals_menu_markup():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("📌 My Coins", "🌍 All Coins")
    markup.row("🎯 Particular Coin", "🚀 Top Movers")
    markup.row("⬅ Back")
    return markup

@bot.message_handler(commands=["start"])
def cmd_start(m):
    get_or_create_settings(m.chat.id)
    bot.send_message(m.chat.id, "🤖 Bot is live — choose an option:", reply_markup=main_menu_markup())

@bot.message_handler(func=lambda msg: msg.text == "📈 Signals")
def open_signals_menu(msg):
    bot.send_message(msg.chat.id, "Choose signals option:", reply_markup=signals_menu_markup())

@bot.message_handler(func=lambda msg: msg.text == "⬅ Back")
def back_to_main(msg):
    bot.send_message(msg.chat.id, "Back to main menu:", reply_markup=main_menu_markup())

@bot.message_handler(func=lambda msg: msg.text == "📌 My Coins")
def my_coins_ui(msg):
    subs = list_subscriptions(msg.chat.id)
    if not subs:
        bot.send_message(msg.chat.id, "You have no coins saved. Use ➕ Add Coin", reply_markup=signals_menu_markup())
        return
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for row in subs:
        sub_id, coin, timeframe, auto, last = row[0], row[1], row[2], row[3], row[4]
        label = f"{coin}:{timeframe}" + ( " 🔁" if row[3] else "" )
        markup.add(label)
    markup.add("⬅ Back")
    bot.send_message(msg.chat.id, "Your saved subscriptions:", reply_markup=markup)

@bot.message_handler(func=lambda m: ":" in (m.text or "") and m.text.strip().upper().split(":")[0].isalnum())
def mycoin_selected(m):
    # expecting format COIN:TF
    try:
        text = m.text.strip().upper()
        if ":" not in text:
            return
        coin, tf = text.split(":", 1)
        coin = coin.replace("/", "").replace("USDT","")  # sanitize
        coin = coin.upper() + "USDT" if not coin.endswith("USDT") else coin
        # generate and show signal
        res = generate_multi_signal(coin, tf)
        if "error" in res:
            bot.send_message(m.chat.id, f"❌ {res['error']}", reply_markup=signals_menu_markup())
            return
        send_signal_messages(m.chat.id, res)
    except Exception as e:
        bot.send_message(m.chat.id, f"❌ Error: {e}")

# Add coin flow: ask symbol, then ask timeframe
@bot.message_handler(func=lambda m: m.text == "➕ Add Coin")
def add_coin_start(m):
    msg = bot.send_message(m.chat.id, "✍️ Send the coin symbol (e.g. BTCUSDT):")
    bot.register_next_step_handler(msg, add_coin_symbol_step)

def add_coin_symbol_step(m):
    symbol = (m.text or "").strip().upper()
    if not symbol:
        bot.send_message(m.chat.id, "❌ Invalid symbol. Cancelled.")
        return
    # ask timeframe
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for tf in TIMEFRAMES:
        markup.add(tf)
    markup.add("Cancel")
    msg = bot.send_message(m.chat.id, f"Selected {symbol}. Now choose timeframe:", reply_markup=markup)
    bot.register_next_step_handler(msg, add_coin_timeframe_step, symbol)

def add_coin_timeframe_step(m, symbol):
    tf = (m.text or "").strip()
    if tf == "Cancel":
        bot.send_message(m.chat.id, "Cancelled.", reply_markup=main_menu_markup())
        return
    if tf not in TIMEFRAMES:
        bot.send_message(m.chat.id, "❌ Invalid timeframe. Cancelled.", reply_markup=main_menu_markup())
        return
    # save
    add_subscription(m.chat.id, symbol, tf, auto=0)
    bot.send_message(m.chat.id, f"✅ Added {symbol} @ {tf}", reply_markup=main_menu_markup())

# Remove coin flow
@bot.message_handler(func=lambda m: m.text == "➖ Remove Coin")
def remove_coin_start(m):
    subs = list_subscriptions(m.chat.id)
    if not subs:
        bot.send_message(m.chat.id, "You have no subscriptions to remove.", reply_markup=main_menu_markup())
        return
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for r in subs:
        _, coin, tf, auto, last = r
        markup.add(f"Remove {coin}:{tf}")
    markup.add("Cancel")
    msg = bot.send_message(m.chat.id, "Select a subscription to remove:", reply_markup=markup)
    bot.register_next_step_handler(msg, remove_coin_step)

def remove_coin_step(m):
    text = (m.text or "").strip()
    if text == "Cancel":
        bot.send_message(m.chat.id, "Cancelled.", reply_markup=main_menu_markup())
        return
    if text.startswith("Remove "):
        pair = text.replace("Remove ", "")
        if ":" in pair:
            coin, tf = pair.split(":",1)
            remove_subscription(m.chat.id, coin, tf)
            bot.send_message(m.chat.id, f"✅ Removed {coin}:{tf}", reply_markup=main_menu_markup())
            return
    bot.send_message(m.chat.id, "❌ Not recognized. Cancelled.", reply_markup=main_menu_markup())

# Particular coin: ask symbol
@bot.message_handler(func=lambda m: m.text == "🎯 Particular Coin")
def particular_start(m):
    msg = bot.send_message(m.chat.id, "Enter coin symbol (e.g. BTCUSDT):")
    bot.register_next_step_handler(msg, particular_symbol_step)

def particular_symbol_step(m):
    symbol = (m.text or "").strip().upper()
    if not symbol:
        bot.send_message(m.chat.id, "❌ Invalid input.", reply_markup=signals_menu_markup())
        return
    # ask timeframe
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for tf in TIMEFRAMES:
        markup.add(tf)
    msg = bot.send_message(m.chat.id, f"Symbol {symbol}. Select timeframe:", reply_markup=markup)
    bot.register_next_step_handler(msg, particular_timeframe_step, symbol)

def particular_timeframe_step(m, symbol):
    tf = (m.text or "").strip()
    if tf not in TIMEFRAMES:
        bot.send_message(m.chat.id, "❌ Invalid timeframe.", reply_markup=signals_menu_markup())
        return
    res = generate_multi_signal(symbol, tf)
    if "error" in res:
        bot.send_message(m.chat.id, f"❌ {res['error']}", reply_markup=signals_menu_markup())
    else:
        send_signal_messages(m.chat.id, res)
    bot.send_message(m.chat.id, "Back to signals menu", reply_markup=signals_menu_markup())

# Top movers
@bot.message_handler(func=lambda m: m.text == "🚀 Top Movers")
def top_movers_cmd(m):
    try:
        tickers = requests.get("https://api.binance.com/api/v3/ticker/24hr", timeout=10).json()
        df = pd.DataFrame(tickers)
        df["priceChangePercent"] = pd.to_numeric(df["priceChangePercent"], errors="coerce").fillna(0)
        df = df[df["symbol"].str.endswith("USDT")]
        top = df.sort_values("priceChangePercent", ascending=False).head(5)
        bot.send_message(m.chat.id, "🔥 Top 5 Gainers (24h):\n" + "\n".join([f"{r['symbol']}: {r['priceChangePercent']}%" for _,r in top.iterrows()]))
    except Exception as e:
        bot.send_message(m.chat.id, f"❌ Error fetching top movers: {e}")

# Settings (default timeframe)
@bot.message_handler(func=lambda m: m.text == "⚙️ Settings")
def settings_menu(m):
    settings = get_or_create_settings(m.chat.id)
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for tf in TIMEFRAMES:
        markup.add(f"Set default {tf}")
    markup.add("⬅ Back")
    bot.send_message(m.chat.id, f"Default timeframe: {settings['default_timeframe']}", reply_markup=markup)

@bot.message_handler(func=lambda m: m.text and m.text.startswith("Set default "))
def set_default_tf(m):
    tf = m.text.replace("Set default ", "").strip()
    if tf in TIMEFRAMES:
        set_default_timeframe(m.chat.id, tf)
        bot.send_message(m.chat.id, f"✅ Default timeframe set to {tf}", reply_markup=main_menu_markup())
    else:
        bot.send_message(m.chat.id, "❌ Invalid timeframe.", reply_markup=main_menu_markup())

# -----------------------
# Start background threads
# -----------------------
if __name__ == "__main__":
    # start flask (keep-alive / port binding)
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # start auto worker
    worker_thread = threading.Thread(target=auto_worker, daemon=True)
    worker_thread.start()

    print("Bot polling starting...")
    bot.infinity_polling(skip_pending=True)




