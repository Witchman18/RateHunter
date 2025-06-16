# --- START OF FILE bot (8).py ---
import os
import asyncio
import time # Импортируем time для работы с timestamp
import aiohttp
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP # Используем Decimal для точности

from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes,
    ConversationHandler, CallbackQueryHandler, filters
)
from pybit.unified_trading import HTTP
from mexc_funding import get_funding_rates_mexc

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
MAX_PAIRS_TO_CONSIDER_PER_CYCLE = 1 # Это MAX_PAIRS_FOR_DETAILED_TEST из старой логики

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
            'min_turnover_usdt': DEFAULT_MIN_TURNOVER_USDT,
            'min_expected_pnl_usdt': DEFAULT_MIN_EXPECTED_PNL_USDT,
            # --- НОВЫЕ НАСТРОЙКИ ДЛЯ TP/SL и ФАНДИНГА ---
            'min_funding_rate_threshold': Decimal("0.001"), # Дефолт 0.1%
            'tp_target_profit_ratio_of_funding': Decimal("0.75"), # Дефолт 75% от ожид. фандинга как чистая прибыль
            'sl_max_loss_ratio_to_tp_target': Decimal("0.6"), # Дефолт SL = 60% от целевого TP
        }
    # Убедимся, что все ключи существуют, даже если чат уже был создан ранее
    sniper_active[chat_id].setdefault('min_turnover_usdt', DEFAULT_MIN_TURNOVER_USDT)
    sniper_active[chat_id].setdefault('min_expected_pnl_usdt', DEFAULT_MIN_EXPECTED_PNL_USDT)
    sniper_active[chat_id].setdefault('max_concurrent_trades', DEFAULT_MAX_CONCURRENT_TRADES)
    sniper_active[chat_id].setdefault('ongoing_trades', {})
    # --- ДОБАВЛЯЕМ setdefault ДЛЯ НОВЫХ НАСТРОЕК ---
    sniper_active[chat_id].setdefault('min_funding_rate_threshold', Decimal("0.001"))
    sniper_active[chat_id].setdefault('tp_target_profit_ratio_of_funding', Decimal("0.75"))
    sniper_active[chat_id].setdefault('sl_max_loss_ratio_to_tp_target', Decimal("0.6"))
    sniper_active[chat_id].setdefault('active_exchanges', ['BYBIT', 'MEXC'])


# ===================== ОСНОВНЫЕ ФУНКЦИИ =====================
async def get_mexc_funding_data(min_turnover_filter: Decimal):
    """Асинхронно получает и фильтрует данные по фандингу с MEXC."""
    mexc_url = "https://contract.mexc.com/api/v1/contract/detail"
    funding_data = []
    try:
        async with aiohttp.ClientSession() as session_http:
            async with session_http.get(mexc_url) as response:
                response.raise_for_status() 
                data = await response.json()
                
                if not data or data.get("success") is not True or not data.get("data"):
                    print("[MEXC Data Error] Invalid response format from MEXC.")
                    return []
                
                tickers = data["data"]
                for t in tickers:
                    if not t.get("quoteCoin") == "USDT" or t.get("state") != "SHOW":
                        continue

                    symbol, rate_str, next_time_str, turnover_str = t.get("symbol"), str(t.get("fundingRate")), str(t.get("nextSettleTime")), str(t.get("volume24"))

                    if not all([symbol, rate_str, next_time_str, turnover_str]):
                        continue
                        
                    try:
                        # В этих строках могла быть ошибка, если приходили нечисловые значения
                        rate_d = Decimal(rate_str)
                        next_time_int = int(next_time_str)
                        turnover_d = Decimal(turnover_str) 

                        if turnover_d < min_turnover_filter:
                            continue
                        if abs(rate_d) < MIN_FUNDING_RATE_ABS_FILTER:
                            continue
                            
                        funding_data.append({
                            "exchange": "MEXC",
                            "symbol": symbol.replace("_", ""),
                            "rate": rate_d,
                            "next_ts": next_time_int 
                        })
                    # === ИЗМЕНЕНИЕ ЗДЕСЬ ===
                    # Добавили InvalidOperation, чтобы ловить ошибки преобразования текста в число
                    except (ValueError, TypeError, decimal.InvalidOperation) as e:
                        # Эта строка поможет понять, на какой паре споткнулся бот, но не будет забивать логи
                        # print(f"[MEXC Parsing Warning] Could not parse data for {symbol} (value: {rate_str}, {turnover_str}): {e}")
                        continue
    except aiohttp.ClientError as e:
        print(f"Error fetching MEXC data: {e}")
    except Exception as e:
        print(f"An unexpected error occurred in get_mexc_funding_data: {e}")
        
    return funding_data

