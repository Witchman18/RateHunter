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

# Конфигурация
BOT_TOKEN = os.getenv("BOT_TOKEN")
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")

# Инициализация
session = HTTP(api_key=BYBIT_API_KEY, api_secret=BYBIT_API_SECRET)
keyboard = [["📊 Топ 5 funding-пар"], ["📈 Расчёт прибыли"], ["📡 Сигналы"], ["🔧 Установить маржу"]]
latest_top_pairs = []
sniper_active = {}

# Состояния
SET_REAL_MARJA = 0
CALC_TEST_SUM, CALC_TEST_MARJA, CALC_PLECHO = range(1, 4)

# ===================== ОСНОВНЫЕ КОМАНДЫ =====================

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

# ===================== СИГНАЛЫ =====================

async def signal_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Меню управления сигналами (ИСПРАВЛЕННАЯ ФУНКЦИЯ)"""
    keyboard = [
        [InlineKeyboardButton("🔔 Включить сигналы", callback_data="sniper_on")],
        [InlineKeyboardButton("🔕 Выключить сигналы", callback_data="sniper_off")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("📡 Режим сигналов:", reply_markup=reply_markup)

async def signal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id

    if query.data == "sniper_on":
        sniper_active[chat_id] = True
        await query.edit_message_text("🟢 Сигналы включены")
    elif query.data == "sniper_off":
        sniper_active[chat_id] = False
        await query.edit_message_text("🔴 Сигналы выключены")

# ===================== ОСТАЛЬНЫЕ ФУНКЦИИ =====================
# ... (остальные функции остаются без изменений)

if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Обработчики команд
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("📊 Топ 5 funding-пар"), show_top_funding))
    app.add_handler(MessageHandler(filters.Regex("📡 Сигналы"), signal_menu))  # Исправленный обработчик
    app.add_handler(CallbackQueryHandler(signal_callback))

    # ... (остальные обработчики)

    async def on_startup(app):
        asyncio.create_task(funding_sniper_loop(app))

    app.post_init = on_startup
    app.run_polling()
