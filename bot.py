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
    518449824, 642874424,452364249  # Замените на свой Telegram ID
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

# ===== УЛУЧШЕННЫЙ МОДУЛЬ: АНАЛИЗАТОР ТРЕНДОВ FUNDING RATE =====

# <<< НАЧАЛО ПОЛНОСТЬЮ ИСПРАВЛЕННОГО БЛОКА АНАЛИЗАТОРА >>>

class EnhancedFundingTrendAnalyzer:
    """
    Улучшенный анализатор трендов funding rates с точными торговыми сигналами
    и всеми необходимыми функциями для получения данных.
    """
    
    def __init__(self):
        self.historical_cache = {}
        self.cache_lifetime_minutes = 30
        
    async def analyze_trading_opportunity(self, symbol: str, exchange: str, current_rate: Decimal) -> Dict:
        """
        Анализирует торговые возможности на основе трендов funding rate.
        """
        history = await self._get_funding_history_real(symbol, exchange, periods=10)
        
        if not history or len(history) < 3:
            return {'signal': 'insufficient_data', 'confidence': 0.0, 'recommendation': 'Недостаточно данных для анализа', 'trend_direction': 'unknown', 'trend_strength': 0.0, 'data_source': 'insufficient'}
        
        trend_analysis = self._analyze_detailed_trend(history, current_rate)
        stability_analysis = self._analyze_trend_stability(history, current_rate)
        trading_signal = self._generate_trading_signal(trend_analysis, stability_analysis, current_rate, history)
        
        return {
            'signal': trading_signal['signal'], 'confidence': trading_signal['confidence'],
            'recommendation': trading_signal['recommendation'], 'trend_direction': trend_analysis['direction'],
            'trend_strength': trend_analysis['strength'], 'recent_change': trend_analysis['recent_change_pct'],
            'momentum': trend_analysis['momentum'], 'stability_score': stability_analysis['score'],
            'data_points': len(history), 'data_source': 'real_api',
            'analysis_details': {'history': history[-5:], 'current_rate': float(current_rate), 'trend_changes': trend_analysis['trend_changes']},
            'change_text': trend_analysis.get('change_text', 'н/д')
        }
    
    def _analyze_detailed_trend(self, history: List[Decimal], current_rate: Decimal) -> Dict:
        """
        УЛУЧШЕННАЯ ВЕРСИЯ 2.0: Готовит текст "Было X, стало Y" для интерфейса.
        """
        all_rates = history + [current_rate]
        
        if len(all_rates) < 3:
            return {'direction': 'unknown', 'strength': 0.0, 'recent_change_pct': 0.0, 'momentum': 'flat', 'trend_changes': [], 'change_text': 'Недостаточно данных'}
        
        changes = []
        NEAR_ZERO_THRESHOLD = Decimal('0.0001')
        for i in range(1, len(all_rates)):
            prev_rate, curr_rate = all_rates[i-1], all_rates[i]
            if abs(prev_rate) < NEAR_ZERO_THRESHOLD:
                change_pct = 500.0 if curr_rate > prev_rate else -500.0 if abs(curr_rate) > NEAR_ZERO_THRESHOLD * 2 else 0.0
            else:
                change_pct = float((curr_rate - prev_rate) / abs(prev_rate) * 100)
            changes.append(change_pct)
        
        recent_changes = changes[-4:] if len(changes) >= 4 else changes[-3:] if len(changes) >= 3 else changes
        positive_changes = sum(1 for c in recent_changes if c > 0.1)
        negative_changes = sum(1 for c in recent_changes if c < -0.1)
        recent_change_pct = sum(recent_changes) if recent_changes else 0
        
        if positive_changes > negative_changes and recent_change_pct > 0.5: direction, strength = 'growing', min(1.0, positive_changes / len(recent_changes))
        elif negative_changes > positive_changes and recent_change_pct < -0.5: direction, strength = 'declining', min(1.0, negative_changes / len(recent_changes))
        else: direction, strength = 'stable', 0.5
        
        if len(recent_changes) >= 3:
            early_avg = sum(recent_changes[:len(recent_changes)//2]) / (len(recent_changes)//2) if len(recent_changes)//2 > 0 else 0
            late_avg = sum(recent_changes[len(recent_changes)//2:]) / (len(recent_changes) - len(recent_changes)//2) if (len(recent_changes) - len(recent_changes)//2) > 0 else 0
            if abs(late_avg) > abs(early_avg) * 1.2: momentum = 'accelerating'
            elif abs(late_avg) < abs(early_avg) * 0.8: momentum = 'decelerating'
            else: momentum = 'steady'
        else: momentum = 'steady'

        before_val_pct = all_rates[-2] * 100
        after_val_pct = all_rates[-1] * 100
        change_text = f"Было: {before_val_pct:+.3f}%, стало: {after_val_pct:+.3f}%"
        
        return {
            'direction': direction, 'strength': strength, 'recent_change_pct': recent_change_pct,
            'momentum': momentum, 'trend_changes': changes, 'change_text': change_text
        }

    def _analyze_trend_stability(self, history: List[Decimal], current_rate: Decimal) -> Dict:
        all_rates = history + [current_rate]
        if len(all_rates) < 3: return {'score': 0.0, 'level': 'unknown'}
        
        changes, pos_count, neg_count = [], 0, 0
        for i in range(1, len(all_rates)):
            if all_rates[i-1] != 0:
                change_pct = abs(float((all_rates[i] - all_rates[i-1]) / all_rates[i-1] * 100))
                changes.append(change_pct)
            if all_rates[i] > all_rates[i-1]: pos_count += 1
            elif all_rates[i] < all_rates[i-1]: neg_count += 1
        
        if not changes: return {'score': 0.0, 'level': 'unknown'}
        
        avg_change = sum(changes[-4:]) / len(changes[-4:]) if len(changes) >= 4 else sum(changes) / len(changes)
        consistency = max(pos_count, neg_count) / (len(all_rates) - 1)
        
        if consistency >= 0.7 and avg_change >= 0.1: score, level = min(1.0, consistency * 1.2), 'high'
        elif consistency >= 0.5: score, level = consistency, 'medium'
        else: score, level = consistency * 0.8, 'low'
        
        return {'score': score, 'level': level}
    
    def _generate_trading_signal(self, trend: Dict, stability: Dict, rate: Decimal, history: List[Decimal]) -> Dict:
        """
        КРИТИЧЕСКОЕ ИСПРАВЛЕНИЕ: Полностью переработана логика для соответствия правильной
        стратегии фандинг-арбитража.
        """
        # --- ОБЩИЕ ПРОВЕРКИ ---
        if abs(rate) < 0.003:
            return {'signal': 'rate_too_low', 'confidence': 0, 'recommendation': 'Ставка слишком низкая'}
        
        confidence = min(1.0, (stability['score'] + trend['strength']) / 2 + min(0.2, len(history) * 0.03))

        # === ПРАВИЛЬНАЯ ЛОГИКА ДЛЯ ЛОНГ ПОЗИЦИЙ (когда ставка ОТРИЦАТЕЛЬНАЯ) ===
        if rate < 0:
            # СИГНАЛ НА ВХОД В ЛОНГ: Ставка отрицательная и становится еще более отрицательной (это хорошо)
            if trend['direction'] == 'declining' and trend['strength'] >= 0.6 and trend['recent_change_pct'] < -1.0:
                if trend['momentum'] == 'accelerating': return {'signal': 'strong_long_entry', 'confidence': min(1.0, confidence*1.2), 'recommendation': '🚀 СИЛЬНЫЙ ЛОНГ: Ставка быстро падает (становится выгоднее).'}
                return {'signal': 'long_entry', 'confidence': confidence, 'recommendation': '📈 Вход в ЛОНГ: Ставка стабильно падает (становится выгоднее).'}

            # СИГНАЛ НА ВЫХОД ИЗ ЛОНГА: Ставка все еще отрицательная, но начала расти к нулю (это плохо)
            if trend['direction'] == 'growing' and trend['strength'] >= 0.6 and trend['recent_change_pct'] > 1.0:
                 return {'signal': 'long_exit', 'confidence': confidence, 'recommendation': '📉 Выход из ЛОНГА: Ставка растет к нулю, становится невыгодно.'}

            # СИГНАЛ ДЕРЖАТЬ ЛОНГ: Ставка отрицательная и стабильная
            if trend['direction'] in ['declining', 'stable'] and rate < -0.003 and trend['strength'] >= 0.4:
                return {'signal': 'hold_long', 'confidence': confidence*0.8, 'recommendation': '⏸️ ДЕРЖАТЬ ЛОНГ: Ставка остается выгодной и стабильной.'}

        # === ПРАВИЛЬНАЯ ЛОГИКА ДЛЯ ШОРТ ПОЗИЦИЙ (когда ставка ПОЛОЖИТЕЛЬНАЯ) ===
        if rate > 0:
            # СИГНАЛ НА ВХОД В ШОРТ: Ставка положительная и растет еще выше (это хорошо)
            if trend['direction'] == 'growing' and trend['strength'] >= 0.6 and trend['recent_change_pct'] > 1.0:
                if trend['momentum'] == 'accelerating': return {'signal': 'strong_short_entry', 'confidence': min(1.0, confidence*1.2), 'recommendation': '🎯 СИЛЬНЫЙ ШОРТ: Ставка быстро растет (становится выгоднее).'}
                return {'signal': 'short_entry', 'confidence': confidence, 'recommendation': '📉 Вход в ШОРТ: Ставка стабильно растет (становится выгоднее).'}
            
            # СИГНАЛ НА ВЫХОД ИЗ ШОРТА: Ставка все еще положительная, но начала падать к нулю (это плохо)
            if trend['direction'] == 'declining' and trend['strength'] >= 0.6 and trend['recent_change_pct'] < -1.0:
                return {'signal': 'short_exit', 'confidence': confidence, 'recommendation': '📈 Выход из ШОРТА: Ставка падает к нулю, становится невыгодно.'}

            # СИГНАЛ ДЕРЖАТЬ ШОРТ: Ставка положительная и стабильная
            if trend['direction'] in ['growing', 'stable'] and rate > 0.003 and trend['strength'] >= 0.4:
                return {'signal': 'hold_short', 'confidence': confidence*0.8, 'recommendation': '⏸️ ДЕРЖАТЬ ШОРТ: Ставка остается выгодной и стабильной.'}
        
        # Если ни одно из правил не сработало, значит тренд неясен
        return {'signal': 'wait', 'confidence': confidence*0.5, 'recommendation': '⏱️ ОЖИДАНИЕ: Тренд неясен, нет четкого сигнала.'}
    
    # --- НЕДОСТАЮЩИЕ ФУНКЦИИ, КОТОРЫЕ МЫ ВОЗВРАЩАЕМ ---
    async def _get_funding_history_real(self, symbol: str, exchange: str, periods: int = 10) -> List[Decimal]:
        cache_key = f"{exchange}_{symbol}"
        now = time.time()
        if cache_key in self.historical_cache:
            cached_data, cached_time = self.historical_cache[cache_key]
            if now - cached_time < self.cache_lifetime_minutes * 60:
                return cached_data
        
        if exchange.upper() == 'MEXC': history = await self._fetch_mexc_funding_history(symbol)
        elif exchange.upper() == 'BYBIT': history = await self._fetch_bybit_funding_history(symbol)
        else: history = []
        
        if history: self.historical_cache[cache_key] = (history, now)
        return history

    async def _fetch_mexc_funding_history(self, symbol: str) -> List[Decimal]:
        mexc_symbol = symbol.replace('USDT', '_USDT')
        url = "https://contract.mexc.com/api/v1/contract/funding_rate/history"
        params = {'symbol': mexc_symbol, 'page_size': 15}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=10) as response:
                    if response.status != 200: return []
                    data = await response.json()
                    if not data.get('success'): return []
                    api_data = data.get('data', {})
                    funding_data = api_data.get('resultList', [])
                    if not funding_data: return []
                    rates = [Decimal(str(item.get('fundingRate', 0))) for item in funding_data]
                    rates.reverse()
                    return rates
        except Exception: return []

    async def _fetch_bybit_funding_history(self, symbol: str) -> List[Decimal]:
        url = "https://api.bybit.com/v5/market/funding/history"
        params = {'category': 'linear', 'symbol': symbol, 'limit': 15}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=10) as response:
                    if response.status != 200: return []
                    data = await response.json()
                    if data.get('retCode') != 0: return []
                    result_list = data.get('result', {}).get('list', [])
                    if not result_list: return []
                    rates = [Decimal(str(item.get('fundingRate', 0))) for item in result_list]
                    rates.reverse()
                    return rates
        except Exception: return []

# Создаем глобальный экземпляр улучшенного анализатора
enhanced_funding_analyzer = EnhancedFundingTrendAnalyzer()

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
        'alert_exchanges': [],
        'sent_notifications': set(),
        
        # === НОВЫЕ ПАРАМЕТРЫ ДЛЯ ИИ-СИГНАЛОВ ===
        'ai_signals_on': False,                         # включить/выключить ИИ-сигналы
        'ai_confidence_threshold': Decimal('0.6'),      # минимальная уверенность ИИ (60%)
        'ai_entry_signals': True,                       # сигналы входа в позицию
        'ai_exit_signals': True,                        # сигналы выхода из позиции
        'ai_sent_notifications': set(),                 # отдельный антиспам для ИИ-сигналов
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

async def get_binance_data():
    """Получает данные по ставкам финансирования с Binance Futures."""
    results = []
    # Эндпоинты API Binance
    funding_rate_url = "https://fapi.binance.com/fapi/v1/premiumIndex"
    ticker_url = "https://fapi.binance.com/fapi/v1/ticker/24hr"

    try:
        print("[DEBUG] Binance: Запрашиваем данные по ставкам и тикерам...")
        async with aiohttp.ClientSession() as session:
            # Асинхронно запрашиваем оба эндпоинта
            async with session.get(funding_rate_url, timeout=15) as funding_response, \
                       session.get(ticker_url, timeout=15) as ticker_response:
                
                if funding_response.status != 200:
                    print(f"[API_ERROR] Binance Funding: Статус {funding_response.status}")
                    return []
                if ticker_response.status != 200:
                    print(f"[API_ERROR] Binance Ticker: Статус {ticker_response.status}")
                    return []

                funding_data = await funding_response.json()
                ticker_data = await ticker_response.json()

                # 1. Создаем словарь для быстрого доступа к данным по фандингу
                funding_info = {}
                for item in funding_data:
                    symbol = item.get("symbol")
                    if symbol and item.get("lastFundingRate"):
                        try:
                            funding_info[symbol] = {
                                'rate': Decimal(str(item["lastFundingRate"])),
                                'next_funding_time': int(item["nextFundingTime"])
                            }
                        except (TypeError, ValueError, decimal.InvalidOperation):
                            continue # Пропускаем, если данные некорректны
                
                print(f"[DEBUG] Binance: Обработано {len(funding_info)} ставок фандинга.")

                # 2. Проходим по данным тикеров и объединяем их с данными фандинга
                print(f"[DEBUG] Binance: Получено {len(ticker_data)} тикеров.")
                for ticker in ticker_data:
                    symbol = ticker.get("symbol")
                    # Проверяем, что для этого тикера есть данные по фандингу
                    if symbol in funding_info:
                        try:
                            # Собираем все данные в стандартный формат
                            results.append({
                                'exchange': 'Binance', 
                                'symbol': symbol, 
                                'rate': funding_info[symbol]['rate'], 
                                'next_funding_time': funding_info[symbol]['next_funding_time'], 
                                'volume_24h_usdt': Decimal(str(ticker.get("quoteVolume", "0"))), # quoteVolume - это объем в USDT
                                'trade_url': f'https://www.binance.com/en/futures/{symbol}'
                            })
                        except (TypeError, ValueError, decimal.InvalidOperation, KeyError) as e:
                            print(f"[DEBUG] Binance: Ошибка обработки тикера {symbol}: {e}")
                            continue
                
                print(f"[DEBUG] Binance: Успешно сформировано {len(results)} инструментов.")

    except asyncio.TimeoutError:
        print("[API_ERROR] Binance: Timeout при запросе к API")
    except Exception as e:
        print(f"[API_ERROR] Binance: Глобальное исключение {type(e).__name__}: {e}")
        print(f"[API_ERROR] Binance: Traceback: {traceback.format_exc()}")
    
    return results

async def get_okx_data():
    """Получает данные по ставкам финансирования и ОИ с OKX."""
    results = []
    base_url = "https://www.okx.com"
    
    try:
        print("[DEBUG] OKX: Запрашиваем данные...")
        async with aiohttp.ClientSession() as session:
            
            # 1. Получаем список всех perpetual-свопов
            instruments_url = f"{base_url}/api/v5/public/instruments?instType=SWAP"
            async with session.get(instruments_url, timeout=15) as response:
                if response.status != 200:
                    print(f"[API_ERROR] OKX Instruments: Статус {response.status}")
                    return []
                inst_json = await response.json()
                if inst_json.get('code') != '0':
                     print(f"[API_ERROR] OKX Instruments: API вернул ошибку: {inst_json.get('msg')}")
                     return []
                instruments_data = inst_json.get('data', [])
            
            usdt_swaps = [inst['instId'] for inst in instruments_data if inst.get('settleCcy') == 'USDT']
            if not usdt_swaps:
                print("[API_ERROR] OKX: Не найдено USDT-свопов.")
                return []
            
            print(f"[DEBUG] OKX: Найдено {len(usdt_swaps)} USDT-свопов.")
            
            # 2. Получаем данные тикеров и ОИ (они работают без instId)
            ticker_info = {}
            oi_info = {}
            
            # 2.1: Получаем все тикеры
            try:
                async with session.get(f"{base_url}/api/v5/public/tickers?instType=SWAP", timeout=20) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get('code') == '0':
                            for item in data.get('data', []):
                                inst_id = item.get('instId')
                                if inst_id in usdt_swaps:
                                    ticker_info[inst_id] = Decimal(str(item.get('volCcy24h', '0')))
                            print(f"[DEBUG] OKX: Получено {len(ticker_info)} тикеров")
                        else:
                            print(f"[API_ERROR] OKX Тикеры: {data.get('msg')}")
                    else:
                        print(f"[API_ERROR] OKX Тикеры HTTP: {resp.status}")
            except Exception as e:
                print(f"[API_ERROR] OKX Тикеры Exception: {e}")

            # 2.2: Получаем открытый интерес
            try:
                async with session.get(f"{base_url}/api/v5/public/open-interest?instType=SWAP", timeout=20) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get('code') == '0':
                            for item in data.get('data', []):
                                inst_id = item.get('instId')
                                if inst_id in usdt_swaps:
                                    oi_info[inst_id] = Decimal(str(item.get('oiCcy', '0')))
                            print(f"[DEBUG] OKX: Получено {len(oi_info)} данных по ОИ")
                        else:
                            print(f"[API_ERROR] OKX ОИ: {data.get('msg')}")
                    else:
                        print(f"[API_ERROR] OKX ОИ HTTP: {resp.status}")
            except Exception as e:
                print(f"[API_ERROR] OKX ОИ Exception: {e}")

            # 3. БЫСТРЫЙ МЕТОД: Берем только топовые инструменты по объему
            # Сортируем по объему и берем только топ-100
            top_instruments = sorted(
                [(inst_id, ticker_info.get(inst_id, Decimal('0'))) for inst_id in usdt_swaps], 
                key=lambda x: x[1], 
                reverse=True
            )[:100]  # Только топ-100 по объему
            
            selected_instruments = [inst_id for inst_id, _ in top_instruments]
            print(f"[DEBUG] OKX: Выбрано {len(selected_instruments)} топовых инструментов по объему")

            # 4. ПАРАЛЛЕЛЬНЫЕ ЗАПРОСЫ для фандинга
            async def get_funding_for_instrument(inst_id):
                try:
                    async with session.get(f"{base_url}/api/v5/public/funding-rate?instId={inst_id}", timeout=8) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if data.get('code') == '0' and data.get('data'):
                                item = data['data'][0]
                                return inst_id, {
                                    'rate': Decimal(str(item['fundingRate'])),
                                    'next_funding_time': int(item['nextFundingTime'])
                                }
                except Exception:
                    pass
                return inst_id, None

            # Запускаем параллельно по 20 запросов за раз
            funding_info = {}
            semaphore = asyncio.Semaphore(20)  # Ограничиваем до 20 параллельных запросов
            
            async def bounded_request(inst_id):
                async with semaphore:
                    return await get_funding_for_instrument(inst_id)
            
            print(f"[DEBUG] OKX: Запускаем параллельные запросы для {len(selected_instruments)} инструментов...")
            
            # Выполняем все запросы параллельно
            tasks = [bounded_request(inst_id) for inst_id in selected_instruments]
            results_funding = await asyncio.gather(*tasks, return_exceptions=True)
            
            # Собираем результаты
            successful = 0
            for result in results_funding:
                if isinstance(result, tuple) and result[1] is not None:
                    inst_id, funding_data = result
                    funding_info[inst_id] = funding_data
                    successful += 1
            
            print(f"[DEBUG] OKX: Получено {successful} ставок фандинга из {len(selected_instruments)} запросов")

            # 5. Формируем финальный результат
            for inst_id in funding_info.keys():
                symbol = inst_id.replace("-SWAP", "").replace("-", "")
                trade_symbol = inst_id.replace("-SWAP", "")
                
                results.append({
                    'exchange': 'OKX', 
                    'symbol': symbol, 
                    'rate': funding_info[inst_id]['rate'],
                    'next_funding_time': funding_info[inst_id]['next_funding_time'],
                    'volume_24h_usdt': ticker_info.get(inst_id, Decimal('0')),
                    'open_interest_usdt': oi_info.get(inst_id, Decimal('0')),
                    'trade_url': f'https://www.okx.com/trade-swap/{trade_symbol}'
                })

            print(f"[DEBUG] OKX: Успешно сформировано {len(results)} инструментов.")

    except Exception as e:
        print(f"[API_ERROR] OKX: Глобальное исключение {type(e).__name__}: {e}")
        print(f"[API_ERROR] OKX: Traceback: {traceback.format_exc()}")
        
    return results

# Вставьте этот код после функции get_okx_data

async def get_kucoin_data():
    """Получает данные по ставкам финансирования с KuCoin Futures."""
    results = []
    base_url = "https://api-futures.kucoin.com"
    
    try:
        print("[DEBUG] KuCoin: Запрашиваем данные по активным контрактам...")
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base_url}/api/v1/contracts/active", timeout=15) as response:
                if response.status != 200:
                    print(f"[API_ERROR] KuCoin: Статус {response.status}")
                    return []
                
                response_json = await response.json()
                if response_json.get('code') != '200000':
                    print(f"[API_ERROR] KuCoin: API вернул ошибку: {response_json.get('msg')}")
                    return []
                
                contracts_data = response_json.get('data', [])
                print(f"[DEBUG] KuCoin: Получено {len(contracts_data)} контрактов.")
                
                for item in contracts_data:
                    # Нас интересуют только USDT-margin perpetual контракты
                    if (item.get('quoteCurrency') == 'USDT' and 
                        item.get('isInverse') is False and 
                        item.get('status') == 'Open'):
                        try:
                            # Преобразуем funding rate в правильный формат
                            funding_rate = Decimal(str(item.get('fundingFeeRate', '0')))
                            next_funding = int(item.get('nextFundingRateTime', 0))
                            
                            # У KuCoin объем может быть в turnoverOf24h (USDT) или volumeOf24h (базовая валюта)
                            volume_usdt = Decimal(str(item.get('turnoverOf24h', '0')))
                            if volume_usdt == 0:
                                # Если turnoverOf24h = 0, пробуем volumeOf24h * markPrice
                                volume_base = Decimal(str(item.get('volumeOf24h', '0')))
                                mark_price = Decimal(str(item.get('markPrice', '0')))
                                volume_usdt = volume_base * mark_price
                            
                            results.append({
                                'exchange': 'KuCoin', 
                                'symbol': item.get('symbol'), 
                                'rate': funding_rate, 
                                'next_funding_time': next_funding, 
                                'volume_24h_usdt': volume_usdt,
                                'open_interest_usdt': Decimal('0'),  # KuCoin не предоставляет ОИ в USDT напрямую
                                'trade_url': f'https://www.kucoin.com/futures/trade/{item.get("symbol")}'
                            })
                        except (TypeError, ValueError, decimal.InvalidOperation, KeyError) as e:
                            print(f"[DEBUG] KuCoin: Ошибка обработки контракта {item.get('symbol', 'unknown')}: {e}")
                            continue
                
                print(f"[DEBUG] KuCoin: Успешно сформировано {len(results)} инструментов.")

    except asyncio.TimeoutError:
        print("[API_ERROR] KuCoin: Timeout при запросе к API")
    except Exception as e:
        print(f"[API_ERROR] KuCoin: Глобальное исключение {type(e).__name__}: {e}")
        print(f"[API_ERROR] KuCoin: Traceback: {traceback.format_exc()}")
    
    return results

async def get_bitget_data():
    """Получает данные по ставкам финансирования с Bitget."""
    results = []
    # Эндпоинт API Bitget для получения тикеров по USDT-M контрактам
    tickers_url = "https://api.bitget.com/api/v2/mix/market/tickers?productType=USDT-FUTURES"

    try:
        print("[DEBUG] Bitget: Запрашиваем данные по тикерам...")
        async with aiohttp.ClientSession() as session:
            async with session.get(tickers_url, timeout=15) as response:
                if response.status != 200:
                    print(f"[API_ERROR] Bitget: Статус {response.status}")
                    return []
                
                response_json = await response.json()
                if response_json.get('code') != '00000':
                    print(f"[API_ERROR] Bitget: API вернул ошибку: {response_json.get('msg')}")
                    return []
                
                tickers_data = response_json.get('data', [])
                print(f"[DEBUG] Bitget: Получено {len(tickers_data)} инструментов.")
                
                for item in tickers_data:
                    try:
                        # Bitget отдает время в секундах, а нам нужны миллисекунды
                        next_funding_time_ms = int(item.get('nextFundingTime', 0))

                        # Собираем все данные в стандартный формат
                        results.append({
                            'exchange': 'Bitget', 
                            'symbol': item.get('symbol'), # Символ уже в формате BTCUSDT
                            'rate': Decimal(str(item.get('fundingRate', '0'))), 
                            'next_funding_time': next_funding_time_ms, 
                            'volume_24h_usdt': Decimal(str(item.get('volume24h', '0'))), # Объем в USDT
                            # У Bitget нет простого способа получить ОИ в USDT, оставляем 0
                            'open_interest_usdt': Decimal('0'),
                            'trade_url': f'https://www.bitget.com/futures/usdt/{item.get("symbol")}'
                        })
                    except (TypeError, ValueError, decimal.InvalidOperation, KeyError) as e:
                        print(f"[DEBUG] Bitget: Ошибка обработки инструмента {item.get('symbol')}: {e}")
                        continue
                
                print(f"[DEBUG] Bitget: Успешно сформировано {len(results)} инструментов.")

    except asyncio.TimeoutError:
        print("[API_ERROR] Bitget: Timeout при запросе к API")
    except Exception as e:
        print(f"[API_ERROR] Bitget: Глобальное исключение {type(e).__name__}: {e}")
        print(f"[API_ERROR] Bitget: Traceback: {traceback.format_exc()}")
    
    return results

async def get_gateio_data():
    """Получает данные по ставкам финансирования с Gate.io."""
    results = []
    # Эндпоинт API Gate.io для получения информации по USDT контрактам
    tickers_url = "https://api.gateio.ws/api/v4/futures/usdt/tickers"

    try:
        print("[DEBUG] Gate.io: Запрашиваем данные по тикерам...")
        async with aiohttp.ClientSession() as session:
            async with session.get(tickers_url, timeout=15) as response:
                if response.status != 200:
                    print(f"[API_ERROR] Gate.io: Статус {response.status}")
                    return []
                
                tickers_data = await response.json()
                print(f"[DEBUG] Gate.io: Получено {len(tickers_data)} инструментов.")
                
                for item in tickers_data:
                    try:
                        # Gate.io отдает время следующей выплаты в секундах, переводим в миллисекунды
                        next_funding_time_ms = int(item.get('funding_next_apply', 0)) * 1000

                        # Собираем все данные в стандартный формат
                        results.append({
                            'exchange': 'Gate.io', 
                            'symbol': item.get('contract').replace('_', ''), # Символ в формате BTC_USDT
                            'rate': Decimal(str(item.get('funding_rate', '0'))), 
                            'next_funding_time': next_funding_time_ms, 
                            'volume_24h_usdt': Decimal(str(item.get('volume_24h_usdt', '0'))),
                            # ОИ у Gate.io доступен, но в контрактах, а не в USDT. Для простоты пока ставим 0.
                            'open_interest_usdt': Decimal('0'),
                            'trade_url': f'https://www.gate.io/futures_trade/USDT/{item.get("contract")}'
                        })
                    except (TypeError, ValueError, decimal.InvalidOperation, KeyError) as e:
                        print(f"[DEBUG] Gate.io: Ошибка обработки инструмента {item.get('contract')}: {e}")
                        continue
                
                print(f"[DEBUG] Gate.io: Успешно сформировано {len(results)} инструментов.")

    except asyncio.TimeoutError:
        print("[API_ERROR] Gate.io: Timeout при запросе к API")
    except Exception as e:
        print(f"[API_ERROR] Gate.io: Глобальное исключение {type(e).__name__}: {e}")
        print(f"[API_ERROR] Gate.io: Traceback: {traceback.format_exc()}")
    
    return results

async def get_htx_data():
    """Получает данные по ставкам финансирования с HTX (Huobi)."""
    results = []
    
    try:
        print("[DEBUG] HTX: Запрашиваем данные...")
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'application/json',
            'Content-Type': 'application/json'
        }
        
        async with aiohttp.ClientSession(headers=headers) as session:
            
            # Сначала получаем список всех контрактов
            contracts_url = "https://api.hbdm.com/linear-swap-api/v1/swap_contract_info"
            
            async with session.get(contracts_url, timeout=15) as response:
                if response.status != 200:
                    print(f"[API_ERROR] HTX Contracts: Статус {response.status}")
                    return []
                
                response_text = await response.text()
                contracts_data = json.loads(response_text)
                
                if contracts_data.get('status') != 'ok':
                    print(f"[API_ERROR] HTX Contracts: {contracts_data.get('err_msg')}")
                    return []
                
                # Получаем список USDT контрактов
                usdt_contracts = [
                    item.get('contract_code') 
                    for item in contracts_data.get('data', []) 
                    if item.get('contract_code', '').endswith('-USDT')
                ]
                
                print(f"[DEBUG] HTX: Найдено {len(usdt_contracts)} USDT контрактов")
                
                if not usdt_contracts:
                    print("[API_ERROR] HTX: Не найдено USDT контрактов")
                    return []
            
            # Теперь получаем фандинг для каждого контракта
            for contract_code in usdt_contracts[:10]:  # Ограничиваем первыми 10 для теста
                try:
                    funding_url = f"https://api.hbdm.com/linear-swap-api/v1/swap_funding_rate?contract_code={contract_code}"
                    
                    async with session.get(funding_url, timeout=10) as fr_response:
                        if fr_response.status == 200:
                            fr_text = await fr_response.text()
                            fr_data = json.loads(fr_text)
                            
                            if fr_data.get('status') == 'ok' and fr_data.get('data'):
                                item = fr_data['data'][0]  # Берем первую запись
                                
                                symbol = contract_code.replace('-', '')  # BTC-USDT -> BTCUSDT
                                
                                results.append({
                                    'exchange': 'HTX',
                                    'symbol': symbol,
                                    'rate': Decimal(str(item.get('funding_rate', '0'))),
                                    'next_funding_time': int(item.get('next_funding_time', 0)),
                                    'volume_24h_usdt': Decimal('0'),
                                    'open_interest_usdt': Decimal('0'),
                                    'trade_url': f'https://www.htx.com/en-us/futures/usdt/{contract_code.lower()}'
                                })
                        
                        # Небольшая задержка между запросами
                        await asyncio.sleep(0.1)
                        
                except Exception as e:
                    print(f"[DEBUG] HTX: Ошибка для {contract_code}: {e}")
                    continue
                
            print(f"[DEBUG] HTX: Успешно сформировано {len(results)} инструментов.")

    except Exception as e:
        print(f"[API_ERROR] HTX: Глобальное исключение {type(e).__name__}: {e}")
    
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
        get_mexc_data(api_key=mexc_api_key, secret_key=mexc_secret_key),
        get_binance_data(),
        get_okx_data(),
        get_kucoin_data(),
        get_bitget_data(),
        get_gateio_data(),
        get_htx_data()
    ]
    results_from_tasks = await asyncio.gather(*tasks, return_exceptions=True)
    
    all_data = []
    for i, res in enumerate(results_from_tasks):
        exchange_name = ['Bybit', 'MEXC', 'Binance', 'OKX', 'KuCoin', 'Bitget', 'Gate.io', 'HTX'][i]
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
    report += f"\n🕐 Кэш действителен: {CACHE_LIFETIME_SECONDS} сек"
    
    report += "\n\n🔑 **Статус ключей:**\n"
    mexc_key = context.bot_data.get('mexc_api_key')
    bybit_key = context.bot_data.get('bybit_api_key')
    
    report += f"{'✅' if mexc_key else '❌'} MEXC: {'Настроены' if mexc_key else 'Отсутствуют'}\n"
    report += f"{'✅' if bybit_key else '❌'} Bybit: {'Настроены' if bybit_key else 'Отсутствуют'}\n"
    
    await msg.edit_text(report, parse_mode='Markdown')