# Эта функция будет создавать меню с кнопками-фильтрами
async def show_top_funding_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = update.effective_chat.id
    ensure_chat_settings(chat_id)
    
    active_exchanges = sniper_active[chat_id].get('active_exchanges', [])
    
    bybit_text = "✅ BYBIT" if "BYBIT" in active_exchanges else "⬜️ BYBIT"
    mexc_text = "✅ MEXC" if "MEXC" in active_exchanges else "⬜️ MEXC"
    
    keyboard = [
        [
            InlineKeyboardButton(bybit_text, callback_data="toggle_exchange_BYBIT"),
            InlineKeyboardButton(mexc_text, callback_data="toggle_exchange_MEXC")
        ],
        [
            InlineKeyboardButton("✅ Выбрать все", callback_data="select_all_exchanges"),
            InlineKeyboardButton("⬜️ Снять все", callback_data="deselect_all_exchanges")
        ],
        [
            InlineKeyboardButton("🚀 Показать Топ-5 Пар", callback_data="fetch_top_pairs_filtered")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    menu_text = "Выберите биржи для поиска и нажмите 'Показать'."
    
    if query:
        try:
            await query.edit_message_text(text=menu_text, reply_markup=reply_markup)
        except Exception as e:
            if "Message is not modified" not in str(e):
                print(f"Error editing message in show_top_funding_menu: {e}")
    else:
        await update.message.reply_text(text=menu_text, reply_markup=reply_markup)

# ==============================================================================
# === ЭТО ПОЛНЫЙ И ОКОНЧАТЕЛЬНЫЙ КОД ФУНКЦИИ. ЗАМЕНИТЕ ВАШУ ВЕРСИЮ ЦЕЛИКОМ ===
# ==============================================================================
async def fetch_and_display_top_pairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = update.effective_chat.id
    ensure_chat_settings(chat_id)
    
    active_exchanges = sniper_active[chat_id].get('active_exchanges', [])
    current_min_turnover_filter = sniper_active[chat_id].get('min_turnover_usdt', DEFAULT_MIN_TURNOVER_USDT)
    
    if not active_exchanges:
        await query.answer(text="⚠️ Выберите хотя бы одну биржу!", show_alert=True)
        return

    try:
        await query.edit_message_text(f"🔄 Ищу топ-5 пар на {', '.join(active_exchanges)}...")

        # === ИЗМЕНЕНИЕ 1: Мы будем собирать задачи и их "имена" ===
        tasks = []
        task_map = {} # Словарь для сопоставления задачи с биржей

        if 'BYBIT' in active_exchanges:
            bybit_task = asyncio.create_task(session.get_tickers(category="linear"))
            tasks.append(bybit_task)
            task_map[bybit_task] = 'BYBIT'

        if 'MEXC' in active_exchanges:
            mexc_task = asyncio.create_task(get_mexc_funding_data(current_min_turnover_filter))
            tasks.append(mexc_task)
            task_map[mexc_task] = 'MEXC'
        
        # === ИЗМЕНЕНИЕ 2: Используем `asyncio.wait` вместо `gather` ===
        # Это более гибкий способ, который лучше работает с разным количеством задач
        done, pending = await asyncio.wait(tasks)
        
        all_funding_data = []
        
        for task in done:
            exchange_name = task_map[task]
            try:
                res = task.result()
            except Exception as e:
                print(f"[Data Fetch Error for {exchange_name}] Task failed: {e}")
                continue

            # Обработка Bybit
            if exchange_name == 'BYBIT':
                if res.get("result") and res.get("result", {}).get("list"):
                    for t in res["result"]["list"]:
                        symbol, rate_str, next_time_str, turnover_str = t.get("symbol"), t.get("fundingRate"), t.get("nextFundingTime"), t.get("turnover24h")
                        if not all([symbol, rate_str, next_time_str, turnover_str]): continue
                        try:
                            rate_d, next_time_int, turnover_d = Decimal(rate_str), int(next_time_str), Decimal(turnover_str)
                            if turnover_d < current_min_turnover_filter: continue
                            if abs(rate_d) < MIN_FUNDING_RATE_ABS_FILTER: continue
                            all_funding_data.append({"exchange": "BYBIT", "symbol": symbol, "rate": rate_d, "next_ts": next_time_int})
                        except (ValueError, TypeError, decimal.InvalidOperation): continue
            
            # Обработка MEXC
            elif exchange_name == 'MEXC':
                if isinstance(res, list):
                    all_funding_data.extend(res)

        all_funding_data.sort(key=lambda x: abs(x['rate']), reverse=True)
        
        top_pairs = all_funding_data[:5]

        if not top_pairs:
            result_msg = f"📊 Нет подходящих пар на выбранных биржах."
        else:
            result_msg = f"📊 Топ-5 пар ({', '.join(active_exchanges)}):\n\n"
            now_ts_dt = datetime.utcnow().timestamp()
            for item in top_pairs:
                exchange, symbol, rate, ts_ms = item['exchange'], item['symbol'], item['rate'], item['next_ts']
                try:
                    delta_sec = int(ts_ms / 1000 - now_ts_dt)
                    if delta_sec < 0: delta_sec = 0
                    h, rem = divmod(delta_sec, 3600); m, s = divmod(rem, 60)
                    time_left = f"{h:01d}ч {m:02d}м {s:02d}с"
                    direction = "📈 LONG (шорты платят)" if rate < 0 else "📉 SHORT (лонги платят)"
                    result_msg += (f"🏦 *{exchange}* | 🎟️ *{symbol}*\n{direction}\n"
                                   f"💹 Фандинг: `{rate * 100:.4f}%`\n⌛ Выплата через: `{time_left}`\n\n")
                except Exception:
                     result_msg += f"🏦 *{exchange}* | 🎟️ *{symbol}* - _ошибка отображения_\n\n"
        
        reply_markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅️ Назад к выбору бирж", callback_data="back_to_funding_menu")
        ]])
        
        await query.edit_message_text(
            text=result_msg.strip(), 
            reply_markup=reply_markup,
            parse_mode='Markdown', 
            disable_web_page_preview=True
        )

    except Exception as e:
        print("!!! AN ERROR OCCURRED IN fetch_and_display_top_pairs !!!")
        import traceback
        traceback.print_exc()
        
        error_message = "❌ Ошибка при получении топа. Проверьте логи."
        try:
            await query.edit_message_text(text=error_message)
        except Exception:
            await context.bot.send_message(chat_id, "❌ Внутренняя ошибка.")
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

