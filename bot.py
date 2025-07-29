# =========================================================================
# ===================== RateHunter 2.0 - Alpha v0.2.6 ===================
# =========================================================================
# Исправления в этой версии:
# - ДОБАВЛЕНО: Полная диагностика MEXC API
# - ИСПРАВЛЕНО: Переписана функция получения данных с MEXC
# - ДОБАВЛЕНО: Тестирование подключения к API
# =========================================================================

import os
import asyncio
import aiohttp
import decimal
import json
import time
import hmac
import hashlib
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes,
    ConversationHandler, CallbackQueryHandler, filters
)
from dotenv import load_dotenv

load_dotenv()

# --- Конфигурация ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
MSK_TIMEZONE = timezone(timedelta(hours=3))

# --- Глобальные переменные и настройки ---
user_settings = {}
api_data_cache = {"last_update": None, "data": []}
CACHE_LIFETIME_SECONDS = 60
ALL_AVAILABLE_EXCHANGES = ['Bybit', 'MEXC', 'Binance', 'OKX', 'KuCoin', 'Gate.io', 'HTX', 'Bitget']

# --- Состояния для ConversationHandler ---
SET_FUNDING_THRESHOLD, SET_VOLUME_THRESHOLD = range(2)

def get_default_settings():
    return {
        'notifications_on': True, 'exchanges': ['Bybit', 'MEXC', 'Binance', 'OKX', 'KuCoin'],
        'funding_threshold': Decimal('0.005'), 'volume_threshold_usdt': Decimal('1000000'),
    }

def ensure_user_settings(chat_id: int):
    if chat_id not in user_settings: user_settings[chat_id] = get_default_settings()
    for key, value in get_default_settings().items():
        user_settings[chat_id].setdefault(key, value)


# =================================================================
# =================== БЫСТРАЯ ДИАГНОСТИКА MEXC ==================
# =================================================================

async def quick_mexc_debug():
    """
    Быстрая диагностика структуры данных MEXC
    """
    print("\n" + "🔍" * 20 + " БЫСТРАЯ ДИАГНОСТИКА MEXC " + "🔍" * 20)
    
    try:
        async with aiohttp.ClientSession() as session:
            contracts_url = "https://contract.mexc.com/api/v1/contract/detail"
            
            async with session.get(contracts_url, timeout=15) as response:
                if response.status != 200:
                    print(f"❌ Ошибка HTTP: {response.status}")
                    return
                
                data = await response.json()
                
                if not data.get("success"):
                    print(f"❌ API ошибка: {data}")
                    return
                
                contracts = data.get("data", [])
                print(f"📊 Всего контрактов: {len(contracts)}")
                
                if contracts:
                    # Анализируем первый контракт
                    first_contract = contracts[0]
                    print(f"\n📋 Структура первого контракта:")
                    for key, value in first_contract.items():
                        print(f"  {key}: {value} ({type(value).__name__})")
                    
                    # Ищем USDT контракты разными способами
                    print(f"\n🔍 Поиск USDT контрактов:")
                    
                    # Метод 1: по quoteCoin
                    usdt_by_quote = [c for c in contracts if c.get("quoteCoin") == "USDT"]
                    print(f"  По quoteCoin='USDT': {len(usdt_by_quote)}")
                    
                    # Метод 2: по symbol
                    usdt_by_symbol = [c for c in contracts if str(c.get("symbol", "")).endswith("USDT")]
                    print(f"  По symbol заканчивается на 'USDT': {len(usdt_by_symbol)}")
                    
                    # Метод 3: по другим полям
                    for field in ["quoteCurrency", "quote_coin", "baseCoin", "base_coin"]:
                        usdt_by_field = [c for c in contracts if c.get(field) == "USDT"]
                        if usdt_by_field:
                            print(f"  По {field}='USDT': {len(usdt_by_field)}")
                    
                    # Показываем уникальные значения важных полей
                    quote_coins = set(c.get("quoteCoin") for c in contracts if c.get("quoteCoin"))
                    states = set(c.get("state") for c in contracts if c.get("state"))
                    
                    print(f"\n📈 Уникальные quoteCoin (первые 10): {sorted(list(quote_coins))[:10]}")
                    print(f"📈 Уникальные state: {sorted(list(states))}")
                    
                    # Показываем примеры USDT контрактов
                    if usdt_by_symbol:
                        print(f"\n💰 Примеры USDT контрактов:")
                        for i, contract in enumerate(usdt_by_symbol[:3]):
                            symbol = contract.get("symbol")
                            state = contract.get("state")
                            quote = contract.get("quoteCoin")
                            print(f"  {i+1}. {symbol} | state: {state} | quoteCoin: {quote}")
                
    except Exception as e:
        print(f"❌ Ошибка диагностики: {e}")
    
    print("🔍" * 60 + "\n")


