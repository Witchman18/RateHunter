import os
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
from pybit.unified_trading import HTTP
from dotenv import load_dotenv
from datetime import datetime, timedelta

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

# Обработка кнопки
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "📊 Топ 5 funding-пар":
        try:
            tickers = session.get_tickers(category="linear")["result"]["list"]
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

            # Получаем время выплат
            msg = "📊 Топ 5 funding-пар:\n\n"
            for symbol, rate in top_5:
                try:
                    funding_info = session.get_funding_rate_history(
                        category="linear", symbol=symbol, limit=1
                    )["result"]["list"][0]
                    timestamp = int(funding_info["fundingRateTimestamp"]) / 1000
                    payout_time = datetime.utcfromtimestamp(timestamp)
                    now = datetime.utcnow()
                    delta = payout_time - now

                    hours, remainder = divmod(int(delta.total_seconds()), 3600)
                    minutes = remainder // 60

                    time_str = f"⏱ через {hours}ч {minutes}м" if hours else f"⏱ через {minutes}м"
                except:
                    time_str = "⏱ время неизвестно"

                direction = "📈 LONG" if rate < 0 else "📉 SHORT"
                msg += f"{symbol} — {rate * 100:.4f}% → {direction} {time_str}\n"

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
