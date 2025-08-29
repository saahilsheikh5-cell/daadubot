import os
import time
import pandas as pd
import numpy as np
from binance.client import Client
import telebot
import ta

# ---------------------------
# Environment variables
# ---------------------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CHAT_ID = os.environ.get("CHAT_ID")  # Your Telegram chat ID

BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY")
BINANCE_API_SECRET = os.environ.get("BINANCE_API_SECRET")

# ---------------------------
# Initialize clients
# ---------------------------
bot = telebot.TeleBot(TELEGRAM_TOKEN)
client = Client(BINANCE_API_KEY, BINANCE_API_SECRET)

# ---------------------------
# User Settings
# ---------------------------
TIMEFRAMES = ["1m", "5m", "15m", "1h"]  # Timeframes
CONFIDENCE_THRESHOLD = 4  # Minimum indicators agreeing
MAX_LEVERAGE = 20  # Max leverage allowed
TOP_N_MOVERS = 5  # Number of coins to track as top movers

# Risk management
TOTAL_CAPITAL = 100000  # Example: your total capital in USD
RISK_PER_TRADE = 1  # % of capital risk per trade

# ---------------------------
# Utility Functions
# ---------------------------
def get_ohlcv(symbol, interval, limit=200):
    klines = client.get_klines(symbol=symbol, interval=interval, limit=limit)
    df = pd.DataFrame(klines, columns=[
        "open_time","open","high","low","close","volume","close_time",
        "quote_asset_volume","number_of_trades","taker_buy_base",
        "taker_buy_quote","ignore"
    ])
    df = df[["open","high","low","close","volume"]].astype(float)
    return df

def calculate_indicators(df):
    df['rsi'] = ta.momentum.RSIIndicator(df['close'], window=14).rsi()
    macd = ta.trend.MACD(df['close'])
    df['macd'] = macd.macd()
    df['macd_signal'] = macd.macd_signal()
    df['ema9'] = ta.trend.EMAIndicator(df['close'], window=9).ema_indicator()
    df['ema21'] = ta.trend.EMAIndicator(df['close'], window=21).ema_indicator()
    df['sma50'] = ta.trend.SMAIndicator(df['close'], window=50).sma_indicator()
    df['sma200'] = ta.trend.SMAIndicator(df['close'], window=200).sma_indicator()
    df['atr'] = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close'], window=14).average_true_range()
    return df

def detect_candlestick(df):
    last = df.iloc[-1]
    prev = df.iloc[-2]
    body = abs(last['close'] - last['open'])
    lower_shadow = last['open'] - last['low'] if last['close'] > last['open'] else last['close'] - last['low']
    upper_shadow = last['high'] - max(last['close'], last['open'])
    
    if lower_shadow >= 2*body and upper_shadow <= body:
        return "hammer"
    if upper_shadow >= 2*body and lower_shadow <= body:
        return "shooting_star"
    if last['close'] > last['open'] and prev['close'] < prev['open'] and last['close'] > prev['open']:
        return "bullish_engulfing"
    if last['close'] < last['open'] and prev['close'] > prev['open'] and last['close'] < prev['open']:
        return "bearish_engulfing"
    if body <= 0.1 * (last['high'] - last['low']):
        return "doji"
    return None

def volume_confirmation(df):
    return df['volume'].iloc[-1] > df['volume'].rolling(20).mean().iloc[-1]

def suggest_leverage(df, risk_percent=RISK_PER_TRADE):
    """Suggest leverage based on ATR and price"""
    last = df.iloc[-1]
    atr = df['atr'].iloc[-1]
    if atr == 0:
        return 1
    leverage = int((last['close'] * risk_percent/100) / atr)
    return min(max(1, leverage), MAX_LEVERAGE)

def calculate_position_size(entry, sl, leverage):
    """Calculate position size based on risk and capital"""
    risk_amount = TOTAL_CAPITAL * RISK_PER_TRADE / 100
    stop_loss_distance = abs(entry - sl)
    if stop_loss_distance == 0:
        return 0
    raw_size = risk_amount / stop_loss_distance
    position_size = raw_size * leverage
    return round(position_size, 4)

