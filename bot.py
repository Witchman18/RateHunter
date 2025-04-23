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

# ===================== ОСНОВНЫЕ ФУНКЦИИ =====================

async def show_top_funding(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает топ-5 пар по funding rate"""
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

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text("Привет! Выбери действие:", reply_markup=reply_markup)

# ===================== СИГНАЛЫ =====================

async def signal_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Меню управления сигналами"""
    buttons = [
        [InlineKeyboardButton("🔔 Включить", callback_data="sniper_on")],
        [InlineKeyboardButton("🔕 Выключить", callback_data="sniper_off")]
    ]
    await update.message.reply_text(
        "📡 Управление сигналами:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

async def signal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик кнопок сигналов"""
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id

    if query.data == "sniper_on":
        sniper_active[chat_id] = {'active': True}
        await query.edit_message_text("🟢 Сигналы включены")
    else:
        sniper_active[chat_id] = {'active': False}
        await query.edit_message_text("🔴 Сигналы выключены")

# ===================== ФОНОВАЯ ЗАДАЧА =====================

async def funding_sniper_loop(app):
    """Постоянная проверка фандинг рейтов"""
    while True:
        try:
            now_ts = datetime.utcnow().timestamp()
            response = session.get_tickers(category="linear")
            tickers = response["result"]["list"]

            for chat_id, data in sniper_active.items():
                if not data.get('active'):
                    continue

                user_marja = data.get('real_marja', 0)
                if user_marja <= 0:
                    continue

                leverage = 5
                position = user_marja * leverage

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
                            await app.bot.send_message(
                                chat_id,
                                f"📡 СИГНАЛ\n{symbol} — фандинг {rate*100:.4f}%\n"
                                f"Прибыль: {net:.2f} USDT"
                            )
                            await asyncio.sleep(60)
        except Exception as e:
            print(f"Ошибка в sniper_loop: {e}")
            await asyncio.sleep(10)
        await asyncio.sleep(30)

# ===================== ЗАПУСК БОТА =====================

if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Регистрация обработчиков
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("📊 Топ 5 funding-пар"), show_top_funding))
    app.add_handler(MessageHandler(filters.Regex("📡 Сигналы"), signal_menu))
    app.add_handler(CallbackQueryHandler(signal_callback))

    # Запуск фоновой задачи
    async def on_startup(app):
        asyncio.create_task(funding_sniper_loop(app))

    app.post_init = on_startup
    print("Бот запущен...")
    app.run_polling()
