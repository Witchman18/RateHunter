import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
from pybit.unified_trading import HTTP
from dotenv import load_dotenv
from datetime import datetime, timedelta

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")

session = HTTP(api_key=BYBIT_API_KEY, api_secret=BYBIT_API_SECRET)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📊 Топ 5 funding-пар", callback_data='top')],
        [InlineKeyboardButton("⏰ Через 10 минут выплата", callback_data='soon')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Привет! Выбери действие:", reply_markup=reply_markup)

async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
        if query.data == "top":
            await top_funding(query)
        elif query.data == "soon":
            await upcoming_funding(query)
    except Exception as e:
        print(f"Ошибка в handle_buttons: {e}")
        await query.message.reply_text("❌ Произошла ошибка при обработке кнопки.")

async def top_funding(query):
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

        try:
            await query.edit_message_text(msg)
        except Exception as e:
            print(f"Ошибка при отправке топа: {e}")
            await query.message.reply_text(msg)
    except Exception as e:
        print(f"Ошибка при получении топа: {e}")
        await query.message.reply_text(f"❌ Ошибка при получении funding-ставок: {e}")

async def upcoming_funding(query):
    try:
        now = datetime.utcnow()
        now_plus_10 = now + timedelta(minutes=10)
        response = session.get_funding_rate_history(category="linear", limit=100)
        result = response["result"]["list"]

        upcoming = []

        for item in result:
            symbol = item["symbol"]
            ts = datetime.utcfromtimestamp(int(item["fundingRateTimestamp"]) / 1000)
            rate = float(item["fundingRate"])
            if now <= ts <= now_plus_10:
                upcoming.append((symbol, rate, ts))

        if not upcoming:
            result_sorted = sorted(result, key=lambda x: x["fundingRateTimestamp"])[:5]
            msg = "⚠️ Нет выплат в течение 10 минут.\n\n🕓 Ближайшие выплаты:\n\n"
            for item in result_sorted:
                symbol = item["symbol"]
                rate = float(item["fundingRate"])
                ts = datetime.utcfromtimestamp(int(item["fundingRateTimestamp"]) / 1000)
                minutes_left = int((ts - now).total_seconds() / 60)
                msg += f"{symbol} — {rate * 100:.4f}% через {minutes_left} мин\n"
        else:
            msg = "⏰ Выплаты через 10 минут:\n\n"
            for symbol, rate, ts in upcoming:
                msg += f"{symbol} — {rate * 100:.4f}% в {ts.strftime('%H:%M:%S')} UTC\n"

        try:
            await query.edit_message_text(msg)
        except Exception as e:
            print(f"Ошибка при отправке выплат: {e}")
            await query.message.reply_text(msg)
    except Exception as e:
        print(f"Ошибка при получении выплат: {e}")
        await query.message.reply_text(f"❌ Ошибка при получении выплат: {e}")

if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_buttons))
    app.run_polling()