# =================================================================
# ===================== ДИАГНОСТИКА MEXC API =====================
# =================================================================

async def test_mexc_connection():
    """
    Полная диагностика подключения к MEXC API
    """
    print("\n" + "="*60)
    print("🔍 ДИАГНОСТИКА MEXC API")
    print("="*60)
    
    try:
        async with aiohttp.ClientSession() as session:
            # 1. Тест базового подключения
            print("1️⃣ Тестирование базового подключения...")
            try:
                ping_url = "https://contract.mexc.com/api/v1/contract/ping"
                async with session.get(ping_url, timeout=10) as response:
                    print(f"   Status: {response.status}")
                    if response.status == 200:
                        data = await response.json()
                        print(f"   Response: {data}")
                        print("   ✅ Базовое подключение работает")
                    else:
                        print("   ❌ Проблема с базовым подключением")
            except Exception as e:
                print(f"   ❌ Ошибка подключения: {e}")
            
            # 2. Тест времени сервера
            print("\n2️⃣ Получение времени сервера...")
            try:
                time_url = "https://contract.mexc.com/api/v1/contract/server_time"
                async with session.get(time_url, timeout=10) as response:
                    print(f"   Status: {response.status}")
                    if response.status == 200:
                        data = await response.json()
                        print(f"   Server Time: {data}")
                        if data.get('success'):
                            server_time = data.get('data')
                            local_time = int(time.time() * 1000)
                            diff = abs(server_time - local_time) if server_time else 0
                            print(f"   Time Diff: {diff}ms")
                            print("   ✅ Время сервера получено" if diff < 5000 else "   ⚠️ Большая разница во времени")
                        else:
                            print("   ❌ Неуспешный ответ сервера")
            except Exception as e:
                print(f"   ❌ Ошибка получения времени: {e}")
            
            # 3. Тест получения списка контрактов
            print("\n3️⃣ Получение списка контрактов...")
            try:
                contracts_url = "https://contract.mexc.com/api/v1/contract/detail"
                async with session.get(contracts_url, timeout=15) as response:
                    print(f"   Status: {response.status}")
                    if response.status == 200:
                        data = await response.json()
                        if data.get('success'):
                            contracts = data.get('data', [])
                            usdt_contracts = [c for c in contracts if c.get('quoteCoin') == 'USDT']
                            print(f"   Всего контрактов: {len(contracts)}")
                            print(f"   USDT контрактов: {len(usdt_contracts)}")
                            
                            if usdt_contracts:
                                sample = usdt_contracts[0]
                                print(f"   Пример контракта: {sample.get('symbol')} - {sample.get('state')}")
                                print("   ✅ Список контрактов получен")
                            else:
                                print("   ⚠️ USDT контракты не найдены")
                        else:
                            print(f"   ❌ API вернул ошибку: {data}")
                    else:
                        print("   ❌ Ошибка HTTP запроса")
            except Exception as e:
                print(f"   ❌ Ошибка получения контрактов: {e}")
            
            # 4. Тест получения фандинг ставки для конкретного символа
            print("\n4️⃣ Тест получения фандинг ставки...")
            try:
                test_symbol = "BTC_USDT"  # Тестовый символ
                funding_url = f"https://contract.mexc.com/api/v1/contract/funding_rate/{test_symbol}"
                async with session.get(funding_url, timeout=10) as response:
                    print(f"   Status для {test_symbol}: {response.status}")
                    if response.status == 200:
                        data = await response.json()
                        print(f"   Response: {data}")
                        if data.get('success') and data.get('data'):
                            funding_data = data['data']
                            print(f"   Funding Rate: {funding_data.get('fundingRate')}")
                            print(f"   Next Funding: {funding_data.get('nextSettleTime')}")
                            print("   ✅ Фандинг ставка получена")
                        else:
                            print("   ❌ Нет данных по фандингу")
                    else:
                        print("   ❌ Ошибка получения фандинг ставки")
            except Exception as e:
                print(f"   ❌ Ошибка фандинг запроса: {e}")
            
            # 5. Тест приватного API (если ключи есть)
            print("\n5️⃣ Тест приватного API...")
            api_key = os.getenv("MEXC_API_KEY")
            secret_key = os.getenv("MEXC_API_SECRET")
            
            if api_key and secret_key:
                print(f"   API Key найден: {api_key[:8]}...")
                print(f"   Secret Key найден: {secret_key[:8]}...")
                
                try:
                    timestamp = str(int(time.time() * 1000))
                    query_string = f"timestamp={timestamp}"
                    signature = hmac.new(
                        secret_key.encode('utf-8'), 
                        query_string.encode('utf-8'), 
                        hashlib.sha256
                    ).hexdigest()
                    
                    headers = {
                        'X-MEXC-APIKEY': api_key,
                        'Content-Type': 'application/json',
                    }
                    
                    private_url = f"https://contract.mexc.com/api/v1/private/account/assets?timestamp={timestamp}&signature={signature}"
                    async with session.get(private_url, headers=headers, timeout=10) as response:
                        print(f"   Private API Status: {response.status}")
                        
                        if response.status == 401:
                            print("   ❌ Неверные API ключи")
                        elif response.status == 403:
                            print("   ❌ Недостаточно прав у API ключей")
                        elif response.status == 200:
                            data = await response.json()
                            print(f"   Private API Response: {data}")
                            print("   ✅ Приватный API работает")
                        else:
                            text = await response.text()
                            print(f"   ❌ Неожиданный статус: {text}")
                            
                except Exception as e:
                    print(f"   ❌ Ошибка приватного API: {e}")
            else:
                print("   ⚠️ API ключи не найдены в переменных окружения")
                print("   Переменные: MEXC_API_KEY, MEXC_API_SECRET")
                
    except Exception as e:
        print(f"❌ Критическая ошибка диагностики: {e}")
    
    print("="*60)
    print("🏁 ДИАГНОСТИКА ЗАВЕРШЕНА")
    print("="*60 + "\n")


