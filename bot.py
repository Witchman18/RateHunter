# =========================================================================
# ===================== RateHunter 2.0 - Alpha v0.2.3 ===================
# =========================================================================
# Исправления в этой версии:
# - ИСПРАВЛЕНА ОШИБКА: Добавлена обработка кнопок "Ставка" и "Объем" в фильтрах
# - Исправлен паттерн регистрации обработчика для всех кнопок фильтров
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
BOT_TOKEN = os.getenv("BOT_TOKEN")

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
                            results.append({
                                'exchange': 'Bybit', 'symbol': t.get("symbol"),
                                'rate': Decimal(t.get("fundingRate")), 'next_funding_time': int(t.get("nextFundingTime")),
                                'volume_24h_usdt': Decimal(t.get("turnover24h")), 'max_order_value_usdt': Decimal('0'),
                                'trade_url': f'https://www.bybit.com/trade/usdt/{t.get("symbol")}'
                            })
                        except (TypeError, ValueError, decimal.InvalidOperation): continue
    except Exception as e: print(f"[API_ERROR] Bybit: {e}")
    return results

async def get_mexc_data():
    mexc_url = "https://contract.mexc.com/api/v1/contract/detail"
    results = []
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(mexc_url) as response:
                response.raise_for_status()
                data = await response.json()
                if data.get("success") and data.get("data"):
                    for t in data["data"]:
                         if t.get("quoteCoin") != "USDT" or t.get("state") != "SHOW": continue
                         try:
                            symbol = t.get("symbol").replace("_", "")
                            results.append({
                                'exchange': 'MEXC', 'symbol': symbol,
                                'rate': Decimal(str(t.get("fundingRate"))), 'next_funding_time': int(t.get("nextSettleTime")),
                                'volume_24h_usdt': Decimal(str(t.get("volume24"))), 'max_order_value_usdt': Decimal(str(t.get("maxVol"))),
                                'trade_url': f'https://futures.mexc.com/exchange/{t.get("symbol")}'
                            })
                         except (TypeError, ValueError, decimal.InvalidOperation): continue
    except Exception as e: print(f"[API_ERROR] MEXC: {e}")
    return results

async def fetch_all_data(force_update=False):
    now = datetime.now().timestamp()
    if not force_update and api_data_cache["last_update"] and (now - api_data_cache["last_update"] < CACHE_LIFETIME_SECONDS):
        return api_data_cache["data"]
    tasks = [get_bybit_data(), get_mexc_data()]
    results_from_tasks = await asyncio.gather(*tasks, return_exceptions=True)
    all_data = []
    for res in results_from_tasks:
        if isinstance(res, list): all_data.extend(res)
    api_data_cache["data"], api_data_cache["last_update"] = all_data, now
    return all_data


# =================================================================
# ================== ПОЛЬЗОВАТЕЛЬСКИЙ ИНТЕРФЕЙС (UI) ==================
# =================================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user_settings(update.effective_chat.id)
    main_menu_keyboard = [["🔥 Топ-ставки сейчас"], ["🔔 Настроить фильтры", "ℹ️ Мои настройки"]]
    reply_markup = ReplyKeyboardMarkup(main_menu_keyboard, resize_keyboard=True)
    await update.message.reply_text(
        "Добро пожаловать в RateHunter 2.0!", reply_markup=reply_markup
    )

async def show_top_rates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    ensure_user_settings(chat_id)
    settings = user_settings[chat_id]
    message_to_edit = update.callback_query.message if update.callback_query else await update.message.reply_text("🔄 Ищу...")
    await message_to_edit.edit_text("🔄 Ищу лучшие ставки по вашим фильтрам...")
    all_data = await fetch_all_data()
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
    now_ts = datetime.now().timestamp()
    for item in top_5:
        symbol_only = item['symbol'].replace("USDT", "")
        time_left_seconds = (item['next_funding_time'] / 1000) - now_ts
        hours, rem = divmod(time_left_seconds, 3600); minutes, _ = divmod(rem, 60)
        time_str = f"{int(hours):02d}:{int(minutes):02d}" if time_left_seconds > 0 else "Сейчас"
        direction_emoji = "🟢" if item['rate'] > 0 else "🔴"
        rate_str = f"{item['rate'] * 100:+.2f}%"
        message_text += f"{direction_emoji} *{symbol_only}* `{rate_str}` до `{time_str}` [{item['exchange']}]\n"
        buttons.append(InlineKeyboardButton(symbol_only, callback_data=f"drill_{item['symbol']}"))
    keyboard = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    await message_to_edit.edit_text(
        message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown', disable_web_page_preview=True
    )
    
