@app.route(WEBHOOK_URL_PATH, methods=["POST"])
def webhook():
    update_json = request.get_json(force=True)
    logger.info(f"Incoming update: {update_json}")

    if "message" in update_json:
        chat_id = update_json["message"]["chat"]["id"]
        text = update_json["message"].get("text", "")
        try:
            if text.startswith("/start") or text.startswith("/help"):
                bot.send_message(chat_id, "✅ Bot is live and ready!")
            elif text == "➕ Add Coin":
                bot.send_message(chat_id, "Type coin symbol (e.g., BTCUSDT):")
                user_state[chat_id] = "adding_coin"
            elif user_state.get(chat_id) == "adding_coin":
                coin = text.upper()
                if coin not in coins:
                    coins.append(coin)
                    save_json(USER_COINS_FILE, coins)
                    bot.send_message(chat_id, f"✅ {coin} added.")
                else:
                    bot.send_message(chat_id, f"{coin} already exists.")
                user_state[chat_id] = None
        except Exception as e:
            logger.error(f"Failed to send message: {e}")

    return "ok", 200

               



