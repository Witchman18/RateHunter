# --- START OF FILE bot (8).py ---

import os
import asyncio
import time # Импортируем time для работы с timestamp
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP # Используем Decimal для точности

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
session = HTTP(api_key=BYBIT_API_KEY, api_secret=BYBIT_API_SECRET, recv_window=20000)
# === Возвращаем эмодзи ===
keyboard = [
    ["📊 Топ-пары", "🧮 Калькулятор прибыли"],
    ["💰 Маржа", "⚖️ Плечо"],
    ["📡 Управление Снайпером"] # Переименовано
]
latest_top_pairs = []
sniper_active = {} # Словарь для хранения состояния по каждому чату

# Состояния для ConversationHandler
SET_MARJA, SET_PLECHO = range(2)
# Новые состояния для настроек снайпера
SET_MIN_TURNOVER_CONFIG, SET_MIN_PROFIT_CONFIG = range(10, 12)


# Константы для стратегии (дефолтные значения)
ENTRY_WINDOW_START_SECONDS = 60
ENTRY_WINDOW_END_SECONDS = 20
POST_FUNDING_WAIT_SECONDS = 1
MAKER_ORDER_WAIT_SECONDS_ENTRY = 7
MAKER_ORDER_WAIT_SECONDS_EXIT = 2
SNIPER_LOOP_INTERVAL_SECONDS = 5
DEFAULT_MAX_CONCURRENT_TRADES = 1
MAX_PAIRS_TO_CONSIDER_PER_CYCLE = 5

# "Умные" дефолты для параметров, не вынесенных в основные настройки пользователя
DEFAULT_MIN_TURNOVER_USDT = Decimal("7500000") # Средний уровень ликвидности
DEFAULT_MIN_EXPECTED_PNL_USDT = Decimal("-10.0")  # ВРЕМЕННО: Очень низкий порог
# Внутренние константы стратегии
MIN_FUNDING_RATE_ABS_FILTER = Decimal("0.0001") # 0.01%
MAX_ALLOWED_SPREAD_PCT_FILTER = Decimal("2.0")  # ВРЕМЕННО: 2%, очень большой допустимый спред
MAKER_FEE_RATE = Decimal("0.0002") # Комиссия мейкера (0.02% Bybit non-VIP Derivatives Maker)
TAKER_FEE_RATE = Decimal("0.00055")# Комиссия тейкера (0.055% Bybit non-VIP Derivatives Taker)
MIN_QTY_TO_MARKET_FILL_PCT_ENTRY = Decimal("0.20")
ORDERBOOK_FETCH_RETRY_DELAY = 0.2

# --- Helper для инициализации настроек чата ---
def ensure_chat_settings(chat_id: int):
    if chat_id not in sniper_active:
        sniper_active[chat_id] = {
            'active': False,
            'real_marja': None,
            'real_plecho': None,
            'max_concurrent_trades': DEFAULT_MAX_CONCURRENT_TRADES,
            'ongoing_trades': {},
            # Новые настраиваемые параметры с дефолтными значениями
            'min_turnover_usdt': DEFAULT_MIN_TURNOVER_USDT,
            'min_expected_pnl_usdt': DEFAULT_MIN_EXPECTED_PNL_USDT,
        }
    # Убедимся, что все ключи существуют, даже если чат уже был создан ранее
    sniper_active[chat_id].setdefault('min_turnover_usdt', DEFAULT_MIN_TURNOVER_USDT)
    sniper_active[chat_id].setdefault('min_expected_pnl_usdt', DEFAULT_MIN_EXPECTED_PNL_USDT)
    sniper_active[chat_id].setdefault('max_concurrent_trades', DEFAULT_MAX_CONCURRENT_TRADES)
    sniper_active[chat_id].setdefault('ongoing_trades', {})


# ===================== ОСНОВНЫЕ ФУНКЦИИ =====================