# =================================================================
# =================== ОБНОВЛЕННЫЙ МОДУЛЬ MEXC ====================
# =================================================================

async def get_mexc_data():
    """
    Получение данных с MEXC через публичный API
    """
    print("[DEBUG] Начинаем получение данных с MEXC...")
    results = []
    
    try:
        async with aiohttp.ClientSession() as session:
            # 1. Получаем список всех контрактов
            print("[DEBUG] MEXC: Получаем список контрактов...")
            contracts_url = "https://contract.mexc.com/api/v1/contract/detail"
            
            async with session.get(contracts_url, timeout=15) as response:
                if response.status != 200:
                    print(f"[API_ERROR] MEXC: Ошибка получения контрактов, статус: {response.status}")
                    return []
                
                contracts_data = await response.json()
                
                if not contracts_data.get("success", False):
                    print(f"[API_ERROR] MEXC: API вернул ошибку: {contracts_data}")
                    return []
                
                # Анализируем структуру данных
                all_contracts = contracts_data.get("data", [])
                print(f"[DEBUG] MEXC: Всего контрактов получено: {len(all_contracts)}")
                
                if all_contracts:
                    # Показываем примеры первых 3 контрактов для анализа
                    print("[DEBUG] MEXC: Примеры контрактов:")
                    for i, contract in enumerate(all_contracts[:3]):
                        print(f"  Контракт {i+1}: {contract}")
                    
                    # Анализируем уникальные значения полей
                    quote_coins = set()
                    states = set()
                    symbols_exist = 0
                    
                    for contract in all_contracts:
                        quote_coin = contract.get("quoteCoin")
                        state = contract.get("state")
                        symbol = contract.get("symbol")
                        
                        if quote_coin:
                            quote_coins.add(quote_coin)
                        if state:
                            states.add(state)
                        if symbol:
                            symbols_exist += 1
                    
                    print(f"[DEBUG] MEXC: Найденные quoteCoin: {sorted(quote_coins)}")
                    print(f"[DEBUG] MEXC: Найденные state: {sorted(states)}")
                    print(f"[DEBUG] MEXC: Контрактов с symbol: {symbols_exist}")
                
                # Пробуем разные варианты фильтрации
                usdt_contracts = []
                
                # Вариант 1: Оригинальный фильтр
                usdt_contracts_v1 = [
                    contract for contract in all_contracts
                    if (contract.get("quoteCoin") == "USDT" and 
                        contract.get("state") == "SHOW" and
                        contract.get("symbol"))
                ]
                
                # Вариант 2: Только по quoteCoin
                usdt_contracts_v2 = [
                    contract for contract in all_contracts
                    if contract.get("quoteCoin") == "USDT"
                ]
                
                # Вариант 3: Альтернативные названия полей
                usdt_contracts_v3 = [
                    contract for contract in all_contracts
                    if (contract.get("quote_coin") == "USDT" or 
                        contract.get("quoteCurrency") == "USDT" or
                        str(contract.get("symbol", "")).endswith("USDT"))
                ]
                
                # Вариант 4: По окончанию символа на USDT
                usdt_contracts_v4 = [
                    contract for contract in all_contracts
                    if str(contract.get("symbol", "")).endswith("USDT")
                ]
                
                print(f"[DEBUG] MEXC: Фильтр v1 (quoteCoin=USDT, state=SHOW): {len(usdt_contracts_v1)}")
                print(f"[DEBUG] MEXC: Фильтр v2 (только quoteCoin=USDT): {len(usdt_contracts_v2)}")
                print(f"[DEBUG] MEXC: Фильтр v3 (альтернативные поля): {len(usdt_contracts_v3)}")
                print(f"[DEBUG] MEXC: Фильтр v4 (symbol ends with USDT): {len(usdt_contracts_v4)}")
                
                # Выбираем лучший вариант
                if usdt_contracts_v1:
                    usdt_contracts = usdt_contracts_v1
                    print("[DEBUG] MEXC: Используем фильтр v1")
                elif usdt_contracts_v2:
                    usdt_contracts = usdt_contracts_v2
                    print("[DEBUG] MEXC: Используем фильтр v2")
                elif usdt_contracts_v4:
                    usdt_contracts = usdt_contracts_v4
                    print("[DEBUG] MEXC: Используем фильтр v4")
                else:
                    usdt_contracts = usdt_contracts_v3
                    print("[DEBUG] MEXC: Используем фильтр v3")
                
                print(f"[DEBUG] MEXC: Итого выбрано USDT контрактов: {len(usdt_contracts)}")
                
                if not usdt_contracts:
                    print("[API_ERROR] MEXC: Не найдено USDT контрактов ни одним фильтром")
                    return []
            
            # 2. Получаем данные для каждого контракта (ограничиваем количество)
            limited_contracts = usdt_contracts[:30]  # Берем первые 30 для тестирования
            print(f"[DEBUG] MEXC: Обрабатываем {len(limited_contracts)} контрактов...")
            
            successful_requests = 0
            failed_requests = 0
            
            for i, contract in enumerate(limited_contracts):
                symbol = contract.get("symbol")
                if not symbol:
                    continue
                
                try:
                    # Получаем фандинг ставку
                    funding_url = f"https://contract.mexc.com/api/v1/contract/funding_rate/{symbol}"
                    
                    async with session.get(funding_url, timeout=8) as funding_response:
                        if funding_response.status == 200:
                            funding_data = await funding_response.json()
                            
                            if funding_data.get("success") and funding_data.get("data"):
                                funding_info = funding_data["data"]
                                funding_rate = funding_info.get("fundingRate")
                                next_funding_time = funding_info.get("nextSettleTime")
                                
                                if funding_rate is not None and next_funding_time is not None:
                                    # Получаем объем торгов
                                    volume_24h = Decimal('0')
                                    try:
                                        ticker_url = f"https://contract.mexc.com/api/v1/contract/ticker/{symbol}"
                                        async with session.get(ticker_url, timeout=5) as ticker_response:
                                            if ticker_response.status == 200:
                                                ticker_data = await ticker_response.json()
                                                if ticker_data.get("success") and ticker_data.get("data"):
                                                    volume_24h = Decimal(str(ticker_data["data"].get("volume24", '0')))
                                    except Exception as e:
                                        print(f"[DEBUG] MEXC: Не удалось получить объем для {symbol}: {e}")
                                    
                                    # Формируем результат
                                    symbol_clean = symbol.replace("_", "")
                                    
                                    results.append({
                                        'exchange': 'MEXC',
                                        'symbol': symbol_clean,
                                        'rate': Decimal(str(funding_rate)),
                                        'next_funding_time': int(next_funding_time),
                                        'volume_24h_usdt': volume_24h,
                                        'max_order_value_usdt': Decimal(str(contract.get("maxVol", '0'))),
                                        'trade_url': f'https://futures.mexc.com/exchange/{symbol}'
                                    })
                                    
                                    successful_requests += 1
                                    
                                    if successful_requests % 10 == 0:
                                        print(f"[DEBUG] MEXC: Обработано {successful_requests} инструментов...")
                                else:
                                    failed_requests += 1
                            else:
                                failed_requests += 1
                        else:
                            failed_requests += 1
                            
                except Exception as e:
                    failed_requests += 1
                    if failed_requests <= 5:  # Показываем только первые 5 ошибок
                        print(f"[API_ERROR] MEXC: Ошибка для {symbol}: {e}")
                
                # Небольшая задержка между запросами
                if i % 5 == 0 and i > 0:
                    await asyncio.sleep(0.2)
                        
    except Exception as e:
        print(f"[API_ERROR] MEXC: Критическая ошибка: {e}")
        return []
    
    print(f"[DEBUG] MEXC: Завершено. Успешно: {successful_requests}, Ошибок: {failed_requests}")
    print(f"[DEBUG] MEXC: Получено {len(results)} инструментов с данными")
    
    return results

