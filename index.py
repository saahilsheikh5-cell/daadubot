import os
import json
import time
import threading
import logging
from typing import Dict, List, Tuple

import requests
import telebot
from telebot import types
from flask import Flask, request
from tradingview_ta import TA_Handler, Interval

# =========================
# CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is not set in environment variables")

RENDER_BASE = "https://daadubot.onrender.com"
WEBHOOK_URL = f"{RENDER_BASE}/{BOT_TOKEN}"

# Flask app (Gunicorn expects `index:app`)
app = Flask(__name__)

# Telegram bot
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# Logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("daadubot")

# =========================
# STORAGE (JSON FILES)
# =========================
USER_COINS_FILE = "user_coins.json"        # { "<chat_id>": ["BTCUSDT","ETHUSDT"] }
AUTO_FLAGS_FILE = "auto_flags.json"        # { "<chat_id>": true/false }
LAST_RECS_FILE  = "last_recs.json"         # { "<chat_id>|<coin>|<tf>": "BUY"/"SELL"/... }

def load_json(path: str, default):
    try:
        if not os.path.exists(path):
            with open(path, "w") as f:
                json.dump(default, f)
            return default
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path: str, data):
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log.error("Failed saving %s: %s", path, e)

user_coins: Dict[str, List[str]] = load_json(USER_COINS_FILE, {})
auto_flags: Dict[str, bool]       = load_json(AUTO_FLAGS_FILE, {})
last_recs: Dict[str, str]         = load_json(LAST_RECS_FILE, {})

# =========================
# BINANCE HELPERS
# =========================
BINANCE_API = "https://api.binance.com"

def binance_price(symbol: str) -> float:
    try:
        r = requests.get(f"{BINANCE_API}/api/v3/ticker/price", params={"symbol": symbol}, timeout=10)
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception:
        return 0.0

def binance_movers(limit: int = 100) -> List[dict]:
    """Top movers among USDT pairs (24h) limited to top `limit` by volume."""
    try:
        tickers = requests.get(f"{BINANCE_API}/api/v3/ticker/24hr", timeout=15).json()
        usdt = [t for t in tickers if t.get("symbol","").endswith("USDT")]
        # Sort by quoteVolume (desc) to get liquid/top coins, then take first `limit`
        liquid = sorted(usdt, key=lambda x: float(x.get("quoteVolume", "0") or 0.0), reverse=True)[:limit]
        # Now sort those by % change to show biggest gainers
        movers_sorted = sorted(liquid, key=lambda x: float(x.get("priceChangePercent","0") or 0.0), reverse=True)
        return movers_sorted
    except Exception:
        return []

# =========================
# TRADINGVIEW TA HELPERS
# =========================
TF_MAP = {
    "1m": Interval.INTERVAL_1_MINUTE,
    "5m": Interval.INTERVAL_5_MINUTES,
    "15m": Interval.INTERVAL_15_MINUTES,
    "1h": Interval.INTERVAL_1_HOUR,
    "4h": Interval.INTERVAL_4_HOURS,
    "1d": Interval.INTERVAL_1_DAY,
}

DEFAULT_TFS = ["1m","5m","15m","1h","4h","1d"]

def get_tv_analysis(symbol: str, tf: str) -> Tuple[dict, dict]:
    """Return (summary, indicators) from TradingView. summary['RECOMMENDATION'] in {STRONG_BUY, BUY, NEUTRAL, SELL, STRONG_SELL}"""
    try:
        handler = TA_Handler(
            symbol=symbol,
            screener="crypto",
            exchange="BINANCE",
            interval=TF_MAP[tf]
        )
        analysis = handler.get_analysis()
        return analysis.summary, analysis.indicators
    except Exception as e:
        return {"RECOMMENDATION":"ERROR","BUY":0,"SELL":0,"NEUTRAL":0}, {"error": str(e)}