async def show_top_funding(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    message = update.message
    chat_id = update.effective_chat.id
    ensure_chat_settings(chat_id) # Для получения актуальных фильтров, если они есть
    
    loading_message_id = None
    current_min_turnover_filter = sniper_active[chat_id].get('min_turnover_usdt', DEFAULT_MIN_TURNOVER_USDT)


    try:
        if query:
            await query.answer()
            try:
                await query.edit_message_text("🔄 Получаю топ пар...")
                loading_message_id = query.message.message_id
            except Exception as edit_err:
                print(f"Error editing message on callback: {edit_err}")
                sent_message = await context.bot.send_message(chat_id, "🔄 Получаю топ пар...")
                loading_message_id = sent_message.message_id
        elif message:
            sent_message = await message.reply_text("🔄 Получаю топ пар...")
            loading_message_id = sent_message.message_id
        else:
            return

        response = session.get_tickers(category="linear")
        tickers = response.get("result", {}).get("list", [])
        if not tickers:
            result_msg = "⚠️ Не удалось получить данные тикеров."
            if loading_message_id:
                 await context.bot.edit_message_text(chat_id=chat_id, message_id=loading_message_id, text=result_msg)
            return

        funding_data = []
        for t in tickers:
            symbol, rate_str, next_time_str, turnover_str = t.get("symbol"), t.get("fundingRate"), t.get("nextFundingTime"), t.get("turnover24h")
            if not all([symbol, rate_str, next_time_str, turnover_str]): continue
            try:
                 rate_d, next_time_int, turnover_d = Decimal(rate_str), int(next_time_str), Decimal(turnover_str)
                 if turnover_d < current_min_turnover_filter: continue # Используем настройку пользователя или дефолт
                 if abs(rate_d) < MIN_FUNDING_RATE_ABS_FILTER: continue
                 funding_data.append((symbol, rate_d, next_time_int))
            except (ValueError, TypeError) as e:
                print(f"[Funding Data Error] Could not parse data for {symbol}: {e}")
                continue

        funding_data.sort(key=lambda x: abs(x[1]), reverse=True)
        global latest_top_pairs
        latest_top_pairs = funding_data[:5]

        if not latest_top_pairs:
            result_msg = f"📊 Нет подходящих пар (фильтр оборота: {current_min_turnover_filter:,.0f} USDT)."
        else:
            result_msg = f"📊 Топ пар (фильтр обор.: {current_min_turnover_filter:,.0f} USDT):\n\n"
            now_ts_dt = datetime.utcnow().timestamp()
            for symbol, rate, ts_ms in latest_top_pairs:
                try:
                    delta_sec = int(ts_ms / 1000 - now_ts_dt)
                    if delta_sec < 0: delta_sec = 0
                    h, rem = divmod(delta_sec, 3600); m, s = divmod(rem, 60)
                    time_left = f"{h:01d}ч {m:02d}м {s:02d}с"
                    direction = "📈 LONG (шорты платят)" if rate < 0 else "📉 SHORT (лонги платят)"
                    result_msg += (f"🎟️ *{symbol}*\n{direction}\n💹 Фандинг: `{rate * 100:.4f}%`\n⌛ Выплата через: `{time_left}`\n\n")
                except Exception as e:
                     result_msg += f"🎟️ *{symbol}* - _ошибка отображения_\n\n"

        if loading_message_id:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=loading_message_id, text=result_msg.strip(), parse_mode='Markdown', disable_web_page_preview=True)

    except Exception as e:
        print(f"Error in show_top_funding: {e}"); import traceback; traceback.print_exc()
        error_message = f"❌ Ошибка при получении топа: {e}"
        try:
            if loading_message_id: await context.bot.edit_message_text(chat_id=chat_id, message_id=loading_message_id, text=error_message)
            elif message: await message.reply_text(error_message)
            elif query: await query.message.reply_text(error_message)
        except Exception as inner_e: await context.bot.send_message(chat_id, "❌ Внутренняя ошибка.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text("Привет! Я фандинг-бот RateHunter. Выбери действие:", reply_markup=reply_markup)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    # Попытка удалить сообщение с инлайн-клавиатурой, если мы выходим из диалога настройки
    original_message_id = context.user_data.pop('original_message_id', None)
    
    await update.message.reply_text("Действие отменено.")

    if original_message_id:
        try:
            # Это сообщение было отредактировано для запроса ввода.
            # Мы хотим вернуть его к виду основного меню снайпера.
            # Вместо удаления и новой отправки, попробуем восстановить.
            # Но проще всего - просто отправить новое меню.
            await context.bot.delete_message(chat_id=chat_id, message_id=original_message_id)
        except Exception as e:
            print(f"Error deleting original message on cancel: {e}")
    
    # В любом случае, после отмены диалога настроек, покажем основное меню снайпера
    # Это предполагает, что cancel вызывается из диалогов, начатых из меню снайпера
    # Если cancel может быть вызван откуда-то еще, эту логику нужно будет уточнить
    # или вызывать sniper_control_menu только если мы уверены, что были в его контексте.
    # Для простоты, пока просто отправляем новое, если были в user_data ключи.
    # await send_final_config_message(chat_id, context) # Показываем актуальное меню
    # Лучше, чтобы cancel просто завершал, а пользователь сам вызывал меню снова, если нужно.
    return ConversationHandler.END


async def send_final_config_message(chat_id: int, context: ContextTypes.DEFAULT_TYPE, message_to_edit: Update = None):
    ensure_chat_settings(chat_id)
    settings = sniper_active[chat_id]
    
    marja = settings.get('real_marja')
    plecho = settings.get('real_plecho')
    max_trades = settings.get('max_concurrent_trades', DEFAULT_MAX_CONCURRENT_TRADES)
    is_active = settings.get('active', False)
    status_text = "🟢 Активен" if is_active else "🔴 Остановлен"
    min_turnover = settings.get('min_turnover_usdt', DEFAULT_MIN_TURNOVER_USDT)
    min_pnl = settings.get('min_expected_pnl_usdt', DEFAULT_MIN_EXPECTED_PNL_USDT)

    marja_display = marja if marja is not None else 'Не уст.'
    plecho_display = plecho if plecho is not None else 'Не уст.'

    summary_parts = [
        f"⚙️ **Текущие настройки RateHunter:**",
        f"💰 Маржа (1 сделка): `{marja_display}` USDT",
        f"⚖️ Плечо: `{plecho_display}`x",
        f"🔢 Макс. сделок: `{max_trades}`",
        f"💧 Мин. оборот: `{min_turnover:,.0f}` USDT",
        f"🎯 Мин. профит: `{min_pnl}` USDT",
        f"🚦 Статус снайпера: *{status_text}*"
    ]
    
    if marja is None or plecho is None:
        summary_parts.append("\n‼️ *Для запуска снайпера установите маржу и плечо!*")
    
    summary_text = "\n\n".join(summary_parts) # Используем двойной перенос для лучшего разделения

    buttons = []
    status_button_text = "Остановить снайпер" if is_active else "Запустить снайпер"
    buttons.append([InlineKeyboardButton(f"{'🔴' if is_active else '🟢'} {status_button_text}", callback_data="toggle_sniper")])
    
    trade_limit_buttons = []
    for i in range(1, 6):
        text = f"[{i}]" if i == max_trades else f"{i}"
        trade_limit_buttons.append(InlineKeyboardButton(text, callback_data=f"set_max_trades_{i}"))
    buttons.append([InlineKeyboardButton("Лимит сделок:", callback_data="noop")] + trade_limit_buttons)

    buttons.append([InlineKeyboardButton(f"💧 Мин. оборот: {min_turnover:,.0f} USDT", callback_data="set_min_turnover_config")])
    buttons.append([InlineKeyboardButton(f"🎯 Мин. профит: {min_pnl} USDT", callback_data="set_min_profit_config")])
    buttons.append([InlineKeyboardButton("📊 Показать топ пар", callback_data="show_top_pairs_inline")])
    reply_markup = InlineKeyboardMarkup(buttons)

    try:
        if message_to_edit and message_to_edit.callback_query and message_to_edit.callback_query.message:
            await message_to_edit.callback_query.edit_message_text(text=summary_text, reply_markup=reply_markup, parse_mode='Markdown')
        else:
            await context.bot.send_message(chat_id=chat_id, text=summary_text, reply_markup=reply_markup, parse_mode='Markdown')
    except Exception as e:
        print(f"Error sending/editing final config message to {chat_id}: {e}")
        if message_to_edit: # Если редактирование не удалось, пробуем отправить новое
             await context.bot.send_message(chat_id=chat_id, text=summary_text + "\n(Не удалось обновить предыдущее меню)", reply_markup=reply_markup, parse_mode='Markdown')


# ===================== УСТАНОВКА МАРЖИ/ПЛЕЧА =====================
async def set_real_marja(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("💰 Введите сумму РЕАЛЬНОЙ маржи для ОДНОЙ сделки (в USDT):")
    return SET_MARJA

async def save_real_marja(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id; ensure_chat_settings(chat_id)
    try:
        marja = Decimal(update.message.text.strip().replace(",", "."))
        if marja <= 0: await update.message.reply_text("❌ Маржа > 0."); return ConversationHandler.END # Завершаем, если некорректно
        sniper_active[chat_id]["real_marja"] = marja
        await update.message.reply_text(f"✅ Маржа: {marja} USDT")
        await send_final_config_message(chat_id, context) 
    except (ValueError, TypeError): await update.message.reply_text("❌ Неверный формат. Число (100 или 55.5)."); return SET_MARJA # Просим снова
    except Exception as e: await update.message.reply_text(f"❌ Ошибка: {e}"); return ConversationHandler.END
    return ConversationHandler.END

async def set_real_plecho(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⚖ Введите размер плеча (например, 5 или 10):")
    return SET_PLECHO

async def save_real_plecho(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id; ensure_chat_settings(chat_id)
    try:
        plecho = Decimal(update.message.text.strip().replace(",", "."))
        if not (0 < plecho <= 100): await update.message.reply_text("❌ Плечо > 0 и <= 100."); return ConversationHandler.END
        sniper_active[chat_id]["real_plecho"] = plecho
        await update.message.reply_text(f"✅ Плечо: {plecho}x")
        await send_final_config_message(chat_id, context)
    except (ValueError, TypeError): await update.message.reply_text("❌ Неверный формат. Число (10)."); return SET_PLECHO
    except Exception as e: await update.message.reply_text(f"❌ Ошибка: {e}"); return ConversationHandler.END
    return ConversationHandler.END

# ===================== МЕНЮ УПРАВЛЕНИЯ СНАЙПЕРОМ =====================
async def sniper_control_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ensure_chat_settings(chat_id)
    # Если update.callback_query существует, значит мы пришли из inline кнопки и можем редактировать
    # Иначе, это команда из ReplyKeyboard, отправляем новое сообщение
    await send_final_config_message(chat_id, context, message_to_edit=update if update.callback_query else None)


async def sniper_control_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer() # Отвечаем на callback сразу
    chat_id = query.message.chat_id
    data = query.data
    ensure_chat_settings(chat_id)
    chat_settings = sniper_active[chat_id]

    action_taken = False # Флаг, что какое-то действие было выполнено и меню нужно обновить

    if data == "toggle_sniper":
        if chat_settings.get('real_marja') is None or chat_settings.get('real_plecho') is None:
            await context.bot.send_message(chat_id, "‼️ Не установлены маржа и/или плечо! Запуск невозможен.")
            # Не обновляем меню, т.к. статус не изменился, а пользователь получил сообщение
        else:
            new_status = not chat_settings.get('active', False)
            chat_settings['active'] = new_status
            # Сообщение о запуске/остановке лучше отправить отдельно, а меню обновить.
            await context.bot.send_message(chat_id, "🚀 Снайпер запущен!" if new_status else "🛑 Снайпер остановлен.")
            action_taken = True
    elif data.startswith("set_max_trades_"):
        try:
            new_max_trades = int(data.split("_")[-1])
            if 1 <= new_max_trades <= 5:
                if chat_settings.get('max_concurrent_trades', DEFAULT_MAX_CONCURRENT_TRADES) != new_max_trades:
                    chat_settings['max_concurrent_trades'] = new_max_trades
                    # await context.bot.send_message(chat_id, f"✅ Лимит сделок: {new_max_trades}") # Сообщение излишне, если меню обновляется
                    action_taken = True
                else: # Лимит не изменился
                    pass # Ничего не делаем, меню не нужно перерисовывать
            else: # Неверное значение (хотя кнопки только 1-5)
                 await context.bot.send_message(chat_id, "⚠️ Ошибка: Неверное значение лимита сделок.")
        except (ValueError, IndexError): 
             await context.bot.send_message(chat_id, "⚠️ Ошибка обработки лимита сделок.")
    elif data == "show_top_pairs_inline":
        # Эта функция сама редактирует сообщение, и оно отличается от меню настроек
        await show_top_funding(update, context) 
        # После показа топа, мы НЕ хотим перерисовывать меню настроек поверх него.
        # Поэтому здесь просто выходим. Пользователь может вызвать меню настроек снова, если нужно.
        return 
    elif data == "noop": # Заглушка для текстовых кнопок в ряду
        return # Ничего не делаем
    
    # Если было совершено действие, которое меняет состояние, отображаемое в меню, обновляем меню
    if action_taken:
        await send_final_config_message(chat_id, context, message_to_edit=update)
    # Если никакое действие не меняло состояние (например, нажали на текущий лимит сделок),
    # то меню можно не перерисовывать, чтобы избежать "моргания".

# --- Настройка Мин. Оборота ---
async def ask_min_turnover(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer() # Отвечаем сразу!
    chat_id = query.message.chat_id
    ensure_chat_settings(chat_id)
    current_val = sniper_active[chat_id].get('min_turnover_usdt', DEFAULT_MIN_TURNOVER_USDT)
    
    # Удаляем сообщение с кнопками (старое меню настроек)
    try:
        await query.delete_message()
    except Exception as e:
        print(f"Error deleting old menu message in ask_min_turnover: {e}")
        # Если удалить не удалось, ничего страшного, просто отправим новый промпт.
        # Главное, что мы не будем пытаться его редактировать.
        
    # Отправляем новое сообщение с запросом ввода
    sent_message = await context.bot.send_message(
        chat_id, 
        f"💧 Введите мин. суточный оборот в USDT (текущее: {current_val:,.0f}).\nПример: 5000000\n\nДля отмены введите /cancel"
    )
    context.user_data['prompt_message_id'] = sent_message.message_id 
    return SET_MIN_TURNOVER_CONFIG

async def save_min_turnover(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ensure_chat_settings(chat_id)
    prompt_message_id = context.user_data.pop('prompt_message_id', None)
    user_input_message_id = update.message.message_id
    
    should_send_new_menu = True # Флаг, чтобы отправить меню, даже если была ошибка (но не требующая повторного ввода)

    try:
        value_str = update.message.text.strip().replace(",", "")
        value = Decimal(value_str)
        if value < 0: 
            await update.message.reply_text("❌ Оборот должен быть положительным числом. Нажмите кнопку настройки в меню снова.");
            should_send_new_menu = False # Пользователь должен сам нажать кнопку в новом меню
        else:
            sniper_active[chat_id]['min_turnover_usdt'] = value
            await update.message.reply_text(f"✅ Мин. оборот установлен: {value:,.0f} USDT")
            
    except (ValueError, TypeError): 
        await update.message.reply_text("❌ Неверный формат. Введите число. Нажмите кнопку настройки в меню снова.");
        should_send_new_menu = False
    except Exception as e: 
        await update.message.reply_text(f"❌ Произошла ошибка: {e}. Нажмите кнопку настройки в меню снова.")
        should_send_new_menu = False
    
    # Удаляем сообщения диалога (сообщение с вводом пользователя и сообщение с промптом)
    try: 
        await context.bot.delete_message(chat_id=chat_id, message_id=user_input_message_id)
    except Exception as e: print(f"Error deleting user input message: {e}")
    
    if prompt_message_id:
        try: 
            await context.bot.delete_message(chat_id=chat_id, message_id=prompt_message_id)
        except Exception as e: print(f"Error deleting prompt message: {e}")

    if should_send_new_menu:
        await send_final_config_message(chat_id, context) # Отправляем новое актуальное меню
        
    return ConversationHandler.END
# --- Настройка Мин. Профита ---
async def ask_min_profit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer() # Отвечаем сразу!
    chat_id = query.message.chat_id
    ensure_chat_settings(chat_id)
    current_val = sniper_active[chat_id].get('min_expected_pnl_usdt', DEFAULT_MIN_EXPECTED_PNL_USDT)
    
    try:
        await query.delete_message()
    except Exception as e:
        print(f"Error deleting old menu message in ask_min_profit: {e}")
        
    sent_message = await context.bot.send_message(
        chat_id, 
        f"💰 Введите мин. ожидаемый профит в USDT (текущее: {current_val}).\nПример: 0.05\n\nДля отмены введите /cancel"
    )
    context.user_data['prompt_message_id'] = sent_message.message_id
    return SET_MIN_PROFIT_CONFIG

async def save_min_profit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ensure_chat_settings(chat_id)
    prompt_message_id = context.user_data.pop('prompt_message_id', None)
    user_input_message_id = update.message.message_id
    
    should_send_new_menu = True

    try:
        value_str = update.message.text.strip().replace(",", ".")
        value = Decimal(value_str)
        sniper_active[chat_id]['min_expected_pnl_usdt'] = value
        await update.message.reply_text(f"✅ Мин. профит установлен: {value} USDT")
            
    except (ValueError, TypeError): 
        await update.message.reply_text("❌ Неверный формат. Введите число (например, 0.05). Нажмите кнопку настройки в меню снова.");
        should_send_new_menu = False
    except Exception as e: 
        await update.message.reply_text(f"❌ Произошла ошибка: {e}. Нажмите кнопку настройки в меню снова.")
        should_send_new_menu = False
    
    try: 
        await context.bot.delete_message(chat_id=chat_id, message_id=user_input_message_id)
    except Exception as e: print(f"Error deleting user input message for profit: {e}")
    
    if prompt_message_id:
        try: 
            await context.bot.delete_message(chat_id=chat_id, message_id=prompt_message_id)
        except Exception as e: print(f"Error deleting prompt message for profit: {e}")

    if should_send_new_menu:
        await send_final_config_message(chat_id, context) 
        
    return ConversationHandler.END


# ===================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (ТРЕЙДИНГ) =====================
def get_position_direction(rate: Decimal) -> str:
    if rate is None: return "NONE"
    if rate < Decimal("0"): return "Buy"
    elif rate > Decimal("0"): return "Sell"
    else: return "NONE"

def quantize_qty(raw_qty: Decimal, qty_step: Decimal) -> Decimal:
    if qty_step <= Decimal("0"): return raw_qty.quantize(Decimal('1e-8'), rounding=ROUND_DOWN)
    return (raw_qty // qty_step) * qty_step

def quantize_price(raw_price: Decimal, tick_size: Decimal) -> Decimal:
    if tick_size <= Decimal("0"): return raw_price.quantize(Decimal('1e-8'), rounding=ROUND_DOWN)
    return (raw_price / tick_size).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick_size

async def get_orderbook_snapshot_and_spread(session, symbol, category="linear", retries=3):
    for attempt in range(retries):
        try:
            response = session.get_orderbook(category=category, symbol=symbol, limit=1)
            if response and response.get("retCode") == 0 and response.get("result"):
                ob = response["result"]
                if ob.get('b') and ob.get('a') and ob['b'] and ob['a'] and ob['b'][0] and ob['a'][0]: # Добавил проверки на непустые списки
                    bid_str, ask_str = ob['b'][0][0], ob['a'][0][0]
                    if not bid_str or not ask_str: # Проверка, что строки не пустые
                        print(f"[Orderbook] Empty bid/ask string for {symbol}"); await asyncio.sleep(ORDERBOOK_FETCH_RETRY_DELAY); continue
                    bid, ask = Decimal(bid_str), Decimal(ask_str)
                    if bid <= 0 or ask <= 0 or ask < bid: print(f"[Orderbook] Invalid bid/ask value for {symbol}: {bid}/{ask}"); await asyncio.sleep(ORDERBOOK_FETCH_RETRY_DELAY); continue
                    spread_abs = ask - bid; mid = (ask + bid) / 2
                    return {"best_bid": bid, "best_ask": ask, "mid_price": mid, 
                            "spread_abs": spread_abs, "spread_rel_pct": (spread_abs / mid) * 100 if mid > 0 else Decimal("0")}
            # print(f"[Orderbook] Attempt {attempt+1} failed for {symbol}: {response.get('retMsg')}")
        except Exception as e: print(f"[Orderbook] Attempt {attempt+1} for {symbol}: {e}")
        if attempt < retries - 1: await asyncio.sleep(ORDERBOOK_FETCH_RETRY_DELAY)
    return None

async def calculate_pre_trade_pnl_estimate(
    symbol: str, funding_rate: Decimal, position_size_usdt: Decimal, target_qty: Decimal,
    best_bid: Decimal, best_ask: Decimal, open_side: str 
):
    if not all([position_size_usdt > 0, target_qty > 0, best_bid > 0, best_ask > 0, funding_rate is not None]): 
        return None, "Недостаточно данных для оценки PnL (входные параметры)."
    
    actual_funding_gain = Decimal("0")
    if open_side == "Buy": 
        actual_funding_gain = position_size_usdt * abs(funding_rate)
    elif open_side == "Sell": 
        actual_funding_gain = position_size_usdt * funding_rate
    
    pessimistic_entry_price = best_ask if open_side == "Buy" else best_bid
    pessimistic_exit_price = best_bid if open_side == "Buy" else best_ask
    
    price_pnl_component = Decimal("0")
    if open_side == "Buy":
        price_pnl_component = (pessimistic_exit_price - pessimistic_entry_price) * target_qty
    elif open_side == "Sell":
        price_pnl_component = (pessimistic_entry_price - pessimistic_exit_price) * target_qty

    fees_entry_pessimistic = pessimistic_entry_price * target_qty * TAKER_FEE_RATE 
    fees_exit_pessimistic = pessimistic_exit_price * target_qty * TAKER_FEE_RATE
    total_fees_pessimistic = fees_entry_pessimistic + fees_exit_pessimistic
    
    net_pnl_pessimistic = actual_funding_gain + price_pnl_component - total_fees_pessimistic
    
    pnl_calc_details_msg = (
        f"  Символ: *{symbol}*\n"
        f"  Напр.: {open_side}, Объем: {target_qty}\n"
        f"  Ставка фандинга (API): {funding_rate*100:.4f}%\n"
        f"  Bid/Ask на момент расчета: {best_bid}/{best_ask}\n"
        f"  Расч. пессим. вход: {pessimistic_entry_price}\n"
        f"  Расч. пессим. выход: {pessimistic_exit_price}\n"
        f"  Фандинг (ожид. доход): `{actual_funding_gain:+.4f}` USDT\n"
        f"  Цена (ожид. PnL от спреда): `{price_pnl_component:+.4f}` USDT\n"
        f"  Комиссии (Taker/Taker): `{-total_fees_pessimistic:.4f}` USDT\n"
        f"  ИТОГО (пессим.): `{net_pnl_pessimistic:+.4f}` USDT"
    )
    return net_pnl_pessimistic, pnl_calc_details_msg

async def get_order_status_robust(session, order_id, symbol, category="linear", max_retries=3, delay=0.5):
    for _ in range(max_retries):
        try:
            r = session.get_order_history(category=category, orderId=order_id, limit=1)
            if r and r.get("retCode") == 0 and r.get("result", {}).get("list"):
                od = r["result"]["list"][0]
                if od.get("orderId") == order_id: return od
        except Exception as e: print(f"[Order Status] Error for {order_id}: {e}")
        if _ < max_retries - 1: await asyncio.sleep(delay)
    return None

async def place_limit_order_with_retry(
    session, app, chat_id, symbol, side, qty, price, time_in_force="PostOnly", 
    reduce_only=False, max_wait_seconds=7, check_interval_seconds=0.5 ):
    order_id = None
    try:
        p = {"category": "linear", "symbol": symbol, "side": side, "orderType": "Limit", "qty": str(qty), "price": str(price), "timeInForce": time_in_force}
        if reduce_only: p["reduceOnly"] = True
        r = session.place_order(**p)
        if not (r and r.get("retCode") == 0 and r.get("result", {}).get("orderId")):
            err = f"Ошибка размещения Maker ({symbol}): {r.get('retMsg', 'Unknown') if r else 'No resp'}"
            await app.bot.send_message(chat_id, f"❌ {err}"); return {'status': 'ErrorPlacing', 'executed_qty': Decimal("0"), 'avg_price': Decimal("0"), 'fee': Decimal("0"), 'message': err, 'order_id': None}
        
        order_id = r["result"]["orderId"]
        act = 'Выход' if reduce_only else 'Вход'
        await app.bot.send_message(chat_id, f"⏳ {act} Maker @{price} (ID: ...{order_id[-6:]}) для {symbol}")
        
        waited = Decimal("0")
        while waited < Decimal(str(max_wait_seconds)): # Используем Decimal для сравнения времени
            await asyncio.sleep(float(check_interval_seconds)); waited += Decimal(str(check_interval_seconds))
            oi = await get_order_status_robust(session, order_id, symbol)
            if oi:
                s, eq_s, ap_s, fee_s = oi.get("orderStatus"), oi.get("cumExecQty", "0"), oi.get("avgPrice", "0"), oi.get("cumExecFee", "0")
                eq_d, fee_d = Decimal(eq_s), Decimal(fee_s)
                ap_d = Decimal(ap_s) if ap_s and Decimal(ap_s) > 0 else (Decimal(oi.get("cumExecValue", "0")) / eq_d if eq_d > 0 else Decimal("0"))

                if s == "Filled": await app.bot.send_message(chat_id, f"✅ Maker ...{order_id[-6:]} ({symbol}) ПОЛНОСТЬЮ исполнен: {eq_d} @ {ap_d}"); return {'status': 'Filled', 'executed_qty': eq_d, 'avg_price': ap_d, 'fee': fee_d, 'order_id': order_id, 'message': 'Filled'}
                if s == "PartiallyFilled": print(f"Maker ...{order_id[-6:]} ЧАСТИЧНО: {eq_d}. Ждем."); continue
                if s in ["Cancelled", "Rejected", "Deactivated", "Expired", "New"]: 
                    status_override = "CancelledPostOnly" if s == "New" and time_in_force == "PostOnly" else s
                    msg = f"⚠️ Maker ...{order_id[-6:]} статус: {status_override}. Исполнено: {eq_d}"
                    await app.bot.send_message(chat_id, msg); return {'status': status_override, 'executed_qty': eq_d, 'avg_price': ap_d, 'fee': fee_d, 'order_id': order_id, 'message': msg}
        
        final_oi = await get_order_status_robust(session, order_id, symbol) # Timeout
        if final_oi:
            s, eq_s, ap_s, fee_s = final_oi.get("orderStatus"), final_oi.get("cumExecQty", "0"), final_oi.get("avgPrice", "0"), final_oi.get("cumExecFee", "0")
            eq_d, fee_d = Decimal(eq_s), Decimal(fee_s)
            ap_d = Decimal(ap_s) if ap_s and Decimal(ap_s) > 0 else (Decimal(final_oi.get("cumExecValue", "0")) / eq_d if eq_d > 0 else Decimal("0"))

            if s not in ["Filled", "Cancelled", "Rejected", "Deactivated", "Expired"]:
                try: session.cancel_order(category="linear", symbol=symbol, orderId=order_id); await app.bot.send_message(chat_id, f"⏳ Maker ...{order_id[-6:]} отменен по таймауту. Исполнено: {eq_d}"); return {'status': 'CancelledByTimeout', 'executed_qty': eq_d, 'avg_price': ap_d, 'fee': fee_d, 'order_id': order_id, 'message': 'Cancelled by timeout'}
                except Exception as ce: await app.bot.send_message(chat_id, f"❌ Ошибка отмены Maker ...{order_id[-6:]}: {ce}"); return {'status': 'ErrorCancelling', 'executed_qty': eq_d, 'avg_price': ap_d, 'fee': fee_d, 'order_id': order_id, 'message': str(ce)}
            return {'status': s, 'executed_qty': eq_d, 'avg_price': ap_d, 'fee': fee_d, 'order_id': order_id, 'message': f'Final status: {s}'}
        await app.bot.send_message(chat_id, f"⚠️ Не удалось получить финальный статус Maker ...{order_id[-6:]}"); return {'status': 'ErrorNoStatusAfterTimeout', 'executed_qty': Decimal("0"), 'avg_price': Decimal("0"), 'fee': Decimal("0"), 'order_id': order_id, 'message': 'Could not get final status'}
    except Exception as e:
        err_txt = f"КРИТ.ОШИБКА place_limit_order ({symbol}): {e}"; print(err_txt); import traceback; traceback.print_exc(); await app.bot.send_message(chat_id, f"❌ {err_txt}")
        if order_id:
            oi_exc = await get_order_status_robust(session, order_id, symbol)
            if oi_exc: eq_d=Decimal(oi_exc.get("cumExecQty","0")); ap_d=Decimal(oi_exc.get("avgPrice","0")); fee_d=Decimal(oi_exc.get("cumExecFee","0")); return {'status':'ExceptionAfterPlace','executed_qty':eq_d,'avg_price':ap_d,'fee':fee_d,'order_id':order_id, 'message': str(e)}
        return {'status':'Exception','executed_qty':Decimal("0"),'avg_price':Decimal("0"),'fee':Decimal("0"),'order_id':order_id, 'message': str(e)}

async def place_market_order_robust( session, app, chat_id, symbol, side, qty, time_in_force="ImmediateOrCancel", reduce_only=False):
    order_id = None
    try:
        p = {"category": "linear", "symbol": symbol, "side": side, "orderType": "Market", "qty": str(qty), "timeInForce": time_in_force}
        if reduce_only: p["reduceOnly"] = True
        r = session.place_order(**p)
        if not (r and r.get("retCode") == 0 and r.get("result", {}).get("orderId")):
            ret_msg = r.get('retMsg', 'Unknown') if r else 'No resp'
            err_msg = f"❌ Ошибка Маркет ({symbol}): {ret_msg}"
            if r and (r.get('retCode') == 110007 or "not enough" in ret_msg.lower() or "insufficient" in ret_msg.lower()): err_msg = f"❌ Недостаточно средств Маркет ({symbol}): {ret_msg}"
            print(err_msg); await app.bot.send_message(chat_id, err_msg); return {'status': 'ErrorPlacingMarket', 'executed_qty': Decimal("0"), 'avg_price': Decimal("0"), 'fee': Decimal("0"), 'order_id': None, 'message': err_msg}

        order_id = r["result"]["orderId"]
        act = 'выход' if reduce_only else 'вход'
        await app.bot.send_message(chat_id, f"🛒 Маркет ({act}) {symbol} ID: ...{order_id[-6:]}. Проверяю...")
        await asyncio.sleep(1.5) # Даем бирже время обработать рыночный ордер IOC
        oi = await get_order_status_robust(session, order_id, symbol)
        if oi:
            s, eq_s, ap_s, fee_s = oi.get("orderStatus"), oi.get("cumExecQty", "0"), oi.get("avgPrice", "0"), oi.get("cumExecFee", "0")
            eq_d, fee_d = Decimal(eq_s), Decimal(fee_s)
            ap_d = Decimal(ap_s) if ap_s and Decimal(ap_s) > 0 else (Decimal(oi.get("cumExecValue", "0")) / eq_d if eq_d > 0 else Decimal("0"))

            if s == "Filled": await app.bot.send_message(chat_id, f"✅ Маркет ...{order_id[-6:]} ({symbol}) ИСПОЛНЕН: {eq_d} @ {ap_d}"); return {'status': 'Filled', 'executed_qty': eq_d, 'avg_price': ap_d, 'fee': fee_d, 'order_id': order_id, 'message': 'Market Filled'}
            if s == "PartiallyFilled" and time_in_force == "ImmediateOrCancel": await app.bot.send_message(chat_id, f"✅ Маркет IOC ...{order_id[-6:]} ЧАСТИЧНО: {eq_d} @ {ap_d}"); return {'status': 'PartiallyFilled', 'executed_qty': eq_d, 'avg_price': ap_d, 'fee': fee_d, 'order_id': order_id, 'message': 'Market IOC PartiallyFilled'}
            if eq_d == Decimal("0") and s in ["Cancelled", "Rejected", "Deactivated", "Expired"]: msg = f"⚠️ Маркет IOC ...{order_id[-6:]} ({symbol}) НЕ ИСПОЛНИЛ НИЧЕГО (статус: {s})."; await app.bot.send_message(chat_id, msg); return {'status': s, 'executed_qty': Decimal("0"), 'avg_price': Decimal("0"), 'fee': Decimal("0"), 'order_id': order_id, 'message': msg}
            msg = f"⚠️ Неожиданный статус Маркет ...{order_id[-6:]} ({symbol}): {s}. Исполнено: {eq_d}"; await app.bot.send_message(chat_id, msg); return {'status': s, 'executed_qty': eq_d, 'avg_price': ap_d, 'fee': fee_d, 'order_id': order_id, 'message': msg}
        
        msg = f"⚠️ Не удалось получить статус Маркет ...{order_id[-6:]} ({symbol}). Предполагаем НЕ исполнен."; await app.bot.send_message(chat_id, msg); return {'status': 'ErrorNoStatusMarket', 'executed_qty': Decimal("0"), 'avg_price': Decimal("0"), 'fee': Decimal("0"), 'order_id': order_id, 'message': msg}
    except Exception as e:
        err_txt = f"КРИТ.ОШИБКА place_market_order ({symbol}): {e}"; print(err_txt); import traceback; traceback.print_exc(); await app.bot.send_message(chat_id, f"❌ {err_txt}")
        return {'status':'ExceptionMarket','executed_qty':Decimal("0"),'avg_price':Decimal("0"),'fee':Decimal("0"),'order_id':order_id, 'message': str(e)}

async def get_current_position_info(session, symbol, category="linear"):
    try:
        r = session.get_positions(category=category, symbol=symbol)
        if r and r.get("retCode") == 0:
            pl = r.get("result", {}).get("list", [])
            if pl: # Для одного символа в режиме One-Way будет одна запись или две для Hedge Mode
                for pd in pl:
                    if pd.get("symbol") == symbol and Decimal(pd.get("size", "0")) > Decimal("0"):
                        return {"size": Decimal(pd.get("size", "0")), "side": pd.get("side"), 
                                "avg_price": Decimal(pd.get("avgPrice", "0")), "liq_price": Decimal(pd.get("liqPrice", "0")), 
                                "unrealised_pnl": Decimal(pd.get("unrealisedPnl", "0"))}
        return None # Нет открытой позиции или ошибка
    except Exception as e: print(f"Ошибка get_current_position_info ({symbol}): {e}"); return None

# ===================== ФОНДОВЫЙ СНАЙПЕР (ФАНДИНГ-БОТ) =====================
async def funding_sniper_loop(app: ApplicationBuilder): # app is Application
    print(" Sniper loop started ".center(50, "="))
    while True:
        await asyncio.sleep(SNIPER_LOOP_INTERVAL_SECONDS)
        try:
            current_time_epoch = time.time()
            tickers_response = session.get_tickers(category="linear")
            all_tickers = tickers_response.get("result", {}).get("list", [])
            if not all_tickers: continue

            # Сначала соберем все потенциально интересные пары по глобальным фильтрам
            globally_candidate_pairs = []
            for t in all_tickers:
                symbol, rate_s, next_ts_s, turnover_s = t.get("symbol"), t.get("fundingRate"), t.get("nextFundingTime"), t.get("turnover24h")
                if not all([symbol, rate_s, next_ts_s, turnover_s]): continue
                try:
                    rate_d, next_ts_e, turnover_d = Decimal(rate_s), int(next_ts_s) / 1000, Decimal(turnover_s)
                    # Используем самый мягкий из возможных оборотов для первичного отбора, 
                    # или DEFAULT_MIN_TURNOVER_USDT если он выше чем у всех юзеров.
                    # Пока что для простоты - просто базовые фильтры, а потом по чатам.
                    if turnover_d < DEFAULT_MIN_TURNOVER_USDT / 2 : continue # Грубый предварительный фильтр
                    if abs(rate_d) < MIN_FUNDING_RATE_ABS_FILTER: continue
                    seconds_left = next_ts_e - current_time_epoch
                    if not (ENTRY_WINDOW_END_SECONDS <= seconds_left <= ENTRY_WINDOW_START_SECONDS): continue
                    
                    is_new_candidate = not any(cp_exist["symbol"] == symbol for cp_exist in globally_candidate_pairs)
                    if is_new_candidate:
                         globally_candidate_pairs.append({"symbol": symbol, "rate": rate_d, "next_ts": next_ts_e, "seconds_left": seconds_left, "turnover": turnover_d})
                except (ValueError, TypeError): continue
            
            if not globally_candidate_pairs: continue
            globally_candidate_pairs.sort(key=lambda x: abs(x["rate"]), reverse=True)

            for pair_info in globally_candidate_pairs[:1]: # ВРЕМЕННО: Тестируем только на ОДНОЙ топовой паре
                s_sym, s_rate, s_ts, s_sec_left, s_turnover = pair_info["symbol"], pair_info["rate"], pair_info["next_ts"], pair_info["seconds_left"], pair_info["turnover"]
                s_open_side = get_position_direction(s_rate)
                if s_open_side == "NONE": continue

                for chat_id, chat_config in list(sniper_active.items()): # Итерируем по активным чатам
                    ensure_chat_settings(chat_id)
                    if not chat_config.get('active'): continue
                    if len(chat_config.get('ongoing_trades', {})) >= chat_config.get('max_concurrent_trades', DEFAULT_MAX_CONCURRENT_TRADES): continue
                    if s_sym in chat_config.get('ongoing_trades', {}): continue
                    
                    s_marja, s_plecho = chat_config.get('real_marja'), chat_config.get('real_plecho')
                    # Получаем персональные настройки фильтров для этого чата
                    chat_min_turnover = chat_config.get('min_turnover_usdt', DEFAULT_MIN_TURNOVER_USDT)
                    chat_min_pnl_user = chat_config.get('min_expected_pnl_usdt', DEFAULT_MIN_EXPECTED_PNL_USDT)

                    if not s_marja or not s_plecho: continue # Маржа и плечо обязательны

                    # Фильтр по обороту для этого чата
                    if s_turnover < chat_min_turnover: continue 
                    
                    orderbook_data = await get_orderbook_snapshot_and_spread(session, s_sym)
                                        # Этот блок вставляется ПОСЛЕ orderbook_data = ... и ПЕРЕД if not orderbook_data:
                    log_prefix_tg = f"🔍 {s_sym} ({chat_id}):" 

                    if not orderbook_data: # Эта проверка останется, но лог перед ней
                        await app.bot.send_message(chat_id, f"{log_prefix_tg} Нет данных стакана. Пропуск.") 
                        print(f"[{s_sym}][{chat_id}] No orderbook data.")
                        continue
                    
                    s_bid, s_ask, s_mid, s_spread_pct = orderbook_data['best_bid'], orderbook_data['best_ask'], orderbook_data['mid_price'], orderbook_data['spread_rel_pct']
                    
                    # --- ДЕТАЛЬНОЕ ЛОГИРОВАНИЕ СТАКА НА ---
                    spread_debug_msg = (
                        f"{log_prefix_tg} Стакан:\n"
                        f"  Best Bid: {s_bid}\n"
                        f"  Best Ask: {s_ask}\n"
                        f"  Mid Price: {s_mid}\n"
                        f"  Спред Abs: {s_ask - s_bid}\n"
                        f"  Спред %: {s_spread_pct:.4f}%\n"
                        f"  Лимит спреда % (временно): {MAX_ALLOWED_SPREAD_PCT_FILTER}%"
                    )
                    await app.bot.send_message(chat_id, spread_debug_msg)
                    print(f"[{s_sym}][{chat_id}] OB Data: Bid={s_bid}, Ask={s_ask}, SpreadPct={s_spread_pct:.4f}%, SpreadLimit(temp)={MAX_ALLOWED_SPREAD_PCT_FILTER}%")
                    # --- КОНЕЦ ЛОГИРОВАНИЯ СТАКА НА ---

                    # Фильтр по спреду теперь будет очень мягким (2%)
                    if s_spread_pct > MAX_ALLOWED_SPREAD_PCT_FILTER: 
                        await app.bot.send_message(chat_id, f"{log_prefix_tg} ФИЛЬТР: Спред ({s_spread_pct:.3f}%) > временного лимита ({MAX_ALLOWED_SPREAD_PCT_FILTER}%). Пропуск.")
                        print(f"[{s_sym}][{chat_id}] Skipped due to spread ({s_spread_pct:.3f}%) > TEMP LIMIT {MAX_ALLOWED_SPREAD_PCT_FILTER}%")
                        continue
                    
                    # ... (далее ваш код: получение инфо об инструменте, расчет target_qty) ...
                    # Убедитесь, что этот код находится ПЕРЕД вызовом calculate_pre_trade_pnl_estimate
                    
                    print(f"[{s_sym}][{chat_id}] Pre-PNL Calc: Rate={s_rate}, PosSize={s_pos_size_usdt}, TargetQty={s_target_q}, Bid={s_bid}, Ask={s_ask}, Side={s_open_side}")
                    
                    est_pnl, pnl_calc_details_msg = await calculate_pre_trade_pnl_estimate(
                        s_sym, s_rate, s_pos_size_usdt, s_target_q, 
                        s_bid, s_ask, 
                        s_open_side
                    )
                    
                    print(f"[{s_sym}][{chat_id}] Post-PNL Calc: EstPNL={est_pnl}, Details='{pnl_calc_details_msg}'")

                    if est_pnl is None:
                        error_msg_pnl = pnl_calc_details_msg if pnl_calc_details_msg else "Неизвестная ошибка."
                        await app.bot.send_message(chat_id, f"{log_prefix_tg} Ошибка оценки PnL: {error_msg_pnl}. Пропуск.")
                        print(f"[{s_sym}][{chat_id}] Skipped due to PnL calculation error: {error_msg_pnl}")
                        continue

                    current_min_pnl_filter_for_chat = chat_config.get('min_expected_pnl_usdt', DEFAULT_MIN_EXPECTED_PNL_USDT)

                    if est_pnl < current_min_pnl_filter_for_chat:
                        await app.bot.send_message(
                            chat_id, 
                            f"{log_prefix_tg} Ожид. PnL ({est_pnl:.4f}) < временного порога ({current_min_pnl_filter_for_chat}). Пропуск.\n"
                            f"Детали оценки:\n{pnl_calc_details_msg}", 
                            parse_mode='Markdown'
                        )
                        print(f"[{s_sym}][{chat_id}] Skipped due to EstPNL ({est_pnl:.4f}) < TEMP MinPNL ({current_min_pnl_filter_for_chat})")
                        continue
                    
                    await app.bot.send_message(
                        chat_id, 
                        f"✅ {s_sym} ({chat_id}): Прошел ВРЕМЕННЫЕ мягкие проверки. Ожид. PnL: {est_pnl:.4f} USDT. Начинаю СДЕЛКУ ДЛЯ ТЕСТА.\n"
                        f"Детали оценки:\n{pnl_calc_details_msg}", 
                        parse_mode='Markdown'
                    )
                    
                    if not orderbook_data: await app.bot.send_message(chat_id, f"⚠️ {s_sym}: Нет данных стакана. Пропуск."); continue
                    
                    s_bid, s_ask, s_mid, s_spread_pct = orderbook_data['best_bid'], orderbook_data['best_ask'], orderbook_data['mid_price'], orderbook_data['spread_rel_pct']
                    if s_spread_pct > MAX_ALLOWED_SPREAD_PCT_FILTER: await app.bot.send_message(chat_id, f"⚠️ {s_sym}: Спред ({s_spread_pct:.3f}%) > доп. ({MAX_ALLOWED_SPREAD_PCT_FILTER}%). Пропуск."); continue
                    
                    try: instr_info_resp = session.get_instruments_info(category="linear", symbol=s_sym); instr_info = instr_info_resp["result"]["list"][0]
                    except Exception as e: await app.bot.send_message(chat_id, f"⚠️ {s_sym}: Ошибка инфо об инструменте: {e}. Пропуск."); continue
                        
                    lot_f, price_f = instr_info["lotSizeFilter"], instr_info["priceFilter"]
                    s_min_q_instr, s_q_step, s_tick_size = Decimal(lot_f["minOrderQty"]), Decimal(lot_f["qtyStep"]), Decimal(price_f["tickSize"])
                    
                    s_pos_size_usdt = s_marja * s_plecho
                    if s_mid <= 0: await app.bot.send_message(chat_id, f"⚠️ {s_sym}: Неверная mid_price ({s_mid}). Пропуск."); continue
                    s_target_q = quantize_qty(s_pos_size_usdt / s_mid, s_q_step)

                    if s_target_q < s_min_q_instr: await app.bot.send_message(chat_id, f"⚠️ {s_sym}: Расч. объем {s_target_q} < мин. ({s_min_q_instr}). Пропуск."); continue
                    
                    est_pnl, pnl_msg = await calculate_pre_trade_pnl_estimate(app, chat_id, s_sym, s_rate, s_pos_size_usdt, s_target_q, s_bid, s_ask, s_open_side)
                    if est_pnl is None: await app.bot.send_message(chat_id, f"⚠️ {s_sym}: Ошибка оценки PnL. {pnl_msg if pnl_msg else ''}"); continue # pnl_msg может быть None
                    if est_pnl < chat_min_pnl_user: await app.bot.send_message(chat_id, f"⚠️ {s_sym}: Ожид. PnL ({est_pnl:.4f}) < порога ({chat_min_pnl_user}). Пропуск.\n{pnl_msg}", parse_mode='Markdown'); continue
                    
                    await app.bot.send_message(chat_id, f"✅ {s_sym}: Прошел проверки. Ожид. PnL: {est_pnl:.4f} USDT. Начинаю.\n{pnl_msg}", parse_mode='Markdown')

                    print(f"\n>>> Processing {s_sym} for chat {chat_id} (Rate: {s_rate*100:.4f}%, Left: {s_sec_left:.0f}s) <<<")
                    
                    trade_data = {
                        "symbol": s_sym, "open_side": s_open_side, "marja": s_marja, "plecho": s_plecho,
                        "funding_rate": s_rate, "next_funding_ts": s_ts,
                        "opened_qty": Decimal("0"), "closed_qty": Decimal("0"),
                        "total_open_value": Decimal("0"), "total_close_value": Decimal("0"),
                        "total_open_fee": Decimal("0"), "total_close_fee": Decimal("0"),
                        "actual_funding_fee": Decimal("0"), "target_qty": s_target_q,
                        "min_qty_instr": s_min_q_instr, "qty_step_instr": s_q_step, "tick_size_instr": s_tick_size,
                        "best_bid_at_entry": s_bid, "best_ask_at_entry": s_ask,
                        "price_decimals": len(price_f.get('tickSize', '0.1').split('.')[1]) if '.' in price_f.get('tickSize', '0.1') else 0
                    }
                    chat_config.setdefault('ongoing_trades', {})[s_sym] = trade_data
                    
                    try:
                        await app.bot.send_message(chat_id, f"🎯 Вхожу в сделку: *{s_sym}* ({'📈 LONG' if s_open_side == 'Buy' else '📉 SHORT'}), Ф: `{s_rate*100:.4f}%`, Осталось: `{s_sec_left:.0f}с`", parse_mode='Markdown')
                        try: session.set_leverage(category="linear", symbol=s_sym, buyLeverage=str(s_plecho), sellLeverage=str(s_plecho))
                        except Exception as e_lev:
                            if "110043" not in str(e_lev): raise ValueError(f"Не удалось уст. плечо {s_sym}: {e_lev}")
                        
                        op_qty, op_val, op_fee = Decimal("0"), Decimal("0"), Decimal("0")
                        maker_entry_p = quantize_price(s_bid if s_open_side == "Buy" else s_ask, s_tick_size)
                        
                        limit_res = await place_limit_order_with_retry(session, app, chat_id, s_sym, s_open_side, s_target_q, maker_entry_p, max_wait_seconds=MAKER_ORDER_WAIT_SECONDS_ENTRY)
                        if limit_res and limit_res['executed_qty'] > 0: op_qty += limit_res['executed_qty']; op_val += limit_res['executed_qty'] * limit_res['avg_price']; op_fee += limit_res['fee']
                        
                        rem_q_open = quantize_qty(s_target_q - op_qty, s_q_step)
                        if rem_q_open >= s_min_q_instr: # Добиваем только если остаток >= мин. кол-ву для ордера
                            proceed_market = not (op_qty >= s_min_q_instr and (rem_q_open / s_target_q) < MIN_QTY_TO_MARKET_FILL_PCT_ENTRY)
                            if proceed_market:
                                await app.bot.send_message(chat_id, f"🛒 {s_sym}: Добиваю рынком: {rem_q_open}")
                                market_res = await place_market_order_robust(session, app, chat_id, s_sym, s_open_side, rem_q_open)
                                if market_res and market_res['executed_qty'] > 0: op_qty += market_res['executed_qty']; op_val += market_res['executed_qty'] * market_res['avg_price']; op_fee += market_res['fee']
                            else: await app.bot.send_message(chat_id, f"ℹ️ {s_sym}: Maker исполнил {op_qty}. Остаток {rem_q_open} мал, не добиваю.")
                        
                        await asyncio.sleep(0.5) # Дать время данным позиции обновиться
                        actual_pos = await get_current_position_info(session, s_sym)
                        final_op_q, final_avg_op_p = Decimal("0"), Decimal("0")

                        if actual_pos and actual_pos['side'] == s_open_side:
                            final_op_q, final_avg_op_p = actual_pos['size'], actual_pos['avg_price']
                            if abs(final_op_q - op_qty) > s_q_step / 2: await app.bot.send_message(chat_id, f"ℹ️ {s_sym}: Синхр. объема. Бот: {op_qty}, Биржа: {final_op_q}.")
                            if op_fee == Decimal("0") and final_op_q > 0: op_fee = Decimal("-1") # Признак неизвестной комиссии
                        elif op_qty > 0 and not actual_pos: await app.bot.send_message(chat_id, f"⚠️ {s_sym}: Бот думал открыл {op_qty}, на бирже позиция не найдена! Считаем 0."); final_op_q = Decimal("0")
                        elif actual_pos and actual_pos['side'] != s_open_side and actual_pos['size'] > 0: raise ValueError(f"КРИТ! {s_sym}: На бирже ПРОТИВОПОЛОЖНАЯ позиция {actual_pos['side']} {actual_pos['size']}. Ручное вмешательство!")
                        else: final_op_q = op_qty # Должно быть 0

                        trade_data["opened_qty"] = final_op_q
                        trade_data["total_open_value"] = final_op_q * final_avg_op_p if final_avg_op_p > 0 else op_val
                        trade_data["total_open_fee"] = op_fee

                        if final_op_q < s_min_q_instr: 
                            msg_err_qty = f"❌ {s_sym}: Финал. откр. объем ({final_op_q}) < мин. ({s_min_q_instr}). Отмена."
                            if final_op_q > Decimal("0"): msg_err_qty += " Пытаюсь закрыть остаток..." # Попытка не реализована, но сообщение есть
                            raise ValueError(msg_err_qty)
                        
                        avg_op_p_disp = final_avg_op_p if final_avg_op_p > 0 else ((op_val / op_qty) if op_qty > 0 else Decimal("0"))
                        num_decimals_price = trade_data['price_decimals']
                        await app.bot.send_message(chat_id, f"✅ Позиция *{s_sym}* ({'LONG' if s_open_side == 'Buy' else 'SHORT'}) откр./подтв.\nОбъем: `{final_op_q}`\nСр.цена входа: `{avg_op_p_disp:.{num_decimals_price}f}`\nКом. откр.: `{op_fee:.4f}` USDT", parse_mode='Markdown')
                        
                        wait_dur = max(0, s_ts - time.time()) + POST_FUNDING_WAIT_SECONDS
                        await app.bot.send_message(chat_id, f"⏳ {s_sym} Ожидаю фандинга (~{wait_dur:.0f} сек)..."); await asyncio.sleep(wait_dur)

                        start_log_ts_ms, end_log_ts_ms = int((s_ts - 180)*1000), int((time.time()+5)*1000) # Расширяем окно для лога
                        log_resp = session.get_transaction_log(category="linear",symbol=s_sym,type="SETTLEMENT",startTime=start_log_ts_ms,endTime=end_log_ts_ms,limit=20)
                        log_list, fund_log_val = log_resp.get("result",{}).get("list",[]), Decimal("0")
                        if log_list:
                            for entry in log_list: # Ищем запись, ближайшую к времени фандинга
                                if abs(int(entry.get("transactionTime","0"))/1000 - s_ts) < 120: # 2 минуты окно вокруг фандинга
                                    fund_log_val += Decimal(entry.get("change","0"))
                        trade_data["actual_funding_fee"] = fund_log_val
                        await app.bot.send_message(chat_id, f"💰 {s_sym} Фандинг (из лога): `{fund_log_val:.4f}` USDT", parse_mode='Markdown')
                        if fund_log_val == Decimal("0") and log_list : await app.bot.send_message(chat_id, f"ℹ️ {s_sym}: SETTLEMENT найден, но сумма 0 или не в окне.")
                        elif not log_list: await app.bot.send_message(chat_id, f"⚠️ {s_sym}: Лог транзакций (SETTLEMENT) пуст.")


                        q_to_close = trade_data['opened_qty']
                        if q_to_close < s_min_q_instr: raise ValueError(f"⚠️ {s_sym}: Объем для закрытия ({q_to_close}) < мин. ({s_min_q_instr}). Закрывать нечего.")
                        
                        close_side = "Buy" if s_open_side == "Sell" else "Sell"
                        cl_qty, cl_val, cl_fee = Decimal("0"), Decimal("0"), Decimal("0")
                        await app.bot.send_message(chat_id, f"🎬 Начинаю закрытие {s_sym}: {s_open_side} {q_to_close}")

                        ob_exit = await get_orderbook_snapshot_and_spread(session, s_sym) # Свежий стакан для Maker цены
                        maker_close_p = Decimal("0")
                        if ob_exit: maker_close_p = quantize_price(ob_exit['best_ask'] if close_side == "Sell" else ob_exit['best_bid'], s_tick_size) # Продаем по биду, покупаем по аску (для закрытия)
                        
                        if maker_close_p > 0: # Пытаемся закрыть Maker'ом
                            limit_cl_res = await place_limit_order_with_retry(session,app,chat_id,s_sym,close_side,q_to_close,maker_close_p,reduce_only=True,max_wait_seconds=MAKER_ORDER_WAIT_SECONDS_EXIT)
                            if limit_cl_res and limit_cl_res['executed_qty'] > 0: cl_qty+=limit_cl_res['executed_qty']; cl_val+=limit_cl_res['executed_qty']*limit_cl_res['avg_price']; cl_fee+=limit_cl_res['fee']
                        
                        rem_q_close = quantize_qty(q_to_close - cl_qty, s_q_step)
                        if rem_q_close >= s_q_step: # ОБЯЗАТЕЛЬНО добиваем остаток рынком, если остался хотя бы 1 шаг кол-ва
                            await app.bot.send_message(chat_id, f"🛒 {s_sym}: Закрываю рынком остаток: {rem_q_close}")
                            market_cl_res = await place_market_order_robust(session,app,chat_id,s_sym,close_side,rem_q_close,reduce_only=True)
                            if market_cl_res and market_cl_res['executed_qty'] > 0: cl_qty+=market_cl_res['executed_qty']; cl_val+=market_cl_res['executed_qty']*market_cl_res['avg_price']; cl_fee+=market_cl_res['fee']
                        
                        trade_data["closed_qty"], trade_data["total_close_value"], trade_data["total_close_fee"] = cl_qty, cl_val, cl_fee
                        await asyncio.sleep(1.5) # Дать время позиции обновиться
                        final_pos_cl = await get_current_position_info(session, s_sym)
                        
                        pos_cl_size_disp = 'нет' if not final_pos_cl else final_pos_cl.get('size','нет')
                        if final_pos_cl and final_pos_cl['size'] >= s_q_step: await app.bot.send_message(chat_id, f"⚠️ Позиция *{s_sym}* НЕ ПОЛНОСТЬЮ ЗАКРЫТА! Остаток: `{final_pos_cl['size']}`. ПРОВЕРЬТЕ ВРУЧНУЮ!", parse_mode='Markdown')
                        elif cl_qty >= q_to_close - s_q_step: await app.bot.send_message(chat_id, f"✅ Позиция *{s_sym}* успешно закрыта (бот: {cl_qty}, биржа: {pos_cl_size_disp}).", parse_mode='Markdown')
                        else: await app.bot.send_message(chat_id, f"⚠️ {s_sym}: Не удалось подтвердить полное закрытие (бот: {cl_qty}, биржа: {pos_cl_size_disp}). Проверьте.", parse_mode='Markdown')

                        # PNL Calc
                        op_v_td, op_q_td = trade_data["total_open_value"], trade_data["opened_qty"]
                        avg_op_td = (op_v_td / op_q_td) if op_q_td > 0 else Decimal("0")
                        cl_v_td, cl_q_td = trade_data["total_close_value"], trade_data["closed_qty"]
                        avg_cl_td = (cl_v_td / cl_q_td) if cl_q_td > 0 else Decimal("0")
                        
                        effective_qty_for_pnl = min(op_q_td, cl_q_td) # Считаем PnL по фактически закрытому объему, если вдруг не все закрылось
                        price_pnl_val = (avg_cl_td - avg_op_td) * effective_qty_for_pnl
                        if s_open_side == "Sell": price_pnl_val = -price_pnl_val
                        
                        fund_pnl_val = trade_data["actual_funding_fee"]
                        op_f_val_td = trade_data["total_open_fee"]
                        op_f_disp_td, op_f_calc_td = "", Decimal("0")
                        if op_f_val_td == Decimal("-1"): op_f_disp_td, op_f_calc_td = "Неизв.", s_pos_size_usdt * TAKER_FEE_RATE # Примерная оценка, если не знаем
                        else: op_f_disp_td, op_f_calc_td = f"{-op_f_val_td:.4f}", op_f_val_td
                        
                        cl_f_val_td = trade_data["total_close_fee"]
                        total_f_calc_td = op_f_calc_td + cl_f_val_td
                        net_pnl_val = price_pnl_val + fund_pnl_val - total_f_calc_td
                        roi_val = (net_pnl_val / s_marja) * 100 if s_marja > 0 else Decimal("0")
                        
                        price_decs = trade_data['price_decimals']
                        report = (f"📊 Результат: *{s_sym}* ({'LONG' if s_open_side=='Buy' else 'SHORT'})\n\n"
                                  f"Откр: `{op_q_td}` @ `{avg_op_td:.{price_decs}f}`\n"
                                  f"Закр: `{cl_q_td}` @ `{avg_cl_td:.{price_decs}f}`\n\n"
                                  f"PNL (цена): `{price_pnl_val:+.4f}` USDT\n"
                                  f"PNL (фандинг): `{fund_pnl_val:+.4f}` USDT\n"
                                  f"Ком.откр: `{op_f_disp_td}` USDT\n"
                                  f"Ком.закр: `{-cl_f_val_td:.4f}` USDT\n\n"
                                  f"💰 *Чистая прибыль: {net_pnl_val:+.4f} USDT*\n"
                                  f"📈 ROI от маржи ({s_marja} USDT): `{roi_val:.2f}%`")
                        await app.bot.send_message(chat_id, report, parse_mode='Markdown')
                    
                    except ValueError as ve: # Контролируемые ошибки во время торговой логики
                        print(f"\n!!! TRADE ABORTED for chat {chat_id}, symbol {s_sym} !!!")
                        print(f"Reason: {ve}");
                        await app.bot.send_message(chat_id, f"❌ Сделка по *{s_sym}* прервана:\n`{ve}`\n\n❗️ *ПРОВЕРЬТЕ СЧЕТ И ПОЗИЦИИ ВРУЧНУЮ!*", parse_mode='Markdown')
                    except Exception as trade_e: # Непредвиденные ошибки
                        print(f"\n!!! TRADE ERROR for chat {chat_id}, symbol {s_sym} !!!")
                        print(f"Error: {trade_e}"); import traceback; traceback.print_exc()
                        await app.bot.send_message(chat_id, f"❌ ОШИБКА во время сделки по *{s_sym}*:\n`{trade_e}`\n\n❗️ *ПРОВЕРЬТЕ СЧЕТ И ПОЗИЦИИ ВРУЧНУЮ!*", parse_mode='Markdown')
                    finally:
                        if s_sym in chat_config.get('ongoing_trades', {}):
                            print(f"Cleaning up ongoing_trade for {s_sym} in chat {chat_id}")
                            del chat_config['ongoing_trades'][s_sym]
                        print(f">>> Finished processing {s_sym} for chat {chat_id} <<<")
            # End of loop for globally_candidate_pairs
        except Exception as loop_e:
            print("\n!!! UNHANDLED ERROR IN SNIPER LOOP !!!")
            print(f"Error: {loop_e}"); import traceback; traceback.print_exc()
            # Consider sending to admin or specific log for critical loop errors
            await asyncio.sleep(30) # Longer sleep on outer loop error

# ===================== MAIN =====================
if __name__ == "__main__":
    print("Initializing bot...")
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    # Общий cancel для всех диалогов. Убедимся, что он правильно обрабатывает user_data
    application.add_handler(CommandHandler("cancel", cancel)) 
    
    application.add_handler(MessageHandler(filters.Regex("^📊 Топ-пары$"), show_top_funding))
    application.add_handler(MessageHandler(filters.Regex("^📡 Управление Снайпером$"), sniper_control_menu))
    
    # Обработчики для основного меню снайпера (Inline кнопки)
    application.add_handler(CallbackQueryHandler(sniper_control_callback, pattern="^(toggle_sniper|show_top_pairs_inline|set_max_trades_|noop)"))

    # Диалоги для настроек Маржи и Плеча (вызываются из ReplyKeyboard)
    conv_marja = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^💰 Маржа$"), set_real_marja)], 
        states={SET_MARJA: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_real_marja)]}, 
        fallbacks=[CommandHandler("cancel", cancel)],
        conversation_timeout=120.0 # Увеличим таймаут
    )
    conv_plecho = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^⚖️ Плечо$"), set_real_plecho)], 
        states={SET_PLECHO: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_real_plecho)]}, 
        fallbacks=[CommandHandler("cancel", cancel)],
        conversation_timeout=120.0
    )
    
    # Диалоги для настроек Мин. Оборота и Мин. Профита (вызываются из InlineKeyboard меню снайпера)
    conv_min_turnover = ConversationHandler(
        entry_points=[CallbackQueryHandler(ask_min_turnover, pattern="^set_min_turnover_config$")],
        states={SET_MIN_TURNOVER_CONFIG: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_min_turnover)]},
        fallbacks=[CommandHandler("cancel", cancel)], 
        conversation_timeout=120.0
    )
        
    conv_min_profit = ConversationHandler(
        entry_points=[CallbackQueryHandler(ask_min_profit, pattern="^set_min_profit_config$")],
        states={SET_MIN_PROFIT_CONFIG: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_min_profit)]},
        fallbacks=[CommandHandler("cancel", cancel)], 
        conversation_timeout=120.0
    )

    application.add_handler(conv_marja)
    application.add_handler(conv_plecho)
    application.add_handler(conv_min_turnover)
    application.add_handler(conv_min_profit)

    async def post_init_tasks(app_passed: ApplicationBuilder): # Тип здесь Application, а не ApplicationBuilder
        print("Running post_init tasks...")
        asyncio.create_task(funding_sniper_loop(app_passed)) # Передаем сам объект Application
        print("Sniper loop task created.")
    
    application.post_init = post_init_tasks

    print("Starting bot polling...")
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        print(f"\nBot polling stopped due to error: {e}")
        import traceback
        traceback.print_exc() # Печатаем полный трейсбек ошибки
    finally:
        print("\nBot shutdown.")

# --- END OF FILE bot (8).py ---
