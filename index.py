#!/usr/bin/env python3
"""
Final webhook-only Telegram signals bot (TA-based)
Features:
 - Persistent my_coins.json (atomic write)
 - TA with ta (RSI, MACD, Bollinger, ATR, EMA)
 - Only STRONG / ULTRA signals (top-N selection) — avoids spam
 - Adaptive leverage based on score
 - Correct directional TP/SL (BUY -> TP above; SELL -> TP below)
 - Inline UI (timeframes, back), Add/Remove/My Coins
 - Top Movers and Auto mode (sends to TELEGRAM_CHAT_ID)
 - Webhook-only (no polling) — set WEBHOOK_URL env var
"""
import os
import time
import json
import threading
from math import isfinite
from typing import List, Tuple

import numpy as np
import pandas as pd
import ta
from flask import Flask, request
from telebot import TeleBot, types
from binance.client import Client
from binance.exceptions import BinanceAPIException

# -------------------------
# ENV / CONFIG
# -------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))  # admin / auto destination
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # e.g. https://your-app.onrender.com
PORT = int(os.getenv("PORT", 5000))

if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID and BINANCE_API_KEY and BINANCE_API_SECRET and WEBHOOK_URL):
    raise RuntimeError("Set TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, BINANCE_API_KEY, BINANCE_API_SECRET, WEBHOOK_URL env vars")

# Behavior tuning
TOP_SIGNALS_COUNT = 5        # how many strongest signals to send for "All Coins" & Auto
SEND_SLEEP = 0.25            # seconds between messages to avoid rate limits
MY_COINS_FILE = "my_coins.json"
VALID_FOR = {"1m":3, "5m":15, "15m":45, "1h":180, "1d":1440}
TIMEFRAMES = ["1m","5m","15m","1h","1d"]
AUTO_SLEEP_MAP = {"1m":60, "5m":300, "15m":900, "1h":3600, "1d":86400}

# -------------------------
# Clients & globals
# -------------------------
bot = TeleBot(TELEGRAM_TOKEN, parse_mode=None, threaded=True)
app = Flask(__name__)
client = Client(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)

auto_flag = False
auto_tf = "5m"
auto_thread = None

movers_flag = False
movers_thread = None

# -------------------------
# Persistence helpers
# -------------------------
def load_my_coins() -> List[str]:
    try:
        if not os.path.exists(MY_COINS_FILE):
            return []
        with open(MY_COINS_FILE, "r") as f:
            data = json.load(f)
            if isinstance(data, list):
                return [s.upper() for s in data]
    except Exception:
        pass
    return []

def save_my_coins_atomic(coins: List[str]):
    tmp = MY_COINS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump([c.upper() for c in coins], f)
    os.replace(tmp, MY_COINS_FILE)

# -------------------------
# Binance + TA helpers
# -------------------------
INTERVAL_MAP = {"1m":"1m","5m":"5m","15m":"15m","1h":"1h","1d":"1d"}

def fetch_klines_df(symbol: str, tf: str, limit: int = 240) -> pd.DataFrame:
    raw = client.get_klines(symbol=symbol, interval=INTERVAL_MAP[tf], limit=limit)
    cols = ["open_time","o","h","l","c","v","close_time","qav","num_trades","taker_base","taker_quote","ignore"]
    df = pd.DataFrame(raw, columns=cols)
    for c in ["o","h","l","c","v"]:
        df[c] = df[c].astype(float)
    return df

def fmt_price(p: float) -> str:
    if not isfinite(p):
        return str(p)
    if p >= 1000:
        return f"{p:.2f}"
    if p >= 1:
        return f"{p:.4f}"
    return f"{p:.6f}"

