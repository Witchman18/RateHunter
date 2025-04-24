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
keyboard = [
    ["📊 Топ-пары", "🧮 Калькулятор прибыли"],
    ["💰 Маржа", "⚖ Плечо"],
    ["📡 Сигналы"]
]
latest_top_pairs = []
sniper_active = {}

# Состояния
SET_MARJA = 0
SET_PLECHO = 1

# Для установки реальной маржи

# ===================== ОСНОВНЫЕ ФУНКЦИИ =====================

async def show_top_funding(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает топ-5 пар по funding rate с улучшенным оформлением"""
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

        msg = "📊 Топ пары:\n\n"
        now_ts = datetime.utcnow().timestamp()
        for symbol, rate, ts in latest_top_pairs:
            delta_sec = int(ts / 1000 - now_ts)
            h, m = divmod(delta_sec // 60, 60)
            time_left = f"{h}ч {m}м"
            direction = "📈 LONG" if rate < 0 else "📉 SHORT"

            msg += (
                f"🎟 {symbol}\n"
                f"{direction} Направление\n"
                f"💹 Фандинг: {rate * 100:.4f}%\n"
                f"⌛ Выплата через: {time_left}\n\n"
            )

        await update.message.reply_text(msg.strip())
    except Exception as e:
        await update.message.reply_text(f"Ошибка при получении топа: {e}")



async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text("Что делаем?", reply_markup=reply_markup)

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
    """Меню управления сигналами"""
    buttons = [
        [InlineKeyboardButton("🔔 Вкл", callback_data="sniper_on")],
        [InlineKeyboardButton("🔕 Выкл", callback_data="sniper_off")]
    ]
    reply_markup = InlineKeyboardMarkup(buttons)
    await update.message.reply_text("📡 Управление сигналами:", reply_markup=reply_markup)

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
    """Фоновая проверка funding rate и автоматическое открытие позиции по самой прибыльной паре"""
    while True:
        try:
            now_ts = datetime.utcnow().timestamp()

            # Получаем funding-рейты
            response = session.get_tickers(category="linear")
            tickers = response["result"]["list"]

            # Обновляем топ 5 пар по фандингу
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

            # Сортируем по абсолютной величине funding rate и берем топ-5
            funding_data.sort(key=lambda x: abs(x[1]), reverse=True)
            global latest_top_pairs
            latest_top_pairs = funding_data[:5]

            # Берем только самую первую — самую прибыльную пару
            if not latest_top_pairs:
                await asyncio.sleep(30)
                continue

            top_symbol, rate, next_ts = latest_top_pairs[0]
            minutes_left = int((next_ts / 1000 - now_ts) / 60)

            # Проверка — если выплата через 1 минуту, то можно заходить
            if 0 <= minutes_left <= 1:
                direction = "LONG" if rate < 0 else "SHORT"

                # Проверяем по всем активным пользователям
                for chat_id, data in sniper_active.items():
                    if not data.get('active'):
                        continue

                    marja = data.get('real_marja')
                    plecho = data.get('real_plecho')
                    if not marja or not plecho:
                        continue

                    position_size = marja * plecho
                    gross = position_size * abs(rate)
                    fees = position_size * 0.0006
                    spread = position_size * 0.0002
                    net = gross - fees - spread

                    # 📡 Уведомление о предстоящей сделке
                    await app.bot.send_message(
    chat_id,
    f"📡 Сигнал обнаружен: {symbol}\n"
    f"{'📈 LONG' if direction == 'LONG' else '📉 SHORT'} | 📊 {rate * 100:.4f}%\n"
    f"💼 {marja} USDT x{plecho}  |  💰 Доход: {net:.2f} USDT\n"
    f"⏱ Вход через 1 минуту"
)

                    # 🔥 Попытка открыть реальную сделку
                    try:
                        side = "Buy" if direction == "LONG" else "Sell"
                        session.place_order(
                            category="linear",
                            symbol=top_symbol,
                            side=side,
                            order_type="Market",
                            qty=round(position_size, 2),
                            time_in_force="FillOrKill"
                        )
                        await app.bot.send_message(
    chat_id,
    f"✅ Сделка завершена: {symbol} ({direction})\n"
    f"💸 Профит: {net:.2f} USDT  |  📈 ROI: {roi:.2f}%"
)

                    except Exception as e:
                        await app.bot.send_message(
                            chat_id,
                            f"❌ Ошибка при открытии сделки по {top_symbol}:\n{str(e)}"
                        )

        except Exception as e:
            print(f"[Sniper Error] {e}")

        await asyncio.sleep(30)

# ===================== MAIN =====================

if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Обработчики команд и кнопок
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("📊 Топ-пары"), show_top_funding))
    app.add_handler(MessageHandler(filters.Regex("📡 Сигналы"), signal_menu))
    app.add_handler(CallbackQueryHandler(signal_callback))

    # Установка маржи
    conv_marja = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("💰 Маржа"), set_real_marja)],
        states={
            SET_MARJA: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_real_marja)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv_marja)

    # Установка плеча
    conv_plecho = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("⚖ Плечо"), set_real_plecho)],
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
