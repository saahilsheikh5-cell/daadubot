port os, json, time, logging, threading, traceback
from typing import Dict, Any, List, Tuple
import requests
import numpy as np
import pandas as pd
from flask import Flask, request
import telebot
from telebot import types

# ========= LOGGING =========
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("index")

# ========= CONFIG =========
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is not set!")

PUBLIC_URL = os.getenv("PUBLIC_URL", "https://daadubot.onrender.com")
WEBHOOK_URL_PATH = "/webhook"

ENABLE_SCANNER = os.getenv("ENABLE_SCANNER", "1") == "1"
TOP_ALL_LIMIT = int(os.getenv("TOP_ALL_LIMIT", "50"))  # For "All coins"

# Binance Futures endpoints
BINANCE_FAPI = "https://fapi.binance.com"
KLINES_EP = BINANCE_FAPI + "/fapi/v1/klines"
TICKER_24H_EP = BINANCE_FAPI + "/fapi/v1/ticker/24hr"

# ========= TELEGRAM =========
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")
app = Flask(__name__)

# ========= STORAGE (JSON persistence) =========
COINS_FILE = "coins.json"            # { chat_id: ["BTCUSDT", ...], ... }
SETTINGS_FILE = "settings.json"      # { chat_id: {...}, ... }
STATE_FILE = "state.json"            # { chat_id: {state:..., temp: {...}} }
SUB_FILE = "subscriptions.json"      # { chat_id: {active:bool, mode:"my/all/one", intervals:[], coin:""}, ... }
LAST_SIG_FILE = "last_signals.json"  # { "<chat_id>|<sym>|<tf>|<side>": ts, ... }

DEFAULT_SETTINGS = {
    "rsi_buy": 30,           # RSI < buy -> bullish bias
    "rsi_sell": 70,          # RSI > sell -> bearish bias
    "signal_validity_min": 15,
    "leverage_cap": 20       # Suggested max leverage
}

def load_json(path: str, default):
    try:
        if not os.path.exists(path):
            return default
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        log.error("Failed to load %s", path)
        return default

def save_json(path: str, data):
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        log.error("Failed to save %s", path)

coins_db: Dict[str, List[str]] = load_json(COINS_FILE, {})
settings_db: Dict[str, Dict[str, Any]] = load_json(SETTINGS_FILE, {})
state_db: Dict[str, Dict[str, Any]] = load_json(STATE_FILE, {})
subs_db: Dict[str, Dict[str, Any]] = load_json(SUB_FILE, {})
last_sig: Dict[str, float] = load_json(LAST_SIG_FILE, {})

def get_settings(chat_id: str) -> Dict[str, Any]:
    if chat_id not in settings_db:
        settings_db[chat_id] = DEFAULT_SETTINGS.copy()
        save_json(SETTINGS_FILE, settings_db)
    return settings_db[chat_id]

def set_state(chat_id: str, state: str = None, **temp):
    state_db[chat_id] = state_db.get(chat_id, {})
    state_db[chat_id]["state"] = state
    state_db[chat_id]["temp"] = temp
    save_json(STATE_FILE, state_db)

def get_state(chat_id: str) -> Tuple[str, Dict[str, Any]]:
    s = state_db.get(chat_id, {})
    return s.get("state"), s.get("temp", {})

def add_coin(chat_id: str, symbol: str) -> str:
    symbol = symbol.upper().strip()
    coins_db.setdefault(chat_id, [])
    if symbol in coins_db[chat_id]:
        return f"ℹ️ {symbol} already in your watchlist."
    # Quick validate by probing klines
    ok, _ = get_klines(symbol, "1m", 5)
    if not ok:
        return f"❌ {symbol} not found on Binance Futures."
    coins_db[chat_id].append(symbol)
    save_json(COINS_FILE, coins_db)
    return f"✅ {symbol} added to watchlist."

