# --- START OF FILE bot (8).py ---

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
    ["💰 Маржа", "⚖️ Плечо"],
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
# === ИЗМЕНЕНО ЗДЕСЬ ===
POST_FUNDING_WAIT_SECONDS = 7 # Сколько секунд ждем ПОСЛЕ времени фандинга перед выходом
# =======================
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
    await update.message.reply_text("⚖ Введите размер плеча (например, 5 или 10):") # Оригинальный эмодзи был без _fe0f
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
    chat_id = query.message.chat_id
    data = query.data

    if data == "toggle_sniper":
        await query.answer()
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
        try:
            await query.edit_message_text(f"{action_text}\n📡 Меню управления снайпером:", reply_markup=reply_markup)
        except Exception as e:
            print(f"Error editing message on toggle: {e}")
            await context.bot.send_message(chat_id, f"{action_text}\n(Не удалось обновить предыдущее сообщение)")

    elif data == "show_top_pairs_inline":
        # query.answer() вызывается внутри show_top_funding
        await show_top_funding(update, context)

# ===================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =====================

def get_position_direction(rate: float) -> str:
    if rate is None: return "NONE"
    if rate < 0: return "Buy"
    elif rate > 0: return "Sell"
    else: return "NONE"

def quantize_qty(raw_qty: Decimal, qty_step: Decimal) -> Decimal:
    if qty_step <= 0: return raw_qty
    return (raw_qty // qty_step) * qty_step

def quantize_price(raw_price: Decimal, tick_size: Decimal) -> Decimal:
    if tick_size <= 0: return raw_price
    return round(raw_price / tick_size) * tick_size

# ===================== ФОНДОВЫЙ СНАЙПЕР (ФАНДИНГ-БОТ) =====================

async def funding_sniper_loop(app: ApplicationBuilder):
    print(" Sniper loop started ".center(50, "="))
    while True:
        await asyncio.sleep(SNIPER_LOOP_INTERVAL_SECONDS)
        try:
            now_ts = time.time()
            # (начало цикла, получение тикеров, фильтрация funding_data - как в полной версии)
            response = session.get_tickers(category="linear")
            tickers = response.get("result", {}).get("list", [])
            if not tickers: print("No tickers."); continue
            funding_data = []
            for t in tickers:
                symbol, rate_str, next_ts_str, _, turnover_str = t.get("symbol"), t.get("fundingRate"), t.get("nextFundingTime"), t.get("volume24h"), t.get("turnover24h")
                if not all([symbol, rate_str, next_ts_str, turnover_str]): continue
                try:
                    rate_f, next_ts_val, turnover_f = float(rate_str), int(next_ts_str) / 1000, float(turnover_str)
                    if turnover_f < 1_000_000 or abs(rate_f) < 0.0001: continue
                    funding_data.append({"symbol": symbol, "rate": rate_f, "next_ts": next_ts_val})
                except: continue # Пропускаем элемент при ошибке парсинга
            if not funding_data: print("No suitable pairs."); continue
            funding_data.sort(key=lambda x: abs(x["rate"]), reverse=True)
            top_pair = funding_data[0]
            top_symbol, rate, next_funding_ts = top_pair["symbol"], top_pair["rate"], top_pair["next_ts"]
            seconds_left = next_funding_ts - now_ts
            # print(f"Top: {top_symbol}, R: {rate*100:.4f}%, In: {seconds_left:.0f}s")

            if ENTRY_WINDOW_END_SECONDS <= seconds_left <= ENTRY_WINDOW_START_SECONDS:
                print(f"Entering trade window for {top_symbol} ({seconds_left:.0f}s left)")
                open_side = get_position_direction(rate)
                if open_side == "NONE": print("Funding rate is zero, skipping."); continue

                for chat_id, data in list(sniper_active.items()):
                    if not data.get('active'): continue
                    if (data.get("last_entry_symbol") == top_symbol and
                            data.get("last_entry_ts") == next_funding_ts):
                        continue

                    marja, plecho = data.get('real_marja'), data.get('real_plecho')
                    if not marja or not plecho: await app.bot.send_message(chat_id, f"⚠️ Пропуск {top_symbol}: Маржа/плечо не установлены."); continue

                    print(f"\n>>> Processing {top_symbol} for chat {chat_id} <<<")
                    await app.bot.send_message(
                        chat_id,
                        f"🎯 Вхожу в окно сделки: *{top_symbol}*\n"
                        f"Направление: {'📈 LONG' if open_side == 'Buy' else '📉 SHORT'}\n"
                        f"Фандинг: `{rate * 100:.4f}%`\n"
                        f"Осталось: `{seconds_left:.0f} сек`",
                         parse_mode='Markdown'
                    )
                    
                    position_data = {
                        "symbol": top_symbol, "open_side": open_side,
                        "marja": marja, "plecho": plecho,
                        "funding_rate": Decimal(str(rate)),
                        "next_funding_ts": next_funding_ts,
                        "opened_qty": Decimal("0"), "closed_qty": Decimal("0"),
                        "total_open_value": Decimal("0"), "total_close_value": Decimal("0"),
                        "total_open_fee": Decimal("0"), "total_close_fee": Decimal("0"),
                        "actual_funding_fee": Decimal("0"), # Инициализируем нулем
                        "target_qty": Decimal("0"),
                    }

                    try:
                        # --- Получение инфо и расчет кол-ва ---
                        print(f"Getting instrument info for {top_symbol}...")
                        info_resp = session.get_instruments_info(category="linear", symbol=top_symbol)
                        instrument_info = info_resp.get("result", {}).get("list", [])[0]
                        lot_filter = instrument_info["lotSizeFilter"]
                        price_filter = instrument_info["priceFilter"]
                        min_qty = Decimal(lot_filter["minOrderQty"])
                        qty_step = Decimal(lot_filter["qtyStep"])
                        tick_size = Decimal(price_filter["tickSize"])
                        
                        print(f"Getting ticker info for {top_symbol}...")
                        ticker_resp = session.get_tickers(category="linear", symbol=top_symbol)
                        last_price = Decimal(ticker_resp["result"]["list"][0]["lastPrice"])
                        
                        position_size_usdt = marja * plecho
                        if last_price <= 0: raise ValueError("Invalid last price")
                        raw_qty = position_size_usdt / last_price
                        adjusted_qty = quantize_qty(raw_qty, qty_step)
                        if adjusted_qty < min_qty: await app.bot.send_message(chat_id, f"⚠️ Расчетный объем {adjusted_qty} {top_symbol} < мин. ({min_qty}). Отмена."); continue
                        position_data["target_qty"] = adjusted_qty

                        # --- Установка плеча ---
                        print(f"Setting leverage {plecho}x for {top_symbol}...")
                        try: session.set_leverage(category="linear", symbol=top_symbol, buyLeverage=str(plecho), sellLeverage=str(plecho))
                        except Exception as e:
                            if "110043" not in str(e): raise ValueError(f"Не удалось установить плечо: {e}")
                            else: print(f"Плечо {plecho}x уже установлено.")

                        # --- ОТКРЫТИЕ (Maker -> Market) ---
                        open_qty_rem = adjusted_qty
                        # Maker Open
                        try:
                            ob_resp = session.get_orderbook(category="linear", symbol=top_symbol, limit=1)
                            ob = ob_resp['result']
                            mp = quantize_price(Decimal(ob['b'][0][0] if open_side=="Buy" else ob['a'][0][0]), tick_size)
                            resp = session.place_order(category="linear",symbol=top_symbol,side=open_side,order_type="Limit",qty=str(open_qty_rem),price=str(mp),time_in_force="PostOnly")
                            oid = resp["result"]["orderId"]
                            await app.bot.send_message(chat_id, f"⏳ Попытка входа Maker @{mp} (ID: ...{oid[-6:]})")
                            await asyncio.sleep(MAKER_ORDER_WAIT_SECONDS_ENTRY)
                            hist_resp = session.get_order_history(category="linear", orderId=oid, limit=1)
                            hist = hist_resp.get("result",{}).get("list",[])
                            if hist:
                                h = hist[0]; exec_q = Decimal(h.get("cumExecQty","0"))
                                if exec_q > 0:
                                    position_data["opened_qty"]+=exec_q; position_data["total_open_value"]+=Decimal(h.get("cumExecValue","0")); position_data["total_open_fee"]+=Decimal(h.get("cumExecFee","0")); open_qty_rem-=exec_q
                                    await app.bot.send_message(chat_id, f"✅ Частично исполнено Maker: {exec_q} {top_symbol}")
                                if h.get("orderStatus") not in ["Filled","Cancelled","Rejected"]: 
                                    try: session.cancel_order(category="linear",symbol=top_symbol,orderId=oid)
                                    except Exception as cancel_e: print(f"Minor cancel error (Maker Open): {cancel_e}")
                        except Exception as e: print(f"Maker Open attempt failed: {e}"); await app.bot.send_message(chat_id, f"⚠️ Ошибка при попытке входа Maker: {e}")
                        # Market Open
                        open_qty_rem = quantize_qty(open_qty_rem, qty_step)
                        if open_qty_rem >= min_qty:
                            await app.bot.send_message(chat_id, f"🛒 Добиваю маркетом остаток: {open_qty_rem} {top_symbol}")
                            try:
                                resp = session.place_order(category="linear",symbol=top_symbol,side=open_side,order_type="Market",qty=str(open_qty_rem),time_in_force="ImmediateOrCancel")
                                oid = resp["result"]["orderId"]; await asyncio.sleep(1.5)
                                hist_resp = session.get_order_history(category="linear",orderId=oid,limit=1)
                                hist = hist_resp.get("result",{}).get("list",[])
                                if hist:
                                    h=hist[0]; exec_q = Decimal(h.get("cumExecQty","0"))
                                    if exec_q > 0:
                                        position_data["opened_qty"]+=exec_q; position_data["total_open_value"]+=Decimal(h.get("cumExecValue","0")); position_data["total_open_fee"]+=Decimal(h.get("cumExecFee","0"))
                                        await app.bot.send_message(chat_id, f"✅ Исполнено Маркет: {exec_q} {top_symbol}")
                                    else: await app.bot.send_message(chat_id, f"⚠️ Маркет ордер ({oid}) не исполнил ничего.")
                            except Exception as e: print(f"Market Open attempt failed: {e}"); await app.bot.send_message(chat_id, f"❌ Ошибка при добивании маркетом: {e}")
                        
                        final_opened_qty = position_data["opened_qty"]
                        if final_opened_qty < min_qty: await app.bot.send_message(chat_id, f"❌ Не открыт мин. объем ({min_qty}). Открыто: {final_opened_qty}. Отмена."); continue
                        avg_op = f"{position_data['total_open_value']/final_opened_qty:.4f}" if final_opened_qty else "N/A"
                        await app.bot.send_message(chat_id, f"✅ Позиция *{top_symbol}* ({'LONG' if open_side=='Buy' else 'SHORT'}) открыта.\nОбъем: `{final_opened_qty}`, Ср.цена: `{avg_op}`, Ком.откр: `{position_data['total_open_fee']:.4f}`", parse_mode='Markdown')
                        data["last_entry_symbol"], data["last_entry_ts"] = top_symbol, next_funding_ts

                        # --- ОЖИДАНИЕ И ПРОВЕРКА ФАНДИНГА ---
                        wait_duration = max(0, next_funding_ts - time.time()) + POST_FUNDING_WAIT_SECONDS
                        await app.bot.send_message(chat_id, f"⏳ Ожидаю выплаты фандинга (~{wait_duration:.0f} сек)...")
                        await asyncio.sleep(wait_duration)

                        # === ИСПРАВЛЕНО ЗДЕСЬ: Проверка фандинга через Transaction Log ===
                        print("Checking actual funding payment using Transaction Log...")
                        try:
                            start_ts_ms = int((next_funding_ts - 120) * 1000) 
                            end_ts_ms = int((next_funding_ts + 120) * 1000)   
                            
                            transaction_log_resp = session.get_transaction_log(
                                category="linear", 
                                symbol=top_symbol, 
                                type="SETTLEMENT",
                                startTime=start_ts_ms,
                                endTime=end_ts_ms,
                                limit=10 
                            )
                            log_list = transaction_log_resp.get("result", {}).get("list", [])
                            found_funding_in_log = Decimal("0")
                            
                            if log_list:
                                for entry in log_list:
                                    change_str = entry.get("change", "0")
                                    exec_time_ms = int(entry.get("transactionTime", "0"))
                                    if abs(exec_time_ms / 1000 - next_funding_ts) < 60: 
                                        found_funding_in_log += Decimal(change_str)
                                        print(f"Found Funding Log: Time {datetime.fromtimestamp(exec_time_ms/1000)}, Change: {change_str}, Symbol: {entry.get('symbol')}")
                                
                                if found_funding_in_log != Decimal("0"):
                                    position_data["actual_funding_fee"] = found_funding_in_log
                                    await app.bot.send_message(chat_id, f"💰 Фандинг (из лога): `{found_funding_in_log:.4f}` USDT", parse_mode='Markdown')
                                else:
                                    await app.bot.send_message(chat_id, f"⚠️ Не найдено SETTLEMENT для {top_symbol} в логе в ожидаемое время.")
                            else:
                                await app.bot.send_message(chat_id, f"⚠️ Лог транзакций пуст для {top_symbol} в указ. период.")
                        
                        except Exception as e:
                            print(f"Error checking transaction log: {e}"); import traceback; traceback.print_exc()
                            await app.bot.send_message(chat_id, f"❌ Ошибка при проверке лога транзакций: {e}")
                        # ==================================================================

                        # --- ЗАКРЫТИЕ (Maker -> Market) ---
                        close_side = "Buy" if open_side == "Sell" else "Sell"
                        close_qty_rem = final_opened_qty
                        # Maker Close
                        try:
                            ob_resp = session.get_orderbook(category="linear",symbol=top_symbol,limit=1)
                            ob = ob_resp['result']
                            mp = quantize_price(Decimal(ob['b'][0][0] if close_side=="Buy" else ob['a'][0][0]), tick_size)
                            resp = session.place_order(category="linear",symbol=top_symbol,side=close_side,order_type="Limit",qty=str(close_qty_rem),price=str(mp),time_in_force="PostOnly",reduce_only=True)
                            oid = resp["result"]["orderId"]
                            await app.bot.send_message(chat_id, f"⏳ Попытка выхода Maker @{mp} (ID: ...{oid[-6:]})")
                            await asyncio.sleep(MAKER_ORDER_WAIT_SECONDS_EXIT)
                            hist_resp = session.get_order_history(category="linear",orderId=oid,limit=1)
                            hist = hist_resp.get("result",{}).get("list",[])
                            if hist:
                                h=hist[0]; exec_q=Decimal(h.get("cumExecQty","0"))
                                if exec_q > 0:
                                    position_data["closed_qty"]+=exec_q; position_data["total_close_value"]+=Decimal(h.get("cumExecValue","0")); position_data["total_close_fee"]+=Decimal(h.get("cumExecFee","0")); close_qty_rem-=exec_q
                                    await app.bot.send_message(chat_id, f"✅ Частично исполнено Maker (закрытие): {exec_q}")
                                if h.get("orderStatus") not in ["Filled","Cancelled","Rejected","Deactivated"]: 
                                    try: session.cancel_order(category="linear",symbol=top_symbol,orderId=oid)
                                    except Exception as cancel_e: print(f"Minor cancel error (Maker Close): {cancel_e}")
                        except Exception as e: print(f"Maker Close attempt failed: {e}"); await app.bot.send_message(chat_id, f"⚠️ Ошибка при попытке выхода Maker: {e}")
                        # Market Close
                        close_qty_rem = quantize_qty(close_qty_rem, qty_step)
                        if close_qty_rem >= min_qty:
                            await app.bot.send_message(chat_id, f"🛒 Закрываю маркетом остаток: {close_qty_rem} {top_symbol}")
                            try:
                                resp = session.place_order(category="linear",symbol=top_symbol,side=close_side,order_type="Market",qty=str(close_qty_rem),time_in_force="ImmediateOrCancel",reduce_only=True)
                                oid = resp["result"]["orderId"]; await asyncio.sleep(1.5)
                                hist_resp = session.get_order_history(category="linear",orderId=oid,limit=1)
                                hist = hist_resp.get("result",{}).get("list",[])
                                if hist:
                                    h=hist[0]; exec_q=Decimal(h.get("cumExecQty","0"))
                                    if exec_q > 0:
                                        position_data["closed_qty"]+=exec_q; position_data["total_close_value"]+=Decimal(h.get("cumExecValue","0")); position_data["total_close_fee"]+=Decimal(h.get("cumExecFee","0"))
                                        await app.bot.send_message(chat_id, f"✅ Исполнено Маркет (закрытие): {exec_q}")
                                    else: await app.bot.send_message(chat_id, f"⚠️ Маркет ордер закрытия ({oid}) не исполнил ничего.")
                            except Exception as e: print(f"Market Close attempt failed: {e}"); await app.bot.send_message(chat_id, f"❌ Ошибка при маркет-закрытии: {e}")
                        
                        final_closed_qty = position_data["closed_qty"]
                        if abs(final_closed_qty - final_opened_qty) > min_qty * Decimal("0.1"): await app.bot.send_message(chat_id, f"⚠️ Позиция *{top_symbol}* не полностью закрыта! Откр: `{final_opened_qty}`, Закр: `{final_closed_qty}`. ПРОВЕРЬТЕ!", parse_mode='Markdown')
                        else: await app.bot.send_message(chat_id, f"✅ Позиция *{top_symbol}* успешно закрыта ({final_closed_qty}).", parse_mode='Markdown')

                        # --- РАСЧЕТ PNL ---
                        price_pnl = position_data["total_close_value"] - position_data["total_open_value"]
                        if open_side == "Sell": price_pnl = -price_pnl
                        funding_pnl = position_data["actual_funding_fee"] 
                        total_fees = position_data["total_open_fee"] + position_data["total_close_fee"]
                        net_pnl = price_pnl + funding_pnl - total_fees
                        roi_pct = (net_pnl / marja) * 100 if marja != Decimal(0) else Decimal("0") # Проверка деления на ноль
                        await app.bot.send_message(
                            chat_id, 
                            f"📊 Результат сделки: *{top_symbol}* ({'LONG' if open_side=='Buy' else 'SHORT'})\n\n"
                            f" PNL (цена): `{price_pnl:+.4f}` USDT\n"
                            f" PNL (фандинг): `{funding_pnl:+.4f}` USDT\n"
                            f" Комиссии (откр+закр): `{-total_fees:.4f}` USDT\n"
                            f"💰 *Чистая прибыль: {net_pnl:+.4f} USDT*\n"
                            f"📈 ROI от маржи ({marja} USDT): `{roi_pct:.2f}%`", 
                            parse_mode='Markdown'
                        )
                        trade_success = True

                    except Exception as trade_e:
                        print(f"\n!!! CRITICAL TRADE ERROR for chat {chat_id}, symbol {top_symbol} !!!")
                        print(f"Error: {trade_e}"); import traceback; traceback.print_exc()
                        await app.bot.send_message(chat_id, f"❌ КРИТИЧЕСКАЯ ОШИБКА во время сделки по *{top_symbol}*:\n`{trade_e}`\n\n❗️ *ПРОВЕРЬТЕ СЧЕТ И ПОЗИЦИИ ВРУЧНУЮ!*", parse_mode='Markdown')
                    finally:
                        print(f">>> Finished processing {top_symbol} for chat {chat_id} <<<")
            else:
                # print(f"Not in entry window for {top_symbol} ({seconds_left:.0f}s left).")
                pass
        except Exception as loop_e:
            print("\n!!! UNHANDLED ERROR IN SNIPER LOOP !!!")
            print(f"Error: {loop_e}"); import traceback; traceback.print_exc()
            await asyncio.sleep(30)

# ===================== MAIN =====================

if __name__ == "__main__":
    print("Initializing bot...")
    app_builder = ApplicationBuilder().token(BOT_TOKEN)
    app = app_builder.build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(MessageHandler(filters.Regex("^📊 Топ-пары$"), show_top_funding))
    app.add_handler(MessageHandler(filters.Regex("^📡 Сигналы$"), signal_menu))
    app.add_handler(CallbackQueryHandler(signal_callback, pattern="^(toggle_sniper|show_top_pairs_inline)$"))

    conv_marja = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^💰 Маржа$"), set_real_marja)],
        states={SET_MARJA: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_real_marja)]},
        fallbacks=[CommandHandler("cancel", cancel)],
        conversation_timeout=60.0
    )
    app.add_handler(conv_marja)

    conv_plecho = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^⚖️ Плечо$"), set_real_plecho)], # Исправлен эмодзи для соответствия клавиатуре
        states={SET_PLECHO: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_real_plecho)]},
        fallbacks=[CommandHandler("cancel", cancel)],
        conversation_timeout=60.0
    )
    app.add_handler(conv_plecho)

    async def post_init_tasks(passed_app: ApplicationBuilder):
        print("Running post_init tasks...")
        asyncio.create_task(funding_sniper_loop(passed_app))
        print("Sniper loop task created.")
    app.post_init = post_init_tasks

    print("Starting bot polling...")
    try:
        app.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        print(f"\nBot polling stopped due to error: {e}")
    finally:
        print("\nBot shutdown.")

# --- END OF FILE bot (8).py ---
