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
    chat_id = update.effective_chat.id
    try:
        marja = float(update.message.text.strip().replace(",", "."))
        if chat_id not in sniper_active:
            sniper_active[chat_id] = {}
        sniper_active[chat_id]["real_marja"] = marja
        await update.message.reply_text(f"✅ Маржа установлена: {marja} USDT")
    except:
        await update.message.reply_text("❌ Неверный формат маржи.")
    return ConversationHandler.END

# ===================== УСТАНОВКА ПЛЕЧА =====================

async def set_real_plecho(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⚖ Введите размер плеча:")
    return SET_PLECHO

async def save_real_plecho(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        plecho = float(update.message.text.strip().replace(",", "."))
        if chat_id not in sniper_active:
            sniper_active[chat_id] = {}
        sniper_active[chat_id]["real_plecho"] = plecho
        await update.message.reply_text(f"✅ Плечо установлено: {plecho}x")
    except:
        await update.message.reply_text("❌ Неверный формат плеча.")
    return ConversationHandler.END

# ===================== МЕНЮ СИГНАЛОВ =====================

async def signal_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    buttons = [
        [InlineKeyboardButton("Запустить снайпера", callback_data="start_sniper")],
        [InlineKeyboardButton("Остановить снайпера", callback_data="stop_sniper")]
    ]
    reply_markup = InlineKeyboardMarkup(buttons)
    await update.message.reply_text("📡 Сигналы:", reply_markup=reply_markup)

async def signal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    data = query.data

    if data == "start_sniper":
        if chat_id not in sniper_active:
            sniper_active[chat_id] = {}
        sniper_active[chat_id]['active'] = True
        await query.edit_message_text("🚀 Снайпер запущен!")
    elif data == "stop_sniper":
        if chat_id in sniper_active:
            sniper_active[chat_id]['active'] = False
        await query.edit_message_text("🛑 Снайпер остановлен.")

def get_position_direction(rate: float) -> str:
    if rate is None:
        return "NONE"
    if rate < 0:
        return "LONG"
    elif rate > 0:
        return "SHORT"
    else:
        return "NONE"

def calculate_adjusted_qty(position_size, price, qty_step, min_qty):
    """
    Возвращает округлённый объём позиции (qty), подходящий по требованиям биржи.
    """
    raw_qty = position_size / price
    adjusted_qty = raw_qty - (raw_qty % qty_step)
    adjusted_qty = round(adjusted_qty, 10)
    if adjusted_qty < min_qty:
        return None
    return adjusted_qty

# ===================== ФОНДОВЫЙ СНАЙПЕР (ФАНДИНГ-БОТ) =====================

async def funding_sniper_loop(app):
    while True:
        try:
            now_ts = datetime.utcnow().timestamp()
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

            if not latest_top_pairs:
                await asyncio.sleep(30)
                continue

            top_symbol, rate, next_ts = latest_top_pairs[0]
            minutes_left = int((next_ts / 1000 - now_ts) / 60)

            if 0 <= minutes_left <= 1:
                direction = get_position_direction(rate)
                if direction == "NONE":
                    continue

                for chat_id, data in sniper_active.items():
                    if not data.get('active'):
                        continue

                    if (data.get("last_entry_symbol") == top_symbol and data.get("last_entry_ts") == next_ts):
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

                    await app.bot.send_message(
                        chat_id,
                        f"📡 Сигнал обнаружен: {top_symbol}\n"
                        f"{'📉 SHORT' if direction == 'SHORT' else '📈 LONG'} | 📊 {rate * 100:.4f}%\n"
                        f"💼 {marja} USDT x{plecho}  |  💰 Доход: {net:.2f} USDT\n"
                        f"⏱ Вход через 1 минуту"
                    )

                    try:
                        info = session.get_instruments_info(category="linear", symbol=top_symbol)
                        filters = info["result"]["list"][0]["lotSizeFilter"]
                        min_qty = float(filters["minOrderQty"])
                        step = float(filters["qtyStep"])

                        ticker_info = session.get_tickers(category="linear", symbol=top_symbol)
                        last_price = float(ticker_info["result"]["list"][0]["lastPrice"])
                        adjusted_qty = calculate_adjusted_qty(position_size, last_price, step, min_qty)
if adjusted_qty is None:
    await app.bot.send_message(
        chat_id,
        f"⚠️ Сделка по {top_symbol} не открыта: объём меньше минимального ({min_qty})"
    )
    continue

                        try:
                            session.set_leverage(
                                category="linear",
                                symbol=top_symbol,
                                buyLeverage=str(plecho),
                                sellLeverage=str(plecho)
                            )
                        except Exception as e:
                            if "110043" in str(e):
                                await app.bot.send_message(chat_id, f"⚠️ Плечо уже установлено: {plecho}x — продолжаю сделку.")
                            else:
                                await app.bot.send_message(chat_id, f"⚠️ Не удалось установить плечо: {str(e)}")
                                continue

                        # Открытие позиции лимитным ордером (с ценой около рынка), с fallback на маркет
                        orderbook = session.get_orderbook(category="linear", symbol=top_symbol, limit=1)
                        best_bid = float(orderbook['result']['b'][0][0])
                        best_ask = float(orderbook['result']['a'][0][0])
                        open_side = "Sell" if direction == "SHORT" else "Buy"
                        open_price = best_ask if direction == "SHORT" else best_bid

                        order_resp = session.place_order(
                            category="linear",
                            symbol=top_symbol,
                            side=open_side,
                            order_type="Limit",
                            qty=adjusted_qty,
                            price=str(open_price),
                            
                        )
                        open_order_id = order_resp["result"]["orderId"]

                        # Ждем 2 секунды исполнения лимитного ордера
                        await asyncio.sleep(2)

                        # Отменяем лимитный ордер, если не исполнился полностью
                        try:
                            session.cancel_order(category="linear", symbol=top_symbol, orderId=open_order_id)
                        except Exception as e:
                            pass

                        # Проверяем исполнение лимитного ордера
                        order_info = session.get_order_history(category="linear", orderId=open_order_id)
                        order_list = order_info.get("result", {}).get("list", [])
                        cum_exec_qty_open = 0.0
                        cum_exec_value_open = 0.0
                        cum_exec_fee_open = 0.0
                        if order_list:
                            ord_data = order_list[0]
                            cum_exec_qty_open = float(ord_data.get("cumExecQty", 0))
                            cum_exec_value_open = float(ord_data.get("cumExecValue", 0))
                            cum_exec_fee_open = float(ord_data.get("cumExecFee", 0))

                        remaining_qty = adjusted_qty - cum_exec_qty_open
                        open_order_id_2 = None
                        cum_exec_qty_open2 = 0.0
                        cum_exec_value_open2 = 0.0
                        cum_exec_fee_open2 = 0.0

                        if remaining_qty > 0:
                            if remaining_qty < min_qty:
                                await app.bot.send_message(
                                    chat_id,
                                    f"⚠️ Частично исполнено {cum_exec_qty_open:.6f} из {adjusted_qty:.6f}. Остаток {remaining_qty:.6f} меньше минимума, продолжаем с позицией {cum_exec_qty_open:.6f}."
                                )
                            else:
                                order_resp2 = session.place_order(
                                    category="linear",
                                    symbol=top_symbol,
                                    side=open_side,
                                    order_type="Market",
                                    qty=remaining_qty,
                                    time_in_force="FillOrKill"
                                )
                                open_order_id_2 = order_resp2["result"]["orderId"]
                                order_info2 = session.get_order_history(category="linear", orderId=open_order_id_2)
                                order_list2 = order_info2.get("result", {}).get("list", [])
                                if order_list2:
                                    ord_data2 = order_list2[0]
                                    cum_exec_qty_open2 = float(ord_data2.get("cumExecQty", 0))
                                    cum_exec_value_open2 = float(ord_data2.get("cumExecValue", 0))
                                    cum_exec_fee_open2 = float(ord_data2.get("cumExecFee", 0))

                        opened_qty = cum_exec_qty_open + cum_exec_qty_open2

                        sniper_active[chat_id]["last_entry_symbol"] = top_symbol
                        sniper_active[chat_id]["last_entry_ts"] = next_ts

                        # Ждем до момента выплаты фандинга
                        now = datetime.utcnow().timestamp()
                        delay = (next_ts / 1000) - now
                        if delay > 0:
                            await asyncio.sleep(delay)

                        await asyncio.sleep(10)  # Ждем ещё 10 сек после выплаты

                        # Закрытие позиции после выплаты (PostOnly + reduceOnly + смещение цены)
close_side = "Buy" if direction == "SHORT" else "Sell"

# Получаем стакан и шаг цены
instrument_info = session.get_instruments_info(category="linear", symbol=top_symbol)
price_filter = instrument_info["result"]["list"][0]["priceFilter"]
tick_size = float(price_filter["tickSize"])
orderbook_close = session.get_orderbook(category="linear", symbol=top_symbol, limit=1)
best_bid_close = float(orderbook_close['result']['b'][0][0])
best_ask_close = float(orderbook_close['result']['a'][0][0])

# Смещаем цену на 0.3% и округляем по tickSize
buffer_pct = 0.003
raw_close_price = (
    best_bid_close * (1 + buffer_pct) if direction == "SHORT"
    else best_ask_close * (1 - buffer_pct)
)
close_price = round(raw_close_price / tick_size) * tick_size

try:
    close_order_resp = session.place_order(
        category="linear",
        symbol=top_symbol,
        side=close_side,
        order_type="Limit",
        qty=opened_qty,
        price=str(close_price),
        time_in_force="PostOnly",
        reduce_only=True
    )
except Exception as e:
    await app.bot.send_message(chat_id, f"❌ Ошибка при выставлении лимитного закрытия: {e}")
    close_order_resp = None

close_order_id = None
if close_order_resp and "result" in close_order_resp and "orderId" in close_order_resp["result"]:
    close_order_id = close_order_resp["result"]["orderId"]
    await asyncio.sleep(5)
    try:
        session.cancel_order(category="linear", symbol=top_symbol, orderId=close_order_id)
    except:
        pass

# Получаем историю исполнения
cum_exec_qty_close = 0.0
cum_exec_value_close = 0.0
cum_exec_fee_close = 0.0
if close_order_id:
    close_info = session.get_order_history(category="linear", orderId=close_order_id)
    close_list = close_info.get("result", {}).get("list", [])
    if close_list:
        close_data = close_list[0]
        cum_exec_qty_close = float(close_data.get("cumExecQty", 0))
        cum_exec_value_close = float(close_data.get("cumExecValue", 0))
        cum_exec_fee_close = float(close_data.get("cumExecFee", 0))

# Если не всё исполнилось — добиваем маркетом
remaining_close_qty = opened_qty - cum_exec_qty_close
if remaining_close_qty > 0:
    try:
        close_order_resp2 = session.place_order(
            category="linear",
            symbol=top_symbol,
            side=close_side,
            order_type="Market",
            qty=remaining_close_qty,
            time_in_force="FillOrKill",
            reduce_only=True
        )
        close_order_id_2 = close_order_resp2["result"]["orderId"]
        close_info2 = session.get_order_history(category="linear", orderId=close_order_id_2)
        close_list2 = close_info2.get("result", {}).get("list", [])
        if close_list2:
            close_data2 = close_list2[0]
            cum_exec_qty_close2 = float(close_data2.get("cumExecQty", 0))
            cum_exec_value_close2 = float(close_data2.get("cumExecValue", 0))
            cum_exec_fee_close2 = float(close_data2.get("cumExecFee", 0))
    except Exception as e:
        await app.bot.send_message(chat_id, f"❌ Ошибка при маркет-закрытии: {e}")
        cum_exec_qty_close2 = cum_exec_value_close2 = cum_exec_fee_close2 = 0.0
else:
    cum_exec_qty_close2 = cum_exec_value_close2 = cum_exec_fee_close2 = 0.0

                        # Рассчитываем комиссии и прибыль
                        total_fees = cum_exec_fee_open + cum_exec_fee_open2 + cum_exec_fee_close + cum_exec_fee_close2
                        total_buy_value = 0.0
                        total_sell_value = 0.0

                        if order_list:
                            if ord_data.get("side") == "Buy":
                                total_buy_value += cum_exec_value_open
                            else:
                                total_sell_value += cum_exec_value_open
                        if open_order_id_2 and order_list2:
                            if ord_data2.get("side") == "Buy":
                                total_buy_value += cum_exec_value_open2
                            else:
                                total_sell_value += cum_exec_value_open2
                        if close_list:
                            if close_data.get("side") == "Buy":
                                total_buy_value += cum_exec_value_close
                            else:
                                total_sell_value += cum_exec_value_close
                        if close_order_id_2 and close_list2:
                            if close_data2.get("side") == "Buy":
                                total_buy_value += cum_exec_value_close2
                            else:
                                total_sell_value += cum_exec_value_close2

                        price_profit = total_sell_value - total_buy_value
                        funding_profit = (cum_exec_value_open + cum_exec_value_open2) * abs(rate)
                        gross_profit = price_profit + funding_profit
                        net_profit = gross_profit - total_fees
                        roi_pct = (net_profit / marja) * 100 if marja else 0.0

                        await app.bot.send_message(
                            chat_id,
                            f"✅ Сделка завершена: {top_symbol} ({direction})\n"
                            f"💰 Грязная прибыль: {gross_profit:.2f} USDT\n"
                            f"💵 Комиссии: {total_fees:.2f} USDT\n"
                            f"💸 Чистая прибыль: {net_profit:.2f} USDT\n"
                            f"📈 ROI: {roi_pct:.2f}%"
                        )

                    except Exception as e:
                        await app.bot.send_message(
                            chat_id,
                            f"❌ Ошибка при открытии или закрытии сделки по {top_symbol}:\n{str(e)}"
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

        # Расчет количества монеты
        raw_qty = position_size / last_price

        if raw_qty < min_qty:
            await context.bot.send_message(
                chat_id,
                f"⚠️ Сделка по {symbol} не открыта: объём {raw_qty:.6f} меньше минимального ({min_qty})"
            )
            return

        # Округляем вниз по шагу
        adjusted_qty = raw_qty - (raw_qty % step)

        # Устанавливаем плечо
        try:
            session.set_leverage(
                category="linear",
                symbol=symbol,
                buyLeverage=str(plecho),
                sellLeverage=str(plecho)
            )
        except Exception as e:
            if "110043" in str(e):
                await context.bot.send_message(chat_id, f"⚠️ Плечо уже установлено: {plecho}x — продолжаю сделку.")
            else:
                await context.bot.send_message(chat_id, f"⚠️ Не удалось установить плечо: {str(e)}")

        # Открытие позиции лимитным ордером с fallback на маркет
        orderbook = session.get_orderbook(category="linear", symbol=symbol, limit=1)
        best_bid = float(orderbook['result']['b'][0][0])
        best_ask = float(orderbook['result']['a'][0][0])
        open_side = "Sell" if direction == "SHORT" else "Buy"
        open_price = best_ask if direction == "SHORT" else best_bid

        order_resp = session.place_order(
            category="linear",
            symbol=symbol,
            side=open_side,
            order_type="Limit",
            qty=adjusted_qty,
            price=str(open_price),
            time_in_force="PostOnly"
        )
        open_order_id = order_resp["result"]["orderId"]
        await asyncio.sleep(2)
        try:
            session.cancel_order(category="linear", symbol=symbol, orderId=open_order_id)
        except Exception as e:
            pass

        order_info = session.get_order_history(category="linear", orderId=open_order_id)
        order_list = order_info.get("result", {}).get("list", [])
        cum_exec_qty_open = 0.0
        if order_list:
            ord_data = order_list[0]
            cum_exec_qty_open = float(ord_data.get("cumExecQty", 0))
        remaining_qty = adjusted_qty - cum_exec_qty_open
        if remaining_qty > 0:
            if remaining_qty >= min_qty:
                session.place_order(
                    category="linear",
                    symbol=symbol,
                    side=open_side,
                    order_type="Market",
                    qty=remaining_qty,
                    time_in_force="FillOrKill"
                )
            else:
                await context.bot.send_message(
                    chat_id,
                    f"⚠️ Частично исполнено: {cum_exec_qty_open:.6f} из {adjusted_qty:.6f}. Остаток {remaining_qty:.6f} не исполнен."
                )

        opened_qty = cum_exec_qty_open + (remaining_qty if remaining_qty > 0 and remaining_qty >= min_qty else 0.0)

        await asyncio.sleep(60)
        await context.bot.send_message(
            chat_id,
            f"✅ Сделка завершена: {symbol} ({direction})\n"
            f"📦 Объём: {opened_qty:.6f} {symbol.replace('USDT', '')}"
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