def format_single_ta(symbol: str, tf: str, summary: dict, indicators: dict) -> str:
    rec = summary.get("RECOMMENDATION","N/A")
    b = summary.get("BUY",0); s = summary.get("SELL",0); n = summary.get("NEUTRAL",0)
    price = binance_price(symbol) or indicators.get("close", 0)
    # Simple protective levels around current price
    sl = price * 0.98 if price else 0
    tp = price * 1.02 if price else 0

    lines = [
        f"<b>📊 {symbol} | {tf}</b>",
        f"Signal: <b>{rec}</b>  (B:{b} / S:{s} / N:{n})",
    ]
    # Helpful indicators if available
    for k in ["RSI","RSI[1]","MACD.macd","MACD.signal","EMA10","EMA20","EMA50","SMA20","SMA50","close"]:
        if k in indicators:
            val = indicators[k]
            try:
                val = round(float(val), 4)
            except Exception:
                pass
            lines.append(f"{k}: {val}")
    if price:
        lines += [
            f"Entry: <b>{round(price, 6)}</b>",
            f"SL: {round(sl, 6)}",
            f"TP1: {round(tp, 6)}",
        ]
    return "\n".join(lines)

def format_multi_ta(symbol: str, tfs: List[str]) -> str:
    parts = []
    for tf in tfs:
        summary, indicators = get_tv_analysis(symbol, tf)
        parts.append(format_single_ta(symbol, tf, summary, indicators))
    return "\n\n".join(parts)

# =========================
# AUTO SIGNAL SCANNER
# =========================
def auto_scan_loop():
    """Background loop scanning all users who enabled auto signals."""
    while True:
        try:
            for chat_id_str, enabled in list(auto_flags.items()):
                if not enabled:
                    continue
                coins = user_coins.get(chat_id_str, [])
                if not coins:
                    continue
                for coin in coins:
                    for tf in ["15m","1h","4h"]:
                        summary, _ = get_tv_analysis(coin, tf)
                        rec = summary.get("RECOMMENDATION","")
                        key = f"{chat_id_str}|{coin}|{tf}"
                        if last_recs.get(key) != rec and rec not in ("", "ERROR", None):
                            last_recs[key] = rec
                            save_json(LAST_RECS_FILE, last_recs)
                            bot.send_message(int(chat_id_str), f"⚡ <b>Signal update</b>\n{format_single_ta(coin, tf, summary, {})}")
                        time.sleep(0.4)  # be gentle with TV
                time.sleep(1)
        except Exception as e:
            log.warning("Auto scan loop error: %s", e)
        time.sleep(15)

threading.Thread(target=auto_scan_loop, daemon=True).start()

# =========================
# KEYBOARDS
# =========================
def main_menu_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("➕ Add Coin", "📂 My Coins")
    kb.row("➖ Remove Coin", "🔥 Movers")
    kb.row("📈 Signals", "⚡ Start Auto", "🛑 Stop Auto")
    kb.row("ℹ️ Help")
    return kb

def timeframes_kb(symbol: str):
    ik = types.InlineKeyboardMarkup()
    row1 = [types.InlineKeyboardButton(tf, callback_data=f"tf|{symbol}|{tf}") for tf in ["1m","5m","15m"]]
    row2 = [types.InlineKeyboardButton(tf, callback_data=f"tf|{symbol}|{tf}") for tf in ["1h","4h","1d"]]
    ik.row(*row1)
    ik.row(*row2)
    ik.add(types.InlineKeyboardButton("🧩 ALL", callback_data=f"tf|{symbol}|ALL"))
    return ik

# =========================
# BOT HANDLERS
# =========================
@bot.message_handler(commands=["start"])
def cmd_start(msg):
    chat_id = str(msg.chat.id)
    if chat_id not in user_coins:
        user_coins[chat_id] = ["BTCUSDT","ETHUSDT","SOLUSDT"]  # sensible defaults
        save_json(USER_COINS_FILE, user_coins)
    bot.send_message(msg.chat.id, "👋 <b>Welcome to DaaduBot</b>\nChoose an option:", reply_markup=main_menu_kb())

