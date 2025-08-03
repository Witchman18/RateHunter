# =========================================================================
# ===================== RateHunter 2.0 - v1.0.0 =========================
# =========================================================================
# Финальная версия для работы на хостинге с полным доступом к сети.
# - Использует приватные API для Bybit и MEXC для максимальной точности.
# - Корректно работает с переменными окружения на Railway.
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

def get_default_settings():
    return {
        'notifications_on': True, 'exchanges': ['Bybit', 'MEXC'],
        'funding_threshold': Decimal('0.005'), 'volume_threshold_usdt': Decimal('1000000'),
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
    headers = {'X-BAPI-API-KEY': api_key, 'X-BAPI-TIMESTAMP': timestamp, 'X-BAPI-RECV-WINDOW': recv_window, 'X-BAPI-SIGN': signature}
    
    results = []
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(base_url + request_path + "?" + params, headers=headers, timeout=15) as response:
                if response.status != 200:
                    print(f"[API_ERROR] Bybit: Приватный API вернул ошибку! Статус: {response.status}, Ответ: {await response.text()}")
                    return []
                data = await response.json()
                if data.get("retCode") == 0 and data.get("result", {}).get("list"):
                    for t in data["result"]["list"]:
                        try:
                            results.append({'exchange': 'Bybit', 'symbol': t.get("symbol"), 'rate': Decimal(t.get("fundingRate")), 'next_funding_time': int(t.get("nextFundingTime")), 'volume_24h_usdt': Decimal(t.get("turnover24h")), 'max_order_value_usdt': Decimal('0'), 'trade_url': f'https://www.bybit.com/trade/usdt/{t.get("symbol")}'})
                        except (TypeError, ValueError, decimal.InvalidOperation): continue
    except Exception as e:
        print(f"[API_ERROR] Bybit (Private): {e}")
    return results

async def get_mexc_data(api_key: str, secret_key: str):
    if not api_key or not secret_key:
        print("[API_WARNING] MEXC: Ключи не настроены.")
        return []

    request_path = "/api/v1/private/contract/open_contracts"
    base_url = "https://contract.mexc.com"
    timestamp = str(int(time.time() * 1000))
    data_to_sign = timestamp + api_key
    signature = hmac.new(secret_key.encode('utf-8'), data_to_sign.encode('utf-8'), hashlib.sha256).hexdigest()
    headers = {'ApiKey': api_key, 'Request-Time': timestamp, 'Signature': signature, 'Content-Type': 'application/json'}
    
    results = []
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(base_url + request_path, headers=headers, timeout=15) as response:
                if response.status != 200:
                    print(f"[API_ERROR] MEXC: Приватный API вернул ошибку! Статус: {response.status}, Ответ: {await response.text()}")
                    return []
                data = await response.json()
                if data.get("success") and data.get("data"):
                    for t in data["data"]:
                        try:
                            rate_val, symbol_from_api, next_funding_ts = t.get("fundingRate"), t.get("symbol"), t.get("nextSettleTime")
                            if rate_val is None or not symbol_from_api or not symbol_from_api.endswith("USDT") or not next_funding_ts: continue
                            normalized_symbol = symbol_from_api.replace("_", "")
                            volume_in_coin, last_price = Decimal(str(t.get("volume24", '0'))), Decimal(str(t.get("lastPrice", '0')))
                            volume_in_usdt = volume_in_coin * last_price if last_price > 0 else Decimal('0')
                            results.append({'exchange': 'MEXC', 'symbol': normalized_symbol, 'rate': Decimal(str(rate_val)), 'next_funding_time': int(next_funding_ts), 'volume_24h_usdt': volume_in_usdt, 'max_order_value_usdt': Decimal('0'), 'trade_url': f'https://futures.mexc.com/exchange/{symbol_from_api}'})
                        except (TypeError, ValueError, decimal.InvalidOperation): continue
    except Exception as e:
        print(f"[API_ERROR] MEXC (Private): {e}")
    return results

async def fetch_all_data(context: ContextTypes.DEFAULT_TYPE, force_update=False):
    now = datetime.now().timestamp()
    if not force_update and api_data_cache["last_update"] and (now - api_data_cache["last_update"] < CACHE_LIFETIME_SECONDS):
        return api_data_cache["data"]

    mexc_api_key, mexc_secret_key = context.bot_data.get('mexc_api_key'), context.bot_data.get('mexc_secret_key')
    bybit_api_key, bybit_secret_key = context.bot_data.get('bybit_api_key'), context.bot_data.get('bybit_secret_key')
    
    tasks = [
        get_bybit_data(api_key=bybit_api_key, secret_key=bybit_secret_key), 
        get_mexc_data(api_key=mexc_api_key, secret_key=mexc_secret_key)
    ]
    results_from_tasks = await asyncio.gather(*tasks, return_exceptions=True)
    
    all_data = []
    for res in results_from_tasks:
        if isinstance(res, list): all_data.extend(res)
            
    api_data_cache["data"], api_data_cache["last_update"] = all_data, now
    return all_data

# =================================================================
# ================== ПОЛЬЗОВАТЕЛЬСКИЙ ИНТЕРФЕЙС ==================
# =================================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user_settings(update.effective_chat.id)
    main_menu_keyboard = [["🔥 Топ-ставки сейчас"], ["🔔 Настроить фильтры", "ℹ️ Мои настройки"]]
    reply_markup = ReplyKeyboardMarkup(main_menu_keyboard, resize_keyboard=True)
    await update.message.reply_text("Добро пожаловать в RateHunter 2.0!", reply_markup=reply_markup)

async def show_top_rates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ensure_user_settings(chat_id)
    settings = user_settings[chat_id]

    msg = update.callback_query.message if update.callback_query else await update.message.reply_text("🔄 Ищу...")
    await msg.edit_text("🔄 Ищу лучшие ставки по вашим фильтрам...")

    all_data = await fetch_all_data(context)
    if not all_data:
        await msg.edit_text("😔 Не удалось получить данные с бирж. Попробуйте позже.")
        return

    filtered_data = [item for item in all_data if item['exchange'] in settings['exchanges'] and abs(item['rate']) >= settings['funding_threshold'] and item.get('volume_24h_usdt', Decimal('0')) >= settings['volume_threshold_usdt']]
    filtered_data.sort(key=lambda x: abs(x['rate']), reverse=True)
    top_5 = filtered_data[:5]

    if not top_5:
        await msg.edit_text("😔 Не найдено пар, соответствующих вашим фильтрам.")
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

        direction, rate_str = ("🟢 LONG", f"{item['rate'] * 100:+.2f}%") if item['rate'] < 0 else ("🔴 SHORT", f"{item['rate'] * 100:+.2f}%")
        time_str = funding_dt_utc.astimezone(MSK_TIMEZONE).strftime('%H:%M МСК')
        message_text += f"{direction} *{symbol_only}* `{rate_str}` в `{time_str}{countdown_str}` [{item['exchange']}]\n"
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

# (Вставьте сюда все остальные функции интерфейса, они не менялись)
async def back_to_top_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... и так далее

# =================================================================
# ========================== ЗАПУСК БОТА ==========================
# =================================================================

if __name__ == "__main__":
    if not BOT_TOKEN: raise ValueError("Не найден BOT_TOKEN.")
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    # Загружаем ключи в "общий склад" бота
    app.bot_data['mexc_api_key'] = os.getenv("MEXC_API_KEY")
    app.bot_data['mexc_secret_key'] = os.getenv("MEXC_API_SECRET")
    app.bot_data['bybit_api_key'] = os.getenv("BYBIT_API_KEY")
    app.bot_data['bybit_secret_key'] = os.getenv("BYBIT_SECRET_KEY")

    # Диагностика при старте
    if app.bot_data['mexc_api_key']: print("✅ Ключи MEXC успешно загружены.")
    else: print("⚠️ Ключи MEXC не найдены.")
    if app.bot_data['bybit_api_key']: print("✅ Ключи Bybit успешно загружены.")
    else: print("⚠️ Ключи Bybit не найдены.")

    # (Вставьте сюда весь ваш код добавления хендлеров `app.add_handler(...)`)
    
    print("🤖 RateHunter 2.0 запущен!")
    app.run_polling()
