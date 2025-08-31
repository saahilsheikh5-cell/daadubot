# index.py
import os
import time
import json
import threading
from typing import List, Tuple
from math import isfinite

import numpy as np
import pandas as pd
from telebot import TeleBot, types
from binance.client import Client
from binance.exceptions import BinanceAPIException

# -----------------------
# Configuration / Env
# -----------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))  # admin/chat to send autosignals
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")

if not TELEGRAM_TOKEN or not BINANCE_API_KEY or not BINANCE_API_SECRET:
    raise RuntimeError("Please set TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, BINANCE_API_KEY, BINANCE_API_SECRET env vars.")

# How many top signals to send when "All Coins" selected or in auto mode
TOP_SIGNALS_COUNT = 5

# Where we persist users coins (simple file)
COINS_FILE = "my_coins.json"

# Sleep between sending messages to avoid flood
SEND_SLEEP = 0.25

# Polling vs Webhook: use polling for robust Render deployment
USE_POLLING = True

# -----------------------
# Helpers: load/save coins
# -----------------------
def load_coins() -> List[str]:
    if not os.path.exists(COINS_FILE):
        return []
    try:
        with open(COINS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return []

def save_coins(coins: List[str]):
    with open(COINS_FILE, "w") as f:
        json.dump(coins, f)

# -----------------------
# Binance / TA helpers
# -----------------------
client = Client(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)

INTERVAL_MAP = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "1h": "1h",
    "1d": "1d",
}

def fetch_klines_pd(symbol: str, timeframe: str, limit: int = 200) -> pd.DataFrame:
    """
    returns DataFrame with columns: o,h,l,c,v (floats). raises on Binance error.
    """
    kl = client.get_klines(symbol=symbol, interval=INTERVAL_MAP[timeframe], limit=limit)
    cols = ["open_time", "o", "h", "l", "c", "v", "close_time", "qav", "num_trades", "taker_base", "taker_quote", "ignore"]
    df = pd.DataFrame(kl, columns=cols)
    for col in ["o","h","l","c","v"]:
        df[col] = df[col].astype(float)
    return df

# Basic indicators (clear, digit-by-digit safe)
def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()

def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    ma_up = up.ewm(alpha=1/period, adjust=False).mean()
    ma_down = down.ewm(alpha=1/period, adjust=False).mean()
    rs = ma_up / (ma_down + 1e-12)
    return 100 - (100 / (1 + rs))

def macd(series: pd.Series, fast=12, slow=26, signal=9):
    macd_line = ema(series, fast) - ema(series, slow)
    macd_signal = ema(macd_line, signal)
    hist = macd_line - macd_signal
    return macd_line, macd_signal, hist

def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["h"]; low = df["l"]; close = df["c"]
    prev = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev).abs()
    tr3 = (low - prev).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, adjust=False).mean()

def bollinger(series: pd.Series, period: int = 20, n_std: float = 2.0):
    mid = series.rolling(period).mean()
    sd = series.rolling(period).std(ddof=0)
    up = mid + n_std * sd
    lo = mid - n_std * sd
    return mid, up, lo

def fmt(p: float) -> str:
    # careful decimal formatting based on magnitude
    if not isfinite(p):
        return str(p)
    if p >= 1000:
        return f"{p:.2f}"
    if p >= 1:
        return f"{p:.4f}"
    return f"{p:.6f}"