def remove_coin(chat_id: str, symbol: str) -> str:
    symbol = symbol.upper().strip()
    lst = coins_db.get(chat_id, [])
    if symbol in lst:
        lst.remove(symbol)
        save_json(COINS_FILE, coins_db)
        return f"✅ {symbol} removed."
    return "❌ Coin not in your watchlist."

# ========= UI: Keyboards =========
def main_menu_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("➕ Add Coin", "📊 My Coins")
    kb.row("➖ Remove Coin", "📈 Top Movers")
    kb.row("📡 Signals", "🛑 Stop Signals")
    kb.row("🔄 Reset Settings", "⚙️ Signal Settings")
    kb.row("🔍 Preview Signal")
    return kb

def back_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("🔙 Back")
    return kb

def timeframes_kb(include_back=True):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("1m", "5m", "15m")
    kb.row("1h")
    if include_back:
        kb.row("🔙 Back")
    return kb

def top_movers_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("5m Movers", "1h Movers")
    kb.row("24h Movers")
    kb.row("🔙 Back")
    return kb

def signals_main_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("a. My coins", "b. All coins")
    kb.row("c. Any particular coin")
    kb.row("🔙 Back")
    return kb

def stop_signals_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("Stop: My coins", "Stop: All coins")
    kb.row("Stop: Particular coin")
    kb.row("🔙 Back")
    return kb

def settings_kb():
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("Set RSI Buy", "Set RSI Sell")
    kb.row("Set Validity (min)", "Set Leverage Cap")
    kb.row("🔙 Back")
    return kb

# ========= Binance Futures helpers =========
def get_klines(symbol: str, interval: str, limit: int = 200) -> Tuple[bool, pd.DataFrame]:
    try:
        resp = requests.get(KLINES_EP, params={"symbol": symbol, "interval": interval, "limit": limit}, timeout=10)
        data = resp.json()
        if not isinstance(data, list) or len(data) == 0:
            return False, pd.DataFrame()
        df = pd.DataFrame(data, columns=[
            "open_time","open","high","low","close","volume","close_time","quote_asset_volume",
            "trades","taker_base_vol","taker_quote_vol","ignore"
        ])
        df["open"] = df["open"].astype(float)
        df["high"] = df["high"].astype(float)
        df["low"] = df["low"].astype(float)
        df["close"] = df["close"].astype(float)
        df["volume"] = df["volume"].astype(float)
        return True, df
    except Exception:
        log.error("get_klines error:\n%s", traceback.format_exc())
        return False, pd.DataFrame()

def ticker_24h_all() -> List[Dict[str, Any]]:
    try:
        r = requests.get(TICKER_24H_EP, timeout=10)
        data = r.json()
        if isinstance(data, list):
            # Only USDT-M perpetual symbols typically end with USDT
            return [d for d in data if d.get("symbol","").endswith("USDT")]
        return []
    except Exception:
        log.error("ticker_24h_all error:\n%s", traceback.format_exc())
        return []

# ========= TA: indicators =========
def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    ma_up = up.rolling(window=period).mean()
    ma_down = down.rolling(window=period).mean()
    rs = ma_up / (ma_down.replace(0, np.nan))
    out = 100 - (100 / (1 + rs))
    return out.fillna(50)

def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()

def macd(series: pd.Series, fast=12, slow=26, signal=9) -> Tuple[pd.Series, pd.Series]:
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line, signal_line

