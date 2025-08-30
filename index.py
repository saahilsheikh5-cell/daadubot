
# index.py
import os
import json
import traceback
from flask import Flask, request
import telebot
from telebot import types
from binance.client import Client
import pandas as pd
import numpy as np
import ta
from apscheduler.schedulers.background import BackgroundScheduler
from threading import Lock
import time

# ------------------------
# ENV VARS (set these in Render)
# ------------------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")       # e.g. https://your-app.onrender.com
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY")
BINANCE_API_SECRET = os.environ.get("BINANCE_API_SECRET")

if not (TELEGRAM_TOKEN and WEBHOOK_URL and BINANCE_API_KEY and BINANCE_API_SECRET):
    raise RuntimeError("Please set TELEGRAM_TOKEN, WEBHOOK_URL, BINANCE_API_KEY, BINANCE_API_SECRET env vars.")

# ------------------------
# Persistence file
# ------------------------
DATA_FILE = "user_data.json"
DATA_LOCK = Lock()

DEFAULT_DATA = {
    "my_coins": ["BTCUSDT", "ETHUSDT", "BNBUSDT"],
    "settings": {
        "rsi_oversold": 30,
        "rsi_overbought": 70,
        "min_votes_for_ultra": 3,
        "timeframes": ["5m", "1h", "1d"],
        "auto_interval": "1h",   # default auto-scan interval
        "auto_mode": "both",     # "my", "all", "both"
        "scan_top_n": 100,
        "ultra_min_score": 4     # minimal total votes for Ultra
    }
}

def load_data():
    with DATA_LOCK:
        if not os.path.exists(DATA_FILE):
            with open(DATA_FILE, "w") as f:
                json.dump(DEFAULT_DATA, f, indent=2)
            return DEFAULT_DATA.copy()
        try:
            with open(DATA_FILE, "r") as f:
                return json.load(f)
        except Exception:
            with open(DATA_FILE, "w") as f:
                json.dump(DEFAULT_DATA, f, indent=2)
            return DEFAULT_DATA.copy()

def save_data(data):
    with DATA_LOCK:
        with open(DATA_FILE, "w") as f:
            json.dump(data, f, indent=2)

data = load_data()

# ------------------------
# Init
# ------------------------
bot = telebot.TeleBot(TELEGRAM_TOKEN)
app = Flask(__name__)
client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)

scheduler = BackgroundScheduler()
scheduler.start()

# We'll keep one scheduled job id for auto-scan
AUTO_JOB_ID = "auto_scan_job"

# ------------------------
# Utilities: Binance & TA wrappers
# ------------------------
def get_top_symbols(limit=100):
    """Return top USDT symbols sorted by 24h quoteVolume (descending)."""
    try:
        tickers = client.get_ticker()
        df = pd.DataFrame(tickers)
        df = df[df['symbol'].str.endswith('USDT')]
        df['quoteVolume'] = df['quoteVolume'].astype(float)
        df = df.sort_values('quoteVolume', ascending=False)
        return df['symbol'].tolist()[:limit]
    except Exception:
        try:
            # fallback: collect USDT symbols
            tickers = client.get_ticker()
            return [t['symbol'] for t in tickers if t['symbol'].endswith('USDT')][:limit]
        except Exception:
            return []

def get_top_movers(limit=20):
    """Return top movers by priceChangePercent (24h)."""
    try:
        tickers = client.get_ticker()
        df = pd.DataFrame(tickers)
        df = df[df['symbol'].str.endswith('USDT')]
        df['priceChangePercent'] = df['priceChangePercent'].astype(float)
        top = df.sort_values('priceChangePercent', ascending=False).head(limit)
        return [f"{row['symbol']} ({row['priceChangePercent']}%)" for _, row in top.iterrows()]
    except Exception:
        return []

def fetch_klines_df(symbol, interval, limit=200):
    """Return a DataFrame of klines for symbol/interval or None."""
    try:
        raw = client.futures_klines(symbol=symbol.upper(), interval=interval, limit=limit)
        df = pd.DataFrame(raw, columns=[
            "open_time","open","high","low","close","volume",
            "close_time","quote_asset_volume","num_trades","taker_buy_base","taker_buy_quote","ignore"
        ])
        for c in ["open","high","low","close","volume","quote_asset_volume"]:
            df[c] = df[c].astype(float)
        return df
    except Exception:
        return None