# -----------------------
# Decision/Scoring engine
# -----------------------
def score_and_signal(symbol: str, timeframe: str) -> Tuple[float, str]:
    """
    returns (score, message) where message is final formatted signal string.
    If score < threshold (neutral) returns (0, None)
    """
    try:
        df = fetch_klines_pd(symbol, timeframe, limit=240)
    except Exception as e:
        return 0.0, None

    c = df["c"]
    if len(c) < 60:
        return 0.0, None

    # compute indicators
    rsi14 = rsi(c, 14)
    macd_line, macd_sig, macd_hist = macd(c)
    ema20 = ema(c, 20)
    ema50 = ema(c, 50)
    ema200 = ema(c, 200)
    mid, bb_up, bb_lo = bollinger(c, 20)
    atr14 = atr(df, 14)

    last = -1
    price = float(c.iloc[last])
    rsi_v = float(rsi14.iloc[last])
    macd_v = float(macd_line.iloc[last])
    macd_s = float(macd_sig.iloc[last])
    hist_v = float(macd_hist.iloc[last])
    ema20_v = float(ema20.iloc[last])
    ema50_v = float(ema50.iloc[last])
    ema200_v = float(ema200.iloc[last])
    bb_up_v = float(bb_up.iloc[last])
    bb_lo_v = float(bb_lo.iloc[last])
    atr_v = float(atr14.iloc[last]) if not pd.isna(atr14.iloc[last]) else (price * 0.005)

    # build buy/sell score
    buy_score = 0.0
    sell_score = 0.0

    # EMA structure
    if price > ema20_v and ema20_v > ema50_v and ema50_v > ema200_v:
        buy_score += 3.0
    if price < ema20_v and ema20_v < ema50_v and ema50_v < ema200_v:
        sell_score += 3.0

    # RSI
    if rsi_v >= 70:
        buy_score += 2.0
    elif rsi_v >= 60:
        buy_score += 1.0
    if rsi_v <= 30:
        sell_score += 2.0
    elif rsi_v <= 40:
        sell_score += 1.0

    # MACD
    if macd_v > macd_s and hist_v > 0:
        buy_score += 2.0
    if macd_v < macd_s and hist_v < 0:
        sell_score += 2.0

    # Bollinger breakout
    if price > bb_up_v and rsi_v >= 60:
        buy_score += 1.0
    if price < bb_lo_v and rsi_v <= 40:
        sell_score += 1.0

    # recent momentum
    if c.iloc[-1] - c.iloc[-6] > 0:
        buy_score += 0.5
    else:
        sell_score += 0.5

    # combine
    best_side = None
    score = 0.0
    if buy_score >= 5 and buy_score >= sell_score + 1.5:
        best_side = "BUY"
        score = buy_score
    elif sell_score >= 5 and sell_score >= buy_score + 1.5:
        best_side = "SELL"
        score = sell_score
    else:
        return 0.0, None  # neutral

    # decide label
    label = "🟢 ULTRA BUY" if score >= 7 else "✅ STRONG BUY" if best_side == "BUY" else \
            "🔴 ULTRA SELL" if score >= 7 else "❌ STRONG SELL"

    # target calculation using ATR to be directional-correct
    if atr_v <= 0 or not isfinite(atr_v):
        atr_v = max(price * 0.005, 0.01)

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

    leverage_map = {"1m":"x5","5m":"x10","15m":"x15","1h":"x5","1d":"x3"}
    leverage = leverage_map.get(timeframe, "x10")
    validity_map = {"1m":3,"5m":15,"15m":45,"1h":180,"1d":1440}
    valid_for = validity_map.get(timeframe, 15)

    # short summary lines (2 lines)
    trend = "Bullish" if best_side == "BUY" else "Bearish"
    momentum = "strong momentum" if abs(hist_v) > 0.05 else "moderate momentum"
    rsi_note = "overbought" if rsi_v > 70 else "oversold" if rsi_v < 30 else "neutral"

    summary = f"{trend} with {momentum}. RSI {rsi_note}."

    message = (
        f"{label} {symbol} ({timeframe})\n"
        f"RSI14: {rsi_v:.2f} | MACD: {macd_v:.4f}/{macd_s:.4f}\n"
        f"EMA20/50/200: {fmt(ema20_v)}/{fmt(ema50_v)}/{fmt(ema200_v)} | ATR14: {fmt(atr_v)}\n\n"
        f"Entry: {fmt(entry)}\n"
        f"TP1: {fmt(tp1)} | TP2: {fmt(tp2)}\n"
        f"SL: {fmt(sl)}\n"
        f"Suggested Leverage: {leverage}\n"
        f"Signal Validity: {valid_for} minutes\n\n"
        f"Notes: {summary}"
    )

    return float(score), message

