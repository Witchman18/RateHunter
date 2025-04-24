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
keyboard = [["📊 Топ 5 funding-пар"], ["📈 Расчёт прибыли"], ["📡 Сигналы"], ["🔧 Установить маржу"], ["📐 Установить плечо"]]
latest_top_pairs = []
sniper_active = {}

# Состояния
SET_MARJA = 0
SET_PLECHO = 1

# Для установки реальной маржи

# ===================== ОСНОВНЫЕ ФУНКЦИИ =====================

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
            msg += (
                f"🔹 {symbol}\n"
                f"   📊 Фандинг: {rate * 100:.4f}%\n"
                f"   🧭 Направление: {direction}\n"
                f"   ⏱ Выплата через: {time_left}\n\n"
            )

        await update.message.reply_text(msg)

    except Exception as e:
        await update.message.reply_text(f"Ошибка при получении топа: {e}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text("Привет! Выбери действие:", reply_markup=reply_markup)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Действие отменено.")
    return ConversationHandler.END

# ===================== УСТАНОВКА МАРЖИ =====================

async def set_real_marja(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("💰 Введите сумму РЕАЛЬНОЙ маржи (в USDT):")
    return SET_MARJA

async def save_real_marja(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        marja = float(update.message.text)
        chat_id = update.effective_chat.id
        balance = session.get_wallet_balance(accountType="UNIFIED")
        usdt_balance = float(balance["result"]["list"][0]["totalEquity"])

        if marja > usdt_balance:
            await update.message.reply_text("❌ Недостаточно средств.")
            return ConversationHandler.END

        if chat_id not in sniper_active:
            sniper_active[chat_id] = {"active": False}
        sniper_active[chat_id]["real_marja"] = marja
        await update.message.reply_text(f"✅ Маржа установлена: {marja} USDT")
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"⚠️ Ошибка: {str(e)}")
        return ConversationHandler.END
        
# ===================== УСТАНОВКА ПЛЕЧА =====================

async def set_real_plecho(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📐 Введите плечо (например: 5):")
    return SET_PLECHO

async def save_real_plecho(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        plecho = float(update.message.text)
        chat_id = update.effective_chat.id

        if chat_id not in sniper_active:
            sniper_active[chat_id] = {}

        sniper_active[chat_id]['real_plecho'] = plecho
        await update.message.reply_text(f"✅ Плечо установлено: {plecho}x")
        return ConversationHandler.END

    except ValueError:
        await update.message.reply_text("❌ Ошибка. Введите число (например: 5)")
        return SET_PLECHO

# ===================== СИГНАЛЫ =====================

async def signal_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    buttons = [
        [InlineKeyboardButton("🔔 Включить", callback_data="sniper_on")],
        [InlineKeyboardButton("🔕 Выключить", callback_data="sniper_off")]
    ]
    await update.message.reply_text("📡 Управление сигналами:", reply_markup=InlineKeyboardMarkup(buttons))

async def signal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    if query.data == "sniper_on":
        sniper_active[chat_id] = sniper_active.get(chat_id, {})
        sniper_active[chat_id]["active"] = True
        await query.edit_message_text("🟢 Сигналы включены")
    else:
        sniper_active[chat_id] = sniper_active.get(chat_id, {})
        sniper_active[chat_id]["active"] = False
        await query.edit_message_text("🔴 Сигналы выключены")

# ===================== СНАИПЕР =====================
# ===================== ФОНОВАЯ ЗАДАЧА =====================

async def funding_sniper_loop(app):
    """Фоновая проверка фандинг рейтов и отправка сигналов"""
    last_signal_time = {}  # Хранилище последних сигналов по паре и чату

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

                # Получаем топ 1 по фандингу
                funding_data = []
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
                        funding_data.append((symbol, rate, next_ts, minutes_left))
                    except:
                        continue

                # Сортируем по доходности
                funding_data.sort(key=lambda x: abs(x[1]), reverse=True)

                if not funding_data:
                    continue

                # Берём только 1 топовую
                top_symbol, rate, next_ts, minutes_left = funding_data[0]
                direction = "LONG" if rate < 0 else "SHORT"

                if 0 <= minutes_left <= 1:
                    key = f"{chat_id}_{top_symbol}"
                    now_min = int(now_ts // 60)

                    if last_signal_time.get(key) == now_min:
                        continue  # Уже отправляли в эту минуту

                    last_signal_time[key] = now_min

                    # Расчёт прибыли
                    gross = position * abs(rate)
                    fees = position * 0.0006
                    spread = position * 0.0002
                    net = gross - fees - spread

                    # Отправляем сигнал
                    await app.bot.send_message(
                        chat_id,
                        f"📡 СИГНАЛ: вход через 1 минуту\n"
                        f"{top_symbol} ({direction}) — {rate * 100:.4f}%\n"
                        f"Ожидаемая прибыль: {net:.2f} USDT"
                    )

                    # Симуляция закрытия через 60 сек
                    await asyncio.sleep(60)
                    await app.bot.send_message(
                        chat_id,
                        f"✅ Сделка завершена по {top_symbol}, прибыль: {net:.2f} USDT"
                    )

        except Exception as e:
            print(f"[Sniper Error] {e}")
            await asyncio.sleep(10)

        await asyncio.sleep(30)

# ===================== MAIN =====================

if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Обработчики команд и кнопок
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("📊 Топ 5 funding-пар"), show_top_funding))
    app.add_handler(MessageHandler(filters.Regex("📡 Сигналы"), signal_menu))
    app.add_handler(CallbackQueryHandler(signal_callback))

    # Установка маржи
    conv_marja = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("🔧 Установить маржу"), set_real_marja)],
        states={
            SET_MARJA: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_real_marja)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv_marja)

    # Установка плеча
    conv_plecho = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("📐 Установить плечо"), set_real_plecho)],
        states={
            SET_PLECHO: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_real_plecho)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv_plecho)

    # Запуск фоновой задачи (фандинг-бот)
    async def on_startup(app):
        asyncio.create_task(funding_sniper_loop(app))

    app.post_init = on_startup
    app.run_polling()