# ===== НОВАЯ ФУНКЦИЯ: УМНЫЙ АНАЛИЗ ВОЗМОЖНОСТЕЙ =====
async def analyze_funding_opportunity(item: Dict) -> Dict:
    """
    ФИНАЛЬНАЯ ВЕРСИЯ: Улучшенные, интуитивно понятные эмодзи и описания сигналов.
    """
    analysis = await enhanced_funding_analyzer.analyze_trading_opportunity(
        symbol=item['symbol'],
        exchange=item['exchange'], 
        current_rate=item['rate']
    )
    
    item['enhanced_analysis'] = analysis
    signal = analysis['signal']
    confidence = analysis['confidence']
    
    # === НОВАЯ, УЛУЧШЕННАЯ КАРТА СИГНАЛОВ ===
    signal_map = {
        'strong_long_entry':  {'emoji': '🚀', 'message': 'Сильный ЛОНГ',   'details': 'Ставка быстро падает, открывайте ЛОНГ.'},
        'long_entry':         {'emoji': '🟢', 'message': 'Вход в ЛОНГ',     'details': 'Ставка стабильно падает, рассмотрите ЛОНГ.'},
        'hold_long':          {'emoji': '💰', 'message': 'Держать ЛОНГ',    'details': 'Продолжайте получать выплаты.'},
        'long_exit':          {'emoji': '⚠️', 'message': 'Выход из ЛОНГА',   'details': 'Тренд разворачивается, закройте ЛОНГ.'},
        
        'strong_short_entry': {'emoji': '🔥', 'message': 'Сильный ШОРТ',  'details': 'Ставка быстро растет, открывайте ШОРТ.'},
        'short_entry':        {'emoji': '🔴', 'message': 'Вход в ШОРТ',    'details': 'Ставка стабильно растет, рассмотрите ШОРТ.'},
        'hold_short':         {'emoji': '💰', 'message': 'Держать ШОРТ',   'details': 'Продолжайте получать выплаты.'},
        'short_exit':         {'emoji': '⚠️', 'message': 'Выход из ШОРТА',  'details': 'Тренд разворачивается, закройте ШОРТ.'},
        
        'wait':               {'emoji': '⏱️', 'message': 'Ожидание',       'details': 'Тренд неясен, ждем лучшего момента.'},
        'rate_too_low':       {'emoji': '📉', 'message': 'Ставка низкая',    'details': 'Слишком низкая для торговли.'},
        'insufficient_data':  {'emoji': '❓', 'message': 'Мало данных',      'details': 'Недостаточно истории для анализа.'}
    }
    
    signal_info = signal_map.get(signal, {'emoji': '❓', 'message': 'Анализ...', 'details': 'Обработка данных'})
    
    item['smart_recommendation'] = {
        'emoji': signal_info['emoji'],
        'message': signal_info['message'],
        'details': signal_info['details'],
        'confidence': confidence,
        'recommendation_type': signal
    }
    
    item['enhanced_recommendation'] = {
        'signal_type': signal,
        'trend_direction': analysis.get('trend_direction', 'unknown'),
        'trend_strength': analysis.get('trend_strength', 0.0),
        'recent_change': analysis.get('recent_change', 0.0),
        'momentum': analysis.get('momentum', 'steady'),
        'full_recommendation': analysis.get('recommendation', ''),
        'data_points': analysis.get('data_points', 0)
    }
    
    return item