# -----------------------
# Utilities: top-100 selection
# -----------------------
def fetch_top100_by_volume() -> List[str]:
    try:
        stats = client.get_ticker()  # 24h stats
        df = pd.DataFrame(stats)
        df = df[df["symbol"].str.endswith("USDT")]
        df = df[~df["symbol"].isin(["BUSDUSDT","USDCUSDT"])]
        df["quoteVolume"] = pd.to_numeric(df.get("quoteVolume", 0), errors="coerce").fillna(0.0)
        df = df.sort_values("quoteVolume", ascending=False)
        syms = df["symbol"].tolist()[:100]
        return syms
    except Exception:
        return []

# -----------------------
# Telegram bot & UI
# -----------------------
bot = TeleBot(TELEGRAM_TOKEN)

TIMEFRAMES = ["1m","5m","15m","1h","1d"]

def main_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("➕ Add Coin", "➖ Remove Coin")
    kb.row("📋 My Coins", "📈 Signals")
    kb.row("🕑 Auto Signals", "⏹ Stop Auto")
    kb.row("🚀 Top Movers", "🚀 Movers Auto")
    return kb

def timeframe_inline(prefix: str, back_to: str):
    ik = types.InlineKeyboardMarkup(row_width=5)
    for tf in TIMEFRAMES:
        ik.add(types.InlineKeyboardButton(tf, callback_data=f"{prefix}|{tf}|{back_to}"))
    ik.add(types.InlineKeyboardButton("⬅️ Back", callback_data=f"back|{back_to}"))
    return ik

# Handlers: start / add / remove / list coins
@bot.message_handler(commands=["start"])
def cmd_start(m):
    bot.send_message(m.chat.id, "🤖 Ultra Signals Bot", reply_markup=main_keyboard())

@bot.message_handler(func=lambda m: m.text == "➕ Add Coin")
def cmd_add(m):
    msg = bot.send_message(m.chat.id, "Send coin symbol to add (e.g. ETHUSDT):")
    bot.register_next_step_handler(msg, process_add)

def process_add(m):
    s = m.text.strip().upper()
    coins = load_coins()
    if s in coins:
        bot.send_message(m.chat.id, f"⚠️ {s} already in your list.", reply_markup=main_keyboard())
        return
    if not s.endswith("USDT"):
        bot.send_message(m.chat.id, "❗ Please add a USDT pair like ETHUSDT", reply_markup=main_keyboard())
        return
    coins.append(s)
    save_coins(coins)
    bot.send_message(m.chat.id, f"✅ {s} added.", reply_markup=main_keyboard())

@bot.message_handler(func=lambda m: m.text == "➖ Remove Coin")
def cmd_remove(m):
    coins = load_coins()
    if not coins:
        bot.send_message(m.chat.id, "❌ No coins to remove.", reply_markup=main_keyboard())
        return
    ik = types.InlineKeyboardMarkup()
    for c in coins:
        ik.add(types.InlineKeyboardButton(f"Remove {c}", callback_data=f"rm|{c}"))
    ik.add(types.InlineKeyboardButton("⬅️ Back", callback_data="back|main"))
    bot.send_message(m.chat.id, "Choose coin to remove:", reply_markup=ik)

@bot.callback_query_handler(func=lambda c: c.data.startswith("rm|"))
def cb_remove_coin(c):
    _, coin = c.data.split("|",1)
    coins = load_coins()
    if coin in coins:
        coins.remove(coin)
        save_coins(coins)
        bot.edit_message_text(f"✅ Removed {coin}", chat_id=c.message.chat.id, message_id=c.message.message_id)
    else:
        bot.answer_callback_query(c.id, "Not found.")

