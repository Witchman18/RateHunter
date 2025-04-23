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

# Статическая комиссия (примерная)
TAKER_FEE = 0.0006
MAKER_FEE = 0.0002

# Reply-кнопки
keyboard = [["📊 Топ 5 funding-пар"], ["📈 Расчёт прибыли"]]

# Пары для расчёта
latest_top_pairs = []

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
        top_5 = funding_data[:5]
        global latest_top_pairs
        latest_top_pairs = top_5  # сохранить для расчёта

        msg = "📊 Топ 5 funding-пар:\n\n"
        from datetime import datetime
        now_ts = datetime.utcnow().timestamp()
        for symbol, rate, ts in top_5:
            delta_sec = int(ts / 1000 - now_ts)
            h, m = divmod(delta_sec // 60, 60)
            time_left = f"{h}ч {m}м"
            direction = "📈 LONG" if rate < 0 else "📉 SHORT"
            msg += f"{symbol} — {rate * 100:.4f}% → {direction} ⏱ через {time_left}\n"

        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"Ошибка при получении топа: {e}")

async def handle_profit_calc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not latest_top_pairs:
        await update.message.reply_text("Сначала получи 📊 Топ 5 funding-пар")
        return

    msg = "💰 Расчёт прибыли для каждой пары (на 100 USDT, 5x плечо):\n\n"
    for symbol, rate, _ in latest_top_pairs:
        margin = 100
        leverage = 5
        position = margin * leverage
        gross = position * rate
        fees = position * (TAKER_FEE * 2)
        spread = position * 0.0002
        net = gross - fees - spread
        msg += (f"{symbol}:\n"
                f"  📈 Грязная прибыль: {gross:.2f} USDT\n"
                f"  💸 Комиссии: {fees:.2f} USDT\n"
                f"  📉 Спред: {spread:.2f} USDT\n"
                f"  ✅ Чистая прибыль: {net:.2f} USDT\n\n")
    await update.message.reply_text(msg)

if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("📊 Топ 5 funding-пар"), show_top_funding))
    app.add_handler(MessageHandler(filters.Regex("📈 Расчёт прибыли"), handle_profit_calc))
    app.run_polling()