# Indicator helpers
def calc_rsi(close_series, window=14):
    return ta.momentum.RSIIndicator(close_series, window=window).rsi()

def calc_macd(close_series):
    m = ta.trend.MACD(close_series)
    return m.macd(), m.macd_signal()

def calc_ema(close_series, window):
    return ta.trend.EMAIndicator(close_series, window=window).ema_indicator()

def candle_pattern_buy_sell(df):
    """Simple bullish/bearish engulfing detection for last 2 candles."""
    try:
        last = df.iloc[-1]
        prev = df.iloc[-2]
        # bullish engulfing (last green and engulfs prev)
        if (last['close'] > last['open']) and (last['open'] < prev['close']) and (last['close'] > prev['open']):
            return 'BUY'
        # bearish engulfing
        if (last['close'] < last['open']) and (last['open'] > prev['close']) and (last['close'] < prev['open']):
            return 'SELL'
    except Exception:
        pass
    return None

# ------------------------
# Scoring per timeframe
# ------------------------
def score_for_timeframe(symbol, tf, settings_local):
    """
    Returns: (vote: 'BUY'|'SELL'|None, details dict, last_price float)
    details contains rsi, macd, ema20, ema50, candle_pattern, buy_votes, sell_votes
    """
    df = fetch_klines_df(symbol, tf, limit=200)
    if df is None or len(df) < 30:
        return None, {"error": "insufficient data"}, None

    close = df['close']
    last_price = float(close.iloc[-1])
    details = {}
    buy_votes = 0
    sell_votes = 0
    reasons = []

    # RSI
    try:
        rsi_series = calc_rsi(close, window=14)
        rsi = float(rsi_series.iloc[-1])
        details['rsi'] = round(rsi, 2)
        if rsi <= settings_local['rsi_oversold']:
            buy_votes += 1
            reasons.append("RSI oversold")
        elif rsi >= settings_local['rsi_overbought']:
            sell_votes += 1
            reasons.append("RSI overbought")
    except Exception:
        details['rsi'] = None

    # MACD
    try:
        macd_series, macd_signal_series = calc_macd(close)
        macd = float(macd_series.iloc[-1])
        macd_signal = float(macd_signal_series.iloc[-1])
        details['macd'] = round(macd, 8)
        details['macd_signal'] = round(macd_signal, 8)
        if macd > macd_signal:
            buy_votes += 1
            reasons.append("MACD>Signal")
        elif macd < macd_signal:
            sell_votes += 1
            reasons.append("MACD<Signal")
    except Exception:
        details['macd'] = None

    # EMA crossover
    try:
        ema20 = float(calc_ema(close, 20).iloc[-1])
        ema50 = float(calc_ema(close, 50).iloc[-1])
        details['ema20'] = round(ema20, 6)
        details['ema50'] = round(ema50, 6)
        if ema20 > ema50:
            buy_votes += 1
            reasons.append("EMA20>EMA50")
        elif ema20 < ema50:
            sell_votes += 1
            reasons.append("EMA20<EMA50")
    except Exception:
        pass

    # Candle pattern
    try:
        cp = candle_pattern_buy_sell(df)
        details['candle'] = cp
        if cp == 'BUY':
            buy_votes += 1
            reasons.append("Bullish engulfing")
        elif cp == 'SELL':
            sell_votes += 1
            reasons.append("Bearish engulfing")
    except Exception:
        details['candle'] = None

    # Simple volume check: last volume > mean volume -> confidence
    try:
        vol = df['volume']
        if float(vol.iloc[-1]) > float(vol[-20:].mean()) * 1.2:
            # amplify majority vote
            if buy_votes > sell_votes:
                buy_votes += 1
                reasons.append("Volume supports BUY")
            elif sell_votes > buy_votes:
                sell_votes += 1
                reasons.append("Volume supports SELL")
            details['volume_boost'] = True
        else:
            details['volume_boost'] = False
    except Exception:
        details['volume_boost'] = None

    details['buy_votes'] = int(buy_votes)
    details['sell_votes'] = int(sell_votes)
    details['reasons'] = reasons

    vote = None
    if buy_votes > sell_votes:
        vote = 'BUY'
    elif sell_votes > buy_votes:
        vote = 'SELL'

    return vote, details, last_price

