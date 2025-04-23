import os
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from pybit.unified_trading import HTTP
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")

session = HTTP(api_key=BYBIT_API_KEY, api_secret=BYBIT_API_SECRET)

# Команда /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [["📊 Топ 5 funding-пар"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text("Привет! Выбери действие:", reply_markup=reply_markup)

# Ответ на кнопку
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "📊 Топ 5 funding-пар":
        try:
            response = session.get_tickers(category="linear")
            tickers = response["result"]["list"]

            funding_data = []
            for t in tickers:
                symbol = t["symbol"]
                raw_rate = t.get("fundingRate")
                try:
                    rate = float(raw_rate)
                    funding_data.append((symbol, rate))
                except:
                    continue

            funding_data.sort(key=lambda x: abs(x[1]), reverse=True)
            top_5 = funding_data[:5]

            msg = "📊 Топ 5 funding-пар:\n\n"
            for symbol, rate in top_5:
                direction = "📈 LONG" if rate < 0 else "📉 SHORT"
                msg += f"{symbol} — {rate * 100:.4f}% → {direction}\n"

            await update.message.reply_text(msg)

        except Exception as e:
            await update.message.reply_text(f"Ошибка при получении данных: {e}")
    else:
        await update.message.reply_text("Пожалуйста, выбери действие с помощью кнопки 👇")

# Запуск
if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()