@bot.message_handler(commands=["help"])
def cmd_help(msg):
    bot.send_message(msg.chat.id,
        "ℹ️ <b>Help</b>\n"
        "• Add coins you want to track (e.g., BTCUSDT)\n"
        "• My Coins → pick a coin → choose timeframe to get TA\n"
        "• Movers → Top 100 liquid Binance USDT pairs by 24h change\n"
        "• Start Auto → background alerts on 15m/1h/4h when signal changes\n"
        "• Stop Auto → stop alerts\n",
        reply_markup=main_menu_kb()
    )

@bot.message_handler(func=lambda m: m.text == "ℹ️ Help")
def help_btn(msg):
    cmd_help(msg)

@bot.message_handler(func=lambda m: m.text == "➕ Add Coin")
def add_coin(msg):
    bot.send_message(msg.chat.id, "Send coin symbol (e.g., <b>BTCUSDT</b>):")
    bot.register_next_step_handler(msg, save_coin_step)

def save_coin_step(msg):
    sym = (msg.text or "").strip().upper()
    if not sym.endswith("USDT") or len(sym) < 6:
        bot.send_message(msg.chat.id, "❌ Please send a valid Binance symbol like <b>BTCUSDT</b>.")
        return
    chat_id = str(msg.chat.id)
    user_coins.setdefault(chat_id, [])
    if sym not in user_coins[chat_id]:
        user_coins[chat_id].append(sym)
        save_json(USER_COINS_FILE, user_coins)
        bot.send_message(msg.chat.id, f"✅ <b>{sym}</b> added.", reply_markup=main_menu_kb())
    else:
        bot.send_message(msg.chat.id, f"⚠️ <b>{sym}</b> is already in your list.", reply_markup=main_menu_kb())

@bot.message_handler(func=lambda m: m.text == "➖ Remove Coin")
def remove_coin(msg):
    chat_id = str(msg.chat.id)
    coins = user_coins.get(chat_id, [])
    if not coins:
        bot.send_message(msg.chat.id, "No coins to remove.", reply_markup=main_menu_kb())
        return
    ik = types.InlineKeyboardMarkup()
    for c in coins:
        ik.add(types.InlineKeyboardButton(f"Remove {c}", callback_data=f"rm|{c}"))
    bot.send_message(msg.chat.id, "Select a coin to remove:", reply_markup=ik)

@bot.callback_query_handler(func=lambda c: c.data.startswith("rm|"))
def cb_remove(call):
    _, sym = call.data.split("|", 1)
    chat_id = str(call.message.chat.id)
    if sym in user_coins.get(chat_id, []):
        user_coins[chat_id].remove(sym)
        save_json(USER_COINS_FILE, user_coins)
        bot.answer_callback_query(call.id, f"Removed {sym}")
        bot.edit_message_text("✅ Removed.\n(Open menu again if needed.)", call.message.chat.id, call.message.id)
    else:
        bot.answer_callback_query(call.id, "Already removed")

@bot.message_handler(func=lambda m: m.text == "📂 My Coins")
def my_coins(msg):
    chat_id = str(msg.chat.id)
    coins = user_coins.get(chat_id, [])
    if not coins:
        bot.send_message(msg.chat.id, "No coins yet. Use <b>➕ Add Coin</b>.", reply_markup=main_menu_kb())
        return
    ik = types.InlineKeyboardMarkup()
    for c in coins:
        ik.add(types.InlineKeyboardButton(c, callback_data=f"coin|{c}"))
    bot.send_message(msg.chat.id, "📂 Your coins:", reply_markup=ik)

@bot.callback_query_handler(func=lambda c: c.data.startswith("coin|"))
def cb_coin(call):
    _, sym = call.data.split("|", 1)
    bot.edit_message_text(f"⏱ Choose timeframe for <b>{sym}</b>:", call.message.chat.id, call.message.id, reply_markup=timeframes_kb(sym))