# ------------------------
# Aggregate across timeframes and decide Ultra
# ------------------------
def aggregate_and_decide(symbol, tf_list, settings_local):
    buy_total = 0
    sell_total = 0
    tf_results = {}
    last_price = None

    for tf in tf_list:
        vote, details, price = score_for_timeframe(symbol, tf, settings_local)
        tf_results[tf] = {"vote": vote, "details": details, "last_price": price}
        if price:
            last_price = price
        if details:
            buy_total += details.get("buy_votes", 0)
            sell_total += details.get("sell_votes", 0)

    agg_direction = None
    if buy_total > sell_total:
        agg_direction = 'BUY'
    elif sell_total > buy_total:
        agg_direction = 'SELL'

    return agg_direction, tf_results, last_price, buy_total, sell_total

# ------------------------
# Trade plan suggestion
# ------------------------
def suggest_trade_plan(price, direction, score_total):
    if not price or direction not in ("BUY","SELL"):
        return None
    # tuned ranges — you can adapt these later via settings if desired
    if direction == "BUY":
        entry = price
        sl = round(entry * 0.994, 8)   # 0.6% SL
        tp1 = round(entry * 1.008, 8)  # 0.8%
        tp2 = round(entry * 1.015, 8)  # 1.5%
    else:  # SELL
        entry = price
        sl = round(entry * 1.006, 8)
        tp1 = round(entry * 0.992, 8)
        tp2 = round(entry * 0.985, 8)

    # map score to leverage (simple)
    lev_map = {1:2, 2:4, 3:8, 4:12, 5:20, 6:30}
    suggested_lev = lev_map.get(min(score_total, max(lev_map.keys())), 2)

    return {"entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2, "leverage": suggested_lev}

def format_ultra_message(symbol, direction, tf_results, trade_plan, buy_total, sell_total):
    header = f"🔥 *ULTRA {direction}* detected for *{symbol}*"
    score_line = f"Score: buy_votes={buy_total} sell_votes={sell_total}"
    per_tf = []
    for tf, res in tf_results.items():
        v = res.get("vote") or "HOLD"
        last = res.get("last_price")
        reasons = ", ".join(res.get("details", {}).get("reasons", [])) if res.get("details") else ""
        per_tf.append(f"• {tf}: {v} | Price: {last} | {reasons}")
    plan = [
        "*Suggested Trade Plan:*",
        f"Entry: `{trade_plan['entry']}`",
        f"Stop Loss: `{trade_plan['sl']}`",
        f"TP1: `{trade_plan['tp1']}`",
        f"TP2: `{trade_plan['tp2']}`",
        f"Suggested Leverage: `{trade_plan['leverage']}x`"
    ]
    message = "\n".join([header, score_line, ""] + per_tf + [""] + plan)
    return message

# ------------------------
# Scheduler & Auto-scan
# ------------------------
INTERVAL_MAP = {
    "5m": 5*60,
    "15m": 15*60,
    "1h": 60*60,
    "4h": 4*60*60,
    "1d": 24*60*60
}

def get_interval_seconds(key):
    return INTERVAL_MAP.get(key, 60*60)

def auto_scan_job():
    try:
        d = load_data()
        settings_local = d['settings']
        mode = settings_local.get('auto_mode', 'my')  # 'my', 'all', 'both'
        interval = settings_local.get('auto_interval', '1h')
        tf_list = settings_local.get('timeframes', ['5m','1h','1d'])
        min_votes = settings_local.get('min_votes_for_ultra', 3)
        ultra_min_score = settings_local.get('ultra_min_score', 4)
        scan_top_n = settings_local.get('scan_top_n', 100)

        targets = []
        if mode in ('my','both'):
            targets += d.get('my_coins', [])
        if mode in ('all','both'):
            targets += get_top_symbols(scan_top_n)

        # dedupe
        targets = list(dict.fromkeys([t.upper() for t in targets]))

        chat_id = d.get('owner_chat_id')  # optionally if you set owner chat id
        # if no chat id stored, we will not auto-message — user must call /start once to set
        # But for single-user usage we will set owner_chat_id when first /start is used.
        if not chat_id:
            # skip auto messaging until owner chat id is set
            return

        for symbol in targets:
            try:
                agg_dir, tf_results, price, buy_total, sell_total = aggregate_and_decide(symbol, tf_list, settings_local)
                total_votes = max(buy_total, sell_total)
                # require agreement across TFs (non-NONE votes must be same)
                votes_per_tf = [ (tf_results[tf]['vote'] if tf_results[tf]['vote'] else 'NONE') for tf in tf_list ]
                non_none = [v for v in votes_per_tf if v != 'NONE']
                agree = len(non_none) > 0 and len(set(non_none)) == 1

                if agg_dir and agree and total_votes >= max(min_votes, ultra_min_score):
                    trade_plan = suggest_trade_plan(price, agg_dir, total_votes)
                    if trade_plan:
                        msg = format_ultra_message(symbol, agg_dir, tf_results, trade_plan, buy_total, sell_total)
                        bot.send_message(chat_id, msg, parse_mode="Markdown")
                # otherwise no message (reduce noise)
            except Exception:
                print("Auto-scan error for", symbol)
                traceback.print_exc()
    except Exception:
        traceback.print_exc()

def schedule_auto_job():
    # remove old job
    try:
        if scheduler.get_job(AUTO_JOB_ID):
            scheduler.remove_job(AUTO_JOB_ID)
    except Exception:
        pass

    d = load_data()
    interval_key = d['settings'].get('auto_interval', '1h')
    seconds = get_interval_seconds(interval_key)
    scheduler.add_job(auto_scan_job, 'interval', seconds=seconds, id=AUTO_JOB_ID, replace_existing=True)
    print("Scheduled auto-scan every", interval_key)

# ------------------------
# Telegram menu and handlers
# ------------------------
def build_main_keyboard():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("My Coins", "All Coins")
    kb.row("Particular Coin", "Top Movers")
    kb.row("Add Coin", "Remove Coin")
    kb.row("Signal Settings", "Show My Coins")
    return kb

@bot.message_handler(commands=['start'])
def handle_start(msg):
    # save owner chat id so auto scan knows where to send messages
    d = load_data()
    d['owner_chat_id'] = msg.chat.id
    save_data(d)
    # ensure scheduler is scheduled
    schedule_auto_job()
    bot.send_message(msg.chat.id, "Welcome — Ultra Pro Signals (auto & on-demand).", reply_markup=build_main_keyboard())

@bot.message_handler(func=lambda m: m.text == "Show My Coins")
def show_my_coins(m):
    d = load_data()
    mc = d.get('my_coins', [])
    bot.send_message(m.chat.id, "Your coins:\n" + ("\n".join(mc) if mc else "No coins saved."))

@bot.message_handler(func=lambda m: m.text == "Add Coin")
def add_coin_prompt(m):
    msg = bot.send_message(m.chat.id, "Send coin symbol to add (e.g. BTCUSDT):")
    bot.register_next_step_handler(msg, add_coin_handler)

def add_coin_handler(m):
    symbol = m.text.strip().upper()
    if not symbol.endswith("USDT"):
        symbol = symbol + "USDT"
    d = load_data()
    mc = d.get('my_coins', [])
    if symbol in mc:
        bot.send_message(m.chat.id, f"{symbol} already in My Coins.", reply_markup=build_main_keyboard())
        return
    mc.append(symbol)
    d['my_coins'] = mc
    save_data(d)
    bot.send_message(m.chat.id, f"Added {symbol} to My Coins.", reply_markup=build_main_keyboard())

@bot.message_handler(func=lambda m: m.text == "Remove Coin")
def remove_coin_prompt(m):
    d = load_data()
    mc = d.get('my_coins', [])
    if not mc:
        bot.send_message(m.chat.id, "Your My Coins list is empty.", reply_markup=build_main_keyboard())
        return
    # present inline buttons to remove easily
    markup = types.InlineKeyboardMarkup(row_width=2)
    for coin in mc:
        markup.add(types.InlineKeyboardButton(coin, callback_data=f"remove::{coin}"))
    bot.send_message(m.chat.id, "Tap a coin to remove:", reply_markup=markup)

@bot.message_handler(func=lambda m: m.text == "My Coins")
def my_coins_menu(m):
    d = load_data()
    mc = d.get('my_coins', [])
    # run immediate on-demand scan for listed coins
    bot.send_message(m.chat.id, "Running on-demand scan for My Coins...")
    from threading import Thread
    Thread(target=send_signals_for_list, args=(m.chat.id, mc), daemon=True).start()

@bot.message_handler(func=lambda m: m.text == "All Coins")
def all_coins_menu(m):
    bot.send_message(m.chat.id, "Running on-demand scan for Top 100 USDT coins...")
    symbols = get_top_symbols(100)
    from threading import Thread
    Thread(target=send_signals_for_list, args=(m.chat.id, symbols), daemon=True).start()

@bot.message_handler(func=lambda m: m.text == "Top Movers")
def top_movers_menu(m):
    movers = get_top_movers(50)
    bot.send_message(m.chat.id, "Top movers (24h):\n" + ("\n".join(movers[:20])))
    # also trigger quick scan on top movers
    symbols = [s.split()[0] for s in movers[:20]]
    from threading import Thread
    Thread(target=send_signals_for_list, args=(m.chat.id, symbols), daemon=True).start()

@bot.message_handler(func=lambda m: m.text == "Particular Coin")
def particular_coin_prompt(m):
    msg = bot.send_message(m.chat.id, "Send coin symbol (e.g. BTCUSDT):")
    bot.register_next_step_handler(msg, particular_coin_handler)

def particular_coin_handler(m):
    symbol = m.text.strip().upper()
    if not symbol.endswith("USDT"):
        symbol += "USDT"
    # run aggregated check and reply
    settings_local = load_data()['settings']
    tf_list = settings_local.get('timeframes', ['5m','1h','1d'])
    agg_dir, tf_results, price, buy_total, sell_total = aggregate_and_decide(symbol, tf_list, settings_local)
    total_votes = max(buy_total, sell_total)
    votes_per_tf = [ (tf_results[tf]['vote'] if tf_results[tf]['vote'] else 'NONE') for tf in tf_list ]
    non_none = [v for v in votes_per_tf if v != 'NONE']
    agree = len(non_none) > 0 and len(set(non_none)) == 1
    if agg_dir and agree and total_votes >= settings_local.get('min_votes_for_ultra', 3):
        plan = suggest_trade_plan(price, agg_dir, total_votes)
        msg = format_ultra_message(symbol, agg_dir, tf_results, plan, buy_total, sell_total)
        bot.send_message(m.chat.id, msg, parse_mode='Markdown', reply_markup=build_main_keyboard())
    else:
        bot.send_message(m.chat.id, f"No Ultra signal for {symbol} now. (Votes: {buy_total}/{sell_total})", reply_markup=build_main_keyboard())

# Inline callback for remove coin and settings buttons
@bot.callback_query_handler(func=lambda call: True)
def inline_cb(call):
    try:
        data_cb = call.data
        chat_id = call.message.chat.id

        if data_cb.startswith("remove::"):
            coin = data_cb.split("::",1)[1]
            d = load_data()
            mc = d.get('my_coins', [])
            if coin in mc:
                mc.remove(coin)
                d['my_coins'] = mc
                save_data(d)
                bot.edit_message_text(f"Removed {coin}.", chat_id, call.message.message_id, reply_markup=None)
            else:
                bot.answer_callback_query(call.id, "Coin not found.")

        elif data_cb == "settings_menu":
            send_settings_menu(chat_id)

        elif data_cb == "set_interval":
            markup = types.InlineKeyboardMarkup()
            for key in ["5m","15m","1h","4h","1d"]:
                markup.add(types.InlineKeyboardButton(key, callback_data=f"set_interval::{key}"))
            bot.send_message(chat_id, "Choose auto interval:", reply_markup=markup)

        elif data_cb.startswith("set_interval::"):
            key = data_cb.split("::",1)[1]
            d = load_data()
            d['settings']['auto_interval'] = key
            save_data(d)
            schedule_auto_job()
            bot.edit_message_text(f"Auto interval set to {key}.", chat_id, call.message.message_id, reply_markup=None)

        elif data_cb == "set_auto_mode":
            markup = types.InlineKeyboardMarkup()
            for m in ["my","all","both"]:
                markup.add(types.InlineKeyboardButton(m, callback_data=f"set_auto_mode::{m}"))
            bot.send_message(chat_id, "Choose auto mode:", reply_markup=markup)

        elif data_cb.startswith("set_auto_mode::"):
            mode = data_cb.split("::",1)[1]
            d = load_data()
            d['settings']['auto_mode'] = mode
            save_data(d)
            bot.edit_message_text(f"Auto mode set to {mode}.", chat_id, call.message.message_id, reply_markup=None)

        elif data_cb == "edit_rsi":
            msg = bot.send_message(chat_id, "Send two integers separated by space: <oversold> <overbought> (e.g. 28 72)")
            bot.register_next_step_handler(msg, handle_rsi_edit)

        elif data_cb == "set_min_votes":
            msg = bot.send_message(chat_id, "Send min votes for ultra (integer):")
            bot.register_next_step_handler(msg, handle_min_votes)

        elif data_cb == "settings_back":
            bot.send_message(chat_id, "Back to main menu.", reply_markup=build_main_keyboard())

        else:
            bot.answer_callback_query(call.id, "Unknown action")
    except Exception:
        traceback.print_exc()
        try:
            bot.send_message(chat_id, "Error handling action.")
        except Exception:
            pass

def send_settings_menu(chat_id):
    d = load_data()
    s = d['settings']
    md = types.InlineKeyboardMarkup()
    md.row(types.InlineKeyboardButton(f"RSI {s['rsi_oversold']}/{s['rsi_overbought']}", callback_data="edit_rsi"),
           types.InlineKeyboardButton(f"Min votes {s['min_votes_for_ultra']}", callback_data="set_min_votes"))
    md.row(types.InlineKeyboardButton(f"Auto interval: {s['auto_interval']}", callback_data="set_interval"),
           types.InlineKeyboardButton(f"Auto mode: {s['auto_mode']}", callback_data="set_auto_mode"))
    md.row(types.InlineKeyboardButton("Back", callback_data="settings_back"))
    bot.send_message(chat_id, "Signal Settings:", reply_markup=md)

def handle_rsi_edit(m):
    try:
        parts = m.text.strip().split()
        if len(parts) != 2:
            raise ValueError("expected 2 ints")
        a = int(parts[0]); b = int(parts[1])
        d = load_data()
        d['settings']['rsi_oversold'] = a
        d['settings']['rsi_overbought'] = b
        save_data(d)
        bot.send_message(m.chat.id, f"RSI thresholds set to {a}/{b}.", reply_markup=build_main_keyboard())
    except Exception:
        bot.send_message(m.chat.id, "Invalid input. Send two integers like: 28 72", reply_markup=build_main_keyboard())

def handle_min_votes(m):
    try:
        v = int(m.text.strip())
        d = load_data()
        d['settings']['min_votes_for_ultra'] = v
        save_data(d)
        bot.send_message(m.chat.id, f"Min votes set to {v}.", reply_markup=build_main_keyboard())
    except Exception:
        bot.send_message(m.chat.id, "Invalid integer.", reply_markup=build_main_keyboard())

# ------------------------
# On-demand sending helper (runs threaded)
# ------------------------
def send_signals_for_list(chat_id, symbols):
    settings_local = load_data()['settings']
    tf_list = settings_local.get('timeframes', ['5m','1h','1d'])
    min_votes = settings_local.get('min_votes_for_ultra', 3)
    for sym in symbols:
        try:
            agg_dir, tf_results, price, buy_total, sell_total = aggregate_and_decide(sym, tf_list, settings_local)
            total_votes = max(buy_total, sell_total)
            votes_per_tf = [ (tf_results[tf]['vote'] if tf_results[tf]['vote'] else 'NONE') for tf in tf_list ]
            non_none = [v for v in votes_per_tf if v != 'NONE']
            agree = len(non_none) > 0 and len(set(non_none)) == 1
            if agg_dir and agree and total_votes >= max(min_votes, settings_local.get('ultra_min_score', 4)):
                plan = suggest_trade_plan(price, agg_dir, total_votes)
                msg = format_ultra_message(sym, agg_dir, tf_results, plan, buy_total, sell_total)
                bot.send_message(chat_id, msg, parse_mode='Markdown')
        except Exception:
            print("Error in on-demand scan for", sym)
            traceback.print_exc()

# ------------------------
# Startup webhook and schedule
# ------------------------
@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def telegram_webhook():
    try:
        json_str = request.get_data().decode("utf-8")
        if not json_str:
            return "", 400
        update = telebot.types.Update.de_json(json_str)
        bot.process_new_updates([update])
    except Exception:
        traceback.print_exc()
    return "", 200

@app.route("/", methods=["GET"])
def root():
    return "Ultra Pro Signals Bot is running."

def set_webhook():
    try:
        bot.remove_webhook()
    except Exception:
        pass
    bot.set_webhook(url=f"{WEBHOOK_URL}/{TELEGRAM_TOKEN}")
    print("Webhook set to:", f"{WEBHOOK_URL}/{TELEGRAM_TOKEN}")

if __name__ == "__main__":
    # set webhook and schedule job as per saved config
    set_webhook()
    # schedule job based on stored interval
    schedule_auto_job()
    # flask will be run by Render (or run locally)
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)