async def get_bybit_data():
    bybit_url = "https://api.bybit.com/v5/market/tickers?category=linear"
    instrument_url = "https://api.bybit.com/v5/market/instruments-info?category=linear"
    results = []
    try:
        async with aiohttp.ClientSession() as session:
            # Получаем лимиты по ордерам
            limits_data = {}
            async with session.get(instrument_url) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get("retCode") == 0 and data.get("result", {}).get("list"):
                        for inst in data["result"]["list"]:
                            limits_data[inst['symbol']] = inst.get('lotSizeFilter', {}).get('maxOrderQty', '0')

            # Получаем основные данные по тикерам
            async with session.get(bybit_url) as response:
                response.raise_for_status()
                data = await response.json()
                if data.get("retCode") == 0 and data.get("result", {}).get("list"):
                    for t in data["result"]["list"]:
                        try:
                            results.append({
                                'exchange': 'Bybit', 'symbol': t.get("symbol"),
                                'rate': Decimal(t.get("fundingRate")), 'next_funding_time': int(t.get("nextFundingTime")),
                                'volume_24h_usdt': Decimal(t.get("turnover24h")),
                                'max_order_value_usdt': Decimal(limits_data.get(t.get("symbol"), '0')),
                                'trade_url': f'https://www.bybit.com/trade/usdt/{t.get("symbol")}'
                            })
                        except (TypeError, ValueError, decimal.InvalidOperation): continue
    except Exception as e: 
        print(f"[API_ERROR] Bybit: {e}")
    
    print(f"[DEBUG] Bybit: Получено {len(results)} инструментов")
    return results

