import os
import time
import requests
import pandas as pd
from threading import Thread
from ta.momentum import RSIIndicator
from ta.trend import MACD
from ta.volatility import BollingerBands
import telebot

# === ENV VARIABLES ===
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

bot = telebot.TeleBot(TELEGRAM_TOKEN)

# === SETTINGS ===
COINS = ["BTCUSDT", "ETHUSDT"]  # Add more coins here
INTERVALS = ["1m", "5m", "15m", "1h", "1d"]
RSI_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
BOLLINGER_WINDOW = 20
BOLLINGER_STD = 2
MIN_INDICATORS_FOR_SIGNAL = 3

# Suggested leverage map per coin
LEVERAGE = {
    "BTCUSDT": 5,
    "ETHUSDT": 10
}

# Keep track of last signals to avoid duplicate alerts
LAST_SIGNAL = {coin: {interval: None for interval in INTERVALS} for coin in COINS}

# === FUNCTIONS ===

def fetch_ohlcv(symbol, interval="1m", limit=100):
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
    data = requests.get(url).json()
    df = pd.DataFrame(data, columns=[
        "timestamp", "open", "high", "low", "close", "volume",
        "close_time", "quote_asset_volume", "trades", "taker_buy_base",
        "taker_buy_quote", "ignore"
    ])
    df = df[["timestamp", "open", "high", "low", "close", "volume"]].astype(float)
    return df

def analyze_coin(df):
    signals = []
    close = df['close']

    # --- RSI ---
    rsi = RSIIndicator(close, RSI_PERIOD).rsi()
    if rsi.iloc[-1] > 70:
        signals.append("overbought")
    elif rsi.iloc[-1] < 30:
        signals.append("oversold")

    # --- MACD ---
    macd_line = MACD(close, MACD_FAST, MACD_SLOW, MACD_SIGNAL).macd()
    signal_line = MACD(close, MACD_FAST, MACD_SLOW, MACD_SIGNAL).macd_signal()
    if macd_line.iloc[-1] > signal_line.iloc[-1]:
        signals.append("macd_buy")
    elif macd_line.iloc[-1] < signal_line.iloc[-1]:
        signals.append("macd_sell")

    # --- Bollinger Bands ---
    bb = BollingerBands(close, BOLLINGER_WINDOW, BOLLINGER_STD)
    if close.iloc[-1] > bb.bollinger_hband().iloc[-1]:
        signals.append("bb_sell")
    elif close.iloc[-1] < bb.bollinger_lband().iloc[-1]:
        signals.append("bb_buy")

    # --- Final Signal ---
    buy_signals = ["oversold", "macd_buy", "bb_buy"]
    sell_signals = ["overbought", "macd_sell", "bb_sell"]

    if sum(sig in buy_signals for sig in signals) >= MIN_INDICATORS_FOR_SIGNAL:
        return "BUY"
    elif sum(sig in sell_signals for sig in signals) >= MIN_INDICATORS_FOR_SIGNAL:
        return "SELL"
    else:
        return None

def calculate_levels(df, signal):
    close = df['close'].iloc[-1]
    if signal == "BUY":
        entry = close
        stop_loss = close * 0.995  # 0.5% below entry
        tp1 = close * 1.01
        tp2 = close * 1.02
    elif signal == "SELL":
        entry = close
        stop_loss = close * 1.005  # 0.5% above entry
        tp1 = close * 0.99
        tp2 = close * 0.98
    else:
        entry = stop_loss = tp1 = tp2 = None
    return entry, stop_loss, tp1, tp2

def send_signal(symbol, interval, signal, entry, stop_loss, tp1, tp2, leverage):
    msg = (
        f"🚀 Signal for {symbol} [{interval}]: {signal}\n"
        f"Leverage: {leverage}x\n"
        f"Entry: {entry:.2f}\n"
        f"Stop Loss: {stop_loss:.2f}\n"
        f"TP1: {tp1:.2f} | TP2: {tp2:.2f}"
    )
    bot.send_message(CHAT_ID, msg)

def run_bot():
    while True:
        try:
            for coin in COINS:
                for interval in INTERVALS:
                    df = fetch_ohlcv(coin, interval)
                    signal = analyze_coin(df)
                    # Avoid duplicate signals
                    if signal and signal != LAST_SIGNAL[coin][interval]:
                        entry, stop_loss, tp1, tp2 = calculate_levels(df, signal)
                        send_signal(coin, interval, signal, entry, stop_loss, tp1, tp2, LEVERAGE.get(coin, 1))
                        LAST_SIGNAL[coin][interval] = signal
            time.sleep(60)
        except Exception as e:
            print("Error:", e)
            time.sleep(10)

# === START THREAD ===
if __name__ == "__main__":
    thread = Thread(target=run_bot)
    thread.start()
    bot.infinity_polling()