# -------------------------
# Signal scoring & builder (uses ta)
# -------------------------
def score_and_build(symbol: str, tf: str) -> Tuple[float,str]:
    """
    Return (score, message). score==0 => no strong signal.
    """
    try:
        df = fetch_klines_df(symbol, tf, limit=300)
    except Exception as e:
        return 0.0, None

    c = df["c"]
    if len(c) < 60:
        return 0.0, None

    # indicators via ta
    try:
        rsi_v = ta.momentum.rsi(c, window=14).iloc[-1]
        macd_line = ta.trend.macd(c).iloc[-1]
        macd_sig = ta.trend.macd_signal(c).iloc[-1]
        ema20 = ta.trend.ema_indicator(c, window=20).iloc[-1]
        ema50 = ta.trend.ema_indicator(c, window=50).iloc[-1]
        ema200 = ta.trend.ema_indicator(c, window=200).iloc[-1]
        bb_mid = ta.volatility.bollinger_mavg(c, window=20).iloc[-1]
        bb_up = ta.volatility.bollinger_hband(c, window=20, window_dev=2).iloc[-1]
        bb_lo = ta.volatility.bollinger_lband(c, window=20, window_dev=2).iloc[-1]
        atr_v = ta.volatility.average_true_range(df["h"], df["l"], df["c"], window=14).iloc[-1]
    except Exception:
        # fallback minimal indicators
        rsi_v = float(np.nan)
        macd_line = float(np.nan)
        macd_sig = float(np.nan)
        ema20 = ema50 = ema200 = float(np.nan)
        bb_up = bb_lo = bb_mid = float(np.nan)
        atr_v = max(float(c.iloc[-1]) * 0.005, 0.01)

    price = float(c.iloc[-1])

    # scores
    buy_score = 0.0
    sell_score = 0.0

    # EMA structure
    if price > ema20 > ema50 > ema200:
        buy_score += 3.0
    if price < ema20 < ema50 < ema200:
        sell_score += 3.0

    # RSI
    if rsi_v >= 70: buy_score += 2.0
    elif rsi_v >= 60: buy_score += 1.0
    if rsi_v <= 30: sell_score += 2.0
    elif rsi_v <= 40: sell_score += 1.0

    # MACD alignment
    if not np.isnan(macd_line) and not np.isnan(macd_sig):
        if macd_line > macd_sig: buy_score += 2.0
        if macd_line < macd_sig: sell_score += 2.0

    # Bollinger
    if price > bb_up and rsi_v >= 60: buy_score += 1.0
    if price < bb_lo and rsi_v <= 40: sell_score += 1.0

    # recent momentum
    recent_slope = (c.iloc[-1] - c.iloc[-6]) / (c.iloc[-6] + 1e-12)
    if recent_slope > 0.01: buy_score += 0.7
    if recent_slope < -0.01: sell_score += 0.7

    # Decide best side
    best_side = None
    score = 0.0
    if buy_score >= 5.0 and buy_score >= sell_score + 1.5:
        best_side = "BUY"; score = buy_score
    elif sell_score >= 5.0 and sell_score >= buy_score + 1.5:
        best_side = "SELL"; score = sell_score
    else:
        return 0.0, None  # neutral

    # Label
    label = "ULTRA BUY" if best_side=="BUY" and score >= 7 else "STRONG BUY" if best_side=="BUY" else \
            "ULTRA SELL" if best_side=="SELL" and score >= 7 else "STRONG SELL"

    # adaptive leverage by score (conservative caps)
    if score >= 8.0:
        lev = "x20"
    elif score >= 6.0:
        lev = "x10"
    else:
        lev = "x5"

    # ATR fallback safeguard
    if not isfinite(atr_v) or atr_v <= 0:
        atr_v = max(price * 0.005, 0.01)

    # directional TP/SL
    if best_side == "BUY":
        entry = price
        tp1 = entry + 1.0 * atr_v
        tp2 = entry + 2.0 * atr_v
        sl = entry - 1.0 * atr_v
    else:
        entry = price
        tp1 = entry - 1.0 * atr_v
        tp2 = entry - 2.0 * atr_v
        sl = entry + 1.0 * atr_v

    valid = VALID_FOR.get(tf, 15)

    # short 2-line summary suggestion
    trend = "Bullish" if best_side=="BUY" else "Bearish"
    momentum = "strong momentum" if abs(macd_line - macd_sig) > (0.03 * max(1, abs(price))) else "moderate momentum"
    rsi_note = ("overbought" if rsi_v > 70 else "oversold" if rsi_v < 30 else "neutral") if isfinite(rsi_v) else "n/a"
    notes = []
    if best_side=="BUY":
        if price > ema20 > ema50 > ema200: notes.append("EMA up")
        if macd_line > macd_sig: notes.append("MACD bull")
        if rsi_v >= 60: notes.append("RSI elevated")
    else:
        if price < ema20 < ema50 < ema200: notes.append("EMA down")
        if macd_line < macd_sig: notes.append("MACD bear")
        if rsi_v <= 40: notes.append("RSI weak")
    notes_text = ", ".join(notes) if notes else "Multi-index confirm"

    # Build message
    msg = (
        f"{'🟢' if 'BUY' in label else '🔴'} {label} {symbol} ({tf})\n"
        f"RSI14: {rsi_v:.2f if isfinite(rsi_v) else 'n/a'} | MACD: {macd_line:.4f if isfinite(macd_line) else 'n/a'}/{macd_sig:.4f if isfinite(macd_sig) else 'n/a'} | ATR14: {fmt_price(atr_v)}\n"
        f"EMA20/50/200: {fmt_price(ema20)}/{fmt_price(ema50)}/{fmt_price(ema200)}\n\n"
        f"Entry: {fmt_price(entry)}\n"
        f"TP1: {fmt_price(tp1)} | TP2: {fmt_price(tp2)}\n"
        f"SL: {fmt_price(sl)}\n"
        f"Suggested Leverage: {lev}\n"
        f"Signal Validity: {valid} minutes\n\n"
        f"Notes: {notes_text}\n"
        f"Recommendation: {trend} with {momentum}. RSI {rsi_note}."
    )
    return float(score), msg