async def fetch_all_data(force_update=False):
    now = datetime.now().timestamp()
    if not force_update and api_data_cache["last_update"] and (now - api_data_cache["last_update"] < CACHE_LIFETIME_SECONDS):
        return api_data_cache["data"]
    
    print("\n🔄 Начинаем сбор данных с бирж...")
    
    # Запускаем сбор данных параллельно
    tasks = [get_bybit_data(), get_mexc_data()]
    results_from_tasks = await asyncio.gather(*tasks, return_exceptions=True)
    all_data = []
    
    for i, res in enumerate(results_from_tasks):
        exchange_name = ['Bybit', 'MEXC'][i]
        if isinstance(res, list): 
            all_data.extend(res)
            print(f"✅ {exchange_name}: {len(res)} инструментов")
        elif isinstance(res, Exception): 
            print(f"❌ {exchange_name}: Ошибка - {res}")

    api_data_cache["data"], api_data_cache["last_update"] = all_data, now
    print(f"🏁 Сбор завершен. Всего получено: {len(all_data)} инструментов\n")
    return all_data


# =================================================================
# ================== ПОЛЬЗОВАТЕЛЬСКИЙ ИНТЕРФЕЙС ==================
# =================================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user_settings(update.effective_chat.id)
    main_menu_keyboard = [
        ["🔥 Топ-ставки сейчас"], 
        ["🔔 Настроить фильтры", "ℹ️ Мои настройки"],
        ["🔧 Диагностика MEXC", "🔍 Быстрая диагностика"]
    ]
    reply_markup = ReplyKeyboardMarkup(main_menu_keyboard, resize_keyboard=True)
    await update.message.reply_text("Добро пожаловать в RateHunter 2.0!\n\n🆕 Добавлена диагностика MEXC API", reply_markup=reply_markup)

