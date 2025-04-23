import os
import asyncio
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes,
    ConversationHandler, CallbackQueryHandler, filters
)
from pybit.unified_trading import HTTP
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")

session = HTTP(api_key=BYBIT_API_KEY, api_secret=BYBIT_API_SECRET)

keyboard = [["📊 Топ 5 funding-пар"], ["📈 Расчёт прибыли"], ["📡 Сигналы"], ["🔧 Установить маржу"]]
latest_top_pairs = []
user_state = {}
sniper_active = {}
MARJA, PLECHO, SET_MARJA = range(3)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text("Привет! Выбери действие:", reply_markup=reply_markup)

async def show_top_funding(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        response = session.get_tickers(category="linear")
        tickers = response["result"]["list"]
        funding_data = []

        for t in tickers:
            symbol = t["symbol"]
            rate = t.get("fundingRate")
            next_time = t.get("nextFundingTime")
            try:
                rate = float(rate)
                funding_data.append((symbol, rate, int(next_time)))
            except:
                continue

        funding_data.sort(key=lambda x: abs(x[1]), reverse=True)
        global latest_top_pairs
        latest_top_pairs = funding_data[:5]

        msg = "📊 Топ 5 funding-пар:\n\n"
        now_ts = datetime.utcnow().timestamp()
        for symbol, rate, ts in latest_top_pairs:
            delta_sec = int(ts / 1000 - now_ts)
            h, m = divmod(delta_sec // 60, 60)
            time_left = f"{h}ч {m}м"
            direction = "📈 LONG" if rate < 0 else "📉 SHORT"
            msg += f"{symbol} — {rate * 100:.4f}% → {direction} ⏱ через {time_left}\n"

        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"Ошибка при получении топа: {e}")

async def set_real_marja(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Введите сумму маржи (в USDT), которую вы хотите использовать для автоматических сделок:")
    return SET_MARJA

async def save_real_marja(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        marja = float(update.message.text)
        chat_id = update.effective_chat.id
        if chat_id not in user_state:
            user_state[chat_id] = {}
        user_state[chat_id]['real_marja'] = marja
        await update.message.reply_text(f"✅ Маржа установлена: {marja} USDT")
        return ConversationHandler.END
    except:
        await update.message.reply_text("Некорректный ввод. Введите сумму в числовом формате.")
        return SET_MARJA

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Расчёт отменён.")
    return ConversationHandler.END

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
                    continue

                position = marja * leverage

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

async def start_calc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Введите сумму маржи (в USDT):")
    return MARJA

async def set_marja(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        marja = float(update.message.text)
        user_state[update.effective_chat.id] = {"marja": marja}
        await update.message.reply_text("Теперь введите плечо (например, 5):")
        return PLECHO
    except:
        await update.message.reply_text("Некорректная сумма. Попробуйте снова.")
        return MARJA

async def set_plecho(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        plecho = float(update.message.text)
        chat_id = update.effective_chat.id
        marja = user_state[chat_id]["marja"]
        position = marja * plecho

        if not latest_top_pairs:
            await update.message.reply_text("Сначала нажмите 📊 Топ 5 funding-пар, чтобы получить актуальные данные.")
            return ConversationHandler.END

        msg = f"📈 Расчёт прибыли по топ 5 парам\nМаржа: {marja} USDT | Плечо: {plecho}x\n\n"
        for symbol, rate, _ in latest_top_pairs:
            gross = position * abs(rate)
            fees = position * 0.0006
            spread = position * 0.0002
            net = gross - fees - spread
            roi = (net / marja) * 100
            direction = "📈 LONG" if rate < 0 else "📉 SHORT"
            warn = "⚠️ Нерентабельно" if net < 0 else ""
            msg += (
                f"{symbol} → {direction}\n"
                f"  📊 Фандинг: {rate * 100:.4f}%\n"
                f"  💰 Грязная прибыль: {gross:.2f} USDT\n"
                f"  💸 Комиссии: {fees:.2f} USDT\n"
                f"  📉 Спред: {spread:.2f} USDT\n"
                f"  ✅ Чистая прибыль: {net:.2f} USDT\n"
                f"  📈 ROI: {roi:.2f}% {warn}\n\n"
            )
        await update.message.reply_text(msg)
        return ConversationHandler.END
    except:
        await update.message.reply_text("Ошибка при вводе плеча. Попробуйте снова.")
        return PLECHO

async def signal_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🔔 Включить сигналы", callback_data="sniper_on")],
        [InlineKeyboardButton("🔕 Выключить сигналы", callback_data="sniper_off")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("📡 Режим сигналов:", reply_markup=reply_markup)

if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("📊 Топ 5 funding-пар"), show_top_funding))
    app.add_handler(MessageHandler(filters.Regex("📈 Расчёт прибыли"), start_calc))
    app.add_handler(MessageHandler(filters.Regex("📡 Сигналы"), signal_menu))
    app.add_handler(MessageHandler(filters.Regex("🔧 Установить маржу"), set_real_marja))
    app.add_handler(CallbackQueryHandler(start))  # пока ты не реализовал отдельную функцию

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
    app.run_polling()