@require_access()
async def show_top_rates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    ОБНОВЛЕННАЯ ВЕРСИЯ: Показывает топ возможностей с торговыми сигналами
    """
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    ensure_user_settings(chat_id, user_id)
    settings = user_settings[chat_id]['settings']

    msg = update.callback_query.message if update.callback_query else await update.message.reply_text("🔄 Ищу...")
    await msg.edit_text("🔄 Ищу лучшие возможности с ИИ-анализом...")

    all_data = await fetch_all_data(context)
    if not all_data:
        await msg.edit_text("😞 Не удалось получить данные с бирж. Попробуйте 🔧 Диагностика API для проверки.")
        return

    exchange_filtered = [item for item in all_data if item['exchange'] in settings['exchanges']]
    rate_filtered = [item for item in exchange_filtered if abs(item['rate']) >= settings['funding_threshold']]
    filtered_data = []
    for item in rate_filtered:
        volume = item.get('volume_24h_usdt', Decimal('0'))
        # Если объем есть - проверяем фильтр, если нет - пропускаем
        if volume == Decimal('0') or volume >= settings['volume_threshold_usdt']:
           filtered_data.append(item)
    
    if not filtered_data:
        stats_msg = f"😞 Не найдено пар, соответствующих всем фильтрам.\n\n"
        stats_msg += f"📊 **Статистика:**\n"
        stats_msg += f"• Всего инструментов: {len(all_data)}\n"
        stats_msg += f"• На выбранных биржах: {len(exchange_filtered)}\n"
        stats_msg += f"• Со ставкой ≥ {settings['funding_threshold']*100:.1f}%: {len(rate_filtered)}\n"
        stats_msg += f"• С объемом ≥ {settings['volume_threshold_usdt']/1_000:.0f}K: {len(filtered_data)}\n"
        await msg.edit_text(stats_msg, parse_mode='Markdown')
        return

    symbol_groups = {}
    for item in filtered_data:
        symbol = item['symbol']
        if symbol not in symbol_groups:
            symbol_groups[symbol] = []
        symbol_groups[symbol].append(item)
    
    unique_opportunities = [max(items, key=lambda x: abs(x['rate'])) for items in symbol_groups.values()]
    unique_opportunities.sort(key=lambda x: abs(x['rate']), reverse=True)
    top_5 = unique_opportunities[:5]
    
    analyzed_opportunities = []
    for item in top_5:
        analyzed_item = await analyze_funding_opportunity(item)
        analyzed_opportunities.append(analyzed_item)
    
    context.user_data['current_opportunities'] = analyzed_opportunities
    context.user_data['all_symbol_data'] = symbol_groups

    message_text = f"🔥 **ТОП-5 фандинг возможностей с ИИ-сигналами**\n\n"
    buttons = []
    now_utc = datetime.now(timezone.utc)
    
    for item in analyzed_opportunities:
        symbol_only = item['symbol'].replace("USDT", "")
        smart_rec = item.get('smart_recommendation', {})
        funding_dt_utc = datetime.fromtimestamp(item['next_funding_time'] / 1000, tz=timezone.utc)
        time_left = funding_dt_utc - now_utc
        countdown_str = ""
        if time_left.total_seconds() > 0:
            h, m = divmod(int(time_left.total_seconds()) // 60, 60)
            countdown_str = f" ({h}ч {m}м)" if h > 0 else f" ({m}м)"

        direction_emoji = "🟢" if item['rate'] < 0 else "🔴"
        rate_str = f"{item['rate'] * 100:+.2f}%"
        time_str = funding_dt_utc.astimezone(MSK_TIMEZONE).strftime('%H:%M МСК')
        ai_emoji = smart_rec.get('emoji', '❓')
        ai_message = smart_rec.get('message', 'Анализ...')
        confidence = smart_rec.get('confidence', 0.0)
        confidence_str = f" ({confidence:.0%})" if confidence > 0 else ""
        
        message_text += f"{direction_emoji} **{symbol_only}** {rate_str} | 🕒 {time_str}{countdown_str} | {item['exchange']}\n"
        message_text += f"  {ai_emoji} *ИИ:* _{ai_message}{confidence_str}_\n\n"
        buttons.append(InlineKeyboardButton(f"{ai_emoji} {symbol_only}", callback_data=f"drill_{item['symbol']}"))

    message_text += "\n💡 *Нажмите на монету для детального просмотра*"
    detail_buttons = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    action_buttons = [
        [InlineKeyboardButton("🧠 Подробный ИИ-анализ", callback_data="ai_analysis")],
        [InlineKeyboardButton("🔄 Обновить", callback_data="back_to_top")]
    ]
    keyboard = detail_buttons + action_buttons
    await msg.edit_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown', disable_web_page_preview=True)

# ===== НОВАЯ ФУНКЦИЯ: ИИ-АНАЛИЗ =====
async def show_ai_analysis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    ОБНОВЛЕННАЯ ВЕРСИЯ: Показывает экран с подробным ИИ-анализом торговых сигналов
    """
    query = update.callback_query
    if not check_access(update.effective_user.id):
        await query.answer("⛔ Доступ запрещён", show_alert=True)
        return
        
    await query.answer()
    await query.edit_message_text("🧠 Формирую подробный торговый анализ...")

    opportunities = context.user_data.get('current_opportunities', [])
    if not opportunities:
        await query.edit_message_text("❌ Нет данных для анализа.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="back_to_top")]]))
        return

    message_text = "🧠 **Подробный торговый анализ ИИ**\n\n"
    groups = {'strong': [], 'entry': [], 'exit': [], 'hold': [], 'wait': []}
    for item in opportunities:
        rec_type = item.get('smart_recommendation', {}).get('recommendation_type', 'wait')
        if 'strong' in rec_type: groups['strong'].append(item)
        elif 'entry' in rec_type: groups['entry'].append(item)
        elif 'exit' in rec_type: groups['exit'].append(item)
        elif 'hold' in rec_type: groups['hold'].append(item)
        else: groups['wait'].append(item)
    
    def format_group(title, items):
        text = f"{title}\n"
        for item in items:
            rec = item['smart_recommendation']
            text += f"{rec['emoji']} **{item['symbol'].replace('USDT','')}** `{item['rate']*100:+.2f}%` - {rec['message']} ({rec['confidence']:.0%})\n"
        return text + "\n"

    if groups['strong']: message_text += format_group("🚀 **ПРИОРИТЕТНЫЕ СИГНАЛЫ:**", groups['strong'])
    if groups['entry']: message_text += format_group("📊 **СИГНАЛЫ ВХОДА:**", groups['entry'])
    if groups['exit']: message_text += format_group("⚠️ **СИГНАЛЫ ВЫХОДА:**", groups['exit'])
    
    message_text += "💡 *Нажмите на монету для детального плана*"
    
    coin_buttons = [InlineKeyboardButton(f"{item['smart_recommendation']['emoji']} {item['symbol'].replace('USDT','')}", callback_data=f"ai_detail_{item['symbol']}") for item in opportunities]
    button_rows = [coin_buttons[i:i + 2] for i in range(0, len(coin_buttons), 2)]
    button_rows.append([InlineKeyboardButton("⬅️ Назад к топу", callback_data="back_to_top")])
    
    await query.edit_message_text(message_text, reply_markup=InlineKeyboardMarkup(button_rows), parse_mode='Markdown')

# ===== НОВАЯ ФУНКЦИЯ: ДЕТАЛЬНЫЙ ИИ-АНАЛИЗ МОНЕТЫ =====
async def show_ai_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    ОБНОВЛЕННАЯ ВЕРСИЯ 2.0: Показывает детальный анализ с текстом "Было X, стало Y"
    """
    query = update.callback_query
    if not check_access(update.effective_user.id):
        await query.answer("⛔ Доступ запрещён", show_alert=True)
        return
        
    symbol_to_analyze = query.data.split('_')[2]
    await query.answer()

    opportunities = context.user_data.get('current_opportunities', [])
    target_item = next((item for item in opportunities if item['symbol'] == symbol_to_analyze), None)
    
    if not target_item:
        await query.edit_message_text("❌ Монета не найдена.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="ai_analysis")]]))
        return

    analyzed_item = await analyze_funding_opportunity(target_item)
    symbol_only = symbol_to_analyze.replace("USDT", "")
    smart_rec = analyzed_item['smart_recommendation']
    enhanced = analyzed_item['enhanced_recommendation']
    analysis = analyzed_item['enhanced_analysis']
    
    message_text = f"🧠 **Торговый анализ: {symbol_only}**\n\n"
    direction_emoji = "🟢" if target_item['rate'] < 0 else "🔴"
    message_text += f"{direction_emoji} **Ставка:** {target_item['rate'] * 100:+.3f}%\n"
    message_text += f"{smart_rec['emoji']} **{smart_rec['message'].upper()}**\n"
    message_text += f"_{analysis.get('recommendation', smart_rec['details'])}_\n\n"
    
    # --- ИЗМЕНЕНИЕ ЗДЕСЬ ---
    change_text = analysis.get('change_text', 'Не удалось рассчитать')
    # -----------------------

    message_text += f"📊 **Анализ тренда:**\n"
    message_text += f"• Направление: {enhanced.get('trend_direction', 'n/a').title()}\n"
    message_text += f"• Сила: {enhanced.get('trend_strength', 0):.0%}\n"
    message_text += f"• Моментум: {enhanced.get('momentum', 'n/a').title()}\n"
    # --- ИЗМЕНЕНИЕ ЗДЕСЬ ---
    message_text += f"• Изменение: {change_text}\n\n"
    # -----------------------
    
    signal_type = enhanced.get('signal_type', 'wait')
    if 'entry' in signal_type:
        plan = "🟢 **План (ЛОНГ):**" if 'long' in signal_type else "🔴 **План (ШОРТ):**"
        message_text += f"{plan}\n• Вход: Открыть позицию\n• Обоснование: Тренд ставки в вашу пользу\n• Выход: При смене тренда\n\n"
    elif 'exit' in signal_type:
        message_text += "⚠️ **Сигнал закрытия:**\n• Действие: Закрыть текущую позицию\n• Причина: Тренд разворачивается\n\n"
    else:
        message_text += "⏱️ **Рекомендация:**\n• Ожидать более четкого сигнала\n\n"

    confidence = smart_rec['confidence']
    data_points = enhanced.get('data_points', 0)
    confidence_text = "очень уверен" if confidence > 0.8 else "уверен" if confidence > 0.6 else "сомневается"
    message_text += f"🎯 **Уверенность ИИ:** {confidence:.0%} ({confidence_text}, {data_points} точек)\n"
    
    keyboard = [[InlineKeyboardButton("⬅️ Назад к ИИ-анализу", callback_data="ai_analysis")], [InlineKeyboardButton("🏠 К топу", callback_data="back_to_top")]]
    await query.edit_message_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

# ===== ИСПРАВЛЕННЫЕ CALLBACK ФУНКЦИИ =====
async def drill_down_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    ОБНОВЛЕННАЯ ВЕРСИЯ: Показывает детали монеты с торговыми сигналами
    """
    query = update.callback_query
    if not check_access(update.effective_user.id):
        await query.answer("⛔ Доступ запрещён", show_alert=True)
        return
        
    symbol_to_show = query.data.split('_')[1]
    await query.answer()

    all_symbol_data = context.user_data.get('all_symbol_data', {})
    if symbol_to_show in all_symbol_data:
        symbol_data = all_symbol_data[symbol_to_show]
    else:
        all_data = api_data_cache.get("data", [])
        if not all_data:
            await query.edit_message_text("🔄 Обновляю данные...")
            all_data = await fetch_all_data(context, force_update=True)
        symbol_data = [item for item in all_data if item['symbol'] == symbol_to_show]
    
    symbol_data = sorted(symbol_data, key=lambda x: abs(x['rate']), reverse=True)
    symbol_only = symbol_to_show.replace("USDT", "")
    if not symbol_data:
        await query.edit_message_text(f"❌ Данные по {symbol_only} не найдены.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="back_to_top")]]))
        return
    
    message_text = f"💎 **Детали по {symbol_only} с сигналами**\n\n"
    now_utc = datetime.now(timezone.utc)
    
    for item in symbol_data:
        analyzed_item = await analyze_funding_opportunity(item)
        smart_rec = analyzed_item['smart_recommendation']
        
        funding_dt_utc = datetime.fromtimestamp(item['next_funding_time'] / 1000, tz=timezone.utc)
        time_left = funding_dt_utc - now_utc
        countdown_str = ""
        if time_left.total_seconds() > 0:
            h, m = divmod(int(time_left.total_seconds()) // 60, 60)
            countdown_str = f" (осталось {h}ч {m}м)"
        
        direction = "🟢 ЛОНГ" if item['rate'] < 0 else "🔴 ШОРТ"
        rate_str = f"{item['rate'] * 100:+.2f}%"
        time_str = funding_dt_utc.astimezone(MSK_TIMEZONE).strftime('%H:%M МСК')
        vol = item.get('volume_24h_usdt', Decimal('0'))
        vol_str = format_volume(vol)
        confidence_str = f" ({smart_rec['confidence']:.0%})" if smart_rec['confidence'] > 0 else ""
        
        message_text += f"{direction} `{rate_str}` в `{time_str}{countdown_str}` [{item['exchange']}]({item['trade_url']})\n"
        message_text += f"  *Объем 24ч:* `{vol_str} USDT`\n"
        message_text += f"  {smart_rec['emoji']} *Сигнал:* _{smart_rec['message']}{confidence_str}_\n\n"

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
        [InlineKeyboardButton("🦄 Биржи", callback_data="filters_exchanges")],
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
    await query.edit_message_text("🦄 **Выберите биржи**", reply_markup=InlineKeyboardMarkup(keyboard))

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
        'funding': f"Текущий порог ставки: `> {settings['funding_threshold']*100:.2f}%`.\n\nОтправьте новое значение (например, `0.75`).",
        'volume': f"Текущий порог объема: `{format_volume(settings['volume_threshold_usdt'])}`.\n\nОтправьте новое значение (например, `500k` или `2M`).",
        'alert_rate': f"Текущий порог для уведомлений: `> {settings['alert_rate_threshold']*100:.2f}%`.\n\nОтправьте новое значение (например, `1.5`).",
        'alert_time': f"Текущее временное окно: `< {settings['alert_time_window_minutes']} минут`.\n\nОтправьте новое значение в минутах (например, `45`).",
        'ai_confidence': f"Текущий порог уверенности ИИ: `> {settings['ai_confidence_threshold']*100:.0f}%`.\n\nОтправьте новое значение (например, `75`)."
    }
    
    # Удаляем предыдущее меню, чтобы не было путаницы
    try:
        await query.message.delete()
    except Exception:
        pass

    sent_message = await context.bot.send_message(chat_id=chat_id, text=prompts[setting_type] + "\n\nДля отмены введите /cancel.", parse_mode='Markdown')
    context.user_data.update({'prompt_message_id': sent_message.message_id, 'menu_to_return': menu_to_return, 'setting_type': setting_type})
    
    # === ГЛАВНОЕ ИСПРАВЛЕНИЕ ЗДЕСЬ ===
    # Для каждой настройки возвращаем свое уникальное состояние
    state_map = {
        'funding': SET_FUNDING_THRESHOLD,
        'volume': SET_VOLUME_THRESHOLD,
        'alert_rate': SET_ALERT_RATE,
        'alert_time': SET_ALERT_TIME,
        'ai_confidence': SET_ALERT_RATE # Для ai_confidence можно переиспользовать существующий стейт, т.к. он тоже ждет число
    }
    return state_map.get(setting_type)

async def save_value(update: Update, context: ContextTypes.DEFAULT_TYPE, setting_type: str = None):
    if not check_access(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещён")
        return ConversationHandler.END # Завершаем разговор, если нет доступа

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    ensure_user_settings(chat_id, user_id)
    settings = user_settings[chat_id]['settings']
    
    current_setting_type = setting_type or context.user_data.get('setting_type')
    
    try:
        value_str = update.message.text.strip().replace(",", ".").upper()
        if current_setting_type in ['funding', 'alert_rate']:
            value = Decimal(value_str)
            if not (0 < value < 100): raise ValueError("Value out of range")
            key = 'funding_threshold' if current_setting_type == 'funding' else 'alert_rate_threshold'
            settings[key] = value / 100
        elif current_setting_type == 'volume':
            num_part = value_str.replace('K', '').replace('M', '').replace('B', '')
            multiplier = 10**3 if 'K' in value_str else 10**6 if 'M' in value_str else 10**9 if 'B' in value_str else 1
            settings['volume_threshold_usdt'] = Decimal(num_part) * multiplier
        elif current_setting_type == 'alert_time':
            value = int(value_str)
            if value <= 0: raise ValueError("Value must be positive")
            settings['alert_time_window_minutes'] = value
        elif current_setting_type == 'ai_confidence':
            value = Decimal(value_str)
            if not (0 <= value <= 100): raise ValueError("Value must be between 0 and 100")
            settings['ai_confidence_threshold'] = value / 100
        else:
            raise ValueError("Unknown setting type")

    except (ValueError, TypeError, decimal.InvalidOperation):
        await update.message.reply_text("❌ Ошибка. Введите корректное числовое значение. Разговор сброшен.", parse_mode='Markdown')
        # === ГЛАВНОЕ ИСПРАВЛЕНИЕ ===
        # Принудительно завершаем "разговор", чтобы бот не зависал
        return ConversationHandler.END
        # ==========================

    if 'prompt_message_id' in context.user_data:
        try: await context.bot.delete_message(chat_id, context.user_data.pop('prompt_message_id'))
        except Exception: pass
    
    try: await context.bot.delete_message(chat_id, update.message.message_id)
    except Exception: pass
    
    await context.user_data.pop('menu_to_return')(update, context)
    return ConversationHandler.END

async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_access(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещён")
        return ConversationHandler.END

    chat_id = update.effective_chat.id
    
    if 'prompt_message_id' in context.user_data:
        try:
            await context.bot.delete_message(chat_id, context.user_data.pop('prompt_message_id'))
        except Exception:
            pass
    try:
        await context.bot.delete_message(chat_id, update.message.id)
    except Exception:
        pass
        
    await context.bot.send_message(chat_id, "Действие отменено. Разговор сброшен.")
    
    # Очищаем user_data от остатков "разговора"
    context.user_data.pop('menu_to_return', None)
    context.user_data.pop('setting_type', None)
    
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

🦄 **Биржи:** {exchanges_list}
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
    
    # Определяем какие биржи используются для уведомлений
    alert_exchanges = settings.get('alert_exchanges', [])
    if alert_exchanges:
        exchanges_text = ", ".join(alert_exchanges)
        exchanges_status = f"Свои: {exchanges_text}"
    else:
        main_exchanges = ", ".join(settings.get('exchanges', ['Не выбраны']))
        exchanges_status = f"Основные: {main_exchanges}"
    
    message_text = "🚨 **Настройки уведомлений**\n\n"
    message_text += "*Бот пришлет сигнал, когда будут выполнены все условия.*\n\n"
    
    print(f"[DEBUG] Alerts menu: alerts_on = {settings.get('alerts_on', False)}")
    
    keyboard = [
    [InlineKeyboardButton(f"📈 Порог ставки: > {settings['alert_rate_threshold']*100:.2f}%", callback_data="alert_set_rate")],
    [InlineKeyboardButton(f"⏰ Окно до выплаты: < {settings['alert_time_window_minutes']} мин", callback_data="alert_set_time")],
    [InlineKeyboardButton(f"🦄 Биржи: {exchanges_status}", callback_data="alert_exchanges_menu")],
    [InlineKeyboardButton("🧠 ИИ-Сигналы", callback_data="ai_signals_menu")],  # <-- НОВАЯ КНОПКА
    [InlineKeyboardButton(f"{status_emoji} Уведомления: {status_text}", callback_data="alert_toggle_on")],
    [InlineKeyboardButton("⬅️ Назад к фильтрам", callback_data="alert_back_filters")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        await update.callback_query.edit_message_text(message_text, reply_markup=reply_markup, parse_mode='Markdown')
    else:
        await update.message.reply_text(message_text, reply_markup=reply_markup, parse_mode='Markdown')
async def show_ai_signals_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает меню настройки ИИ-сигналов"""
    query = update.callback_query
    
    if not check_access(update.effective_user.id):
        await query.answer("⛔ Доступ запрещён", show_alert=True)
        return
        
    await query.answer()
    chat_id, user_id = update.effective_chat.id, update.effective_user.id
    ensure_user_settings(chat_id, user_id)
    settings = user_settings[chat_id]['settings']
    
    ai_status_text = "✅ ВКЛЮЧЕНЫ" if settings.get('ai_signals_on', False) else "🔴 ВЫКЛЮЧЕНЫ"
    entry_status = "✅" if settings.get('ai_entry_signals', True) else "⬜️"
    exit_status = "✅" if settings.get('ai_exit_signals', True) else "⬜️"
    
    message_text = "🧠 **Настройки ИИ-сигналов**\n\n"
    message_text += "*Бот пришлет сигнал только когда ИИ уверен в торговой возможности.*\n\n"
    
    keyboard = [
        [InlineKeyboardButton(f"🎯 Уверенность ИИ: > {settings['ai_confidence_threshold']*100:.0f}%", callback_data="ai_set_confidence")],
        [InlineKeyboardButton(f"{entry_status} Сигналы входа", callback_data="ai_toggle_entry")],
        [InlineKeyboardButton(f"{exit_status} Сигналы выхода", callback_data="ai_toggle_exit")],
        [InlineKeyboardButton(f"🧠 ИИ-Сигналы: {ai_status_text}", callback_data="ai_toggle_on")],
        [InlineKeyboardButton("⬅️ Назад к уведомлениям", callback_data="alert_show_menu")]
    ]
    
    await query.edit_message_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def ai_signals_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает кнопки меню ИИ-сигналов"""
    query = update.callback_query
    
    if not check_access(update.effective_user.id):
        await query.answer("⛔ Доступ запрещён", show_alert=True)
        return
        
    await query.answer()
    action = query.data.split('_', 2)[1]
    
    chat_id, user_id = update.effective_chat.id, update.effective_user.id
    ensure_user_settings(chat_id, user_id)
    settings = user_settings[chat_id]['settings']
    
    if action == "toggle":
        sub_action = query.data.split('_', 2)[2]
        if sub_action == "on": settings['ai_signals_on'] = not settings.get('ai_signals_on', False)
        elif sub_action == "entry": settings['ai_entry_signals'] = not settings.get('ai_entry_signals', True)
        elif sub_action == "exit": settings['ai_exit_signals'] = not settings.get('ai_exit_signals', True)
    
    await show_ai_signals_menu(update, context)

async def ask_for_ai_confidence(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запрашивает новое значение уверенности ИИ."""
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    settings = user_settings[chat_id]['settings']
    
    text = (f"Текущий порог уверенности ИИ: `> {settings['ai_confidence_threshold']*100:.0f}%`.\n\n"
            f"Отправьте новое значение в процентах (например, `75`).")
    
    sent_message = await context.bot.send_message(chat_id=chat_id, text=text + "\n\nДля отмены введите /cancel.", parse_mode='Markdown')
    context.user_data.update({'prompt_message_id': sent_message.message_id, 'menu_to_return': show_ai_signals_menu, 'setting_type': 'ai_confidence'})
    return SET_ALERT_RATE # Используем тот же стейт, что и для ставки
    
async def show_alert_exchanges_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает меню выбора бирж для уведомлений"""
    query = update.callback_query
    
    if not check_access(update.effective_user.id):
        await query.answer("⛔ Доступ запрещён", show_alert=True)
        return
        
    await query.answer()
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    ensure_user_settings(chat_id, user_id)
    settings = user_settings[chat_id]['settings']
    
    alert_exchanges = settings.get('alert_exchanges', [])
    main_exchanges = settings.get('exchanges', [])
    
    message_text = "🦄 **Биржи для уведомлений**\n\n"
    message_text += "*Выберите биржи, по которым получать уведомления.*\n"
    message_text += "*Если ничего не выбрано - используются основные настройки.*\n\n"
    message_text += f"🔧 **Основные биржи:** {', '.join(main_exchanges)}\n\n"
    
    # Кнопки выбора бирж
    buttons = []
    for exchange in ALL_AVAILABLE_EXCHANGES:
        if exchange in alert_exchanges:
            emoji = "✅"
        else:
            emoji = "⬜️"
        buttons.append(InlineKeyboardButton(f"{emoji} {exchange}", callback_data=f"alert_exch_{exchange}"))
    
    # Группируем кнопки по 2 в ряд
    keyboard = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    
    # Дополнительные кнопки
    keyboard.append([InlineKeyboardButton("🗑️ Очистить выбор", callback_data="alert_exch_clear")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад к уведомлениям", callback_data="alert_show_menu")])
    
    await query.edit_message_text(message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def alert_exchanges_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает выбор бирж для уведомлений"""
    query = update.callback_query
    
    if not check_access(update.effective_user.id):
        await query.answer("⛔ Доступ запрещён", show_alert=True)
        return
        
    await query.answer()
    action = query.data.split('_', 2)[2]  # alert_exch_ACTION
    
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    ensure_user_settings(chat_id, user_id)
    settings = user_settings[chat_id]['settings']
    
    alert_exchanges = settings.get('alert_exchanges', [])
    
    if action == "clear":
        # Очищаем выбор
        settings['alert_exchanges'] = []
        await query.answer("🗑️ Выбор очищен. Будут использоваться основные биржи.", show_alert=True)
    elif action in ALL_AVAILABLE_EXCHANGES:
        # Переключаем биржу
        if action in alert_exchanges:
            alert_exchanges.remove(action)
        else:
            alert_exchanges.append(action)
        settings['alert_exchanges'] = alert_exchanges
    
    # Обновляем меню
    await show_alert_exchanges_menu(update, context)

async def toggle_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переключает состояние уведомлений"""
    query = update.callback_query
    
    if not check_access(update.effective_user.id):
        await query.answer("⛔ Доступ запрещён", show_alert=True)
        return
        
    await query.answer()
    
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    ensure_user_settings(chat_id, user_id)
    
    # Переключаем состояние уведомлений
    current_state = user_settings[chat_id]['settings']['alerts_on']
    new_state = not current_state
    user_settings[chat_id]['settings']['alerts_on'] = new_state
    
    print(f"[DEBUG] Уведомления переключены для chat_id {chat_id}: {current_state} -> {new_state}")
    
    # Показываем обновленное меню
    await show_alerts_menu(update, context)

async def alert_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает нажатия в меню уведомлений."""
    query = update.callback_query
    
    if not check_access(update.effective_user.id):
        await query.answer("⛔ Доступ запрещён", show_alert=True)
        return
        
    action = query.data.split('_', 1)[1]  # alert_ACTION
    
    print(f"[DEBUG] Alert callback action: {action}")
    
    if action == "toggle_on":
        await toggle_alerts(update, context)
    elif action == "back_filters":
        await query.answer()
        await send_filters_menu(update, context)
    else:
        await query.answer()

# ===== ИСПРАВЛЕННЫЙ ФОНОВЫЙ СКАНЕР =====
async def background_scanner(app: Application):
    print("🚀 Фоновый сканер уведомлений запущен.")
    while True:
        await asyncio.sleep(60)
        try:
            all_data = await fetch_all_data(app, force_update=True)
            if not all_data: continue
            now_utc, current_ts_ms = datetime.now(timezone.utc), int(datetime.now(timezone.utc).timestamp() * 1000)
            
            for chat_id, user_data in list(user_settings.items()):
                stored_user_id = user_data.get('user_id')
                if not stored_user_id or not check_access(stored_user_id): continue
                
                settings = user_data['settings']
                target_exchanges = settings.get('alert_exchanges', []) or settings.get('exchanges', [])

                # --- Блок ОБЫЧНЫХ УВЕДОМЛЕНИЙ (без изменений) ---
                if settings.get('alerts_on', False):
                    settings['sent_notifications'] = {nid for nid in settings.get('sent_notifications', set()) if int(nid.split('_')[-1]) > current_ts_ms - (3 * 60 * 60 * 1000)}
                    for item in all_data:
                        if item['exchange'] not in target_exchanges: continue
                        if abs(item['rate']) < settings['alert_rate_threshold']: continue
                        time_left_seconds = (item['next_funding_time'] / 1000) - now_utc.timestamp()
                        if not (0 < time_left_seconds <= settings['alert_time_window_minutes'] * 60): continue
                        notification_id = f"{item['exchange']}_{item['symbol']}_{item['next_funding_time']}"
                        if notification_id in settings['sent_notifications']: continue
                        h, m = divmod(int(time_left_seconds // 60), 60)
                        countdown_str = f"{h}ч {m}м" if h > 0 else f"{m}м"
                        message = (f"⚠️ **Найден фандинг по вашему фильтру!**\n\n"
                                   f"{'🟢' if item['rate'] < 0 else '🔴'} **{item['symbol'].replace('USDT', '')}** `{item['rate'] * 100:+.2f}%`\n"
                                   f"⏰ Выплата через *{countdown_str}* на *{item['exchange']}*")
                        try:
                            await app.bot.send_message(chat_id, message, parse_mode='Markdown')
                            settings['sent_notifications'].add(notification_id)
                        except Exception as e: print(f"[BG_SCANNER] ❌ Ошибка отправки уведомления: {e}")

                # === НОВЫЙ БЛОК ИИ-СИГНАЛОВ ===
                if settings.get('ai_signals_on', False):
                    settings['ai_sent_notifications'] = {nid for nid in settings.get('ai_sent_notifications', set()) if int(nid.split('_')[-1]) > current_ts_ms - (3 * 60 * 60 * 1000)} # Очистка старых
                    for item in all_data:
                        if item['exchange'] not in target_exchanges: continue
                        
                        analyzed_item = await analyze_funding_opportunity(item)
                        smart_rec = analyzed_item.get('smart_recommendation', {})
                        signal_type = smart_rec.get('recommendation_type', '')
                        
                        entry_signals = ['strong_long_entry', 'long_entry', 'strong_short_entry', 'short_entry']
                        exit_signals = ['long_exit', 'short_exit']
                        
                        if (signal_type in entry_signals and not settings.get('ai_entry_signals', True)) or \
                           (signal_type in exit_signals and not settings.get('ai_exit_signals', True)) or \
                           (signal_type not in entry_signals + exit_signals):
                            continue
                        
                        confidence = smart_rec.get('confidence', 0.0)
                        if confidence < settings.get('ai_confidence_threshold', Decimal('0.6')):
                            continue
                            
                        ai_notification_id = f"AI_{item['exchange']}_{item['symbol']}_{signal_type}_{current_ts_ms}"
                        if any(nid.startswith(f"AI_{item['exchange']}_{item['symbol']}") for nid in settings.get('ai_sent_notifications', set())):
                            continue # Анти-спам: не отправлять повторно по той же монете, пока старый не истечет
                            
                        message = (f"🧠 **ИИ ТОРГОВЫЙ СИГНАЛ!**\n\n"
                                   f"{smart_rec.get('emoji', '❓')} **{smart_rec.get('message', '')}** по **{item['symbol'].replace('USDT', '')}**\n"
                                   f"Уверенность: **{confidence:.0%}**\n\n"
                                   f"💡 _{smart_rec.get('details', '')}_\n\n"
                                   f"📊 Биржа: *{item['exchange']}* | Ставка: `{item['rate'] * 100:+.2f}%`")
                        
                        try:
                            await app.bot.send_message(chat_id, message, parse_mode='Markdown')
                            settings['ai_sent_notifications'].add(ai_notification_id)
                            print(f"[AI_SIGNALS] ✅ Отправлен ИИ-сигнал для chat_id {chat_id}: {signal_type} {item['symbol']}")
                        except Exception as e:
                            print(f"[AI_SIGNALS] ❌ Ошибка отправки ИИ-сигнала для chat_id {chat_id}: {e}")
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
@require_access()
async def get_funding_history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Команда для получения истории funding rate конкретной монеты
    """
    args = context.args
    if not args:
        await update.message.reply_text("Использование: `/history СИМВОЛ [БИРЖА]`\nПример: `/history API3USDT MEXC`", parse_mode='Markdown')
        return
    
    symbol = args[0].upper()
    if not symbol.endswith('USDT'): symbol += 'USDT'
    exchange = args[1].upper() if len(args) > 1 else None
    exchanges_to_check = [exchange] if exchange else ['MEXC', 'BYBIT']
    
    message = await update.message.reply_text(f"🔍 Получаю историю для {symbol}...")
    
    report_text = f"📊 **История Funding Rate: {symbol.replace('USDT', '')}**\n\n"
    for ex in exchanges_to_check:
        history = await enhanced_funding_analyzer._get_funding_history_real(symbol, ex)
        if history:
            report_text += f"**{ex}** ({len(history)} периодов):\n"
            for rate in history[-10:]:
                report_text += f"`{float(rate) * 100:+.3f}%` "
            report_text += "\n\n"
    
    await message.edit_text(report_text, parse_mode='Markdown')

@require_access()
async def quick_signal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Быстрый анализ торгового сигнала для конкретной монеты
    """
    args = context.args
    if not args:
        await update.message.reply_text("Использование: `/signal СИМВОЛ [БИРЖА]`\nПример: `/signal API3`", parse_mode='Markdown')
        return
    
    symbol = args[0].upper()
    if not symbol.endswith('USDT'): symbol += 'USDT'
    exchange = args[1].upper() if len(args) > 1 else None
    
    message = await update.message.reply_text(f"🧠 Анализирую сигнал для {symbol}...")
    
    all_data = await fetch_all_data(context, force_update=True)
    target_items = [item for item in all_data if item['symbol'] == symbol]
    if exchange: target_items = [item for item in target_items if item['exchange'].upper() == exchange]
    
    if not target_items:
        await message.edit_text(f"❌ Не найдены данные для {symbol}.")
        return
        
    best_item = max(target_items, key=lambda x: abs(x['rate']))
    analyzed_item = await analyze_funding_opportunity(best_item)
    smart_rec = analyzed_item['smart_recommendation']
    
    report = f"🎯 **Сигнал: {symbol.replace('USDT', '')}** ({best_item['exchange']})\n\n"
    report += f"**Ставка:** `{best_item['rate'] * 100:+.3f}%`\n"
    report += f"{smart_rec['emoji']} **{smart_rec['message'].upper()}**\n"
    report += f"_{smart_rec['details']} ({smart_rec['confidence']:.0%})_\n"
    await message.edit_text(report, parse_mode='Markdown')

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
        states={SET_FUNDING_THRESHOLD: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_value)]},
        fallbacks=fallbacks, allow_reentry=True
    ),
    ConversationHandler(
        entry_points=[CallbackQueryHandler(lambda u, c: ask_for_value(u, c, 'volume', send_filters_menu), pattern="^filters_volume$")],
        states={SET_VOLUME_THRESHOLD: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_value)]},
        fallbacks=fallbacks, allow_reentry=True
    ),
    ConversationHandler(
        entry_points=[CallbackQueryHandler(lambda u, c: ask_for_value(u, c, 'alert_rate', show_alerts_menu), pattern="^alert_set_rate$")],
        states={SET_ALERT_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_value)]},
        fallbacks=fallbacks, allow_reentry=True
    ),
    ConversationHandler(
        entry_points=[CallbackQueryHandler(lambda u, c: ask_for_value(u, c, 'alert_time', show_alerts_menu), pattern="^alert_set_time$")],
        states={SET_ALERT_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_value)]},
        fallbacks=fallbacks, allow_reentry=True
    ),
    ConversationHandler(
        entry_points=[CallbackQueryHandler(lambda u, c: ask_for_value(u, c, 'ai_confidence', show_ai_signals_menu), pattern="^ai_set_confidence$")],
        states={SET_ALERT_RATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_value)]}, # Используем тот же стейт, но в отдельном хендлере
        fallbacks=fallbacks, allow_reentry=True
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
        # ИСПРАВЛЕННЫЕ обработчики уведомлений
        CallbackQueryHandler(alert_callback_handler, pattern="^alert_toggle_on$"),
        CallbackQueryHandler(alert_callback_handler, pattern="^alert_back_filters$"),
        # НОВЫЕ обработчики ИИ-анализа
        CallbackQueryHandler(show_ai_analysis, pattern="^ai_analysis$"),
        CallbackQueryHandler(show_ai_detail, pattern="^ai_detail_"),
        # НОВЫЕ обработчики ИИ-сигналов
        CallbackQueryHandler(show_ai_signals_menu, pattern="^ai_signals_menu$"),
        CallbackQueryHandler(ai_signals_callback_handler, pattern="^ai_toggle_"),
        # НОВЫЕ обработчики бирж для уведомлений
        CallbackQueryHandler(show_alert_exchanges_menu, pattern="^alert_exchanges_menu$"),
        CallbackQueryHandler(alert_exchanges_callback_handler, pattern="^alert_exch_"),
        # Универсальный обработчик для всех остальных сообщений (должен быть последним)
        MessageHandler(filters.TEXT, handle_unauthorized_message),
    ]

    # Добавляем все обработчики в приложение
    app.add_handlers(conv_handlers)
    app.add_handlers(regular_handlers)
    app.add_handler(CommandHandler("history", get_funding_history_command))
    app.add_handler(CommandHandler("signal", quick_signal_command))

    # 4. Запуск фонового сканера
    async def post_init(app):
        asyncio.create_task(background_scanner(app))

    app.post_init = post_init

    # 5. Запускаем бота
    print("🤖 RateHunter 2.0 с ИИ-анализатором запущен с ограничением доступа!")
    print(f"🔒 Разрешенные пользователи: {ALLOWED_USERS}")
    print("🚀 Фоновый сканер для уведомлений активен!")
    print("🧠 Умный анализ funding rates включен!")
    app.run_polling()# ===============
