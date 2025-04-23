import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
from pybit.unified_trading import HTTP
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")

session = HTTP(api_key=BYBIT_API_KEY, api_secret=BYBIT_API_SECRET)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("📊 Топ 5 funding-пар", callback_data='top')]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Привет! Выбери действие:", reply_markup=reply_markup)

async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "top":
        await send_top_funding(query.message.chat_id, context)

async def send_top_funding(chat_id, context):
    try:
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

        msg = "📊 Топ 5 funding-пар:\n\n"
        for symbol, rate in top_5:
            direction = "📈 LONG" if rate < 0 else "📉 SHORT"
            msg += f"{symbol} — {rate * 100:.4f}% → {direction}\n"

        keyboard = [[InlineKeyboardButton("📊 Топ 5 funding-пар", callback_data='top')]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await context.bot.send_message(chat_id=chat_id, text=msg, reply_markup=reply_markup)

    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Ошибка при получении данных: {e}")

if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_buttons))
    app.run_polling()