@bot.callback_query_handler(func=lambda c: c.data.startswith("tf|"))
def cb_tf(call):
    _, sym, tf = call.data.split("|", 2)
    if tf == "ALL":
        text = format_multi_ta(sym, DEFAULT_TFS)
        bot.edit_message_text(text, call.message.chat.id, call.message.id)
    else:
        summary, indicators = get_tv_analysis(sym, tf)
        text = format_single_ta(sym, tf, summary, indicators)
        bot.edit_message_text(text, call.message.chat.id, call.message.id)

@bot.message_handler(func=lambda m: m.text == "📈 Signals")
def signals_prompt(msg):
    bot.send_message(msg.chat.id, "Send a symbol (e.g., <b>BTCUSDT</b>) to get full multi-TF analysis.")
    bot.register_next_step_handler(msg, signals_run)

def signals_run(msg):
    sym = (msg.text or "").strip().upper()
    if not sym.endswith("USDT"):
        bot.send_message(msg.chat.id, "❌ Send a valid Binance symbol like <b>BTCUSDT</b>.")
        return
    bot.send_message(msg.chat.id, format_multi_ta(sym, DEFAULT_TFS), reply_markup=main_menu_kb())

@bot.message_handler(func=lambda m: m.text == "🔥 Movers")
def movers(msg):
    top = binance_movers(limit=100)[:10]  # show top 10 movers out of top 100 by liquidity
    if not top:
        bot.send_message(msg.chat.id, "Could not fetch movers right now.")
        return
    lines = ["🔥 <b>Top Movers (24h)</b> among top 100 liquid USDT pairs:\n"]
    for t in top:
        sym = t.get("symbol","?")
        chg = t.get("priceChangePercent","0")
        last = t.get("lastPrice","0")
        lines.append(f"{sym}: {chg}%  •  Last: {last}")
    bot.send_message(msg.chat.id, "\n".join(lines), reply_markup=main_menu_kb())

@bot.message_handler(func=lambda m: m.text == "⚡ Start Auto")
def start_auto(msg):
    chat_id = str(msg.chat.id)
    auto_flags[chat_id] = True
    save_json(AUTO_FLAGS_FILE, auto_flags)
    bot.send_message(msg.chat.id, "⚡ Auto alerts enabled (15m/1h/4h).", reply_markup=main_menu_kb())

@bot.message_handler(func=lambda m: m.text == "🛑 Stop Auto")
def stop_auto(msg):
    chat_id = str(msg.chat.id)
    auto_flags[chat_id] = False
    save_json(AUTO_FLAGS_FILE, auto_flags)
    bot.send_message(msg.chat.id, "🛑 Auto alerts disabled.", reply_markup=main_menu_kb())

# =========================
# FLASK ROUTES (WEBHOOK + HEALTH)
# =========================
@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def tg_webhook():
    try:
        update = telebot.types.Update.de_json(request.data.decode("utf-8"))
        bot.process_new_updates([update])
    except Exception as e:
        log.error("Webhook error: %s", e)
    return "OK", 200

@app.route("/health", methods=["GET"])
def health():
    return "Bot is alive ✅", 200

@app.route("/", methods=["GET"])
def root():
    return "DaaduBot is running. Set webhook at /<BOT_TOKEN>.", 200

# =========================
# WEBHOOK INIT & APP ENTRY
# =========================
def ensure_webhook():
    try:
        bot.remove_webhook()
        resp = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook", params={"url": WEBHOOK_URL}, timeout=10)
        log.info("Webhook set response: %s", resp.text)
    except Exception as e:
        log.error("Failed to set webhook: %s", e)

# Set webhook on import (safe for Render) and run app via Gunicorn
ensure_webhook()

if __name__ == "__main__":
    # For local debug runs (Render uses gunicorn)
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
