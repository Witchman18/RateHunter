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
ENTRY_WINDOW_START_SECONDS = 35 # За сколько секунд ДО фандинга начинаем пытаться войти
ENTRY_WINDOW_END_SECONDS = 7  # За сколько секунд ДО фандинга прекращаем попытки входа
# === ИЗМЕНЕНО ЗДЕСЬ ===
POST_FUNDING_WAIT_SECONDS = 7 # Сколько секунд ждем ПОСЛЕ времени фандинга перед выходом
# =======================
MAKER_ORDER_WAIT_SECONDS_ENTRY = 7 # Сколько секунд ждем исполнения PostOnly ордера на ВХОД
MAKER_ORDER_WAIT_SECONDS_EXIT = 5  # Сколько секунд ждем исполнения PostOnly ордера на ВЫХОД
SNIPER_LOOP_INTERVAL_SECONDS = 5 # Как часто проверяем тикеры в основном цикле
DEFAULT_MAX_CONCURRENT_TRADES = 1 # Одна сделка дефолт
MAX_PAIRS_TO_CONSIDER_PER_CYCLE = 5 # NEW: How many top pairs to check in each sniper loop

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
            now_ts_dt = datetime.utcnow().timestamp() # Renamed to avoid conflict
            for symbol, rate, ts in latest_top_pairs:
                try:
                    delta_sec = int(ts / 1000 - now_ts_dt)
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
                'ongoing_trades': {}, # Ensures 'ongoing_trades' key exists
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
                'ongoing_trades': {}, # Ensures 'ongoing_trades' key exists
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
        text = f"[{i}]" if i == current_max_trades else f"{i}" # Выделяем текущее значение
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
            'ongoing_trades': {}, # Ensures 'ongoing_trades' key exists
        }
    chat_settings = sniper_active[chat_id]
    # --- Конец инициализации ---

    action_text = "" # Текст для подтверждения действия

    if data == "toggle_sniper":
        new_status = not chat_settings.get('active', False)
        chat_settings['active'] = new_status
        action_text = "🚀 Снайпер запущен!" if new_status else "🛑 Снайпер остановлен."
        # Send config message after status change to reflect new status
        await send_final_config_message(chat_id, context)


    elif data.startswith("set_max_trades_"):
        try:
            new_max_trades = int(data.split("_")[-1])
            if 1 <= new_max_trades <= 5:
                chat_settings['max_concurrent_trades'] = new_max_trades
                action_text = f"✅ Лимит сделок изменен на: {new_max_trades}"
                # Send config message after limit change
                await send_final_config_message(chat_id, context)
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
            text = f"[{i}]" if i == current_max_trades else f"{i}"
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
            await app.bot.send_message(chat_id, f"⏳ {('Выход' if reduce_only else 'Вход')} Maker @{price} (ID: ...{order_id[-6:]}) для {symbol}")

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
                        await app.bot.send_message(chat_id, f"✅ Maker ордер ...{order_id[-6:]} ({symbol}) ПОЛНОСТЬЮ исполнен: {cum_exec_qty} {symbol}")
                        return {'status': 'Filled', 'executed_qty': cum_exec_qty, 'avg_price': avg_price, 'fee': cum_exec_fee, 'message': 'Filled'}
                    elif order_status == "PartiallyFilled":
                        # Продолжаем ждать, но запоминаем текущее исполнение
                        print(f"Maker ордер ...{order_id[-6:]} ({symbol}) ЧАСТИЧНО исполнен: {cum_exec_qty}. Ждем дальше.")
                        continue # Не выходим, ждем полного исполнения или таймаута
                    elif order_status in ["Cancelled", "Rejected", "Deactivated", "Expired"]:
                        msg = f"⚠️ Maker ордер ...{order_id[-6:]} ({symbol}) {order_status}. Исполнено: {cum_exec_qty}"
                        await app.bot.send_message(chat_id, msg)
                        return {'status': order_status, 'executed_qty': cum_exec_qty, 'avg_price': avg_price, 'fee': cum_exec_fee, 'message': msg}
                else:
                    print(f"Не удалось получить статус для Maker ордера ...{order_id[-6:]} ({symbol}). Попытка {int(waited_seconds/check_interval_seconds)}.")
            
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
                        print(f"Отменяю Maker ордер ...{order_id[-6:]} ({symbol}) по таймауту.")
                        session.cancel_order(category="linear", symbol=symbol, orderId=order_id)
                        await app.bot.send_message(chat_id, f"⏳ Maker ордер ...{order_id[-6:]} ({symbol}) отменен по таймауту. Исполнено: {cum_exec_qty}")
                        return {'status': 'CancelledByTimeout', 'executed_qty': cum_exec_qty, 'avg_price': avg_price, 'fee': cum_exec_fee, 'message': 'Cancelled by timeout'}
                    except Exception as cancel_e:
                        await app.bot.send_message(chat_id, f"❌ Ошибка отмены Maker ордера ...{order_id[-6:]} ({symbol}): {cancel_e}")
                        return {'status': 'ErrorCancelling', 'executed_qty': cum_exec_qty, 'avg_price': avg_price, 'fee': cum_exec_fee, 'message': str(cancel_e)}
                else: # Если он уже сам отменился/исполнился пока мы ждали
                     return {'status': order_status, 'executed_qty': cum_exec_qty, 'avg_price': avg_price, 'fee': cum_exec_fee, 'message': f'Final status: {order_status}'}
            else:
                 await app.bot.send_message(chat_id, f"⚠️ Не удалось получить финальный статус для Maker ордера ...{order_id[-6:]} ({symbol}) после таймаута.")
                 return {'status': 'ErrorNoStatus', 'executed_qty': Decimal("0"), 'avg_price': Decimal("0"), 'fee': Decimal("0"), 'message': 'Could not get final status'}

        else:
            err_msg = f"Ошибка размещения Maker ордера ({symbol}): {response.get('retMsg', 'Unknown error') if response else 'No response'}"
            print(err_msg)
            await app.bot.send_message(chat_id, f"❌ {err_msg}")
            return {'status': 'ErrorPlacing', 'executed_qty': Decimal("0"), 'avg_price': Decimal("0"), 'fee': Decimal("0"), 'message': err_msg}

    except Exception as e:
        error_text = f"КРИТ.ОШИБКА в place_limit_order_with_retry ({symbol}): {e}"
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
            await app.bot.send_message(chat_id, f"🛒 Маркет ордер ({('выход' if reduce_only else 'вход')}) для {symbol} отправлен (ID: ...{order_id[-6:]}). Проверяю исполнение...")
            
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
                    await app.bot.send_message(chat_id, f"✅ Маркет ордер ...{order_id[-6:]} ({symbol}) ИСПОЛНЕН: {cum_exec_qty} {symbol}")
                    return {'status': 'Filled', 'executed_qty': cum_exec_qty, 'avg_price': avg_price, 'fee': cum_exec_fee, 'message': 'Market Filled'}
                elif order_status == "PartiallyFilled" and time_in_force == "ImmediateOrCancel": # Для IOC это частичное исполнение
                    await app.bot.send_message(chat_id, f"✅ Маркет IOC ордер ...{order_id[-6:]} ({symbol}) ЧАСТИЧНО ИСПОЛНЕН: {cum_exec_qty} {symbol}")
                    return {'status': 'PartiallyFilled', 'executed_qty': cum_exec_qty, 'avg_price': avg_price, 'fee': cum_exec_fee, 'message': 'Market IOC PartiallyFilled'}
                elif cum_exec_qty == Decimal("0") and order_status in ["Cancelled", "Rejected", "Deactivated"]: # IOC не исполнился
                    msg = f"⚠️ Маркет IOC ордер ...{order_id[-6:]} ({symbol}) не исполнил ничего (статус: {order_status})."
                    await app.bot.send_message(chat_id, msg)
                    return {'status': order_status, 'executed_qty': Decimal("0"), 'avg_price': Decimal("0"), 'fee': Decimal("0"), 'message': msg}
                else: # Неожиданный статус для рыночного ордера
                    msg = f"⚠️ Неожиданный статус для Маркет ордера ...{order_id[-6:]} ({symbol}): {order_status}. Исполнено: {cum_exec_qty}"
                    await app.bot.send_message(chat_id, msg)
                    return {'status': order_status, 'executed_qty': cum_exec_qty, 'avg_price': avg_price, 'fee': cum_exec_fee, 'message': msg}
            else:
                msg = f"⚠️ Не удалось получить статус для Маркет ордера ...{order_id[-6:]} ({symbol})."
                await app.bot.send_message(chat_id, msg)
                # В этом случае мы не знаем, что произошло. Лучше считать, что не исполнился.
                return {'status': 'ErrorNoStatusMarket', 'executed_qty': Decimal("0"), 'avg_price': Decimal("0"), 'fee': Decimal("0"), 'message': msg}
        else:
            # Проверяем код ошибки 110007 "ab not enough for new order"
            ret_msg = response.get('retMsg', 'Unknown error') if response else 'No response'
            error_code = response.get('retCode') if response else None
            if error_code == 110007 or "not enough" in ret_msg.lower() or "insufficient" in ret_msg.lower():
                 err_msg = f"❌ Ошибка 'Недостаточно средств' при размещении Маркет ордера ({symbol}): {ret_msg}"
            else:
                 err_msg = f"❌ Ошибка размещения Маркет ордера ({symbol}): {ret_msg}"
            print(err_msg)
            await app.bot.send_message(chat_id, err_msg)
            return {'status': 'ErrorPlacingMarket', 'executed_qty': Decimal("0"), 'avg_price': Decimal("0"), 'fee': Decimal("0"), 'message': err_msg}

    except Exception as e:
        error_text = f"КРИТ.ОШИБКА в place_market_order_robust ({symbol}): {e}"
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

