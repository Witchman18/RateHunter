import os
import asyncio
import time # Импортируем time для работы с timestamp
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN # Используем Decimal для точности

from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes,
    ConversationHandler, CallbackQueryHandler, filters
)
from pybit.unified_trading import HTTP
# Убедись, что pybit последней версии: pip install -U pybit
# Или используй from pybit.exceptions import InvalidRequestError и т.д. для обработки ошибок API

from dotenv import load_dotenv

load_dotenv()

# Конфигурация
BOT_TOKEN = os.getenv("BOT_TOKEN")
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")

# Инициализация
session = HTTP(api_key=BYBIT_API_KEY, api_secret=BYBIT_API_SECRET, recv_window=20000) # Увеличим окно ожидания ответа
# === Возвращаем эмодзи ===
keyboard = [
    ["📊 Топ-пары", "🧮 Калькулятор прибыли"], # Калькулятор пока не реализован
    ["💰 Маржа", "⚖ Плечо"],
    ["📡 Сигналы"]
]
latest_top_pairs = []
sniper_active = {} # Словарь для хранения состояния по каждому чату

# Состояния для ConversationHandler
SET_MARJA = 0
SET_PLECHO = 1

# Константы для стратегии
ENTRY_WINDOW_START_SECONDS = 25 # За сколько секунд ДО фандинга начинаем пытаться войти
ENTRY_WINDOW_END_SECONDS = 10  # За сколько секунд ДО фандинга прекращаем попытки входа
POST_FUNDING_WAIT_SECONDS = 15 # Сколько секунд ждем ПОСЛЕ времени фандинга перед выходом
MAKER_ORDER_WAIT_SECONDS_ENTRY = 2 # Сколько секунд ждем исполнения PostOnly ордера на ВХОД
MAKER_ORDER_WAIT_SECONDS_EXIT = 5  # Сколько секунд ждем исполнения PostOnly ордера на ВЫХОД
SNIPER_LOOP_INTERVAL_SECONDS = 5 # Как часто проверяем тикеры в основном цикле

# ===================== ОСНОВНЫЕ ФУНКЦИИ =====================