# Этот обработчик будет управлять меню "Топ-пар"
async def top_funding_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer() # Сразу отвечаем на нажатие
    
    chat_id = query.message.chat_id
    data = query.data
    ensure_chat_settings(chat_id)
    
    if data == "fetch_top_pairs_filtered":
        await fetch_and_display_top_pairs(update, context)
        return
        
    if data == "back_to_funding_menu":
        await show_top_funding_menu(update, context)
        return

    # Логика для кнопок-переключателей
    active_exchanges = sniper_active[chat_id].get('active_exchanges', [])
    
    if data.startswith("toggle_exchange_"):
        exchange = data.split("_")[-1]
        if exchange in active_exchanges:
            active_exchanges.remove(exchange)
        else:
            active_exchanges.append(exchange)
    elif data == "select_all_exchanges":
        active_exchanges = ['BYBIT', 'MEXC']
    elif data == "deselect_all_exchanges":
        active_exchanges = []
        
    sniper_active[chat_id]['active_exchanges'] = active_exchanges
    # После любого изменения настроек - перерисовываем меню
    await show_top_funding_menu(update, context)


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
    # Получаем новые настройки с дефолтами
    min_fr_thresh = settings.get('min_funding_rate_threshold', Decimal("0.001"))
    tp_ratio_funding = settings.get('tp_target_profit_ratio_of_funding', Decimal("0.75"))
    sl_ratio_tp = settings.get('sl_max_loss_ratio_to_tp_target', Decimal("0.6"))

    marja_display = marja if marja is not None else 'Не уст.'
    plecho_display = plecho if plecho is not None else 'Не уст.'

    summary_parts = [
        f"⚙️ **Текущие настройки RateHunter:**",
        f"💰 Маржа (1 сделка): `{marja_display}` USDT",
        f"⚖️ Плечо: `{plecho_display}`x",
        f"🔢 Макс. сделок: `{max_trades}`",
        f"💧 Мин. оборот: `{min_turnover:,.0f}` USDT",
        f"📊 Мин. ставка фандинга: `{min_fr_thresh*100:.1f}%`",
        f"🎯 Мин. профит (предв. оценка): `{min_pnl}` USDT",
        f"📈 TP (цель от фандинга): `{tp_ratio_funding*100:.0f}%`",
        f"📉 SL (риск от TP): `{sl_ratio_tp*100:.0f}%`",
        f"🚦 Статус снайпера: *{status_text}*"
    ]
    
    if marja is None or plecho is None:
        summary_parts.append("\n‼️ *Для запуска снайпера установите маржу и плечо!*")
    
    summary_text = "\n\n".join(summary_parts) # ИСПРАВЛЕН ОТСТУП

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
    
    # --- ИСПРАВЛЕНЫ ОТСТУПЫ ДЛЯ НОВЫХ КНОПОК ---
    # Кнопки для Мин. ставки фандинга
    fr_buttons_row = [InlineKeyboardButton("Мин.Фанд%:", callback_data="noop")]
    fr_options = {"0.1": "0.001", "0.3": "0.003", "0.5": "0.005", "1.0": "0.01"} 
    for text, val_str in fr_options.items():
        val_decimal = Decimal(val_str)
        button_text = f"[{text}%]" if min_fr_thresh == val_decimal else f"{text}%"
        fr_buttons_row.append(InlineKeyboardButton(button_text, callback_data=f"set_min_fr_{val_str}"))
    buttons.append(fr_buttons_row)

    # Кнопки для TP (доля от фандинга)
    tp_buttons_row = [InlineKeyboardButton("TP% от Ф:", callback_data="noop")]
    tp_options = {"50": "0.50", "65": "0.65", "75": "0.75", "90": "0.90"}
    for text, val_str in tp_options.items():
        val_decimal = Decimal(val_str)
        button_text = f"[{text}%]" if tp_ratio_funding == val_decimal else f"{text}%"
        tp_buttons_row.append(InlineKeyboardButton(button_text, callback_data=f"set_tp_rf_{val_str}"))
    buttons.append(tp_buttons_row)
    
    # Кнопки для SL (доля от TP)
    sl_buttons_row = [InlineKeyboardButton("SL% от TP:", callback_data="noop")]
    sl_options = {"40": "0.40", "50": "0.50", "60": "0.60", "75": "0.75"}
    for text, val_str in sl_options.items():
        val_decimal = Decimal(val_str)
        button_text = f"[{text}%]" if sl_ratio_tp == val_decimal else f"{text}%"
        sl_buttons_row.append(InlineKeyboardButton(button_text, callback_data=f"set_sl_rtp_{val_str}"))
    buttons.append(sl_buttons_row)
    # --- КОНЕЦ ИСПРАВЛЕНИЯ ОТСТУПОВ ДЛЯ НОВЫХ КНОПОК ---
    
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
    await query.answer() # Отвечаем на callback сразу, чтобы кнопка не "висела"
    chat_id = query.message.chat_id
    data = query.data
    ensure_chat_settings(chat_id)
    chat_settings = sniper_active[chat_id]

    action_taken = False # Флаг, что какое-то действие было выполнено и меню нужно обновить

    if data == "toggle_sniper":
        if chat_settings.get('real_marja') is None or chat_settings.get('real_plecho') is None:
            # Отправляем всплывающее уведомление вместо сообщения в чат, чтобы не замусоривать
            await context.bot.answer_callback_query(query.id, text="‼️ Не установлены маржа и/или плечо!", show_alert=True)
        else:
            new_status = not chat_settings.get('active', False)
            chat_settings['active'] = new_status
            # Сообщение о запуске/остановке лучше отправить отдельно, а меню обновить.
            # Для answer_callback_query текст короткий, основное изменение будет в меню.
            await context.bot.answer_callback_query(query.id, text="🚀 Снайпер запущен!" if new_status else "🛑 Снайпер остановлен.")
            action_taken = True # Статус всегда меняется, так что меню нужно обновить
    
    elif data.startswith("set_max_trades_"):
        try:
            new_max_trades = int(data.split("_")[-1])
            current_max_trades = chat_settings.get('max_concurrent_trades', DEFAULT_MAX_CONCURRENT_TRADES)
            if 1 <= new_max_trades <= 5:
                if current_max_trades != new_max_trades:
                    chat_settings['max_concurrent_trades'] = new_max_trades
                    action_taken = True
                    await context.bot.answer_callback_query(query.id, text=f"Лимит сделок: {new_max_trades}")
                else:
                    # Значение не изменилось, просто отвечаем на callback
                    await context.bot.answer_callback_query(query.id, text="Лимит сделок не изменен.")
            else: 
                 # Это условие не должно срабатывать, если кнопки генерируются правильно
                 await context.bot.answer_callback_query(query.id, text="⚠️ Ошибка: Неверное значение лимита.", show_alert=True)
        except (ValueError, IndexError): 
             await context.bot.answer_callback_query(query.id, text="⚠️ Ошибка обработки лимита.", show_alert=True)

    elif data.startswith("set_min_fr_"): 
        try:
            rate_val_str = data.split("_")[-1] 
            new_val = Decimal(rate_val_str)
            current_val = chat_settings.get('min_funding_rate_threshold', Decimal("0.001"))
            if current_val != new_val:
                chat_settings['min_funding_rate_threshold'] = new_val
                action_taken = True
                await context.bot.answer_callback_query(query.id, text=f"Мин. ставка фандинга: {new_val*100:.1f}%")
            else:
                await context.bot.answer_callback_query(query.id, text="Значение не изменилось")
        except Exception as e:
            print(f"Error setting min_funding_rate_threshold: {e}")
            await context.bot.answer_callback_query(query.id, text="Ошибка установки значения", show_alert=True)

    elif data.startswith("set_tp_rf_"): 
        try:
            val_str = data.split("_")[-1] 
            new_val = Decimal(val_str)
            current_val = chat_settings.get('tp_target_profit_ratio_of_funding', Decimal("0.75"))
            if current_val != new_val:
                chat_settings['tp_target_profit_ratio_of_funding'] = new_val
                action_taken = True
                await context.bot.answer_callback_query(query.id, text=f"TP (доля от фандинга): {new_val*100:.0f}%")
            else:
                await context.bot.answer_callback_query(query.id, text="Значение не изменилось")
        except Exception as e:
            print(f"Error setting tp_target_profit_ratio_of_funding: {e}")
            await context.bot.answer_callback_query(query.id, text="Ошибка установки значения", show_alert=True)

    elif data.startswith("set_sl_rtp_"): 
        try:
            val_str = data.split("_")[-1]
            new_val = Decimal(val_str)
            current_val = chat_settings.get('sl_max_loss_ratio_to_tp_target', Decimal("0.6"))
            if current_val != new_val:
                chat_settings['sl_max_loss_ratio_to_tp_target'] = new_val
                action_taken = True
                await context.bot.answer_callback_query(query.id, text=f"SL (доля от TP): {new_val*100:.0f}%")
            else:
                await context.bot.answer_callback_query(query.id, text="Значение не изменилось")
        except Exception as e:
            print(f"Error setting sl_max_loss_ratio_to_tp_target: {e}")
            await context.bot.answer_callback_query(query.id, text="Ошибка установки значения", show_alert=True)
            
    elif data == "show_top_pairs_inline":
        # Эта функция сама управляет сообщением (редактирует или отправляет новое)
        await show_top_funding(update, context) 
        # После показа топа, мы НЕ хотим перерисовывать меню настроек поверх него.
        return # Важно! Выходим, чтобы не вызывать send_final_config_message ниже
    
    elif data == "noop":
        # Ничего не делаем, на callback уже ответили в начале функции
        return # Важно! Выходим, чтобы не вызывать send_final_config_message
    
    # Только если action_taken is True, обновляем сообщение с конфигурацией
    if action_taken:
        await send_final_config_message(chat_id, context, message_to_edit=update)
    # Если action_taken is False (например, нажали на уже активную кнопку или noop),
    # то меню не перерисовываем, чтобы избежать ошибки "Message is not modified".

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

    fees_entry_maker = pessimistic_entry_price * target_qty * MAKER_FEE_RATE 
    fees_exit_maker = pessimistic_exit_price * target_qty * MAKER_FEE_RATE
    total_maker_maker_fees = fees_entry_maker + fees_exit_maker
         
    net_pnl_estimate_mm = actual_funding_gain + price_pnl_component - total_maker_maker_fees
    
    pnl_calc_details_msg = (
        f"  Символ: *{symbol}*\n"
        f"  Напр.: {open_side}, Объем: {target_qty}\n"
        f"  Ставка фандинга (API): {funding_rate*100:.4f}%\n"
        f"  Bid/Ask на момент расчета: {best_bid}/{best_ask}\n"
        f"  Расч. пессим. вход: {pessimistic_entry_price}\n"
        f"  Расч. пессим. выход: {pessimistic_exit_price}\n"
        f"  Фандинг (ожид. доход): `{actual_funding_gain:+.4f}` USDT\n"
        f"  Цена (ожид. PnL от спреда): `{price_pnl_component:+.4f}` USDT\n"
        f"  Комиссии (Maker/Maker): `{-total_maker_maker_fees:.4f}` USDT\n"
        f"  ИТОГО (оценка с M/M ком.): `{net_pnl_estimate_mm:+.4f}` USDT"
    )
    return net_pnl_estimate_mm, pnl_calc_details_msg

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
# ===================== ФОНДОВЫЙ СНАЙПЕР (ФАНДИНГ-БОТ) =====================
async def funding_sniper_loop(app: ApplicationBuilder): # app is Application
    print(" Sniper loop started ".center(50, "="))
    while True: # Внешний бесконечный цикл снайпера
        await asyncio.sleep(SNIPER_LOOP_INTERVAL_SECONDS) # Пауза между итерациями снайпера
        try: # Внешний try для перехвата ошибок на уровне всего цикла итерации
            current_time_epoch = time.time()
            tickers_response = session.get_tickers(category="linear")
            all_tickers = tickers_response.get("result", {}).get("list", [])
            if not all_tickers: 
                print("[SniperLoop] No tickers received. Skipping iteration.")
                continue

            globally_candidate_pairs = []
            # Первичный отбор кандидатов
            for t in all_tickers:
                symbol, rate_s, next_ts_s, turnover_s = t.get("symbol"), t.get("fundingRate"), t.get("nextFundingTime"), t.get("turnover24h")
                if not all([symbol, rate_s, next_ts_s, turnover_s]): continue
                try:
                    rate_d, next_ts_e, turnover_d = Decimal(rate_s), int(next_ts_s) / 1000, Decimal(turnover_s)
                    # Грубый предварительный фильтр по обороту (можно настроить или убрать)
                    if turnover_d < DEFAULT_MIN_TURNOVER_USDT / 2 : continue 
                    # Фильтр по минимальной абсолютной ставке фандинга
                    if abs(rate_d) < MIN_FUNDING_RATE_ABS_FILTER: continue
                    
                    # Рассчитываем время до фандинга НА МОМЕНТ ПЕРВИЧНОГО ОТБОРА
                    seconds_left_initial = next_ts_e - current_time_epoch 
                    # Проверяем, попадает ли это время в окно входа
                    if not (ENTRY_WINDOW_END_SECONDS <= seconds_left_initial <= ENTRY_WINDOW_START_SECONDS): continue
                    
                    is_new_candidate = not any(cp_exist["symbol"] == symbol for cp_exist in globally_candidate_pairs)
                    if is_new_candidate:
                         globally_candidate_pairs.append({
                             "symbol": symbol, 
                             "rate": rate_d, 
                             "next_ts": next_ts_e, # Время фандинга в секундах эпохи
                             "seconds_left": seconds_left_initial, # Сохраняем для логов/сравнения
                             "turnover": turnover_d
                         })
                except (ValueError, TypeError) as e_parse:
                    print(f"[SniperLoop] Error parsing ticker data for {symbol}: {e_parse}")
                    continue
            
            if not globally_candidate_pairs:
                # print("[SniperLoop] No globally candidate pairs after initial filter.")
                continue

            # Сортируем кандидатов: сначала по времени до фандинга (меньше = лучше), потом по модулю ставки (больше = лучше)
            globally_candidate_pairs.sort(key=lambda x: (x["seconds_left"], -abs(x["rate"])))
            
            # Логирование отобранных кандидатов с их ИЗНАЧАЛЬНЫМ и АКТУАЛЬНЫМ временем до фандинга
            print(f"[SniperLoop] Top {len(globally_candidate_pairs)} candidates after initial filter. Checking up to {MAX_PAIRS_TO_CONSIDER_PER_CYCLE}.")
            if globally_candidate_pairs:
                log_time_now_for_candidates = time.time()
                for i, p_info_debug in enumerate(globally_candidate_pairs[:MAX_PAIRS_TO_CONSIDER_PER_CYCLE]):
                     log_actual_sl = p_info_debug["next_ts"] - log_time_now_for_candidates
                     print(f"  Candidate {i+1} (pre-check): {p_info_debug['symbol']}, "
                           f"InitialTimeLeft: {p_info_debug['seconds_left']:.0f}s, "
                           f"CurrentTimeLeft: {log_actual_sl:.0f}s, "
                           f"Rate: {p_info_debug['rate']*100:.4f}%")

            # Начинаем итерацию по отфильтрованным парам (не более MAX_PAIRS_TO_CONSIDER_PER_CYCLE штук)
            for pair_info in globally_candidate_pairs[:MAX_PAIRS_TO_CONSIDER_PER_CYCLE]: 
                s_sym = pair_info["symbol"]
                s_rate = pair_info["rate"]
                s_ts = pair_info["next_ts"] # Время фандинга в эпохе (секунды)
                s_turnover_pair = pair_info["turnover"] # Оборот этой конкретной пары

                # --- КЛЮЧЕВАЯ ПРОВЕРКА: АКТУАЛЬНОЕ ВРЕМЯ ДО ФАНДИНГА ПЕРЕД ОБРАБОТКОЙ ---
                current_time_for_processing = time.time() # Получаем текущее время СЕЙЧАС
                actual_seconds_left = s_ts - current_time_for_processing # Считаем, сколько СЕЙЧАС секунд осталось до фандинга

                # Проверяем, находится ли АКТУАЛЬНОЕ время в допустимом окне входа
                if not (ENTRY_WINDOW_END_SECONDS <= actual_seconds_left <= ENTRY_WINDOW_START_SECONDS):
                    print(f"[SniperLoop][{s_sym}] Skipped (before chat loop). Actual time left ({actual_seconds_left:.0f}s) "
                          f"is outside entry window ({ENTRY_WINDOW_END_SECONDS}s - {ENTRY_WINDOW_START_SECONDS}s).")
                    continue # Если время вышло (или еще слишком рано), пропускаем эту пару и переходим к следующей
                # --- КОНЕЦ КЛЮЧЕВОЙ ПРОВЕРКИ ---

                s_open_side = get_position_direction(s_rate) # Определяем направление сделки (Buy/Sell)
                if s_open_side == "NONE": 
                    print(f"[SniperLoop][{s_sym}] Skipped. Open side is NONE (funding rate is zero or None).")
                    continue 

                # Теперь итерируем по активным чатам, чтобы проверить, подходит ли эта пара для них
                for chat_id, chat_config in list(sniper_active.items()): 
                    ensure_chat_settings(chat_id) # Убеждаемся, что настройки чата существуют
                    if not chat_config.get('active'): continue # Если снайпер в этом чате не активен, пропускаем
                    # Если уже есть максимальное количество одновременных сделок для этого чата, пропускаем
                        # ... (другие проверки чата: active, ongoing_trades, etc.) ...
    
    # --- НОВАЯ ПРОВЕРКА: Фильтр по минимальной ставке фандинга для ДАННОГО ЧАТА ---
                    current_chat_min_fr_threshold = chat_config.get('min_funding_rate_threshold', MIN_FUNDING_RATE_ABS_FILTER) # MIN_FUNDING_RATE_ABS_FILTER - глобальный дефолт, если в чате нет
                    if abs(s_rate) < current_chat_min_fr_threshold:
                        # print(f"[{s_sym}][{chat_id}] Skipped. Pair funding rate {abs(s_rate)*100:.4f}% < chat threshold {current_chat_min_fr_threshold*100:.1f}%.")
                      continue # Пропускаем эту пару ДЛЯ ЭТОГО ЧАТА
                        # --- КОНЕЦ НОВОЙ ПРОВЕРКИ ---

    # Получаем настройки маржи и плеча для этого чата (этот код у вас уже есть)
                    s_marja = chat_config.get('real_marja')
    # ... и так далее ...
                    if len(chat_config.get('ongoing_trades', {})) >= chat_config.get('max_concurrent_trades', DEFAULT_MAX_CONCURRENT_TRADES):
                        # print(f"[SniperLoop][{s_sym}][{chat_id}] Skipped. Max concurrent trades reached for this chat.")
                        continue
                    # Если по этой паре уже есть активная сделка в этом чате, пропускаем
                    if s_sym in chat_config.get('ongoing_trades', {}):
                        # print(f"[SniperLoop][{s_sym}][{chat_id}] Skipped. Trade for this symbol already ongoing in this chat.")
                        continue
                    
                    # Получаем настройки маржи и плеча для этого чата
                    s_marja = chat_config.get('real_marja')
                    s_plecho = chat_config.get('real_plecho')
                    # Получаем персональные настройки фильтров для этого чата
                    chat_min_turnover = chat_config.get('min_turnover_usdt', DEFAULT_MIN_TURNOVER_USDT)
                    chat_min_pnl_user = chat_config.get('min_expected_pnl_usdt', DEFAULT_MIN_EXPECTED_PNL_USDT)

                    if not s_marja or not s_plecho: continue # Если маржа или плечо не установлены, пропускаем
                    
                    # Проверяем оборот пары с настройками чата
                    if s_turnover_pair < chat_min_turnover: 
                        # print(f"[{s_sym}][{chat_id}] Skipped. Pair turnover ({s_turnover_pair:,.0f}) < chat min turnover ({chat_min_turnover:,.0f}).")
                        continue 
                    
                    log_prefix_tg = f"🔍 {s_sym} ({chat_id}):" 
                    
                    # Получаем данные стакана
                    orderbook_data = await get_orderbook_snapshot_and_spread(session, s_sym)
                    if not orderbook_data: 
                        await app.bot.send_message(chat_id, f"{log_prefix_tg} Нет данных стакана. Пропуск.") 
                        print(f"[{s_sym}][{chat_id}] No orderbook data. Skipping.")
                        continue
                    
                    s_bid, s_ask, s_mid, s_spread_pct = orderbook_data['best_bid'], orderbook_data['best_ask'], orderbook_data['mid_price'], orderbook_data['spread_rel_pct']
                    
                    # Отправляем информацию о стакане в чат (можно закомментировать для уменьшения спама)
                    # spread_debug_msg = (
                    #     f"{log_prefix_tg} Стакан:\n"
                    #     f"  Best Bid: {s_bid}\n"
                    #     f"  Best Ask: {s_ask}\n"
                    #     f"  Mid Price: {s_mid}\n"
                    #     f"  Спред Abs: {s_ask - s_bid}\n"
                    #     f"  Спред %: {s_spread_pct:.4f}%\n"
                    #     f"  Лимит спреда % (временно): {MAX_ALLOWED_SPREAD_PCT_FILTER}%"
                    # )
                    # await app.bot.send_message(chat_id, spread_debug_msg)
                    # print(f"[{s_sym}][{chat_id}] OB Data: Bid={s_bid}, Ask={s_ask}, SpreadPct={s_spread_pct:.4f}%, SpreadLimit(temp)={MAX_ALLOWED_SPREAD_PCT_FILTER}%")

                    # Проверяем спред
                    if s_spread_pct > MAX_ALLOWED_SPREAD_PCT_FILTER: 
                        await app.bot.send_message(chat_id, f"{log_prefix_tg} ФИЛЬТР: Спред ({s_spread_pct:.3f}%) > лимита ({MAX_ALLOWED_SPREAD_PCT_FILTER}%). Пропуск.")
                        print(f"[{s_sym}][{chat_id}] Skipped due to spread ({s_spread_pct:.3f}%) > LIMIT {MAX_ALLOWED_SPREAD_PCT_FILTER}%")
                        continue
                    
                    # Получаем информацию об инструменте (шаг лота, тика и т.д.)
                    try: 
                        instr_info_resp = session.get_instruments_info(category="linear", symbol=s_sym)
                        instr_info = instr_info_resp["result"]["list"][0]
                    except Exception as e_instr: 
                        await app.bot.send_message(chat_id, f"⚠️ {s_sym}: Ошибка инфо об инструменте: {e_instr}. Пропуск.")
                        print(f"[{s_sym}][{chat_id}] Error getting instrument info: {e_instr}. Skipping.")
                        continue
                        
                                            # ... (код try...except для get_instruments_info успешно завершен, или был continue)
                        
                    lot_f, price_f = instr_info["lotSizeFilter"], instr_info["priceFilter"]
                    s_min_q_instr, s_q_step, s_tick_size = Decimal(lot_f["minOrderQty"]), Decimal(lot_f["qtyStep"]), Decimal(price_f["tickSize"])
                    
                    # Рассчитываем размер позиции и целевое количество
                    s_pos_size_usdt = s_marja * s_plecho # s_marja и s_plecho берутся из chat_config
                    if s_mid <= 0: # s_mid из orderbook_data, полученного ранее
                        await app.bot.send_message(chat_id, f"⚠️ {s_sym}: Неверная mid_price ({s_mid}). Пропуск.")
                        print(f"[{s_sym}][{chat_id}] Invalid mid_price ({s_mid}). Skipping.")
                        continue
                    s_target_q = quantize_qty(s_pos_size_usdt / s_mid, s_q_step)

                    if s_target_q < s_min_q_instr: 
                        await app.bot.send_message(chat_id, f"⚠️ {s_sym}: Расч. объем {s_target_q} < мин. ({s_min_q_instr}). Пропуск.")
                        print(f"[{s_sym}][{chat_id}] Calculated qty {s_target_q} < min instrument qty {s_min_q_instr}. Skipping.")
                        continue
                    
                    # Оцениваем потенциальный PnL перед сделкой
                    print(f"[{s_sym}][{chat_id}] Pre-PNL Calc: Rate={s_rate*100:.4f}%, PosSize={s_pos_size_usdt}, TargetQty={s_target_q}, Bid={s_bid}, Ask={s_ask}, Side={s_open_side}, ActualTimeLeft={actual_seconds_left:.0f}s")
                    est_pnl, pnl_calc_details_msg = await calculate_pre_trade_pnl_estimate(
                        s_sym, s_rate, s_pos_size_usdt, s_target_q, 
                        s_bid, s_ask, s_open_side # s_bid и s_ask из orderbook_data
                    )
                    print(f"[{s_sym}][{chat_id}] Post-PNL Calc: EstPNL={est_pnl}, Details='{pnl_calc_details_msg if est_pnl is not None else 'Error in PNL calc'}'")

                    if est_pnl is None: 
                        error_msg_pnl = pnl_calc_details_msg if pnl_calc_details_msg else "Неизвестная ошибка при расчете PnL."
                        await app.bot.send_message(chat_id, f"{log_prefix_tg} Ошибка оценки PnL: {error_msg_pnl}. Пропуск.")
                        print(f"[{s_sym}][{chat_id}] Skipped due to PnL calculation error: {error_msg_pnl}")
                        continue

                    # Проверяем, соответствует ли ожидаемый PnL минимальному порогу пользователя
                    # chat_min_pnl_user должен быть уже определен из chat_config
                    if est_pnl < chat_min_pnl_user: 
                        await app.bot.send_message(
                            chat_id, 
                            f"{log_prefix_tg} Ожид. PnL ({est_pnl:.4f}) < порога ({chat_min_pnl_user}). Пропуск.\n"
                            f"Детали оценки:\n{pnl_calc_details_msg}", 
                            parse_mode='Markdown'
                        )
                        print(f"[{s_sym}][{chat_id}] Skipped due to EstPNL ({est_pnl:.4f}) < MinPNL_User ({chat_min_pnl_user})")
                        continue
                    
                    # Если все проверки пройдены, сообщаем о начале сделки
                    await app.bot.send_message(
                        chat_id, 
                        f"✅ {s_sym} ({chat_id}): Прошел проверки. Ожид. PnL: {est_pnl:.4f} USDT. Начинаю СДЕЛКУ.\n"
                        f"Детали оценки:\n{pnl_calc_details_msg}", 
                        parse_mode='Markdown'
                    )
                    
                    print(f"\n>>> Processing {s_sym} for chat {chat_id} (Rate: {s_rate*100:.4f}%, Actual Left: {actual_seconds_left:.0f}s) <<<")
                    
                    # --- НОВЫЙ БЛОК: Расчет целевых TP/SL в USDT на основе настроек чата ---
                    # s_marja, s_plecho, s_rate уже должны быть определены для этого чата и пары
                    position_size_usdt_for_tpsl_calc = s_marja * s_plecho 
                    expected_funding_usdt = position_size_usdt_for_tpsl_calc * abs(s_rate) # s_rate уже десятичное

                    tp_ratio = chat_config.get('tp_target_profit_ratio_of_funding', Decimal('0.75'))
                    tp_target_net_profit_usdt = expected_funding_usdt * tp_ratio

                    sl_ratio_of_tp = chat_config.get('sl_max_loss_ratio_to_tp_target', Decimal('0.6'))
                    sl_max_net_loss_usdt = tp_target_net_profit_usdt * sl_ratio_of_tp

                    # Коррекция SL, чтобы он не был больше ~95% от ожидаемого фандинга
                    if sl_max_net_loss_usdt > expected_funding_usdt * Decimal('0.95'):
                        sl_max_net_loss_usdt = expected_funding_usdt * Decimal('0.95')
                        print(f"[{s_sym}][{chat_id}] SL_max_net_loss_usdt corrected to 95% of expected funding: {sl_max_net_loss_usdt:.4f} USDT")

                    print(f"[{s_sym}][{chat_id}] Calculated for TP/SL: ExpectedFunding={expected_funding_usdt:.4f}, TP_TargetNetProfit={tp_target_net_profit_usdt:.4f}, SL_MaxNetLoss={sl_max_net_loss_usdt:.4f}")
                    # --- КОНЕЦ НОВОГО БЛОКА ---
                    # Готовим данные для отслеживания сделки

