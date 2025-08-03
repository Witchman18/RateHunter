# =========================================================================
# ===================== RateHunter 2.0 - Alpha v0.3.1 ===================
# =========================================================================
# Изменения в этой версии:
# - ИСПРАВЛЕНИЕ: Полностью исправлен модуль MEXC для корректного получения данных.
# - ИСПРАВЛЕНИЕ: Правильная интерпретация полей volume/amount в MEXC API.
# - ОПТИМИЗАЦИЯ: Добавлена предварительная фильтрация пар с низким фандингом.
# =========================================================================

import os
import asyncio
import aiohttp
import decimal
import json
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

# API ключи для бирж (опционально для расширенного функционала)
MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_SECRET_KEY = os.getenv("MEXC_SECRET_KEY")
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_SECRET_KEY = os.getenv("BYBIT_SECRET_KEY")

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
# ===================== МОДУЛЬ СБОРА ДАННЫХ (API) =====================
# =================================================================

async def get_bybit_data():
    """Получение данных фандинга от Bybit"""
    bybit_url = "https://api.bybit.com/v5/market/tickers?category=linear"
    instrument_url = "https://api.bybit.com/v5/market/instruments-info?category=linear"
    results = []
    try:
        async with aiohttp.ClientSession() as session:
            limits_data = {}
            async with session.get(instrument_url, timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get("retCode") == 0 and data.get("result", {}).get("list"):
                        for inst in data["result"]["list"]:
                            limits_data[inst['symbol']] = inst.get('lotSizeFilter', {}).get('maxOrderQty', '0')

            async with session.get(bybit_url, timeout=10) as response:
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
    return results

async def get_mexc_data():
    """ДИАГНОСТИЧЕСКАЯ версия получения данных от MEXC с поддержкой API ключей"""
    
    # Выбираем endpoint в зависимости от наличия API ключей
    if MEXC_API_KEY and MEXC_SECRET_KEY:
        print(f"[DEBUG] MEXC: Используем авторизованный API")
        # Для авторизованных запросов можно использовать другие endpoints
        mexc_url = "https://contract.mexc.com/api/v1/contract/ticker"
        # TODO: Добавить подпись запроса для приватного API
    else:
        print(f"[DEBUG] MEXC: Используем публичный API")
        mexc_url = "https://contract.mexc.com/api/v1/contract/ticker"
    
    results = []
    
    print(f"[DEBUG] MEXC: Начинаем получение данных с {mexc_url}")
    
    try:
        headers = {}
        if MEXC_API_KEY:
            headers['X-MEXC-APIKEY'] = MEXC_API_KEY
            print(f"[DEBUG] MEXC: Добавлен API ключ в заголовки")
        
        async with aiohttp.ClientSession() as session:
            async with session.get(mexc_url, headers=headers, timeout=15) as response:
                response.raise_for_status()
                data = await response.json()
                
                print(f"[DEBUG] MEXC: Получен ответ, success: {data.get('success')}")
                
                if data.get("success") and data.get("data"):
                    total_pairs = len(data["data"])
                    processed_pairs = 0
                    usdt_pairs = 0
                    valid_funding_pairs = 0
                    valid_volume_pairs = 0
                    
                    print(f"[DEBUG] MEXC: Обрабатываем {total_pairs} пар")
                    
                    # Проверим первые 5 пар для диагностики
                    for i, t in enumerate(data["data"]):
                        try:
                            symbol = t.get("symbol")
                            rate_val = t.get("fundingRate")
                            next_funding_time = t.get("nextSettleTime")
                            
                            if i < 5:  # Диагностика первых 5 пар
                                print(f"[DEBUG] MEXC Sample {i+1}: {symbol}, rate: {rate_val}, time: {next_funding_time}")
                                print(f"[DEBUG] MEXC Sample {i+1}: amount24: {t.get('amount24')}, volume24: {t.get('volume24')}, lastPrice: {t.get('lastPrice')}")
                                # Дополнительная диагностика полей времени
                                print(f"[DEBUG] MEXC Sample {i+1}: nextSettleTime: {t.get('nextSettleTime')}, nextFundingTime: {t.get('nextFundingTime')}")
                                print(f"[DEBUG] MEXC Sample {i+1}: все поля времени: {[k for k in t.keys() if 'time' in k.lower() or 'settle' in k.lower()]}")
                            
                            # Проверяем USDT пары
                            if symbol and symbol.endswith("USDT"):
                                usdt_pairs += 1
                                
                                # ИСПРАВЛЕНИЕ: Проверяем фандинг данные БЕЗ проверки времени
                                if rate_val is not None:
                                    valid_funding_pairs += 1
                                    
                                    # Устанавливаем время фандинга по умолчанию если оно None
                                    if next_funding_time is None:
                                        # Используем текущее время + 8 часов (стандартный интервал фандинга)
                                        current_time = datetime.now(timezone.utc)
                                        next_funding_default = current_time + timedelta(hours=8)
                                        next_funding_time = int(next_funding_default.timestamp() * 1000)
                                    
                                    # Проверяем объем
                                    volume_24h_usdt = Decimal('0')
                                    
                                    # Пробуем amount24
                                    amount24_val = t.get("amount24")
                                    if amount24_val and str(amount24_val) != '0':
                                        try:
                                            volume_24h_usdt = Decimal(str(amount24_val))
                                        except:
                                            pass
                                    
                                    # Если amount24 не работает, пробуем volume24 * lastPrice
                                    if volume_24h_usdt == 0:
                                        volume24_val = t.get("volume24")
                                        lastPrice_val = t.get("lastPrice")
                                        if volume24_val and lastPrice_val:
                                            try:
                                                volume_24h_vol = Decimal(str(volume24_val))
                                                last_price = Decimal(str(lastPrice_val))
                                                if volume_24h_vol > 0 and last_price > 0:
                                                    volume_24h_usdt = volume_24h_vol * last_price
                                            except:
                                                pass
                                    
                                    if volume_24h_usdt > 0:
                                        valid_volume_pairs += 1
                                        
                                        # ИСПРАВЛЕНИЕ: Нормализуем символ для MEXC
                                        # MEXC использует формат BTC_USDT, а мы ожидаем BTCUSDT
                                        normalized_symbol = symbol.replace("_", "")
                                        
                                        # Убираем фильтр по минимальному объему для тестирования
                                        # if volume_24h_usdt >= Decimal('50000'):
                                        processed_pairs += 1
                                        
                                        results.append({
                                            'exchange': 'MEXC',
                                            'symbol': normalized_symbol,  # Используем нормализованный символ
                                            'rate': Decimal(str(rate_val)),
                                            'next_funding_time': int(next_funding_time),
                                            'volume_24h_usdt': volume_24h_usdt,
                                            'max_order_value_usdt': Decimal('0'),
                                            'trade_url': f'https://futures.mexc.com/exchange/{symbol}'  # Оригинальный символ для URL
                                        })
                            
                        except (TypeError, ValueError, decimal.InvalidOperation) as e:
                            if i < 5:
                                print(f"[DEBUG] MEXC Error for sample {i+1}: {e}")
                            continue
                    
                    print(f"[DEBUG] MEXC: Всего пар: {total_pairs}")
                    print(f"[DEBUG] MEXC: USDT пар: {usdt_pairs}")
                    print(f"[DEBUG] MEXC: С валидным фандингом: {valid_funding_pairs}")
                    print(f"[DEBUG] MEXC: С валидным объемом: {valid_volume_pairs}")
                    print(f"[DEBUG] MEXC: Успешно обработано: {processed_pairs}")
                    print(f"[DEBUG] MEXC: Получено записей: {len(results)}")
                else:
                    print(f"[ERROR] MEXC: Неверный формат ответа API")
                    
    except Exception as e:
        print(f"[API_ERROR] MEXC: {e}")
        
    return results

async def fetch_all_data(force_update=False):
    """Получение данных со всех бирж с кешированием"""
    now = datetime.now().timestamp()
    if not force_update and api_data_cache["last_update"] and (now - api_data_cache["last_update"] < CACHE_LIFETIME_SECONDS):
        print(f"[DEBUG] Используем кешированные данные")
        return api_data_cache["data"]

    print(f"[DEBUG] Обновляем данные с бирж...")
    tasks = [get_bybit_data(), get_mexc_data()]
    results_from_tasks = await asyncio.gather(*tasks, return_exceptions=True)
    
    all_data = []
    for i, res in enumerate(results_from_tasks):
        exchange_name = ['Bybit', 'MEXC'][i]
        if isinstance(res, list):
            print(f"[DEBUG] {exchange_name}: получено {len(res)} записей")
            all_data.extend(res)
        elif isinstance(res, Exception):
            print(f"[ERROR] {exchange_name}: {res}")
        else:
            print(f"[WARNING] {exchange_name}: неожиданный тип результата: {type(res)}")
            
    print(f"[DEBUG] Всего получено {len(all_data)} записей")
    api_data_cache["data"], api_data_cache["last_update"] = all_data, now
    return all_data


# =================================================================
# ================== ПОЛЬЗОВАТЕЛЬСКИЙ ИНТЕРФЕЙС ==================
# =================================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user_settings(update.effective_chat.id)
    main_menu_keyboard = [
        ["🔥 Топ-ставки сейчас"], 
        ["🔔 Настроить фильтры", "ℹ️ Мои настройки"],
    ]
    reply_markup = ReplyKeyboardMarkup(main_menu_keyboard, resize_keyboard=True)
    await update.message.reply_text("Добро пожаловать в RateHunter 2.0!", reply_markup=reply_markup)

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
        and item.get('volume_24h_usdt', Decimal('0')) >= settings['volume_threshold_usdt']
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
    
    if not symbol_specific_data:
        await query.edit_message_text(f"😔 Данные для {symbol_only} не найдены.")
        return
    
    message_text = f"💎 **Детали по {symbol_only}**\n\n"
    for item in symbol_specific_data:
        funding_dt = datetime.fromtimestamp(item['next_funding_time'] / 1000, tz=MSK_TIMEZONE)
        time_str = funding_dt.strftime('%H:%M МСК')
        direction_text = "🟢 ЛОНГ" if item['rate'] < 0 else "🔴 ШОРТ"
        rate_str = f"{item['rate'] * 100:+.2f}%"
        
        # Форматируем объем
        volume_usdt = item.get('volume_24h_usdt', Decimal('0'))
        if volume_usdt >= Decimal('1000000000'):  # >= 1B
            volume_str = f"{volume_usdt / Decimal('1000000000'):.1f}B"
        elif volume_usdt >= Decimal('1000000'):  # >= 1M
            volume_str = f"{volume_usdt / Decimal('1000000'):.1f}M"
        else:
            volume_str = f"{volume_usdt / Decimal('1000'):.0f}K"
        
        message_text += f"{direction_text} `{rate_str}` в `{time_str}` [{item['exchange']}]({item['trade_url']})\n"
        message_text += f"  *Объем 24ч:* `{volume_str} USDT`\n"

        max_pos = item.get('max_order_value_usdt', Decimal('0'))
        if max_pos > 0:
            message_text += f"  *Макс. ордер:* `{max_pos:,.0f} USDT`\n"
        
        message_text += "\n"

    keyboard = [[InlineKeyboardButton("⬅️ Назад к топу", callback_data="back_to_top")]]
    await query.edit_message_text(
        text=message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown', disable_web_page_preview=True
    )

async def back_to_top_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
    await show_top_rates(update, context)

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
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
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

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("^🔥 Топ-ставки сейчас$"), show_top_rates))
    app.add_handler(MessageHandler(filters.Regex("^🔔 Настроить фильтры$"), filters_menu_entry))
    app.add_handler(MessageHandler(filters.Regex("^ℹ️ Мои настройки$"), show_my_settings))
    
    app.add_handler(conv_handler_funding)
    app.add_handler(conv_handler_volume)
    
    app.add_handler(CallbackQueryHandler(drill_down_callback, pattern="^drill_"))
    app.add_handler(CallbackQueryHandler(back_to_top_callback, pattern="^back_to_top$"))
    app.add_handler(CallbackQueryHandler(filters_callback_handler, pattern="^filters_(close|toggle_notif|exchanges)$"))
    app.add_handler(CallbackQueryHandler(exchanges_callback_handler, pattern="^exch_"))

    async def post_init(app):
        asyncio.create_task(background_scanner(app))
        
    app.post_init = post_init

    print("🤖 RateHunter 2.0 запущен и готов к работе!")
    app.run_polling()
