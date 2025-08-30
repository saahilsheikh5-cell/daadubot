# index.py
import os
import json
import time
import traceback
from threading import Lock, Thread
from datetime import datetime

from flask import Flask, request
import telebot
from telebot import types

import pandas as pd
import numpy as np
import ta

from binance.client import Client
from apscheduler.schedulers.background import BackgroundScheduler

# ──────────────────────────────────────────────────────────────────────────────
# ENV VARS (Render → Environment)
# ──────────────────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
WEBHOOK_URL    = os.environ.get("WEBHOOK_URL")       # e.g. https://your-app.onrender.com
BINANCE_API_KEY    = os.environ.get("BINANCE_API_KEY")
BINANCE_API_SECRET = os.environ.get("BINANCE_API_SECRET")

if not (TELEGRAM_TOKEN and WEBHOOK_URL and BINANCE_API_KEY and BINANCE_API_SECRET):
    raise RuntimeError(
        "Please set TELEGRAM_TOKEN, WEBHOOK_URL, BINANCE_API_KEY, BINANCE_API_SECRET in Render."
    )

# ──────────────────────────────────────────────────────────────────────────────
# App / Bot / Binance
# ──────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)
bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=True, num_threads=4)
client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)

# ──────────────────────────────────────────────────────────────────────────────
# Persistence (JSON file)
# ──────────────────────────────────────────────────────────────────────────────
DATA_FILE = "settings.json"
DATA_LOCK = Lock()

DEFAULT_SETTINGS = {
    "rsi_oversold": 30,
    "rsi_overbought": 70,
    "min_confirmations": 2,      # confirmations within a timeframe to call BUY/SELL
    "ultra_min_score": 3,        # total votes across TFs for Ultra message
    "auto_interval": "15m",      # 5m|15m|1h|4h|1d
    "auto_mode": "my",           # my | all | both
    "scan_top_n": 100,           # for 'all coins'
    "timeframes": ["5m", "1h", "1d"]
}

DEFAULT_USER = {
    "my_coins": ["BTCUSDT", "ETHUSDT", "BNBUSDT"],
    "settings": DEFAULT_SETTINGS.copy()
}

def _init_store():
    if not os.path.exists(DATA_FILE):
        init = {"users": {}, "owner_chat_id": None}
        with open(DATA_FILE, "w") as f:
            json.dump(init, f, indent=2)
        return init
    try:
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    except Exception:
        init = {"users": {}, "owner_chat_id": None}
        with open(DATA_FILE, "w") as f:
            json.dump(init, f, indent=2)
        return init

STORE = _init_store()

def save_store():
    with DATA_LOCK:
        with open(DATA_FILE, "w") as f:
            json.dump(STORE, f, indent=2)

def get_user(chat_id: int):
    with DATA_LOCK:
        user = STORE["users"].get(str(chat_id))
        if not user:
            user = json.loads(json.dumps(DEFAULT_USER))  # deep copy
            STORE["users"][str(chat_id)] = user
            save_store()
        # ensure new keys exist if we add later
        for k, v in DEFAULT_SETTINGS.items():
            if k not in user["settings"]:
                user["settings"][k] = v
        return user

def set_owner_chat(chat_id: int):
    with DATA_LOCK:
        STORE["owner_chat_id"] = chat_id
        if str(chat_id) not in STORE["users"]:
            STORE["users"][str(chat_id)] = json.loads(json.dumps(DEFAULT_USER))
        save_store()

def get_owner_chat():
    with DATA_LOCK:
        return STORE.get("owner_chat_id")

# ──────────────────────────────────────────────────────────────────────────────
# Helpers — Binance fetchers
# ──────────────────────────────────────────────────────────────────────────────
def get_top_symbols(limit=100):
    """Top USDT symbols by 24h quoteVolume."""
    try:
        tickers = client.get_ticker()
        df = pd.DataFrame(tickers)
        df = df[df["symbol"].str.endswith("USDT")]
        df["quoteVolume"] = pd.to_numeric(df["quoteVolume"], errors="coerce").fillna(0.0)
        df = df.sort_values("quoteVolume", ascending=False)
        return df["symbol"].head(limit).tolist()
    except Exception:
        try:
            tickers = client.get_ticker()
            syms = [t["symbol"] for t in tickers if t["symbol"].endswith("USDT")]
            return syms[:limit]
        except Exception:
            return []

