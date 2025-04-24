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
SET_MARJA = 0  # Для установки реальной маржи

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

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена текущего действия"""
    await update.message.reply_text("Действие отменено.")
    return ConversationHandler.END

# ===================== УСТАНОВКА МАРЖИ =====================

async def set_real_marja(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало диалога установки маржи"""
    await update.message.reply_text(
        "💰 Введите сумму РЕАЛЬНОЙ маржи (в USDT) для автоматических сделок:\n"
        "(Будет проверен баланс на Bybit)"
    )
    return SET_MARJA

async def save_real_marja(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохранение маржи после проверки баланса"""
    try:
        marja = float(update.message.text)
        chat_id = update.effective_chat.id

        # Проверка баланса
        balance = session.get_wallet_balance(accountType="UNIFIED")
        usdt_balance = float(balance["result"]["list"][0]["totalEquity"])
        
        if marja > usdt_balance:
            await update.message.reply_text("❌ Недостаточно средств. Пополните баланс.")
            return ConversationHandler.END

        # Сохраняем маржу
        if chat_id not in sniper_active:
            sniper_active[chat_id] = {'active': False}
        
        sniper_active[chat_id]['real_marja'] = marja
        await update.message.reply_text(f"✅ РЕАЛЬНАЯ маржа установлена: {marja} USDT")
        return ConversationHandler.END
        
    except ValueError:
        await update.message.reply_text("❌ Ошибка. Введите число (например: 100.50)")
        return SET_MARJA
    except Exception as e:
        await update.message.reply_text(f"⚠️ Ошибка: {str(e)}")
        return ConversationHandler.END

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
        if chat_id not in sniper_active:
            sniper_active[chat_id] = {}
        sniper_active[chat_id]['active'] = True
        await query.edit_message_text("🟢 Сигналы включены")
    else:
        if chat_id not in sniper_active:
            sniper_active[chat_id] = {}
        sniper_active[chat_id]['active'] = False
        await query.edit_message_text("🔴 Сигналы выключены")

# ===================== ФОНОВАЯ ЗАДАЧА =====================

async def funding_sniper_loop(app):
    """Фоновая проверка фандинг рейтов"""
    while True:
        try:
            now_ts = datetime.utcnow().timestamp()
            response = session.get_tickers(category="linear")
            tickers = response["result"]["list"]

            for chat_id, data in sniper_active.items():
                if not data.get('active', False):
                    continue

                marja = data.get('real_marja', 0)
                if marja <= 0:
                    continue

                leverage = 5
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

    # Обработчики команд
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("📊 Топ 5 funding-пар"), show_top_funding))
    app.add_handler(MessageHandler(filters.Regex("📡 Сигналы"), signal_menu))
    app.add_handler(CallbackQueryHandler(signal_callback))

    # Обработчик установки маржи
    conv_marja = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("🔧 Установить маржу"), set_real_marja)],
        states={
            SET_MARJA: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_real_marja)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv_marja)

    # Запуск фоновой задачи
    async def on_startup(app):
        asyncio.create_task(funding_sniper_loop(app))

    app.post_init = on_startup
    print("Бот запущен...")
    app.run_polling()