def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    # Use high/low/close TR method; here we have only HL/prev close approximated
    high = df["high"]; low = df["low"]; close = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low),
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def analyze_coin(symbol: str, interval: str) -> Tuple[bool, str, Dict[str, Any]]:
    ok, df = get_klines(symbol, interval, 300)
    if not ok or df.empty:
        return False, f"❌ Failed to fetch data for {symbol} | {interval}", {}
    close = df["close"]
    vol = df["volume"]
    r = rsi(close)
    e20 = ema(close, 20)
    e50 = ema(close, 50)
    m, s = macd(close)
    a = atr(df, 14)
    last = int(df.index[-1])

    price = close.iloc[-1]
    rsi_v = round(float(r.iloc[-1]), 2)
    ema20 = float(e20.iloc[-1]); ema50 = float(e50.iloc[-1])
    macd_v = float(m.iloc[-1]); sig_v = float(s.iloc[-1])
    atr_v = float(a.iloc[-1]) if not np.isnan(a.iloc[-1]) else float(np.nan)
    vol_v = float(vol.iloc[-1])
    bias_ema = "Bullish" if ema20 > ema50 else "Bearish" if ema20 < ema50 else "Flat"
    bias_macd = "Bullish" if macd_v > sig_v else "Bearish" if macd_v < sig_v else "Flat"

    txt = (
        f"📊 <b>{symbol}</b> | <b>{interval}</b>\n"
        f"Price: <b>{price:.4f}</b>\n"
        f"RSI(14): <b>{rsi_v}</b>\n"
        f"EMA20/50: <b>{bias_ema}</b>  (20={ema20:.4f}, 50={ema50:.4f})\n"
        f"MACD: <b>{bias_macd}</b>  (MACD={macd_v:.5f}, Signal={sig_v:.5f})\n"
        f"ATR(14): <b>{atr_v:.6f}</b>\n"
        f"Vol(last): <b>{vol_v:.2f}</b>"
    )
    info = {
        "price": price, "rsi": rsi_v, "ema20": ema20, "ema50": ema50,
        "macd": macd_v, "macd_sig": sig_v, "atr": atr_v, "vol": vol_v
    }
    return True, txt, info

def leverage_suggestion(atr_value: float, price: float, cap: int) -> int:
    if atr_value is None or np.isnan(atr_value) or price <= 0:
        return max(1, min(cap, 10))
    vol_pct = atr_value / price  # rough
    if vol_pct <= 0:
        return max(1, min(cap, 10))
    # inverse volatility scaling
    lev = int(min(cap, max(1, round(0.5 / vol_pct))))
    return lev

def generate_signal(symbol: str, interval: str, st: Dict[str, Any]) -> str or None:
    ok, df = get_klines(symbol, interval, 300)
    if not ok or df.empty:
        return None
    close = df["close"]
    vol = df["volume"]
    r = rsi(close)
    e20 = ema(close, 20)
    e50 = ema(close, 50)
    m, s = macd(close)
    a = atr(df, 14)

    price = close.iloc[-1]
    rsi_v = float(r.iloc[-1])
    macd_v = float(m.iloc[-1]); sig_v = float(s.iloc[-1])
    ema20 = float(e20.iloc[-1]); ema50 = float(e50.iloc[-1])
    atr_v = float(a.iloc[-1]) if not np.isnan(a.iloc[-1]) else None

    # Basic rule-set (futures bias)
    # Strong Buy: RSI < rsi_buy AND MACD>Signal AND EMA20>EMA50
    # Strong Sell: RSI > rsi_sell AND MACD<Signal AND EMA20<EMA50
    side = None
    label = None
    if (rsi_v < st["rsi_buy"]) and (macd_v > sig_v) and (ema20 > ema50):
        side = "BUY"; label = "🟢 Strong BUY"
    elif (rsi_v > st["rsi_sell"]) and (macd_v < sig_v) and (ema20 < ema50):
        side = "SELL"; label = "🔴 Strong SELL"
    else:
        return None

    # Targets from ATR
    atr = atr_v if atr_v and atr_v > 0 else price * 0.005
    entry = price
    if side == "BUY":
        sl = entry - atr
        tp1 = entry + 1.5 * atr
        tp2 = entry + 3.0 * atr
    else:
        sl = entry + atr
        tp1 = entry - 1.5 * atr
        tp2 = entry - 3.0 * atr

    lev = leverage_suggestion(atr_v, price, st.get("leverage_cap", 20))
    msg = (
        f"{label} | {symbol} | {interval}\n"
        f"Entry: <b>{entry:.6f}</b>\nSL: <b>{sl:.6f}</b>\n"
        f"TP1: <b>{tp1:.6f}</b>\nTP2: <b>{tp2:.6f}</b>\n"
        f"RSI: <b>{rsi_v:.2f}</b> | EMA20/50: <b>{'Bull' if ema20>ema50 else 'Bear'}</b> | MACD: <b>{'Bull' if macd_v>sig_v else 'Bear'}</b>\n"
        f"Leverage suggestion: <b>{lev}x</b>"
    )
    return msg