# -------------------------
# Top100 helper
# -------------------------
def fetch_top100_by_volume() -> List[str]:
    try:
        stats = client.get_ticker()
        df = pd.DataFrame(stats)
        df = df[df["symbol"].str.endswith("USDT")]
        df = df[~df["symbol"].isin(["BUSDUSDT","USDCUSDT"])]
        df["quoteVolume"] = pd.to_numeric(df.get("quoteVolume", 0), errors="coerce").fillna(0.0)
        df = df.sort_values("quoteVolume", ascending=False)
        return df["symbol"].tolist()[:100]
    except Exception:
        return []

# -------------------------
# Telegram UI helpers
# -------------------------
def main_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("➕ Add Coin", "➖ Remove Coin")
    kb.row("📋 My Coins", "📈 Signals")
    kb.row("🕑 Auto Signals", "⏹ Stop Auto")
    kb.row("🚀 Top Movers", "🚀 Movers Auto")
    return kb

def timeframe_inline(prefix: str, back: str):
    ik = types.InlineKeyboardMarkup(row_width=5)
    for tf in TIMEFRAMES:
        ik.add(types.InlineKeyboardButton(tf, callback_data=f"{prefix}|{tf}|{back}"))
    ik.add(types.InlineKeyboardButton("⬅️ Back", callback_data=f"back|{back}"))
    return ik

# -------------------------
# Bot handlers
# -------------------------
@bot.message_handler(commands=["start"])
def cmd_start(m):
    bot.send_message(m.chat.id, "🤖 Ultra Signals Bot — Ready", reply_markup=main_keyboard())

@bot.message_handler(func=lambda m: m.text == "➕ Add Coin")
def cmd_add(m):
    q = bot.send_message(m.chat.id, "Send coin symbol to add (e.g. ETHUSDT):")
    bot.register_next_step_handler(q, handle_add)