async def show_top_funding(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает топ-5 пар по funding rate с улучшенным оформлением.
       Работает и для MessageHandler, и для CallbackQueryHandler.
    """
    query = update.callback_query
    message = update.message

    chat_id = update.effective_chat.id
    loading_message_id = None # ID сообщения "Загрузка..." для последующего редактирования

    try:
        # Определяем, как отправить/отредактировать сообщение "Загрузка..."
        if query:
            # Если это callback от inline-кнопки, редактируем существующее сообщение
            await query.answer() # Отвечаем на callback, чтобы кнопка перестала "грузиться"
            # Используем try-except на случай, если сообщение уже было удалено или изменено
            try:
                await query.edit_message_text("🔄 Получаю топ пар...")
                loading_message_id = query.message.message_id # Запоминаем ID для след. редактирования
            except Exception as edit_err:
                print(f"Error editing message on callback: {edit_err}")
                # Если редактировать не вышло, попробуем отправить новое
                sent_message = await context.bot.send_message(chat_id, "🔄 Получаю топ пар...")
                loading_message_id = sent_message.message_id
        elif message:
            # Если это обычное сообщение от кнопки, отправляем новое сообщение
            sent_message = await message.reply_text("🔄 Получаю топ пар...")
            loading_message_id = sent_message.message_id # Запоминаем ID для редактирования
        else:
            print("Error: show_top_funding called without message or query.")
            return

        # Получаем данные с биржи
        response = session.get_tickers(category="linear")
        tickers = response.get("result", {}).get("list", [])
        if not tickers:
            result_msg = "⚠️ Не удалось получить данные тикеров."
            # Пытаемся отредактировать сообщение "Загрузка..." на сообщение об ошибке
            if loading_message_id:
                 await context.bot.edit_message_text(chat_id=chat_id, message_id=loading_message_id, text=result_msg)
            return

        funding_data = []
        # Фильтрация и парсинг тикеров
        for t in tickers:
            symbol = t.get("symbol")
            rate = t.get("fundingRate")
            next_time = t.get("nextFundingTime")
            volume = t.get("volume24h")
            turnover = t.get("turnover24h") # Оборот в USDT

            if not all([symbol, rate, next_time, volume, turnover]):
                 continue
            try:
                 rate_f = float(rate)
                 next_time_int = int(next_time)
                 turnover_f = float(turnover)
                 # Фильтр по минимальному обороту (например, > 1 млн USDT)
                 if turnover_f < 1_000_000: continue
                 # Фильтр по минимальному модулю фандинга (например, > 0.01%)
                 if abs(rate_f) < 0.0001: continue

                 funding_data.append((symbol, rate_f, next_time_int))
            except (ValueError, TypeError):
                print(f"[Funding Data Error] Could not parse data for {symbol}")
                continue

        # Сортировка по модулю фандинга
        funding_data.sort(key=lambda x: abs(x[1]), reverse=True)
        global latest_top_pairs
        latest_top_pairs = funding_data[:5] # Берем топ-5 после фильтрации

        # Формируем итоговое сообщение
        if not latest_top_pairs:
            result_msg = "📊 Нет подходящих пар с высоким фандингом и ликвидностью."
        else:
            result_msg = "📊 Топ ликвидных пар по фандингу:\n\n"
            now_ts = datetime.utcnow().timestamp()
            for symbol, rate, ts in latest_top_pairs:
                try:
                    delta_sec = int(ts / 1000 - now_ts)
                    if delta_sec < 0: delta_sec = 0 # Если время уже прошло
                    h, rem = divmod(delta_sec, 3600)
                    m, s = divmod(rem, 60)
                    time_left = f"{h:01d}ч {m:02d}м {s:02d}с"
                    direction = "📈 LONG (шорты платят)" if rate < 0 else "📉 SHORT (лонги платят)"

                    # === Markdown форматирование для выделения и копирования ===
                    # Если не хочешь выделение - убери обратные кавычки ` `
                    result_msg += (
                        f"🎟️ *{symbol}*\n"
                        f"{direction}\n"
                        f"💹 Фандинг: `{rate * 100:.4f}%`\n"
                        f"⌛ Выплата через: `{time_left}`\n\n"
                    )
                    # ============================================================

                except Exception as e:
                     print(f"Error formatting pair {symbol}: {e}")
                     result_msg += f"🎟️ *{symbol}* - _ошибка отображения_\n\n"

        # Редактируем сообщение "Загрузка..." с итоговым результатом
        if loading_message_id:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=loading_message_id,
                text=result_msg.strip(),
                parse_mode='Markdown', # Обязательно указываем parse_mode
                disable_web_page_preview=True # Отключаем превью ссылок, если они вдруг появятся
            )

    except Exception as e:
        print(f"Error in show_top_funding: {e}")
        import traceback
        traceback.print_exc()
        error_message = f"❌ Ошибка при получении топа: {e}"
        try:
            # Пытаемся отредактировать исходное сообщение "Загрузка..." на сообщение об ошибке
            if loading_message_id:
                 await context.bot.edit_message_text(chat_id=chat_id, message_id=loading_message_id, text=error_message)
            # Если редактирование не удалось (или не было loading_message_id), отправляем новое
            elif message:
                 await message.reply_text(error_message)
            elif query:
                 await query.message.reply_text(error_message) # Отвечаем на сообщение с кнопками
        except Exception as inner_e:
             print(f"Failed to send error message: {inner_e}")
             # Если даже отправить ошибку не можем, просто логируем
             await context.bot.send_message(chat_id, "❌ Произошла внутренняя ошибка при обработке запроса.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text("Привет! Я фандинг-бот RateHunter. Выбери действие:", reply_markup=reply_markup)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Действие отменено.")
    return ConversationHandler.END

# ===================== УСТАНОВКА МАРЖИ =====================

async def set_real_marja(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("💰 Введите сумму РЕАЛЬНОЙ маржи для ОДНОЙ сделки (в USDT):")
    return SET_MARJA

async def save_real_marja(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        marja_str = update.message.text.strip().replace(",", ".")
        marja = Decimal(marja_str)
        if marja <= 0:
             await update.message.reply_text("❌ Маржа должна быть положительным числом.")
             return ConversationHandler.END
        if chat_id not in sniper_active:
            sniper_active[chat_id] = {}
        sniper_active[chat_id]["real_marja"] = marja
        await update.message.reply_text(f"✅ Маржа для сделки установлена: {marja} USDT")
    except Exception:
        await update.message.reply_text("❌ Неверный формат маржи. Введите число (например, 100 или 55.5).")
        return SET_MARJA
    return ConversationHandler.END

# ===================== УСТАНОВКА ПЛЕЧА =====================

async def set_real_plecho(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⚖ Введите размер плеча (например, 5 или 10):")
    return SET_PLECHO

async def save_real_plecho(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        plecho_str = update.message.text.strip().replace(",", ".")
        plecho = Decimal(plecho_str)
        if not (0 < plecho <= 100):
             await update.message.reply_text("❌ Плечо должно быть положительным числом (обычно до 100).")
             return ConversationHandler.END
        if chat_id not in sniper_active:
            sniper_active[chat_id] = {}
        sniper_active[chat_id]["real_plecho"] = plecho
        await update.message.reply_text(f"✅ Плечо установлено: {plecho}x")
    except Exception:
        await update.message.reply_text("❌ Неверный формат плеча. Введите число (например, 10).")
        return SET_PLECHO
    return ConversationHandler.END

# ===================== МЕНЮ СИГНАЛОВ =====================

async def signal_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    is_active = sniper_active.get(chat_id, {}).get('active', False)
    status_text = "🟢 Активен" if is_active else "🔴 Остановлен"
    buttons = [
        [InlineKeyboardButton(f"Статус: {status_text}", callback_data="toggle_sniper")],
        [InlineKeyboardButton("📊 Показать топ пар", callback_data="show_top_pairs_inline")]
    ]
    reply_markup = InlineKeyboardMarkup(buttons)
    await update.message.reply_text("📡 Меню управления снайпером:", reply_markup=reply_markup)

async def signal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    # query.answer() теперь вызывается внутри show_top_funding, если нужно
    chat_id = query.message.chat_id
    data = query.data

    if data == "toggle_sniper":
        await query.answer() # Отвечаем здесь для toggle
        if chat_id not in sniper_active:
            sniper_active[chat_id] = {'active': False}

        current_status = sniper_active[chat_id].get('active', False)
        new_status = not current_status
        sniper_active[chat_id]['active'] = new_status

        status_text = "🟢 Активен" if new_status else "🔴 Остановлен"
        action_text = "🚀 Снайпер запущен!" if new_status else "🛑 Снайпер остановлен."

        buttons = [
            [InlineKeyboardButton(f"Статус: {status_text}", callback_data="toggle_sniper")],
            [InlineKeyboardButton("📊 Показать топ пар", callback_data="show_top_pairs_inline")]
        ]
        reply_markup = InlineKeyboardMarkup(buttons)
        # Используем try-except для редактирования сообщения
        try:
            await query.edit_message_text(f"{action_text}\n📡 Меню управления снайпером:", reply_markup=reply_markup)
        except Exception as e:
            print(f"Error editing message on toggle: {e}")
            # Если не удалось отредактировать, отправляем новое сообщение
            await context.bot.send_message(chat_id, f"{action_text}\n(Не удалось обновить предыдущее сообщение)")


    elif data == "show_top_pairs_inline":
        # Просто вызываем функцию, она сама разберется с редактированием и query.answer()
        await show_top_funding(update, context)

# ===================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =====================

def get_position_direction(rate: float) -> str:
    # Возвращает 'Buy' для лонга (если rate < 0), 'Sell' для шорта (если rate > 0)
    if rate is None: return "NONE"
    if rate < 0: return "Buy"
    elif rate > 0: return "Sell"
    else: return "NONE"

def quantize_qty(raw_qty: Decimal, qty_step: Decimal) -> Decimal:
    """ Округляет кол-во ВНИЗ до шага qty_step """
    if qty_step <= 0: return raw_qty
    return (raw_qty // qty_step) * qty_step

def quantize_price(raw_price: Decimal, tick_size: Decimal) -> Decimal:
    """ Округляет цену по правилам tick_size (обычно к ближайшему) """
    if tick_size <= 0: return raw_price
    return round(raw_price / tick_size) * tick_size

# ===================== ФОНДОВЫЙ СНАЙПЕР (ФАНДИНГ-БОТ) =====================

async def funding_sniper_loop(app: ApplicationBuilder):
    print(" Sniper loop started ".center(50, "="))
    while True:
        await asyncio.sleep(SNIPER_LOOP_INTERVAL_SECONDS)
        try:
            now_ts = time.time()
            now_dt = datetime.utcnow()
            print(f"\n--- {now_dt.strftime('%Y-%m-%d %H:%M:%S UTC')} Checking ---")

            # Получаем свежие тикеры
            response = session.get_tickers(category="linear")
            tickers = response.get("result", {}).get("list", [])
            if not tickers:
                print("No tickers received.")
                continue

            funding_data = []
            for t in tickers:
                # (Парсинг и фильтрация тикеров как в прошлой версии)
                symbol = t.get("symbol")
                rate = t.get("fundingRate")
                next_time_str = t.get("nextFundingTime")
                turnover = t.get("turnover24h")
                if not all([symbol, rate, next_time_str, turnover]): continue
                try:
                    rate_f = float(rate)
                    next_ts = int(next_time_str) / 1000
                    turnover_f = float(turnover)
                    if turnover_f < 1_000_000 or abs(rate_f) < 0.0001: continue
                    funding_data.append({"symbol": symbol, "rate": rate_f, "next_ts": next_ts})
                except (ValueError, TypeError): continue

            if not funding_data:
                print("No suitable pairs found after filtering.")
                continue

            funding_data.sort(key=lambda x: abs(x["rate"]), reverse=True)
            top_pair = funding_data[0]
            top_symbol = top_pair["symbol"]
            rate = top_pair["rate"]
            next_funding_ts = top_pair["next_ts"]

            seconds_left = next_funding_ts - now_ts
            print(f"Top pair: {top_symbol}, Rate: {rate*100:.4f}%, Funding in: {seconds_left:.0f}s")

            # === ПРОВЕРКА ОКНА ВХОДА ===
            if ENTRY_WINDOW_END_SECONDS <= seconds_left <= ENTRY_WINDOW_START_SECONDS:
                print(f"Entering trade window for {top_symbol} ({seconds_left:.0f}s left)")
                open_side = get_position_direction(rate)
                if open_side == "NONE":
                    print("Funding rate is zero, skipping.")
                    continue

                # Обработка активных пользователей
                for chat_id, data in list(sniper_active.items()):
                    if not data.get('active'): continue
                    if (data.get("last_entry_symbol") == top_symbol and
                            data.get("last_entry_ts") == next_funding_ts):
                        continue

                    marja = data.get('real_marja')
                    plecho = data.get('real_plecho')
                    if not marja or not plecho:
                        await app.bot.send_message(chat_id, f"⚠️ Пропуск {top_symbol}: Маржа или плечо не установлены.")
                        continue

                    print(f"\n>>> Processing {top_symbol} for chat {chat_id} <<<")
                    await app.bot.send_message(
                        chat_id,
                        f"🎯 Вхожу в окно сделки: *{top_symbol}*\n"
                        f"Направление: {'📈 LONG' if open_side == 'Buy' else '📉 SHORT'}\n"
                        f"Фандинг: `{rate * 100:.4f}%`\n"
                        f"Осталось: `{seconds_left:.0f} сек`",
                         parse_mode='Markdown'
                    )

                    # ==================== НАЧАЛО БЛОКА СДЕЛКИ ====================
                    trade_success = False
                    position_data = {
                        "symbol": top_symbol, "open_side": open_side,
                        "marja": marja, "plecho": plecho,
                        "funding_rate": Decimal(str(rate)),
                        "next_funding_ts": next_funding_ts,
                        "opened_qty": Decimal("0"), "closed_qty": Decimal("0"),
                        "total_open_value": Decimal("0"), "total_close_value": Decimal("0"),
                        "total_open_fee": Decimal("0"), "total_close_fee": Decimal("0"),
                        "actual_funding_fee": None,
                        "target_qty": Decimal("0"),
                    }

                    try:
                        # --- Получение инфо и расчет кол-ва ---
                        print(f"Getting instrument info for {top_symbol}...")
                        info = session.get_instruments_info(category="linear", symbol=top_symbol)
                        instrument_info = info.get("result", {}).get("list", [])[0]
                        lot_filter = instrument_info["lotSizeFilter"]
                        price_filter = instrument_info["priceFilter"]
                        min_qty = Decimal(lot_filter["minOrderQty"])
                        qty_step = Decimal(lot_filter["qtyStep"])
                        tick_size = Decimal(price_filter["tickSize"])
                        print(f"Min Qty: {min_qty}, Qty Step: {qty_step}, Tick Size: {tick_size}")

                        print(f"Getting ticker info for {top_symbol}...")
                        ticker_info = session.get_tickers(category="linear", symbol=top_symbol)
                        last_price = Decimal(ticker_info["result"]["list"][0]["lastPrice"])
                        print(f"Last Price: {last_price}")

                        position_size_usdt = marja * plecho
                        if last_price <= 0: raise ValueError("Invalid last price")
                        raw_qty = position_size_usdt / last_price
                        adjusted_qty = quantize_qty(raw_qty, qty_step)
                        print(f"Calculated Qty: {raw_qty:.8f}, Adjusted Qty: {adjusted_qty}")

                        if adjusted_qty < min_qty:
                            await app.bot.send_message(chat_id, f"⚠️ Расчетный объем {adjusted_qty} {top_symbol} меньше минимального ({min_qty}). Сделка отменена.")
                            continue

                        position_data["target_qty"] = adjusted_qty

                        # --- Установка плеча ---
                        print(f"Setting leverage {plecho}x for {top_symbol}...")
                        try:
                            session.set_leverage(
                                category="linear", symbol=top_symbol,
                                buyLeverage=str(plecho), sellLeverage=str(plecho)
                            )
                            print("Leverage set successfully.")
                        except Exception as e:
                            if "110043" in str(e): print(f"Leverage already set to {plecho}x.")
                            else: raise ValueError(f"Failed to set leverage: {e}")

                        # ==================== ОТКРЫТИЕ ПОЗИЦИИ (Maker -> Market) ====================
                        print("\n--- Attempting to Open Position ---")
                        open_qty_remaining = adjusted_qty

                        # --- Попытка Maker Open ---
                        try:
                            orderbook = session.get_orderbook(category="linear", symbol=top_symbol, limit=1)
                            best_bid = Decimal(orderbook['result']['b'][0][0])
                            best_ask = Decimal(orderbook['result']['a'][0][0])
                            maker_price = best_bid if open_side == "Buy" else best_ask
                            maker_price_adj = quantize_price(maker_price, tick_size)
                            print(f"Attempting Maker Open ({open_side}) at {maker_price_adj}...")

                            maker_order_resp = session.place_order(
                                category="linear", symbol=top_symbol, side=open_side,
                                order_type="Limit", qty=str(open_qty_remaining),
                                price=str(maker_price_adj), time_in_force="PostOnly"
                            )
                            maker_order_id = maker_order_resp.get("result", {}).get("orderId")
                            if not maker_order_id: raise ValueError("Failed to place maker order (no ID).")
                            print(f"Maker Open Order ID: {maker_order_id}")
                            await app.bot.send_message(chat_id, f"⏳ Попытка входа Maker @{maker_price_adj} (ID: ...{maker_order_id[-6:]})")
                            await asyncio.sleep(MAKER_ORDER_WAIT_SECONDS_ENTRY)

                            order_info = session.get_order_history(category="linear", orderId=maker_order_id, limit=1)
                            order_data = order_info.get("result", {}).get("list", [])
                            if order_data:
                                order_data = order_data[0]
                                cum_exec_qty = Decimal(order_data.get("cumExecQty", "0"))
                                status = order_data.get("orderStatus")
                                print(f"Maker Open Order Status: {status}, Filled Qty: {cum_exec_qty}")
                                if cum_exec_qty > 0:
                                    position_data["opened_qty"] += cum_exec_qty
                                    position_data["total_open_value"] += Decimal(order_data.get("cumExecValue", "0"))
                                    position_data["total_open_fee"] += Decimal(order_data.get("cumExecFee", "0"))
                                    open_qty_remaining -= cum_exec_qty
                                    await app.bot.send_message(chat_id, f"✅ Частично исполнено Maker: {cum_exec_qty} {top_symbol}")
                                if status not in ["Filled", "Cancelled", "Rejected"]:
                                    try:
                                        print(f"Cancelling Maker Open Order {maker_order_id}...")
                                        session.cancel_order(category="linear", symbol=top_symbol, orderId=maker_order_id)
                                        print("Maker order cancelled.")
                                    except Exception as cancel_e:
                                         if "Order does not exist" not in str(cancel_e) and "already been filled" not in str(cancel_e):
                                            print(f"Minor error cancelling maker order: {cancel_e}")
                            else: print(f"Could not get history for Maker Order {maker_order_id}")
                        except Exception as e:
                            print(f"Maker Open attempt failed: {e}")
                            await app.bot.send_message(chat_id, f"⚠️ Ошибка при попытке входа Maker: {e}")

                        # --- Добивание Market Open ---
                        open_qty_remaining = quantize_qty(open_qty_remaining, qty_step)
                        if open_qty_remaining >= min_qty:
                            print(f"Attempting Market Open ({open_side}) for remaining {open_qty_remaining}...")
                            await app.bot.send_message(chat_id, f"🛒 Добиваю маркетом остаток: {open_qty_remaining} {top_symbol}")
                            try:
                                market_order_resp = session.place_order(
                                    category="linear", symbol=top_symbol, side=open_side,
                                    order_type="Market", qty=str(open_qty_remaining),
                                    time_in_force="ImmediateOrCancel"
                                )
                                market_order_id = market_order_resp.get("result", {}).get("orderId")
                                if not market_order_id: raise ValueError("Failed to place market order (no ID).")
                                print(f"Market Open Order ID: {market_order_id}")
                                await asyncio.sleep(1.5) # Увеличил немного ожидание
                                order_info = session.get_order_history(category="linear", orderId=market_order_id, limit=1)
                                order_data = order_info.get("result", {}).get("list", [])
                                if order_data:
                                    order_data = order_data[0]
                                    cum_exec_qty = Decimal(order_data.get("cumExecQty", "0"))
                                    status = order_data.get("orderStatus")
                                    print(f"Market Open Order Status: {status}, Filled Qty: {cum_exec_qty}")
                                    if cum_exec_qty > 0:
                                        position_data["opened_qty"] += cum_exec_qty
                                        position_data["total_open_value"] += Decimal(order_data.get("cumExecValue", "0"))
                                        position_data["total_open_fee"] += Decimal(order_data.get("cumExecFee", "0"))
                                        await app.bot.send_message(chat_id, f"✅ Исполнено Маркет: {cum_exec_qty} {top_symbol}")
                                    else: await app.bot.send_message(chat_id, f"⚠️ Маркет ордер ({market_order_id}) не исполнил ничего.")
                                else: print(f"Could not get history for Market Order {market_order_id}")
                            except Exception as e:
                                print(f"Market Open attempt failed: {e}")
                                await app.bot.send_message(chat_id, f"❌ Ошибка при добивании маркетом: {e}")
                        elif open_qty_remaining > 0: print(f"Remaining open qty {open_qty_remaining} < min qty {min_qty}.")

                        # --- Проверка итогового открытия ---
                        final_opened_qty = position_data["opened_qty"]
                        if final_opened_qty < min_qty:
                            await app.bot.send_message(chat_id, f"❌ Не удалось открыть минимальный объем ({min_qty}) для {top_symbol}. Итого открыто: {final_opened_qty}. Отмена сделки.")
                            continue

                        avg_open_price_str = f"{position_data['total_open_value'] / final_opened_qty:.4f}" if final_opened_qty else "N/A"
                        await app.bot.send_message(
                            chat_id,
                            f"✅ Позиция *{top_symbol}* ({'LONG' if open_side == 'Buy' else 'SHORT'}) открыта.\n"
                            f"Объем: `{final_opened_qty}`\n"
                            f"Средств. цена (прибл.): `{avg_open_price_str}`\n"
                            f"Комиссия откр.: `{position_data['total_open_fee']:.4f}` USDT",
                            parse_mode='Markdown'
                        )
                        print(f"Position Opened. Total Qty: {final_opened_qty}")
                        sniper_active[chat_id]["last_entry_symbol"] = top_symbol
                        sniper_active[chat_id]["last_entry_ts"] = next_funding_ts

                        # ==================== ОЖИДАНИЕ И ПРОВЕРКА ФАНДИНГА ====================
                        print("\n--- Waiting for Funding Payment ---")
                        now_ts_before_wait = time.time()
                        delay_needed = next_funding_ts - now_ts_before_wait
                        wait_duration = max(0, delay_needed) + POST_FUNDING_WAIT_SECONDS
                        print(f"Funding at {datetime.fromtimestamp(next_funding_ts)} UTC. Waiting for {wait_duration:.1f} seconds...")
                        await app.bot.send_message(chat_id, f"⏳ Ожидаю выплаты фандинга (~{wait_duration:.0f} сек)...")
                        await asyncio.sleep(wait_duration)

                        print("Checking actual funding payment...")
                        try:
                            funding_check_start_time = int(next_funding_ts * 1000)
                            funding_check_end_time = int((next_funding_ts + 60) * 1000)
                            funding_history = session.get_funding_history(
                                category="linear", symbol=top_symbol,
                                startTime=funding_check_start_time, endTime=funding_check_end_time,
                                limit=1
                            )
                            funding_list = funding_history.get("result", {}).get("list", [])
                            if funding_list:
                                last_funding = funding_list[0]
                                funding_fee = Decimal(last_funding.get("fundingFee", "0"))
                                funding_time_ms = int(last_funding.get("execTime", "0"))
                                if abs(funding_time_ms / 1000 - next_funding_ts) < 60:
                                    position_data["actual_funding_fee"] = funding_fee
                                    print(f"Actual Funding Fee recorded: {funding_fee}")
                                    await app.bot.send_message(chat_id, f"💰 Фандинг получен: `{funding_fee:.4f}` USDT", parse_mode='Markdown')
                                else:
                                     print(f"Funding record found, but timestamp mismatch: expected ~{next_funding_ts*1000}, got {funding_time_ms}")
                                     await app.bot.send_message(chat_id, f"⚠️ Найден фандинг, но время не совпадает.")
                                     position_data["actual_funding_fee"] = Decimal("0")
                            else:
                                print("No funding fee record found.")
                                await app.bot.send_message(chat_id, f"⚠️ Не найдено записей о фандинге для {top_symbol}.")
                                position_data["actual_funding_fee"] = Decimal("0")
                        except Exception as e:
                            print(f"Error checking funding history: {e}")
                            await app.bot.send_message(chat_id, f"❌ Ошибка при проверке истории фандинга: {e}")
                            position_data["actual_funding_fee"] = Decimal("0")

                        # ==================== ЗАКРЫТИЕ ПОЗИЦИИ (Maker -> Market) ====================
                        print("\n--- Attempting to Close Position ---")
                        close_side = "Buy" if open_side == "Sell" else "Sell"
                        close_qty_remaining = final_opened_qty

                        # --- Попытка Maker Close ---
                        try:
                            orderbook = session.get_orderbook(category="linear", symbol=top_symbol, limit=1)
                            best_bid = Decimal(orderbook['result']['b'][0][0])
                            best_ask = Decimal(orderbook['result']['a'][0][0])
                            maker_price = best_bid if close_side == "Buy" else best_ask
                            maker_price_adj = quantize_price(maker_price, tick_size)
                            print(f"Attempting Maker Close ({close_side}) at {maker_price_adj}...")
                            maker_close_resp = session.place_order(
                                category="linear", symbol=top_symbol, side=close_side,
                                order_type="Limit", qty=str(close_qty_remaining),
                                price=str(maker_price_adj), time_in_force="PostOnly",
                                reduce_only=True
                            )
                            maker_close_id = maker_close_resp.get("result", {}).get("orderId")
                            if not maker_close_id: raise ValueError("Failed to place maker close order (no ID).")
                            print(f"Maker Close Order ID: {maker_close_id}")
                            await app.bot.send_message(chat_id, f"⏳ Попытка выхода Maker @{maker_price_adj} (ID: ...{maker_close_id[-6:]})")
                            await asyncio.sleep(MAKER_ORDER_WAIT_SECONDS_EXIT)

                            order_info = session.get_order_history(category="linear", orderId=maker_close_id, limit=1)
                            order_data = order_info.get("result", {}).get("list", [])
                            if order_data:
                                order_data = order_data[0]
                                cum_exec_qty = Decimal(order_data.get("cumExecQty", "0"))
                                status = order_data.get("orderStatus")
                                print(f"Maker Close Order Status: {status}, Filled Qty: {cum_exec_qty}")
                                if cum_exec_qty > 0:
                                    position_data["closed_qty"] += cum_exec_qty
                                    position_data["total_close_value"] += Decimal(order_data.get("cumExecValue", "0"))
                                    position_data["total_close_fee"] += Decimal(order_data.get("cumExecFee", "0"))
                                    close_qty_remaining -= cum_exec_qty
                                    await app.bot.send_message(chat_id, f"✅ Частично исполнено Maker (закрытие): {cum_exec_qty} {top_symbol}")
                                if status not in ["Filled", "Cancelled", "Rejected", "Deactivated"]:
                                    try:
                                        print(f"Cancelling Maker Close Order {maker_close_id}...")
                                        session.cancel_order(category="linear", symbol=top_symbol, orderId=maker_close_id)
                                        print("Maker close order cancelled.")
                                    except Exception as cancel_e:
                                         if "Order does not exist" not in str(cancel_e) and "already been filled" not in str(cancel_e):
                                            print(f"Minor error cancelling maker close order: {cancel_e}")
                            else: print(f"Could not get history for Maker Close Order {maker_close_id}")
                        except Exception as e:
                            print(f"Maker Close attempt failed: {e}")
                            await app.bot.send_message(chat_id, f"⚠️ Ошибка при попытке выхода Maker: {e}")

                        # --- Добивание Market Close ---
                        close_qty_remaining = quantize_qty(close_qty_remaining, qty_step)
                        if close_qty_remaining >= min_qty:
                            print(f"Attempting Market Close ({close_side}) for remaining {close_qty_remaining}...")
                            await app.bot.send_message(chat_id, f"🛒 Закрываю маркетом остаток: {close_qty_remaining} {top_symbol}")
                            try:
                                market_close_resp = session.place_order(
                                    category="linear", symbol=top_symbol, side=close_side,
                                    order_type="Market", qty=str(close_qty_remaining),
                                    time_in_force="ImmediateOrCancel", reduce_only=True
                                )
                                market_close_id = market_close_resp.get("result", {}).get("orderId")
                                if not market_close_id: raise ValueError("Failed to place market close order (no ID).")
                                print(f"Market Close Order ID: {market_close_id}")
                                await asyncio.sleep(1.5) # Увеличил немного ожидание
                                order_info = session.get_order_history(category="linear", orderId=market_close_id, limit=1)
                                order_data = order_info.get("result", {}).get("list", [])
                                if order_data:
                                    order_data = order_data[0]
                                    cum_exec_qty = Decimal(order_data.get("cumExecQty", "0"))
                                    status = order_data.get("orderStatus")
                                    print(f"Market Close Order Status: {status}, Filled Qty: {cum_exec_qty}")
                                    if cum_exec_qty > 0:
                                        position_data["closed_qty"] += cum_exec_qty
                                        position_data["total_close_value"] += Decimal(order_data.get("cumExecValue", "0"))
                                        position_data["total_close_fee"] += Decimal(order_data.get("cumExecFee", "0"))
                                        await app.bot.send_message(chat_id, f"✅ Исполнено Маркет (закрытие): {cum_exec_qty} {top_symbol}")
                                    else: await app.bot.send_message(chat_id, f"⚠️ Маркет ордер закрытия ({market_close_id}) не исполнил ничего.")
                                else: print(f"Could not get history for Market Close Order {market_close_id}")
                            except Exception as e:
                                print(f"Market Close attempt failed: {e}")
                                await app.bot.send_message(chat_id, f"❌ Ошибка при маркет-закрытии: {e}")
                        elif close_qty_remaining > 0: print(f"Remaining close qty {close_qty_remaining} < min qty {min_qty}.")

                        # --- Проверка итогового закрытия ---
                        final_closed_qty = position_data["closed_qty"]
                        print(f"Position Closed. Total Qty: {final_closed_qty}")
                        if abs(final_closed_qty - final_opened_qty) > min_qty * Decimal("0.1"):
                             await app.bot.send_message(
                                 chat_id,
                                 f"⚠️ Позиция *{top_symbol}* закрыта не полностью!\n"
                                 f"Открыто: `{final_opened_qty}`, Закрыто: `{final_closed_qty}`.\n"
                                 f"❗️ Проверьте вручную!", parse_mode='Markdown'
                            )
                        else:
                             await app.bot.send_message(
                                 chat_id,
                                 f"✅ Позиция *{top_symbol}* успешно закрыта ({final_closed_qty}).", parse_mode='Markdown'
                             )

                        # ==================== РАСЧЕТ РЕАЛЬНОГО PNL ====================
                        print("\n--- Calculating Real PNL ---")
                        price_pnl = position_data["total_close_value"] - position_data["total_open_value"]
                        if open_side == "Sell": price_pnl = -price_pnl
                        funding_pnl = position_data.get("actual_funding_fee") or Decimal("0")
                        total_fees = position_data["total_open_fee"] + position_data["total_close_fee"]
                        net_pnl = price_pnl + funding_pnl - total_fees
                        roi_pct = (net_pnl / marja) * 100 if marja else Decimal("0")

                        print(f"Price PNL: {price_pnl:.4f}")
                        print(f"Funding PNL: {funding_pnl:.4f}")
                        print(f"Total Fees: {total_fees:.4f}")
                        print(f"Net PNL: {net_pnl:.4f}")
                        print(f"ROI: {roi_pct:.2f}%")

                        # Вывод результата пользователю
                        await app.bot.send_message(
                            chat_id,
                            f"📊 Результат сделки: *{top_symbol}* ({'LONG' if open_side == 'Buy' else 'SHORT'})\n\n"
                            f" PNL (цена): `{price_pnl:+.4f}` USDT\n"
                            f" PNL (фандинг): `{funding_pnl:+.4f}` USDT\n"
                            f" Комиссии (откр+закр): `{-total_fees:.4f}` USDT\n"
                            f"💰 *Чистая прибыль: {net_pnl:+.4f} USDT*\n"
                            f"📈 ROI от маржи ({marja} USDT): `{roi_pct:.2f}%`",
                             parse_mode='Markdown'
                        )
                        trade_success = True

                    # ==================== КОНЕЦ БЛОКА СДЕЛКИ (обработка ошибок) =====================
                    except Exception as trade_e:
                        print(f"\n!!! CRITICAL TRADE ERROR for chat {chat_id}, symbol {top_symbol} !!!")
                        print(f"Error: {trade_e}")
                        import traceback
                        traceback.print_exc()
                        await app.bot.send_message(
                            chat_id,
                            f"❌ КРИТИЧЕСКАЯ ОШИБКА во время сделки по *{top_symbol}*:\n"
                            f"`{trade_e}`\n\n"
                            f"❗️ *ПРОВЕРЬТЕ СЧЕТ И ПОЗИЦИИ ВРУЧНУЮ!*",
                             parse_mode='Markdown'
                        )

                    finally:
                        print(f">>> Finished processing {top_symbol} for chat {chat_id} <<<")

            else:
                # print(f"Not in entry window for {top_symbol} ({seconds_left:.0f}s left).")
                pass

        except Exception as loop_e:
            print("\n!!! UNHANDLED ERROR IN SNIPER LOOP !!!")
            print(f"Error: {loop_e}")
            import traceback
            traceback.print_exc()
            await asyncio.sleep(30)


# ===================== MAIN =====================

if __name__ == "__main__":
    print("Initializing bot...")
    app_builder = ApplicationBuilder().token(BOT_TOKEN)
    app = app_builder.build()

    # --- Добавляем хендлеры ---
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))

    # --- Обработчики кнопок основного меню ---
    # Используем Regex с якорями ^ и $ для точного совпадения
    app.add_handler(MessageHandler(filters.Regex("^📊 Топ-пары$"), show_top_funding))
    app.add_handler(MessageHandler(filters.Regex("^📡 Сигналы$"), signal_menu))
    # app.add_handler(MessageHandler(filters.Regex("^🧮 Калькулятор прибыли$"), calculator_handler)) # Placeholder

    # --- Обработчик Inline кнопок ---
    app.add_handler(CallbackQueryHandler(signal_callback, pattern="^(toggle_sniper|show_top_pairs_inline)$"))

    # --- Диалоги ---
    conv_marja = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^💰 Маржа$"), set_real_marja)],
        states={SET_MARJA: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_real_marja)]},
        fallbacks=[CommandHandler("cancel", cancel)],
        conversation_timeout=60.0
    )
    app.add_handler(conv_marja)

    conv_plecho = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^⚖ Плечо$"), set_real_plecho)],
        states={SET_PLECHO: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_real_plecho)]},
        fallbacks=[CommandHandler("cancel", cancel)],
        conversation_timeout=60.0
    )
    app.add_handler(conv_plecho)

    # --- Запуск фоновой задачи ---
    async def post_init_tasks(passed_app: ApplicationBuilder):
        print("Running post_init tasks...")
        asyncio.create_task(funding_sniper_loop(passed_app))
        print("Sniper loop task created.")

    app.post_init = post_init_tasks

    # --- Запуск бота ---
    print("Starting bot polling...")
    try:
        # allowed_updates можно убрать или настроить точнее, если нужно
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        print(f"\nBot polling stopped due to error: {e}")
    finally:
        print("\nBot shutdown.")


# --- END OF FILE bot (7).py ---
