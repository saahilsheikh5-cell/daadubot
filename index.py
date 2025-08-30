import os
import requests
from flask import Flask, request
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from binance.client import Client
import pandas as pd
import ta

# ====== ENVIRONMENT VARIABLES ======
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")  # Telegram bot token
WEBHOOK_URL = os.getenv("WEBHOOK_URL")        # Your webhook URL (https://yourdomain.com/<token>)
BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")

# ====== INITIALIZE ======
bot = telebot.TeleBot(TELEGRAM_TOKEN)
client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)
app = Flask(__name__)

TIMEFRAMES = ["5m", "1h", "1d"]

# ====== HELPER FUNCTIONS ======
def get_top_100_symbols():
    info = client.get_ticker()
    symbols = [x['symbol'] for x in info if x['symbol'].endswith('USDT')]
    return symbols[:100]

def fetch_klines(symbol, interval):
    klines = client.get_klines(symbol=symbol, interval=interval, limit=100)
    df = pd.DataFrame(klines, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_asset_volume", "number_of_trades",
        "taker_buy_base_asset_volume", "taker_buy_quote_asset_volume", "ignore"
    ])
    df = df.astype({"open":"float", "high":"float", "low":"float", "close":"float", "volume":"float"})
    return df

def analyze(df):
    df['rsi'] = ta.momentum.RSIIndicator(df['close']).rsi()
    df['macd'] = ta.trend.MACD(df['close']).macd()
    df['macd_signal'] = ta.trend.MACD(df['close']).macd_signal()
    df['ema20'] = ta.trend.EMAIndicator(df['close'], window=20).ema_indicator()
    last = df.iloc[-1]
    signal = "❌ No signal"
    leverage = 5  # example suggestion

    # Basic multi-indicator strategy
    if last['rsi'] < 30 and last['macd'] > last['macd_signal'] and last['close'] > last['ema20']:
        signal = f"✅ BUY | Leverage: {leverage}x | Entry: {last['close']:.4f} | TP1: {last['close']*1.02:.4f} | TP2: {last['close']*1.04:.4f} | SL: {last['close']*0.98:.4f}"
    elif last['rsi'] > 70 and last['macd'] < last['macd_signal'] and last['close'] < last['ema20']:
        signal = f"✅ SELL | Leverage: {leverage}x | Entry: {last['close']:.4f} | TP1: {last['close']*0.98:.4f} | TP2: {last['close']*0.96:.4f} | SL: {last['close']*1.02:.4f}"
    return signal

def get_signals(symbol):
    results = {}
    for tf in TIMEFRAMES:
        try:
            df = fetch_klines(symbol, tf)
            results[tf] = analyze(df)
        except Exception as e:
            results[tf] = f"❌ Error fetching data: {str(e)}"
    return results

# ====== BOT MENUS ======
def main_menu():
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("My Coins", callback_data="my_coins"),
        InlineKeyboardButton("All Coins", callback_data="all_coins")
    )
    markup.add(
        InlineKeyboardButton("Particular Coin", callback_data="particular_coin"),
        InlineKeyboardButton("Top Movers", callback_data="top_movers")
    )
    return markup

# ====== TELEGRAM HANDLERS ======
@bot.message_handler(commands=['start'])
def start(message):
    bot.send_message(message.chat.id, "Welcome to Ultra Pro Signals Bot! Select an option:", reply_markup=main_menu())

@bot.callback_query_handler(func=lambda call: True)
def handle_query(call):
    chat_id = call.message.chat.id
    if call.data == "my_coins":
        bot.send_message(chat_id, "Feature coming soon.")
    elif call.data == "all_coins":
        symbols = get_top_100_symbols()
        bot.send_message(chat_id, "Top 100 USDT coins:\n" + ", ".join(symbols))
    elif call.data == "particular_coin":
        msg = bot.send_message(chat_id, "Send me the symbol of the coin (like BTCUSDT):")
        bot.register_next_step_handler(msg, send_particular_coin_signal)
    elif call.data == "top_movers":
        bot.send_message(chat_id, "Feature coming soon.")
    else:
        bot.send_message(chat_id, "Invalid option. Returning to menu.", reply_markup=main_menu())

def send_particular_coin_signal(message):
    symbol = message.text.upper()
    signals = get_signals(symbol)
    response = f"📊 Signals for {symbol}:\n"
    for tf, sig in signals.items():
        response += f"\n⏱ {tf} => {sig}"
    bot.send_message(message.chat.id, response, reply_markup=main_menu())

# ====== FLASK WEBHOOK ======
@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def webhook():
    json_data = request.get_json()
    if json_data:
        bot.process_new_updates([telebot.types.Update.de_json(json_data)])
    return "OK"

@app.route("/")
def index():
    return "Bot is live!"

# ====== START WEBHOOK ======
def set_webhook():
    bot.remove_webhook()
    bot.set_webhook(url=WEBHOOK_URL + "/" + TELEGRAM_TOKEN)
    print("Webhook set to:", WEBHOOK_URL + "/" + TELEGRAM_TOKEN)

if __name__ == "__main__":
    set_webhook()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))




