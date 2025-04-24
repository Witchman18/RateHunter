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

# ===================== УСТАНОВКА МАРЖИ И ПЛЕЧА =====================

async def set_real_marja(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("💰 Введите сумму РЕАЛЬНОЙ маржи (в USDT):")
    return SET_MARJA

async def save_real_marja(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        marja = float(update.message.text)
        chat_id = update.effective_chat.id
        balance = session.get_wallet_balance(accountType="UNIFIED")
        usdt_balance = float(balance["result"]["list"][0]["availableBalance"])

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

# ===================== ОСНОВНАЯ ЛОГИКА ТОРГОВЛИ =====================

async def open_position(symbol: str, direction: str, marja: float, plecho: float, chat_id: int, rate: float):
    try:
        # Получаем параметры символа
        symbol_info = session.get_instruments_info(category="linear", symbol=symbol)
        if not symbol_info["result"]["list"]:
            await app.bot.send_message(chat_id, f"❌ Не удалось получить данные по {symbol}")
            return None

        filters = symbol_info["result"]["list"][0]["lotSizeFilter"]
        min_qty = float(filters["minOrderQty"])
        qty_step = float(filters["qtyStep"])

        # Рассчитываем объем позиции
        position_size = marja * plecho
        gross_profit = position_size * abs(rate)
        fees = position_size * 0.0006
        spread = position_size * 0.0002
        expected_net = gross_profit - fees - spread
        expected_roi = (expected_net / marja) * 100

        # 🔔 Уведомление о планируемой сделке
        await app.bot.send_message(
            chat_id,
            f"🔍 Анализ сделки:\n"
            f"• Пара: {symbol}\n"
            f"• Направление: {'LONG' if direction == 'LONG' else 'SHORT'}\n"
            f"• Маржа: {marja} USDT x{plecho}\n"
            f"• Объем: {position_size:.2f} USDT\n"
            f"• Ожидаемая прибыль: {expected_net:.2f} USDT\n"
            f"• ROI: {expected_roi:.2f}%"
        )

        # Проверка минимального объема
        if position_size < min_qty:
            await app.bot.send_message(
                chat_id,
                f"⚠️ Торговля невозможна:\n"
                f"Минимальный объем: {min_qty} USDT\n"
                f"Ваш объем: {position_size:.2f} USDT"
            )
            return None

        # Округление объема
        adjusted_qty = round(position_size / qty_step) * qty_step
        if adjusted_qty < min_qty:
            adjusted_qty = min_qty

        # Проверка баланса
        balance = session.get_wallet_balance(accountType="UNIFIED")
        available_balance = float(balance["result"]["list"][0]["availableBalance"])
        
        if adjusted_qty > available_balance:
            await app.bot.send_message(
                chat_id,
                f"❌ Недостаточно средств:\n"
                f"Доступно: {available_balance:.2f} USDT\n"
                f"Требуется: {adjusted_qty:.2f} USDT"
            )
            return None

        # 📢 Уведомление о начале открытия позиции
        await app.bot.send_message(
            chat_id,
            f"🔄 Открываю позицию:\n"
            f"• {symbol} {direction}\n"
            f"• Объем: {adjusted_qty:.2f} USDT"
        )

        # Открытие позиции
        order = session.place_order(
            category="linear",
            symbol=symbol,
            side="Buy" if direction == "LONG" else "Sell",
            order_type="Market",
            qty=adjusted_qty,
            time_in_force="FillOrKill",
            position_idx=0
        )

        # 💰 Уведомление об успешном открытии
        await app.bot.send_message(
            chat_id,
            f"✅ Позиция открыта:\n"
            f"• ID ордера: {order['result']['orderId']}\n"
            f"• Исполнено: {order['result']['cumExecQty']} USDT\n"
            f"• Средняя цена: {order['result']['avgPrice']}"
        )

        return {
            "symbol": symbol,
            "qty": adjusted_qty,
            "entry_price": float(order["result"]["avgPrice"]),
            "direction": direction,
            "expected_profit": expected_net
        }

    except Exception as e:
        await app.bot.send_message(
            chat_id,
            f"⛔ Ошибка открытия позиции:\n{str(e)}"
        )
        return None

async def funding_sniper_loop(app):
    """Основной цикл торговли"""
    while True:
        try:
            now_ts = datetime.utcnow().timestamp()
            response = session.get_tickers(category="linear")
            tickers = response["result"]["list"]

            # Обновляем топ пар
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

                    # Открываем позицию
                    position = await open_position(
                        symbol=top_symbol,
                        direction=direction,
                        marja=marja,
                        plecho=plecho,
                        chat_id=chat_id,
                        rate=rate
                    )

                    if position:
                        # Ждем 1 минуту (до выплаты фандинга)
                        await asyncio.sleep(60)
                        
                        # Получаем фактическую прибыль
                        pnl = session.get_closed_pnl(
                            category="linear",
                            symbol=top_symbol
                        )
                        
                        # 📈 Уведомление о результате
                        await app.bot.send_message(
                            chat_id,
                            f"📊 Итог сделки:\n"
                            f"• Пара: {top_symbol}\n"
                            f"• Ожидалось: {position['expected_profit']:.2f} USDT\n"
                            f"• Фактический PnL: {pnl['result']['list'][0]['closedPnl']} USDT\n"
                            f"• ROI: {float(pnl['result']['list'][0]['closedPnl']) / marja * 100:.2f}%"
                        )

        except Exception as e:
            print(f"Ошибка в основном цикле: {e}")
        finally:
            await asyncio.sleep(30)

# ===================== ЗАПУСК БОТА =====================

if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Установка одностороннего режима
    try:
        session.set_position_mode(category="linear", mode=0)
        print("✅ Режим позиции: Односторонний")
    except Exception as e:
        print(f"❌ Ошибка настройки режима: {e}")

    # Обработчики
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("📊 Топ-пары"), show_top_funding))
    app.add_handler(MessageHandler(filters.Regex("📡 Сигналы"), signal_menu))
    app.add_handler(CallbackQueryHandler(signal_callback))

    # Установка маржи и плеча
    conv_marja = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("💰 Маржа"), set_real_marja)],
        states={SET_MARJA: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_real_marja)]},
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    app.add_handler(conv_marja)

    conv_plecho = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("⚖ Плечо"), set_real_plecho)],
        states={SET_PLECHO: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_real_plecho)]},
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    app.add_handler(conv_plecho)

    # Запуск фоновой задачи
    async def on_startup(app):
        asyncio.create_task(funding_sniper_loop(app))

    app.post_init = on_startup
    print("Бот запущен...")
    app.run_polling()
