import asyncio

# ==== MAIN ====

if __name__ == "__main__":
    async def main():
        app = ApplicationBuilder().token(BOT_TOKEN).build()

        app.add_handler(CommandHandler("start", start))
        app.add_handler(MessageHandler(filters.Regex("📊 Топ 5 funding-пар"), show_top_funding))

        conv_handler = ConversationHandler(
            entry_points=[MessageHandler(filters.Regex("📈 Расчёт прибыли"), start_calc)],
            states={
                MARJA: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_marja)],
                PLECHO: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_plecho)],
            },
            fallbacks=[CommandHandler("cancel", cancel)],
        )
        app.add_handler(conv_handler)

        app.add_handler(MessageHandler(filters.Regex("📡 Сигналы"), signal_menu))
        app.add_handler(CallbackQueryHandler(signal_callback))

        # ⏱️ запуск фонового снайпера
        asyncio.create_task(funding_sniper_loop(app))

        await app.run_polling()

    # Запуск через уже работающий event loop
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