def handle_add(m):
    sym = m.text.strip().upper()
    if not sym.endswith("USDT"):
        bot.send_message(m.chat.id, "Please add a USDT pair, e.g., ETHUSDT", reply_markup=main_keyboard())
        return
    coins = load_my_coins()
    if sym in coins:
        bot.send_message(m.chat.id, f"{sym} already in list.", reply_markup=main_keyboard())
        return
    coins.append(sym)
    save_my_coins_atomic(coins)
    bot.send_message(m.chat.id, f"✅ {sym} added.", reply_markup=main_keyboard())

@bot.message_handler(func=lambda m: m.text == "➖ Remove Coin")
def cmd_remove(m):
    coins = load_my_coins()
    if not coins:
        bot.send_message(m.chat.id, "No coins to remove.", reply_markup=main_keyboard())
        return
    ik = types.InlineKeyboardMarkup()
    for c in coins:
        ik.add(types.InlineKeyboardButton(f"Remove {c}", callback_data=f"rm|{c}"))
    ik.add(types.InlineKeyboardButton("⬅️ Back", callback_data="back|main"))
    bot.send_message(m.chat.id, "Choose coin to remove:", reply_markup=ik)

@bot.callback_query_handler(func=lambda c: c.data.startswith("rm|"))
def cb_rm(c):
    _, coin = c.data.split("|",1)
    coins = load_my_coins()
    if coin in coins:
        coins.remove(coin)
        save_my_coins_atomic(coins)
        bot.edit_message_text(f"✅ Removed {coin}", chat_id=c.message.chat.id, message_id=c.message.message_id)
    else:
        bot.answer_callback_query(c.id, "Not found")

@bot.message_handler(func=lambda m: m.text == "📋 My Coins")
def cmd_mycoins(m):
    coins = load_my_coins()
    if not coins:
        bot.send_message(m.chat.id, "No coins. Add with ➕ Add Coin", reply_markup=main_keyboard())
    else:
        bot.send_message(m.chat.id, "Your coins:\n" + "\n".join(coins), reply_markup=main_keyboard())

# Signals menu
@bot.message_handler(func=lambda m: m.text == "📈 Signals")
def cmd_signals(m):
    ik = types.InlineKeyboardMarkup(row_width=2)
    ik.add(types.InlineKeyboardButton("💼 My Coins", callback_data="scope|my"))
    ik.add(types.InlineKeyboardButton("🌍 All Coins (Top100)", callback_data="scope|all"))
    ik.add(types.InlineKeyboardButton("🔎 Particular Coin", callback_data="scope|part"))
    ik.add(types.InlineKeyboardButton("⬅️ Back", callback_data="back|main"))
    bot.send_message(m.chat.id, "Choose scope:", reply_markup=ik)

@bot.callback_query_handler(func=lambda c: c.data.startswith("scope|"))
def cb_scope(c):
    _, scope = c.data.split("|",1)
    if scope == "part":
        bot.send_message(c.message.chat.id, "Send coin symbol (e.g. XRPUSDT):")
        bot.register_next_step_handler(c.message, handle_particular)
        return
    # show timeframe inline
    bot.edit_message_text("Choose timeframe:", chat_id=c.message.chat.id, message_id=c.message.message_id,
                         reply_markup=timeframe_inline(f"sig|{scope}", "signals"))

def handle_particular(m):
    sym = m.text.strip().upper()
    bot.send_message(m.chat.id, f"Choose timeframe for {sym}:", reply_markup=timeframe_inline(f"sigpart|{sym}", "signals"))

