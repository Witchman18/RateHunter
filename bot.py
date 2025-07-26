# =========================================================================
# ===================== RateHunter 2.0 - Alpha v0.1 =====================
# =========================================================================
# Концепция: Информационно-аналитический бот для поиска и анализа
# ставок финансирования на различных криптовалютных биржах.
#
# Ключевые отличия от старой версии:
# - НЕТ ТОРГОВОЙ ЛОГИКИ: Бот не размещает ордера и не управляет счетами.
# - НЕТ ПРИВАТНЫХ КЛЮЧЕЙ: Работа только с публичными API, 100% безопасность.
# - МОДУЛЬНАЯ АРХИТЕКТУРА: Легко добавлять новые биржи и функции.
# - ИНТЕРАКТИВНЫЙ ИНТЕРФЕЙС: Панели с "проваливанием" (drill-down) для анализа.
# - ГИБКИЕ ФИЛЬТРЫ: Настройка уведомлений по биржам, ставке, объему и волатильности.
# =========================================================================

import os
import asyncio
import aiohttp
import decimal
from datetime import datetime
from decimal import Decimal

from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes,
    ConversationHandler, CallbackQueryHandler, filters
)
from dotenv import load_dotenv

load_dotenv()

# --- Конфигурация ---
BOT_TOKEN = os.getenv("BOT_TOKEN") # Единственный секрет, который нам нужен

# --- Глобальные переменные и настройки ---
user_settings = {} # Словарь для хранения настроек каждого пользователя (chat_id)
# Кэш для данных, чтобы не запрашивать API при каждом клике в меню
api_data_cache = {
    "last_update": None,
    "data": []
} 
CACHE_LIFETIME_SECONDS = 60 # Как часто обновлять данные с бирж (1 минута)


# --- "Умные" дефолты для фильтров новых пользователей ---
def get_default_settings():
    return {
        'notifications_on': True,
        'exchanges': ['Bybit', 'MEXC', 'Binance', 'OKX', 'KuCoin'], # Основные биржи по умолчанию
        'funding_threshold': Decimal('0.005'),  # 0.5%
        'volume_threshold_usdt': Decimal('1000000'), # 1 млн USDT
        'time_window_minutes': 60, # Уведомлять за час до фандинга
        # 'volatility_threshold_percent': Decimal('2.0'), # Пока не реализуем в этой версии
        # 'watchlist': [], # Пока не реализуем в этой версии
    }

# --- Хелпер для инициализации настроек чата ---
def ensure_user_settings(chat_id: int):
    """Проверяет и при необходимости создает дефолтные настройки для пользователя."""
    if chat_id not in user_settings:
        user_settings[chat_id] = get_default_settings()
    # Проверка на случай добавления новых ключей в будущем
    for key, value in get_default_settings().items():
        user_settings[chat_id].setdefault(key, value)


# =================================================================
# ===================== МОДУЛЬ СБОРА ДАННЫХ (API) =====================
# =================================================================

# --- Коннектор для Bybit ---
async def get_bybit_data():
    """Получает данные по фандингу с Bybit через публичный API."""
    bybit_url = "https://api.bybit.com/v5/market/tickers?category=linear"
    results = []
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(bybit_url) as response:
                response.raise_for_status()
                data = await response.json()
                if data.get("retCode") == 0 and data.get("result", {}).get("list"):
                    for t in data["result"]["list"]:
                        try:
                            # Мы берем только те данные, что нам нужны
                            results.append({
                                'exchange': 'Bybit',
                                'symbol': t.get("symbol"),
                                'rate': Decimal(t.get("fundingRate")),
                                'next_funding_time': int(t.get("nextFundingTime")),
                                'volume_24h_usdt': Decimal(t.get("turnover24h")),
                                # В этом эндпоинте нет лимитов, нужен будет доп. запрос
                                'max_order_value_usdt': Decimal('0'), # Заглушка
                                'trade_url': f'https://www.bybit.com/trade/usdt/{t.get("symbol")}'
                            })
                        except (TypeError, ValueError, decimal.InvalidOperation):
                            continue # Пропускаем пару, если данные некорректны
    except Exception as e:
        print(f"[API_ERROR] Bybit: {e}")
    return results

