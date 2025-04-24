import os
import asyncio
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes,
    ConversationHandler, CallbackQueryHandler, filters
)
from pybit.unified_trading import HTTP
from dotenv import load_dotenv

load_dotenv()

# --- Конфигурация ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")

# --- Инициализация ---
session = HTTP(
    api_key=BYBIT_API_KEY,
    api_secret=BYBIT_API_SECRET,
    testnet=False  # True для тестовой сети
)

# Принудительная настройка режима
try:
    session.set_position_mode(category="linear", mode=0)  # Односторонний режим
    print("⚙️ Режим позиции: Односторонний")
except Exception as e:
    print(f"❌ Ошибка настройки режима: {e}")

keyboard = [
    ["📊 Топ-пары", "🧮 Калькулятор"],
    ["💰 Установить маржу", "⚖ Установить плечо"],
    ["📡 Управление сигналами"]
]
latest_top_pairs = []
sniper_active = {}

# --- Состояния ---
SET_MARJA, SET_PLECHO = range(2)

# --- Улучшенный контроль баланса ---
async def get_usdt_balance():
    """Возвращает доступный баланс USDT с обработкой всех форматов ответа"""
    try:
        balance = session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
        balance_data = balance.get("result", {}).get("list", [{}])[0]
        
        return float(
            balance_data.get("availableBalance") or
            balance_data.get("walletBalance") or
            balance_data.get("totalEquity") or
            0
        )
    except Exception as e:
        print(f"🚨 Ошибка получения баланса: {e}")
        return 0

# --- Основные функции ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(
        "🚀 Бот готов к работе!\n"
        "Выберите действие:",
        reply_markup=reply_markup
    )

