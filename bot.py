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

            funding_data.sort(key=lambda x: abs(x[1]), reverse=True)
            global latest_top_pairs
            latest_top_pairs = funding_data[:5]

            if not latest_top_pairs:
                await asyncio.sleep(30)
                continue

            top_symbol, rate, next_ts = latest_top_pairs[0]
            minutes_left = int((next_ts / 1000 - now_ts) / 60)

            if 0 <= minutes_left <= 1:
                direction = "LONG" if rate < 0 else "SHORT"

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
                    roi = (net / marja) * 100

                    # 📡 Уведомление
                    await app.bot.send_message(
                        chat_id,
                        f"📡 Сигнал обнаружен: {top_symbol}\n"
                        f"{'📈 LONG' if direction == 'LONG' else '📉 SHORT'} | 📊 {rate * 100:.4f}%\n"
                        f"💼 {marja} USDT x{plecho}  |  💰 Доход: {net:.2f} USDT\n"
                        f"⏱ Вход через 1 минуту"
                    )

                    # 🔥 Попытка открыть реальную сделку
                    try:
                        info = session.get_instruments_info(category="linear", symbol=top_symbol)
                        filters = info["result"]["list"][0]["lotSizeFilter"]
                        min_qty = float(filters["minOrderQty"])
                        step = float(filters["qtyStep"])

                        # Получаем цену и пересчитываем в qty
                        ticker_info = session.get_tickers(category="linear", symbol=top_symbol)
                        last_price = float(ticker_info["result"]["list"][0]["lastPrice"])
                        raw_qty = position_size / last_price
                        adjusted_qty = raw_qty - (raw_qty % step)

                        if adjusted_qty < min_qty:
                            await app.bot.send_message(
                                chat_id,
                                f"⚠️ Сделка по {top_symbol} не открыта: объём {adjusted_qty:.6f} меньше минимального ({min_qty})"
                            )
                            continue

                        session.place_order(
                            category="linear",
                            symbol=top_symbol,
                            side="Buy" if direction == "LONG" else "Sell",
                            order_type="Market",
                            qty=adjusted_qty,
                            time_in_force="FillOrKill"
                        )

                        await asyncio.sleep(60)
                        await app.bot.send_message(
                            chat_id,
                            f"✅ Сделка завершена: {top_symbol} ({direction})\n"
                            f"📦 Объём: {adjusted_qty:.6f} {top_symbol.replace('USDT', '')}"
                        )

                    except Exception as e:
                        await app.bot.send_message(
                            chat_id,
                            f"❌ Ошибка при открытии сделки по {top_symbol}:\n{str(e)}"
                        )

        except Exception as e:
            print(f"[Sniper Error] {e}")

        await asyncio.sleep(30)

async def test_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    # Проверяем активность и параметры
    if chat_id not in sniper_active:
        await update.message.reply_text("❌ Сначала установите маржу и плечо.")
        return

    marja = sniper_active[chat_id].get("real_marja")
    plecho = sniper_active[chat_id].get("real_plecho")
    if not marja or not plecho:
        await update.message.reply_text("❌ Сначала установите маржу и плечо.")
        return

    symbol = "SOLUSDT"  # Можешь изменить на любую пару
    direction = "LONG"
    position_size = marja * plecho

    try:
        # Получаем цену символа
        ticker_info = session.get_tickers(category="linear", symbol=symbol)
        last_price = float(ticker_info["result"]["list"][0]["lastPrice"])

        # Получаем параметры торгов для символа
        info = session.get_instruments_info(category="linear", symbol=symbol)
        filters = info["result"]["list"][0]["lotSizeFilter"]
        min_qty = float(filters["minOrderQty"])
        step = float(filters["qtyStep"])

        # Расчёт количества монеты
        raw_qty = position_size / last_price

        if raw_qty < min_qty:
            await context.bot.send_message(
                chat_id,
                f"⚠️ Сделка по {symbol} не открыта: объём {raw_qty:.6f} меньше минимального ({min_qty})"
            )
            return

        # Округляем вниз по шагу
        adjusted_qty = raw_qty - (raw_qty % step)

        # Открытие рыночного ордера
        session.place_order(
            category="linear",
            symbol=symbol,
            side="Buy" if direction == "LONG" else "Sell",
            order_type="Market",
            qty=adjusted_qty,
            time_in_force="FillOrKill"
        )

        await asyncio.sleep(60)
        await context.bot.send_message(
            chat_id,
            f"✅ Сделка завершена: {symbol} ({direction})\n"
            f"📦 Объём: {adjusted_qty:.6f} {symbol.replace('USDT', '')}"
        )

    except Exception as e:
        await context.bot.send_message(
            chat_id,
            f"❌ Ошибка при открытии сделки по {symbol}:\n{str(e)}"
        )

# ===================== MAIN =====================

if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Обработчики команд и кнопок
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("📊 Топ-пары"), show_top_funding))
    app.add_handler(MessageHandler(filters.Regex("📡 Сигналы"), signal_menu))
    app.add_handler(CallbackQueryHandler(signal_callback))
    app.add_handler(CommandHandler("test_trade", test_trade))

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
