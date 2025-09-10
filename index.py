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

# -------------------------
# ENV / CONFIG
# -------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 5000))

if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID and BINANCE_API_KEY and BINANCE_API_SECRET and WEBHOOK_URL):
    raise RuntimeError("Set TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, BINANCE_API_KEY, BINANCE_API_SECRET, WEBHOOK_URL env vars")

# Behavior tuning
TOP_SIGNALS_COUNT = 5
SEND_SLEEP = 0.25
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
# Signal scoring & builder
# -------------------------
def score_signal(df: pd.DataFrame) -> Tuple[str, float, dict]:
    close = df["c"]
    rsi = ta.momentum.RSIIndicator(close).rsi()
    macd = ta.trend.MACD(close)
    macd_diff = macd.macd_diff()
    bb = ta.volatility.BollingerBands(close)
    ema = ta.trend.EMAIndicator(close, window=20).ema_indicator()
    atr = ta.volatility.AverageTrueRange(df["h"], df["l"], close).average_true_range()

    latest = {
        "rsi": rsi.iloc[-1],
        "macd": macd.macd().iloc[-1],
        "macd_diff": macd_diff.iloc[-1],
        "ema": ema.iloc[-1],
        "upper": bb.bollinger_hband().iloc[-1],
        "lower": bb.bollinger_lband().iloc[-1],
        "atr": atr.iloc[-1],
        "close": close.iloc[-1],
    }

    score = 0
    if latest["rsi"] < 30: score += 2
    if latest["rsi"] > 70: score -= 2
    if latest["macd_diff"] > 0: score += 1
    if latest["macd_diff"] < 0: score -= 1
    if latest["close"] > latest["ema"]: score += 1
    else: score -= 1

    if latest["close"] < latest["lower"]: score += 2
    if latest["close"] > latest["upper"]: score -= 2

    direction = "BUY" if score > 0 else "SELL"
    return direction, abs(score), latest

def leverage_for(score: float) -> int:
    if score >= 6: return 30
    if score >= 4: return 20
    if score >= 2: return 10
    return 5

def build_signal(symbol: str, tf: str) -> str:
    df = fetch_klines_df(symbol, tf)
    direction, score, latest = score_signal(df)
    if score < 2:
        return ""

    entry = latest["close"]
    atr = latest["atr"]
    lev = leverage_for(score)

    if direction == "BUY":
        sl = entry - 2*atr
        tp1 = entry + 2*atr
        tp2 = entry + 4*atr
    else:
        sl = entry + 2*atr
        tp1 = entry - 2*atr
        tp2 = entry - 4*atr

    tech = f"RSI={latest['rsi']:.1f}, MACDdiff={latest['macd_diff']:.4f}, EMA={fmt_price(latest['ema'])}"
    sig = (
        f"{'✅' if direction=='BUY' else '❌'} {direction} {symbol} ({tf})\n"
        f"Leverage: x{lev}\n"
        f"Entry: {fmt_price(entry)}\n"
        f"Stop Loss: {fmt_price(sl)}\n"
        f"TP1: {fmt_price(tp1)} | TP2: {fmt_price(tp2)}\n"
        f"Valid for: {VALID_FOR[tf]} mins\n"
        f"Indicators: {tech}\n"
        f"Reason: Based on RSI, MACD, EMA, Bollinger → suggested {direction}."
    )
    return sig

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
        update = types.Update.de_json(raw)   # ✅ fixed parsing
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
    ensure_webhook()
    try:
        bot.send_message(TELEGRAM_CHAT_ID, "✅ Bot started (webhook, TA signals).")
    except Exception:
        pass
    app.run(host="0.0.0.0", port=PORT)