# ========= Movers =========
def movers_24h(limit=5) -> Tuple[List[Tuple[str,float]], List[Tuple[str,float]]]:
    arr = ticker_24h_all()
    # Use priceChangePercent from 24h stats
    good = []
    for d in arr:
        try:
            sym = d["symbol"]
            chg = float(d.get("priceChangePercent", 0.0))
            # filter out odd pairs (ensure USDT)
            if sym.endswith("USDT"):
                good.append((sym, chg))
        except:
            pass
    good.sort(key=lambda x: x[1], reverse=True)
    top = good[:limit]
    bot = good[-limit:][::-1]
    return top, bot

def movers_period(tf_label: str, limit=5) -> Tuple[List[Tuple[str,float]], List[Tuple[str,float]]]:
    # For 5m: use 5m klines last candle (close - open)/open
    # For 1h: use 1h klines last candle
    interval = "5m" if tf_label == "5m" else "1h"
    arr = ticker_24h_all()
    # Pick top N by quote volume to limit symbols
    arr.sort(key=lambda d: float(d.get("quoteVolume", 0.0)), reverse=True)
    syms = [d["symbol"] for d in arr if d["symbol"].endswith("USDT")][:TOP_ALL_LIMIT]
    changes = []
    for sym in syms:
        ok, df = get_klines(sym, interval, 2)
        if not ok or df.empty:
            continue
        # last candle change %
        o = float(df["open"].iloc[-1]); c = float(df["close"].iloc[-1])
        if o > 0:
            pct = (c - o) * 100.0 / o
            changes.append((sym, pct))
    changes.sort(key=lambda x: x[1], reverse=True)
    top = changes[:limit]
    bot = changes[-limit:][::-1]
    return top, bot

# ========= Subscriptions & Scanner =========
def start_subscription(chat_id: str, mode: str, interval: str, coin: str = "") -> str:
    subs_db[chat_id] = subs_db.get(chat_id, {})
    subs_db[chat_id]["active"] = True
    subs_db[chat_id]["mode"] = mode  # "my" | "all" | "one"
    subs_db[chat_id]["interval"] = interval
    subs_db[chat_id]["coin"] = coin.upper() if coin else ""
    save_json(SUB_FILE, subs_db)
    target = "your coins" if mode == "my" else ("top coins" if mode == "all" else coin.upper())
    return f"✅ Started signals for <b>{target}</b> at <b>{interval}</b>."

def stop_subscription(chat_id: str, mode: str, coin: str = "") -> str:
    if chat_id not in subs_db:
        return "ℹ️ No active subscriptions."
    sub = subs_db[chat_id]
    if mode == "my" and sub.get("mode") == "my":
        sub["active"] = False
    elif mode == "all" and sub.get("mode") == "all":
        sub["active"] = False
    elif mode == "one" and sub.get("mode") == "one" and (not coin or sub.get("coin","").upper() == coin.upper()):
        sub["active"] = False
    else:
        return "ℹ️ No matching active subscription."
    save_json(SUB_FILE, subs_db)
    return "🛑 Stopped."

