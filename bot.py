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

# Загрузка переменных окружения из файла .env
load_dotenv()

# Получение токенов из переменных окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")

# Инициализация сессии для работы с API Bybit
session = HTTP(api_key=BYBIT_API_KEY, api_secret=BYBIT_API_SECRET)

# Клавиатура главного меню
keyboard = [["📊 Топ 5 funding-пар"], ["📈 Расчёт прибыли"], ["📡 Сигналы"], ["🔧 Установить маржу"]]

# Глобальные переменные для хранения данных
latest_top_pairs = []  # Здесь будем хранить топ-5 пар
sniper_active = {}     # Для отслеживания активных сигналов

# Состояния для ConversationHandler
CALC_SUM, CALC_MARJA, CALC_PLECHO = range(3)  # Для расчета прибыли
SET_MARJA = 0  # Для установки маржи

# ===================== ОСНОВНЫЕ КОМАНДЫ =====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start - показывает главное меню"""
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text("Привет! Выбери действие:", reply_markup=reply_markup)

async def show_top_funding(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает топ-5 funding-пар"""
    try:
        # Получаем данные о парах с Bybit
        response = session.get_tickers(category="linear")
        tickers = response["result"]["list"]
        funding_data = []

        # Обрабатываем данные о funding rate
        for t in tickers:
            symbol = t["symbol"]
            rate = t.get("fundingRate")
            next_time = t.get("nextFundingTime")
            try:
                rate = float(rate)
                funding_data.append((symbol, rate, int(next_time)))
            except:
                continue

        # Сортируем по абсолютному значению funding rate
        funding_data.sort(key=lambda x: abs(x[1]), reverse=True)
        global latest_top_pairs
        latest_top_pairs = funding_data[:5]  # Сохраняем топ-5

        # Формируем сообщение с данными
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

# ===================== РАСЧЕТ ПРИБЫЛИ =====================

async def start_calc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало диалога расчета прибыли - запрашиваем сумму"""
    await update.message.reply_text("Введите сумму для расчета потенциальной прибыли (в USDT):")
    return CALC_SUM

async def set_calc_sum(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка введенной суммы для расчета"""
    try:
        calc_sum = float(update.message.text)
        context.user_data['calc_sum'] = calc_sum  # Сохраняем в контексте
        await update.message.reply_text("Теперь введите маржу (в USDT):")
        return CALC_MARJA
    except:
        await update.message.reply_text("Некорректная сумма. Попробуйте снова.")
        return CALC_SUM

async def set_calc_marja(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка введенной маржи"""
    try:
        marja = float(update.message.text)
        context.user_data['marja'] = marja  # Сохраняем в контексте
        await update.message.reply_text("Теперь введите плечо (например, 5):")
        return CALC_PLECHO
    except:
        await update.message.reply_text("Некорректная маржа. Попробуйте снова.")
        return CALC_MARJA

async def set_calc_plecho(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка введенного плеча и вывод результатов"""
    try:
        plecho = float(update.message.text)
        calc_sum = context.user_data['calc_sum']
        marja = context.user_data['marja']
        
        if not latest_top_pairs:
            await update.message.reply_text("Сначала нажмите 📊 Топ 5 funding-пар, чтобы получить актуальные данные.")
            return ConversationHandler.END

        # Формируем сообщение с результатами
        msg = (f"📈 Расчёт прибыли по топ 5 парам\n"
               f"Сумма для расчета: {calc_sum} USDT\n"
               f"Маржа: {marja} USDT | Плечо: {plecho}x\n\n")
               
        for symbol, rate, _ in latest_top_pairs:
            position = calc_sum * plecho
            gross = position * abs(rate)
            fees = position * 0.0006
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
    except Exception as e:
        await update.message.reply_text(f"Ошибка при расчетах: {e}. Попробуйте снова.")
        return CALC_PLECHO

# ===================== УСТАНОВКА МАРЖИ =====================

async def set_real_marja(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало диалога установки маржи"""
    await update.message.reply_text("Введите сумму маржи (в USDT), которую вы хотите использовать для автоматических сделок:")
    return SET_MARJA

async def save_real_marja(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сохранение маржи в глобальную переменную"""
    try:
        marja = float(update.message.text)
        chat_id = update.effective_chat.id

        # Проверяем баланс
        balance = session.get_wallet_balance(accountType="UNIFIED")
        usdt_balance = float(balance["result"]["list"][0]["totalEquity"])
        if marja > usdt_balance:
            await update.message.reply_text("❌ Недостаточно средств. Пополните баланс.")
            return ConversationHandler.END

        # Сохраняем маржу
        if chat_id not in sniper_active:
            sniper_active[chat_id] = {}
        sniper_active[chat_id]['real_marja'] = marja
        await update.message.reply_text(f"✅ Маржа установлена: {marja} USDT")
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text("Ошибка. Убедитесь, что ввели число.")
        return SET_MARJA

# ===================== СИГНАЛЫ =====================

async def signal_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Меню управления сигналами"""
    keyboard = [
        [InlineKeyboardButton("🔔 Включить сигналы", callback_data="sniper_on")],
        [InlineKeyboardButton("🔕 Выключить сигналы", callback_data="sniper_off")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("📡 Режим сигналов:", reply_markup=reply_markup)

async def signal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатий кнопок сигналов"""
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id

    if query.data == "sniper_on":
        sniper_active[chat_id] = True
        await query.edit_message_text("🟢 Сигналы включены.")
    elif query.data == "sniper_off":
        sniper_active[chat_id] = False
        await query.edit_message_text("🔴 Сигналы выключены.")

# ===================== ОТМЕНА =====================

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена текущего диалога"""
    await update.message.reply_text("Действие отменено.")
    return ConversationHandler.END

# ===================== ФОНДОВЫЕ ЗАДАЧИ =====================

async def funding_sniper_loop(app):
    """Фоновая задача для проверки funding rate и отправки сигналов"""
    await asyncio.sleep(5)
    while True:
        try:
            now_ts = datetime.utcnow().timestamp()
            response = session.get_tickers(category="linear")
            tickers = response["result"]["list"]

            # Проверяем для каждого активного чата
            for chat_id, active in sniper_active.items():
                if not active:
                    continue

                # Получаем настройки пользователя
                marja = sniper_active[chat_id].get("real_marja", 0)
                leverage = 5
                if marja <= 0:
                    continue

                position = marja * leverage

                # Проверяем все пары
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

                    # Если до funding осталось менее 1 минуты
                    if 0 <= minutes_left <= 1:
                        gross = position * abs(rate)
                        fees = position * 0.0006
                        spread = position * 0.0002
                        net = gross - fees - spread

                        if net > 0:  # Если прибыль положительная
                            await app.bot.send_message(
                                chat_id, 
                                f"📡 СИГНАЛ\n{symbol} — фандинг {rate * 100:.4f}%\n"
                                f"Ожидаемая чистая прибыль: {net:.2f} USDT"
                            )
                            await asyncio.sleep(60)
                            await app.bot.send_message(
                                chat_id, 
                                f"✅ Сделка завершена по {symbol}\n"
                                f"Симуляция: {net:.2f} USDT прибыли"
                            )
        except Exception as e:
            print(f"[Sniper Error] {e}")
        await asyncio.sleep(60)

# ===================== ЗАПУСК БОТА =====================

if __name__ == "__main__":
    # Создаем приложение бота
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Обработчики команд
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("📊 Топ 5 funding-пар"), show_top_funding))
    app.add_handler(MessageHandler(filters.Regex("📡 Сигналы"), signal_menu))
    app.add_handler(CallbackQueryHandler(signal_callback))

    # Обработчик диалога расчета прибыли
    conv_calc = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("📈 Расчёт прибыли"), start_calc)],
        states={
            CALC_SUM: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_calc_sum)],
            CALC_MARJA: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_calc_marja)],
            CALC_PLECHO: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_calc_plecho)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv_calc)

    # Обработчик диалога установки маржи
    conv_marja = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("🔧 Установить маржу"), set_real_marja)],
        states={
            SET_MARJA: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_real_marja)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv_marja)

    # Запускаем фоновую задачу
    async def on_startup(app):
        asyncio.create_task(funding_sniper_loop(app))

    app.post_init = on_startup
    
    # Запускаем бота
    print("Бот запущен...")
    app.run_polling()