@bot.callback_query_handler(func=lambda c: c.data.startswith("sig|") or c.data.startswith("sigpart|"))
def cb_sig(c):
    parts = c.data.split("|")
    # sig|<scope>|<tf>|<back>  OR  sigpart|<sym>|<tf>|<back>
    if parts[0] == "sig":
        # expected len >=3
        if len(parts) < 3:
            bot.answer_callback_query(c.id, "Bad data")
            return
        _, scope, tf = parts[0], parts[1], parts[2]
        if scope == "my":
            symbols = load_my_coins()
            if not symbols:
                bot.send_message(c.message.chat.id, "Your My Coins list is empty.")
                return
        else:
            symbols = fetch_top100_by_volume()
    else:
        # sigpart
        if len(parts) < 3:
            bot.answer_callback_query(c.id, "Bad data")
            return
        _, sym, tf = parts[0], parts[1], parts[2]
        symbols = [sym]

    scored = []
    for s in symbols:
        try:
            sc, msg = score_and_build(s, tf)
            if sc and msg:
                scored.append((sc, s, msg))
        except Exception:
            continue

    if not scored:
        bot.send_message(c.message.chat.id, "No STRONG/ULTRA signals at the moment for that selection.", reply_markup=main_keyboard())
        return

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:TOP_SIGNALS_COUNT]
    for sc, s, msg in top:
        bot.send_message(c.message.chat.id, msg)
        time.sleep(SEND_SLEEP)
    bot.send_message(c.message.chat.id, f"Sent top {len(top)} signals.", reply_markup=main_keyboard())

# -------------------------
# Auto signals (admin chat)
# -------------------------
def auto_worker():
    global auto_flag, auto_tf
    while auto_flag:
        syms = fetch_top100_by_volume()
        results = []
        for s in syms:
            try:
                sc, msg = score_and_build(s, auto_tf)
                if sc and msg:
                    results.append((sc, s, msg))
            except Exception:
                continue
        results.sort(key=lambda x: x[0], reverse=True)
        top = results[:TOP_SIGNALS_COUNT]
        if top:
            for sc, s, msg in top:
                try:
                    bot.send_message(TELEGRAM_CHAT_ID, msg)
                    time.sleep(SEND_SLEEP)
                except Exception:
                    pass
            bot.send_message(TELEGRAM_CHAT_ID, f"Auto-scan ({auto_tf}): sent {len(top)} signals.")
        sec = AUTO_SLEEP_MAP.get(auto_tf, 300)
        for _ in range(sec):
            if not auto_flag:
                break
            time.sleep(1)

@bot.message_handler(func=lambda m: m.text == "🕑 Auto Signals")
def cmd_auto_ui(m):
    ik = types.InlineKeyboardMarkup(row_width=3)
    for tf in TIMEFRAMES:
        ik.add(types.InlineKeyboardButton(tf, callback_data=f"auto|{tf}"))
    ik.add(types.InlineKeyboardButton("Stop Auto", callback_data="auto|stop"))
    bot.send_message(m.chat.id, "Auto Signals — choose timeframe or Stop:", reply_markup=ik)

@bot.callback_query_handler(func=lambda c: c.data.startswith("auto|"))
def cb_auto(c):
    global auto_flag, auto_tf, auto_thread
    _, arg = c.data.split("|",1)
    if arg == "stop":
        auto_flag = False
        bot.edit_message_text("Auto stopped.", chat_id=c.message.chat.id, message_id=c.message.message_id)
        return
    auto_tf = arg
    if not auto_flag:
        auto_flag = True
        auto_thread = threading.Thread(target=auto_worker, daemon=True)
        auto_thread.start()
    bot.edit_message_text(f"Auto ON ({auto_tf}). Top {TOP_SIGNALS_COUNT} signals will be sent to admin.", chat_id=c.message.chat.id, message_id=c.message.message_id)

# -------------------------
# Top movers (manual + auto UI)
# -------------------------
@bot.message_handler(func=lambda m: m.text == "🚀 Top Movers")
def cmd_top_movers(m):
    try:
        stats = client.get_ticker()
        df = pd.DataFrame(stats)
        df = df[df["symbol"].str.endswith("USDT")]
        df["priceChangePercent"] = pd.to_numeric(df["priceChangePercent"], errors="coerce").fillna(0.0)
        top = df.sort_values("priceChangePercent", key=lambda s: s.abs(), ascending=False).head(10)
        lines = [f"{row.symbol}: {float(row.priceChangePercent):.2f}%" for _, row in top.iterrows()]
        bot.send_message(m.chat.id, "Top movers (24h):\n" + "\n".join(lines))
    except Exception as e:
        bot.send_message(m.chat.id, f"Error fetching movers: {e}")