def scanner_thread():
    log.info("Scanner thread starting...")
    while True:
        try:
            # iterate active subs
            for chat_id, sub in list(subs_db.items()):
                if not sub.get("active"):
                    continue
                mode = sub.get("mode")
                interval = sub.get("interval", "15m")
                st = get_settings(chat_id)

                # Build symbol list
                symbols = []
                if mode == "my":
                    symbols = coins_db.get(chat_id, [])[:]
                elif mode == "all":
                    arr = ticker_24h_all()
                    arr.sort(key=lambda d: float(d.get("quoteVolume", 0.0)), reverse=True)
                    symbols = [d["symbol"] for d in arr if d["symbol"].endswith("USDT")][:TOP_ALL_LIMIT]
                elif mode == "one":
                    c = sub.get("coin", "").upper()
                    if c:
                        symbols = [c]

                # Scan and send fresh signals
                for sym in symbols:
                    msg = generate_signal(sym, interval, st)
                    if not msg:
                        continue
                    key = f"{chat_id}|{sym}|{interval}|{('BUY' if 'BUY' in msg else 'SELL')}"
                    now = time.time()
                    if (key not in last_sig) or (now - last_sig[key] > st["signal_validity_min"] * 60):
                        try:
                            bot.send_message(int(chat_id), f"⚡ {msg}")
                            last_sig[key] = now
                            save_json(LAST_SIG_FILE, last_sig)
                            time.sleep(0.3)  # gentle rate limit
                        except Exception:
                            log.error("Failed to send signal:\n%s", traceback.format_exc())
                time.sleep(0.5)
        except Exception:
            log.error("scanner loop error:\n%s", traceback.format_exc())
        time.sleep(10)

def maybe_start_scanner_once():
    try:
        if not ENABLE_SCANNER:
            log.info("Scanner disabled by env.")
            return
        lock_path = "/tmp/scanner.lock"
        if os.path.exists(lock_path):
            # Another worker holds it. If stale (>10min), take over.
            if time.time() - os.path.getmtime(lock_path) > 600:
                os.remove(lock_path)
        if not os.path.exists(lock_path):
            with open(lock_path, "w") as f:
                f.write(str(os.getpid()))
            t = threading.Thread(target=scanner_thread, daemon=True)
            t.start()
            log.info("Scanner started in pid=%s", os.getpid())
        else:
            log.info("Scanner already running in another worker.")
    except Exception:
        log.error("Failed to start scanner:\n%s", traceback.format_exc())

# ========= Menu Handlers (message router) =========
def send_main_menu(chat_id: int):
    bot.send_message(chat_id, "🤖 Main Menu:", reply_markup=main_menu_kb())
    set_state(str(chat_id), None)

def handle_add_coin(chat_id: int):
    set_state(str(chat_id), "adding_coin")
    bot.send_message(chat_id, "Type coin symbol to add (Futures pair, e.g., BTCUSDT).", reply_markup=back_kb())

def handle_remove_coin(chat_id: int):
    lst = coins_db.get(str(chat_id), [])
    if not lst:
        bot.send_message(chat_id, "⚠️ No coins to remove.", reply_markup=main_menu_kb())
        return
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for c in lst:
        kb.row(c)
    kb.row("🔙 Back")
    set_state(str(chat_id), "removing_coin")
    bot.send_message(chat_id, "Select coin to remove:", reply_markup=kb)

def handle_my_coins(chat_id: int):
    lst = coins_db.get(str(chat_id), [])
    if not lst:
        bot.send_message(chat_id, "⚠️ No coins in your watchlist.", reply_markup=main_menu_kb())
        return
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    for c in lst:
        kb.row(c)
    kb.row("🔙 Back")
    set_state(str(chat_id), "choose_coin")
    bot.send_message(chat_id, "Select a coin to view analysis:", reply_markup=kb)

def handle_top_movers_menu(chat_id: int):
    set_state(str(chat_id), "top_movers")
    bot.send_message(chat_id, "Choose movers window:", reply_markup=top_movers_kb())

def handle_signals_menu(chat_id: int):
    set_state(str(chat_id), "signals_menu")
    bot.send_message(chat_id, "📡 Signals — choose source:", reply_markup=signals_main_kb())

def handle_stop_signals_menu(chat_id: int):
    set_state(str(chat_id), "stop_signals_menu")
    bot.send_message(chat_id, "🛑 Stop Signals — choose:", reply_markup=stop_signals_kb())

