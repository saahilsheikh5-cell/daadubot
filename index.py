import os
import threading
import time
import pandas as pd
import numpy as np
from flask import Flask, request
import telebot
from telebot import types
from binance.client import Client
from binance.exceptions import BinanceAPIException

# --- ENV VARIABLES ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = int(os.environ.get("Telegram_Chat_ID", 0))
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY")
BINANCE_API_SECRET = os.environ.get("BINANCE_API_SECRET")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")  # e.g., https://yourdomain.com/<bot_token>

bot = telebot.TeleBot(TELEGRAM_TOKEN)
app = Flask(__name__)
client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)

# --- GLOBALS ---
my_coins = []
top100_list = []
auto_signal_flag = False
top_movers_auto = False

# --- FETCH TOP 100 BINANCE COINS ---
def fetch_top100():
    global top100_list
    try:
        tickers = client.get_ticker()
        df = pd.DataFrame(tickers)
        df['quoteVolume'] = df['quoteVolume'].astype(float)
        df = df.sort_values('quoteVolume', ascending=False)
        top100_list = df['symbol'].tolist()[:100]
    except Exception as e:
        print(f"Error fetching top100: {e}")

# --- SIGNAL CALCULATION PLACEHOLDER ---
def calculate_signal(symbol, interval):
    """
    Replace this with your ultra signal logic using multiple indicators
    Returns dict:
    {
        'decision': '✅ Strong BUY' or '❌ Strong SELL',
        'RSI': value,
        'MACD': (macd, signal),
        'Price': current_price,
        'Entry': entry_price,
        'TP1': tp1,
        'TP2': tp2,
        'SL': sl,
        'Leverage': x10,
        'valid_for': minutes,
        'notes': 'Ultra signal based on multiple indices'
    }
    """
    try:
        # Dummy data (replace with real calculation)
        price = float(client.get_symbol_ticker(symbol=symbol)['price'])
        decision = np.random.choice(['✅ Strong BUY', '❌ Strong SELL'])
        tp_adjust = 1.01 if 'BUY' in decision else 0.99
        signal_data = {
            'decision': decision,
            'RSI': round(np.random.uniform(30, 70),2),
            'MACD': (round(np.random.uniform(-1,1),4), round(np.random.uniform(-1,1),4)),
            'Price': price,
            'Entry': price,
            'TP1': round(price*tp_adjust,4),
            'TP2': round(price*tp_adjust**2,4),
            'SL': round(price*0.99 if 'BUY' in decision else price*1.01,4),
            'Leverage': 'x10',
            'valid_for': interval_to_minutes(interval),
            'notes': 'Ultra signal based on multiple indices'
        }
        return signal_data
    except BinanceAPIException as e:
        return {'error': str(e)}

def interval_to_minutes(interval):
    mapping = {'1m':1, '5m':5, '15m':15, '1h':60, '1d':1440}
    return mapping.get(interval,5)

# --- GENERATE SIGNAL MESSAGE ---
def format_signal_msg(symbol, interval, signal):
    return f"""📊 Signal for {symbol} [{interval}]
Decision: {signal['decision']}
RSI: {signal['RSI']}
MACD: {signal['MACD'][0]} / Signal: {signal['MACD'][1]}
Price: {signal['Price']}

Entry: {signal['Entry']}
TP1: {signal['TP1']}
TP2: {signal['TP2']}
SL: {signal['SL']}
Suggested Leverage: {signal['Leverage']}
Signal valid for: {signal['valid_for']} mins
Notes: {signal['notes']}"""

# --- MANUAL SIGNAL HANDLER ---
def send_manual_signals(symbols, interval):
    for sym in symbols:
        signal = calculate_signal(sym, interval)
        if 'error' in signal: 
            bot.send_message(CHAT_ID, f"⚠️ Error fetching data for {sym} [{interval}]: {signal['error']}")
            continue
        if 'Neutral' in signal['decision']:  # skip neutral
            continue
        msg = format_signal_msg(sym, interval, signal)
        bot.send_message(CHAT_ID, msg)

# --- AUTO SIGNAL LOOP ---
def auto_signal_loop(interval):
    global auto_signal_flag
    while auto_signal_flag:
        fetch_top100()
        send_manual_signals(top100_list, interval)
        time.sleep(interval_to_minutes(interval)*60)

