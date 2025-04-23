async def funding_sniper_loop(app):
    await asyncio.sleep(5)
    while True:
        try:
            now_ts = datetime.utcnow().timestamp()
            response = session.get_tickers(category="linear")
            tickers = response["result"]["list"]

            for chat_id, active in sniper_active.items():
                if not active:
                    continue

                user = user_state.get(chat_id, {})
                marja = user.get("real_marja", 0)
                leverage = 5
                if marja <= 0:
                    continue  # пользователь не установил маржу — пропускаем

                position = marja * leverage
                if not active:
                    continue

                for t in tickers:
                    symbol = t["symbol"]
                    rate = t.get("fundingRate")
                    next_time = t.get("nextFundingTime")

                    if not rate or not next_time:
                        continue

                    try:
                        rate = float(rate)
                        next_ts = int(next_time) / 1000
                        minutes_left = int((next_ts - now_ts) / 60)
                    except:
                        continue

                    if 0 <= minutes_left <= 1:
                        gross = position * abs(rate)
                        fees = position * 0.0006
                        spread = position * 0.0002
                        net = gross - fees - spread

                        if net > 0:
                            await app.bot.send_message(chat_id, f"📡 СИГНАЛ\n{symbol} — фандинг {rate * 100:.4f}%\nОжидаемая чистая прибыль: {net:.2f} USDT")
                            await asyncio.sleep(60)
                            await app.bot.send_message(chat_id, f"✅ Сделка завершена по {symbol}\nСимуляция: {net:.2f} USDT прибыли")
        except Exception as e:
            print(f"[Sniper Error] {e}")
        await asyncio.sleep(60)

# === MAIN ===
async def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("📊 Топ 5 funding-пар"), show_top_funding))
    app.add_handler(MessageHandler(filters.Regex("📈 Расчёт прибыли"), start_calc))
    app.add_handler(MessageHandler(filters.Regex("📡 Сигналы"), signal_menu))
    app.add_handler(MessageHandler(filters.Regex("🔧 Установить маржу"), set_real_marja))
    app.add_handler(CallbackQueryHandler(signal_callback))

    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("📈 Расчёт прибыли"), start_calc)],
        states={
            MARJA: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_marja)],
            PLECHO: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_plecho)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv_handler)

    conv_marja = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("🔧 Установить маржу"), set_real_marja)],
        states={
            SET_MARJA: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_real_marja)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv_marja)

    async def on_startup(app):
        asyncio.create_task(funding_sniper_loop(app))

    app.post_init = on_startup
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