@bot.message_handler(func=lambda m: m.text == "📋 My Coins")
def cmd_mycoins(m):
    coins = load_coins()
    if not coins:
        bot.send_message(m.chat.id, "No coins in your list. Add with ➕ Add Coin", reply_markup=main_keyboard())
        return
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
        bot.send_message(c.message.chat.id, "Send coin symbol (e.g. ETHUSDT):")
        bot.register_next_step_handler(c.message, lambda m: ask_particular(m))
        return
    # show timeframe inline
    bot.edit_message_text("Choose timeframe:", chat_id=c.message.chat.id, message_id=c.message.message_id,
                         reply_markup=timeframe_inline(f"sig|{scope}", "signals"))

def ask_particular(m):
    sym = m.text.strip().upper()
    # ask timeframe via inline
    bot.send_message(m.chat.id, f"Symbol: {sym} — choose timeframe:", reply_markup=timeframe_inline(f"sigpart|{sym}", "signals"))

@bot.callback_query_handler(func=lambda c: c.data.startswith("sig|"))
def cb_sig(c):
    # data: sig|scope|tf|signals  but we encoded as "sig|scope|back"? earlier we used prefix|tf|back
    # our timeframes use pattern f"{prefix}|{tf}|{back_to}"
    try:
        _, scope_tf_back = c.data.split("|",1)
        # we used format: "sig|<scope>" earlier in timeframe_inline -> actually prefix is "sig|{scope}"
        # For consistency with timeframe_inline(prefix, back_to) above, prefix passed is like "sig|my"
        # So c.data is like "sig|my|5m|signals" but we wrote callback_data f"{prefix}|{tf}|{back_to}"
        # So split by '|' into three parts:
        parts = c.data.split("|")
        # expected: [prefix_token, maybe scope, tf, back_to]
        # Example: ["sig","my","5m","signals"]
        if len(parts) == 4:
            _, scope, tf, back = parts
        elif len(parts) == 3:
            # maybe prefix was "sig|my" and we got ["sig|my","5m","signals"] — handle
            p1, tf, back = parts
            if "|" in p1:
                _, scope = p1.split("|",1)
            else:
                scope = "my"
        else:
            bot.answer_callback_query(c.id, "Bad callback format")
            return
    except Exception:
        bot.answer_callback_query(c.id, "Callback parse error")
        return

    # determine symbols to evaluate
    symbols = []
    if scope == "my":
        symbols = load_coins()
        if not symbols:
            bot.send_message(c.message.chat.id, "Your My Coins list is empty. Add coins first.")
            return
    elif scope == "all":
        symbols = fetch_top100_by_volume()
    else:
        bot.answer_callback_query(c.id, "Unknown scope")
        return

    bot.edit_message_text(f"Scanning top signals ({len(symbols)} coins) — please wait...", chat_id=c.message.chat.id, message_id=c.message.message_id)
    # score each symbol and pick top N by score
    scored = []
    for s in symbols:
        try:
            sc, msg = score_and_signal(s, tf)
            if sc and msg:
                scored.append((sc, s, msg))
        except Exception:
            continue

    if not scored:
        bot.send_message(c.message.chat.id, "No STRONG/ULTRA signals right now for that timeframe.", reply_markup=main_keyboard())
        return

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:TOP_SIGNALS_COUNT]
    for sc, s, msg in top:
        bot.send_message(c.message.chat.id, msg)
        time.sleep(SEND_SLEEP)

    bot.send_message(c.message.chat.id, f"Sent top {len(top)} signals (confidence sorted).", reply_markup=main_keyboard())

# Particular symbol flow (callback sigpart|SYMBOL|TF|back)
@bot.callback_query_handler(func=lambda c: c.data.startswith("sigpart|"))
def cb_sigpart(c):
    parts = c.data.split("|")
    # expected ["sigpart", SYMBOL, TF, back]
    if len(parts) == 4:
        _, sym, tf, back = parts
    elif len(parts) == 3:
        _, sym, tf = parts
    else:
        bot.answer_callback_query(c.id, "Bad data")
        return
    sc, msg = score_and_signal(sym, tf)
    if msg:
        bot.send_message(c.message.chat.id, msg)
    else:
        bot.send_message(c.message.chat.id, "No STRONG/ULTRA signal for that coin/timeframe.", reply_markup=main_keyboard())