# --- TOP MOVERS AUTO LOOP ---
def top_movers_loop():
    global top_movers_auto
    while top_movers_auto:
        try:
            tickers = client.get_ticker()
            df = pd.DataFrame(tickers)
            df['priceChangePercent'] = df['priceChangePercent'].astype(float)
            top = df.sort_values('priceChangePercent', ascending=False).head(10)
            for idx, row in top.iterrows():
                if abs(row['priceChangePercent']) >= 5:
                    direction = "🚀 Up" if row['priceChangePercent'] > 0 else "❌ Down"
                    msg = f"{direction} {row['symbol']}: {row['priceChangePercent']:.2f}% change"
                    bot.send_message(CHAT_ID, msg)
            time.sleep(60)
        except Exception as e:
            print(f"Top Movers Auto Error: {e}")
            time.sleep(30)

# --- TELEGRAM BUTTON HANDLERS ---
@bot.message_handler(commands=['start'])
def send_welcome(message):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("➕ Add Coin", "📈 Signals")
    markup.row("🚀 Top Movers", "🔎 Particular Coin")
    markup.row("🕑 Auto Signals Start", "⏹ Stop Auto Signals")
    markup.row("🚀 Top Movers Auto", "⏹ Stop Top Movers Auto")
    bot.send_message(message.chat.id, "🤖 Welcome to Ultra Signals Bot!", reply_markup=markup)

@bot.message_handler(func=lambda m: m.text=="➕ Add Coin")
def add_coin(message):
    msg = bot.send_message(message.chat.id, "Enter coin symbol to add (e.g., BTCUSDT):")
    bot.register_next_step_handler(msg, save_coin)

def save_coin(message):
    coin = message.text.upper()
    if coin not in my_coins:
        my_coins.append(coin)
        bot.send_message(message.chat.id, f"✅ {coin} added to My Coins.")
    else:
        bot.send_message(message.chat.id, f"⚠️ {coin} already in My Coins.")

@bot.message_handler(func=lambda m: m.text=="📈 Signals")
def signals_menu(message):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row("💼 My Coins", "🌍 All Coins")
    markup.row("1m","5m","15m","1h","1d")
    bot.send_message(message.chat.id, "Choose a signal option:", reply_markup=markup)

@bot.message_handler(func=lambda m: m.text in ["💼 My Coins","🌍 All Coins"])
def handle_manual_signal(message):
    interval_msg = bot.send_message(message.chat.id, "Choose timeframe: 1m,5m,15m,1h,1d")
    bot.register_next_step_handler(interval_msg, manual_signal_process, message.text)

def manual_signal_process(msg, choice):
    interval = msg.text
    symbols = my_coins if choice=="💼 My Coins" else top100_list
    send_manual_signals(symbols, interval)

@bot.message_handler(func=lambda m: m.text=="🕑 Auto Signals Start")
def start_auto_signals(message):
    global auto_signal_flag
    if not auto_signal_flag:
        auto_signal_flag = True
        interval_msg = bot.send_message(message.chat.id, "Select timeframe for Auto Signals: 1m,5m,15m,1h,1d")
        bot.register_next_step_handler(interval_msg, start_auto_loop)
        
def start_auto_loop(msg):
    interval = msg.text
    threading.Thread(target=auto_signal_loop, args=(interval,), daemon=True).start()
    bot.send_message(CHAT_ID, f"✅ Auto Signals started every {interval}.")

@bot.message_handler(func=lambda m: m.text=="⏹ Stop Auto Signals")
def stop_auto_signals(message):
    global auto_signal_flag
    auto_signal_flag = False
    bot.send_message(message.chat.id, "⏹ Auto signals stopped.")

@bot.message_handler(func=lambda m: m.text=="🚀 Top Movers Auto")
def start_top_movers(message):
    global top_movers_auto
    if not top_movers_auto:
        top_movers_auto = True
        threading.Thread(target=top_movers_loop, daemon=True).start()
        bot.send_message(message.chat.id, "✅ Top Movers Auto started (24x7).")

@bot.message_handler(func=lambda m: m.text=="⏹ Stop Top Movers Auto")
def stop_top_movers(message):
    global top_movers_auto
    top_movers_auto = False
    bot.send_message(message.chat.id, "⏹ Top Movers Auto stopped.")

# --- FLASK WEBHOOK ---
@app.route(f"/{TELEGRAM_TOKEN}", methods=['POST'])
def webhook():
    json_str = request.get_data().decode('utf-8')
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "!", 200

if __name__ == "__main__":
    fetch_top100()
    bot.remove_webhook()
    bot.set_webhook(url=f"{WEBHOOK_URL}/{TELEGRAM_TOKEN}")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))