async def funding_sniper_loop(app: ApplicationBuilder): # app is Application, not ApplicationBuilder
    print(" Sniper loop started ".center(50, "="))
    while True:
        await asyncio.sleep(SNIPER_LOOP_INTERVAL_SECONDS)
        try:
            current_time_epoch = time.time() # Renamed from now_ts to avoid conflicts
            
            response = session.get_tickers(category="linear")
            tickers = response.get("result", {}).get("list", [])
            if not tickers:
                # print("No tickers received in sniper loop.")
                continue

            funding_data_raw = []
            for t in tickers:
                symbol_val = t.get("symbol")
                rate_str = t.get("fundingRate")
                next_ts_str = t.get("nextFundingTime")
                turnover_str = t.get("turnover24h")
                
                if not all([symbol_val, rate_str, next_ts_str, turnover_str]):
                    continue
                try:
                    rate_float = float(rate_str)
                    next_ts_epoch = int(next_ts_str) / 1000 # Convert ms to seconds
                    turnover_float = float(turnover_str)
                    
                    if turnover_float < 1_000_000 or abs(rate_float) < 0.0001:
                        continue
                    funding_data_raw.append({
                        "symbol": symbol_val, 
                        "rate": rate_float, 
                        "next_ts": next_ts_epoch
                    })
                except (ValueError, TypeError):
                    # print(f"Error parsing ticker data for {symbol_val if symbol_val else 'Unknown'}")
                    continue
            
            if not funding_data_raw:
                # print("No suitable pairs after filtering in sniper loop.")
                continue
            
            funding_data_raw.sort(key=lambda x: abs(x["rate"]), reverse=True)

            # Iterate through TOP_N_PAIRS_TO_CONSIDER
            for pair_info in funding_data_raw[:MAX_PAIRS_TO_CONSIDER_PER_CYCLE]:
                
                # These are specific to the pair being considered in this iteration
                symbol_to_trade = pair_info["symbol"]
                rate_of_trade = pair_info["rate"]
                funding_timestamp_of_trade = pair_info["next_ts"]
                seconds_left_for_trade = funding_timestamp_of_trade - current_time_epoch

                # Check if this pair is in the entry window
                if not (ENTRY_WINDOW_END_SECONDS <= seconds_left_for_trade <= ENTRY_WINDOW_START_SECONDS):
                    # print(f"Pair {symbol_to_trade} not in entry window ({seconds_left_for_trade:.0f}s left). Will check next pair.")
                    continue # Move to the next pair in funding_data_raw

                open_side_for_trade = get_position_direction(rate_of_trade)
                if open_side_for_trade == "NONE":
                    # print(f"Funding rate for {symbol_to_trade} is zero, skipping.")
                    continue # Move to the next pair

                # Iterate through active chats
                for chat_id, chat_config in list(sniper_active.items()): # Use .items() and list() for safe iteration
                    if not chat_config.get('active'):
                        continue # This chat is not active, try next chat for this pair

                    # Check if chat has capacity for another trade
                    max_allowed_trades = chat_config.get('max_concurrent_trades', DEFAULT_MAX_CONCURRENT_TRADES)
                    ongoing_trades_for_chat = chat_config.get('ongoing_trades', {}) # This should be initialized

                    if len(ongoing_trades_for_chat) >= max_allowed_trades:
                        # print(f"Chat {chat_id} at max trades ({len(ongoing_trades_for_chat)}/{max_allowed_trades}). Cannot take {symbol_to_trade}.")
                        continue # This chat has no capacity, try next chat for this pair

                    # Check if this chat is ALREADY trading THIS specific symbol
                    if symbol_to_trade in ongoing_trades_for_chat:
                        # print(f"Chat {chat_id} is already trading {symbol_to_trade}. Skipping.")
                        continue # This chat is already trading this symbol, try next chat for this pair

                    marja_for_trade, plecho_for_trade = chat_config.get('real_marja'), chat_config.get('real_plecho')
                    if not marja_for_trade or not plecho_for_trade:
                        # Consider sending this message less frequently if it becomes spammy
                        await app.bot.send_message(chat_id, f"⚠️ Пропуск {symbol_to_trade}: Маржа/плечо не установлены.")
                        continue # This chat is not configured, try next chat for this pair
                    
                    # --- If all checks pass, proceed with trade logic for symbol_to_trade and chat_id ---
                    print(f"\n>>> Processing {symbol_to_trade} for chat {chat_id} (Rate: {rate_of_trade*100:.4f}%, Left: {seconds_left_for_trade:.0f}s) <<<")
                    
                    # Prepare position_data for this specific trade attempt
                    # Ensure all trade-specific variables are used (e.g., symbol_to_trade)
                    current_trade_position_data = {
                        "symbol": symbol_to_trade, 
                        "open_side": open_side_for_trade,
                        "marja": marja_for_trade, 
                        "plecho": plecho_for_trade,
                        "funding_rate": Decimal(str(rate_of_trade)),
                        "next_funding_ts": funding_timestamp_of_trade, # Critical for funding check
                        "opened_qty": Decimal("0"), "closed_qty": Decimal("0"),
                        "total_open_value": Decimal("0"), "total_close_value": Decimal("0"),
                        "total_open_fee": Decimal("0"), "total_close_fee": Decimal("0"),
                        "actual_funding_fee": Decimal("0"),
                        "target_qty": Decimal("0"),
                    }
                    
                    # Add to ongoing_trades for this chat *before* starting async operations for this trade
                    # This marks the "slot" as taken for this symbol in this chat
                    chat_config.setdefault('ongoing_trades', {})[symbol_to_trade] = current_trade_position_data
                    
                    # Send initial message about entering trade window
                    await app.bot.send_message(
                        chat_id,
                        f"🎯 Вхожу в окно сделки: *{symbol_to_trade}*\n"
                        f"Направление: {'📈 LONG' if open_side_for_trade == 'Buy' else '📉 SHORT'}\n"
                        f"Фандинг: `{rate_of_trade * 100:.4f}%`\n"
                        f"Осталось: `{seconds_left_for_trade:.0f} сек`",
                         parse_mode='Markdown'
                    )

                    try:
                        # --- Получение инфо и расчет кол-ва ---
                        print(f"Getting instrument info for {symbol_to_trade}...")
                        info_resp = session.get_instruments_info(category="linear", symbol=symbol_to_trade)
                        instrument_info = info_resp.get("result", {}).get("list", [])[0]
                        lot_filter = instrument_info["lotSizeFilter"]
                        price_filter = instrument_info["priceFilter"]
                        min_qty = Decimal(lot_filter["minOrderQty"])
                        qty_step = Decimal(lot_filter["qtyStep"])
                        tick_size = Decimal(price_filter["tickSize"])
                        
                        print(f"Getting ticker info for {symbol_to_trade}...")
                        ticker_resp = session.get_tickers(category="linear", symbol=symbol_to_trade)
                        last_price = Decimal(ticker_resp["result"]["list"][0]["lastPrice"])
                        
                        position_size_usdt = marja_for_trade * plecho_for_trade
                        if last_price <= 0: raise ValueError(f"Invalid last price for {symbol_to_trade}")
                        raw_qty = position_size_usdt / last_price
                        adjusted_qty = quantize_qty(raw_qty, qty_step)

                        if adjusted_qty < min_qty:
                            await app.bot.send_message(chat_id, f"⚠️ Расчетный объем {adjusted_qty} {symbol_to_trade} < мин. ({min_qty}). Отмена для {symbol_to_trade}.")
                            # No 'continue' here, 'finally' will clean up ongoing_trades
                            raise ValueError(f"Calculated qty too small for {symbol_to_trade}") # Raise to go to finally

                        current_trade_position_data["target_qty"] = adjusted_qty

                        # --- Установка плеча ---
                        print(f"Setting leverage {plecho_for_trade}x for {symbol_to_trade}...")
                        try:
                            session.set_leverage(category="linear", symbol=symbol_to_trade, buyLeverage=str(plecho_for_trade), sellLeverage=str(plecho_for_trade))
                        except Exception as e:
                            if "110043" not in str(e): # 110043: leverage not modified
                                raise ValueError(f"Не удалось установить плечо для {symbol_to_trade}: {e}")
                            else:
                                print(f"Плечо {plecho_for_trade}x уже установлено для {symbol_to_trade}.")
                        
                        # --- НОВАЯ ЛОГИКА ОТКРЫТИЯ ПОЗИЦИИ ---
                        print(f"Attempting to open position: {open_side_for_trade} {adjusted_qty} {symbol_to_trade}")
                        
                        current_trade_position_data["opened_qty"] = Decimal("0")
                        current_trade_position_data["total_open_value"] = Decimal("0")
                        current_trade_position_data["total_open_fee"] = Decimal("0")
                        
                        maker_price = Decimal("0")
                        try:
                            ob_resp = session.get_orderbook(category="linear", symbol=symbol_to_trade, limit=1)
                            ob = ob_resp['result']
                            if open_side_for_trade == "Buy":
                                maker_price = quantize_price(Decimal(ob['b'][0][0]), tick_size)
                            else:
                                maker_price = quantize_price(Decimal(ob['a'][0][0]), tick_size)
                        except Exception as e:
                            await app.bot.send_message(chat_id, f"⚠️ Не удалось получить ордербук для {symbol_to_trade} для Maker цены: {e}. Пропускаю Maker вход.")
                            maker_price = Decimal("0")

                        limit_order_result = None
                        if maker_price > 0:
                             limit_order_result = await place_limit_order_with_retry(
                                session, app, chat_id, symbol_to_trade, open_side_for_trade, 
                                adjusted_qty, maker_price,
                                time_in_force="PostOnly",
                                max_wait_seconds=MAKER_ORDER_WAIT_SECONDS_ENTRY
                            )

                        if limit_order_result and limit_order_result['executed_qty'] > 0:
                            current_trade_position_data["opened_qty"] += limit_order_result['executed_qty']
                            current_trade_position_data["total_open_value"] += limit_order_result['executed_qty'] * limit_order_result['avg_price']
                            current_trade_position_data["total_open_fee"] += limit_order_result['fee']

                        remaining_qty_to_open = adjusted_qty - current_trade_position_data["opened_qty"]
                        remaining_qty_to_open = quantize_qty(remaining_qty_to_open, qty_step)

                        if remaining_qty_to_open >= min_qty:
                            await app.bot.send_message(chat_id, f"🛒 Добиваю маркетом остаток для {symbol_to_trade}: {remaining_qty_to_open} {symbol_to_trade}")
                            market_order_result = await place_market_order_robust(
                                session, app, chat_id, symbol_to_trade, open_side_for_trade, 
                                remaining_qty_to_open,
                                time_in_force="ImmediateOrCancel"
                            )
                            if market_order_result and market_order_result['executed_qty'] > 0:
                                current_trade_position_data["opened_qty"] += market_order_result['executed_qty']
                                current_trade_position_data["total_open_value"] += market_order_result['executed_qty'] * market_order_result['avg_price']
                                current_trade_position_data["total_open_fee"] += market_order_result['fee']
                            elif market_order_result and market_order_result['status'] == 'ErrorPlacingMarket' and "not enough" in market_order_result['message'].lower():
                                await app.bot.send_message(chat_id, f"⚠️ Не хватило средств для добивания маркетом {symbol_to_trade}. Проверяю позицию...")

                        await app.bot.send_message(chat_id, f"🔍 Финальная проверка открытой позиции для {symbol_to_trade}...")
                        actual_position_on_exchange = await get_current_position_info(session, symbol_to_trade)
                        final_opened_qty_on_bot = current_trade_position_data["opened_qty"]
                        
                        final_opened_qty_actual = Decimal("0") # Will hold the confirmed qty

                        if actual_position_on_exchange:
                            actual_size = actual_position_on_exchange['size']
                            actual_side = actual_position_on_exchange['side']
                            actual_avg_price = actual_position_on_exchange['avg_price']
                            
                            await app.bot.send_message(chat_id, f"   {symbol_to_trade} Биржа: {actual_side} {actual_size} @ {actual_avg_price}. Бот думает: {open_side_for_trade} {final_opened_qty_on_bot}.")

                            if actual_side == open_side_for_trade and actual_size > 0:
                                if abs(actual_size - final_opened_qty_on_bot) > qty_step:
                                    await app.bot.send_message(chat_id, f"⚠️ {symbol_to_trade} Расхождение! Бот: {final_opened_qty_on_bot}, Биржа: {actual_size}. Синхронизируюсь.")
                                current_trade_position_data["opened_qty"] = actual_size
                                current_trade_position_data["total_open_value"] = actual_size * actual_avg_price
                                if final_opened_qty_on_bot == Decimal("0") and actual_size > 0:
                                     current_trade_position_data["total_open_fee"] = Decimal("0")
                                     await app.bot.send_message(chat_id, f"   ({symbol_to_trade} Комиссия открытия неизвестна, принята за 0)")
                                final_opened_qty_actual = actual_size
                            elif actual_side != "None" and actual_side != open_side_for_trade:
                                await app.bot.send_message(chat_id, f"❌ КРИТ. ОШИБКА: {symbol_to_trade} На бирже позиция ПРОТИВОПОЛОЖНАЯ ({actual_side} {actual_size})! Пропускаю.")
                                raise ValueError(f"Opposite position exists for {symbol_to_trade}") # Go to finally
                            else:
                                await app.bot.send_message(chat_id, f"   {symbol_to_trade} По данным биржи, позиция {open_side_for_trade} НЕ открыта.")
                                current_trade_position_data["opened_qty"] = Decimal("0")
                                final_opened_qty_actual = Decimal("0")
                        else:
                            await app.bot.send_message(chat_id, f"   {symbol_to_trade} Нет данных о позиции с биржи или позиция отсутствует.")
                            if final_opened_qty_on_bot > 0:
                                await app.bot.send_message(chat_id, f"   {symbol_to_trade} Бот думал открыл {final_opened_qty_on_bot}, но на бирже пусто.")
                            current_trade_position_data["opened_qty"] = Decimal("0")
                            final_opened_qty_actual = Decimal("0")

                        if final_opened_qty_actual < min_qty:
                            await app.bot.send_message(chat_id, f"❌ {symbol_to_trade} Не открыт мин. объем ({min_qty}). Факт: {final_opened_qty_actual}. Отмена.")
                            if final_opened_qty_actual > Decimal("0"):
                                 await app.bot.send_message(chat_id, f"❗️ ВНИМАНИЕ: {symbol_to_trade} На бирже осталась позиция {final_opened_qty_actual}. Закройте вручную.")
                            raise ValueError(f"Min qty not met for {symbol_to_trade}") # Go to finally

                        avg_open_price_display = (current_trade_position_data['total_open_value'] / final_opened_qty_actual) if final_opened_qty_actual > 0 else Decimal("0")
                        if actual_position_on_exchange and actual_position_on_exchange['avg_price'] > 0:
                             avg_open_price_display = actual_position_on_exchange['avg_price']

                        await app.bot.send_message(
                            chat_id,
                            f"✅ Позиция *{symbol_to_trade}* ({'LONG' if open_side_for_trade == 'Buy' else 'SHORT'}) открыта/подтверждена.\n"
                            f"Объем: `{final_opened_qty_actual}`\n"
                            f"Ср.цена входа (биржа): `{avg_open_price_display:.{price_filter['tickSize'].split('.')[1].__len__()}f}`\n"
                            f"Комиссия откр. (бот): `{current_trade_position_data['total_open_fee']:.4f}` USDT",
                            parse_mode='Markdown'
                        )
                        
                        # --- ОЖИДАНИЕ И ПРОВЕРКА ФАНДИНГА ---
                        wait_duration = max(0, funding_timestamp_of_trade - time.time()) + POST_FUNDING_WAIT_SECONDS
                        await app.bot.send_message(chat_id, f"⏳ {symbol_to_trade} Ожидаю выплаты фандинга (~{wait_duration:.0f} сек)...")
                        await asyncio.sleep(wait_duration)

                        print(f"Checking actual funding payment for {symbol_to_trade} using Transaction Log...")
                        try:
                            start_ts_ms = int((funding_timestamp_of_trade - 120) * 1000) 
                            end_ts_ms = int((funding_timestamp_of_trade + 120) * 1000)   
                            
                            transaction_log_resp = session.get_transaction_log(
                                category="linear", 
                                symbol=symbol_to_trade, 
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
                                    # Check if settlement is close to the expected funding time
                                    if abs(exec_time_ms / 1000 - funding_timestamp_of_trade) < 60: 
                                        found_funding_in_log += Decimal(change_str)
                                        print(f"Found Funding Log ({symbol_to_trade}): Time {datetime.fromtimestamp(exec_time_ms/1000)}, Change: {change_str}")
                                
                                if found_funding_in_log != Decimal("0"):
                                    current_trade_position_data["actual_funding_fee"] = found_funding_in_log
                                    await app.bot.send_message(chat_id, f"💰 {symbol_to_trade} Фандинг (из лога): `{found_funding_in_log:.4f}` USDT", parse_mode='Markdown')
                                else:
                                    await app.bot.send_message(chat_id, f"⚠️ Не найдено SETTLEMENT для {symbol_to_trade} в логе в ожидаемое время.")
                            else:
                                await app.bot.send_message(chat_id, f"⚠️ Лог транзакций пуст для {symbol_to_trade} в указ. период.")
                        
                        except Exception as e_log:
                            print(f"Error checking transaction log for {symbol_to_trade}: {e_log}"); import traceback; traceback.print_exc()
                            await app.bot.send_message(chat_id, f"❌ Ошибка при проверке лога транзакций для {symbol_to_trade}: {e_log}")

                        # --- НОВАЯ ЛОГИКА ЗАКРЫТИЯ ПОЗИЦИИ ---
                        # active_trade is current_trade_position_data
                        if current_trade_position_data.get('opened_qty', Decimal("0")) < min_qty:
                            await app.bot.send_message(chat_id, f"⚠️ Нет активной сделки для {symbol_to_trade} для закрытия (объем {current_trade_position_data.get('opened_qty', Decimal('0'))} < {min_qty}).")
                            raise ValueError(f"Not enough qty to close for {symbol_to_trade}") # Go to finally

                        qty_to_close = current_trade_position_data['opened_qty']
                        original_open_side_for_closing = current_trade_position_data['open_side']
                        close_side_for_trade = "Buy" if original_open_side_for_closing == "Sell" else "Sell"

                        current_trade_position_data["closed_qty"] = Decimal("0")
                        current_trade_position_data["total_close_value"] = Decimal("0")
                        current_trade_position_data["total_close_fee"] = Decimal("0")

                        await app.bot.send_message(chat_id, f"🎬 Начинаю закрытие {symbol_to_trade}: {original_open_side_for_closing} {qty_to_close}")

                        maker_close_price = Decimal("0")
                        try:
                            ob_resp_close = session.get_orderbook(category="linear", symbol=symbol_to_trade, limit=1)
                            ob_close = ob_resp_close['result']
                            if close_side_for_trade == "Buy": 
                                maker_close_price = quantize_price(Decimal(ob_close['b'][0][0]), tick_size)
                            else: 
                                maker_close_price = quantize_price(Decimal(ob_close['a'][0][0]), tick_size)
                        except Exception as e_ob_close:
                            await app.bot.send_message(chat_id, f"⚠️ Не удалось получить ордербук для {symbol_to_trade} (закрытие): {e_ob_close}. Пропускаю Maker выход.")
                            maker_close_price = Decimal("0")

                        if maker_close_price > 0 and qty_to_close >= min_qty:
                            limit_close_order_result = await place_limit_order_with_retry(
                                session, app, chat_id, symbol_to_trade, close_side_for_trade,
                                qty_to_close, maker_close_price,
                                time_in_force="PostOnly", reduce_only=True,
                                max_wait_seconds=MAKER_ORDER_WAIT_SECONDS_EXIT
                            )
                            if limit_close_order_result and limit_close_order_result.get('executed_qty', Decimal("0")) > 0:
                                current_trade_position_data["closed_qty"] += limit_close_order_result['executed_qty']
                                current_trade_position_data["total_close_value"] += limit_close_order_result['executed_qty'] * limit_close_order_result['avg_price']
                                current_trade_position_data["total_close_fee"] += limit_close_order_result['fee']
                        
                        remaining_qty_to_close = qty_to_close - current_trade_position_data["closed_qty"]
                        remaining_qty_to_close = quantize_qty(remaining_qty_to_close, qty_step)

                        if remaining_qty_to_close >= min_qty:
                            await app.bot.send_message(chat_id, f"🛒 Закрываю маркетом остаток для {symbol_to_trade}: {remaining_qty_to_close}")
                            market_close_order_result = await place_market_order_robust(
                                session, app, chat_id, symbol_to_trade, close_side_for_trade,
                                remaining_qty_to_close,
                                time_in_force="ImmediateOrCancel", reduce_only=True
                            )
                            if market_close_order_result and market_close_order_result.get('executed_qty', Decimal("0")) > 0:
                                current_trade_position_data["closed_qty"] += market_close_order_result['executed_qty']
                                current_trade_position_data["total_close_value"] += market_close_order_result['executed_qty'] * market_close_order_result['avg_price']
                                current_trade_position_data["total_close_fee"] += market_close_order_result['fee']
                        
                        final_closed_qty_bot = current_trade_position_data["closed_qty"]
                        await asyncio.sleep(1.5)
                        final_position_after_close = await get_current_position_info(session, symbol_to_trade)
                        
                        actual_qty_left_on_exchange = Decimal("0")
                        if final_position_after_close:
                            actual_qty_left_on_exchange = final_position_after_close.get('size', Decimal("0"))
                            pos_side_after_close = final_position_after_close.get('side', "None")
                            await app.bot.send_message(chat_id, f"   {symbol_to_trade} Биржа после закрытия: осталось {actual_qty_left_on_exchange} (Сторона: {pos_side_after_close})")
                        else:
                            await app.bot.send_message(chat_id, f"   {symbol_to_trade} Биржа после закрытия: позиция отсутствует или не удалось получить инфо.")

                        if actual_qty_left_on_exchange >= min_qty:
                             await app.bot.send_message(chat_id, f"⚠️ Позиция *{symbol_to_trade}* НЕ ПОЛНОСТЬЮ ЗАКРЫТА! Остаток: `{actual_qty_left_on_exchange}`. Бот: `{final_closed_qty_bot}`. ПРОВЕРЬТЕ ВРУЧНУЮ!", parse_mode='Markdown')
                        elif final_closed_qty_bot >= qty_to_close - qty_step:
                             await app.bot.send_message(chat_id, f"✅ Позиция *{symbol_to_trade}* успешно закрыта (бот: {final_closed_qty_bot}, остаток: {actual_qty_left_on_exchange}).", parse_mode='Markdown')
                        else:
                             await app.bot.send_message(chat_id, f"⚠️ Похоже, позиция *{symbol_to_trade}* закрыта/почти, но бот не подтвердил весь объем (бот: {final_closed_qty_bot}, остаток: {actual_qty_left_on_exchange}). Проверьте.", parse_mode='Markdown')

                        # --- РАСЧЕТ PNL ---
                        total_open_val = current_trade_position_data.get("total_open_value", Decimal("0"))
                        total_close_val = current_trade_position_data.get("total_close_value", Decimal("0"))
                        open_s_pnl = current_trade_position_data.get("open_side", "Buy")
                        
                        price_pnl = total_close_val - total_open_val
                        if open_s_pnl == "Sell": price_pnl = -price_pnl
                        
                        funding_pnl = current_trade_position_data.get("actual_funding_fee", Decimal("0"))
                        total_open_f = current_trade_position_data.get("total_open_fee", Decimal("0"))
                        total_close_f = current_trade_position_data.get("total_close_fee", Decimal("0"))
                        total_fees = total_open_f + total_close_f
                        net_pnl = price_pnl + funding_pnl - total_fees
                        
                        marja_for_pnl_calc = chat_config.get('real_marja', Decimal("1")) 
                        if not isinstance(marja_for_pnl_calc, Decimal) or marja_for_pnl_calc <= Decimal("0"): 
                            marja_for_pnl_calc = Decimal("1")

                        roi_pct = (net_pnl / marja_for_pnl_calc) * 100
                        opened_qty_display = current_trade_position_data.get('opened_qty', 'N/A')
                        closed_qty_display = current_trade_position_data.get('closed_qty', 'N/A')

                        await app.bot.send_message(
                            chat_id, 
                            f"📊 Результат сделки: *{symbol_to_trade}* ({'LONG' if open_s_pnl=='Buy' else 'SHORT'})\n\n"
                            f" Открыто: `{opened_qty_display}` Закрыто: `{closed_qty_display}`\n"
                            f" PNL (цена): `{price_pnl:+.4f}` USDT\n"
                            f" PNL (фандинг): `{funding_pnl:+.4f}` USDT\n"
                            f" Комиссии (откр+закр): `{-total_fees:.4f}` USDT\n"
                            f"💰 *Чистая прибыль: {net_pnl:+.4f} USDT*\n"
                            f"📈 ROI от маржи ({marja_for_pnl_calc} USDT): `{roi_pct:.2f}%`", 
                            parse_mode='Markdown'
                        )
                        # Successful completion, no explicit raise, 'finally' will clean up.
                    
                    except Exception as trade_e: # Catches exceptions from the trade logic block
                        print(f"\n!!! TRADE ERROR for chat {chat_id}, symbol {symbol_to_trade} !!!")
                        print(f"Error: {trade_e}"); import traceback; traceback.print_exc()
                        await app.bot.send_message(chat_id, f"❌ ОШИБКА во время сделки по *{symbol_to_trade}*:\n`{trade_e}`\n\n❗️ *ПРОВЕРЬТЕ СЧЕТ И ПОЗИЦИИ ВРУЧНУЮ!*", parse_mode='Markdown')
                        # Exception occurred, 'finally' will handle cleanup of ongoing_trades
                    finally:
                        # ALWAYS remove from ongoing_trades for this chat and symbol after attempt
                        if symbol_to_trade in chat_config.get('ongoing_trades', {}):
                            print(f"Cleaning up ongoing_trade for {symbol_to_trade} in chat {chat_id}")
                            del chat_config['ongoing_trades'][symbol_to_trade]
                        print(f">>> Finished processing {symbol_to_trade} for chat {chat_id} <<<")
            # End of loop for funding_data_raw pairs
        except Exception as loop_e:
            print("\n!!! UNHANDLED ERROR IN SNIPER LOOP !!!")
            print(f"Error: {loop_e}"); import traceback; traceback.print_exc()
            # To prevent spamming telegram on rapid errors, send to a specific admin chat or log differently
            # For now, just print and sleep
            await asyncio.sleep(30) # Longer sleep on outer loop error

# ===================== MAIN =====================

if __name__ == "__main__":
    print("Initializing bot...")
    # The app object is Application, not ApplicationBuilder after build()
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(MessageHandler(filters.Regex("^📊 Топ-пары$"), show_top_funding))
    application.add_handler(MessageHandler(filters.Regex("^📡 Сигналы$"), signal_menu))
    application.add_handler(CallbackQueryHandler(signal_callback, pattern="^(toggle_sniper|show_top_pairs_inline|set_max_trades_)"))

    conv_marja = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^💰 Маржа$"), set_real_marja)],
        states={SET_MARJA: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_real_marja)]},
        fallbacks=[CommandHandler("cancel", cancel)],
        conversation_timeout=60.0
    )
    application.add_handler(conv_marja)

    conv_plecho = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^⚖️ Плечо$"), set_real_plecho)], # Исправлен эмодзи для соответствия клавиатуре
        states={SET_PLECHO: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_real_plecho)]},
        fallbacks=[CommandHandler("cancel", cancel)],
        conversation_timeout=60.0
    )
    application.add_handler(conv_plecho)

    async def post_init_tasks(app_passed: type(application)): # type hint for clarity
        print("Running post_init tasks...")
        # Pass the Application instance, not the builder
        asyncio.create_task(funding_sniper_loop(app_passed))
        print("Sniper loop task created.")
    
    application.post_init = post_init_tasks

    print("Starting bot polling...")
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        print(f"\nBot polling stopped due to error: {e}")
    finally:
        print("\nBot shutdown.")

# --- END OF FILE bot (8).py ---