async def run_quick_diagnostics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запуск быстрой диагностики MEXC"""
    await update.message.reply_text("🔍 Запускаю быструю диагностику MEXC...\nПроверьте логи для подробностей.")
    
    # Запускаем быструю диагностику
    await quick_mexc_debug()
    
    await update.message.reply_text("✅ Быстрая диагностика завершена! Проверьте логи выше.")

async def run_diagnostics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запуск полной диагностики MEXC"""
    await update.message.reply_text("🔍 Запускаю полную диагностику MEXC API...\nПроверьте логи сервера для подробной информации.")
    
    # Запускаем диагностику
    await test_mexc_connection()
    
    await update.message.reply_text("✅ Полная диагностика завершена! Проверьте логи выше для получения подробной информации.")

async def show_top_rates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ensure_user_settings(chat_id)
    settings = user_settings[chat_id]

    if update.callback_query:
        message_to_edit = update.callback_query.message
        await message_to_edit.edit_text("🔄 Ищу лучшие ставки по вашим фильтрам...")
    else:
        message_to_edit = await update.message.reply_text("🔄 Ищу...")

    all_data = await fetch_all_data()
    if not all_data:
        await message_to_edit.edit_text("😔 Не удалось получить данные с бирж. Попробуйте позже.")
        return

    user_filtered_data = [
        item for item in all_data
        if item['exchange'] in settings['exchanges'] and abs(item['rate']) >= settings['funding_threshold']
        and item['volume_24h_usdt'] >= settings['volume_threshold_usdt']
    ]
    user_filtered_data.sort(key=lambda x: abs(x['rate']), reverse=True)
    top_5 = user_filtered_data[:5]
    if not top_5:
        await message_to_edit.edit_text("😔 Не найдено пар, соответствующих вашим фильтрам.")
        return

    message_text = f"🔥 **ТОП-5 фандингов > {settings['funding_threshold']*100:.2f}%**\n\n"
    buttons = []
    for item in top_5:
        symbol_only = item['symbol'].replace("USDT", "")
        funding_dt = datetime.fromtimestamp(item['next_funding_time'] / 1000, tz=MSK_TIMEZONE)
        time_str = funding_dt.strftime('%H:%M МСК')
        direction_text = "🟢 LONG" if item['rate'] < 0 else "🔴 SHORT"
        rate_str = f"{item['rate'] * 100:+.2f}%"
        message_text += f"{direction_text} *{symbol_only}* `{rate_str}` в `{time_str}` [{item['exchange']}]\n"
        buttons.append(InlineKeyboardButton(symbol_only, callback_data=f"drill_{item['symbol']}"))

    keyboard = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    await message_to_edit.edit_text(
        message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown', disable_web_page_preview=True
    )

async def drill_down_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    symbol_to_show = query.data.split('_')[1]

    all_data = api_data_cache.get("data", [])
    if not all_data:
        await query.edit_message_text("🔄 Обновляю данные...")
        all_data = await fetch_all_data(force_update=True)

    symbol_specific_data = [item for item in all_data if item['symbol'] == symbol_to_show]
    symbol_specific_data.sort(key=lambda x: abs(x['rate']), reverse=True)
    symbol_only = symbol_to_show.replace("USDT", "")
    message_text = f"💎 **Детали по {symbol_only}**\n\n"
    for item in symbol_specific_data:
        funding_dt = datetime.fromtimestamp(item['next_funding_time'] / 1000, tz=MSK_TIMEZONE)
        time_str = funding_dt.strftime('%H:%M МСК')
        direction_text = "🟢 ЛОНГ" if item['rate'] < 0 else "🔴 ШОРТ"
        rate_str = f"{item['rate'] * 100:+.2f}%"
        message_text += f"{direction_text} `{rate_str}` в `{time_str}` [{item['exchange']}]({item['trade_url']})\n"

        max_pos = item.get('max_order_value_usdt', Decimal('0'))
        if max_pos > 0:
            message_text += f"  *Макс. ордер:* `{max_pos:,.0f}`\n"

    keyboard = [[InlineKeyboardButton("⬅️ Назад к топу", callback_data="back_to_top")]]
    await query.edit_message_text(
        text=message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown', disable_web_page_preview=True
    )

async def back_to_top_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
    await show_top_rates(update, context)

# --- Остальные функции интерфейса (без изменений) ---