def handle_settings_menu(chat_id: int):
    set_state(str(chat_id), "settings_menu")
    st = get_settings(str(chat_id))
    txt = (f"⚙️ Signal Settings\n"
           f"RSI Buy<thresh: <b>{st['rsi_buy']}</b>\n"
           f"RSI Sell>thresh: <b>{st['rsi_sell']}</b>\n"
           f"Signal validity (min): <b>{st['signal_validity_min']}</b>\n"
           f"Leverage cap: <b>{st['leverage_cap']}x</b>")
    bot.send_message(chat_id, txt, reply_markup=settings_kb())

def handle_preview(chat_id: int):
    st = get_settings(str(chat_id))
    sub = subs_db.get(str(chat_id), {})
    target = "None"
    if sub.get("active"):
        target = f"{sub.get('mode')} | {sub.get('coin','') or '-'} | {sub.get('interval','-')}"
    txt = (f"🔍 Current Settings\n"
           f"RSI Buy: <b>{st['rsi_buy']}</b>\n"
           f"RSI Sell: <b>{st['rsi_sell']}</b>\n"
           f"Validity: <b>{st['signal_validity_min']} min</b>\n"
           f"Leverage cap: <b>{st['leverage_cap']}x</b>\n"
           f"Active subscription: <b>{target}</b>")
    bot.send_message(chat_id, txt, reply_markup=main_menu_kb())

# ========= Webhook routes (NO decorators; we route here) =========
@app.route("/", methods=["GET"])
def home():
    log.info("Health check received at /")
    return "Bot is alive ✅", 200