def get_top_movers(limit=20):
    """Top movers by 24h priceChangePercent."""
    try:
        tickers = client.get_ticker()
        df = pd.DataFrame(tickers)
        df = df[df["symbol"].str.endswith("USDT")]
        df["priceChangePercent"] = pd.to_numeric(df["priceChangePercent"], errors="coerce").fillna(0.0)
        top = df.sort_values("priceChangePercent", ascending=False).head(limit)
        return [f"{row['symbol']} ({row['priceChangePercent']}%)" for _, row in top.iterrows()]
    except Exception:
        return []

def fetch_klines_df(symbol, interval, limit=200):
    """Futures klines -> DataFrame."""
    try:
        raw = client.futures_klines(symbol=symbol.upper(), interval=interval, limit=limit)
        if not raw:
            return None
        cols = [
            "open_time","open","high","low","close","volume",
            "close_time","quote_asset_volume","num_trades","taker_buy_base","taker_buy_quote","ignore"
        ]
        df = pd.DataFrame(raw, columns=cols)
        for c in ["open","high","low","close","volume","quote_asset_volume"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        return df
    except Exception:
        return None

# ──────────────────────────────────────────────────────────────────────────────
# Indicators & Scoring (per timeframe)
# ──────────────────────────────────────────────────────────────────────────────
def candle_pattern(df):
    """Very simple engulfing detector on last 2 candles."""
    try:
        last = df.iloc[-1]
        prev = df.iloc[-2]
        # bullish engulfing
        if (last["close"] > last["open"]) and (last["open"] < prev["close"]) and (last["close"] > prev["open"]):
            return "BUY"
        # bearish engulfing
        if (last["close"] < last["open"]) and (last["open"] > prev["close"]) and (last["close"] < prev["open"]):
            return "SELL"
    except Exception:
        pass
    return None

def score_timeframe(symbol, tf, settings):
    """
    Returns (vote, details, last_price)
      vote: 'BUY'|'SELL'|None (HOLD)
      details: dictionary with component scores/votes & reasons
    """
    df = fetch_klines_df(symbol, tf, limit=200)
    if df is None or len(df) < 60:
        return None, {"error": "insufficient data"}, None

    close = df["close"]
    last_price = float(close.iloc[-1])
    reasons = []
    buy_votes = 0
    sell_votes = 0

    # RSI
    try:
        rsi_series = ta.momentum.RSIIndicator(close, window=14).rsi()
        rsi_val = float(rsi_series.iloc[-1])
        if rsi_val <= settings["rsi_oversold"]:
            buy_votes += 1; reasons.append("RSI oversold")
        elif rsi_val >= settings["rsi_overbought"]:
            sell_votes += 1; reasons.append("RSI overbought")
    except Exception:
        rsi_val = None

    # MACD
    try:
        macd_obj = ta.trend.MACD(close)
        macd = float(macd_obj.macd().iloc[-1])
        macd_signal = float(macd_obj.macd_signal().iloc[-1])
        if macd > macd_signal:
            buy_votes += 1; reasons.append("MACD>Signal")
        elif macd < macd_signal:
            sell_votes += 1; reasons.append("MACD<Signal")
    except Exception:
        macd = macd_signal = None

    # EMA 20/50
    try:
        ema20 = float(ta.trend.EMAIndicator(close, window=20).ema_indicator().iloc[-1])
        ema50 = float(ta.trend.EMAIndicator(close, window=50).ema_indicator().iloc[-1])
        if ema20 > ema50:
            buy_votes += 1; reasons.append("EMA20>EMA50")
        elif ema20 < ema50:
            sell_votes += 1; reasons.append("EMA20<EMA50")
    except Exception:
        ema20 = ema50 = None

    # Candle pattern
    cpat = candle_pattern(df)
    if cpat == "BUY":
        buy_votes += 1; reasons.append("Bullish engulfing")
    elif cpat == "SELL":
        sell_votes += 1; reasons.append("Bearish engulfing")

    # Volume boost (last > 1.2x mean of last 20)
    try:
        vol = df["volume"]
        if float(vol.iloc[-1]) > float(vol[-20:].mean()) * 1.2:
            if buy_votes > sell_votes:
                buy_votes += 1; reasons.append("Volume supports BUY")
            elif sell_votes > buy_votes:
                sell_votes += 1; reasons.append("Volume supports SELL")
        volume_boost = True
    except Exception:
        volume_boost = None

    # Final vote within timeframe requires min_confirmations to avoid noise
    vote = None
    if buy_votes >= settings["min_confirmations"] and buy_votes > sell_votes:
        vote = "BUY"
    elif sell_votes >= settings["min_confirmations"] and sell_votes > buy_votes:
        vote = "SELL"

    details = {
        "rsi": rsi_val,
        "macd": macd,
        "macd_signal": macd_signal,
        "ema20": ema20,
        "ema50": ema50,
        "candle": cpat,
        "volume_boost": volume_boost,
        "buy_votes": int(buy_votes),
        "sell_votes": int(sell_votes),
        "reasons": reasons,
    }
    return vote, details, last_price

# ──────────────────────────────────────────────────────────────────────────────
# Aggregate across TFs & trade plan
# ──────────────────────────────────────────────────────────────────────────────
def aggregate_decision(symbol, tfs, settings):
    buy_total = 0
    sell_total = 0
    tf_results = {}
    last_price = None

    for tf in tfs:
        v, det, price = score_timeframe(symbol, tf, settings)
        tf_results[tf] = {"vote": v, "details": det, "last_price": price}
        if price:
            last_price = price
        if det:
            buy_total += det.get("buy_votes", 0)
            sell_total += det.get("sell_votes", 0)

    direction = None
    if buy_total > sell_total:
        direction = "BUY"
    elif sell_total > buy_total:
        direction = "SELL"

    return direction, tf_results, last_price, buy_total, sell_total

def suggest_trade(price, direction, score_total):
    if not price or direction not in ("BUY", "SELL"):
        return None
    if direction == "BUY":
        entry = price
        sl  = round(entry * 0.994, 8)   # ~0.6% SL
        tp1 = round(entry * 1.008, 8)
        tp2 = round(entry * 1.015, 8)
    else:
        entry = price
        sl  = round(entry * 1.006, 8)
        tp1 = round(entry * 0.992, 8)
        tp2 = round(entry * 0.985, 8)

    lev_map = {1:2, 2:4, 3:8, 4:12, 5:20, 6:30}
    lev = lev_map.get(min(score_total, max(lev_map.keys())), 2)
    return {"entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2, "leverage": lev}

def format_ultra(symbol, direction, tf_results, trade_plan, buy_total, sell_total):
    header = f"🔥 *ULTRA {direction}* — *{symbol}*"
    score_line = f"*Votes:* BUY={buy_total}  SELL={sell_total}"
    per_tf = []
    for tf, res in tf_results.items():
        v = res.get("vote") or "HOLD"
        last = res.get("last_price")
        reasons = ", ".join(res.get("details", {}).get("reasons", [])) if res.get("details") else ""
        per_tf.append(f"• `{tf}`: *{v}* | Price: `{last}` | {reasons}")
    plan = [
        "",
        "*Suggested Trade Plan*",
        f"Entry: `{trade_plan['entry']}`",
        f"Stop Loss: `{trade_plan['sl']}`",
        f"TP1: `{trade_plan['tp1']}`",
        f"TP2: `{trade_plan['tp2']}`",
        f"Suggested Leverage: `{trade_plan['leverage']}x`"
    ]
    return "\n".join([header, score_line, ""] + per_tf + plan)

# ──────────────────────────────────────────────────────────────────────────────
# Auto Scanner (scheduler)
# ──────────────────────────────────────────────────────────────────────────────
INTERVAL_SECONDS = {"5m": 5*60, "15m": 15*60, "1h": 60*60, "4h": 4*60*60, "1d": 24*60*60}

scheduler = BackgroundScheduler()
scheduler.start()
AUTO_JOB_ID = "auto_scan_job"

def schedule_auto_job():
    try:
        if scheduler.get_job(AUTO_JOB_ID):
            scheduler.remove_job(AUTO_JOB_ID)
    except Exception:
        pass

    with DATA_LOCK:
        owner = STORE.get("owner_chat_id")
        if not owner:
            return  # schedule when /start sets owner

        settings = get_user(owner)["settings"]
        key = settings.get("auto_interval", "15m")
        seconds = INTERVAL_SECONDS.get(key, 15*60)
        scheduler.add_job(auto_scan_job, "interval", seconds=seconds, id=AUTO_JOB_ID, replace_existing=True)
        print(f"[Scheduler] Auto-scan every {key} ({seconds}s)")

def auto_scan_job():
    try:
        owner_chat = get_owner_chat()
        if not owner_chat:
            return
        u = get_user(owner_chat)
        s = u["settings"]
        tfs = s.get("timeframes", ["5m","1h","1d"])
        min_conf = s.get("min_confirmations", 2)
        ultra_min = s.get("ultra_min_score", 3)
        mode = s.get("auto_mode", "my")
        scan_top_n = s.get("scan_top_n", 100)

        targets = []
        if mode in ("my", "both"):
            targets += u.get("my_coins", [])
        if mode in ("all", "both"):
            targets += get_top_symbols(scan_top_n)
        targets = list(dict.fromkeys([t.upper() for t in targets]))

        for sym in targets:
            direction, tf_res, price, btot, stol = aggregate_decision(sym, tfs, s)
            total = max(btot, stol)
            votes_tf = [(tf_res[tf]["vote"] or "NONE") for tf in tfs]
            nn = [v for v in votes_tf if v != "NONE"]
            agree = len(nn) > 0 and len(set(nn)) == 1
            if direction and agree and total >= max(min_conf, ultra_min):
                plan = suggest_trade(price, direction, total)
                if plan:
                    msg = format_ultra(sym, direction, tf_res, plan, btot, stol)
                    bot.send_message(owner_chat, msg, parse_mode="Markdown")
    except Exception:
        traceback.print_exc()

# ──────────────────────────────────────────────────────────────────────────────
# Keyboards
# ──────────────────────────────────────────────────────────────────────────────
def main_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("My Coins", "All Coins")
    kb.row("Particular Coin", "Top Movers")
    kb.row("Add Coin", "Remove Coin")
    kb.row("Signal Settings", "Show My Coins")
    return kb

def settings_inline_kb(s):
    md = types.InlineKeyboardMarkup()
    md.row(
        types.InlineKeyboardButton(f"RSI {s['rsi_oversold']}/{s['rsi_overbought']}", callback_data="edit_rsi"),
        types.InlineKeyboardButton(f"Min Conf {s['min_confirmations']}", callback_data="edit_minconf"),
    )
    md.row(
        types.InlineKeyboardButton(f"Ultra Score {s['ultra_min_score']}", callback_data="edit_ultra"),
        types.InlineKeyboardButton(f"TopN {s['scan_top_n']}", callback_data="edit_topn"),
    )
    md.row(
        types.InlineKeyboardButton(f"Auto {s['auto_interval']}", callback_data="edit_interval"),
        types.InlineKeyboardButton(f"Mode {s['auto_mode']}", callback_data="edit_mode"),
    )
    md.row(
        types.InlineKeyboardButton(f"TFs {','.join(s['timeframes'])}", callback_data="edit_tfs"),
    )
    md.row(types.InlineKeyboardButton("Back", callback_data="settings_back"))
    return md

# ──────────────────────────────────────────────────────────────────────────────
# Telegram Handlers
# ──────────────────────────────────────────────────────────────────────────────
@bot.message_handler(commands=["start"])
def cmd_start(m):
    set_owner_chat(m.chat.id)
    schedule_auto_job()
    bot.send_message(m.chat.id, "Welcome — Ultra Pro Signals is live ✅", reply_markup=main_kb())

@bot.message_handler(func=lambda x: x.text == "Show My Coins")
def show_my_coins(m):
    u = get_user(m.chat.id)
    lst = u.get("my_coins", [])
    bot.send_message(m.chat.id, "Your coins:\n" + ("\n".join(lst) if lst else "None set"), reply_markup=main_kb())

@bot.message_handler(func=lambda x: x.text == "Add Coin")
def add_coin_prompt(m):
    msg = bot.send_message(m.chat.id, "Send coin symbol (e.g. BTCUSDT):")
    bot.register_next_step_handler(msg, add_coin_handler)

def add_coin_handler(m):
    sym = (m.text or "").strip().upper()
    if not sym:
        bot.send_message(m.chat.id, "Empty. Cancelled.", reply_markup=main_kb()); return
    if not sym.endswith("USDT"):
        sym += "USDT"
    u = get_user(m.chat.id)
    if sym in u["my_coins"]:
        bot.send_message(m.chat.id, f"{sym} already present.", reply_markup=main_kb()); return
    u["my_coins"].append(sym)
    save_store()
    bot.send_message(m.chat.id, f"Added {sym}.", reply_markup=main_kb())

@bot.message_handler(func=lambda x: x.text == "Remove Coin")
def remove_coin_prompt(m):
    u = get_user(m.chat.id)
    mc = u.get("my_coins", [])
    if not mc:
        bot.send_message(m.chat.id, "No coins to remove.", reply_markup=main_kb()); return
    kb = types.InlineKeyboardMarkup(row_width=2)
    for c in mc:
        kb.add(types.InlineKeyboardButton(c, callback_data=f"rm::{c}"))
    bot.send_message(m.chat.id, "Tap a coin to remove:", reply_markup=kb)

@bot.message_handler(func=lambda x: x.text == "My Coins")
def my_coins_scan(m):
    u = get_user(m.chat.id)
    syms = u.get("my_coins", [])
    bot.send_message(m.chat.id, "Scanning My Coins…")
    Thread(target=scan_and_push, args=(m.chat.id, syms), daemon=True).start()

@bot.message_handler(func=lambda x: x.text == "All Coins")
def all_coins_scan(m):
    u = get_user(m.chat.id)
    topn = u["settings"].get("scan_top_n", 100)
    syms = get_top_symbols(topn)
    bot.send_message(m.chat.id, f"Scanning Top {topn} USDT coins…")
    Thread(target=scan_and_push, args=(m.chat.id, syms), daemon=True).start()

@bot.message_handler(func=lambda x: x.text == "Top Movers")
def top_movers(m):
    movers = get_top_movers(50)
    if not movers:
        bot.send_message(m.chat.id, "Couldn't fetch movers.")
        return
    bot.send_message(m.chat.id, "Top movers (24h):\n" + "\n".join(movers[:20]))
    syms = [s.split()[0] for s in movers[:20]]
    Thread(target=scan_and_push, args=(m.chat.id, syms), daemon=True).start()

@bot.message_handler(func=lambda x: x.text == "Particular Coin")
def particular_coin_prompt(m):
    msg = bot.send_message(m.chat.id, "Send coin symbol (e.g. BTCUSDT):")
    bot.register_next_step_handler(msg, particular_coin_handler)

def particular_coin_handler(m):
    sym = (m.text or "").strip().upper()
    if not sym:
        bot.send_message(m.chat.id, "Empty. Cancelled.", reply_markup=main_kb()); return
    if not sym.endswith("USDT"):
        sym += "USDT"
    u = get_user(m.chat.id)
    s = u["settings"]
    tfs = s.get("timeframes", ["5m","1h","1d"])
    direction, tf_res, price, btot, stol = aggregate_decision(sym, tfs, s)
    total = max(btot, stol)
    votes_tf = [(tf_res[tf]["vote"] or "NONE") for tf in tfs]
    nn = [v for v in votes_tf if v != "NONE"]
    agree = len(nn) > 0 and len(set(nn)) == 1

    if direction and agree and total >= max(s.get("min_confirmations",2), s.get("ultra_min_score",3)):
        plan = suggest_trade(price, direction, total)
        msg = format_ultra(sym, direction, tf_res, plan, btot, stol)
        bot.send_message(m.chat.id, msg, parse_mode="Markdown", reply_markup=main_kb())
    else:
        bot.send_message(m.chat.id, f"No Ultra signal for {sym} now. (BUY={btot} SELL={stol})", reply_markup=main_kb())

@bot.message_handler(func=lambda x: x.text == "Signal Settings")
def settings_menu(m):
    u = get_user(m.chat.id)
    s = u["settings"]
    bot.send_message(m.chat.id, "Signal Settings:", reply_markup=settings_inline_kb(s))

# ── Inline callbacks (remove coin + settings) ─────────────────────────────────
@bot.callback_query_handler(func=lambda c: True)
def cb(c):
    try:
        chat_id = c.message.chat.id
        u = get_user(chat_id)
        s = u["settings"]
        data = c.data

        # Remove coin
        if data.startswith("rm::"):
            coin = data.split("::",1)[1]
            if coin in u["my_coins"]:
                u["my_coins"].remove(coin)
                save_store()
                bot.edit_message_text(f"Removed {coin}.", chat_id, c.message.message_id)
            else:
                bot.answer_callback_query(c.id, "Not found.")
            return

        # Settings navigation
        if data == "settings_back":
            bot.send_message(chat_id, "Back to main menu.", reply_markup=main_kb()); return

        if data == "edit_rsi":
            msg = bot.send_message(chat_id, "Send two integers: <oversold> <overbought> (e.g. 28 72)")
            bot.register_next_step_handler(msg, handle_edit_rsi); return

        if data == "edit_minconf":
            msg = bot.send_message(chat_id, "Send minimum confirmations (integer, e.g. 2):")
            bot.register_next_step_handler(msg, handle_edit_minconf); return

        if data == "edit_ultra":
            msg = bot.send_message(chat_id, "Send Ultra min score (integer, e.g. 3):")
            bot.register_next_step_handler(msg, handle_edit_ultra); return

        if data == "edit_topn":
            msg = bot.send_message(chat_id, "Send Top N coins to scan (e.g. 100):")
            bot.register_next_step_handler(msg, handle_edit_topn); return

        if data == "edit_interval":
            kb = types.InlineKeyboardMarkup()
            for key in ["5m","15m","1h","4h","1d"]:
                kb.add(types.InlineKeyboardButton(key, callback_data=f"interval::{key}"))
            bot.send_message(chat_id, "Choose auto interval:", reply_markup=kb); return

        if data.startswith("interval::"):
            key = data.split("::",1)[1]
            s["auto_interval"] = key
            save_store()
            schedule_auto_job()
            bot.edit_message_text(f"Auto interval set to {key}.", chat_id, c.message.message_id); return

        if data == "edit_mode":
            kb = types.InlineKeyboardMarkup()
            for m in ["my","all","both"]:
                kb.add(types.InlineKeyboardButton(m, callback_data=f"mode::{m}"))
            bot.send_message(chat_id, "Choose auto mode:", reply_markup=kb); return

        if data.startswith("mode::"):
            mode = data.split("::",1)[1]
            s["auto_mode"] = mode
            save_store()
            bot.edit_message_text(f"Auto mode set to {mode}.", chat_id, c.message.message_id); return

        if data == "edit_tfs":
            kb = types.InlineKeyboardMarkup()
            for tf in ["5m","15m","1h","4h","1d"]:
                sel = "✅" if tf in s["timeframes"] else "➕"
                kb.add(types.InlineKeyboardButton(f"{sel} {tf}", callback_data=f"tftoggle::{tf}"))
            kb.add(types.InlineKeyboardButton("Done", callback_data="tfdone"))
            bot.send_message(chat_id, "Toggle timeframes:", reply_markup=kb); return

        if data.startswith("tftoggle::"):
            tf = data.split("::",1)[1]
            if tf in s["timeframes"]:
                s["timeframes"].remove(tf)
            else:
                s["timeframes"].append(tf)
            # keep order by INTERVAL_SECONDS list order
            order = ["5m","15m","1h","4h","1d"]
            s["timeframes"] = [x for x in order if x in s["timeframes"]]
            save_store()
            # refresh inline list
            kb = types.InlineKeyboardMarkup()
            for t in ["5m","15m","1h","4h","1d"]:
                sel = "✅" if t in s["timeframes"] else "➕"
                kb.add(types.InlineKeyboardButton(f"{sel} {t}", callback_data=f"tftoggle::{t}"))
            kb.add(types.InlineKeyboardButton("Done", callback_data="tfdone"))
            bot.edit_message_reply_markup(chat_id, c.message.message_id, reply_markup=kb)
            return

        if data == "tfdone":
            bot.edit_message_text(f"TFs set: {','.join(s['timeframes'])}", chat_id, c.message.message_id)
            return

        # Fallback
        bot.answer_callback_query(c.id, "Unknown action.")
    except Exception:
        traceback.print_exc()
        try:
            bot.answer_callback_query(c.id, "Error.")
        except Exception:
            pass

# ── Settings Editors (text replies) ──────────────────────────────────────────
def handle_edit_rsi(m):
    try:
        a, b = map(int, (m.text or "").split())
        u = get_user(m.chat.id)
        u["settings"]["rsi_oversold"] = a
        u["settings"]["rsi_overbought"] = b
        save_store()
        bot.send_message(m.chat.id, f"RSI set to {a}/{b}.", reply_markup=main_kb())
    except Exception:
        bot.send_message(m.chat.id, "Invalid. Example: 28 72", reply_markup=main_kb())

def handle_edit_minconf(m):
    try:
        v = int(m.text.strip())
        u = get_user(m.chat.id)
        u["settings"]["min_confirmations"] = v
        save_store()
        bot.send_message(m.chat.id, f"Min confirmations set to {v}.", reply_markup=main_kb())
    except Exception:
        bot.send_message(m.chat.id, "Invalid integer.", reply_markup=main_kb())

def handle_edit_ultra(m):
    try:
        v = int(m.text.strip())
        u = get_user(m.chat.id)
        u["settings"]["ultra_min_score"] = v
        save_store()
        bot.send_message(m.chat.id, f"Ultra min score set to {v}.", reply_markup=main_kb())
    except Exception:
        bot.send_message(m.chat.id, "Invalid integer.", reply_markup=main_kb())

def handle_edit_topn(m):
    try:
        v = int(m.text.strip())
        u = get_user(m.chat.id)
        u["settings"]["scan_top_n"] = max(1, min(500, v))
        save_store()
        bot.send_message(m.chat.id, f"Top N set to {u['settings']['scan_top_n']}.", reply_markup=main_kb())
    except Exception:
        bot.send_message(m.chat.id, "Invalid integer.", reply_markup=main_kb())

# ──────────────────────────────────────────────────────────────────────────────
# On-demand scanner (threaded)
# ──────────────────────────────────────────────────────────────────────────────
def scan_and_push(chat_id, symbols):
    u = get_user(chat_id)
    s = u["settings"]
    tfs = s.get("timeframes", ["5m","1h","1d"])
    min_conf = s.get("min_confirmations", 2)
    ultra_min = s.get("ultra_min_score", 3)

    for sym in symbols:
        try:
            direction, tf_res, price, btot, stol = aggregate_decision(sym, tfs, s)
            total = max(btot, stol)
            votes_tf = [(tf_res[tf]["vote"] or "NONE") for tf in tfs]
            nn = [v for v in votes_tf if v != "NONE"]
            agree = len(nn) > 0 and len(set(nn)) == 1
            if direction and agree and total >= max(min_conf, ultra_min):
                plan = suggest_trade(price, direction, total)
                if plan:
                    msg = format_ultra(sym, direction, tf_res, plan, btot, stol)
                    bot.send_message(chat_id, msg, parse_mode="Markdown")
        except Exception:
            traceback.print_exc()

# ──────────────────────────────────────────────────────────────────────────────
# Webhook endpoints
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def root():
    return "Ultra Pro Signals Bot is running."

@app.route("/health", methods=["GET"])
def health():
    return "OK"

@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def telegram_webhook():
    try:
        payload = request.get_data().decode("utf-8")
        if not payload:
            return "", 400
        update = telebot.types.Update.de_json(payload)
        bot.process_new_updates([update])
    except Exception:
        traceback.print_exc()
    return "", 200

def set_webhook():
    try:
        bot.remove_webhook()
    except Exception:
        pass
    bot.set_webhook(url=f"{WEBHOOK_URL}/{TELEGRAM_TOKEN}")
    print("Webhook set:", f"{WEBHOOK_URL}/{TELEGRAM_TOKEN}")

# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    set_webhook()
    # schedule if owner already known (after first /start it will reschedule anyway)
    if get_owner_chat():
        schedule_auto_job()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)