# --- Коннектор для MEXC ---
async def get_mexc_data():
    """Получает данные по фандингу с MEXC через публичный API."""
    mexc_url = "https://contract.mexc.com/api/v1/contract/detail"
    results = []
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(mexc_url) as response:
                response.raise_for_status()
                data = await response.json()
                if data.get("success") and data.get("data"):
                    for t in data["data"]:
                         if t.get("quoteCoin") != "USDT" or t.get("state") != "SHOW":
                            continue
                         try:
                            symbol = t.get("symbol").replace("_", "")
                            results.append({
                                'exchange': 'MEXC',
                                'symbol': symbol,
                                'rate': Decimal(str(t.get("fundingRate"))),
                                'next_funding_time': int(t.get("nextSettleTime")),
                                'volume_24h_usdt': Decimal(str(t.get("volume24"))),
                                'max_order_value_usdt': Decimal(str(t.get("maxVol"))), # MEXC отдает лимит в базовом активе
                                'trade_url': f'https://futures.mexc.com/exchange/{t.get("symbol")}'
                            })
                         except (TypeError, ValueError, decimal.InvalidOperation):
                            continue
    except Exception as e:
        print(f"[API_ERROR] MEXC: {e}")
    return results

# --- Главная функция-агрегатор ---
async def fetch_all_data(force_update=False):
    """Собирает данные со всех бирж, используя кэш."""
    now = datetime.now().timestamp()
    if not force_update and api_data_cache["last_update"] and (now - api_data_cache["last_update"] < CACHE_LIFETIME_SECONDS):
        print("[Cache] Using cached API data.")
        return api_data_cache["data"]

    print("[API] Fetching new data from all exchanges...")
    # Создаем задачи для асинхронного выполнения
    tasks = [
        get_bybit_data(),
        get_mexc_data(),
        # Сюда будем добавлять вызовы для Binance, OKX и т.д.
        # asyncio.create_task(get_binance_data()),
    ]
    
    # Собираем результаты
    results_from_tasks = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Разворачиваем список списков в один плоский список
    all_data = []
    for res in results_from_tasks:
        if isinstance(res, list):
            all_data.extend(res)
        elif isinstance(res, Exception):
            print(f"[API_GATHER_ERROR] {res}")

    # Сохраняем в кэш
    api_data_cache["data"] = all_data
    api_data_cache["last_update"] = now
    print(f"[API] Fetched {len(all_data)} total pairs.")
    return all_data


# =================================================================
# ================== ПОЛЬЗОВАТЕЛЬСКИЙ ИНТЕРФЕЙС (UI) ==================
# =================================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start. Приветствует и показывает главное меню."""
    chat_id = update.effective_chat.id
    ensure_user_settings(chat_id)
    
    main_menu_keyboard = [
        ["🔥 Топ-ставки сейчас"],
        ["🔔 Настроить фильтры", "ℹ️ Мои настройки"]
    ]
    reply_markup = ReplyKeyboardMarkup(main_menu_keyboard, resize_keyboard=True)
    
    await update.message.reply_text(
        "Добро пожаловать в RateHunter 2.0!\n\n"
        "Я помогу вам найти лучшие ставки финансирования на криптобиржах.\n"
        "Используйте меню для навигации.",
        reply_markup=reply_markup
    )