async def send_filters_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ensure_user_settings(chat_id)
    settings = user_settings[chat_id]
    notif_emoji = "✅" if settings['notifications_on'] else "🔴"
    vol = settings['volume_threshold_usdt']
    vol_str = f"{vol / 1_000_000:.1f}M" if vol >= 1_000_000 else f"{vol / 1_000:.0f}K"
    message_text = "🔔 **Настройки фильтров и уведомлений**"
    keyboard = [
        [InlineKeyboardButton("🏦 Биржи", callback_data="filters_exchanges")],
        [InlineKeyboardButton(f"🔔 Ставка: > {settings['funding_threshold']*100:.2f}%", callback_data="filters_funding")],
        [InlineKeyboardButton(f"💧 Объем: > {vol_str}", callback_data="filters_volume")],
        [InlineKeyboardButton(f"{notif_emoji} Уведомления: {'ВКЛ' if settings['notifications_on'] else 'ВЫКЛ'}", callback_data="filters_toggle_notif")],
        [InlineKeyboardButton("❌ Закрыть", callback_data="filters_close")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query:
        await update.callback_query.edit_message_text(message_text, reply_markup=reply_markup, parse_mode='Markdown')
    else:
        await update.message.reply_text(message_text, reply_markup=reply_markup, parse_mode='Markdown')

async def filters_menu_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_filters_menu(update, context)

async def filters_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    action = query.data.split('_', 1)[1]
    if action == "close":
        await query.message.delete()
    elif action == "toggle_notif":
        user_settings[update.effective_chat.id]['notifications_on'] ^= True
        await send_filters_menu(update, context)
    elif action == "exchanges":
        await show_exchanges_menu(update, context)

async def show_exchanges_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    active_exchanges = user_settings[query.message.chat_id]['exchanges']
    buttons = [InlineKeyboardButton(f"{'✅' if ex in active_exchanges else '⬜️'} {ex}", callback_data=f"exch_{ex}") for ex in ALL_AVAILABLE_EXCHANGES]
    keyboard = [buttons[i:i + 2] for i in range(0, len(buttons), 2)] + [[InlineKeyboardButton("⬅️ Назад", callback_data="exch_back")]]
    await query.edit_message_text("🏦 **Выберите биржи**", reply_markup=InlineKeyboardMarkup(keyboard))

async def exchanges_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    action = query.data.split('_', 1)[1]
    if action == "back": await send_filters_menu(update, context)
    else:
        active_exchanges = user_settings[query.message.chat_id]['exchanges']
        if action in active_exchanges: active_exchanges.remove(action)
        else: active_exchanges.append(action)
        await show_exchanges_menu(update, context)

async def ask_for_value(update: Update, context: ContextTypes.DEFAULT_TYPE, setting_type: str):
    query = update.callback_query; await query.answer()
    chat_id = update.effective_chat.id
    prompts = {
        'funding': (f"Текущий порог ставки: `> {user_settings[chat_id]['funding_threshold']*100:.2f}%`.\n\n"
                    "Отправьте новое значение в процентах (например, `0.75`)."),
        'volume': (f"Текущий порог объема: `{user_settings[chat_id]['volume_threshold_usdt']:,.0f} USDT`.\n\n"
                   "Отправьте новое значение в USDT (например, `500000`).")
    }
    await query.message.delete()
    sent_message = await context.bot.send_message(
        chat_id=chat_id, text=prompts[setting_type] + "\n\nДля отмены введите /cancel.", parse_mode='Markdown'
    )
    context.user_data['prompt_message_id'] = sent_message.message_id
    return SET_FUNDING_THRESHOLD if setting_type == 'funding' else SET_VOLUME_THRESHOLD

async def save_value(update: Update, context: ContextTypes.DEFAULT_TYPE, setting_type: str):
    chat_id = update.effective_chat.id
    try:
        value_str = update.message.text.strip().replace(",", ".")
        value = Decimal(value_str)
        if setting_type == 'funding':
            if not (0 < value < 100): raise ValueError("Value out of range 0-100")
            user_settings[chat_id]['funding_threshold'] = value / 100
        elif setting_type == 'volume':
            if value < 0: raise ValueError("Value must be positive")
            user_settings[chat_id]['volume_threshold_usdt'] = value
    except (ValueError, TypeError, decimal.InvalidOperation):
        error_messages = {
            'funding': "❌ Ошибка. Введите число от 0 до 100 (например, `0.75`).",
            'volume': "❌ Ошибка. Введите целое положительное число (например, `500000`)."
        }
        await update.message.reply_text(error_messages[setting_type] + " Попробуйте снова.", parse_mode='Markdown')
        return SET_FUNDING_THRESHOLD if setting_type == 'funding' else SET_VOLUME_THRESHOLD
    if 'prompt_message_id' in context.user_data:
        await context.bot.delete_message(chat_id, context.user_data.pop('prompt_message_id'))
    await context.bot.delete_message(chat_id, update.message.message_id)
    await send_filters_menu(update, context)
    return ConversationHandler.END

async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if 'prompt_message_id' in context.user_data:
        try: await context.bot.delete_message(chat_id, context.user_data.pop('prompt_message_id'))
        except Exception: pass
    try: await context.bot.delete_message(chat_id, update.message.id)
    except Exception: pass
    await context.bot.send_message(chat_id, "Действие отменено.")
    await send_filters_menu(update, context)
    return ConversationHandler.END

async def show_my_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать текущие настройки пользователя"""
    chat_id = update.effective_chat.id
    ensure_user_settings(chat_id)
    settings = user_settings[chat_id]
    
    exchanges_list = ", ".join(settings['exchanges'])
    vol = settings['volume_threshold_usdt']
    vol_str = f"{vol / 1_000_000:.1f}M" if vol >= 1_000_000 else f"{vol / 1_000:.0f}K"
    
    message_text = f"""ℹ️ **Ваши текущие настройки:**

🏦 **Биржи:** {exchanges_list}
🔔 **Минимальная ставка:** > {settings['funding_threshold']*100:.2f}%
💧 **Минимальный объем:** > {vol_str} USDT
🔕 **Уведомления:** {'Включены' if settings['notifications_on'] else 'Выключены'}

Для изменения настроек используйте "🔔 Настроить фильтры"
"""
    
    await update.message.reply_text(message_text, parse_mode='Markdown')

async def background_scanner(app):
    """Фоновый сканер для уведомлений (пока не реализован)"""
    pass


# =================================================================
# ========================== ЗАПУСК БОТА ==========================
# =================================================================

if __name__ == "__main__":
    # Опции диагностики при старте (раскомментируйте нужную):
    
    # ВАРИАНТ 1: Быстрая диагностика структуры данных
    print("🚀 Запуск быстрой диагностики MEXC...")
    asyncio.run(quick_mexc_debug())
    
    # ВАРИАНТ 2: Полная диагностика API (раскомментируйте если нужно)
    # print("🚀 Запуск полной диагностики MEXC...")
    # asyncio.run(test_mexc_connection())
    
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Обработчики разговоров для настройки фильтров
    conv_handler_funding = ConversationHandler(
        entry_points=[CallbackQueryHandler(lambda u, c: ask_for_value(u, c, 'funding'), pattern="^filters_funding$")],
        states={SET_FUNDING_THRESHOLD: [MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: save_value(u, c, 'funding'))]},
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
    )
    
    conv_handler_volume = ConversationHandler(
        entry_points=[CallbackQueryHandler(lambda u, c: ask_for_value(u, c, 'volume'), pattern="^filters_volume$")],
        states={SET_VOLUME_THRESHOLD: [MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: save_value(u, c, 'volume'))]},
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
    )

    # Основные обработчики команд
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("^🔥 Топ-ставки сейчас$"), show_top_rates))
    app.add_handler(MessageHandler(filters.Regex("^🔔 Настроить фильтры$"), filters_menu_entry))
    app.add_handler(MessageHandler(filters.Regex("^ℹ️ Мои настройки$"), show_my_settings))
    app.add_handler(MessageHandler(filters.Regex("^🔧 Диагностика MEXC$"), run_diagnostics))
    app.add_handler(MessageHandler(filters.Regex("^🔍 Быстрая диагностика$"), run_quick_diagnostics))
    
    # Обработчики настроек
    app.add_handler(conv_handler_funding)
    app.add_handler(conv_handler_volume)
    
    # Обработчики callback кнопок
    app.add_handler(CallbackQueryHandler(drill_down_callback, pattern="^drill_"))
    app.add_handler(CallbackQueryHandler(back_to_top_callback, pattern="^back_to_top$"))
    app.add_handler(CallbackQueryHandler(filters_callback_handler, pattern="^filters_(close|toggle_notif|exchanges)$"))
    app.add_handler(CallbackQueryHandler(exchanges_callback_handler, pattern="^exch_"))

    # Инициализация фонового сканера
    async def post_init(app): 
        asyncio.create_task(background_scanner(app))
    app.post_init = post_init

    print("🤖 RateHunter 2.0 запущен!")
    print("🔧 Для диагностики MEXC используйте кнопку в боте или раскомментируйте строку выше")
    app.run_polling()