@bot.message_handler(func=lambda m: m.text == "🚀 Movers Auto")
def cmd_movers_auto_ui(m):
    ik = types.InlineKeyboardMarkup()
    ik.add(types.InlineKeyboardButton("Start Movers Auto", callback_data="movers|start"))
    ik.add(types.InlineKeyboardButton("Stop Movers Auto", callback_data="movers|stop"))
    bot.send_message(m.chat.id, "Top Movers Auto:", reply_markup=ik)

def movers_worker():
    global movers_flag
    cooldown = {}
    while movers_flag:
        try:
            stats = client.get_ticker()
            df = pd.DataFrame(stats)
            df = df[df["symbol"].str.endswith("USDT")]
            df["priceChangePercent"] = pd.to_numeric(df["priceChangePercent"], errors="coerce").fillna(0.0)
            df = df.sort_values("priceChangePercent", key=lambda s: s.abs(), ascending=False).head(25)
            now = time.time()
            sent = 0
            for _, row in df.iterrows():
                sym = row["symbol"]; chg = float(row["priceChangePercent"])
                last = cooldown.get(sym, 0)
                if abs(chg) >= 5.0 and now - last > 20*60:
                    dirc = "🚀 Up" if chg > 0 else "🔻 Down"
                    try:
                        bot.send_message(TELEGRAM_CHAT_ID, f"{dirc} {sym}: {chg:.2f}% (24h)")
                        cooldown[sym] = now
                        sent += 1
                        time.sleep(SEND_SLEEP)
                    except Exception:
                        pass
            if sent:
                bot.send_message(TELEGRAM_CHAT_ID, f"Movers scan sent: {sent}")
        except Exception:
            pass
        for _ in range(60):
            if not movers_flag:
                break
            time.sleep(1)

@bot.callback_query_handler(func=lambda c: c.data.startswith("movers|"))
def cb_movers(c):
    global movers_flag, movers_thread
    _, act = c.data.split("|",1)
    if act == "start":
        if not movers_flag:
            movers_flag = True
            movers_thread = threading.Thread(target=movers_worker, daemon=True)
            movers_thread.start()
        bot.edit_message_text("Movers Auto started.", chat_id=c.message.chat.id, message_id=c.message.message_id)
    else:
        movers_flag = False
        bot.edit_message_text("Movers Auto stopped.", chat_id=c.message.chat.id, message_id=c.message.message_id)

# -------------------------
# Webhook endpoints & boot
# -------------------------
@app.route("/", methods=["GET","HEAD"])
def health():
    return "ok", 200

@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def webhook():
    try:
        raw = request.get_data().decode("utf-8")
        update = telebot.types.Update.de_json(raw)
        bot.process_new_updates([update])
    except Exception as e:
        print("Webhook processing error:", e)
    return "OK", 200

def ensure_webhook():
    url = f"{WEBHOOK_URL.rstrip('/')}/{TELEGRAM_TOKEN}"
    try:
        bot.remove_webhook()
        time.sleep(0.5)
        ok = bot.set_webhook(url=url, max_connections=40)
        print(f"[webhook] set {url} -> {ok}")
    except Exception as e:
        print("Failed to set webhook:", e)
        raise

if __name__ == "__main__":
    # warm top100
    try:
        _ = fetch_top100_by_volume()
    except Exception:
        pass
    ensure_webhook()
    try:
        bot.send_message(TELEGRAM_CHAT_ID, "✅ Bot started (webhook, TA signals).")
    except Exception:
        pass
    app.run(host="0.0.0.0", port=PORT)