def generate_signal(df):
    df = calculate_indicators(df)
    pattern = detect_candlestick(df)
    vol_ok = volume_confirmation(df)
    last = df.iloc[-1]
    score = 0
    signal_type = None

    # RSI
    if last['rsi'] < 30: score += 1; signal_type = "BUY"
    elif last['rsi'] > 70: score += 1; signal_type = "SELL"

    # MACD
    if last['macd'] > last['macd_signal']: score += 1; signal_type = "BUY"
    elif last['macd'] < last['macd_signal']: score += 1; signal_type = "SELL"

    # EMA
    if last['ema9'] > last['ema21']: score += 1; signal_type = "BUY"
    elif last['ema9'] < last['ema21']: score += 1; signal_type = "SELL"

    # SMA
    if last['sma50'] > last['sma200']: score += 1; signal_type = "BUY"
    elif last['sma50'] < last['sma200']: score += 1; signal_type = "SELL"

    # Candle patterns
    if pattern in ["hammer", "bullish_engulfing"]: score += 1; signal_type = "BUY"
    elif pattern in ["shooting_star", "bearish_engulfing"]: score += 1; signal_type = "SELL"

    # Volume
    if vol_ok: score += 1

    if score >= CONFIDENCE_THRESHOLD:
        entry = last['close']
        sl = entry * 0.995 if signal_type == "BUY" else entry * 1.005
        tp1 = entry * 1.01 if signal_type == "BUY" else entry * 0.99
        tp2 = entry * 1.02 if signal_type == "BUY" else entry * 0.98
        leverage = suggest_leverage(df)
        position_size = calculate_position_size(entry, sl, leverage)
        return {
            "type": signal_type,
            "entry": round(entry,2),
            "sl": round(sl,2),
            "tp1": round(tp1,2),
            "tp2": round(tp2,2),
            "leverage": leverage,
            "position_size": position_size,
            "score": score,
            "pattern": pattern
        }
    return None

# ---------------------------
# Top Movers
# ---------------------------
def get_top_movers():
    info = client.get_ticker()
    df = pd.DataFrame(info)
    df['priceChangePercent'] = df['priceChangePercent'].astype(float)
    df = df[df['symbol'].str.endswith("USDT")]
    top_gainers = df.sort_values(by='priceChangePercent', ascending=False).head(TOP_N_MOVERS)
    return top_gainers['symbol'].tolist()

# ---------------------------
# Telegram Handlers
# ---------------------------
sent_signals = set()

@bot.message_handler(commands=["mycoins"])
def mycoins_dashboard(message):
    symbols = get_top_movers()
    msg = "Select coin and timeframe:\n"
    for sym in symbols:
        for tf in TIMEFRAMES:
            msg += f"/{sym}_{tf}\n"
    bot.send_message(message.chat.id, msg)

# Dynamic handlers for each coin/timeframe
def create_handlers():
    symbols = get_top_movers()
    for sym in symbols:
        for tf in TIMEFRAMES:
            cmd = f"/{sym}_{tf}"
            def handler(message, s=sym, t=tf):
                try:
                    df = get_ohlcv(s, t)
                    signal = generate_signal(df)
                    if signal:
                        msg_text = (
                            f"🚀 {signal['type']} SIGNAL\n"
                            f"Coin: {s}\nTimeframe: {t}\n"
                            f"Entry: {signal['entry']}\nSL: {signal['sl']}\n"
                            f"TP1: {signal['tp1']} | TP2: {signal['tp2']}\n"
                            f"Suggested Leverage: {signal['leverage']}x\n"
                            f"Position Size: {signal['position_size']} units\n"
                            f"Confidence Score: {signal['score']}\n"
                            f"Candle Pattern: {signal['pattern']}"
                        )
                    else:
                        msg_text = f"No strong signal for {s} on {t} timeframe."
                    bot.send_message(message.chat.id, msg_text)
                except Exception as e:
                    bot.send_message(message.chat.id, f"Error: {e}")
            bot.message_handler(commands=[cmd])(handler)

# ---------------------------
# Auto Signals Loop
# ---------------------------
def run_auto_signals():
    global sent_signals
    while True:
        symbols = get_top_movers()
        for symbol in symbols:
            for tf in TIMEFRAMES:
                try:
                    df = get_ohlcv(symbol, tf)
                    signal = generate_signal(df)
                    if signal:
                        unique_id = f"{symbol}-{tf}-{signal['type']}-{df.index[-1]}"
                        if unique_id not in sent_signals:
                            msg = (
                                f"🚀 {signal['type']} SIGNAL\n"
                                f"Coin: {symbol}\nTimeframe: {tf}\n"
                                f"Entry: {signal['entry']}\nSL: {signal['sl']}\n"
                                f"TP1: {signal['tp1']} | TP2: {signal['tp2']}\n"
                                f"Suggested Leverage: {signal['leverage']}x\n"
                                f"Position Size: {signal['position_size']} units\n"
                                f"Confidence Score: {signal['score']}\n"
                                f"Candle Pattern: {signal['pattern']}"
                            )
                            bot.send_message(CHAT_ID, msg)
                            sent_signals.add(unique_id)
                    time.sleep(1)
                except Exception as e:
                    print(f"Error for {symbol} {tf}: {e}")
        time.sleep(60)

# ---------------------------
# Start Bot
# ---------------------------
if __name__ == "__main__":
    import threading
    create_handlers()
    threading.Thread(target=run_auto_signals).start()
    bot.infinity_polling()
