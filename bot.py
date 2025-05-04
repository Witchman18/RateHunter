# --- START OF FILE bot (7).py ---

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
    ["📊 Топ-пары", "🧮 Калькулятор прибыли"],
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
    """Показывает топ-5 пар по funding rate с улучшенным оформлением"""
    message = update.message or update.callback_query.message # Для работы из callback
    try:
        await message.reply_text("🔄 Получаю топ пар...")
        response = session.get_tickers(category="linear")
        tickers = response.get("result", {}).get("list", [])
        if not tickers:
             await message.edit_text("⚠️ Не удалось получить данные тикеров.")
             return

        funding_data = []
        for t in tickers:
            symbol = t.get("symbol")
            rate = t.get("fundingRate")
            next_time = t.get("nextFundingTime")
            volume = t.get("volume24h")
            turnover = t.get("turnover24h") # Оборот в USDT

            # Пропускаем пары без данных или с околонулевым фандингом/оборотом
            if not all([symbol, rate, next_time, volume, turnover]):
                 continue
            try:
                 rate_f = float(rate)
                 next_time_int = int(next_time)
                 turnover_f = float(turnover)
                 # Фильтр по минимальному обороту (например, > 1 млн USDT)
                 if turnover_f < 1_000_000:
                     continue
                 # Фильтр по минимальному модулю фандинга (например, > 0.01%)
                 if abs(rate_f) < 0.0001:
                     continue

                 funding_data.append((symbol, rate_f, next_time_int))
            except (ValueError, TypeError):
                print(f"[Funding Data Error] Could not parse data for {symbol}")
                continue

        # Сортировка по модулю фандинга
        funding_data.sort(key=lambda x: abs(x[1]), reverse=True)
        global latest_top_pairs
        latest_top_pairs = funding_data[:5] # Берем топ-5 после фильтрации

        if not latest_top_pairs:
            await message.edit_text("📊 Нет подходящих пар с высоким фандингом и ликвидностью.")
            return

        msg = "📊 Топ ликвидных пар по фандингу:\n\n"
        now_ts = datetime.utcnow().timestamp()
        for symbol, rate, ts in latest_top_pairs:
            try:
                delta_sec = int(ts / 1000 - now_ts)
                if delta_sec < 0: delta_sec = 0 # Если время уже прошло
                h, rem = divmod(delta_sec, 3600)
                m, s = divmod(rem, 60)
                time_left = f"{h:01d}ч {m:02d}м {s:02d}с"
                direction = "📈 LONG" if rate < 0 else "📉 SHORT" # Если фандинг отриц., лонги платят шортам (выгодно шортить) -> Corrected logic: отриц = шорты платят лонгам => выгодно ЛОНГ
                # direction = "📈 LONG" if rate < 0 else "📉 SHORT" # Old logic, needs correction if funding is negative means shorts pay longs
                direction = "📈 LONG (шорты платят)" if rate < 0 else "📉 SHORT (лонги платят)"


                msg += (
                    f"🎟️ *{symbol}*\n"
                    f"{direction}\n"
                    f"💹 Фандинг: `{rate * 100:.4f}%`\n"
                    f"⌛ Выплата через: `{time_left}`\n\n"
                )
            except Exception as e:
                 print(f"Error formatting pair {symbol}: {e}")
                 msg += f"🎟️ *{symbol}* - _ошибка отображения_\n\n"


        await message.edit_text(msg.strip(), parse_mode='Markdown')
    except Exception as e:
        print(f"Error in show_top_funding: {e}")
        import traceback
        traceback.print_exc()
        try:
             await message.edit_text(f"❌ Ошибка при получении топа: {e}")
        except: # If edit fails, send new message
             await context.bot.send_message(message.chat_id, f"❌ Ошибка при получении топа: {e}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text("Привет! Я фандинг-бот. Выбери действие:", reply_markup=reply_markup)

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
        # Используем Decimal для точности
        marja_str = update.message.text.strip().replace(",", ".")
        marja = Decimal(marja_str)
        if marja <= 0:
             await update.message.reply_text("❌ Маржа должна быть положительным числом.")
             return ConversationHandler.END # Остаемся в том же состоянии или отменяем? Лучше отменить.
        if chat_id not in sniper_active:
            sniper_active[chat_id] = {}
        sniper_active[chat_id]["real_marja"] = marja
        await update.message.reply_text(f"✅ Маржа для сделки установлена: {marja} USDT")
    except Exception: # Ловим ошибки конвертации Decimal
        await update.message.reply_text("❌ Неверный формат маржи. Введите число (например, 100 или 55.5).")
        # Не выходим из ConversationHandler, чтобы пользователь мог попробовать еще раз
        return SET_MARJA # Остаемся в состоянии ожидания ввода
    return ConversationHandler.END

# ===================== УСТАНОВКА ПЛЕЧА =====================

async def set_real_plecho(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⚖ Введите размер плеча (например, 5 или 10):")
    return SET_PLECHO

async def save_real_plecho(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        plecho_str = update.message.text.strip().replace(",", ".")
        plecho = Decimal(plecho_str) # Используем Decimal
        # Bybit обычно требует целое плечо или с .5, но тут зависит от API
        # Добавим проверку на разумность плеча
        if not (0 < plecho <= 100): # Например, плечо от >0 до 100
             await update.message.reply_text("❌ Плечо должно быть положительным числом (обычно до 100).")
             return ConversationHandler.END
        if chat_id not in sniper_active:
            sniper_active[chat_id] = {}
        sniper_active[chat_id]["real_plecho"] = plecho
        await update.message.reply_text(f"✅ Плечо установлено: {plecho}x")
    except Exception:
        await update.message.reply_text("❌ Неверный формат плеча. Введите число (например, 10).")
        return SET_PLECHO # Остаемся в состоянии ожидания ввода
    return ConversationHandler.END

# ===================== МЕНЮ СИГНАЛОВ =====================

async def signal_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    is_active = sniper_active.get(chat_id, {}).get('active', False)
    status_text = "🟢 Активен" if is_active else "🔴 Остановлен"
    buttons = [
        [InlineKeyboardButton(f"Статус: {status_text}", callback_data="toggle_sniper")],
        [InlineKeyboardButton("📊 Показать топ пар", callback_data="show_top_pairs_inline")]
        # Можно добавить кнопки для просмотра текущих настроек маржи/плеча
    ]
    reply_markup = InlineKeyboardMarkup(buttons)
    await update.message.reply_text("📡 Меню управления снайпером:", reply_markup=reply_markup)

async def signal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    data = query.data

    if data == "toggle_sniper":
        if chat_id not in sniper_active:
            sniper_active[chat_id] = {'active': False} # Инициализируем, если нет

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
        await query.edit_message_text(f"{action_text}\n📡 Меню управления снайпером:", reply_markup=reply_markup)

    elif data == "show_top_pairs_inline":
        await show_top_funding(update, context) # Вызываем функцию показа топа

# ===================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =====================

def get_position_direction(rate: float) -> str:
    # Возвращает 'Buy' для лонга (если rate < 0), 'Sell' для шорта (если rate > 0)
    # Это сторона ОРДЕРА НА ОТКРЫТИЕ позиции
    if rate is None: return "NONE"
    # Исправленная логика: rate < 0 => шорты платят лонгам => выгодно ОТКРЫВАТЬ ЛОНГ ('Buy')
    if rate < 0: return "Buy"
    # rate > 0 => лонги платят шортам => выгодно ОТКРЫВАТЬ ШОРТ ('Sell')
    elif rate > 0: return "Sell"
    else: return "NONE"

def quantize_qty(raw_qty: Decimal, qty_step: Decimal) -> Decimal:
    """ Округляет кол-во ВНИЗ до шага qty_step """
    if qty_step <= 0: return raw_qty # Избегаем деления на ноль
    return (raw_qty // qty_step) * qty_step

def quantize_price(raw_price: Decimal, tick_size: Decimal) -> Decimal:
    """ Округляет цену по правилам tick_size (обычно к ближайшему) """
    if tick_size <= 0: return raw_price
    # Округляем к ближайшему шагу тика
    return round(raw_price / tick_size) * tick_size

# ===================== ФОНДОВЫЙ СНАЙПЕР (ФАНДИНГ-БОТ) =====================

async def funding_sniper_loop(app: ApplicationBuilder):
    print(" Sniper loop started ".center(50, "="))
    while True:
        await asyncio.sleep(SNIPER_LOOP_INTERVAL_SECONDS) # Пауза между проверками
        try:
            now_ts = time.time() # Используем time.time() для большей точности
            now_dt = datetime.utcnow()
            print(f"\n--- {now_dt.strftime('%Y-%m-%d %H:%M:%S UTC')} Checking ---")

            # Получаем свежие тикеры (не из кэша)
            response = session.get_tickers(category="linear")
            tickers = response.get("result", {}).get("list", [])
            if not tickers:
                print("No tickers received.")
                continue

            funding_data = []
            for t in tickers:
                symbol = t.get("symbol")
                rate = t.get("fundingRate")
                next_time_str = t.get("nextFundingTime") # Время следующего фандинга (ms)
                turnover = t.get("turnover24h")

                if not all([symbol, rate, next_time_str, turnover]): continue
                try:
                    rate_f = float(rate)
                    next_ts = int(next_time_str) / 1000 # Время фандинга в секундах
                    turnover_f = float(turnover)

                    # Фильтры ликвидности и силы фандинга
                    if turnover_f < 1_000_000 or abs(rate_f) < 0.0001: continue

                    funding_data.append({"symbol": symbol, "rate": rate_f, "next_ts": next_ts})
                except (ValueError, TypeError):
                    continue

            if not funding_data:
                print("No suitable pairs found after filtering.")
                continue

            # Сортируем по модулю фандинга
            funding_data.sort(key=lambda x: abs(x["rate"]), reverse=True)

            # Обрабатываем топ-1 пару
            top_pair = funding_data[0]
            top_symbol = top_pair["symbol"]
            rate = top_pair["rate"]
            next_funding_ts = top_pair["next_ts"]

            seconds_left = next_funding_ts - now_ts
            print(f"Top pair: {top_symbol}, Rate: {rate*100:.4f}%, Funding in: {seconds_left:.0f}s")

            # === ПРОВЕРКА ОКНА ВХОДА ===
            if ENTRY_WINDOW_END_SECONDS <= seconds_left <= ENTRY_WINDOW_START_SECONDS:
                print(f"Entering trade window for {top_symbol} ({seconds_left:.0f}s left)")
                open_side = get_position_direction(rate) # 'Buy' or 'Sell'
                if open_side == "NONE":
                    print("Funding rate is zero, skipping.")
                    continue

                # Проходим по всем активным пользователям
                for chat_id, data in list(sniper_active.items()): # Используем list() для возможности удаления во время итерации
                    if not data.get('active'):
                        continue

                    # Проверяем, не входили ли мы уже в эту сессию фандинга для этого юзера
                    if (data.get("last_entry_symbol") == top_symbol and
                            data.get("last_entry_ts") == next_funding_ts):
                        print(f"Already entered {top_symbol} for chat {chat_id} this funding period.")
                        continue

                    marja = data.get('real_marja') # Decimal
                    plecho = data.get('real_plecho') # Decimal
                    if not marja or not plecho:
                        await app.bot.send_message(chat_id, f"⚠️ Пропуск {top_symbol}: Маржа или плечо не установлены.")
                        continue

                    print(f"\n>>> Processing {top_symbol} for chat {chat_id} <<<")
                    await app.bot.send_message(
                        chat_id,
                        f"🎯 Вхожу в окно сделки: *{top_symbol}*\n"
                        f"Направление: {'📈 LONG' if open_side == 'Buy' else '📉 SHORT'}\n"
                        f"Фандинг: `{rate * 100:.4f}%`\n"
                        f"Осталось: `{seconds_left:.0f} сек`"
                        , parse_mode='Markdown'
                    )

                    # ==================== НАЧАЛО БЛОКА СДЕЛКИ ====================
                    trade_success = False
                    position_data = { # Словарь для хранения данных по сделке
                        "symbol": top_symbol,
                        "open_side": open_side,
                        "marja": marja,
                        "plecho": plecho,
                        "funding_rate": Decimal(str(rate)), # Сохраняем фандинг как Decimal
                        "next_funding_ts": next_funding_ts,
                        "opened_qty": Decimal("0"),
                        "closed_qty": Decimal("0"),
                        "total_open_value": Decimal("0"),
                        "total_close_value": Decimal("0"),
                        "total_open_fee": Decimal("0"),
                        "total_close_fee": Decimal("0"),
                        "actual_funding_fee": None, # Будет заполнено позже
                    }

                    try:
                        # 1. Получаем инфо по инструменту (шаги, мин. кол-во)
                        print(f"Getting instrument info for {top_symbol}...")
                        info = session.get_instruments_info(category="linear", symbol=top_symbol)
                        instrument_info = info.get("result", {}).get("list", [])[0]
                        lot_filter = instrument_info["lotSizeFilter"]
                        price_filter = instrument_info["priceFilter"]
                        min_qty = Decimal(lot_filter["minOrderQty"])
                        qty_step = Decimal(lot_filter["qtyStep"])
                        tick_size = Decimal(price_filter["tickSize"])
                        print(f"Min Qty: {min_qty}, Qty Step: {qty_step}, Tick Size: {tick_size}")

                        # 2. Получаем текущую цену для расчета кол-ва
                        print(f"Getting ticker info for {top_symbol}...")
                        ticker_info = session.get_tickers(category="linear", symbol=top_symbol)
                        last_price = Decimal(ticker_info["result"]["list"][0]["lastPrice"])
                        print(f"Last Price: {last_price}")

                        # 3. Расчет и корректировка кол-ва (qty)
                        position_size_usdt = marja * plecho # Decimal
                        if last_price <= 0: raise ValueError("Invalid last price")
                        raw_qty = position_size_usdt / last_price
                        adjusted_qty = quantize_qty(raw_qty, qty_step)
                        print(f"Calculated Qty: {raw_qty:.8f}, Adjusted Qty: {adjusted_qty}")

                        if adjusted_qty < min_qty:
                            await app.bot.send_message(chat_id, f"⚠️ Расчетный объем {adjusted_qty} {top_symbol} меньше минимального ({min_qty}). Сделка отменена.")
                            continue # Переход к следующему пользователю

                        position_data["target_qty"] = adjusted_qty # Сохраняем целевое кол-во

                        # 4. Установка плеча (лучше делать заранее, но проверим)
                        print(f"Setting leverage {plecho}x for {top_symbol}...")
                        try:
                            session.set_leverage(
                                category="linear", symbol=top_symbol,
                                buyLeverage=str(plecho), sellLeverage=str(plecho)
                            )
                            print("Leverage set successfully.")
                        except Exception as e:
                            # 110043: Leverage not modified - ОК
                            if "110043" in str(e):
                                print(f"Leverage already set to {plecho}x.")
                            else:
                                raise ValueError(f"Failed to set leverage: {e}") # Прерываем сделку

                        # ==================== ОТКРЫТИЕ ПОЗИЦИИ (Maker -> Market) ====================
                        print("\n--- Attempting to Open Position ---")
                        open_qty_remaining = adjusted_qty

                        # 5. Попытка открыть через Limit PostOnly
                        try:
                            orderbook = session.get_orderbook(category="linear", symbol=top_symbol, limit=1)
                            best_bid = Decimal(orderbook['result']['b'][0][0])
                            best_ask = Decimal(orderbook['result']['a'][0][0])
                            # Ставим цену ТОЧНО на лучший бид/аск для PostOnly
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

                            # Проверяем исполнение Maker ордера
                            order_info = session.get_order_history(category="linear", orderId=maker_order_id, limit=1)
                            order_data = order_info.get("result", {}).get("list", [])
                            if order_data:
                                order_data = order_data[0]
                                cum_exec_qty = Decimal(order_data.get("cumExecQty", "0"))
                                cum_exec_value = Decimal(order_data.get("cumExecValue", "0"))
                                cum_exec_fee = Decimal(order_data.get("cumExecFee", "0"))
                                status = order_data.get("orderStatus")
                                print(f"Maker Order Status: {status}, Filled Qty: {cum_exec_qty}")

                                if cum_exec_qty > 0:
                                    position_data["opened_qty"] += cum_exec_qty
                                    position_data["total_open_value"] += cum_exec_value
                                    position_data["total_open_fee"] += cum_exec_fee
                                    open_qty_remaining -= cum_exec_qty
                                    await app.bot.send_message(chat_id, f"✅ Частично исполнено Maker: {cum_exec_qty} {top_symbol}")


                                # Отменяем ордер, если он не исполнился полностью или частично
                                if status not in ["Filled", "Cancelled", "Rejected"]:
                                    try:
                                        print(f"Cancelling Maker Open Order {maker_order_id}...")
                                        session.cancel_order(category="linear", symbol=top_symbol, orderId=maker_order_id)
                                        print("Maker order cancelled.")
                                    except Exception as cancel_e:
                                         # Игнорируем ошибку, если ордер уже не существует или исполнен
                                         if "Order does not exist" not in str(cancel_e) and "already been filled" not in str(cancel_e):
                                            print(f"Minor error cancelling maker order: {cancel_e}")
                            else:
                                print(f"Could not get history for Maker Order {maker_order_id}")


                        except Exception as e:
                            print(f"Maker Open attempt failed: {e}")
                            await app.bot.send_message(chat_id, f"⚠️ Ошибка при попытке входа Maker: {e}")

                        # 6. Добиваем остаток через Market IOC
                        open_qty_remaining = quantize_qty(open_qty_remaining, qty_step) # Пересчитываем остаток
                        if open_qty_remaining >= min_qty:
                            print(f"Attempting Market Open ({open_side}) for remaining {open_qty_remaining}...")
                            await app.bot.send_message(chat_id, f"🛒 Добиваю маркетом остаток: {open_qty_remaining} {top_symbol}")
                            try:
                                market_order_resp = session.place_order(
                                    category="linear", symbol=top_symbol, side=open_side,
                                    order_type="Market", qty=str(open_qty_remaining),
                                    time_in_force="ImmediateOrCancel" # IOC - исполнить что можно сразу, остальное отменить
                                )
                                market_order_id = market_order_resp.get("result", {}).get("orderId")
                                if not market_order_id: raise ValueError("Failed to place market order (no ID).")
                                print(f"Market Open Order ID: {market_order_id}")

                                # Ждем немного и проверяем исполнение Market ордера
                                await asyncio.sleep(1) # Даем время на обработку
                                order_info = session.get_order_history(category="linear", orderId=market_order_id, limit=1)
                                order_data = order_info.get("result", {}).get("list", [])
                                if order_data:
                                    order_data = order_data[0]
                                    cum_exec_qty = Decimal(order_data.get("cumExecQty", "0"))
                                    cum_exec_value = Decimal(order_data.get("cumExecValue", "0"))
                                    cum_exec_fee = Decimal(order_data.get("cumExecFee", "0"))
                                    status = order_data.get("orderStatus")
                                    print(f"Market Order Status: {status}, Filled Qty: {cum_exec_qty}")

                                    if cum_exec_qty > 0:
                                        position_data["opened_qty"] += cum_exec_qty
                                        position_data["total_open_value"] += cum_exec_value
                                        position_data["total_open_fee"] += cum_exec_fee
                                        await app.bot.send_message(chat_id, f"✅ Исполнено Маркет: {cum_exec_qty} {top_symbol}")
                                    else:
                                         await app.bot.send_message(chat_id, f"⚠️ Маркет ордер ({market_order_id}) не исполнил ничего.")

                                else:
                                    print(f"Could not get history for Market Order {market_order_id}")

                            except Exception as e:
                                print(f"Market Open attempt failed: {e}")
                                await app.bot.send_message(chat_id, f"❌ Ошибка при добивании маркетом: {e}")
                        elif open_qty_remaining > 0:
                             print(f"Remaining open qty {open_qty_remaining} is less than min qty {min_qty}. Skipping market order.")


                        # 7. Проверка итогового открытого кол-ва
                        final_opened_qty = position_data["opened_qty"]
                        if final_opened_qty < min_qty:
                            await app.bot.send_message(chat_id, f"❌ Не удалось открыть минимальный объем ({min_qty}) для {top_symbol}. Итого открыто: {final_opened_qty}. Отмена сделки.")
                            # Здесь можно добавить логику закрытия, если что-то все же открылось, но пока просто отменяем
                            continue # К следующему пользователю

                        await app.bot.send_message(
                            chat_id,
                            f"✅ Позиция *{top_symbol}* ({'LONG' if open_side == 'Buy' else 'SHORT'}) открыта.\n"
                            f"Объем: `{final_opened_qty}`\n"
                            f"Средств. цена (прибл.): `{position_data['total_open_value'] / final_opened_qty if final_opened_qty else 0:.4f}`\n"
                            f"Комиссия откр.: `{position_data['total_open_fee']:.4f}` USDT",
                            parse_mode='Markdown'
                        )
                        print(f"Position Opened. Total Qty: {final_opened_qty}")

                        # Запоминаем, что вошли в сделку в эту сессию фандинга
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

                        # === ПРОВЕРКА ФАКТИЧЕСКОГО ФАНДИНГА ===
                        print("Checking actual funding payment...")
                        try:
                            # Запрашиваем историю за небольшой период ПОСЛЕ времени фандинга
                            funding_check_start_time = int(next_funding_ts * 1000) # мс
                            # Берем окно в 1 минуту после фандинга для надежности
                            funding_check_end_time = int((next_funding_ts + 60) * 1000) # мс

                            funding_history = session.get_funding_history(
                                category="linear",
                                symbol=top_symbol,
                                startTime=funding_check_start_time,
                                endTime=funding_check_end_time,
                                limit=1 # Нужна только последняя запись в этом интервале
                            )
                            funding_list = funding_history.get("result", {}).get("list", [])
                            if funding_list:
                                last_funding = funding_list[0]
                                funding_fee = Decimal(last_funding.get("fundingFee", "0"))
                                funding_time_ms = int(last_funding.get("execTime", "0"))
                                # Доп. проверка, что время записи близко к ожидаемому
                                if abs(funding_time_ms / 1000 - next_funding_ts) < 60: # Разница меньше минуты
                                    position_data["actual_funding_fee"] = funding_fee
                                    print(f"Actual Funding Fee recorded: {funding_fee}")
                                    await app.bot.send_message(chat_id, f"💰 Фандинг получен: `{funding_fee:.4f}` USDT")
                                else:
                                     print(f"Funding record found, but timestamp mismatch: expected ~{next_funding_ts*1000}, got {funding_time_ms}")
                                     await app.bot.send_message(chat_id, f"⚠️ Найден фандинг, но время не совпадает. Возможно, это не та выплата.")
                            else:
                                print("No funding fee record found in the expected timeframe.")
                                await app.bot.send_message(chat_id, f"⚠️ Не удалось найти запись о выплате фандинга для {top_symbol}.")
                                position_data["actual_funding_fee"] = Decimal("0") # Считаем, что не получили

                        except Exception as e:
                            print(f"Error checking funding history: {e}")
                            await app.bot.send_message(chat_id, f"❌ Ошибка при проверке истории фандинга: {e}")
                            position_data["actual_funding_fee"] = Decimal("0") # Считаем, что не получили

                        # ==================== ЗАКРЫТИЕ ПОЗИЦИИ (Maker -> Market) ====================
                        print("\n--- Attempting to Close Position ---")
                        close_side = "Buy" if open_side == "Sell" else "Sell"
                        close_qty_remaining = final_opened_qty # Начинаем с полного открытого объема

                        # 8. Попытка закрыть через Limit PostOnly ReduceOnly
                        try:
                            orderbook = session.get_orderbook(category="linear", symbol=top_symbol, limit=1)
                            best_bid = Decimal(orderbook['result']['b'][0][0])
                            best_ask = Decimal(orderbook['result']['a'][0][0])
                            # Ставим цену ТОЧНО на лучший бид/аск для PostOnly
                            maker_price = best_bid if close_side == "Buy" else best_ask
                            maker_price_adj = quantize_price(maker_price, tick_size)
                            print(f"Attempting Maker Close ({close_side}) at {maker_price_adj}...")

                            maker_close_resp = session.place_order(
                                category="linear", symbol=top_symbol, side=close_side,
                                order_type="Limit", qty=str(close_qty_remaining),
                                price=str(maker_price_adj), time_in_force="PostOnly",
                                reduce_only=True # Важно для закрытия
                            )
                            maker_close_id = maker_close_resp.get("result", {}).get("orderId")
                            if not maker_close_id: raise ValueError("Failed to place maker close order (no ID).")
                            print(f"Maker Close Order ID: {maker_close_id}")
                            await app.bot.send_message(chat_id, f"⏳ Попытка выхода Maker @{maker_price_adj} (ID: ...{maker_close_id[-6:]})")

                            await asyncio.sleep(MAKER_ORDER_WAIT_SECONDS_EXIT)

                            # Проверяем исполнение Maker ордера закрытия
                            order_info = session.get_order_history(category="linear", orderId=maker_close_id, limit=1)
                            order_data = order_info.get("result", {}).get("list", [])
                            if order_data:
                                order_data = order_data[0]
                                cum_exec_qty = Decimal(order_data.get("cumExecQty", "0"))
                                cum_exec_value = Decimal(order_data.get("cumExecValue", "0"))
                                cum_exec_fee = Decimal(order_data.get("cumExecFee", "0"))
                                status = order_data.get("orderStatus")
                                print(f"Maker Close Order Status: {status}, Filled Qty: {cum_exec_qty}")

                                if cum_exec_qty > 0:
                                    position_data["closed_qty"] += cum_exec_qty
                                    position_data["total_close_value"] += cum_exec_value
                                    position_data["total_close_fee"] += cum_exec_fee
                                    close_qty_remaining -= cum_exec_qty
                                    await app.bot.send_message(chat_id, f"✅ Частично исполнено Maker (закрытие): {cum_exec_qty} {top_symbol}")


                                # Отменяем ордер, если он не исполнился полностью или частично
                                if status not in ["Filled", "Cancelled", "Rejected", "Deactivated"]: # Deactivated тоже бывает у reduceOnly
                                    try:
                                        print(f"Cancelling Maker Close Order {maker_close_id}...")
                                        session.cancel_order(category="linear", symbol=top_symbol, orderId=maker_close_id)
                                        print("Maker close order cancelled.")
                                    except Exception as cancel_e:
                                         if "Order does not exist" not in str(cancel_e) and "already been filled" not in str(cancel_e):
                                            print(f"Minor error cancelling maker close order: {cancel_e}")
                            else:
                                print(f"Could not get history for Maker Close Order {maker_close_id}")

                        except Exception as e:
                            print(f"Maker Close attempt failed: {e}")
                            await app.bot.send_message(chat_id, f"⚠️ Ошибка при попытке выхода Maker: {e}")

                        # 9. Добиваем остаток закрытия через Market IOC ReduceOnly
                        close_qty_remaining = quantize_qty(close_qty_remaining, qty_step) # Пересчитываем остаток
                        if close_qty_remaining >= min_qty:
                            print(f"Attempting Market Close ({close_side}) for remaining {close_qty_remaining}...")
                            await app.bot.send_message(chat_id, f"🛒 Закрываю маркетом остаток: {close_qty_remaining} {top_symbol}")
                            try:
                                market_close_resp = session.place_order(
                                    category="linear", symbol=top_symbol, side=close_side,
                                    order_type="Market", qty=str(close_qty_remaining),
                                    time_in_force="ImmediateOrCancel",
                                    reduce_only=True
                                )
                                market_close_id = market_close_resp.get("result", {}).get("orderId")
                                if not market_close_id: raise ValueError("Failed to place market close order (no ID).")
                                print(f"Market Close Order ID: {market_close_id}")

                                await asyncio.sleep(1)
                                order_info = session.get_order_history(category="linear", orderId=market_close_id, limit=1)
                                order_data = order_info.get("result", {}).get("list", [])
                                if order_data:
                                    order_data = order_data[0]
                                    cum_exec_qty = Decimal(order_data.get("cumExecQty", "0"))
                                    cum_exec_value = Decimal(order_data.get("cumExecValue", "0"))
                                    cum_exec_fee = Decimal(order_data.get("cumExecFee", "0"))
                                    status = order_data.get("orderStatus")
                                    print(f"Market Close Order Status: {status}, Filled Qty: {cum_exec_qty}")

                                    if cum_exec_qty > 0:
                                        position_data["closed_qty"] += cum_exec_qty
                                        position_data["total_close_value"] += cum_exec_value
                                        position_data["total_close_fee"] += cum_exec_fee
                                        await app.bot.send_message(chat_id, f"✅ Исполнено Маркет (закрытие): {cum_exec_qty} {top_symbol}")
                                    else:
                                         await app.bot.send_message(chat_id, f"⚠️ Маркет ордер закрытия ({market_close_id}) не исполнил ничего.")
                                else:
                                    print(f"Could not get history for Market Close Order {market_close_id}")

                            except Exception as e:
                                print(f"Market Close attempt failed: {e}")
                                await app.bot.send_message(chat_id, f"❌ Ошибка при маркет-закрытии: {e}")
                        elif close_qty_remaining > 0:
                             print(f"Remaining close qty {close_qty_remaining} is less than min qty {min_qty}. Assuming closed.")
                             # Считаем, что этот маленький остаток закрылся (или не важен)

                        # 10. Проверка итогового закрытого кол-ва
                        final_closed_qty = position_data["closed_qty"]
                        print(f"Position Closed. Total Qty: {final_closed_qty}")
                        if abs(final_closed_qty - final_opened_qty) > min_qty * Decimal("0.1"): # Если разница больше 10% от мин. лота
                             await app.bot.send_message(
                                 chat_id,
                                 f"⚠️ Позиция *{top_symbol}* закрыта не полностью!\n"
                                 f"Открыто: `{final_opened_qty}`, Закрыто: `{final_closed_qty}`.\n"
                                 f"❗️ Проверьте вручную!"
                                 , parse_mode='Markdown'
                            )
                        else:
                             await app.bot.send_message(
                                 chat_id,
                                 f"✅ Позиция *{top_symbol}* закрыта ({final_closed_qty})."
                                 , parse_mode='Markdown'
                             )


                        # ==================== РАСЧЕТ РЕАЛЬНОГО PNL ====================
                        print("\n--- Calculating Real PNL ---")
                        # PNL от изменения цены: total_close_value - total_open_value
                        # Для шорта: total_open_value - total_close_value
                        # Общая формула: (1 if short else -1) * (total_close_value - total_open_value)
                        price_pnl = position_data["total_close_value"] - position_data["total_open_value"]
                        if open_side == "Sell": # Если был шорт, инвертируем PNL цены
                             price_pnl = -price_pnl

                        # Фандинг (если не получили, будет 0 или None)
                        funding_pnl = position_data.get("actual_funding_fee") or Decimal("0")

                        # Комиссии
                        total_fees = position_data["total_open_fee"] + position_data["total_close_fee"]

                        # Итоговый чистый PNL
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
                            f"📈 ROI от маржи ({marja} USDT): `{roi_pct:.2f}%`"
                            , parse_mode='Markdown'
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
                            f"❗️ *ПРОВЕРЬТЕ СЧЕТ И ПОЗИЦИИ ВРУЧНУЮ!*"
                            , parse_mode='Markdown'
                        )
                        # Можно добавить логику для попытки аварийного закрытия позиции, если она была открыта
                        # Например, проверить get_positions() и если есть позиция - закрыть маркетом


                    finally:
                        print(f">>> Finished processing {top_symbol} for chat {chat_id} <<<")
                        # Можно добавить запись результата сделки в базу данных или лог-файл здесь


            else:
                # print(f"Not in entry window for {top_symbol} ({seconds_left:.0f}s left).")
                pass # Не спамим в лог, если не в окне входа


        except Exception as loop_e:
            print("\n!!! UNHANDLED ERROR IN SNIPER LOOP !!!")
            print(f"Error: {loop_e}")
            import traceback
            traceback.print_exc()
            # Не уведомляем пользователя об ошибках цикла, чтобы избежать спама
            await asyncio.sleep(30) # Делаем паузу подольше в случае серьезной ошибки


# ===================== MAIN =====================

if __name__ == "__main__":
    print("Initializing bot...")
    app_builder = ApplicationBuilder().token(BOT_TOKEN)
    # Увеличиваем лимиты (если нужно, зависит от хостинга и кол-ва юзеров)
    # app_builder.concurrent_updates(20)
    # app_builder.connection_pool_size(10)
    app = app_builder.build()


    # --- Добавляем хендлеры ---
    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel)) # Для выхода из диалогов

    # Кнопки главного меню (Regex)
    app.add_handler(MessageHandler(filters.Regex("^📊 Топ-пары$"), show_top_funding))
    app.add_handler(MessageHandler(filters.Regex("^📡 Сигналы$"), signal_menu))
    # Обработчик Калькулятора пока не реализован
    # app.add_handler(MessageHandler(filters.Regex("^🧮 Калькулятор прибыли$"), calculator_handler))

    # Обработчик Inline кнопок из меню сигналов
    app.add_handler(CallbackQueryHandler(signal_callback, pattern="^(toggle_sniper|show_top_pairs_inline)$"))

    # Диалог установки маржи
    conv_marja = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^💰 Маржа$"), set_real_marja)],
        states={
            SET_MARJA: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_real_marja)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        # Таймаут для диалога, если пользователь долго не отвечает
        conversation_timeout=60.0
    )
    app.add_handler(conv_marja)

    # Диалог установки плеча
    conv_plecho = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^⚖ Плечо$"), set_real_plecho)],
        states={
            SET_PLECHO: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_real_plecho)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        conversation_timeout=60.0
    )
    app.add_handler(conv_plecho)

    # --- Запуск фоновой задачи снайпера ---
    async def post_init_tasks(passed_app: ApplicationBuilder):
        print("Running post_init tasks...")
        # Запускаем цикл снайпера в фоне
        asyncio.create_task(funding_sniper_loop(passed_app))
        print("Sniper loop task created.")

    app.post_init = post_init_tasks

    # --- Запуск бота ---
    print("Starting bot polling...")
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        print(f"\nBot polling stopped due to error: {e}")
    finally:
        print("\nBot shutdown.")


# --- END OF FILE bot (7).py ---