async def show_top_pairs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает топ USDT-пар с фандингом"""
    try:
        response = session.get_tickers(category="linear")
        usdt_pairs = [t for t in response["result"]["list"] if t["symbol"].endswith("USDT")]
        
        if not usdt_pairs:
            await update.message.reply_text("❌ Нет доступных USDT-пар")
            return

        # Сортировка по абсолютному значению фандинга
        top_pairs = sorted(
            [(t["symbol"], float(t["fundingRate"]), int(t["nextFundingTime"])) 
            for t in usdt_pairs if t.get("fundingRate")],
            key=lambda x: abs(x[1]),
            reverse=True
        )[:5]

        global latest_top_pairs
        latest_top_pairs = top_pairs

        msg = "📊 Топ USDT-пар:\n\n"
        for symbol, rate, ts in top_pairs:
            time_left = datetime.fromtimestamp(ts/1000).strftime("%H:%M:%S")
            msg += (
                f"▪️ {symbol}\n"
                f"Фандинг: {rate*100:.4f}% | "
                f"Выплата: {time_left}\n"
                f"Направление: {'LONG' if rate < 0 else 'SHORT'}\n\n"
            )
        
        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"⚠️ Ошибка: {str(e)}")

# --- Управление маржой и плечом ---
async def set_marja(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💰 Введите сумму маржи в USDT:\n"
        "(Минимум 10 USDT)"
    )
    return SET_MARJA

async def save_marja(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        marja = float(update.message.text)
        if marja < 10:
            await update.message.reply_text("❌ Минимум 10 USDT")
            return SET_MARJA
            
        available = await get_usdt_balance()
        if marja > available:
            await update.message.reply_text(
                f"❌ Недостаточно USDT. Доступно: {available:.2f}"
            )
            return SET_MARJA

        chat_id = update.effective_chat.id
        sniper_active[chat_id] = {
            **sniper_active.get(chat_id, {}),
            "real_marja": marja
        }
        await update.message.reply_text(f"✅ Маржа: {marja} USDT")
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("❌ Введите число (например: 100)")
        return SET_MARJA

async def set_plecho(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("↔️ Введите плечо (1-100):")
    return SET_PLECHO

async def save_plecho(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        plecho = int(update.message.text)
        if not 1 <= plecho <= 100:
            await update.message.reply_text("❌ Допустимо 1-100")
            return SET_PLECHO

        chat_id = update.effective_chat.id
        sniper_active[chat_id] = {
            **sniper_active.get(chat_id, {}),
            "real_plecho": plecho
        }
        await update.message.reply_text(f"✅ Плечо: {plecho}x")
        return ConversationHandler.END
    except ValueError:
        await update.message.reply_text("❌ Введите целое число")
        return SET_PLECHO

# --- Торговая логика ---
async def execute_trade(symbol: str, direction: str, chat_id: int):
    """Безопасное исполнение сделки"""
    try:
        # Проверка настроек
        settings = sniper_active.get(chat_id, {})
        if not settings.get("real_marja") or not settings.get("real_plecho"):
            await app.bot.send_message(chat_id, "❌ Не установлены маржа/плечо")
            return False

        # Получаем параметры символа
        symbol_info = session.get_instruments_info(category="linear", symbol=symbol)
        if not symbol_info["result"]["list"]:
            await app.bot.send_message(chat_id, f"❌ Пара {symbol} не найдена")
            return False

        # Расчет объема
        min_qty = float(symbol_info["result"]["list"][0]["lotSizeFilter"]["minOrderQty"])
        qty_step = float(symbol_info["result"]["list"][0]["lotSizeFilter"]["qtyStep"])
        position_size = settings["real_marja"] * settings["real_plecho"]
        adjusted_qty = max(min_qty, round(position_size / qty_step) * qty_step)

        # Проверка баланса
        if adjusted_qty > await get_usdt_balance():
            await app.bot.send_message(chat_id, "❌ Недостаточно USDT для сделки")
            return False

        # Открытие позиции
        order = session.place_order(
            category="linear",
            symbol=symbol,
            side="Buy" if direction == "LONG" else "Sell",
            order_type="Market",
            qty=adjusted_qty,
            time_in_force="FillOrKill",
            position_idx=0
        )

        # Успешное исполнение
        await app.bot.send_message(
            chat_id,
            f"✅ Сделка исполнена:\n"
            f"• {symbol} {direction}\n"
            f"• Объем: {adjusted_qty:.2f} USDT\n"
            f"• ID: {order['result']['orderId']}"
        )
        return True

    except Exception as e:
        await app.bot.send_message(
            chat_id,
            f"⛔ Ошибка исполнения:\n{str(e)}"
        )
        return False

async def funding_sniper(app):
    """Основной торговый цикл"""
    while True:
        try:
            # Получаем USDT-пары
            tickers = session.get_tickers(category="linear")["result"]["list"]
            usdt_pairs = [t for t in tickers if t["symbol"].endswith("USDT")]

            # Анализ фандинга
            valid_pairs = []
            for t in usdt_pairs:
                try:
                    rate = float(t["fundingRate"])
                    valid_pairs.append((
                        t["symbol"],
                        rate,
                        int(t["nextFundingTime"])
                    ))
                except:
                    continue

            if not valid_pairs:
                await asyncio.sleep(30)
                continue

            # Выбор лучшей пары
            best_pair = max(valid_pairs, key=lambda x: abs(x[1]))
            symbol, rate, next_ts = best_pair
            time_left = (next_ts - int(datetime.now().timestamp()*1000)) // 60000

            if 0 <= time_left <= 1:  # За 1 минуту до выплаты
                direction = "LONG" if rate < 0 else "SHORT"
                
                for chat_id in list(sniper_active.keys()):
                    if sniper_active[chat_id].get("active"):
                        await execute_trade(symbol, direction, chat_id)
                        await asyncio.sleep(1)  # Задержка между чатами

        except Exception as e:
            print(f"🔴 Цикл прерван: {e}")
        finally:
            await asyncio.sleep(30)

# --- Запуск ---
if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Обработчики
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("📊 Топ-пары"), show_top_pairs))
    
    # Установка параметров
    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("💰 Установить маржу"), set_marja)],
        states={SET_MARJA: [MessageHandler(filters.TEXT, save_marja)]},
        fallbacks=[]
    ))
    
    app.add_handler(ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("⚖ Установить плечо"), set_plecho)],
        states={SET_PLECHO: [MessageHandler(filters.TEXT, save_plecho)]},
        fallbacks=[]
    ))

    # Сигналы
    app.add_handler(MessageHandler(filters.Regex("📡 Управление сигналами"), signal_menu))
    app.add_handler(CallbackQueryHandler(signal_callback))

    # Фоновая задача
    async def on_startup(_):
        asyncio.create_task(funding_sniper(app))
    
    app.post_init = on_startup
    print("🟢 Бот запущен")
    app.run_polling()
