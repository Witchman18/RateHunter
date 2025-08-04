# =========================================================================
# ===================== RateHunter 2.0 - v1.0.2 =========================
# =========================================================================
# Исправленная версия с улучшенной диагностикой API ошибок
# =========================================================================

import os
import asyncio
import aiohttp
import decimal
import json
import time
import hmac
import hashlib
import traceback
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes,
    ConversationHandler, CallbackQueryHandler, filters
)
from dotenv import load_dotenv

dotenv_path = os.path.join(os.path.dirname(__file__), '.env')
if os.path.exists(dotenv_path):
    load_dotenv(dotenv_path)

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
SET_ALERT_RATE, SET_ALERT_TIME = range(10, 12) 

def get_default_settings():
    return {
        'exchanges': ['Bybit', 'MEXC'],
        'funding_threshold': Decimal('0.005'),         
        'volume_threshold_usdt': Decimal('1000000'),   
        
        # --- НОВЫЕ ПАРАМЕТРЫ ДЛЯ УВЕДОМЛЕНИЙ ---
        'alerts_on': False,                             
        'alert_rate_threshold': Decimal('0.015'),       
        'alert_time_window_minutes': 30,                
        'sent_notifications': set(),                    
    }

def ensure_user_settings(chat_id: int):
    if chat_id not in user_settings: user_settings[chat_id] = get_default_settings()
    for key, value in get_default_settings().items():
        user_settings[chat_id].setdefault(key, value)

# =================================================================
# ===================== МОДУЛЬ СБОРА ДАННЫХ (API) =====================
# =================================================================

async def get_bybit_data(api_key: str, secret_key: str):
    if not api_key or not secret_key:
        print("[API_WARNING] Bybit: Ключи не настроены.")
        return []

    request_path = "/v5/market/tickers"
    base_url = "https://api.bybit.com"
    timestamp = str(int(time.time() * 1000))
    recv_window = "5000"
    params = "category=linear"
    string_to_sign = timestamp + api_key + recv_window + params
    signature = hmac.new(secret_key.encode('utf-8'), string_to_sign.encode('utf-8'), hashlib.sha256).hexdigest()
    headers = {
        'X-BAPI-API-KEY': api_key, 
        'X-BAPI-TIMESTAMP': timestamp, 
        'X-BAPI-RECV-WINDOW': recv_window, 
        'X-BAPI-SIGN': signature,
        'Content-Type': 'application/json'
    }
    
    results = []
    try:
        print(f"[DEBUG] Bybit: Отправляем запрос к {base_url + request_path}?{params}")
        async with aiohttp.ClientSession() as session:
            async with session.get(base_url + request_path + "?" + params, headers=headers, timeout=15) as response:
                response_text = await response.text()
                print(f"[DEBUG] Bybit: Статус {response.status}, размер ответа: {len(response_text)} символов")
                
                if response.status != 200:
                    print(f"[API_ERROR] Bybit: Статус {response.status}")
                    print(f"[API_ERROR] Bybit: Ответ: {response_text[:500]}...")
                    return []
                
                try:
                    data = json.loads(response_text)
                except json.JSONDecodeError as e:
                    print(f"[API_ERROR] Bybit: Ошибка парсинга JSON: {e}")
                    print(f"[API_ERROR] Bybit: Первые 200 символов ответа: {response_text[:200]}")
                    return []
                
                if data.get("retCode") == 0 and data.get("result", {}).get("list"):
                    print(f"[DEBUG] Bybit: Получено {len(data['result']['list'])} инструментов")
                    for t in data["result"]["list"]:
                        try:
                            if not t.get("symbol") or not t.get("fundingRate"):
                                continue
                            results.append({
                                'exchange': 'Bybit', 
                                'symbol': t.get("symbol"), 
                                'rate': Decimal(t.get("fundingRate")), 
                                'next_funding_time': int(t.get("nextFundingTime")), 
                                'volume_24h_usdt': Decimal(t.get("turnover24h", "0")), 
                                'max_order_value_usdt': Decimal('0'), 
                                'trade_url': f'https://www.bybit.com/trade/usdt/{t.get("symbol")}'
                            })
                        except (TypeError, ValueError, decimal.InvalidOperation) as e:
                            print(f"[DEBUG] Bybit: Ошибка обработки инструмента {t.get('symbol', 'unknown')}: {e}")
                            continue
                    print(f"[DEBUG] Bybit: Успешно обработано {len(results)} инструментов")
                else:
                    print(f"[API_ERROR] Bybit: retCode={data.get('retCode')}, retMsg={data.get('retMsg')}")
                    
    except asyncio.TimeoutError:
        print("[API_ERROR] Bybit: Timeout при запросе к API")
    except Exception as e:
        print(f"[API_ERROR] Bybit: Исключение {type(e).__name__}: {e}")
        print(f"[API_ERROR] Bybit: Traceback: {traceback.format_exc()}")
    
    return results

async def get_mexc_data(api_key: str, secret_key: str):
    # API ключи для MEXC больше не требуются для публичных данных,
    # но оставим проверку на случай будущих изменений.
    # if not api_key or not secret_key:
    #     print("[API_WARNING] MEXC: Ключи не настроены (для публичных данных не требуются).")

    results = []
    ticker_url = "https://contract.mexc.com/api/v1/contract/ticker"
    funding_rate_url = "https://contract.mexc.com/api/v1/contract/funding_rate"

    try:
        print("[DEBUG] MEXC: Запрашиваем данные по тикерам и ставкам...")
        async with aiohttp.ClientSession() as session:
            # Используем asyncio.gather для параллельного выполнения запросов
            tasks = [
                session.get(ticker_url, timeout=15),
                session.get(funding_rate_url, timeout=15)
            ]
            responses = await asyncio.gather(*tasks, return_exceptions=True)

            # Проверяем ответы и парсим JSON
            ticker_response, funding_response = responses
            
            if isinstance(ticker_response, Exception) or ticker_response.status != 200:
                print(f"[API_ERROR] MEXC Ticker: Не удалось получить данные. Статус: {getattr(ticker_response, 'status', 'N/A')}")
                return []
            
            if isinstance(funding_response, Exception) or funding_response.status != 200:
                print(f"[API_ERROR] MEXC Funding: Не удалось получить данные. Статус: {getattr(funding_response, 'status', 'N/A')}")
                return []
                
            ticker_data = await ticker_response.json()
            funding_data = await funding_response.json()

            # 1. Создаем словарь с правильными данными о ставках и времени фандинга
            funding_info = {}
            if funding_data.get("success") and funding_data.get("data"):
                for item in funding_data["data"]:
                    symbol = item.get("symbol")
                    if symbol:
                        try:
                            funding_info[symbol] = {
                                'rate': Decimal(str(item.get("fundingRate", "0"))),
                                'next_funding_time': int(item.get("nextSettleTime", 0)) # В этом эндпоинте время верное
                            }
                        except (TypeError, ValueError, decimal.InvalidOperation) as e:
                            print(f"[DEBUG] MEXC: Ошибка парсинга данных фандинга для {symbol}: {e}")
                            continue
            print(f"[DEBUG] MEXC: Обработано {len(funding_info)} ставок фандинга.")

            # 2. Обрабатываем данные тикеров, используя информацию из funding_info
            if ticker_data.get("success") and ticker_data.get("data"):
                print(f"[DEBUG] MEXC: Получено {len(ticker_data['data'])} тикеров.")
                for ticker in ticker_data["data"]:
                    symbol = ticker.get("symbol")
                    if not symbol or not symbol.endswith("_USDT"):
                        continue

                    # Используем данные о ставке и времени из нашего словаря
                    if symbol in funding_info:
                        try:
                            rate = funding_info[symbol]['rate']
                            next_funding = funding_info[symbol]['next_funding_time']
                            
                            # Объем для USDT-M контрактов уже указан в USDT
                            volume_usdt = Decimal(str(ticker.get("amount24", "0")))

                            results.append({
                                'exchange': 'MEXC',
                                'symbol': symbol.replace("_", ""),
                                'rate': rate,
                                'next_funding_time': next_funding,
                                'volume_24h_usdt': volume_usdt,
                                'max_order_value_usdt': Decimal('0'),
                                'trade_url': f'https://futures.mexc.com/exchange/{symbol}'
                            })
                        except (TypeError, ValueError, decimal.InvalidOperation, KeyError) as e:
                            print(f"[DEBUG] MEXC: Ошибка обработки тикера {symbol}: {e}")
                            continue
                
                print(f"[DEBUG] MEXC: Успешно сформировано {len(results)} инструментов.")
            else:
                 print(f"[API_ERROR] MEXC Ticker: API вернул ошибку или пустые данные.")

    except asyncio.TimeoutError:
        print("[API_ERROR] MEXC: Timeout при запросе к API")
    except Exception as e:
        print(f"[API_ERROR] MEXC: Глобальное исключение {type(e).__name__}: {e}")
        print(f"[API_ERROR] MEXC: Traceback: {traceback.format_exc()}")
    
    return results

async def fetch_all_data(context: ContextTypes.DEFAULT_TYPE, force_update=False):
    now = datetime.now().timestamp()
    if not force_update and api_data_cache["last_update"] and (now - api_data_cache["last_update"] < CACHE_LIFETIME_SECONDS):
        print(f"[DEBUG] Используем кэш, возраст: {int(now - api_data_cache['last_update'])} сек")
        return api_data_cache["data"]

    print("[DEBUG] Обновляем данные с API...")
    mexc_api_key, mexc_secret_key = context.bot_data.get('mexc_api_key'), context.bot_data.get('mexc_secret_key')
    bybit_api_key, bybit_secret_key = context.bot_data.get('bybit_api_key'), context.bot_data.get('bybit_secret_key')
    
    tasks = [
        get_bybit_data(api_key=bybit_api_key, secret_key=bybit_secret_key), 
        get_mexc_data(api_key=mexc_api_key, secret_key=mexc_secret_key)
    ]
    results_from_tasks = await asyncio.gather(*tasks, return_exceptions=True)
    
    all_data = []
    for i, res in enumerate(results_from_tasks):
        exchange_name = ['Bybit', 'MEXC'][i]
        if isinstance(res, list): 
            all_data.extend(res)
            print(f"[DEBUG] {exchange_name}: Добавлено {len(res)} инструментов")
        else:
            print(f"[DEBUG] {exchange_name}: Исключение - {res}")
            
    print(f"[DEBUG] Всего получено {len(all_data)} инструментов")
    api_data_cache["data"], api_data_cache["last_update"] = all_data, now
    return all_data

# =================================================================
# ================== ПОЛЬЗОВАТЕЛЬСКИЙ ИНТЕРФЕЙС ==================
# =================================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user_settings(update.effective_chat.id)
    main_menu_keyboard = [["🔥 Топ-ставки сейчас"], ["🔔 Настроить фильтры", "ℹ️ Мои настройки"], ["🔧 Диагностика API"]]
    reply_markup = ReplyKeyboardMarkup(main_menu_keyboard, resize_keyboard=True)
    await update.message.reply_text("Добро пожаловать в RateHunter 2.0!", reply_markup=reply_markup)

async def api_diagnostics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Диагностика состояния API"""
    msg = await update.message.reply_text("🔧 Проверяю состояние API...")
    
    # Принудительно обновляем данные
    all_data = await fetch_all_data(context, force_update=True)
    
    # Подсчитываем данные по биржам
    exchange_counts = {}
    for item in all_data:
        exchange = item.get('exchange', 'Unknown')
        exchange_counts[exchange] = exchange_counts.get(exchange, 0) + 1
    
    # Анализируем ставки и объемы
    rates_analysis = {"high_rates": 0, "medium_rates": 0, "low_rates": 0}
    volume_analysis = {"high_volume": 0, "medium_volume": 0, "low_volume": 0}
    
    for item in all_data:
        rate_pct = abs(item['rate']) * 100
        volume_m = item.get('volume_24h_usdt', Decimal('0')) / 1_000_000
        
        if rate_pct >= 0.5:
            rates_analysis["high_rates"] += 1
        elif rate_pct >= 0.1:
            rates_analysis["medium_rates"] += 1
        else:
            rates_analysis["low_rates"] += 1
            
        if volume_m >= 100:
            volume_analysis["high_volume"] += 1
        elif volume_m >= 10:
            volume_analysis["medium_volume"] += 1
        else:
            volume_analysis["low_volume"] += 1
    
    # Формируем отчет
    report = "🔧 **Диагностика API**\n\n"
    
    if exchange_counts:
        for exchange, count in exchange_counts.items():
            status_emoji = "✅" if count > 0 else "❌"
            report += f"{status_emoji} **{exchange}**: {count} инструментов\n"
    else:
        report += "❌ Нет данных ни с одной биржи\n"
    
    report += f"\n📊 **Анализ ставок:**\n"
    report += f"• ≥ 0.5%: {rates_analysis['high_rates']} пар\n"
    report += f"• 0.1-0.5%: {rates_analysis['medium_rates']} пар\n"
    report += f"• < 0.1%: {rates_analysis['low_rates']} пар\n"
    
    report += f"\n💰 **Анализ объемов:**\n"
    report += f"• ≥ 100M USDT: {volume_analysis['high_volume']} пар\n"
    report += f"• 10-100M USDT: {volume_analysis['medium_volume']} пар\n"
    report += f"• < 10M USDT: {volume_analysis['low_volume']} пар\n"
    
    # Топ-5 по ставкам
    if all_data:
        top_rates = sorted(all_data, key=lambda x: abs(x['rate']), reverse=True)[:5]
        report += f"\n🔥 **Топ-5 ставок:**\n"
        for item in top_rates:
            rate_pct = abs(item['rate']) * 100
            vol_m = item.get('volume_24h_usdt', Decimal('0')) / 1_000_000
            report += f"• {item['symbol'].replace('USDT', '')}: {rate_pct:.3f}% (объем: {vol_m:.1f}M) [{item['exchange']}]\n"
    
    report += f"\n⏰ Время обновления: {datetime.now(MSK_TIMEZONE).strftime('%H:%M:%S MSK')}"
    report += f"\n🕒 Кэш действителен: {CACHE_LIFETIME_SECONDS} сек"
    
    # Проверяем наличие ключей
    report += "\n\n🔑 **Статус ключей:**\n"
    mexc_key = context.bot_data.get('mexc_api_key')
    bybit_key = context.bot_data.get('bybit_api_key')
    
    report += f"{'✅' if mexc_key else '❌'} MEXC: {'Настроены' if mexc_key else 'Отсутствуют'}\n"
    report += f"{'✅' if bybit_key else '❌'} Bybit: {'Настроены' if bybit_key else 'Отсутствуют'}\n"
    
    await msg.edit_text(report, parse_mode='Markdown')

async def show_top_rates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ensure_user_settings(chat_id)
    settings = user_settings[chat_id]

    msg = update.callback_query.message if update.callback_query else await update.message.reply_text("🔄 Ищу...")
    await msg.edit_text("🔄 Ищу лучшие ставки по вашим фильтрам...")

    all_data = await fetch_all_data(context)
    if not all_data:
        await msg.edit_text("😔 Не удалось получить данные с бирж. Попробуйте 🔧 Диагностика API для проверки.")
        return

    # Диагностика фильтров
    print(f"[DEBUG] Фильтры: биржи={settings['exchanges']}, ставка>={settings['funding_threshold']}, объем>={settings['volume_threshold_usdt']}")
    
    # Показываем топ-10 ставок без фильтров для диагностики
    all_sorted = sorted(all_data, key=lambda x: abs(x['rate']), reverse=True)[:10]
    print("[DEBUG] Топ-10 ставок без фильтров:")
    for i, item in enumerate(all_sorted):
        rate_pct = abs(item['rate']) * 100
        vol_m = item.get('volume_24h_usdt', Decimal('0')) / 1_000_000
        print(f"  {i+1}. {item['symbol']} ({item['exchange']}): {rate_pct:.3f}%, объем: {vol_m:.1f}M USDT")

    filtered_data = [item for item in all_data if item['exchange'] in settings['exchanges'] and abs(item['rate']) >= settings['funding_threshold'] and item.get('volume_24h_usdt', Decimal('0')) >= settings['volume_threshold_usdt']]
    filtered_data.sort(key=lambda x: abs(x['rate']), reverse=True)
    top_5 = filtered_data[:5]

    print(f"[DEBUG] После фильтрации найдено: {len(filtered_data)} пар")

    if not top_5:
        # Показываем альтернативную статистику
        exchange_filtered = [item for item in all_data if item['exchange'] in settings['exchanges']]
        rate_filtered = [item for item in exchange_filtered if abs(item['rate']) >= settings['funding_threshold']]
        
        stats_msg = f"😔 Не найдено пар, соответствующих всем фильтрам.\n\n"
        stats_msg += f"📊 **Статистика:**\n"
        stats_msg += f"• Всего инструментов: {len(all_data)}\n"
        stats_msg += f"• На выбранных биржах: {len(exchange_filtered)}\n"
        stats_msg += f"• Со ставкой ≥ {settings['funding_threshold']*100:.1f}%: {len(rate_filtered)}\n"
        stats_msg += f"• С объемом ≥ {settings['volume_threshold_usdt']/1_000:.0f}K: {len(filtered_data)}\n\n"
        
        # Показываем топ-3 без фильтра объема
        if rate_filtered:
            stats_msg += f"🔥 **Топ-3 со ставкой ≥ {settings['funding_threshold']*100:.1f}%:**\n"
            for item in sorted(rate_filtered, key=lambda x: abs(x['rate']), reverse=True)[:3]:
                rate_pct = abs(item['rate']) * 100
                vol_m = item.get('volume_24h_usdt', Decimal('0')) / 1_000_000
                direction = "🟢 LONG" if item['rate'] < 0 else "🔴 SHORT"
                stats_msg += f"{direction} {item['symbol'].replace('USDT', '')} `{rate_pct:.2f}%` (объем: {vol_m:.1f}M) [{item['exchange']}]\n"
        
        await msg.edit_text(stats_msg, parse_mode='Markdown')
        return

    message_text = f"🔥 **ТОП-5 фандингов > {settings['funding_threshold']*100:.2f}%**\n\n"
    buttons = []
    now_utc = datetime.now(timezone.utc)
    
    for item in top_5:
        symbol_only = item['symbol'].replace("USDT", "")
        funding_dt_utc = datetime.fromtimestamp(item['next_funding_time'] / 1000, tz=timezone.utc)
        time_left = funding_dt_utc - now_utc
        countdown_str = ""
        if time_left.total_seconds() > 0:
            h, m = divmod(int(time_left.total_seconds()) // 60, 60)
            countdown_str = f" (осталось {h}ч {m}м)" if h > 0 else f" (осталось {m}м)" if m > 0 else " (меньше минуты)"

        arrow = "🟢" if item['rate'] < 0 else "🔴"
        rate_str = f"{item['rate'] * 100:+.2f}%"
        time_str = funding_dt_utc.astimezone(MSK_TIMEZONE).strftime('%H:%M МСК')
        message_text += f"{arrow} {symbol_only} {rate_str} | 🕒 {time_str}{countdown_str} | {item['exchange']}\n\n"

        buttons.append(InlineKeyboardButton(symbol_only, callback_data=f"drill_{item['symbol']}"))

    keyboard = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    await msg.edit_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown', disable_web_page_preview=True)

async def drill_down_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    symbol_to_show = query.data.split('_')[1]
    await query.answer()

    all_data = api_data_cache.get("data", [])
    if not all_data:
        await query.edit_message_text("🔄 Обновляю данные...")
        all_data = await fetch_all_data(context, force_update=True)
        
    symbol_data = sorted([item for item in all_data if item['symbol'] == symbol_to_show], key=lambda x: abs(x['rate']), reverse=True)
    symbol_only = symbol_to_show.replace("USDT", "")
    message_text = f"💎 **Детали по {symbol_only}**\n\n"
    now_utc = datetime.now(timezone.utc)
    
    for item in symbol_data:
        funding_dt_utc = datetime.fromtimestamp(item['next_funding_time'] / 1000, tz=timezone.utc)
        time_left = funding_dt_utc - now_utc
        countdown_str = ""
        if time_left.total_seconds() > 0:
            h, m = divmod(int(time_left.total_seconds()) // 60, 60)
            countdown_str = f" (осталось {h}ч {m}м)" if h > 0 else f" (осталось {m}м)" if m > 0 else " (меньше минуты)"
        
        direction, rate_str = ("🟢 ЛОНГ", f"{item['rate'] * 100:+.2f}%") if item['rate'] < 0 else ("🔴 ШОРТ", f"{item['rate'] * 100:+.2f}%")
        time_str = funding_dt_utc.astimezone(MSK_TIMEZONE).strftime('%H:%M МСК')
        vol = item.get('volume_24h_usdt', Decimal('0'))
        vol_str = f"{vol/10**9:.1f}B" if vol >= 10**9 else f"{vol/10**6:.1f}M" if vol >= 10**6 else f"{vol/10**3:.0f}K"
            
        message_text += f"{direction} `{rate_str}` в `{time_str}{countdown_str}` [{item['exchange']}]({item['trade_url']})\n  *Объем 24ч:* `{vol_str} USDT`\n"
        if (max_pos := item.get('max_order_value_usdt', Decimal('0'))) > 0: message_text += f"  *Макс. ордер:* `{max_pos:,.0f}`\n"
        message_text += "\n"

    keyboard = [[InlineKeyboardButton("⬅️ Назад к топу", callback_data="back_to_top")]]
    await query.edit_message_text(text=message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown', disable_web_page_preview=True)

async def back_to_top_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
    await show_top_rates(update, context)

async def send_filters_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ensure_user_settings(chat_id)
    settings = user_settings[chat_id]
    message_text = "🔔 **Настройки фильтров для ручного поиска**"
    keyboard = [
        [InlineKeyboardButton("🏦 Биржи", callback_data="filters_exchanges")],
        [InlineKeyboardButton(f"🔔 Ставка: > {settings['funding_threshold']*100:.2f}%", callback_data="filters_funding")],
        [InlineKeyboardButton(f"💧 Объем: > {format_volume(settings['volume_threshold_usdt'])}", callback_data="filters_volume")],
        # --- НОВАЯ КНОПКА ---
        [InlineKeyboardButton("🚨 Настроить Уведомления", callback_data="alert_show_menu")],
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

async def ask_for_value(update: Update, context: ContextTypes.DEFAULT_TYPE, setting_type: str, menu_to_return: callable):
    query, chat_id = update.callback_query, update.effective_chat.id
    await query.answer()
    settings = user_settings[chat_id]
    
    prompts = {
        'funding': (f"Текущий порог ставки: `> {settings['funding_threshold']*100:.2f}%`.\n\nОтправьте новое значение в процентах (например, `0.75`)."),
        'volume': (f"Текущий порог объема: `{format_volume(settings['volume_threshold_usdt'])}`.\n\nОтправьте новое значение (например, `500k` или `2M`)."),
        'alert_rate': (f"Текущий порог для уведомлений: `> {settings['alert_rate_threshold']*100:.2f}%`.\n\nОтправьте новое значение в процентах (например, `1.5`)."),
        'alert_time': (f"Текущее временное окно: `< {settings['alert_time_window_minutes']} минут`.\n\nОтправьте новое значение в минутах (например, `45`).")
    }
    await query.message.delete()
    sent_message = await context.bot.send_message(chat_id=chat_id, text=prompts[setting_type] + "\n\nДля отмены введите /cancel.", parse_mode='Markdown')
    context.user_data.update({'prompt_message_id': sent_message.message_id, 'menu_to_return': menu_to_return})
    
    state_map = {'funding': SET_FUNDING_THRESHOLD, 'volume': SET_VOLUME_THRESHOLD, 'alert_rate': SET_ALERT_RATE, 'alert_time': SET_ALERT_TIME}
    return state_map.get(setting_type)

async def save_value(update: Update, context: ContextTypes.DEFAULT_TYPE, setting_type: str):
    chat_id = update.effective_chat.id
    try:
        value_str = update.message.text.strip().replace(",", ".").upper()
        if setting_type == 'funding' or setting_type == 'alert_rate':
            value = Decimal(value_str)
            if not (0 < value < 100): raise ValueError("Value out of range 0-100")
            key = 'funding_threshold' if setting_type == 'funding' else 'alert_rate_threshold'
            user_settings[chat_id][key] = value / 100
        elif setting_type == 'volume':
            num_part = value_str.replace('K', '').replace('M', '')
            multiplier = 1000 if 'K' in value_str else 1_000_000 if 'M' in value_str else 1
            user_settings[chat_id]['volume_threshold_usdt'] = Decimal(num_part) * multiplier
        elif setting_type == 'alert_time':
            value = int(value_str)
            if value <= 0: raise ValueError("Value must be positive")
            user_settings[chat_id]['alert_time_window_minutes'] = value
    except (ValueError, TypeError, decimal.InvalidOperation):
        await update.message.reply_text("❌ Ошибка. Введите корректное значение. Попробуйте снова.", parse_mode='Markdown')
        return # Остаемся в том же состоянии, чтобы пользователь мог попробовать снова

    if 'prompt_message_id' in context.user_data:
        await context.bot.delete_message(chat_id, context.user_data.pop('prompt_message_id'))
    await context.bot.delete_message(chat_id, update.message.message_id)
    await context.user_data.pop('menu_to_return')(update, context) # Возвращаемся в нужное меню
    return ConversationHandler.END

async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if 'prompt_message_id' in context.user_data:
        try: await context.bot.delete_message(chat_id, context.user_data.pop('prompt_message_id'))
        except Exception: pass
    try: await context.bot.delete_message(chat_id, update.message.id)
    except Exception: pass
    await context.bot.send_message(chat_id, "Действие отменено.")
    if 'menu_to_return' in context.user_data:
        await context.user_data.pop('menu_to_return')(update, context)
    return ConversationHandler.END
    
async def show_my_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

# --- Блок для настройки уведомлений ---
async def show_alerts_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает меню настройки кастомных уведомлений."""
    if query := update.callback_query: await query.answer()
    chat_id = update.effective_chat.id
    ensure_user_settings(chat_id)
    settings = user_settings[chat_id]
    
    status_emoji = "✅" if settings.get('alerts_on', False) else "🔴"
    status_text = "ВКЛЮЧЕНЫ" if settings.get('alerts_on', False) else "ВЫКЛЮЧЕНЫ"
    message_text = "🚨 **Настройки уведомлений**\n\nБот пришлет сигнал, когда будут выполнены оба условия."
    
    keyboard = [
        [InlineKeyboardButton(f"📈 Порог ставки: > {settings['alert_rate_threshold']*100:.2f}%", callback_data="alert_set_rate")],
        [InlineKeyboardButton(f"⏰ Окно до выплаты: < {settings['alert_time_window_minutes']} мин", callback_data="alert_set_time")],
        [InlineKeyboardButton(f"{status_emoji} Уведомления: {status_text}", callback_data="alert_toggle_on")],
        [InlineKeyboardButton("⬅️ Назад к фильтрам", callback_data="alert_back_filters")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        await update.callback_query.edit_message_text(message_text, reply_markup=reply_markup, parse_mode='Markdown')
    else:
        await update.message.reply_text(message_text, reply_markup=reply_markup, parse_mode='Markdown')

async def alert_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает нажатия в меню уведомлений."""
    query, action = update.callback_query, query.data.split('_', 1)[1]
    await query.answer()
    if action == "toggle_on":
        user_settings[update.effective_chat.id]['alerts_on'] ^= True
        await show_alerts_menu(update, context)
    elif action == "back_filters":
        await send_filters_menu(update, context)

async def background_scanner(app: ApplicationBuilder):
    """Фоновый процесс для мониторинга и отправки кастомных уведомлений."""
    print("🚀 Фоновый сканер уведомлений запущен.")
    while True:
        await asyncio.sleep(60) # Проверка раз в минуту
        try:
            # Получаем свежие данные для всех
            all_data = await fetch_all_data(app, force_update=True)
            if not all_data: continue

            now_utc = datetime.now(timezone.utc)
            current_ts_ms = int(now_utc.timestamp() * 1000)

            # Проходим по всем пользователям
            for chat_id, settings in list(user_settings.items()):
                if not settings.get('alerts_on', False): continue

                # Очистка старых ID уведомлений (старше 3 часов)
                settings['sent_notifications'] = {nid for nid in settings['sent_notifications'] if int(nid.split('_')[-1]) > current_ts_ms - (3 * 60 * 60 * 1000)}
                
                # Ищем подходящие пары для этого пользователя
                for item in all_data:
                    if item['exchange'] not in settings['exchanges']: continue
                    if abs(item['rate']) < settings['alert_rate_threshold']: continue

                    time_left = datetime.fromtimestamp(item['next_funding_time'] / 1000, tz=timezone.utc) - now_utc
                    if not (0 < time_left.total_seconds() <= settings['alert_time_window_minutes'] * 60): continue

                    # Анти-спам
                    notification_id = f"{item['exchange']}_{item['symbol']}_{item['next_funding_time']}"
                    if notification_id in settings['sent_notifications']: continue
                    
                    # Все условия выполнены! Отправляем уведомление.
                    h, m = divmod(int(time_left.total_seconds() // 60), 60)
                    countdown_str = f"{h}ч {m}м" if h > 0 else f"{m}м"
                    message = (f"⚠️ **Найден фандинг по вашему фильтру!**\n\n"
                               f"{'🟢' if item['rate'] < 0 else '🔴'} **{item['symbol'].replace('USDT', '')}** `{item['rate'] * 100:+.2f}%`\n"
                               f"⏰ Выплата через *{countdown_str}* на *{item['exchange']}*")
                    try:
                        await app.bot.send_message(chat_id, message, parse_mode='Markdown')
                        settings['sent_notifications'].add(notification_id)
                        print(f"[BG_SCANNER] Отправлено уведомление для {chat_id}: {notification_id}")
                    except Exception as e:
                        print(f"[BG_SCANNER] Не удалось отправить уведомление для {chat_id}: {e}")
        except Exception as e:
            print(f"[BG_SCANNER] Критическая ошибка в цикле сканера: {e}\n{traceback.format_exc()}")

# =================================================================
# ========================== ЗАПУСК БОТА ==========================
# =================================================================

if __name__ == "__main__":
    if not BOT_TOKEN:
        raise ValueError("Не найден BOT_TOKEN. Убедитесь, что он задан в .env файле.")
    
    # 1. Создаем приложение
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    # 2. Загружаем ключи API
    app.bot_data['mexc_api_key'] = os.getenv("MEXC_API_KEY")
    app.bot_data['mexc_secret_key'] = os.getenv("MEXC_API_SECRET")
    app.bot_data['bybit_api_key'] = os.getenv("BYBIT_API_KEY")
    app.bot_data['bybit_secret_key'] = os.getenv("BYBIT_API_SECRET")

    if app.bot_data['bybit_api_key']: print("✅ Ключи Bybit успешно загружены.")
    else: print("⚠️ Ключи Bybit не найдены.")
    print("ℹ️ Ключи для MEXC (публичные данные) больше не требуются.")

    # --- 3. РЕГИСТРАЦИЯ ОБРАБОТЧИКОВ ---
    
    # Упрощенные fallbacks
    fallbacks = [CommandHandler("cancel", cancel_conversation)]

    # Список всех диалогов (ConversationHandlers)
    conv_handlers = [
        ConversationHandler(
            entry_points=[CallbackQueryHandler(lambda u, c: ask_for_value(u, c, 'funding', send_filters_menu), pattern="^filters_funding$")],
            states={
                SET_FUNDING_THRESHOLD: [MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: save_value(u, c, 'funding'))]
            },
            fallbacks=fallbacks,
            allow_reentry=True
        ),
        ConversationHandler(
            entry_points=[CallbackQueryHandler(lambda u, c: ask_for_value(u, c, 'volume', send_filters_menu), pattern="^filters_volume$")],
            states={
                SET_VOLUME_THRESHOLD: [MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: save_value(u, c, 'volume'))]
            },
            fallbacks=fallbacks,
            allow_reentry=True
        ),
        ConversationHandler(
            entry_points=[CallbackQueryHandler(lambda u, c: ask_for_value(u, c, 'alert_rate', show_alerts_menu), pattern="^alert_set_rate$")],
            states={
                SET_ALERT_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: save_value(u, c, 'alert_rate'))]
            },
            fallbacks=fallbacks,
            allow_reentry=True
        ),
        ConversationHandler(
            entry_points=[CallbackQueryHandler(lambda u, c: ask_for_value(u, c, 'alert_time', show_alerts_menu), pattern="^alert_set_time$")],
            states={
                SET_ALERT_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: save_value(u, c, 'alert_time'))]
            },
            fallbacks=fallbacks,
            allow_reentry=True
        ),
    ]
    
    # Список обычных обработчиков (команды, текст, кнопки)
    regular_handlers = [
        CommandHandler("start", start),
        MessageHandler(filters.Regex("^🔥 Топ-ставки сейчас$"), show_top_rates),
        MessageHandler(filters.Regex("^🔔 Настроить фильтры$"), filters_menu_entry),
        MessageHandler(filters.Regex("^ℹ️ Мои настройки$"), show_my_settings),
        MessageHandler(filters.Regex("^🔧 Диагностика API$"), api_diagnostics),
        # Обработчик для текстовых сообщений
        MessageHandler(filters.TEXT & ~filters.COMMAND, lambda update, context: start(update, context) if update.message.text == "/start" else None),
        # Обработчики кнопок
        CallbackQueryHandler(drill_down_callback, pattern="^drill_"),
        CallbackQueryHandler(back_to_top_callback, pattern="^back_to_top$"),
        CallbackQueryHandler(exchanges_callback_handler, pattern="^exch_"),
        CallbackQueryHandler(show_alerts_menu, pattern="^alert_show_menu$"),
        CallbackQueryHandler(alert_callback_handler, pattern="^alert_"),
    ]

    # Добавляем все обработчики в приложение
    app.add_handlers(conv_handlers)
    app.add_handlers(regular_handlers)

    # 4. ПРАВИЛЬНЫЙ запуск фонового сканера
    async def post_init(app: Application):
        # Создаем фоновую задачу, не блокируя основной поток
        asyncio.create_task(background_scanner(app))

    app.post_init = post_init

    # 5. Запускаем бота
    print("🤖 RateHunter 2.0 запущен!")
    app.run_polling()
