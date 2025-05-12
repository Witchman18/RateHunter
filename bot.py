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
MAKER_ORDER_WAIT_SECONDS_ENTRY = 7 # Сколько секунд ждем исполнения PostOnly ордера на ВХОД
MAKER_ORDER_WAIT_SECONDS_EXIT = 5  # Сколько секунд ждем исполнения PostOnly ордера на ВЫХОД
SNIPER_LOOP_INTERVAL_SECONDS = 5 # Как часто проверяем тикеры в основном цикле
DEFAULT_MAX_CONCURRENT_TRADES = 1 # Одна сделка дефолт 

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

async def send_final_config_message(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Отправляет итоговое сообщение с настройками."""
    if chat_id not in sniper_active:
        print(f"[send_final_config_message] No data found for chat_id {chat_id}") # Добавим лог
        return # Нет данных для этого чата

    settings = sniper_active[chat_id]
    # Получаем значения. Если ключа нет или значение None, будет None.
    marja = settings.get('real_marja')
    plecho = settings.get('real_plecho')
    max_trades = settings.get('max_concurrent_trades', DEFAULT_MAX_CONCURRENT_TRADES)
    is_active = settings.get('active', False)
    status_text = "🟢 Активен" if is_active else "🔴 Остановлен"

    # Отображаемые значения для сообщения
    marja_display = marja if marja is not None else 'Не установлено'
    plecho_display = plecho if plecho is not None else 'Не установлено'

    print(f"[send_final_config_message] Checking for chat {chat_id}: marja={marja}, plecho={plecho}") # Добавим лог

    # *** ИСПРАВЛЕННАЯ ПРОВЕРКА ***
    # Проверяем, что оба значения больше НЕ None
    if marja is not None and plecho is not None:
        summary_text = (
            f"⚙️ **Текущие настройки RateHunter:**\n\n"
            f"💰 Маржа (1 сделка): `{marja_display}` USDT\n" # Используем _display для текста
            f"⚖️ Плечо: `{plecho_display}`x\n"
            f"🔢 Макс. сделок: `{max_trades}`\n"
            f"🚦 Статус сигналов: *{status_text}*"
        )
        try:
            print(f"[send_final_config_message] Sending summary to chat {chat_id}") # Добавим лог
            await context.bot.send_message(chat_id=chat_id, text=summary_text, parse_mode='Markdown')
        except Exception as e:
            print(f"Error sending final config message to {chat_id}: {e}")
    else:
        print(f"[send_final_config_message] Condition not met for chat {chat_id}. Not sending summary.") # Добавим лог
        # Ничего не отправляем, если не все настроено
        pass

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
             # Завершаем диалог, если значение некорректно
             return ConversationHandler.END

        if chat_id not in sniper_active:
            # Инициализация полной структуры
            sniper_active[chat_id] = {
                'active': False,
                'real_marja': None,
                'real_plecho': None,
                'max_concurrent_trades': DEFAULT_MAX_CONCURRENT_TRADES,
                'ongoing_trades': {},
            }
        # Устанавливаем маржу
        sniper_active[chat_id]["real_marja"] = marja
        await update.message.reply_text(f"✅ Маржа для сделки установлена: {marja} USDT")

        # --- ВЫЗОВ ИТОГОВОГО СООБЩЕНИЯ ---
        # Вызываем функцию здесь, после успешного сохранения
        await send_final_config_message(chat_id, context)
        # ------------------------------------

    except (ValueError, TypeError): # Объединяем обработку ошибок формата
        await update.message.reply_text("❌ Неверный формат маржи. Введите число (например, 100 или 55.5).")
        # Не завершаем диалог, позволяем пользователю попробовать еще раз или отменить
        # return ConversationHandler.END # Раскомментируйте, если хотите завершать при ошибке формата
        # Вместо завершения, можем вернуть состояние, чтобы запросить ввод снова:
        return SET_MARJA
    except Exception as e: # Отлавливаем другие возможные ошибки
        print(f"Error in save_real_marja: {e}")
        await update.message.reply_text("❌ Произошла ошибка при сохранении маржи.")
        return ConversationHandler.END # Завершаем при других ошибках

    # Завершаем диалог только при полном успехе
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
        if not (0 < plecho <= 100): # Проверяем диапазон
             await update.message.reply_text("❌ Плечо должно быть положительным числом (обычно до 100).")
             # Завершаем диалог, если значение некорректно
             return ConversationHandler.END

        if chat_id not in sniper_active:
             # Инициализация полной структуры
            sniper_active[chat_id] = {
                'active': False,
                'real_marja': None,
                'real_plecho': None,
                'max_concurrent_trades': DEFAULT_MAX_CONCURRENT_TRADES,
                'ongoing_trades': {},
            }
        # Устанавливаем плечо
        sniper_active[chat_id]["real_plecho"] = plecho
        await update.message.reply_text(f"✅ Плечо установлено: {plecho}x")

        # --- ВЫЗОВ ИТОГОВОГО СООБЩЕНИЯ ---
        # Вызываем функцию здесь, после успешного сохранения
        await send_final_config_message(chat_id, context)
        # ------------------------------------

    except (ValueError, TypeError): # Объединяем обработку ошибок формата
        await update.message.reply_text("❌ Неверный формат плеча. Введите число (например, 10).")
        # Не завершаем диалог, позволяем пользователю попробовать еще раз или отменить
        # return ConversationHandler.END # Раскомментируйте, если хотите завершать при ошибке формата
        # Вместо завершения, можем вернуть состояние, чтобы запросить ввод снова:
        return SET_PLECHO
    except Exception as e: # Отлавливаем другие возможные ошибки
         print(f"Error in save_real_plecho: {e}")
         await update.message.reply_text("❌ Произошла ошибка при сохранении плеча.")
         return ConversationHandler.END # Завершаем при других ошибках

    # Завершаем диалог только при полном успехе
    return ConversationHandler.END

# ===================== МЕНЮ СИГНАЛОВ =====================

async def signal_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    # Получаем текущие настройки или значения по умолчанию
    chat_settings = sniper_active.get(chat_id, {})
    is_active = chat_settings.get('active', False)
    current_max_trades = chat_settings.get('max_concurrent_trades', DEFAULT_MAX_CONCURRENT_TRADES)

    status_text = "🟢 Активен" if is_active else "🔴 Остановлен"
    status_button = InlineKeyboardButton(f"Статус: {status_text}", callback_data="toggle_sniper")
    top_pairs_button = InlineKeyboardButton("📊 Показать топ пар", callback_data="show_top_pairs_inline")

    # Создаем кнопки для выбора количества сделок
    trade_limit_buttons = []
    for i in range(1, 6): # Кнопки от 1 до 5
        text = f"{i}" if i == current_max_trades else f"{i}" # Выделяем текущее значение
        trade_limit_buttons.append(InlineKeyboardButton(text, callback_data=f"set_max_trades_{i}"))

    # Собираем клавиатуру
    buttons = [
        [status_button],
        trade_limit_buttons, # Ряд кнопок [1] [2] [3] [4] [5]
        [top_pairs_button]
    ]
    reply_markup = InlineKeyboardMarkup(buttons)

    # Сообщение меню
    menu_text = (
        f"📡 Меню управления снайпером:\n\n"
        f"Лимит одновременных сделок: *{current_max_trades}*\n"
        f"(Нажмите на цифру ниже, чтобы изменить)"
    )
    # Используем reply_text для отправки нового сообщения при вызове через команду / Сигналы
    # Если нужно будет редактировать существующее, логику нужно будет усложнить
    await update.message.reply_text(menu_text, reply_markup=reply_markup, parse_mode='Markdown')

async def signal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer() # Отвечаем на callback сразу

    chat_id = query.message.chat_id
    data = query.data

    # --- Инициализация настроек чата, если их нет ---
    if chat_id not in sniper_active:
        sniper_active[chat_id] = {
            'active': False,
            'real_marja': None,
            'real_plecho': None,
            'max_concurrent_trades': DEFAULT_MAX_CONCURRENT_TRADES,
            'ongoing_trades': {},
        }
    chat_settings = sniper_active[chat_id]
    # --- Конец инициализации ---

    action_text = "" # Текст для подтверждения действия

    if data == "toggle_sniper":
        new_status = not chat_settings.get('active', False)
        chat_settings['active'] = new_status
        action_text = "🚀 Снайпер запущен!" if new_status else "🛑 Снайпер остановлен."

    elif data.startswith("set_max_trades_"):
        try:
            new_max_trades = int(data.split("_")[-1])
            if 1 <= new_max_trades <= 5:
                chat_settings['max_concurrent_trades'] = new_max_trades
                action_text = f"✅ Лимит сделок изменен на: {new_max_trades}"
            else:
                action_text = "⚠️ Ошибка: Неверное значение лимита."
        except (ValueError, IndexError):
             action_text = "⚠️ Ошибка: Не удалось обработать значение лимита."

    elif data == "show_top_pairs_inline":
        # Эта команда сама редактирует сообщение, выходим из коллбека здесь
        await show_top_funding(update, context)
        return # Важно выйти, чтобы не пытаться редактировать сообщение ниже

    # --- Перерисовка меню после toggle_sniper или set_max_trades ---
    if data == "toggle_sniper" or data.startswith("set_max_trades_"):
        current_status = chat_settings.get('active', False)
        current_max_trades = chat_settings.get('max_concurrent_trades', DEFAULT_MAX_CONCURRENT_TRADES)
        status_text = "🟢 Активен" if current_status else "🔴 Остановлен"

        status_button = InlineKeyboardButton(f"Статус: {status_text}", callback_data="toggle_sniper")
        top_pairs_button = InlineKeyboardButton("📊 Показать топ пар", callback_data="show_top_pairs_inline")

        trade_limit_buttons = []
        for i in range(1, 6):
            text = f"{i}" if i == current_max_trades else f"{i}"
            trade_limit_buttons.append(InlineKeyboardButton(text, callback_data=f"set_max_trades_{i}"))

        buttons = [
            [status_button],
            trade_limit_buttons,
            [top_pairs_button]
        ]
        reply_markup = InlineKeyboardMarkup(buttons)

        menu_text = (
            f"{action_text}\n\n" # Добавляем результат действия
            f"📡 Меню управления снайпером:\n\n"
            f"Лимит одновременных сделок: *{current_max_trades}*\n"
            f"(Нажмите на цифру ниже, чтобы изменить)"
        )
        try:
            await query.edit_message_text(
                text=menu_text,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
        except Exception as e:
            print(f"Error editing message on callback {data}: {e}")
            # Если редактирование не удалось, можно отправить новое сообщение с подтверждением
            await context.bot.send_message(chat_id, action_text + "\n(Не удалось обновить меню)")

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

# ===================== НОВЫЕ ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ ТОРГОВЛИ =====================
# (ВСТАВЬТЕ ЭТОТ БЛОК ПЕРЕД funding_sniper_loop)

async def get_order_status_robust(session, order_id, symbol, category="linear", max_retries=3, delay=0.5):
    """Надежно получает статус ордера с несколькими попытками."""
    for attempt in range(max_retries):
        try:
            # Используем get_order_history, так как он показывает и исполненные, и отмененные
            # Если нужен только активный ордер, можно get_open_orders, но история надежнее для пост-проверки
            response = session.get_order_history(
                category=category,
                orderId=order_id,
                limit=1 # Нам нужен только этот ордер
            )
            if response and response.get("retCode") == 0 and response.get("result", {}).get("list"):
                order_data = response["result"]["list"][0]
                if order_data.get("orderId") == order_id: # Убедимся, что это тот самый ордер
                    return order_data # Возвращаем всю информацию об ордере
            print(f"[get_order_status_robust] Попытка {attempt+1}: Не найден ордер {order_id} или ошибка API: {response}")
        except Exception as e:
            print(f"[get_order_status_robust] Попытка {attempt+1}: Ошибка при запросе статуса ордера {order_id}: {e}")
        if attempt < max_retries - 1:
            await asyncio.sleep(delay)
    return None # Если все попытки неудачны


async def place_limit_order_with_retry(
    session, app, chat_id,
    symbol, side, qty, price,
    time_in_force="PostOnly", reduce_only=False,
    max_wait_seconds=MAKER_ORDER_WAIT_SECONDS_ENTRY, # Используем вашу константу
    check_interval_seconds=0.5
):
    """
    Размещает лимитный ордер и ждет его исполнения или отмены.
    Возвращает словарь с результатом или None в случае неудачи.
    Результат: {'status': 'Filled'/'PartiallyFilled'/'Cancelled'/'Error', 'executed_qty': Decimal, 'avg_price': Decimal, 'fee': Decimal, 'message': str}
    """
    order_id = None
    try:
        params = {
            "category": "linear",
            "symbol": symbol,
            "side": side,
            "orderType": "Limit",
            "qty": str(qty),
            "price": str(price),
            "timeInForce": time_in_force
        }
        if reduce_only:
            params["reduceOnly"] = True

        print(f"Размещаю ЛИМИТНЫЙ ордер: {side} {qty} {symbol} @ {price} (ReduceOnly: {reduce_only})")
        response = session.place_order(**params)
        
        if response and response.get("retCode") == 0 and response.get("result", {}).get("orderId"):
            order_id = response["result"]["orderId"]
            await app.bot.send_message(chat_id, f"⏳ {('Выход' if reduce_only else 'Вход')} Maker @{price} (ID: ...{order_id[-6:]})")

            waited_seconds = 0
            while waited_seconds < max_wait_seconds:
                await asyncio.sleep(check_interval_seconds)
                waited_seconds += check_interval_seconds
                
                order_info = await get_order_status_robust(session, order_id, symbol)
                if order_info:
                    order_status = order_info.get("orderStatus")
                    cum_exec_qty = Decimal(order_info.get("cumExecQty", "0"))
                    avg_price = Decimal(order_info.get("avgPrice", "0")) # avgPrice может быть "0" если не исполнен
                    if avg_price == Decimal("0") and cum_exec_qty > 0 and Decimal(order_info.get("cumExecValue", "0")) > 0: # Для V5 avgPrice может быть 0, если ордер только создан
                         avg_price = Decimal(order_info.get("cumExecValue", "0")) / cum_exec_qty

                    cum_exec_fee = Decimal(order_info.get("cumExecFee", "0"))

                    if order_status == "Filled":
                        await app.bot.send_message(chat_id, f"✅ Maker ордер ...{order_id[-6:]} ПОЛНОСТЬЮ исполнен: {cum_exec_qty} {symbol}")
                        return {'status': 'Filled', 'executed_qty': cum_exec_qty, 'avg_price': avg_price, 'fee': cum_exec_fee, 'message': 'Filled'}
                    elif order_status == "PartiallyFilled":
                        # Продолжаем ждать, но запоминаем текущее исполнение
                        print(f"Maker ордер ...{order_id[-6:]} ЧАСТИЧНО исполнен: {cum_exec_qty}. Ждем дальше.")
                        continue # Не выходим, ждем полного исполнения или таймаута
                    elif order_status in ["Cancelled", "Rejected", "Deactivated", "Expired"]:
                        msg = f"⚠️ Maker ордер ...{order_id[-6:]} {order_status}. Исполнено: {cum_exec_qty}"
                        await app.bot.send_message(chat_id, msg)
                        return {'status': order_status, 'executed_qty': cum_exec_qty, 'avg_price': avg_price, 'fee': cum_exec_fee, 'message': msg}
                else:
                    print(f"Не удалось получить статус для Maker ордера ...{order_id[-6:]}. Попытка {int(waited_seconds/check_interval_seconds)}.")
            
            # Таймаут ожидания
            final_order_info = await get_order_status_robust(session, order_id, symbol)
            if final_order_info:
                order_status = final_order_info.get("orderStatus")
                cum_exec_qty = Decimal(final_order_info.get("cumExecQty", "0"))
                avg_price = Decimal(final_order_info.get("avgPrice", "0"))
                if avg_price == Decimal("0") and cum_exec_qty > 0 and Decimal(final_order_info.get("cumExecValue", "0")) > 0:
                     avg_price = Decimal(final_order_info.get("cumExecValue", "0")) / cum_exec_qty
                cum_exec_fee = Decimal(final_order_info.get("cumExecFee", "0"))

                if order_status not in ["Filled", "Cancelled", "Rejected", "Deactivated", "Expired"]:
                    try:
                        print(f"Отменяю Maker ордер ...{order_id[-6:]} по таймауту.")
                        session.cancel_order(category="linear", symbol=symbol, orderId=order_id)
                        await app.bot.send_message(chat_id, f"⏳ Maker ордер ...{order_id[-6:]} отменен по таймауту. Исполнено: {cum_exec_qty}")
                        return {'status': 'CancelledByTimeout', 'executed_qty': cum_exec_qty, 'avg_price': avg_price, 'fee': cum_exec_fee, 'message': 'Cancelled by timeout'}
                    except Exception as cancel_e:
                        await app.bot.send_message(chat_id, f"❌ Ошибка отмены Maker ордера ...{order_id[-6:]}: {cancel_e}")
                        return {'status': 'ErrorCancelling', 'executed_qty': cum_exec_qty, 'avg_price': avg_price, 'fee': cum_exec_fee, 'message': str(cancel_e)}
                else: # Если он уже сам отменился/исполнился пока мы ждали
                     return {'status': order_status, 'executed_qty': cum_exec_qty, 'avg_price': avg_price, 'fee': cum_exec_fee, 'message': f'Final status: {order_status}'}
            else:
                 await app.bot.send_message(chat_id, f"⚠️ Не удалось получить финальный статус для Maker ордера ...{order_id[-6:]} после таймаута.")
                 return {'status': 'ErrorNoStatus', 'executed_qty': Decimal("0"), 'avg_price': Decimal("0"), 'fee': Decimal("0"), 'message': 'Could not get final status'}

        else:
            err_msg = f"Ошибка размещения Maker ордера: {response.get('retMsg', 'Unknown error') if response else 'No response'}"
            print(err_msg)
            await app.bot.send_message(chat_id, f"❌ {err_msg}")
            return {'status': 'ErrorPlacing', 'executed_qty': Decimal("0"), 'avg_price': Decimal("0"), 'fee': Decimal("0"), 'message': err_msg}

    except Exception as e:
        error_text = f"КРИТ.ОШИБКА в place_limit_order_with_retry: {e}"
        print(error_text)
        import traceback
        traceback.print_exc()
        await app.bot.send_message(chat_id, f"❌ {error_text}")
        if order_id: # Если ордер был создан, но потом произошла ошибка
             # Попытаться получить его статус и вернуть хоть что-то
            final_order_info_on_exc = await get_order_status_robust(session, order_id, symbol)
            if final_order_info_on_exc:
                cum_exec_qty = Decimal(final_order_info_on_exc.get("cumExecQty", "0"))
                avg_price = Decimal(final_order_info_on_exc.get("avgPrice", "0"))
                if avg_price == Decimal("0") and cum_exec_qty > 0 and Decimal(final_order_info_on_exc.get("cumExecValue", "0")) > 0:
                     avg_price = Decimal(final_order_info_on_exc.get("cumExecValue", "0")) / cum_exec_qty
                cum_exec_fee = Decimal(final_order_info_on_exc.get("cumExecFee", "0"))
                return {'status': 'ExceptionAfterPlace', 'executed_qty': cum_exec_qty, 'avg_price': avg_price, 'fee': cum_exec_fee, 'message': str(e)}
        return {'status': 'Exception', 'executed_qty': Decimal("0"), 'avg_price': Decimal("0"), 'fee': Decimal("0"), 'message': str(e)}


async def place_market_order_robust(
    session, app, chat_id,
    symbol, side, qty,
    time_in_force="ImmediateOrCancel", reduce_only=False
):
    """
    Размещает рыночный ордер и проверяет его исполнение.
    Возвращает словарь с результатом или None в случае неудачи.
    Результат: {'status': 'Filled'/'PartiallyFilled'/'Error', 'executed_qty': Decimal, 'avg_price': Decimal, 'fee': Decimal, 'message': str}
    """
    try:
        params = {
            "category": "linear",
            "symbol": symbol,
            "side": side,
            "orderType": "Market",
            "qty": str(qty),
            "timeInForce": time_in_force
        }
        if reduce_only:
            params["reduceOnly"] = True

        print(f"Размещаю РЫНОЧНЫЙ ордер: {side} {qty} {symbol} (ReduceOnly: {reduce_only})")
        response = session.place_order(**params)

        if response and response.get("retCode") == 0 and response.get("result", {}).get("orderId"):
            order_id = response["result"]["orderId"]
            await app.bot.send_message(chat_id, f"🛒 Маркет ордер ({('выход' if reduce_only else 'вход')}) отправлен (ID: ...{order_id[-6:]}). Проверяю исполнение...")
            
            await asyncio.sleep(1.5) # Даем бирже время обработать рыночный ордер IOC

            order_info = await get_order_status_robust(session, order_id, symbol)
            if order_info:
                order_status = order_info.get("orderStatus")
                cum_exec_qty = Decimal(order_info.get("cumExecQty", "0"))
                avg_price = Decimal(order_info.get("avgPrice", "0"))
                if avg_price == Decimal("0") and cum_exec_qty > 0 and Decimal(order_info.get("cumExecValue", "0")) > 0:
                     avg_price = Decimal(order_info.get("cumExecValue", "0")) / cum_exec_qty
                cum_exec_fee = Decimal(order_info.get("cumExecFee", "0"))

                if order_status == "Filled":
                    await app.bot.send_message(chat_id, f"✅ Маркет ордер ...{order_id[-6:]} ИСПОЛНЕН: {cum_exec_qty} {symbol}")
                    return {'status': 'Filled', 'executed_qty': cum_exec_qty, 'avg_price': avg_price, 'fee': cum_exec_fee, 'message': 'Market Filled'}
                elif order_status == "PartiallyFilled" and time_in_force == "ImmediateOrCancel": # Для IOC это частичное исполнение
                    await app.bot.send_message(chat_id, f"✅ Маркет IOC ордер ...{order_id[-6:]} ЧАСТИЧНО ИСПОЛНЕН: {cum_exec_qty} {symbol}")
                    return {'status': 'PartiallyFilled', 'executed_qty': cum_exec_qty, 'avg_price': avg_price, 'fee': cum_exec_fee, 'message': 'Market IOC PartiallyFilled'}
                elif cum_exec_qty == Decimal("0") and order_status in ["Cancelled", "Rejected", "Deactivated"]: # IOC не исполнился
                    msg = f"⚠️ Маркет IOC ордер ...{order_id[-6:]} не исполнил ничего (статус: {order_status})."
                    await app.bot.send_message(chat_id, msg)
                    return {'status': order_status, 'executed_qty': Decimal("0"), 'avg_price': Decimal("0"), 'fee': Decimal("0"), 'message': msg}
                else: # Неожиданный статус для рыночного ордера
                    msg = f"⚠️ Неожиданный статус для Маркет ордера ...{order_id[-6:]}: {order_status}. Исполнено: {cum_exec_qty}"
                    await app.bot.send_message(chat_id, msg)
                    return {'status': order_status, 'executed_qty': cum_exec_qty, 'avg_price': avg_price, 'fee': cum_exec_fee, 'message': msg}
            else:
                msg = f"⚠️ Не удалось получить статус для Маркет ордера ...{order_id[-6:]}."
                await app.bot.send_message(chat_id, msg)
                # В этом случае мы не знаем, что произошло. Лучше считать, что не исполнился.
                return {'status': 'ErrorNoStatusMarket', 'executed_qty': Decimal("0"), 'avg_price': Decimal("0"), 'fee': Decimal("0"), 'message': msg}
        else:
            # Проверяем код ошибки 110007 "ab not enough for new order"
            ret_msg = response.get('retMsg', 'Unknown error') if response else 'No response'
            error_code = response.get('retCode') if response else None
            if error_code == 110007 or "not enough" in ret_msg.lower() or "insufficient" in ret_msg.lower():
                 err_msg = f"❌ Ошибка 'Недостаточно средств' при размещении Маркет ордера: {ret_msg}"
            else:
                 err_msg = f"❌ Ошибка размещения Маркет ордера: {ret_msg}"
            print(err_msg)
            await app.bot.send_message(chat_id, err_msg)
            return {'status': 'ErrorPlacingMarket', 'executed_qty': Decimal("0"), 'avg_price': Decimal("0"), 'fee': Decimal("0"), 'message': err_msg}

    except Exception as e:
        error_text = f"КРИТ.ОШИБКА в place_market_order_robust: {e}"
        print(error_text)
        import traceback
        traceback.print_exc()
        await app.bot.send_message(chat_id, f"❌ {error_text}")
        return {'status': 'ExceptionMarket', 'executed_qty': Decimal("0"), 'avg_price': Decimal("0"), 'fee': Decimal("0"), 'message': str(e)}


async def get_current_position_info(session, symbol, category="linear"):
    """Получает информацию о текущей открытой позиции для символа."""
    try:
        response = session.get_positions(category=category, symbol=symbol)
        if response and response.get("retCode") == 0:
            pos_list = response.get("result", {}).get("list", [])
            if pos_list:
                # Обычно для одного символа в режиме One-Way будет одна запись
                # или две для Hedge Mode (но мы ориентируемся на One-Way)
                for pos_data in pos_list:
                    if pos_data.get("symbol") == symbol and Decimal(pos_data.get("size", "0")) > 0:
                        return {
                            "size": Decimal(pos_data.get("size", "0")),
                            "side": pos_data.get("side"), # "Buy", "Sell", or "None"
                            "avg_price": Decimal(pos_data.get("avgPrice", "0")),
                            "liq_price": Decimal(pos_data.get("liqPrice", "0")),
                            "unrealised_pnl": Decimal(pos_data.get("unrealisedPnl", "0"))
                        }
        return None # Нет открытой позиции или ошибка
    except Exception as e:
        print(f"Ошибка при получении информации о позиции для {symbol}: {e}")
        return None

# ===================== КОНЕЦ НОВЫХ ВСПОМОГАТЕЛЬНЫХ ФУНКЦИЙ =====================

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

                                                # --- НОВАЯ ЛОГИКА ОТКРЫТИЯ ПОЗИЦИИ ---
                        print(f"Attempting to open position: {open_side} {adjusted_qty} {top_symbol}")
                        
                        # Сброс данных о текущей попытке входа в position_data
                        position_data["opened_qty"] = Decimal("0")
                        position_data["total_open_value"] = Decimal("0")
                        position_data["total_open_fee"] = Decimal("0")
                        
                        # 1. Попытка входа лимитным ордером (Maker)
                        maker_price = Decimal("0")
                        try:
                            ob_resp = session.get_orderbook(category="linear", symbol=top_symbol, limit=1)
                            ob = ob_resp['result']
                            # Для покупки (LONG) берем лучшую цену продажи (ask), для продажи (SHORT) - лучшую цену покупки (bid)
                            # Чтобы наш PostOnly ордер встал в стакан и не исполнился сразу как Taker.
                            # НО, так как мы хотим быть Maker, мы должны ставить цену чуть хуже для нас.
                            # Если Long (Buy), то цена должна быть на bid[0] или чуть ниже.
                            # Если Short (Sell), то цена должна быть на ask[0] или чуть выше.
                            # Однако, для PostOnly, если цена пересекает спред, он отменяется.
                            # Поэтому ставим точно на лучшую цену в "нашу" сторону стакана.
                            if open_side == "Buy": # LONG
                                maker_price = quantize_price(Decimal(ob['b'][0][0]), tick_size) # Лучший бид
                            else: # SHORT
                                maker_price = quantize_price(Decimal(ob['a'][0][0]), tick_size) # Лучший аск
                        except Exception as e:
                            await app.bot.send_message(chat_id, f"⚠️ Не удалось получить ордербук для {top_symbol} для Maker цены: {e}. Пропускаю Maker вход.")
                            maker_price = Decimal("0") # Не будем пытаться мейкером если нет цены

                        limit_order_result = None
                        if maker_price > 0:
                             limit_order_result = await place_limit_order_with_retry(
                                session, app, chat_id, top_symbol, open_side, 
                                adjusted_qty, # Пытаемся весь объем лимиткой
                                maker_price,
                                time_in_force="PostOnly",
                                max_wait_seconds=MAKER_ORDER_WAIT_SECONDS_ENTRY # Ваша константа
                            )

                        if limit_order_result and limit_order_result['executed_qty'] > 0:
                            position_data["opened_qty"] += limit_order_result['executed_qty']
                            # Средняя цена считается как общая стоимость / общее количество
                            position_data["total_open_value"] += limit_order_result['executed_qty'] * limit_order_result['avg_price']
                            position_data["total_open_fee"] += limit_order_result['fee']

                        remaining_qty_to_open = adjusted_qty - position_data["opened_qty"]
                        remaining_qty_to_open = quantize_qty(remaining_qty_to_open, qty_step) # Округляем до шага лота

                        # 2. "Добивание" рыночным ордером, если не весь объем вошел лимиткой
                        if remaining_qty_to_open >= min_qty:
                            await app.bot.send_message(chat_id, f"🛒 Добиваю маркетом остаток: {remaining_qty_to_open} {top_symbol}")
                            market_order_result = await place_market_order_robust(
                                session, app, chat_id, top_symbol, open_side, 
                                remaining_qty_to_open,
                                time_in_force="ImmediateOrCancel" # Важно для "добивания"
                            )
                            if market_order_result and market_order_result['executed_qty'] > 0:
                                position_data["opened_qty"] += market_order_result['executed_qty']
                                position_data["total_open_value"] += market_order_result['executed_qty'] * market_order_result['avg_price']
                                position_data["total_open_fee"] += market_order_result['fee']
                            elif market_order_result and market_order_result['status'] == 'ErrorPlacingMarket' and "not enough" in market_order_result['message'].lower():
                                await app.bot.send_message(chat_id, f"⚠️ Не хватило средств для добивания маркетом. Возможно, первая часть сделки уже использовала баланс. Проверяю позицию...")


                        # 3. ФИНАЛЬНАЯ ПРОВЕРКА И СИНХРОНИЗАЦИЯ ПОЗИЦИИ С БИРЖЕЙ
                        await app.bot.send_message(chat_id, f"🔍 Финальная проверка открытой позиции для {top_symbol}...")
                        actual_position_on_exchange = await get_current_position_info(session, top_symbol)

                        final_opened_qty_on_bot = position_data["opened_qty"]
                        
                        if actual_position_on_exchange:
                            actual_size = actual_position_on_exchange['size']
                            actual_side = actual_position_on_exchange['side'] # 'Buy' или 'Sell'
                            actual_avg_price = actual_position_on_exchange['avg_price']
                            
                            await app.bot.send_message(chat_id, f"   Биржа: {actual_side} {actual_size} @ {actual_avg_price}. Бот думает: {open_side} {final_opened_qty_on_bot}.")

                            if actual_side == open_side and actual_size > 0:
                                # Позиция на бирже соответствует ожиданиям по направлению
                                if abs(actual_size - final_opened_qty_on_bot) > qty_step: # Если расхождение больше шага лота
                                    await app.bot.send_message(chat_id, f"⚠️ Расхождение! Бот насчитал {final_opened_qty_on_bot}, на бирже {actual_size}. Синхронизируюсь с биржей.")
                                # Синхронизируем данные бота с биржей в любом случае, если позиция есть
                                position_data["opened_qty"] = actual_size
                                # Пересчитать total_open_value и total_open_fee сложнее без истории сделок,
                                # пока что будем доверять средней цене с биржи. Для PNL это будет точнее.
                                position_data["total_open_value"] = actual_size * actual_avg_price
                                # Комиссию за открытие по факту сложно вытащить без перебора истории сделок,
                                # будем использовать то, что насчитали по ордерам, если только не было полного расхождения.
                                # Если же бот думал 0, а на бирже есть, то комиссию пока не знаем.
                                if final_opened_qty_on_bot == Decimal("0") and actual_size > 0:
                                     position_data["total_open_fee"] = Decimal("0") # Не можем точно знать, ставим 0
                                     await app.bot.send_message(chat_id, "   (Комиссия открытия неизвестна из-за расхождения, принята за 0)")

                                final_opened_qty = actual_size # Это теперь наш "официальный" открытый объем
                            elif actual_side != "None" and actual_side != open_side:
                                await app.bot.send_message(chat_id, f"❌ КРИТИЧЕСКАЯ ОШИБКА: На бирже позиция в ПРОТИВОПОЛОЖНУЮ сторону ({actual_side} {actual_size})! Пропускаю сделку.")
                                continue # Переходим к следующей итерации цикла по чатам/парам
                            else: # actual_side == "None" or actual_size == 0
                                await app.bot.send_message(chat_id, f"   По данным биржи, позиция {open_side} НЕ открыта.")
                                position_data["opened_qty"] = Decimal("0")
                                final_opened_qty = Decimal("0")
                        else:
                            # get_current_position_info вернул None (нет позиции или ошибка API)
                            await app.bot.send_message(chat_id, f"   Не удалось получить данные о позиции с биржи или позиция отсутствует.")
                            if final_opened_qty_on_bot > 0:
                                await app.bot.send_message(chat_id, f"   Бот думал, что открыл {final_opened_qty_on_bot}, но на бирже пусто. Считаем, что не открыто.")
                            position_data["opened_qty"] = Decimal("0") # Синхронизируем - позиции нет
                            final_opened_qty = Decimal("0")

                        # Проверка минимального объема после всех синхронизаций
                        if final_opened_qty < min_qty:
                            await app.bot.send_message(chat_id, f"❌ Не открыт мин. объем ({min_qty}). Открыто по факту: {final_opened_qty}. Отмена сделки.")
                            # Тут можно добавить логику принудительного закрытия этого "мусора", если он есть.
                            # Но для начала просто отменяем сделку в боте.
                            if final_opened_qty > Decimal("0"):
                                 await app.bot.send_message(chat_id, f"❗️ ВНИМАНИЕ: На бирже осталась небольшая позиция {final_opened_qty} {top_symbol}. Закройте вручную, если не нужна.")
                            continue # Переходим к следующей итерации цикла по чатам/парам

                        # Если дошли сюда, значит, позиция успешно открыта (или синхронизирована) и удовлетворяет мин. объему
                        avg_open_price_display = (position_data['total_open_value'] / final_opened_qty) if final_opened_qty > 0 else Decimal("0")
                        
                        # Используем actual_avg_price из позиции, если она есть, для большей точности отображения
                        if actual_position_on_exchange and actual_position_on_exchange['avg_price'] > 0:
                             avg_open_price_display = actual_position_on_exchange['avg_price']

                        await app.bot.send_message(
                            chat_id,
                            f"✅ Позиция *{top_symbol}* ({'LONG' if open_side == 'Buy' else 'SHORT'}) открыта/подтверждена.\n"
                            f"Объем: `{final_opened_qty}`\n"
                            f"Ср.цена входа (биржа): `{avg_open_price_display:.{price_filter['tickSize'].split('.')[1].__len__()}f}`\n" # Форматируем по tickSize
                            f"Комиссия откр. (бот): `{position_data['total_open_fee']:.4f}` USDT",
                            parse_mode='Markdown'
                        )
                        data["last_entry_symbol"] = top_symbol
                        data["last_entry_ts"] = next_funding_ts
                        # ВАЖНО: Сохраняем `position_data` в `data` для этого чата, чтобы использовать при закрытии
                        data['active_trade_data'] = position_data 
                        # --- КОНЕЦ НОВОЙ ЛОГИКИ ОТКРЫТИЯ ---
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

                                                # --- НОВАЯ ЛОГИКА ЗАКРЫТИЯ ПОЗИЦИИ ---
                        active_trade = data.get('active_trade_data')
                        # Убедимся, что есть что закрывать и есть нужные ключи
                        if not active_trade or \
                           active_trade.get('opened_qty', Decimal("0")) < min_qty or \
                           'open_side' not in active_trade:
                            await app.bot.send_message(chat_id, f"⚠️ Нет активной подтвержденной сделки для {top_symbol} для закрытия, или объем/данные некорректны.")
                            # Очищаем на всякий случай, если данные неполные
                            if 'active_trade_data' in data: del data['active_trade_data']
                            continue # Пропускаем закрытие для этого чата

                        qty_to_close = active_trade['opened_qty'] # Объем, который нужно закрыть
                        original_open_side = active_trade['open_side']
                        close_side = "Buy" if original_open_side == "Sell" else "Sell"

                        # Сбрасываем/инициализируем данные о закрытии в active_trade перед попытками
                        active_trade["closed_qty"] = Decimal("0")
                        active_trade["total_close_value"] = Decimal("0")
                        active_trade["total_close_fee"] = Decimal("0")

                        await app.bot.send_message(chat_id, f"🎬 Начинаю закрытие позиции {original_open_side} {qty_to_close} {top_symbol}")

                        # 1. Попытка закрытия лимитным ордером (Maker)
                        maker_close_price = Decimal("0")
                        try:
                            ob_resp = session.get_orderbook(category="linear", symbol=top_symbol, limit=1)
                            ob = ob_resp['result']
                            # Для закрытия LONG (Sell) - ставим на лучший Ask
                            # Для закрытия SHORT (Buy) - ставим на лучший Bid
                            if close_side == "Buy": 
                                maker_close_price = quantize_price(Decimal(ob['b'][0][0]), tick_size)
                            else: 
                                maker_close_price = quantize_price(Decimal(ob['a'][0][0]), tick_size)
                        except Exception as e:
                            await app.bot.send_message(chat_id, f"⚠️ Не удалось получить ордербук для {top_symbol} для Maker цены (закрытие): {e}. Пропускаю Maker выход.")
                            maker_close_price = Decimal("0") # Не будем пытаться мейкером если нет цены

                        limit_close_order_result = None
                        if maker_close_price > 0 and qty_to_close >= min_qty : # Убедимся, что есть что закрывать и есть цена
                            limit_close_order_result = await place_limit_order_with_retry(
                                session, app, chat_id, top_symbol, close_side,
                                qty_to_close, # Пытаемся весь объем лимиткой
                                maker_close_price,
                                time_in_force="PostOnly", 
                                reduce_only=True, # Обязательно для закрытия
                                max_wait_seconds=MAKER_ORDER_WAIT_SECONDS_EXIT # Ваша константа
                            )
                        
                        if limit_close_order_result and limit_close_order_result.get('executed_qty', Decimal("0")) > 0:
                            active_trade["closed_qty"] += limit_close_order_result['executed_qty']
                            active_trade["total_close_value"] += limit_close_order_result['executed_qty'] * limit_close_order_result['avg_price']
                            active_trade["total_close_fee"] += limit_close_order_result['fee']

                        remaining_qty_to_close = qty_to_close - active_trade["closed_qty"]
                        remaining_qty_to_close = quantize_qty(remaining_qty_to_close, qty_step) # Округляем до шага лота

                        # 2. "Добивание" закрытия рыночным ордером
                        if remaining_qty_to_close >= min_qty:
                            await app.bot.send_message(chat_id, f"🛒 Закрываю маркетом остаток: {remaining_qty_to_close} {top_symbol}")
                            market_close_order_result = await place_market_order_robust(
                                session, app, chat_id, top_symbol, close_side,
                                remaining_qty_to_close,
                                time_in_force="ImmediateOrCancel", # Важно для "добивания"
                                reduce_only=True # Обязательно для закрытия
                            )
                            if market_close_order_result and market_close_order_result.get('executed_qty', Decimal("0")) > 0:
                                active_trade["closed_qty"] += market_close_order_result['executed_qty']
                                active_trade["total_close_value"] += market_close_order_result['executed_qty'] * market_close_order_result['avg_price']
                                active_trade["total_close_fee"] += market_close_order_result['fee']
                        
                        final_closed_qty_bot = active_trade["closed_qty"]

                        # ФИНАЛЬНАЯ ПРОВЕРКА ПОЗИЦИИ ПОСЛЕ ЗАКРЫТИЯ
                        await asyncio.sleep(1.5) # Небольшая пауза перед финальной проверкой
                        final_position_after_close = await get_current_position_info(session, top_symbol)
                        
                        actual_qty_left_on_exchange = Decimal("0")
                        if final_position_after_close:
                            actual_qty_left_on_exchange = final_position_after_close.get('size', Decimal("0"))
                            pos_side_after_close = final_position_after_close.get('side', "None")
                            await app.bot.send_message(chat_id, f"   Биржа после закрытия: осталось {actual_qty_left_on_exchange} {top_symbol} (Сторона: {pos_side_after_close})")
                        else:
                            await app.bot.send_message(chat_id, f"   Биржа после закрытия: позиция по {top_symbol} отсутствует или не удалось получить инфо.")

                        # Сравниваем то, что бот ПЫТАЛСЯ закрыть (qty_to_close), с тем, что ОСТАЛОСЬ на бирже
                        if actual_qty_left_on_exchange >= min_qty: # Если остался значимый "хвост"
                             await app.bot.send_message(chat_id, f"⚠️ Позиция *{top_symbol}* НЕ ПОЛНОСТЬЮ ЗАКРЫТА! Остаток на бирже: `{actual_qty_left_on_exchange}`. Бот обработал для закрытия: `{final_closed_qty_bot}`. ПРОВЕРЬТЕ ВРУЧНУЮ!", parse_mode='Markdown')
                        elif final_closed_qty_bot >= qty_to_close - qty_step: # Если бот считает, что закрыл почти все что должен был
                             await app.bot.send_message(chat_id, f"✅ Позиция *{top_symbol}* успешно закрыта (бот обработал: {final_closed_qty_bot}, остаток на бирже: {actual_qty_left_on_exchange}).", parse_mode='Markdown')
                        else: # Бот не смог закрыть то, что должен был, но на бирже почти пусто
                             await app.bot.send_message(chat_id, f"⚠️ Похоже, позиция *{top_symbol}* закрыта или почти закрыта, но бот не смог подтвердить весь объем закрытия (бот: {final_closed_qty_bot}, на бирже остаток: {actual_qty_left_on_exchange}). Проверьте для уверенности.", parse_mode='Markdown')


                        # --- РАСЧЕТ PNL ---
                        # Убедимся, что все нужные ключи есть в active_trade
                        total_open_val = active_trade.get("total_open_value", Decimal("0"))
                        total_close_val = active_trade.get("total_close_value", Decimal("0"))
                        open_s = active_trade.get("open_side", "Buy") # По умолчанию Buy, если что-то пошло не так
                        
                        price_pnl = total_close_val - total_open_val
                        if open_s == "Sell": # Если был шорт
                            price_pnl = -price_pnl
                        
                        funding_pnl = active_trade.get("actual_funding_fee", Decimal("0"))
                        total_open_f = active_trade.get("total_open_fee", Decimal("0"))
                        total_close_f = active_trade.get("total_close_fee", Decimal("0"))
                        total_fees = total_open_f + total_close_f
                        
                        net_pnl = price_pnl + funding_pnl - total_fees
                        
                        marja_for_pnl = data.get('real_marja', Decimal("1")) 
                        if not isinstance(marja_for_pnl, Decimal) or marja_for_pnl <= Decimal("0"): 
                            marja_for_pnl = Decimal("1") # Защита от некорректной маржи

                        roi_pct = (net_pnl / marja_for_pnl) * 100
                        
                        opened_qty_display = active_trade.get('opened_qty', 'N/A')
                        closed_qty_display = active_trade.get('closed_qty', 'N/A')

                        await app.bot.send_message(
                            chat_id, 
                            f"📊 Результат сделки: *{top_symbol}* ({'LONG' if open_s=='Buy' else 'SHORT'})\n\n"
                            f" Открыто: `{opened_qty_display}` Закрыто: `{closed_qty_display}`\n"
                            f" PNL (цена): `{price_pnl:+.4f}` USDT\n"
                            f" PNL (фандинг): `{funding_pnl:+.4f}` USDT\n"
                            f" Комиссии (откр+закр): `{-total_fees:.4f}` USDT\n"
                            f"💰 *Чистая прибыль: {net_pnl:+.4f} USDT*\n"
                            f"📈 ROI от маржи ({marja_for_pnl} USDT): `{roi_pct:.2f}%`", 
                            parse_mode='Markdown'
                        )
                        
                        # Очищаем данные об активной сделке после ее завершения
                        if 'active_trade_data' in data:
                            del data['active_trade_data']
                        
                        trade_success = True # Это было в вашем коде, оставляю
                        # --- КОНЕЦ НОВОЙ ЛОГИКИ ЗАКРЫТИЯ И PNL ---

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
    app.add_handler(CallbackQueryHandler(signal_callback, pattern="^(toggle_sniper|show_top_pairs_inline|set_max_trades_)"))

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
