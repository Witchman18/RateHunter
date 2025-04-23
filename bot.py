import os
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes,
    ConversationHandler, filters
)
from pybit.unified_trading import HTTP
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")

session = HTTP(api_key=BYBIT_API_KEY, api_secret=BYBIT_API_SECRET)

keyboard = [["📊 Топ 5 funding-пар"], ["📈 Расчёт прибыли"]]

latest_top_pairs = []
user_state = {}

# Conversation steps
MARJA, PLECHO = range(2)

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

# ==== РАСЧЁТ ПРИБЫЛИ ====

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

        # Готовим расчёты
        msg = f"📈 Расчёт прибыли по топ 5 парам\nМаржа: {marja} USDT | Плечо: {plecho}x\n\n"

        for symbol, rate, _ in latest_top_pairs:
            gross = position * rate
            fees = position * 0.0006  # вход+выход
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

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Расчёт отменён.")
    return ConversationHandler.END

# ==== MAIN ====

if __name__ == "__main__":
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
    app.run_polling()