# --- НАЧАЛО ТОРГОВОЙ ЛОГИКИ (вход, ожидание фандинга, выход, отчет) ---
                    # Готовим данные для отслеживания сделки
                    trade_data = {
                        "symbol": s_sym, "open_side": s_open_side, "marja": s_marja, "plecho": s_plecho,
                        "funding_rate": s_rate, "next_funding_ts": s_ts, # s_ts - время эпохи фандинга
                        "opened_qty": Decimal("0"), "closed_qty": Decimal("0"),
                        "total_open_value": Decimal("0"), "total_close_value": Decimal("0"),
                        "total_open_fee": Decimal("0"), "total_close_fee": Decimal("0"),
                        "actual_funding_fee": Decimal("0"), "target_qty": s_target_q,
                        "min_qty_instr": s_min_q_instr, "qty_step_instr": s_q_step, "tick_size_instr": s_tick_size,
                        "best_bid_at_entry": s_bid, "best_ask_at_entry": s_ask,
                        "price_decimals": len(price_f.get('tickSize', '0.1').split('.')[1]) if '.' in price_f.get('tickSize', '0.1') else 0
                    }
                    chat_config.setdefault('ongoing_trades', {})[s_sym] = trade_data # Добавляем сделку в активные для этого чата
                    
                                            # ... (предыдущий код, заканчивающийся на await app.bot.send_message с подтверждением открытия позиции) ...
                        
                                            # --- НАЧАЛО ТОРГОВОЙ ЛОГИКИ (вход, ожидание фандинга, выход, отчет) ---
                    try: # try для всей торговой операции по этой паре в этом чате
                        await app.bot.send_message(chat_id, f"🎯 Вхожу в сделку: *{s_sym}* ({'📈 LONG' if s_open_side == 'Buy' else '📉 SHORT'}), Ф: `{s_rate*100:.4f}%`, Осталось: `{actual_seconds_left:.0f}с`", parse_mode='Markdown')
                        
                        try: 
                            session.set_leverage(category="linear", symbol=s_sym, buyLeverage=str(s_plecho), sellLeverage=str(s_plecho))
                        except Exception as e_lev:
                            if "110043" not in str(e_lev): # 110043: Leverage not modified
                                raise ValueError(f"Не удалось уст. плечо {s_sym}: {e_lev}")
                        
                        op_qty, op_val, op_fee = Decimal("0"), Decimal("0"), Decimal("0")
                        maker_entry_p = quantize_price(s_bid if s_open_side == "Buy" else s_ask, s_tick_size)
                        
                        limit_res = await place_limit_order_with_retry(session, app, chat_id, s_sym, s_open_side, s_target_q, maker_entry_p, max_wait_seconds=MAKER_ORDER_WAIT_SECONDS_ENTRY)
                        if limit_res and limit_res['executed_qty'] > 0: 
                            op_qty += limit_res['executed_qty']
                            op_val += limit_res['executed_qty'] * limit_res['avg_price']
                            op_fee += limit_res['fee']
                        
                        rem_q_open = quantize_qty(s_target_q - op_qty, s_q_step)
                        if rem_q_open >= s_min_q_instr: 
                            proceed_market = not (op_qty >= s_min_q_instr and (rem_q_open / s_target_q) < MIN_QTY_TO_MARKET_FILL_PCT_ENTRY)
                            if proceed_market:
                                await app.bot.send_message(chat_id, f"🛒 {s_sym}: Добиваю рынком: {rem_q_open}")
                                market_res = await place_market_order_robust(session, app, chat_id, s_sym, s_open_side, rem_q_open)
                                if market_res and market_res['executed_qty'] > 0: 
                                    op_qty += market_res['executed_qty']
                                    op_val += market_res['executed_qty'] * market_res['avg_price']
                                    op_fee += market_res['fee']
                            else: 
                                await app.bot.send_message(chat_id, f"ℹ️ {s_sym}: Maker исполнил {op_qty}. Остаток {rem_q_open} мал, не добиваю.")
                        
                        await asyncio.sleep(0.5) 
                        actual_pos = await get_current_position_info(session, s_sym)
                        final_op_q, final_avg_op_p = Decimal("0"), Decimal("0")

                        if actual_pos and actual_pos['side'] == s_open_side:
                            final_op_q, final_avg_op_p = actual_pos['size'], actual_pos['avg_price']
                            if abs(final_op_q - op_qty) > s_q_step / 2: 
                                await app.bot.send_message(chat_id, f"ℹ️ {s_sym}: Синхр. объема. Бот: {op_qty}, Биржа: {final_op_q}.")
                            if op_fee == Decimal("0") and final_op_q > 0: 
                                op_fee = Decimal("-1") # Флаг неизвестной комиссии, если была исполнена позиция
                        elif op_qty > 0 and not actual_pos: 
                            await app.bot.send_message(chat_id, f"⚠️ {s_sym}: Бот думал открыл {op_qty}, на бирже позиция не найдена! Считаем 0.")
                            final_op_q = Decimal("0")
                        elif actual_pos and actual_pos['side'] != s_open_side and actual_pos['size'] > 0: 
                            raise ValueError(f"КРИТ! {s_sym}: На бирже ПРОТИВОПОЛОЖНАЯ позиция {actual_pos['side']} {actual_pos['size']}. Ручное вмешательство!")
                        else: 
                            final_op_q = op_qty 

                        trade_data["opened_qty"] = final_op_q
                        trade_data["total_open_value"] = final_op_q * final_avg_op_p if final_avg_op_p > 0 else op_val
                        trade_data["total_open_fee"] = op_fee

                        if final_op_q < s_min_q_instr: 
                            msg_err_qty = f"❌ {s_sym}: Финал. откр. объем ({final_op_q}) < мин. ({s_min_q_instr}). Отмена."
                            if final_op_q > Decimal("0"): 
                                msg_err_qty += " Пытаюсь закрыть остаток..." # Логика закрытия остатка здесь не реализована, но сообщение есть
                            raise ValueError(msg_err_qty)
                        
                        avg_op_p_disp = final_avg_op_p if final_avg_op_p > 0 else ((op_val / op_qty) if op_qty > 0 else Decimal("0"))
                        num_decimals_price = trade_data['price_decimals']
                        await app.bot.send_message(chat_id, f"✅ Позиция *{s_sym}* ({'LONG' if s_open_side == 'Buy' else 'SHORT'}) откр./подтв.\nОбъем: `{final_op_q}`\nСр.цена входа: `{avg_op_p_disp:.{num_decimals_price}f}`\nКом. откр.: `{op_fee:.4f}` USDT", parse_mode='Markdown')
                        
                        # --- НАЧАЛО БЛОКА УСТАНОВКИ TP/SL НА БИРЖЕ ---
                        if final_op_q > Decimal("0"): # Устанавливаем TP/SL только если позиция действительно открыта
                            tp_target_net_profit_usdt = trade_data.get('tp_target_net_profit_usdt', Decimal("0"))
                            sl_max_net_loss_usdt = trade_data.get('sl_max_net_loss_usdt', Decimal("0"))
                            expected_funding_usdt_on_trade_open = trade_data.get('expected_funding_usdt_on_trade_open', Decimal("0"))

                            _position_size_usdt = trade_data.get('marja', Decimal("0")) * trade_data.get('plecho', Decimal("0"))
                            expected_total_fees_usdt = _position_size_usdt * (TAKER_FEE_RATE + TAKER_FEE_RATE) 

                            price_pnl_needed_for_tp = tp_target_net_profit_usdt - expected_funding_usdt_on_trade_open + expected_total_fees_usdt
                            price_pnl_triggering_sl = -sl_max_net_loss_usdt - expected_funding_usdt_on_trade_open - expected_total_fees_usdt

                            price_change_for_tp_per_unit = price_pnl_needed_for_tp / final_op_q
                            price_change_for_sl_per_unit = price_pnl_triggering_sl / final_op_q 

                            take_profit_price_raw = Decimal("0")
                            stop_loss_price_raw = Decimal("0")

                            if s_open_side == "Buy":
                                take_profit_price_raw = final_avg_op_p + price_change_for_tp_per_unit
                                stop_loss_price_raw = final_avg_op_p + price_change_for_sl_per_unit 
                            elif s_open_side == "Sell":
                                take_profit_price_raw = final_avg_op_p - price_change_for_tp_per_unit
                                stop_loss_price_raw = final_avg_op_p - price_change_for_sl_per_unit
                            
                            s_tick_size = trade_data['tick_size_instr']
                            take_profit_price = quantize_price(take_profit_price_raw, s_tick_size)
                            stop_loss_price = quantize_price(stop_loss_price_raw, s_tick_size)

                            print(f"[{s_sym}][{chat_id}] Calculated TP price: {take_profit_price}, SL price: {stop_loss_price}")
                            await app.bot.send_message(chat_id, f"ℹ️ {s_sym}: Расчетные цены для биржи:\nTP: `{take_profit_price}`\nSL: `{stop_loss_price}`")

                            can_place_tp = False
                            if s_open_side == "Buy" and take_profit_price > final_avg_op_p: can_place_tp = True
                            elif s_open_side == "Sell" and take_profit_price < final_avg_op_p and take_profit_price > 0: can_place_tp = True
                            
                            can_place_sl = False
                            if s_open_side == "Buy" and stop_loss_price < final_avg_op_p and stop_loss_price > 0: can_place_sl = True
                            elif s_open_side == "Sell" and stop_loss_price > final_avg_op_p: can_place_sl = True

                            if can_place_tp and can_place_sl and \
                               ((s_open_side == "Buy" and take_profit_price <= stop_loss_price) or \
                                (s_open_side == "Sell" and take_profit_price >= stop_loss_price)):
                                await app.bot.send_message(chat_id, f"⚠️ {s_sym}: Логическая ошибка TP/SL цен: TP {take_profit_price}, SL {stop_loss_price}. Установка отменена.")
                                print(f"[{s_sym}][{chat_id}] Logical error in TP/SL prices. TP: {take_profit_price}, SL: {stop_loss_price}. Cancelling TP/SL setup.")
                                can_place_tp = False
                                can_place_sl = False
                            
                            if can_place_tp or can_place_sl:
                                params_trading_stop = {
                                    "category": "linear", "symbol": s_sym, "tpslMode": "Full",
                                    "tpTriggerBy": "LastPrice", "slTriggerBy": "LastPrice",
                                    "positionIdx" : 0 
                                }
                                if can_place_tp:
                                    params_trading_stop["takeProfit"] = str(take_profit_price)
                                    params_trading_stop["tpOrderType"] = "Market" 
                                if can_place_sl:
                                    params_trading_stop["stopLoss"] = str(stop_loss_price)
                                    params_trading_stop["slOrderType"] = "Market"

                                try:
                                    print(f"[{s_sym}][{chat_id}] Attempting to set trading stop: {params_trading_stop}")
                                    response_tpsl = session.set_trading_stop(**params_trading_stop)
                                    print(f"[{s_sym}][{chat_id}] Set_trading_stop response: {response_tpsl}")
                                    if response_tpsl and response_tpsl.get("retCode") == 0:
                                        await app.bot.send_message(chat_id, f"✅ {s_sym}: TP/SL ордера успешно установлены/обновлены на бирже.")
                                        if can_place_tp: trade_data['tp_order_price_set_on_exchange'] = take_profit_price
                                        if can_place_sl: trade_data['sl_order_price_set_on_exchange'] = stop_loss_price
                                    else: 
                                        err_msg_tpsl = response_tpsl.get('retMsg', 'Unknown error') if response_tpsl else "No response"
                                        await app.bot.send_message(chat_id, f"⚠️ {s_sym}: Не удалось установить TP/SL на бирже: {err_msg_tpsl}")
                                        print(f"[{s_sym}][{chat_id}] Failed to set TP/SL on exchange: {err_msg_tpsl}")
                                except Exception as e_tpsl: 
                                    await app.bot.send_message(chat_id, f"❌ {s_sym}: Ошибка при установке TP/SL на бирже: {e_tpsl}")
                                    print(f"[{s_sym}][{chat_id}] Exception while setting TP/SL on exchange: {e_tpsl}")
                            else: 
                                await app.bot.send_message(chat_id, f"ℹ️ {s_sym}: Не удалось рассчитать корректные или безопасные цены для установки TP/SL.")
                        else: # Этот else относится к if final_op_q > Decimal("0"):
                            print(f"[{s_sym}][{chat_id}] Position quantity is zero (final_op_q = {final_op_q}). Skipping TP/SL setup.")
                        # --- КОНЕЦ БЛОКА УСТАНОВКИ TP/SL НА БИРЖЕ ---
                        
                
                        current_wait_time = time.time()
                        wait_dur = max(0, s_ts - current_wait_time) + POST_FUNDING_WAIT_SECONDS 
                        await app.bot.send_message(chat_id, f"⏳ {s_sym} Ожидаю фандинга (~{wait_dur:.0f} сек)..."); await asyncio.sleep(wait_dur)

                        start_log_ts_ms, end_log_ts_ms = int((s_ts - 180)*1000), int((time.time()+5)*1000) 
                        log_resp = session.get_transaction_log(category="linear",symbol=s_sym,type="SETTLEMENT",startTime=start_log_ts_ms,endTime=end_log_ts_ms,limit=20)
                        log_list, fund_log_val = log_resp.get("result",{}).get("list",[]), Decimal("0")
                        if log_list:
                            for entry in log_list: 
                                if abs(int(entry.get("transactionTime","0"))/1000 - s_ts) < 120: 
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

                        ob_exit = await get_orderbook_snapshot_and_spread(session, s_sym) 
                        maker_close_p = Decimal("0")
                        if ob_exit: maker_close_p = quantize_price(ob_exit['best_ask'] if close_side == "Sell" else ob_exit['best_bid'], s_tick_size) 
                        
                        if maker_close_p > 0: 
                            limit_cl_res = await place_limit_order_with_retry(session,app,chat_id,s_sym,close_side,q_to_close,maker_close_p,reduce_only=True,max_wait_seconds=MAKER_ORDER_WAIT_SECONDS_EXIT)
                            if limit_cl_res and limit_cl_res['executed_qty'] > 0: 
                                cl_qty+=limit_cl_res['executed_qty']; cl_val+=limit_cl_res['executed_qty']*limit_cl_res['avg_price']; cl_fee+=limit_cl_res['fee']
                        
                        rem_q_close = quantize_qty(q_to_close - cl_qty, s_q_step)
                        if rem_q_close >= s_q_step: 
                            await app.bot.send_message(chat_id, f"🛒 {s_sym}: Закрываю рынком остаток: {rem_q_close}")
                            market_cl_res = await place_market_order_robust(session,app,chat_id,s_sym,close_side,rem_q_close,reduce_only=True)
                            if market_cl_res and market_cl_res['executed_qty'] > 0: 
                                cl_qty+=market_cl_res['executed_qty']; cl_val+=market_cl_res['executed_qty']*market_cl_res['avg_price']; cl_fee+=market_cl_res['fee']
                        
                        trade_data["closed_qty"], trade_data["total_close_value"], trade_data["total_close_fee"] = cl_qty, cl_val, cl_fee
                        await asyncio.sleep(1.5) 
                        final_pos_cl = await get_current_position_info(session, s_sym)
                        
                        pos_cl_size_disp = 'нет' if not final_pos_cl else final_pos_cl.get('size','нет')
                        if final_pos_cl and final_pos_cl['size'] >= s_q_step: await app.bot.send_message(chat_id, f"⚠️ Позиция *{s_sym}* НЕ ПОЛНОСТЬЮ ЗАКРЫТА! Остаток: `{final_pos_cl['size']}`. ПРОВЕРЬТЕ ВРУЧНУЮ!", parse_mode='Markdown')
                        elif cl_qty >= q_to_close - s_q_step: await app.bot.send_message(chat_id, f"✅ Позиция *{s_sym}* успешно закрыта (бот: {cl_qty}, биржа: {pos_cl_size_disp}).", parse_mode='Markdown')
                        else: await app.bot.send_message(chat_id, f"⚠️ {s_sym}: Не удалось подтвердить полное закрытие (бот: {cl_qty}, биржа: {pos_cl_size_disp}). Проверьте.", parse_mode='Markdown')

                        op_v_td, op_q_td = trade_data["total_open_value"], trade_data["opened_qty"]
                        avg_op_td = (op_v_td / op_q_td) if op_q_td > 0 else Decimal("0")
                        cl_v_td, cl_q_td = trade_data["total_close_value"], trade_data["closed_qty"]
                        avg_cl_td = (cl_v_td / cl_q_td) if cl_q_td > 0 else Decimal("0")
                        
                        effective_qty_for_pnl = min(op_q_td, cl_q_td) 
                        price_pnl_val = (avg_cl_td - avg_op_td) * effective_qty_for_pnl
                        if s_open_side == "Sell": price_pnl_val = -price_pnl_val
                        
                        fund_pnl_val = trade_data["actual_funding_fee"]
                        op_f_val_td = trade_data["total_open_fee"]
                        op_f_disp_td, op_f_calc_td = "", Decimal("0")
                        if op_f_val_td == Decimal("-1"): op_f_disp_td, op_f_calc_td = "Неизв.", s_pos_size_usdt * TAKER_FEE_RATE 
                        else: op_f_disp_td, op_f_calc_td = f"{-op_f_val_td:.4f}", op_f_val_td # Комиссия на открытие обычно отрицательная
                        
                        cl_f_val_td = trade_data["total_close_fee"] # Комиссия на закрытие обычно отрицательная
                        total_fee_calculated = op_f_calc_td + cl_f_val_td # Суммируем комиссии (обе отрицательные)
                        
                        net_pnl_val = price_pnl_val + fund_pnl_val + total_fee_calculated # Прибавляем, так как комиссии уже с минусом
                        roi_val = (net_pnl_val / s_marja) * 100 if s_marja > 0 else Decimal("0")
                        
                        price_decs = trade_data['price_decimals']
                        report = (f"📊 Результат: *{s_sym}* ({'LONG' if s_open_side=='Buy' else 'SHORT'})\n\n"
                                  f"Откр: `{op_q_td}` @ `{avg_op_td:.{price_decs}f}`\n"
                                  f"Закр: `{cl_q_td}` @ `{avg_cl_td:.{price_decs}f}`\n\n"
                                  f"PNL (цена): `{price_pnl_val:+.4f}` USDT\n"
                                  f"PNL (фандинг): `{fund_pnl_val:+.4f}` USDT\n"
                                  f"Ком.откр: `{op_f_disp_td}` USDT\n"
                                  f"Ком.закр: `{cl_f_val_td:+.4f}` USDT\n\n" # Отображаем с плюсом, если отрицательная
                                  f"💰 *Чистая прибыль: {net_pnl_val:+.4f} USDT*\n"
                                  f"📈 ROI от маржи ({s_marja} USDT): `{roi_val:.2f}%`")
                        await app.bot.send_message(chat_id, report, parse_mode='Markdown')
                    
                    except ValueError as ve: 
                        print(f"\n!!! TRADE ABORTED for chat {chat_id}, symbol {s_sym} !!! Reason: {ve}")
                        await app.bot.send_message(chat_id, f"❌ Сделка по *{s_sym}* прервана:\n`{ve}`\n\n❗️ *ПРОВЕРЬТЕ СЧЕТ И ПОЗИЦИИ ВРУЧНУЮ!*", parse_mode='Markdown')
                    except Exception as trade_e: 
                        print(f"\n!!! TRADE ERROR for chat {chat_id}, symbol {s_sym} !!! Error: {trade_e}")
                        import traceback; traceback.print_exc()
                        await app.bot.send_message(chat_id, f"❌ ОШИБКА во время сделки по *{s_sym}*:\n`{trade_e}`\n\n❗️ *ПРОВЕРЬТЕ СЧЕТ И ПОЗИЦИИ ВРУЧНУЮ!*", parse_mode='Markdown')
                    finally:
                        if s_sym in chat_config.get('ongoing_trades', {}):
                            print(f"Cleaning up ongoing_trade for {s_sym} in chat {chat_id}")
                            del chat_config['ongoing_trades'][s_sym]
                        print(f">>> Finished processing {s_sym} for chat {chat_id} <<<")
                # Конец цикла по chat_id
            # Конец цикла по globally_candidate_pairs
        except Exception as loop_e: # Внешний except для всего while True цикла снайпера
            print("\n!!! UNHANDLED ERROR IN SNIPER LOOP !!!")
            print(f"Error: {loop_e}"); import traceback; traceback.print_exc()
            await asyncio.sleep(30) # Пауза перед следующей попыткой основного цикла
