import os
import time
import logging
from logging.handlers import TimedRotatingFileHandler
import telebot
from apscheduler.schedulers.background import BackgroundScheduler
from db import init_db, add_subscription, remove_subscription, list_subscriptions, reset_settings, test_db_connection
from signals import analyze_and_signal

# Setup logging with daily rotation
log_handler = TimedRotatingFileHandler("bot.log", when="midnight", interval=1, backupCount=7)
log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
log_handler.suffix = "%Y-%m-%d"
logging.basicConfig(level=logging.INFO, handlers=[log_handler])

API_KEY = os.getenv("TELEGRAM_TOKEN")
bot = telebot.TeleBot(API_KEY)
scheduler = BackgroundScheduler()
scheduler.start()

# Initialize DB
init_db()

# Restore subscriptions from DB on startup
def restore_jobs():
    all_chats = list_subscriptions(None, all_chats=True)
    for chat_id, symbol, interval, threshold in all_chats:
        try:
            scheduler.add_job(
                analyze_and_signal,
                'interval',
                seconds=interval,
                args=[bot, chat_id, symbol, threshold],
                id=f"{chat_id}_{symbol}",
                replace_existing=True
            )
            logging.info(f"Restored job for {chat_id} {symbol} ({interval}s, threshold {threshold})")
        except Exception as e:
            logging.error(f"Failed to restore {chat_id} {symbol}: {e}")

@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, "🤖 Welcome! Use /add <symbol> <interval_seconds> <threshold> to start getting ultra signals.")
    logging.info(f"/start called by {message.chat.id}")

@bot.message_handler(commands=['add'])
def add_coin(message):
    try:
        _, symbol, interval, threshold = message.text.split()
        interval = int(interval)
        threshold = int(threshold)
        add_subscription(message.chat.id, symbol.upper(), interval, threshold)
        scheduler.add_job(analyze_and_signal, 'interval', seconds=interval, args=[bot, message.chat.id, symbol.upper(), threshold], id=f"{message.chat.id}_{symbol}", replace_existing=True)
        bot.reply_to(message, f"✅ Subscribed to {symbol.upper()} every {interval}s with threshold {threshold}")
        logging.info(f"Added subscription for {message.chat.id}: {symbol.upper()}, {interval}s, threshold {threshold}")
    except Exception as e:
        bot.reply_to(message, f"❌ Usage: /add BTCUSDT 60 70  — Error: {e}")
        logging.error(f"Failed to add subscription for {message.chat.id}: {e}")

@bot.message_handler(commands=['stop'])
def stop_coin(message):
    try:
        _, symbol = message.text.split()
        remove_subscription(message.chat.id, symbol.upper())
        scheduler.remove_job(f"{message.chat.id}_{symbol.upper()}")
        bot.reply_to(message, f"🛑 Stopped {symbol.upper()}")
        logging.info(f"Stopped subscription for {message.chat.id}: {symbol.upper()}")
    except Exception as e:
        bot.reply_to(message, f"❌ Usage: /stop BTCUSDT — Error: {e}")
        logging.error(f"Failed to stop subscription for {message.chat.id}: {e}")

@bot.message_handler(commands=['mycoins'])
def mycoins(message):
    subs = list_subscriptions(message.chat.id)
    if not subs:
        bot.reply_to(message, "📭 No active subscriptions")
        logging.info(f"{message.chat.id} has no active subscriptions")
    else:
        msg = "📊 Your subscriptions:\n"
        for s in subs:
            msg += f"- {s[0]} (interval {s[1]}s, threshold {s[2]})\n"
        bot.reply_to(message, msg)
        logging.info(f"Listed subscriptions for {message.chat.id}")

@bot.message_handler(commands=['reset'])
def reset(message):
    reset_settings(message.chat.id)
    bot.reply_to(message, "♻️ All settings reset")
    logging.info(f"Reset settings for {message.chat.id}")

@bot.message_handler(commands=['logs'])
def send_logs(message):
    try:
        if os.path.exists("bot.log"):
            with open("bot.log", "rb") as f:
                bot.send_document(message.chat.id, f)
            logging.info(f"Sent logs to {message.chat.id}")
        else:
            bot.reply_to(message, "⚠️ No log file found yet.")
    except Exception as e:
        bot.reply_to(message, f"❌ Failed to send logs: {e}")
        logging.error(f"Failed to send logs to {message.chat.id}: {e}")

@bot.message_handler(commands=['health'])
def health_check(message):
    try:
        db_ok = test_db_connection()
        sched_jobs = len(scheduler.get_jobs())
        msg = f"🩺 Health Check:\n- DB Connection: {'✅' if db_ok else '❌'}\n- Scheduled Jobs: {sched_jobs}\n- Bot: ✅ Running"
        bot.reply_to(message, msg)
        logging.info(f"Health check requested by {message.chat.id}: DB={db_ok}, Jobs={sched_jobs}")
    except Exception as e:
        bot.reply_to(message, f"❌ Health check failed: {e}")
        logging.error(f"Health check failed for {message.chat.id}: {e}")

# Safe polling with auto-reconnect
def run_bot():
    restore_jobs()
    while True:
        try:
            logging.info("Bot polling started...")
            bot.infinity_polling(timeout=60, long_polling_timeout=60)
        except Exception as e:
            logging.error(f"Polling error: {e}. Restarting in 5s...")
            time.sleep(5)

if __name__ == "__main__":
    run_bot()




