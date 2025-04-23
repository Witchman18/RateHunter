import os
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
    ConversationHandler,
)
from pybit.unified_trading import HTTP
from dotenv import load_dotenv
from datetime import datetime
import math

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")

session = HTTP(api_key=BYBIT_API_KEY, api_secret=BYBIT_API_SECRET)

# Шаги для ConversationHandler
MARJA, PLECHO = range(2)

user_inputs = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [["📊 Топ 5 funding-пар"], ["📈 Расчёт прибыли"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text("Привет! Выбери действие:", reply_markup=reply_markup)

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "📊 Топ 5 funding-пар":
        await top_funding(update, context)
    elif text == "📈 Расчёт прибыли":
        await update.message.reply_text("Введите сумму маржи (в USDT):")
        return MARJA

async def ask_leverage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_inputs[update.effective_chat.id] = {"margin": float(update.message.text)}
        await update.message.reply_text("Введите плечо (например, 5):")
        return PLECHO
    except ValueError:
        await update.message.reply_text("Введите корректное число для маржи.")
        return MARJA

async def calculate_profit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_id = update.effective_chat.id
        user_inputs[chat_id]["leverage"] = float(update.message.text)

        margin = user_inputs[chat_id]["margin"]
        leverage = user_inputs[chat_id]["leverage"]
        response = session.get_tickers(category="linear")
        tickers = response["result"]["list"]

        funding_data = []
        for t in tickers:
            symbol = t["symbol"]
            rate = t.get("fundingRate")
            try:
                rate = float(rate)
                funding_data.append((symbol, rate))
            except:
                continue

        funding_data.sort(key=lambda x: abs(x[1]), reverse=True)
        top_5 = funding_data[:5]

        msg = f"🧮 Расчёт прибыли (на {margin} USDT, {leverage}x плечо):\n\n"

        for symbol, rate in top_5:
            gross = (rate * leverage * margin)
            commission = margin * leverage * 0.0003 * 2  # вход и выход
            spread = margin * 0.001
            net = gross - commission - spread
            roi = (net / margin) * 100

            msg += (
                f"{symbol}:\n"
                f"📉 Грязная прибыль: {gross:.2f} USDT\n"
                f"💸 Комиссии: {commission:.2f} USDT\n"
                f"📉 Спред: {spread:.2f} USDT\n"
                f"✅ Чистая прибыль: {net:.2f} USDT\n"
                f"📊 ROI: {roi:.2f}%\n\n"
            )

        await update.message.reply_text(msg)
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"Ошибка при расчёте прибыли: {e}")
        return ConversationHandler.END

async def top_funding(update: Update, context: ContextTypes.DEFAULT_TYPE):
    response = session.get_tickers(category="linear")
    tickers = response["result"]["list"]

    funding_data = []
    for t in tickers:
        symbol = t["symbol"]
        rate = t.get("fundingRate")
        try:
            rate = float(rate)
            funding_data.append((symbol, rate))
        except:
            continue

    funding_data.sort(key=lambda x: abs(x[1]), reverse=True)
    top_5 = funding_data[:5]

    msg = "📊 Топ 5 funding-пар:\n"
    for symbol, rate in top_5:
        direction = "📈 LONG" if rate < 0 else "📉 SHORT"
        msg += f"{symbol} — {rate * 100:.4f}% → {direction}\n"

    await update.message.reply_text(msg)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Операция отменена.")
    return ConversationHandler.END

if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("📈 Расчёт прибыли"), message_handler)],
        states={
            MARJA: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_leverage)],
            PLECHO: [MessageHandler(filters.TEXT & ~filters.COMMAND, calculate_profit)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("📊 Топ 5 funding-пар"), message_handler))
    app.add_handler(conv_handler)

    app.run_polling()
