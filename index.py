
import os
from flask import Flask, request
import telebot
from binance.client import Client
import pandas as pd
import ta

# --- Environment Variables ---
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")  # e.g., https://<your-service>.onrender.com/webhook
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY")
BINANCE_API_SECRET = os.environ.get("BINANCE_API_SECRET")

# --- Initialize ---
bot = telebot.TeleBot(TELEGRAM_TOKEN)
app = Flask(__name__)
client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)

# --- Top 100 Binance Coins ---
TOP_100_COINS = [coin['symbol'] for coin in client.get_ticker()][:100]  # simplified

# --- Menu Keyboard ---
from telebot.types import ReplyKeyboardMarkup, KeyboardButton

def main_menu():
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(KeyboardButton("My Coins"))
    markup.add(KeyboardButton("All Coins"))
    markup.add(KeyboardButton("Particular Coin"))
    markup.add(KeyboardButton("Top Movers"))
    return markup

# --- Command Handlers ---
@bot.message_handler(commands=["start"])
def start(message):
    bot.send_message(message.chat.id, "Welcome! Choose an option:", reply_markup=main_menu())

# --- Fetch signals for a coin ---
def get_signals(symbol):
    try:
        # Example: fetch last 100 candles for 3 timeframes
        timeframes = ["5m", "1h", "1d"]
        signals = {}
        for tf in timeframes:
            klines = client.get_klines(symbol=symbol, interval=tf, limit=100)
            df = pd.DataFrame(klines, columns=[
                "Open time", "Open", "High", "Low", "Close", "Volume",
                "Close time", "Quote asset volume", "Number of trades",
                "Taker buy base asset volume", "Taker buy quote asset volume", "Ignore"
            ])
            df["Close"] = df["Close"].astype(float)
            df["rsi"] = ta.momentum.RSIIndicator(df["Close"]).rsi()
            df["macd"] = ta.trend.MACD(df["Close"]).macd()
            df["signal"] = "Buy" if df["rsi"].iloc[-1] < 30 and df["macd"].iloc[-1] > 0 else \
                           "Sell" if df["rsi"].iloc[-1] > 70 and df["macd"].iloc[-1] < 0 else "Hold"
            signals[tf] = {
                "Last Close": df["Close"].iloc[-1],
                "RSI": df["rsi"].iloc[-1],
                "MACD": df["macd"].iloc[-1],
                "Signal": df["signal"],
                "Entry": df["Close"].iloc[-1],
                "Stop Loss": round(df["Close"].iloc[-1]*0.98, 4),
                "TP1": round(df["Close"].iloc[-1]*1.02, 4),
                "TP2": round(df["Close"].iloc[-1]*1.05, 4),
                "Suggested Leverage": 5
            }
        return signals
    except Exception as e:
        return {"error": str(e)}

# --- Message Handlers ---
@bot.message_handler(func=lambda m: m.text == "My Coins")
def my_coins(message):
    bot.send_message(message.chat.id, "Fetching signals for your coins...")
    # Fetch your saved coins here (example)
    my_coin_list = ["BTCUSDT", "ETHUSDT"]
    for coin in my_coin_list:
        signals = get_signals(coin)
        bot.send_message(message.chat.id, f"{coin} Signals:\n{signals}")

@bot.message_handler(func=lambda m: m.text == "All Coins")
def all_coins(message):
    bot.send_message(message.chat.id, "Fetching signals for top 100 coins...")
    for coin in TOP_100_COINS:
        signals = get_signals(coin)
        bot.send_message(message.chat.id, f"{coin} Signals:\n{signals}")

@bot.message_handler(func=lambda m: m.text == "Particular Coin")
def particular_coin(message):
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    for coin in TOP_100_COINS:
        markup.add(KeyboardButton(coin))
    bot.send_message(message.chat.id, "Select a coin:", reply_markup=markup)

@bot.message_handler(func=lambda m: m.text in TOP_100_COINS)
def coin_selected(message):
    coin = message.text
    signals = get_signals(coin)
    bot.send_message(message.chat.id, f"{coin} Signals:\n{signals}", reply_markup=main_menu())

@bot.message_handler(func=lambda m: m.text == "Top Movers")
def top_movers(message):
    tickers = client.get_ticker()
    movers = sorted(tickers, key=lambda x: float(x['priceChangePercent']), reverse=True)[:10]
    msg = "Top Movers (24h):\n"
    for m in movers:
        msg += f"{m['symbol']} → {m['priceChangePercent']}%\n"
    bot.send_message(message.chat.id, msg, reply_markup=main_menu())

# --- Webhook Routes ---
@app.route("/webhook", methods=["POST"])
def webhook():
    json_str = request.get_data().decode("utf-8")
    update = telebot.types.Update.de_json(json_str)
    bot.process_new_updates([update])
    return "", 200

# --- Set Webhook on Startup ---
bot.remove_webhook()
bot.set_webhook(url=WEBHOOK_URL)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)




