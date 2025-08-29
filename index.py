import os
import time
import pandas as pd
import numpy as np
from binance.client import Client
import talib
from ta.momentum import RSIIndicator
import telebot

# --------------------------
# Environment Variables
# --------------------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY")
BINANCE_API_SECRET = os.environ.get("BINANCE_API_SECRET")

if not TELEGRAM_TOKEN or not BINANCE_API_KEY or not BINANCE_API_SECRET:
    raise Exception("Missing TELEGRAM_TOKEN or Binance API keys in environment variables.")

bot = telebot.TeleBot(TELEGRAM_TOKEN)
client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)

# --------------------------
# Technical Indicators
# --------------------------
def get_signals(symbol, interval='1h', limit=500):
    """
    Returns signals for a given symbol and interval based on multiple indicators.
    Only returns 'strong' signals.
    """
    try:
        klines = client.get_klines(symbol=symbol, interval=interval, limit=limit)
        df = pd.DataFrame(klines, columns=['OpenTime','Open','High','Low','Close','Volume',
                                           'CloseTime','QuoteAssetVolume','Trades','TBBV','TBAV','Ignore'])
        df = df.astype({'Open':'float','High':'float','Low':'float','Close':'float','Volume':'float'})
        
        close = df['Close']
        
        # RSI
        rsi = RSIIndicator(close, window=14).rsi()
        last_rsi = rsi.iloc[-1]
        
        # MACD
        macd, macd_signal, _ = talib.MACD(close, fastperiod=12, slowperiod=26, signalperiod=9)
        last_macd = macd.iloc[-1]
        last_macd_signal = macd_signal.iloc[-1]
        
        # Candle pattern (example: bullish engulfing)
        candle = talib.CDLENGULFING(df['Open'], df['High'], df['Low'], df['Close'])
        last_candle = candle.iloc[-1]
        
        # Combine signals
        signals = []
        if last_rsi < 30:
            signals.append("RSI oversold")
        elif last_rsi > 70:
            signals.append("RSI overbought")
            
        if last_macd > last_macd_signal:
            signals.append("MACD bullish")
        elif last_macd < last_macd_signal:
            signals.append("MACD bearish")
        
        if last_candle > 0:
            signals.append("Bullish Engulfing")
        elif last_candle < 0:
            signals.append("Bearish Engulfing")
        
        # Only return strong signals (at least 2 indicators aligned)
        if len(signals) >= 2:
            return signals
        else:
            return []
        
    except Exception as e:
        print(f"Error fetching signals for {symbol}: {e}")
        return []

# --------------------------
# Bot Commands
# --------------------------
@bot.message_handler(commands=['start'])
def send_welcome(message):
    bot.reply_to(message, "Welcome! Use /signal <COIN> <INTERVAL> to get signals.")

@bot.message_handler(commands=['signal'])
def send_signal(message):
    try:
        args = message.text.split()
        if len(args) != 3:
            bot.reply_to(message, "Usage: /signal <COIN> <INTERVAL>\nExample: /signal BTCUSDT 1h")
            return
        symbol = args[1].upper()
        interval = args[2]
        signals = get_signals(symbol, interval)
        if signals:
            bot.reply_to(message, f"Strong signals for {symbol} ({interval}):\n- " + "\n- ".join(signals))
        else:
            bot.reply_to(message, f"No strong signals for {symbol} ({interval}) at the moment.")
    except Exception as e:
        bot.reply_to(message, f"Error: {e}")

# --------------------------
# Auto Signal Broadcast (optional)
# --------------------------
def broadcast_signals(coins, interval='1h', chat_id=None):
    for coin in coins:
        signals = get_signals(coin, interval)
        if signals and chat_id:
            bot.send_message(chat_id, f"Strong signals for {coin} ({interval}):\n- " + "\n- ".join(signals))

# --------------------------
# Run Bot
# --------------------------
if __name__ == "__main__":
    print("Bot is running...")
    bot.infinity_polling()