@app.route(WEBHOOK_URL_PATH, methods=["POST"])
def webhook():
    update_json = request.get_json(force=True)
    log.info(f"Incoming update: {update_json}")
    try:
        if "message" in update_json:
            msg = update_json["message"]
            chat_id = msg["chat"]["id"]
            text = msg.get("text", "") or ""
            text = text.strip()

            # ----- Quick /start -----
            if text.startswith("/start"):
                bot.send_message(chat_id, "✅ Bot is live and configured for <b>BINANCE FUTURES</b> signals.",
                                 reply_markup=main_menu_kb())
                set_state(str(chat_id), None)
                return "ok", 200

            # ----- State machine -----
            state, temp = get_state(str(chat_id))

            # Global BACK
            if text == "🔙 Back":
                send_main_menu(chat_id)
                return "ok", 200

            # ===== MAIN MENU BUTTONS =====
            if text == "➕ Add Coin":
                handle_add_coin(chat_id)
                return "ok", 200

            if text == "➖ Remove Coin":
                handle_remove_coin(chat_id)
                return "ok", 200

            if text == "📊 My Coins":
                handle_my_coins(chat_id)
                return "ok", 200

            if text == "📈 Top Movers":
                handle_top_movers_menu(chat_id)
                return "ok", 200

            if text == "📡 Signals":
                handle_signals_menu(chat_id)
                return "ok", 200

            if text == "🛑 Stop Signals":
                handle_stop_signals_menu(chat_id)
                return "ok", 200

            if text == "🔄 Reset Settings":
                settings_db[str(chat_id)] = DEFAULT_SETTINGS.copy()
                save_json(SETTINGS_FILE, settings_db)
                bot.send_message(chat_id, "✅ Settings reset to defaults.", reply_markup=main_menu_kb())
                return "ok", 200

            if text == "⚙️ Signal Settings":
                handle_settings_menu(chat_id)
                return "ok", 200

            if text == "🔍 Preview Signal":
                handle_preview(chat_id)
                return "ok", 200

            # ===== ADDING COIN =====
            if state == "adding_coin":
                if text == "🔙 Back":
                    send_main_menu(chat_id); return "ok", 200
                reply = add_coin(str(chat_id), text.upper())
                bot.send_message(chat_id, reply, reply_markup=main_menu_kb())
                set_state(str(chat_id), None)
                return "ok", 200

            # ===== REMOVING COIN =====
            if state == "removing_coin":
                if text == "🔙 Back":
                    send_main_menu(chat_id); return "ok", 200
                bot.send_message(chat_id, remove_coin(str(chat_id), text.upper()), reply_markup=main_menu_kb())
                set_state(str(chat_id), None)
                return "ok", 200

            # ===== MY COINS → choose coin → timeframe → analysis =====
            if state == "choose_coin":
                if text == "🔙 Back":
                    send_main_menu(chat_id); return "ok", 200
                # must be a coin from list
                if text.upper() in coins_db.get(str(chat_id), []):
                    kb = timeframes_kb(include_back=True)
                    set_state(str(chat_id), "view_tf", coin=text.upper())
                    bot.send_message(chat_id, f"Select timeframe for <b>{text.upper()}</b>:", reply_markup=kb)
                else:
                    bot.send_message(chat_id, "❌ Choose a coin from the list.", reply_markup=back_kb())
                return "ok", 200

            if state == "view_tf":
                if text == "🔙 Back":
                    handle_my_coins(chat_id); return "ok", 200
                if text in ["1m", "5m", "15m", "1h"]:
                    coin = temp.get("coin")
                    ok, txt, info = analyze_coin(coin, text)
                    if ok:
                        bot.send_message(chat_id, txt, reply_markup=timeframes_kb(include_back=True))
                        # keep state to allow choosing other tfs
                        set_state(str(chat_id), "view_tf", coin=coin)
                    else:
                        bot.send_message(chat_id, txt, reply_markup=timeframes_kb(include_back=True))
                else:
                    bot.send_message(chat_id, "❌ Choose a timeframe.", reply_markup=timeframes_kb(include_back=True))
                return "ok", 200

            # ===== TOP MOVERS =====
            if state == "top_movers":
                if text == "🔙 Back":
                    send_main_menu(chat_id); return "ok", 200
                if text == "24h Movers":
                    top, botm = movers_24h(5)
                    t = "📈 Top 5 Gainers (24h):\n" + "\n".join(f"{s}: {p:.3f}%" for s,p in top)
                    b = "📉 Top 5 Losers (24h):\n" + "\n".join(f"{s}: {p:.3f}%" for s,p in botm)
                    bot.send_message(chat_id, f"{t}\n\n{b}", reply_markup=top_movers_kb())
                elif text in ["5m Movers", "1h Movers"]:
                    win = "5m" if text.startswith("5m") else "1h"
                    top, botm = movers_period(win, 5)
                    t = f"📈 Top 5 Gainers ({win}):\n" + "\n".join(f"{s}: {p:.3f}%" for s,p in top)
                    b = f"📉 Top 5 Losers ({win}):\n" + "\n".join(f"{s}: {p:.3f}%" for s,p in botm)
                    bot.send_message(chat_id, f"{t}\n\n{b}", reply_markup=top_movers_kb())
                else:
                    bot.send_message(chat_id, "❌ Choose a movers window.", reply_markup=top_movers_kb())
                return "ok", 200

            # ===== SIGNALS =====
            if state == "signals_menu":
                if text == "🔙 Back":
                    send_main_menu(chat_id); return "ok", 200
                if text == "a. My coins":
                    set_state(str(chat_id), "signals_my_tf")
                    bot.send_message(chat_id, "Pick timeframe for <b>My coins</b>:", reply_markup=timeframes_kb())
                elif text == "b. All coins":
                    set_state(str(chat_id), "signals_all_tf")
                    bot.send_message(chat_id, "Pick timeframe for <b>All coins</b>:", reply_markup=timeframes_kb())
                elif text == "c. Any particular coin":
                    set_state(str(chat_id), "signals_one_coin")
                    bot.send_message(chat_id, "Send the coin symbol (e.g., BTCUSDT):", reply_markup=back_kb())
                else:
                    bot.send_message(chat_id, "❌ Choose a signals source.", reply_markup=signals_main_kb())
                return "ok", 200

            # My coins → choose tf → start subscription
            if state == "signals_my_tf":
                if text == "🔙 Back":
                    handle_signals_menu(chat_id); return "ok", 200
                if text in ["1m","5m","15m","1h"]:
                    msg = start_subscription(str(chat_id), "my", text)
                    bot.send_message(chat_id, msg, reply_markup=main_menu_kb())
                    set_state(str(chat_id), None)
                else:
                    bot.send_message(chat_id, "❌ Choose a timeframe.", reply_markup=timeframes_kb())
                return "ok", 200

            # All coins → choose tf → start subscription
            if state == "signals_all_tf":
                if text == "🔙 Back":
                    handle_signals_menu(chat_id); return "ok", 200
                if text in ["1m","5m","15m","1h"]:
                    msg = start_subscription(str(chat_id), "all", text)
                    bot.send_message(chat_id, msg, reply_markup=main_menu_kb())
                    set_state(str(chat_id), None)
                else:
                    bot.send_message(chat_id, "❌ Choose a timeframe.", reply_markup=timeframes_kb())
                return "ok", 200

            # Any coin → ask coin → then tf
            if state == "signals_one_coin":
                if text == "🔙 Back":
                    handle_signals_menu(chat_id); return "ok", 200
                # validate symbol
                sym = text.upper()
                ok, _ = get_klines(sym, "1m", 2)
                if not ok:
                    bot.send_message(chat_id, "❌ Invalid symbol. Try again (e.g., BTCUSDT).", reply_markup=back_kb())
                else:
                    set_state(str(chat_id), "signals_one_tf", coin=sym)
                    bot.send_message(chat_id, f"Pick timeframe for <b>{sym}</b>:", reply_markup=timeframes_kb())
                return "ok", 200

            if state == "signals_one_tf":
                if text == "🔙 Back":
                    # go back to ask coin again
                    set_state(str(chat_id), "signals_one_coin")
                    bot.send_message(chat_id, "Send the coin symbol (e.g., BTCUSDT):", reply_markup=back_kb())
                    return "ok", 200
                if text in ["1m","5m","15m","1h"]:
                    sym = temp.get("coin","")
                    msg = start_subscription(str(chat_id), "one", text, coin=sym)
                    bot.send_message(chat_id, msg, reply_markup=main_menu_kb())
                    set_state(str(chat_id), None)
                else:
                    bot.send_message(chat_id, "❌ Choose a timeframe.", reply_markup=timeframes_kb())
                return "ok", 200

            # ===== STOP SIGNALS =====
            if state == "stop_signals_menu":
                if text == "🔙 Back":
                    send_main_menu(chat_id); return "ok", 200
                if text == "Stop: My coins":
                    bot.send_message(chat_id, stop_subscription(str(chat_id), "my"), reply_markup=main_menu_kb())
                    set_state(str(chat_id), None)
                elif text == "Stop: All coins":
                    bot.send_message(chat_id, stop_subscription(str(chat_id), "all"), reply_markup=main_menu_kb())
                    set_state(str(chat_id), None)
                elif text == "Stop: Particular coin":
                    set_state(str(chat_id), "stop_one_coin")
                    bot.send_message(chat_id, "Enter the coin to stop (e.g., BTCUSDT):", reply_markup=back_kb())
                else:
                    bot.send_message(chat_id, "❌ Choose an option.", reply_markup=stop_signals_kb())
                return "ok", 200

            if state == "stop_one_coin":
                if text == "🔙 Back":
                    handle_stop_signals_menu(chat_id); return "ok", 200
                sym = text.upper()
                bot.send_message(chat_id, stop_subscription(str(chat_id), "one", coin=sym), reply_markup=main_menu_kb())
                set_state(str(chat_id), None)
                return "ok", 200

            # ===== SETTINGS =====
            if state == "settings_menu":
                if text == "🔙 Back":
                    send_main_menu(chat_id); return "ok", 200
                if text == "Set RSI Buy:



