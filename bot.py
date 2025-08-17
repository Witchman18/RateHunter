# =========================================================================
# ===================== RateHunter 2.0 - v1.1.0 С АНАЛИЗАТОРОМ ===========
# =========================================================================
# Добавлен умный анализатор трендов funding rate
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
import pandas as pd
import io
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Dict, List, Tuple, Optional

from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes,
    ConversationHandler, CallbackQueryHandler, filters
)
from dotenv import load_dotenv

dotenv_path = os.path.join(os.path.dirname(__file__), '.env')
if os.path.exists(dotenv_path):
    load_dotenv(dotenv_path)

# --- Конфигурация ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
MSK_TIMEZONE = timezone(timedelta(hours=3))

# === СПИСОК РАЗРЕШЕННЫХ ПОЛЬЗОВАТЕЛЕЙ ===
ALLOWED_USERS = [
    518449824,642874424  # Замените на свой Telegram ID
    # Можете добавить ID других пользователей
]

# Функция для проверки доступа
def check_access(user_id: int) -> bool:
    """Проверяет, разрешен ли доступ пользователю"""
    # Приводим к int для надежности
    try:
        user_id = int(user_id)
    except (ValueError, TypeError):
        return False
    return user_id in ALLOWED_USERS

# ===== НОВЫЙ МОДУЛЬ: АНАЛИЗАТОР ТРЕНДОВ FUNDING RATE =====
class FundingTrendAnalyzer:
    """Анализирует тренды и стабильность funding rates"""
    
    def __init__(self):
        self.historical_cache = {}  # Кэш исторических данных
        
    async def analyze_funding_stability(self, symbol: str, exchange: str, current_rate: Decimal) -> Dict:
        """
        Анализирует стабильность и тренд funding rate
        Возвращает классификацию: стабильная_аномалия / истощающаяся_аномалия
        """
        
        # Получаем историю ставок за последние несколько периодов
        history = await self._get_funding_history(symbol, exchange, periods=3)
        
        if not history or len(history) < 2:
            return {
                'trend': 'unknown',
                'stability': 'unknown',
                'confidence': 0.0,
                'recommendation': 'insufficient_data'
            }
        
        # Анализируем тренд
        trend_analysis = self._analyze_trend(history, current_rate)
        
        # Анализируем стабильность
        stability_analysis = self._analyze_stability(history, current_rate)
        
        # Формируем рекомендацию
        recommendation = self._make_recommendation(trend_analysis, stability_analysis, current_rate)
        
        return {
            'trend': trend_analysis['direction'],  # 'growing', 'declining', 'stable'
            'trend_strength': trend_analysis['strength'],  # 0.0 - 1.0
            'stability': stability_analysis['level'],  # 'stable', 'volatile', 'declining'
            'stability_score': stability_analysis['score'],  # 0.0 - 1.0
            'confidence': min(trend_analysis['confidence'], stability_analysis['confidence']),
            'recommendation': recommendation,
            'history': history,
            'analysis_details': {
                'rate_change': trend_analysis['rate_change'],
                'volatility': stability_analysis['volatility']
            }
        }
    
    def _analyze_trend(self, history: List[Decimal], current_rate: Decimal) -> Dict:
        """Анализирует направление тренда ставки"""
        
        if len(history) < 2:
            return {'direction': 'unknown', 'strength': 0.0, 'confidence': 0.0, 'rate_change': 0.0}
        
        # Вычисляем изменения между периодами
        changes = []
        all_rates = history + [current_rate]
        
        for i in range(1, len(all_rates)):
            change = float(all_rates[i] - all_rates[i-1])
            changes.append(change)
        
        if not changes:
            return {'direction': 'unknown', 'strength': 0.0, 'confidence': 0.0, 'rate_change': 0.0}
        
        # Определяем общее направление
        total_change = sum(changes)
        avg_change = total_change / len(changes)
        
        # Определяем силу тренда (консистентность направления)
        positive_changes = sum(1 for c in changes if c > 0)
        negative_changes = sum(1 for c in changes if c < 0)
        
        if positive_changes > negative_changes:
            direction = 'growing'
            strength = positive_changes / len(changes)
        elif negative_changes > positive_changes:
            direction = 'declining' 
            strength = negative_changes / len(changes)
        else:
            direction = 'stable'
            strength = 0.5
        
        # Уверенность зависит от количества данных и консистентности
        confidence = min(1.0, len(changes) / 3.0) * strength
        
        return {
            'direction': direction,
            'strength': strength,
            'confidence': confidence,
            'rate_change': avg_change,
            'total_change': total_change
        }
    
    def _analyze_stability(self, history: List[Decimal], current_rate: Decimal) -> Dict:
        """Анализирует стабильность (волатильность) ставки"""
        
        all_rates = history + [current_rate]
        
        if len(all_rates) < 2:
            return {'level': 'unknown', 'score': 0.0, 'confidence': 0.0, 'volatility': 0.0}
        
        # Вычисляем волатильность как стандартное отклонение
        rates_float = [float(rate) for rate in all_rates]
        mean_rate = sum(rates_float) / len(rates_float)
        
        variance = sum((rate - mean_rate) ** 2 for rate in rates_float) / len(rates_float)
        volatility = variance ** 0.5
        
        # Классифицируем уровень стабильности
        # Эти пороги можно будет подстроить на основе тестов
        if volatility < 0.001:  # Изменения меньше 0.1%
            level = 'stable'
            score = 0.9
        elif volatility < 0.003:  # Изменения меньше 0.3%
            level = 'moderate'
            score = 0.7
        else:
            level = 'volatile'
            score = 0.3
        
        confidence = min(1.0, len(all_rates) / 3.0)
        
        return {
            'level': level,
            'score': score,
            'confidence': confidence,
            'volatility': volatility
        }
    
    def _make_recommendation(self, trend_analysis: Dict, stability_analysis: Dict, current_rate: Decimal) -> str:
        """
        Формирует рекомендацию на основе анализа тренда и стабильности
        """
        
        abs_rate = abs(float(current_rate))
        trend = trend_analysis['direction']
        stability = stability_analysis['level']
        
        # Низкие ставки - не интересны
        if abs_rate < 0.005:  # Меньше 0.5%
            return 'rate_too_low'
        
        # Сценарии из документа
        if trend == 'growing' or trend == 'stable':
            if stability in ['stable', 'moderate']:
                return 'ideal_arbitrage'  # ✅ Идеальный лонг/шорт
            else:
                return 'risky_arbitrage'  # ⚠️ Рискованный из-за волатильности
        
        elif trend == 'declining':
            return 'contrarian_opportunity'  # 🔥 Возможность на развороте
        
        else:
            return 'unclear_signal'  # ⚪️ Неоднозначная ситуация
    
    async def _get_funding_history(self, symbol: str, exchange: str, periods: int = 3) -> List[Decimal]:
        """
        Получает историю funding rates
        TODO: Подключить к реальным API
        """
        
        # Пока используем заглушку для тестирования
        cache_key = f"{exchange}_{symbol}"
        
        if cache_key not in self.historical_cache:
            # Имитируем разные сценарии для тестов
            current_time = int(time.time())
            symbol_hash = hash(symbol) % 4
            
            if symbol_hash == 0:
                # Стабильная высокая аномалия
                self.historical_cache[cache_key] = [Decimal('-0.019'), Decimal('-0.020')]
            elif symbol_hash == 1:
                # Истощающаяся аномалия
                self.historical_cache[cache_key] = [Decimal('-0.021'), Decimal('-0.017')]
            elif symbol_hash == 2:
                # Растущая аномалия
                self.historical_cache[cache_key] = [Decimal('0.008'), Decimal('0.012')]
            else:
                # Волатильная ситуация
                self.historical_cache[cache_key] = [Decimal('-0.025'), Decimal('-0.010')]
        
        return self.historical_cache[cache_key]

# Глобальный анализатор
funding_analyzer = FundingTrendAnalyzer()

# ===== ИСПРАВЛЕННАЯ ФУНКЦИЯ ОТКАЗА В ДОСТУПЕ =====
async def access_denied_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправляет сообщение об отказе в доступе (работает и с callback_query)"""
    user_id = update.effective_user.id
    username = update.effective_user.username or "неизвестно"
    
    message_text = (
        f"⛔ **Доступ запрещён**\n\n"
        f"Ваш ID: `{user_id}`\n"
        f"Username: @{username}\n\n"
        f"Обратитесь к администратору для получения доступа."
    )
    
    # Логируем попытку несанкционированного доступа
    print(f"[ACCESS_DENIED] Пользователь {user_id} (@{username}) попытался получить доступ")
    
    # Правильная обработка как для сообщений, так и для callback_query
    try:
        if update.callback_query:
            await update.callback_query.answer("⛔ Доступ запрещён", show_alert=True)
            # Пытаемся отредактировать сообщение, если возможно
            try:
                await update.callback_query.edit_message_text(message_text, parse_mode='Markdown')
            except:
                # Если не получается отредактировать, отправляем новое
                await context.bot.send_message(
                    chat_id=update.effective_chat.id, 
                    text=message_text, 
                    parse_mode='Markdown'
                )
        elif update.message:
            await update.message.reply_text(message_text, parse_mode='Markdown')
        else:
            # Fallback
            await context.bot.send_message(
                chat_id=update.effective_chat.id, 
                text=message_text, 
                parse_mode='Markdown'
            )
    except Exception as e:
        print(f"[ERROR] Не удалось отправить сообщение об отказе в доступе: {e}")

# Декоратор для проверки доступа
def require_access():
    """Декоратор для проверки доступа к функциям бота"""
    def decorator(func):
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if not check_access(update.effective_user.id):
                await access_denied_message(update, context)
                return
            return await func(update, context)
        return wrapper
    return decorator

# --- Состояния для ConversationHandler ---
(SET_FUNDING_THRESHOLD, SET_VOLUME_THRESHOLD, 
 SET_ALERT_RATE, SET_ALERT_TIME) = range(4)

# === ИСПРАВЛЕННАЯ СТРУКТУРА ДАННЫХ ===
# Теперь храним и user_id для корректной проверки доступа
user_settings = {}  # Ключ: chat_id, значение: {'user_id': int, 'settings': dict}
api_data_cache = {"last_update": None, "data": []}
CACHE_LIFETIME_SECONDS = 60
ALL_AVAILABLE_EXCHANGES = ['Bybit', 'MEXC', 'Binance', 'OKX', 'KuCoin', 'Gate.io', 'HTX', 'Bitget']

# Функция форматирования объема
def format_volume(volume_usdt: Decimal) -> str:
    """Форматирует объем в читаемый вид (K, M, B)"""
    vol = volume_usdt
    if vol >= 1_000_000_000:
        return f"{vol / 1_000_000_000:.1f}B"
    elif vol >= 1_000_000:
        return f"{vol / 1_000_000:.1f}M"
    elif vol >= 1_000:
        return f"{vol / 1_000:.0f}K"
    else:
        return f"{vol:.0f}"
        
def get_default_settings():
    return {
        'exchanges': ['Bybit', 'MEXC'],
        'funding_threshold': Decimal('0.005'),         
        'volume_threshold_usdt': Decimal('1000000'),   
        
        # --- ПАРАМЕТРЫ ДЛЯ УВЕДОМЛЕНИЙ ---
        'alerts_on': False,                             
        'alert_rate_threshold': Decimal('0.015'),       
        'alert_time_window_minutes': 30,                
        'sent_notifications': set(),                    
    }

# ===== ИСПРАВЛЕННАЯ ФУНКЦИЯ НАСТРОЕК =====
def ensure_user_settings(chat_id: int, user_id: int):
    """Убеждается, что настройки пользователя существуют и сохраняет user_id"""
    if chat_id not in user_settings:
        user_settings[chat_id] = {
            'user_id': user_id,
            'settings': get_default_settings()
        }
    else:
        # Обновляем user_id на случай, если он изменился
        user_settings[chat_id]['user_id'] = user_id
        
    # Убеждаемся, что все настройки есть
    for key, value in get_default_settings().items():
        user_settings[chat_id]['settings'].setdefault(key, value)

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
    results = []
    ticker_url = "https://contract.mexc.com/api/v1/contract/ticker"
    funding_rate_url = "https://contract.mexc.com/api/v1/contract/funding_rate"

    try:
        print("[DEBUG] MEXC: Запрашиваем данные по тикерам и ставкам...")
        async with aiohttp.ClientSession() as session:
            tasks = [
                session.get(ticker_url, timeout=15),
                session.get(funding_rate_url, timeout=15)
            ]
            responses = await asyncio.gather(*tasks, return_exceptions=True)

            ticker_response, funding_response = responses
            
            if isinstance(ticker_response, Exception) or ticker_response.status != 200:
                print(f"[API_ERROR] MEXC Ticker: Не удалось получить данные. Статус: {getattr(ticker_response, 'status', 'N/A')}")
                return []
            
            if isinstance(funding_response, Exception) or funding_response.status != 200:
                print(f"[API_ERROR] MEXC Funding: Не удалось получить данные. Статус: {getattr(funding_response, 'status', 'N/A')}")
                return []
                
            ticker_data = await ticker_response.json()
            funding_data = await funding_response.json()

            funding_info = {}
            if funding_data.get("success") and funding_data.get("data"):
                for item in funding_data["data"]:
                    symbol = item.get("symbol")
                    if symbol:
                        try:
                            funding_info[symbol] = {
                                'rate': Decimal(str(item.get("fundingRate", "0"))),
                                'next_funding_time': int(item.get("nextSettleTime", 0))
                            }
                        except (TypeError, ValueError, decimal.InvalidOperation) as e:
                            print(f"[DEBUG] MEXC: Ошибка парсинга данных фандинга для {symbol}: {e}")
                            continue
            print(f"[DEBUG] MEXC: Обработано {len(funding_info)} ставок фандинга.")

            if ticker_data.get("success") and ticker_data.get("data"):
                print(f"[DEBUG] MEXC: Получено {len(ticker_data['data'])} тикеров.")
                for ticker in ticker_data["data"]:
                    symbol = ticker.get("symbol")
                    if not symbol or not symbol.endswith("_USDT"):
                        continue

                    if symbol in funding_info:
                        try:
                            rate = funding_info[symbol]['rate']
                            next_funding = funding_info[symbol]['next_funding_time']
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

async def fetch_all_data(context: ContextTypes.DEFAULT_TYPE | Application, force_update=False):
    now = datetime.now().timestamp()
    if not force_update and api_data_cache["last_update"] and (now - api_data_cache["last_update"] < CACHE_LIFETIME_SECONDS):
        return api_data_cache["data"]

    bot_data = context.bot_data if isinstance(context, Application) else context.bot_data
    
    print("[DEBUG] Обновляем данные с API...")
    mexc_api_key = bot_data.get('mexc_api_key')
    mexc_secret_key = bot_data.get('mexc_secret_key')
    bybit_api_key = bot_data.get('bybit_api_key')
    bybit_secret_key = bot_data.get('bybit_secret_key')
    
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


async def fetch_funding_history_async(symbol, start_time, end_time):
    """Асинхронно получает историю ставок финансирования с MEXC."""
    url = f"https://contract.mexc.com/api/v1/contract/funding_rate/history"
    params = {'symbol': symbol, 'page_size': 100, 'start_time': start_time, 'end_time': end_time}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=10) as response:
                response.raise_for_status()
                data = await response.json()
                if data.get("success"): return data.get('data', [])
                else: return []
    except Exception: return []

async def fetch_klines_async(symbol, start_time, end_time):
    """Асинхронно получает 1-минутные свечи с MEXC."""
    url = f"https://contract.mexc.com/api/v1/contract/kline/{symbol}"
    all_klines = []
    current_time = start_time
    try:
        async with aiohttp.ClientSession() as session:
            while current_time < end_time:
                params = {'symbol': symbol, 'interval': 'Min1', 'start': int(current_time / 1000), 'end': int(end_time / 1000)}
                async with session.get(url, params=params, timeout=20) as response:
                    response.raise_for_status()
                    data = await response.json()
                    if data.get("success") and data.get('data', {}).get('time'):
                        klines = data['data']
                        for i in range(len(klines['time'])):
                            all_klines.append([klines['time'][i] * 1000, klines['open'][i], klines['high'][i], klines['low'][i], klines['close'][i], klines['vol'][i]])
                        last_time = klines['time'][-1] * 1000
                        if last_time >= current_time: current_time = last_time + 60000
                        else: break
                    else: break
    except Exception: return []
    return all_klines

# =================================================================
# ========== ПОЛЬЗОВАТЕЛЬСКИЙ ИНТЕРФЕЙС С УМНЫМ АНАЛИЗОМ ==========
# =================================================================

@require_access()
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f"--- ПОЛУЧЕНА КОМАНДА /start от пользователя {update.effective_user.id} ---")
    ensure_user_settings(update.effective_chat.id, update.effective_user.id)
    main_menu_keyboard = [["🔥 Топ-ставки сейчас"], ["🔧 Настроить фильтры", "ℹ️ Мои настройки"], ["🔧 Диагностика API"]]
    reply_markup = ReplyKeyboardMarkup(main_menu_keyboard, resize_keyboard=True)
    await update.message.reply_text("Добро пожаловать в RateHunter 2.0 с умным анализатором!", reply_markup=reply_markup)

@require_access()
async def api_diagnostics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Диагностика состояния API"""
    msg = await update.message.reply_text("🔧 Проверяю состояние API...")
    
    all_data = await fetch_all_data(context, force_update=True)
    
    exchange_counts = {}
    for item in all_data:
        exchange = item.get('exchange', 'Unknown')
        exchange_counts[exchange] = exchange_counts.get(exchange, 0) + 1
    
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
    
    if all_data:
        top_rates = sorted(all_data, key=lambda x: abs(x['rate']), reverse=True)[:5]
        report += f"\n🔥 **Топ-5 ставок:**\n"
        for item in top_rates:
            rate_pct = abs(item['rate']) * 100
            vol_m = item.get('volume_24h_usdt', Decimal('0')) / 1_000_000
            report += f"• {item['symbol'].replace('USDT', '')}: {rate_pct:.3f}% (объем: {vol_m:.1f}M) [{item['exchange']}]\n"
    
    report += f"\n⏰ Время обновления: {datetime.now(MSK_TIMEZONE).strftime('%H:%M:%S MSK')}"
    report += f"\n🕑 Кэш действителен: {CACHE_LIFETIME_SECONDS} сек"
    
    report += "\n\n🔑 **Статус ключей:**\n"
    mexc_key = context.bot_data.get('mexc_api_key')
    bybit_key = context.bot_data.get('bybit_api_key')
    
    report += f"{'✅' if mexc_key else '❌'} MEXC: {'Настроены' if mexc_key else 'Отсутствуют'}\n"
    report += f"{'✅' if bybit_key else '❌'} Bybit: {'Настроены' if bybit_key else 'Отсутствуют'}\n"
    
    await msg.edit_text(report, parse_mode='Markdown')

# ===== НОВАЯ ФУНКЦИЯ: УМНЫЙ АНАЛИЗ ВОЗМОЖНОСТЕЙ =====
async def analyze_funding_opportunity(item: Dict) -> Dict:
    """
    Интегрирует умный анализ в данные инструмента
    Добавляет рекомендации на основе анализа тренда
    """
    
    # Запускаем анализ стабильности
    stability_analysis = await funding_analyzer.analyze_funding_stability(
        symbol=item['symbol'],
        exchange=item['exchange'], 
        current_rate=item['rate']
    )
    
    # Добавляем анализ к данным элемента
    item['stability_analysis'] = stability_analysis
    
    # Формируем умное сообщение
    recommendation = stability_analysis['recommendation']
    confidence = stability_analysis['confidence']
    
    # Эмодзи и сообщения для разных типов рекомендаций
    recommendation_map = {
        'ideal_arbitrage': {
            'emoji': '✅',
            'message': 'Идеальные условия',
            'details': 'Ставка стабильна, низкий риск'
        },
        'risky_arbitrage': {
            'emoji': '⚠️', 
            'message': 'Рискованно',
            'details': 'Высокая волатильность ставки'
        },
        'contrarian_opportunity': {
            'emoji': '🔥',
            'message': 'Возможность на развороте', 
            'details': 'Ставка истощается'
        },
        'unclear_signal': {
            'emoji': '⚪️',
            'message': 'Неоднозначно',
            'details': 'Смешанные сигналы'
        },
        'rate_too_low': {
            'emoji': '📉',
            'message': 'Ставка низкая',
            'details': 'Не достигает порога'
        },
        'insufficient_data': {
            'emoji': '❓',
            'message': 'Мало данных',
            'details': 'Нужна история ставок'
        }
    }
    
    rec_info = recommendation_map.get(recommendation, {
        'emoji': '❓',
        'message': 'Анализ...',
        'details': 'Обработка данных'
    })
    
    item['smart_recommendation'] = {
        'emoji': rec_info['emoji'],
        'message': rec_info['message'],
        'details': rec_info['details'],
        'confidence': confidence,
        'recommendation_type': recommendation
    }
    
    return item

@require_access()
async def show_top_rates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    ensure_user_settings(chat_id, user_id)
    settings = user_settings[chat_id]['settings']

    msg = update.callback_query.message if update.callback_query else await update.message.reply_text("🔄 Ищу...")
    await msg.edit_text("🔄 Ищу лучшие возможности...")

    all_data = await fetch_all_data(context)
    if not all_data:
        await msg.edit_text("😞 Не удалось получить данные с бирж. Попробуйте 🔧 Диагностика API для проверки.")
        return

    print(f"[DEBUG] Фильтры: биржи={settings['exchanges']}, ставка>={settings['funding_threshold']}, объем>={settings['volume_threshold_usdt']}")
    
    # Применяем фильтры
    filtered_data = [
        item for item in all_data 
        if item['exchange'] in settings['exchanges'] 
        and abs(item['rate']) >= settings['funding_threshold'] 
        and item.get('volume_24h_usdt', Decimal('0')) >= settings['volume_threshold_usdt']
    ]
    
    if not filtered_data:
        exchange_filtered = [item for item in all_data if item['exchange'] in settings['exchanges']]
        rate_filtered = [item for item in exchange_filtered if abs(item['rate']) >= settings['funding_threshold']]
        
        stats_msg = f"😞 Не найдено пар, соответствующих всем фильтрам.\n\n"
        stats_msg += f"📊 **Статистика:**\n"
        stats_msg += f"• Всего инструментов: {len(all_data)}\n"
        stats_msg += f"• На выбранных биржах: {len(exchange_filtered)}\n"
        stats_msg += f"• Со ставкой ≥ {settings['funding_threshold']*100:.1f}%: {len(rate_filtered)}\n"
        stats_msg += f"• С объемом ≥ {settings['volume_threshold_usdt']/1_000:.0f}K: {len(filtered_data)}\n\n"
        
        if rate_filtered:
            stats_msg += f"🔥 **Топ-3 со ставкой ≥ {settings['funding_threshold']*100:.1f}%:**\n"
            for item in sorted(rate_filtered, key=lambda x: abs(x['rate']), reverse=True)[:3]:
                rate_pct = abs(item['rate']) * 100
                vol_m = item.get('volume_24h_usdt', Decimal('0')) / 1_000_000
                direction = "🟢 LONG" if item['rate'] < 0 else "🔴 SHORT"
                stats_msg += f"{direction} {item['symbol'].replace('USDT', '')} `{rate_pct:.2f}%` (объем: {vol_m:.1f}M) [{item['exchange']}]\n"
        
        await msg.edit_text(stats_msg, parse_mode='Markdown')
        return

    # Сортируем по абсолютной ставке
    filtered_data.sort(key=lambda x: abs(x['rate']), reverse=True)
    top_5 = filtered_data[:5]

    # ===== ЧИСТЫЙ ИНТЕРФЕЙС БЕЗ ИИ =====
    # Сохраняем данные для ИИ-анализа, но не показываем их сразу
    context.chat_data = context.chat_data or {}
    context.chat_data['current_opportunities'] = top_5

    # Формируем чистое сообщение
    message_text = f"🔥 **ТОП-5 фандинг возможностей**\n\n"
    buttons = []
    now_utc = datetime.now(timezone.utc)
    
    for item in top_5:
        symbol_only = item['symbol'].replace("USDT", "")
        funding_dt_utc = datetime.fromtimestamp(item['next_funding_time'] / 1000, tz=timezone.utc)
        time_left = funding_dt_utc - now_utc
        countdown_str = ""
        if time_left.total_seconds() > 0:
            h, m = divmod(int(time_left.total_seconds()) // 60, 60)
            countdown_str = f" ({h}ч {m}м)" if h > 0 else f" ({m}м)" if m > 0 else " (<1м)"

        # Основная информация - ЧИСТО И ПОНЯТНО
        arrow = "🟢" if item['rate'] < 0 else "🔴"
        rate_str = f"{item['rate'] * 100:+.2f}%"
        time_str = funding_dt_utc.astimezone(MSK_TIMEZONE).strftime('%H:%M МСК')
        
        message_text += f"{arrow} **{symbol_only}** {rate_str} | 🕑 {time_str} {countdown_str} | {item['exchange']}\n"

        buttons.append(InlineKeyboardButton(symbol_only, callback_data=f"drill_{item['symbol']}"))

    message_text += "\n💡 *Хотите ИИ-анализ? Нажмите кнопку ниже* ↓"

    # Кнопки: детали монет + ИИ-анализ
    detail_buttons = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    ai_buttons = [
        [InlineKeyboardButton("🧠 ИИ-Анализ", callback_data="ai_analysis")],
        [InlineKeyboardButton("🔄 Обновить", callback_data="back_to_top")]
    ]
    
    keyboard = detail_buttons + ai_buttons
    await msg.edit_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown', disable_web_page_preview=True)

# ===== НОВАЯ ФУНКЦИЯ: ИИ-АНАЛИЗ =====
async def show_ai_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает экран с ИИ-анализом возможностей"""
    query = update.callback_query
    
    if not check_access(update.effective_user.id):
        await query.answer("⛔ Доступ запрещён", show_alert=True)
        return
        
    await query.answer()
    await query.edit_message_text("🧠 Анализирую с помощью ИИ...")

    # Получаем сохраненные данные
    opportunities = context.chat_data.get('current_opportunities', [])
    
    if not opportunities:
        await query.edit_message_text(
            "❓ Нет данных для анализа. Сначала получите список возможностей.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад к топу", callback_data="back_to_top")]])
        )
        return

    # ===== ПРИМЕНЯЕМ ИИ-АНАЛИЗ =====
    print(f"[AI_ANALYSIS] Анализирую {len(opportunities)} возможностей...")
    
    analyzed_opportunities = []
    for item in opportunities:
        analyzed_item = await analyze_funding_opportunity(item)
        analyzed_opportunities.append(analyzed_item)
        print(f"[AI_ANALYSIS] {item['symbol']}: {analyzed_item['smart_recommendation']['message']}")

    # Формируем сообщение ИИ-анализа
    message_text = "🧠 **ИИ-Анализ возможностей**\n\n"
    message_text += "*Выберите монету для подробного анализа:*\n\n"
    
    buttons = []
    for item in analyzed_opportunities:
        symbol_only = item['symbol'].replace("USDT", "")
        rec = item['smart_recommendation']
        confidence = rec['confidence']
        
        # Определяем уровень уверенности простыми словами
        if confidence >= 0.8:
            confidence_text = "ИИ очень уверен"
        elif confidence >= 0.6:
            confidence_text = "ИИ довольно уверен"  
        elif confidence >= 0.4:
            confidence_text = "ИИ сомневается"
        else:
            confidence_text = "ИИ не уверен"
            
        message_text += f"{rec['emoji']} **{symbol_only}** - {rec['message']}\n"
        message_text += f"   _{confidence_text} ({confidence:.0%})_\n\n"

        buttons.append(InlineKeyboardButton(f"{rec['emoji']} {symbol_only}", callback_data=f"ai_detail_{item['symbol']}"))

    message_text += "💡 *Нажмите на монету для подробного анализа*"

    # Кнопки: выбор монет + назад
    coin_buttons = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    back_button = [[InlineKeyboardButton("⬅️ Назад к топу", callback_data="back_to_top")]]
    
    keyboard = coin_buttons + back_button
    await query.edit_message_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

# ===== НОВАЯ ФУНКЦИЯ: ДЕТАЛЬНЫЙ ИИ-АНАЛИЗ МОНЕТЫ =====
async def show_ai_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает детальный ИИ-анализ конкретной монеты"""
    query = update.callback_query
    
    if not check_access(update.effective_user.id):
        await query.answer("⛔ Доступ запрещён", show_alert=True)
        return
        
    symbol_to_analyze = query.data.split('_')[2]  # ai_detail_SYMBOLUSDT
    await query.answer()

    # Получаем сохраненные данные
    opportunities = context.chat_data.get('current_opportunities', [])
    target_item = None
    
    for item in opportunities:
        if item['symbol'] == symbol_to_analyze:
            target_item = item
            break
    
    if not target_item:
        await query.edit_message_text(
            "❓ Монета не найдена в текущем списке.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад к ИИ", callback_data="ai_analysis")]])
        )
        return

    # Применяем ИИ-анализ к конкретной монете
    analyzed_item = await analyze_funding_opportunity(target_item)
    symbol_only = symbol_to_analyze.replace("USDT", "")
    rec = analyzed_item['smart_recommendation']
    stability = analyzed_item['stability_analysis']
    
    # Формируем детальный анализ
    message_text = f"🧠 **ИИ-Анализ: {symbol_only}**\n\n"
    
    # Основные данные
    rate_pct = abs(target_item['rate']) * 100
    message_text += f"📈 **Ставка:** {target_item['rate'] * 100:+.2f}%\n"
    message_text += f"📊 **Тренд:** {stability['trend'].title()}\n"
    message_text += f"⚡ **Стабильность:** {stability['stability'].title()}\n"
    message_text += f"🎯 **Рекомендация:** {rec['message'].upper()}\n\n"
    
    # Объяснение что это значит
    message_text += "❓ **Что это значит?**\n"
    
    explanation_map = {
        'ideal_arbitrage': "Ставка стабильна и предсказуема. Низкий риск резких изменений. Хорошие условия для заработка на фандинге.",
        'risky_arbitrage': "Ставка нестабильна и может резко измениться. Риск потерь выше обычного. Торговать осторожно.",
        'contrarian_opportunity': "Ставка истощается - возможен разворот цены. Можно рассмотреть противоположную позицию после выплаты.",
        'unclear_signal': "Смешанные сигналы от ИИ. Ситуация неоднозначная. Лучше дождаться более четкого сигнала.",
        'rate_too_low': "Ставка слишком низкая для получения значимой прибыли. Не рекомендуется к торговле.",
        'insufficient_data': "Недостаточно исторических данных для точного анализа. ИИ не может дать надежную рекомендацию."
    }
    
    explanation = explanation_map.get(rec['recommendation_type'], "Требуется дополнительный анализ.")
    message_text += f"_{explanation}_\n\n"
    
    # Практические советы
    if rec['recommendation_type'] == 'ideal_arbitrage':
        message_text += "✅ **Что делать:**\n"
        message_text += "• Можно входить в позицию стандартным размером\n"
        message_text += "• Держать до выплаты фандинга\n"
        message_text += "• Риск минимален\n\n"
    elif rec['recommendation_type'] == 'risky_arbitrage':
        message_text += "⚠️ **Что делать:**\n"
        message_text += "• Используйте уменьшенный размер позиции\n"
        message_text += "• Установите тесный стоп-лосс\n"
        message_text += "• Следите за рынком внимательно\n\n"
    elif rec['recommendation_type'] == 'contrarian_opportunity':
        message_text += "🔥 **Что делать:**\n"
        message_text += "• Дождитесь выплаты фандинга\n"
        message_text += "• Рассмотрите противоположную позицию\n"
        message_text += "• Следите за разворотом тренда\n\n"
    else:
        message_text += "⏸️ **Что делать:**\n"
        message_text += "• Лучше пропустить эту возможность\n"
        message_text += "• Дождаться более четкого сигнала\n"
        message_text += "• Искать другие варианты\n\n"
    
    # Уверенность ИИ
    confidence = rec['confidence']
    if confidence >= 0.8:
        confidence_text = "очень уверен"
        confidence_explanation = "(как прогноз погоды с вероятностью 90%)"
    elif confidence >= 0.6:
        confidence_text = "довольно уверен"
        confidence_explanation = "(как мнение опытного трейдера)"
    elif confidence >= 0.4:
        confidence_text = "сомневается"
        confidence_explanation = "(смешанные сигналы)"
    else:
        confidence_text = "не уверен"
        confidence_explanation = "(мало данных для анализа)"
        
    message_text += f"🎯 **Уверенность ИИ:** {confidence:.0%}\n"
    message_text += f"_ИИ {confidence_text} {confidence_explanation}_"

    keyboard = [
        [InlineKeyboardButton("⬅️ Назад к ИИ-анализу", callback_data="ai_analysis")],
        [InlineKeyboardButton("🏠 К топу возможностей", callback_data="back_to_top")]
    ]
    
    await query.edit_message_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

# ===== ИСПРАВЛЕННЫЕ CALLBACK ФУНКЦИИ =====
async def drill_down_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    if not check_access(update.effective_user.id):
        await query.answer("⛔ Доступ запрещён", show_alert=True)
        return
        
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
            countdown_str = f" ({h}ч {m}м)" if h > 0 else f" ({m}м)" if m > 0 else " (<1м)"
        
        direction, rate_str = ("🟢 ЛОНГ", f"{item['rate'] * 100:+.2f}%") if item['rate'] < 0 else ("🔴 ШОРТ", f"{item['rate'] * 100:+.2f}%")
        time_str = funding_dt_utc.astimezone(MSK_TIMEZONE).strftime('%H:%M МСК')
        vol = item.get('volume_24h_usdt', Decimal('0'))
        vol_str = f"{vol/10**9:.1f}B" if vol >= 10**9 else f"{vol/10**6:.1f}M" if vol >= 10**6 else f"{vol/10**3:.0f}K"
        
        message_text += f"{direction} `{rate_str}` в `{time_str}{countdown_str}` [{item['exchange']}]({item['trade_url']})\n"
        message_text += f"  *Объем 24ч:* `{vol_str} USDT`\n"
        if (max_pos := item.get('max_order_value_usdt', Decimal('0'))) > 0: 
            message_text += f"  *Макс. ордер:* `{max_pos:,.0f}`\n"
        message_text += "\n"

    # Добавляем кнопку ИИ-анализа для этой конкретной монеты
    keyboard = [
        [InlineKeyboardButton("🧠 ИИ-Анализ этой монеты", callback_data=f"ai_detail_{symbol_to_show}")],
        [InlineKeyboardButton("⬅️ Назад к топу", callback_data="back_to_top")]
    ]
    await query.edit_message_text(text=message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown', disable_web_page_preview=True)get("data", [])
    if not all_data:
        await query.edit_message_text("🔄 Обновляю данные...")
        all_data = await fetch_all_data(context, force_update=True)
        
    symbol_data = sorted([item for item in all_data if item['symbol'] == symbol_to_show], key=lambda x: abs(x['rate']), reverse=True)
    symbol_only = symbol_to_show.replace("USDT", "")
    message_text = f"💎 **Детали по {symbol_only}**\n\n"
    now_utc = datetime.now(timezone.utc)
    
    for item in symbol_data:
        # Применяем умный анализ и к детальному просмотру
        analyzed_item = await analyze_funding_opportunity(item)
        rec = analyzed_item['smart_recommendation']
        
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
        
        confidence_str = f" ({rec['confidence']:.0%})" if rec['confidence'] > 0 else ""
        
        message_text += f"{direction} `{rate_str}` в `{time_str}{countdown_str}` [{item['exchange']}]({item['trade_url']})\n"
        message_text += f"  *Объем 24ч:* `{vol_str} USDT`\n"
        message_text += f"  {rec['emoji']} *ИИ:* _{rec['message']}{confidence_str}_\n"
        if (max_pos := item.get('max_order_value_usdt', Decimal('0'))) > 0: 
            message_text += f"  *Макс. ордер:* `{max_pos:,.0f}`\n"
        message_text += "\n"

    keyboard = [[InlineKeyboardButton("⬅️ Назад к топу", callback_data="back_to_top")]]
    await query.edit_message_text(text=message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown', disable_web_page_preview=True)

async def back_to_top_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    if not check_access(update.effective_user.id):
        await query.answer("⛔ Доступ запрещён", show_alert=True)
        return
        
    if query:
        await query.answer()
    await show_top_rates(update, context)

@require_access()
async def send_filters_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    ensure_user_settings(chat_id, user_id)
    settings = user_settings[chat_id]['settings']
    
    message_text = "🔧 **Настройки фильтров для ручного поиска**"
    keyboard = [
        [InlineKeyboardButton("🏦 Биржи", callback_data="filters_exchanges")],
        [InlineKeyboardButton(f"📈 Ставка: > {settings['funding_threshold']*100:.2f}%", callback_data="filters_funding")],
        [InlineKeyboardButton(f"💧 Объем: > {format_volume(settings['volume_threshold_usdt'])}", callback_data="filters_volume")],
        [InlineKeyboardButton("🚨 Настроить Уведомления", callback_data="alert_show_menu")],
        [InlineKeyboardButton("❌ Закрыть", callback_data="filters_close")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query:
        await update.callback_query.edit_message_text(message_text, reply_markup=reply_markup, parse_mode='Markdown')
    else:
        await update.message.reply_text(message_text, reply_markup=reply_markup, parse_mode='Markdown')
        
@require_access()
async def filters_menu_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_filters_menu(update, context)

async def filters_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    if not check_access(update.effective_user.id):
        await query.answer("⛔ Доступ запрещён", show_alert=True)
        return
        
    await query.answer()
    action = query.data.split('_', 1)[1]
    if action == "close":
        await query.message.delete()
    elif action == "toggle_notif":
        user_settings[update.effective_chat.id]['settings']['notifications_on'] ^= True
        await send_filters_menu(update, context)
    elif action == "exchanges":
        await show_exchanges_menu(update, context)

async def show_exchanges_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    if not check_access(update.effective_user.id):
        await query.answer("⛔ Доступ запрещён", show_alert=True)
        return
        
    active_exchanges = user_settings[query.message.chat_id]['settings']['exchanges']
    buttons = [InlineKeyboardButton(f"{'✅' if ex in active_exchanges else '⬜️'} {ex}", callback_data=f"exch_{ex}") for ex in ALL_AVAILABLE_EXCHANGES]
    keyboard = [buttons[i:i + 2] for i in range(0, len(buttons), 2)] + [[InlineKeyboardButton("⬅️ Назад", callback_data="exch_back")]]
    await query.edit_message_text("🏦 **Выберите биржи**", reply_markup=InlineKeyboardMarkup(keyboard))

async def exchanges_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    if not check_access(update.effective_user.id):
        await query.answer("⛔ Доступ запрещён", show_alert=True)
        return
        
    await query.answer()
    action = query.data.split('_', 1)[1]
    if action == "back": 
        await send_filters_menu(update, context)
    else:
        active_exchanges = user_settings[query.message.chat_id]['settings']['exchanges']
        if action in active_exchanges: 
            active_exchanges.remove(action)
        else: 
            active_exchanges.append(action)
        await show_exchanges_menu(update, context)

async def ask_for_value(update: Update, context: ContextTypes.DEFAULT_TYPE, setting_type: str, menu_to_return: callable):
    query = update.callback_query
    
    if not check_access(update.effective_user.id):
        await query.answer("⛔ Доступ запрещён", show_alert=True)
        return
        
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    await query.answer()
    ensure_user_settings(chat_id, user_id)
    settings = user_settings[chat_id]['settings']
    
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
    if not check_access(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещён")
        return
        
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    ensure_user_settings(chat_id, user_id)
    settings = user_settings[chat_id]['settings']
    
    try:
        value_str = update.message.text.strip().replace(",", ".").upper()
        if setting_type == 'funding' or setting_type == 'alert_rate':
            value = Decimal(value_str)
            if not (0 < value < 100): raise ValueError("Value out of range 0-100")
            key = 'funding_threshold' if setting_type == 'funding' else 'alert_rate_threshold'
            settings[key] = value / 100
        elif setting_type == 'volume':
            num_part = value_str.replace('K', '').replace('M', '')
            multiplier = 1000 if 'K' in value_str else 1_000_000 if 'M' in value_str else 1
            settings['volume_threshold_usdt'] = Decimal(num_part) * multiplier
        elif setting_type == 'alert_time':
            value = int(value_str)
            if value <= 0: raise ValueError("Value must be positive")
            settings['alert_time_window_minutes'] = value
    except (ValueError, TypeError, decimal.InvalidOperation):
        await update.message.reply_text("❌ Ошибка. Введите корректное значение. Попробуйте снова.", parse_mode='Markdown')
        return

    if 'prompt_message_id' in context.user_data:
        await context.bot.delete_message(chat_id, context.user_data.pop('prompt_message_id'))
    await context.bot.delete_message(chat_id, update.message.message_id)
    await context.user_data.pop('menu_to_return')(update, context)
    return ConversationHandler.END

async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_access(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещён")
        return
        
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
    
@require_access()
async def show_my_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    ensure_user_settings(chat_id, user_id)
    settings = user_settings[chat_id]['settings']
    
    exchanges_list = ", ".join(settings['exchanges'])
    vol = settings['volume_threshold_usdt']
    vol_str = f"{vol / 1_000_000:.1f}M" if vol >= 1_000_000 else f"{vol / 1_000:.0f}K"
    
    message_text = f"""ℹ️ **Ваши текущие настройки:**

🏦 **Биржи:** {exchanges_list}
📈 **Минимальная ставка:** > {settings['funding_threshold']*100:.2f}%
💧 **Минимальный объем:** > {vol_str} USDT
📕 **Уведомления:** {'Включены' if settings['alerts_on'] else 'Выключены'}

Для изменения настроек используйте "🔧 Настроить фильтры"
"""
    await update.message.reply_text(message_text, parse_mode='Markdown')

# --- Блок для настройки уведомлений ---
async def show_alerts_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает меню настройки кастомных уведомлений."""
    
    if update.callback_query and not check_access(update.effective_user.id):
        await update.callback_query.answer("⛔ Доступ запрещён", show_alert=True)
        return
        
    if query := update.callback_query: 
        await query.answer()
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    ensure_user_settings(chat_id, user_id)
    settings = user_settings[chat_id]['settings']
    
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
    query = update.callback_query
    
    if not check_access(update.effective_user.id):
        await query.answer("⛔ Доступ запрещён", show_alert=True)
        return
        
    action = query.data.split('_', 1)[1]
    
    await query.answer()
    if action == "toggle_on":
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        ensure_user_settings(chat_id, user_id)
        user_settings[chat_id]['settings']['alerts_on'] ^= True
        await show_alerts_menu(update, context)
    elif action == "back_filters":
        await send_filters_menu(update, context)

# ===== ИСПРАВЛЕННЫЙ ФОНОВЫЙ СКАНЕР =====
async def background_scanner(app: Application):
    """Фоновый процесс для мониторинга и отправки кастомных уведомлений."""
    print("🚀 Фоновый сканер уведомлений запущен.")
    while True:
        await asyncio.sleep(60)  # Проверка раз в минуту
        try:
            all_data = await fetch_all_data(app, force_update=True)
            if not all_data: 
                continue

            now_utc = datetime.now(timezone.utc)
            current_ts_ms = int(now_utc.timestamp() * 1000)

            # ===== ГЛАВНОЕ ИСПРАВЛЕНИЕ =====
            for chat_id, user_data in list(user_settings.items()):
                # Теперь правильно получаем user_id для проверки доступа
                stored_user_id = user_data.get('user_id')
                if not stored_user_id or not check_access(stored_user_id):
                    print(f"[BG_SCANNER] Пропускаем chat_id {chat_id}: нет доступа (user_id: {stored_user_id})")
                    continue
                    
                settings = user_data['settings']
                if not settings.get('alerts_on', False): 
                    continue

                # Очистка старых ID уведомлений (старше 3 часов)
                settings['sent_notifications'] = {nid for nid in settings['sent_notifications'] if int(nid.split('_')[-1]) > current_ts_ms - (3 * 60 * 60 * 1000)}
                
                # Ищем подходящие пары для этого пользователя
                for item in all_data:
                    if item['exchange'] not in settings['exchanges']: 
                        continue
                    if abs(item['rate']) < settings['alert_rate_threshold']: 
                        continue

                    time_left = datetime.fromtimestamp(item['next_funding_time'] / 1000, tz=timezone.utc) - now_utc
                    if not (0 < time_left.total_seconds() <= settings['alert_time_window_minutes'] * 60): 
                        continue

                    # Анти-спам
                    notification_id = f"{item['exchange']}_{item['symbol']}_{item['next_funding_time']}"
                    if notification_id in settings['sent_notifications']: 
                        continue
                    
                    # Все условия выполнены! Отправляем уведомление.
                    h, m = divmod(int(time_left.total_seconds() // 60), 60)
                    countdown_str = f"{h}ч {m}м" if h > 0 else f"{m}м"
                    message = (f"⚠️ **Найден фандинг по вашему фильтру!**\n\n"
                               f"{'🟢' if item['rate'] < 0 else '🔴'} **{item['symbol'].replace('USDT', '')}** `{item['rate'] * 100:+.2f}%`\n"
                               f"⏰ Выплата через *{countdown_str}* на *{item['exchange']}*")
                    try:
                        await app.bot.send_message(chat_id, message, parse_mode='Markdown')
                        settings['sent_notifications'].add(notification_id)
                        print(f"[BG_SCANNER] ✅ Отправлено уведомление для chat_id {chat_id} (user_id {stored_user_id}): {notification_id}")
                    except Exception as e:
                        print(f"[BG_SCANNER] ❌ Не удалось отправить уведомление для chat_id {chat_id}: {e}")
        except Exception as e:
            print(f"[BG_SCANNER] ❌ Критическая ошибка в цикле сканера: {e}\n{traceback.format_exc()}")

# Универсальный обработчик для неавторизованных пользователей
async def handle_unauthorized_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает сообщения от неавторизованных пользователей"""
    if not check_access(update.effective_user.id):
        await access_denied_message(update, context)
        return
    
    # Если пользователь авторизован, но сообщение не обработано другими хендлерами
    await update.message.reply_text(
        "🤖 Используйте кнопки меню или команду /start для начала работы."
    )

async def get_data_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ALLOWED_USERS:
        await update.message.reply_text("Эта команда доступна только администратору.")
        return

    message = await update.message.reply_text("Начинаю сбор данных по MYX_USDT за вчера. Это может занять до минуты...")
    
    # Определяем символ и временной диапазон
    symbol_to_fetch = "MYX_USDT"
    today = datetime.utcnow().date()
    end_of_yesterday = datetime.combine(today, datetime.min.time())
    start_of_yesterday = end_of_yesterday - timedelta(days=1)
    start_ts_ms = int(start_of_yesterday.timestamp() * 1000)
    end_ts_ms = int(end_of_yesterday.timestamp() * 1000) - 1

    # Запускаем сбор данных
    funding_data = await fetch_funding_history_async(symbol_to_fetch, start_ts_ms, end_ts_ms)
    kline_data = await fetch_klines_async(symbol_to_fetch, start_ts_ms, end_ts_ms)

    if not funding_data and not kline_data:
        await message.edit_text("Не удалось получить данные. Возможно, по этой монете вчера не было торгов или фандинга.")
        return
        
    await message.edit_text("Данные собраны, формирую файлы...")

    # Отправляем файл с фандингом
    if funding_data:
        df_funding = pd.DataFrame(funding_data)
        json_buffer = io.StringIO()
        df_funding.to_json(json_buffer, orient="records", indent=4)
        json_buffer.seek(0)
        await context.bot.send_document(
            chat_id=user_id,
            document=io.BytesIO(json_buffer.read().encode()),
            filename="funding_history.json"
        )

    # Отправляем файл со свечами
    if kline_data:
        df_klines = pd.DataFrame(kline_data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        json_buffer = io.StringIO()
        df_klines.to_json(json_buffer, orient="records", indent=4)
        json_buffer.seek(0)
        await context.bot.send_document(
            chat_id=user_id,
            document=io.BytesIO(json_buffer.read().encode()),
            filename="klines_1m.json"
        )
    
    await message.edit_text("Готово! Файлы отправлены вам в личку.")

# =================================================================
# ========================== ЗАПУСК БОТА ==========================
# =================================================================

if __name__ == "__main__":
    if not BOT_TOKEN:
        raise ValueError("Не найден BOT_TOKEN. Убедитесь, что он задан в .env файле.")
    
    # Проверяем, что список разрешенных пользователей настроен
    if not ALLOWED_USERS or ALLOWED_USERS == [123456789, 987654321]:
        print("⚠️ ВНИМАНИЕ: Не забудьте изменить ALLOWED_USERS на ваши реальные Telegram ID!")
        print("   Для получения своего ID напишите боту @userinfobot")
    
    from telegram.ext import Application
    
    # 1. Создаем приложение
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    # 2. Загружаем ключи API
    app.bot_data['mexc_api_key'] = os.getenv("MEXC_API_KEY")
    app.bot_data['mexc_secret_key'] = os.getenv("MEXC_API_SECRET")
    app.bot_data['bybit_api_key'] = os.getenv("BYBIT_API_KEY")
    app.bot_data['bybit_secret_key'] = os.getenv("BYBIT_API_SECRET")

    if app.bot_data['bybit_api_key']: 
        print("✅ Ключи Bybit успешно загружены.")
    else: 
        print("⚠️ Ключи Bybit не найдены.")
    print("ℹ️ Ключи для MEXC (публичные данные) больше не требуются.")

    # --- 3. РЕГИСТРАЦИЯ ОБРАБОТЧИКОВ ---
    
    fallbacks = [CommandHandler("cancel", cancel_conversation)]

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
    
    regular_handlers = [
        CommandHandler("start", start),
        MessageHandler(filters.Regex("^🔥 Топ-ставки сейчас$"), show_top_rates),
        MessageHandler(filters.Regex("^🔧 Настроить фильтры$"), filters_menu_entry),
        MessageHandler(filters.Regex("^ℹ️ Мои настройки$"), show_my_settings),
        MessageHandler(filters.Regex("^🔧 Диагностика API$"), api_diagnostics),
        # Обработчики кнопок
        CallbackQueryHandler(filters_callback_handler, pattern="^filters_"),
        CallbackQueryHandler(drill_down_callback, pattern="^drill_"),
        CallbackQueryHandler(back_to_top_callback, pattern="^back_to_top$"),
        CallbackQueryHandler(exchanges_callback_handler, pattern="^exch_"),
        CallbackQueryHandler(show_alerts_menu, pattern="^alert_show_menu$"),
        CallbackQueryHandler(alert_callback_handler, pattern="^alert_"),
        # НОВЫЕ обработчики ИИ-анализа
        CallbackQueryHandler(show_ai_analysis, pattern="^ai_analysis$"),
        CallbackQueryHandler(show_ai_detail, pattern="^ai_detail_"),
        # Универсальный обработчик для всех остальных сообщений (должен быть последним)
        MessageHandler(filters.TEXT, handle_unauthorized_message),
    ]

    # Добавляем все обработчики в приложение
    app.add_handlers(conv_handlers)
    app.add_handlers(regular_handlers)
    app.add_handler(CommandHandler("getdata", get_data_command))

    # 4. Запуск фонового сканера
    async def post_init(app):
        asyncio.create_task(background_scanner(app))

    app.post_init = post_init

    # 5. Запускаем бота
    print("🤖 RateHunter 2.0 с ИИ-анализатором запущен с ограничением доступа!")
    print(f"🔑 Разрешенные пользователи: {ALLOWED_USERS}")
    print("🚀 Фоновый сканер для уведомлений активен!")
    print("🧠 Умный анализ funding rates включен!")
    app.run_polling()