async def drill_down_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    symbol_to_show = query.data.split('_')[1]
    all_data = api_data_cache.get("data", []);
    if not all_data:
        all_data = await fetch_all_data(force_update=True)
    symbol_specific_data = [item for item in all_data if item['symbol'] == symbol_to_show]
    symbol_specific_data.sort(key=lambda x: abs(x['rate']), reverse=True)
    symbol_only = symbol_to_show.replace("USDT", "")
    message_text = f"💎 **Детали по {symbol_only}**\n\n"
    now_ts = datetime.now().timestamp()
    for item in symbol_specific_data:
        time_left_seconds = (item['next_funding_time'] / 1000) - now_ts
        hours, rem = divmod(time_left_seconds, 3600); minutes, _ = divmod(rem, 60)
        time_str = f"{int(hours):02d}:{int(minutes):02d}" if time_left_seconds > 0 else "Сейчас"
        direction_emoji = "🟢" if item['rate'] > 0 else "🔴"; rate_str = f"{item['rate'] * 100:+.2f}%"
        message_text += f"{direction_emoji} `{rate_str}` до `{time_str}` [{item['exchange']}]({item['trade_url']})\n"
    keyboard = [[InlineKeyboardButton("⬅️ Назад к топу", callback_data="back_to_top")]]
    await query.edit_message_text(
        text=message_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown', disable_web_page_preview=True
    )

async def back_to_top_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await query.answer() if (query := update.callback_query) else None
    await show_top_rates(update, context)

# --- БЛОК: Меню "Настроить фильтры" ---

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
        user_settings[query.effective_chat.id]['notifications_on'] ^= True
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
    chat_id = query.effective_chat.id
    
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
    except (ValueError, TypeError, decimal.InvalidOperation) as e:
        error_messages = {
            'funding': "❌ Ошибка. Введите число от 0 до 100 (например, `0.75`).",
            'volume': "❌ Ошибка. Введите целое положительное число (например, `500000`)."
        }
        await update.message.reply_text(error_messages[setting_type] + " Попробуйте снова.", parse_mode='Markdown')
        return SET_FUNDING_THRESHOLD if setting_type == 'funding' else SET_VOLUME_THRESHOLD

    # Очистка
    await context.bot.delete_message(chat_id, context.user_data.pop('prompt_message_id'))
    await context.bot.delete_message(chat_id, update.message.message_id)
    
    await send_filters_menu(update, context)
    return ConversationHandler.END

async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await context.bot.delete_message(chat_id, context.user_data.pop('prompt_message_id', None))
    await context.bot.delete_message(chat_id, update.message.id)
    await context.bot.send_message(chat_id, "Действие отменено.")
    await send_filters_menu(update, context) # Отправляем новое меню
    return ConversationHandler.END

# Заглушка для background_scanner (функция отсутствует в оригинальном коде)
async def background_scanner(app):
    """Фоновый сканер - заглушка"""
    pass

# =================================================================
# ========================== ЗАПУСК БОТА ==========================
# =================================================================
if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    funding_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(lambda u,c: ask_for_value(u,c,'funding'), pattern="^filters_funding$")],
        states={SET_FUNDING_THRESHOLD: [MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u,c: save_value(u,c,'funding'))]},
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
    )
    volume_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(lambda u,c: ask_for_value(u,c,'volume'), pattern="^filters_volume$")],
        states={SET_VOLUME_THRESHOLD: [MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u,c: save_value(u,c,'volume'))]},
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
    )
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("^🔥 Топ-ставки сейчас$"), show_top_rates))
    app.add_handler(MessageHandler(filters.Regex("^🔔 Настроить фильтры$"), filters_menu_entry))
    
    app.add_handler(funding_conv)
    app.add_handler(volume_conv)

    app.add_handler(CallbackQueryHandler(drill_down_callback, pattern="^drill_"))
    app.add_handler(CallbackQueryHandler(back_to_top_callback, pattern="^back_to_top$"))
    # ИСПРАВЛЕНО: убраны funding и volume из паттерна, т.к. они обрабатываются ConversationHandler'ами
    app.add_handler(CallbackQueryHandler(filters_callback_handler, pattern="^filters_(close|toggle_notif|exchanges)$"))
    app.add_handler(CallbackQueryHandler(exchanges_callback_handler, pattern="^exch_"))
    
    async def post_init(app): asyncio.create_task(background_scanner(app))
    app.post_init = post_init
    
    print("Bot is running...")
    app.run_polling()
