# --- START OF FILE bot (7).py ---

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
            except (ValueError, TypeError): # Лучше ловить конкретные ошибки
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
        if marja <= 0:
             await update.message.reply_text("❌ Маржа должна быть положительным числом.")
             return ConversationHandler.END
        if chat_id not in sniper_active:
            sniper_active[chat_id] = {}
        sniper_active[chat_id]["real_marja"] = marja
        await update.message.reply_text(f"✅ Маржа установлена: {marja} USDT")
    except ValueError:
        await update.message.reply_text("❌ Неверный формат маржи. Введите число.")
    return ConversationHandler.END

# ===================== УСТАНОВКА ПЛЕЧА =====================

async def set_real_plecho(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⚖ Введите размер плеча:")
    return SET_PLECHO

async def save_real_plecho(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        plecho = float(update.message.text.strip().replace(",", "."))
        if plecho <= 0:
             await update.message.reply_text("❌ Плечо должно быть положительным числом.")
             return ConversationHandler.END
        if chat_id not in sniper_active:
            sniper_active[chat_id] = {}
        sniper_active[chat_id]["real_plecho"] = plecho
        await update.message.reply_text(f"✅ Плечо установлено: {plecho}x")
    except ValueError:
        await update.message.reply_text("❌ Неверный формат плеча. Введите число.")
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
    if price <= 0: # Добавлена проверка деления на ноль или отрицательную цену
        return None
    raw_qty = position_size / price
    # Округление ВНИЗ до шага qty_step
    adjusted_qty = (raw_qty // qty_step) * qty_step
    # Округляем до разумного количества знаков после запятой, чтобы избежать проблем с float
    adjusted_qty = round(adjusted_qty, 8)
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
                    rate = float(rate) if rate else 0.0 # Обработка None или пустой строки
                    funding_data.append((symbol, rate, int(next_time)))
                except (ValueError, TypeError):
                    continue

            funding_data.sort(key=lambda x: abs(x[1]), reverse=True)
            global latest_top_pairs
            latest_top_pairs = funding_data[:5]

            if not latest_top_pairs:
                await asyncio.sleep(30)
                continue

            top_symbol, rate, next_ts = latest_top_pairs[0]
            minutes_left = int((next_ts / 1000 - now_ts) / 60)

            if 0 <= minutes_left <= 1: # Вход за 0-1 минуту до фандинга
                direction = get_position_direction(rate)
                if direction == "NONE":
                    continue

                for chat_id, data in sniper_active.items():
                    if not data.get('active'):
                        continue

                    # Пропускаем, если уже входили по этой паре в этот фандинг-период
                    if (data.get("last_entry_symbol") == top_symbol and
                            data.get("last_entry_ts") == next_ts):
                        continue

                    marja = data.get('real_marja')
                    plecho = data.get('real_plecho')
                    if not marja or not plecho:
                        await app.bot.send_message(
                            chat_id,
                            f"⚠️ Пропуск {top_symbol}: Маржа ({marja}) или плечо ({plecho}) не установлены."
                        )
                        continue

                    position_size = marja * plecho
                    gross = position_size * abs(rate)
                    fees = position_size * 0.0006 * 2 # Учитываем открытие и закрытие
                    # Спред - вещь непредсказуемая, лучше считать по факту
                    # net = gross - fees
                    # roi = (net / marja) * 100

                    await app.bot.send_message(
                        chat_id,
                        f"📡 Сигнал обнаружен: {top_symbol}\n"
                        f"{'📉 SHORT' if direction == 'SHORT' else '📈 LONG'} | 📊 {rate * 100:.4f}%\n"
                        f"💼 {marja:.2f} USDT x{plecho} | Расчетный размер: {position_size:.2f} USDT\n"
                        # f"💰 Расчетный доход (без спреда): {net:.2f} USDT ({roi:.2f}%)\n"
                        f"⏱ Вход через ~{minutes_left} мин."
                    )

                    # ==================== НАЧАЛО БЛОКА СДЕЛКИ ====================
                    try:
                        # Получаем инфо по инструменту
                        info = session.get_instruments_info(category="linear", symbol=top_symbol)
                        filters = info["result"]["list"][0]["lotSizeFilter"]
                        min_qty = float(filters["minOrderQty"])
                        step = float(filters["qtyStep"])
                        price_filter = info["result"]["list"][0]["priceFilter"]
                        tick_size = float(price_filter["tickSize"])


                        # Получаем цену
                        ticker_info = session.get_tickers(category="linear", symbol=top_symbol)
                        last_price = float(ticker_info["result"]["list"][0]["lastPrice"])

                        # Расчитываем и корректируем кол-во
                        adjusted_qty = calculate_adjusted_qty(position_size, last_price, step, min_qty)

                        # ---- ИСПРАВЛЕНО ЗДЕСЬ (Отступы) ----
                        if adjusted_qty is None:
                            await app.bot.send_message(
                                chat_id,
                                f"⚠️ Сделка по {top_symbol} не открыта: расчетный объём ({position_size / last_price:.6f}) меньше минимального ({min_qty}) после округления."
                            )
                            continue
                        # -------------------------------------

                        # Устанавливаем плечо
                        try:
                            session.set_leverage(
                                category="linear",
                                symbol=top_symbol,
                                buyLeverage=str(plecho),
                                sellLeverage=str(plecho)
                            )
                        except Exception as e:
                            if "110043" in str(e): # Leverage not modified
                                await app.bot.send_message(chat_id, f"ℹ️ Плечо {plecho}x для {top_symbol} уже установлено.")
                            else:
                                await app.bot.send_message(chat_id, f"⚠️ Не удалось установить плечо для {top_symbol}: {str(e)}")
                                continue # Пропускаем сделку, если не удалось установить плечо

                        # --- Открытие позиции ---
                        orderbook = session.get_orderbook(category="linear", symbol=top_symbol, limit=1)
                        best_bid = float(orderbook['result']['b'][0][0])
                        best_ask = float(orderbook['result']['a'][0][0])
                        open_side = "Sell" if direction == "SHORT" else "Buy"
                        # Цена для лимитного ордера (чуть хуже рыночной, чтобы быстрее исполнился)
                        open_price = best_ask if open_side == "Buy" else best_bid

                        await app.bot.send_message(chat_id, f"⏱ Открываю {direction} {adjusted_qty} {top_symbol}...")

                        open_order_resp = None
                        open_order_id = None
                        cum_exec_qty_open = 0.0
                        cum_exec_value_open = 0.0
                        cum_exec_fee_open = 0.0

                        # Попытка открыть лимитным ордером
                        try:
                            open_order_resp = session.place_order(
                                category="linear",
                                symbol=top_symbol,
                                side=open_side,
                                order_type="Limit",
                                qty=str(adjusted_qty), # Передаем как строку
                                price=str(open_price),
                                time_in_force="GoodTillCancel" # Или другое подходящее
                            )
                            open_order_id = open_order_resp["result"]["orderId"]
                            await app.bot.send_message(chat_id, f"⏳ Лимитный ордер {open_order_id} на открытие размещен.")
                            await asyncio.sleep(3) # Даем время на исполнение
                        except Exception as e:
                             await app.bot.send_message(chat_id, f"⚠️ Ошибка размещения лимитного ордера: {e}")

                        # Проверка и отмена неисполненного остатка лимитки
                        if open_order_id:
                            try:
                                order_info = session.get_order_history(category="linear", orderId=open_order_id, limit=1)
                                order_list = order_info.get("result", {}).get("list", [])
                                if order_list:
                                    ord_data = order_list[0]
                                    cum_exec_qty_open = float(ord_data.get("cumExecQty", 0))
                                    cum_exec_value_open = float(ord_data.get("cumExecValue", 0))
                                    cum_exec_fee_open = float(ord_data.get("cumExecFee", 0))
                                    order_status = ord_data.get("orderStatus")
                                    # Отменяем, если не исполнился полностью
                                    if order_status not in ["Filled", "Cancelled", "Rejected"]:
                                        try:
                                            session.cancel_order(category="linear", symbol=top_symbol, orderId=open_order_id)
                                            await app.bot.send_message(chat_id, f"↪️ Лимитный ордер {open_order_id} отменен (исполнено {cum_exec_qty_open}).")
                                        except Exception as cancel_e:
                                            # Возможно, он уже исполнился/отменился пока мы спали
                                             if "Order does not exist" not in str(cancel_e) and "already been filled" not in str(cancel_e):
                                                await app.bot.send_message(chat_id, f"⚠️ Ошибка отмены лимитного ордера {open_order_id}: {cancel_e}")
                                else:
                                     await app.bot.send_message(chat_id, f"⚠️ Не удалось получить информацию по ордеру {open_order_id}")

                            except Exception as e:
                                await app.bot.send_message(chat_id, f"⚠️ Ошибка проверки/отмены лимитного ордера {open_order_id}: {e}")

                        # Добиваем маркетом, если нужно
                        remaining_qty = round(adjusted_qty - cum_exec_qty_open, 8) # Округляем разницу
                        open_order_id_2 = None
                        cum_exec_qty_open2 = 0.0
                        cum_exec_value_open2 = 0.0
                        cum_exec_fee_open2 = 0.0

                        if remaining_qty >= min_qty: # Сравниваем с минимальным объемом
                            await app.bot.send_message(chat_id, f"🛒 Добиваю {remaining_qty} {top_symbol} маркетом...")
                            try:
                                order_resp2 = session.place_order(
                                    category="linear",
                                    symbol=top_symbol,
                                    side=open_side,
                                    order_type="Market",
                                    qty=str(remaining_qty), # Передаем как строку
                                    time_in_force="FillOrKill" # Или ImmediateOrCancel
                                )
                                open_order_id_2 = order_resp2["result"]["orderId"]
                                # Даем время на обработку маркет ордера
                                await asyncio.sleep(2)
                                order_info2 = session.get_order_history(category="linear", orderId=open_order_id_2, limit=1)
                                order_list2 = order_info2.get("result", {}).get("list", [])
                                if order_list2:
                                    ord_data2 = order_list2[0]
                                    # Убедимся что ордер исполнился
                                    if ord_data2.get("orderStatus") == "Filled":
                                        cum_exec_qty_open2 = float(ord_data2.get("cumExecQty", 0))
                                        cum_exec_value_open2 = float(ord_data2.get("cumExecValue", 0))
                                        cum_exec_fee_open2 = float(ord_data2.get("cumExecFee", 0))
                                        await app.bot.send_message(chat_id, f"✅ Маркет ордер {open_order_id_2} исполнен ({cum_exec_qty_open2}).")
                                    else:
                                        await app.bot.send_message(chat_id, f"⚠️ Маркет ордер {open_order_id_2} не исполнился полностью (статус {ord_data2.get('orderStatus')}).")

                            except Exception as e:
                                await app.bot.send_message(chat_id, f"❌ Ошибка размещения маркет-ордера на открытие: {e}")
                        elif remaining_qty > 0:
                             await app.bot.send_message(chat_id, f"ℹ️ Остаток {remaining_qty} меньше минимального ({min_qty}), не добиваем.")


                        opened_qty = round(cum_exec_qty_open + cum_exec_qty_open2, 8)

                        if opened_qty < min_qty:
                             await app.bot.send_message(chat_id, f"❌ Не удалось открыть позицию {top_symbol} на минимальный объем ({min_qty}). Исполнено: {opened_qty}.")
                             continue # Переходим к следующему чату/итерации

                        await app.bot.send_message(chat_id, f"✅ Позиция {direction} {opened_qty} {top_symbol} открыта.")

                        # Запоминаем вход
                        sniper_active[chat_id]["last_entry_symbol"] = top_symbol
                        sniper_active[chat_id]["last_entry_ts"] = next_ts

                        # --- Ожидание выплаты фандинга ---
                        now = datetime.utcnow().timestamp()
                        funding_time_sec = next_ts / 1000
                        delay = funding_time_sec - now
                        if delay > 0:
                            await app.bot.send_message(chat_id, f"⏳ Жду выплаты фандинга ({delay:.0f} сек)...")
                            await asyncio.sleep(delay)

                        await asyncio.sleep(15)  # Ждем ещё 15 сек после времени выплаты на всякий случай

                        await app.bot.send_message(chat_id, f"⏱ Закрываю позицию {top_symbol}...")

                        # --- Закрытие позиции ---
                        # ---- ИСПРАВЛЕНО ЗДЕСЬ (Отступы всего блока закрытия) ----
                        close_side = "Buy" if direction == "SHORT" else "Sell"

                        # Попытка закрыть лимитным ордером (PostOnly, reduceOnly)
                        close_order_id = None
                        cum_exec_qty_close = 0.0
                        cum_exec_value_close = 0.0
                        cum_exec_fee_close = 0.0

                        try:
                            # Получаем стакан для цены закрытия
                            orderbook_close = session.get_orderbook(category="linear", symbol=top_symbol, limit=1)
                            best_bid_close = float(orderbook_close['result']['b'][0][0])
                            best_ask_close = float(orderbook_close['result']['a'][0][0])

                            # Ставим цену чуть агрессивнее для PostOnly, чтобы быть мейкером
                            # Если закрываем SHORT (делаем BUY), ставим на best_bid
                            # Если закрываем LONG (делаем SELL), ставим на best_ask
                            raw_close_price = best_bid_close if close_side == "Buy" else best_ask_close
                            # Округляем по tick_size
                            close_price = round(raw_close_price / tick_size) * tick_size

                            close_order_resp = session.place_order(
                                category="linear",
                                symbol=top_symbol,
                                side=close_side,
                                order_type="Limit",
                                qty=str(opened_qty), # Весь открытый объем
                                price=str(close_price),
                                time_in_force="PostOnly", # Только мейкер
                                reduce_only=True # Только закрытие
                            )
                            close_order_id = close_order_resp["result"]["orderId"]
                            await app.bot.send_message(chat_id, f"⏳ Лимитный ордер {close_order_id} на закрытие (PostOnly) размещен.")
                            await asyncio.sleep(5) # Даем время на исполнение

                        except Exception as e:
                            await app.bot.send_message(chat_id, f"⚠️ Ошибка размещения лимитного ордера (PostOnly) на закрытие: {e}")
                            # Если PostOnly не прошел (например, цена ушла), можно попробовать обычный лимит или сразу маркет

                        # Проверка и отмена неисполненного остатка лимитки
                        if close_order_id:
                            try:
                                order_info_close = session.get_order_history(category="linear", orderId=close_order_id, limit=1)
                                order_list_close = order_info_close.get("result", {}).get("list", [])
                                if order_list_close:
                                    close_data = order_list_close[0]
                                    cum_exec_qty_close = float(close_data.get("cumExecQty", 0))
                                    cum_exec_value_close = float(close_data.get("cumExecValue", 0))
                                    cum_exec_fee_close = float(close_data.get("cumExecFee", 0))
                                    order_status_close = close_data.get("orderStatus")

                                    if order_status_close not in ["Filled", "Cancelled", "Rejected"]:
                                        try:
                                            session.cancel_order(category="linear", symbol=top_symbol, orderId=close_order_id)
                                            await app.bot.send_message(chat_id, f"↪️ Лимитный ордер закрытия {close_order_id} отменен (исполнено {cum_exec_qty_close}).")
                                        except Exception as cancel_e:
                                            if "Order does not exist" not in str(cancel_e) and "already been filled" not in str(cancel_e):
                                                await app.bot.send_message(chat_id, f"⚠️ Ошибка отмены лимитного ордера закрытия {close_order_id}: {cancel_e}")
                                else:
                                     await app.bot.send_message(chat_id, f"⚠️ Не удалось получить информацию по ордеру закрытия {close_order_id}")

                            except Exception as e:
                                await app.bot.send_message(chat_id, f"⚠️ Ошибка проверки/отмены лимитного ордера закрытия {close_order_id}: {e}")


                        # Добиваем маркетом, если не всё закрылось лимиткой
                        remaining_close_qty = round(opened_qty - cum_exec_qty_close, 8)
                        close_order_id_2 = None
                        cum_exec_qty_close2 = 0.0
                        cum_exec_value_close2 = 0.0
                        cum_exec_fee_close2 = 0.0

                        if remaining_close_qty >= min_qty: # Сравниваем с минимальным объемом
                            await app.bot.send_message(chat_id, f"🛒 Закрываю остаток {remaining_close_qty} {top_symbol} маркетом...")
                            try:
                                close_order_resp2 = session.place_order(
                                    category="linear",
                                    symbol=top_symbol,
                                    side=close_side,
                                    order_type="Market",
                                    qty=str(remaining_close_qty),
                                    time_in_force="FillOrKill", # Или ImmediateOrCancel
                                    reduce_only=True
                                )
                                close_order_id_2 = close_order_resp2["result"]["orderId"]
                                await asyncio.sleep(2) # Даем время на обработку
                                close_info2 = session.get_order_history(category="linear", orderId=close_order_id_2, limit=1)
                                close_list2 = close_info2.get("result", {}).get("list", [])
                                if close_list2:
                                    close_data2 = close_list2[0]
                                    if close_data2.get("orderStatus") == "Filled":
                                        cum_exec_qty_close2 = float(close_data2.get("cumExecQty", 0))
                                        cum_exec_value_close2 = float(close_data2.get("cumExecValue", 0))
                                        cum_exec_fee_close2 = float(close_data2.get("cumExecFee", 0))
                                        await app.bot.send_message(chat_id, f"✅ Маркет ордер закрытия {close_order_id_2} исполнен ({cum_exec_qty_close2}).")

                                    else:
                                         await app.bot.send_message(chat_id, f"⚠️ Маркет ордер закрытия {close_order_id_2} не исполнился полностью (статус {close_data2.get('orderStatus')}).")
                            except Exception as e:
                                await app.bot.send_message(chat_id, f"❌ Ошибка при маркет-закрытии: {e}")
                        elif remaining_close_qty > 0:
                             await app.bot.send_message(chat_id, f"ℹ️ Остаток для закрытия {remaining_close_qty} меньше минимального ({min_qty}).")

                        closed_qty = round(cum_exec_qty_close + cum_exec_qty_close2, 8)

                        if closed_qty < opened_qty * 0.99: # Если закрыли менее 99% от открытого
                             await app.bot.send_message(chat_id, f"⚠️ Позиция {top_symbol} закрыта не полностью! Открыто: {opened_qty}, Закрыто: {closed_qty}. Проверьте вручную!")
                        else:
                             await app.bot.send_message(chat_id, f"✅ Позиция {top_symbol} успешно закрыта ({closed_qty}).")


                        # --- Расчет и вывод результата ---
                        total_fees = cum_exec_fee_open + cum_exec_fee_open2 + cum_exec_fee_close + cum_exec_fee_close2
                        # PNL от изменения цены: (средняя цена продажи * кол-во) - (средняя цена покупки * кол-во)
                        # или общая стоимость продажи - общая стоимость покупки
                        total_buy_value = 0.0
                        total_sell_value = 0.0
                        total_buy_qty = 0.0
                        total_sell_qty = 0.0

                        # Аккумулируем стоимость и количество по ордерам открытия
                        if open_side == "Buy":
                            total_buy_value += cum_exec_value_open + cum_exec_value_open2
                            total_buy_qty += cum_exec_qty_open + cum_exec_qty_open2
                        else: # Sell
                            total_sell_value += cum_exec_value_open + cum_exec_value_open2
                            total_sell_qty += cum_exec_qty_open + cum_exec_qty_open2

                        # Аккумулируем стоимость и количество по ордерам закрытия
                        if close_side == "Buy":
                            total_buy_value += cum_exec_value_close + cum_exec_value_close2
                            total_buy_qty += cum_exec_qty_close + cum_exec_qty_close2
                        else: # Sell
                            total_sell_value += cum_exec_value_close + cum_exec_value_close2
                            total_sell_qty += cum_exec_qty_close + cum_exec_qty_close2

                        # PNL от цены: Продал дороже чем купил (для лонга) или купил дешевле чем продал (для шорта)
                        price_pnl = total_sell_value - total_buy_value

                        # PNL от фандинга (приблизительно, т.к. точный расчет сложнее)
                        # (Объем позиции в USDT на момент фандинга) * funding_rate
                        # Берем стоимость открытия как оценку объема
                        funding_pnl_approx = (cum_exec_value_open + cum_exec_value_open2) * rate * (-1 if direction == "SHORT" else 1) # Фандинг платится или получается

                        net_profit = price_pnl + funding_pnl_approx - total_fees
                        roi_pct = (net_profit / marja) * 100 if marja else 0.0

                        await app.bot.send_message(
                            chat_id,
                            f"📊 Результат сделки: {top_symbol} ({direction})\n"
                            f" Открыто: {opened_qty:.6f} | Закрыто: {closed_qty:.6f}\n"
                            f" PNL (цена): {price_pnl:.4f} USDT\n"
                            f" PNL (фандинг, прибл.): {funding_pnl_approx:.4f} USDT\n"
                            f" Комиссии: {total_fees:.4f} USDT\n"
                            f"💰 Чистая прибыль: {net_profit:.4f} USDT\n"
                            f"📈 ROI: {roi_pct:.2f}% (от маржи {marja:.2f} USDT)"
                        )
                        # ----------------------------------------------------------

                    # ==================== КОНЕЦ БЛОКА СДЕЛКИ =====================
                    except Exception as trade_e:
                        await app.bot.send_message(
                            chat_id,
                            f"❌ КРИТИЧЕСКАЯ ОШИБКА во время сделки по {top_symbol}:\n{str(trade_e)}\n"
                            f"Проверьте состояние позиции и ордеров вручную!"
                        )
                        # Можно добавить логирование ошибки для отладки
                        print(f"[Trade Error] Chat {chat_id}, Symbol {top_symbol}: {trade_e}")
                        import traceback
                        traceback.print_exc()


        except Exception as loop_e:
            print(f"[Sniper Loop Error] {loop_e}")
            import traceback
            traceback.print_exc()
            # Не отправляем сообщение пользователю об ошибке цикла, чтобы не спамить

        await asyncio.sleep(30) # Пауза перед следующей проверкой

# ===================== Тестовая функция (НЕ ИСПОЛЬЗОВАТЬ В ПРОДЕ) =====================
# Эта функция была для отладки, она содержит много дублирования кода
# и не рекомендуется к использованию в текущем виде.
# Оставил ее закомментированной на случай, если она нужна для справки.
# async def test_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     chat_id = update.effective_chat.id
#     # ... (остальной код функции test_trade) ...
#     pass


# ===================== MAIN =====================

if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Обработчики команд и кнопок
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("📊 Топ-пары"), show_top_funding))
    app.add_handler(MessageHandler(filters.Regex("📡 Сигналы"), signal_menu))
    app.add_handler(CallbackQueryHandler(signal_callback))
    # app.add_handler(CommandHandler("test_trade", test_trade)) # Закомментировал тестовую команду

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
    async def on_startup(passed_app): # Используем другое имя переменной
        print("Starting background sniper loop...")
        asyncio.create_task(funding_sniper_loop(passed_app))
        print("Sniper loop task created.")

    app.post_init = on_startup
    print("Starting bot polling...")
    app.run_polling()
    print("Bot polling stopped.")

# --- END OF FILE bot (7).py ---