async def show_top_rates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает топ-5 пар с самым высоким фандингом (первый уровень)."""
    chat_id = update.effective_chat.id
    ensure_user_settings(chat_id)
    settings = user_settings[chat_id]
    
    await update.message.reply_text("🔄 Ищу лучшие ставки, пожалуйста, подождите...")

    all_data = await fetch_all_data()

    # Фильтруем данные согласно настройкам пользователя
    user_filtered_data = [
        item for item in all_data
        if item['exchange'] in settings['exchanges']
        and abs(item['rate']) >= settings['funding_threshold']
        and item['volume_24h_usdt'] >= settings['volume_threshold_usdt']
    ]

    # Сортируем по модулю ставки фандинга
    user_filtered_data.sort(key=lambda x: abs(x['rate']), reverse=True)
    
    top_5 = user_filtered_data[:5]

    if not top_5:
        await update.message.reply_text("😔 Не найдено пар, соответствующих вашим фильтрам. Попробуйте ослабить их в настройках.")
        return

    # Формируем сообщение
    message_text = f"🔥 **ТОП-5 ближайших фандингов > {settings['funding_threshold']*100:.2f}%**\n\n"
    buttons = []
    
    now_ts = datetime.now().timestamp()
    
    for item in top_5:
        symbol_only = item['symbol'].replace("USDT", "")
        # Расчет времени до выплаты
        time_left_seconds = (item['next_funding_time'] / 1000) - now_ts
        hours, remainder = divmod(time_left_seconds, 3600)
        minutes, _ = divmod(remainder, 60)
        time_str = f"{int(hours):02d}:{int(minutes):02d}" if time_left_seconds > 0 else "00:00"

        # Форматирование строки
        direction_emoji = "🟢" if item['rate'] > 0 else "🔴" # 🟢 = лонги платят, 🔴 = шорты платят
        rate_str = f"{item['rate'] * 100:+.2f}%"
        
        message_text += f"{direction_emoji} *{symbol_only}* `{rate_str}` до `{time_str}` [{item['exchange']}]\n"
        buttons.append(InlineKeyboardButton(symbol_only, callback_data=f"drill_{item['symbol']}"))

    # Группируем кнопки по 3 в ряд
    keyboard = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    
    await update.message.reply_text(
        message_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown',
        disable_web_page_preview=True
    )
    
async def drill_down_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает нажатие на кнопку с тикером (второй уровень)."""
    query = update.callback_query
    await query.answer()

    symbol_to_show = query.data.split('_')[1]
    
    # Используем закешированные данные
    all_data = api_data_cache.get("data", [])
    if not all_data:
        await query.edit_message_text("⏳ Данные устарели, обновляю...")
        all_data = await fetch_all_data(force_update=True)

    # Находим все пары с этим символом на разных биржах
    symbol_specific_data = [item for item in all_data if item['symbol'] == symbol_to_show]
    symbol_specific_data.sort(key=lambda x: abs(x['rate']), reverse=True)

    if not symbol_specific_data:
        await query.edit_message_text(f"Не удалось найти детальную информацию по {symbol_to_show}")
        return

    # Формируем детальное сообщение
    symbol_only = symbol_to_show.replace("USDT", "")
    message_text = f"💎 **Детали по {symbol_only}**\n\n"
    
    now_ts = datetime.now().timestamp()

    for item in symbol_specific_data:
        time_left_seconds = (item['next_funding_time'] / 1000) - now_ts
        hours, remainder = divmod(time_left_seconds, 3600)
        minutes, _ = divmod(remainder, 60)
        time_str = f"{int(hours):02d}:{int(minutes):02d}" if time_left_seconds > 0 else "00:00"
        
        direction_emoji = "🟢" if item['rate'] > 0 else "🔴"
        rate_str = f"{item['rate'] * 100:+.2f}%"
        
        message_text += (f"{direction_emoji} `{rate_str}` до `{time_str}` "
                         f"[{item['exchange']}]({item['trade_url']})\n")

    # TODO: Добавить кнопки навигации для переключения между топ-5 тикерами
    keyboard = [[InlineKeyboardButton("⬅️ Назад к топу", callback_data="back_to_top")]]
    
    await query.edit_message_text(
        text=message_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='Markdown',
        disable_web_page_preview=True
    )

# Эта функция будет вызываться по кнопке "Назад"
async def back_to_top_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Возвращает пользователя к главному списку топ-5."""
    query = update.callback_query
    await query.answer()
    # Просто заново вызываем функцию, которая строит главный экран
    # Для этого ее нужно будет немного переделать, чтобы она могла редактировать сообщение
    await query.message.delete() # Пока просто удаляем
    await show_top_rates(query.message, context)


# =================================================================
# ======================== ГЛАВНЫЙ ЦИКЛ БОТА ========================
# =================================================================

async def background_scanner(app: ApplicationBuilder):
    """Фоновый процесс, который ищет подходящие пары и уведомляет пользователей."""
    print(" Background scanner started ".center(50, "="))
    while True:
        await asyncio.sleep(60) # Проверяем раз в минуту
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Running background scan...")
        
        all_data = await fetch_all_data()
        
        # TODO: Реализовать логику уведомлений
        # 1. Пройти по всем пользователям в user_settings
        # 2. Если у пользователя включены уведомления
        # 3. Отфильтровать all_data по его персональным настройкам
        # 4. Сравнить с предыдущим результатом, чтобы не слать дубли
        # 5. Если есть новые подходящие пары - отправить уведомление
        pass

# =================================================================
# ========================== ЗАПУСК БОТА ==========================
# =================================================================

if __name__ == "__main__":
    print("Initializing bot...")
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # --- Регистрация обработчиков ---
    application.add_handler(CommandHandler("start", start))
    
    # Обработчики для главного меню
    application.add_handler(MessageHandler(filters.Regex("^🔥 Топ-ставки сейчас$"), show_top_rates))
    # application.add_handler(MessageHandler(filters.Regex("^🔔 Настроить фильтры$"), show_filters_menu))
    # application.add_handler(MessageHandler(filters.Regex("^ℹ️ Мои настройки$"), show_my_settings))

    # Обработчик для "проваливания" в детали
    application.add_handler(CallbackQueryHandler(drill_down_callback, pattern="^drill_"))
    application.add_handler(CallbackQueryHandler(back_to_top_callback, pattern="^back_to_top$"))
    
    # --- Запуск фоновых задач ---
    async def post_init(app: ApplicationBuilder):
        print("Running post_init tasks...")
        # Запускаем фоновый сканер
        asyncio.create_task(background_scanner(app))
        print("Background scanner task created.")

    application.post_init = post_init

    print("Starting bot polling...")
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception as e:
        print(f"\nBot polling stopped due to error: {e}")
    finally:
        print("\nBot shutdown.")