# --- КОНЕЦ КОРРЕКТНОГО БЛОКА КОДА ДЛЯ funding_sniper_loop --- 

# ===================== MAIN =====================
if __name__ == "__main__":
    print("Initializing bot...")
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("cancel", cancel)) 
    
    application.add_handler(MessageHandler(filters.Regex("^📊 Топ-пары$"), show_top_funding_menu))
    
    application.add_handler(MessageHandler(filters.Regex("^📡 Управление Снайпером$"), sniper_control_menu))
    
    application.add_handler(CallbackQueryHandler(sniper_control_callback, pattern="^(toggle_sniper|show_top_pairs_inline|set_max_trades_|noop|set_min_fr_|set_tp_rf_|set_sl_rtp_)"))

    application.add_handler(CallbackQueryHandler(top_funding_menu_callback, pattern="^(toggle_exchange_|select_all_exchanges|deselect_all_exchanges|fetch_top_pairs_filtered|back_to_funding_menu)$"))

    conv_marja = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^💰 Маржа$"), set_real_marja)], 
        states={SET_MARJA: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_real_marja)]}, 
        fallbacks=[CommandHandler("cancel", cancel)],
        conversation_timeout=120.0 
    )
    conv_plecho = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^⚖️ Плечо$"), set_real_plecho)], 
        states={SET_PLECHO: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_real_plecho)]}, 
        fallbacks=[CommandHandler("cancel", cancel)],
        conversation_timeout=120.0
    )
    
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

    async def post_init_tasks(app_passed: ApplicationBuilder): 
        print("Running post_init tasks...")
        asyncio.create_task(funding_sniper_loop(app_passed)) 
        print("Sniper loop task created.")
    
    application.post_init = post_init_tasks

    print("Starting bot polling...")
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        print(f"\nBot polling stopped due to error: {e}")
        import traceback
        traceback.print_exc() 
    finally:
        print("\nBot shutdown.")

# --- END OF FILE bot (8).py ---