# Auto mode (sends top signals to TELEGRAM_CHAT_ID)
auto_running_flag = False
auto_thread = None
auto_tf = "5m"

def auto_worker():
    global auto_running_flag
    while auto_running_flag:
        symbols = fetch_top100_by_volume()
        results = []
        for s in symbols:
            try:
                sc, msg = score_and_signal(s, auto_tf)
                if sc and msg:
                    results.append((sc, s, msg))
            except Exception:
                continue
        results.sort(key=lambda x: x[0], reverse=True)
        top = results[:TOP_SIGNALS_COUNT]
        if top and TELEGRAM_CHAT_ID:
            for sc, s, msg in top:
                try:
                    bot.send_message(TELEGRAM_CHAT_ID, msg)
                    time.sleep(SEND_SLEEP)
                except Exception:
                    continue
            # summary
            bot.send_message(TELEGRAM_CHAT_ID, f"Auto-scan ({auto_tf}): sent {len(top)} signals.")
        # sleep until next timeframe
        sleep_seconds = {"1m":60,"5m":300,"15m":900,"1h":3600,"1d":86400}.get(auto_tf, 300)
        for _ in range(int(sleep_seconds)):
            if not auto_running_flag:
                break
            time.sleep(1)

@bot.message_handler(func=lambda m: m.text == "🕑 Auto Signals")
def cmd_auto_ui(m):
    ik = types.InlineKeyboardMarkup(row_width=3)
    for tf in TIMEFRAMES:
        ik.add(types.InlineKeyboardButton(tf, callback_data=f"auto|{tf}"))
    ik.add(types.InlineKeyboardButton("Stop Auto", callback_data="auto|stop"))
    bot.send_message(m.chat.id, "Auto Signals — pick timeframe or Stop:", reply_markup=ik)

@bot.callback_query_handler(func=lambda c: c.data.startswith("auto|"))
def cb_auto(c):
    global auto_running_flag, auto_thread, auto_tf
    _, arg = c.data.split("|",1)
    if arg == "stop":
        auto_running_flag = False
        bot.edit_message_text("Auto stopped.", chat_id=c.message.chat.id, message_id=c.message.message_id)
    else:
        auto_tf = arg
        if not auto_running_flag:
            auto_running_flag = True
            auto_thread = threading.Thread(target=auto_worker, daemon=True)
            auto_thread.start()
        bot.edit_message_text(f"Auto ON ({auto_tf}) — sending top {TOP_SIGNALS_COUNT} signals to admin.", chat_id=c.message.chat.id, message_id=c.message.message_id)

# Top Movers (simple manual)
@bot.message_handler(func=lambda m: m.text == "🚀 Top Movers")
def cmd_top_movers(m):
    try:
        stats = client.get_ticker()
        df = pd.DataFrame(stats)
        df = df[df["symbol"].str.endswith("USDT")]
        df["priceChangePercent"] = pd.to_numeric(df["priceChangePercent"], errors="coerce").fillna(0.0)
        top = df.sort_values("priceChangePercent", key=lambda s: s.abs(), ascending=False).head(10)
        lines = [f"{r.symbol}: {float(r.priceChangePercent):.2f}%" for _, r in top.iterrows()]
        bot.send_message(m.chat.id, "Top movers (24h):\n" + "\n".join(lines))
    except Exception as e:
        bot.send_message(m.chat.id, f"Error fetching movers: {e}")

# -----------------------
# Run
# -----------------------
if __name__ == "__main__":
    # warm top100
    try:
        _ = fetch_top100_by_volume()
    except Exception:
        pass

    if USE_POLLING:
        bot.infinity_polling(timeout=60, long_polling_timeout=30)
    else:
        # webhook mode (optional) - not recommended for Render unless you set WEBHOOK_URL and HTTPS
        from flask import Flask, request
        app = Flask(__name__)
        @app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
        def webhook():
            update = request.get_json()
            bot.process_new_updates([telebot.types.Update.de_json(update)])
            return "OK", 200
        app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))









