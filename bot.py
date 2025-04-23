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

BOT_TOKEN = os.getenv("BOT_TOKEN")
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")

session = HTTP(api_key=BYBIT_API_KEY, api_secret=BYBIT_API_SECRET)

keyboard = [["📊 Топ 5 funding-пар"], ["📈 Расчёт прибыли"], ["📡 Сигналы"], ["🔧 Установить маржу"]]
latest_top_pairs = []
sniper_active = {}

# NEW: Обновленные состояния для разделения реальной и тестовой маржи
SET_REAL_MARJA = 0  # Для установки реальной маржи
CALC_TEST_SUM, CALC_TEST_MARJA, CALC_PLECHO = range(1, 4)  # Для тестовых расчетов

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text("Привет! Выбери действие:", reply_markup=reply_markup)

async def show_top_funding(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        response = session.get_tickers(category="linear")
        tickers = response["result"]["list"]
        funding_data = []

        for t in tickers:
            symbol = t["symbol"]
            rate = t.get("fundingRate")
            next_time = t.get("nextFundingTime")
            try:
                rate = float(rate)
                funding_data.append((symbol, rate, int(next_time)))
            except:
                continue

        funding_data.sort(key=lambda x: abs(x[1]), reverse=True)
        global latest_top_pairs
        latest_top_pairs = funding_data[:5]

        msg = "📊 Топ 5 funding-пар:\n\n"
        now_ts = datetime.utcnow().timestamp()
        for symbol, rate, ts in latest_top_pairs:
            delta_sec = int(ts / 1000 - now_ts)
            h, m = divmod(delta_sec // 60, 60)
            time_left = f"{h}ч {m}м"
            direction = "📈 LONG" if rate < 0 else "📉 SHORT"
            msg += f"{symbol} — {rate * 100:.4f}% → {direction} ⏱ через {time_left}\n"

        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"Ошибка при получении топа: {e}")

# NEW: Обновленный блок для установки РЕАЛЬНОЙ маржи (для автоматических сделок)
async def set_real_marja(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💰 Введите сумму РЕАЛЬНОЙ маржи (в USDT) для автоматических сделок:\n"
        "(Будет проверен баланс на Bybit)"
    )
    return SET_REAL_MARJA

async def save_real_marja(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        marja = float(update.message.text)
        chat_id = update.effective_chat.id

        balance = session.get_wallet_balance(accountType="UNIFIED")
        usdt_balance = float(balance["result"]["list"][0]["totalEquity"])
        if marja > usdt_balance:
            await update.message.reply_text("❌ Недостаточно средств. Пополните баланс.")
            return ConversationHandler.END

        if chat_id not in sniper_active:
            sniper_active[chat_id] = {}
        sniper_active[chat_id]['real_marja'] = marja
        await update.message.reply_text(f"✅ РЕАЛЬНАЯ маржа установлена: {marja} USDT")
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text("Ошибка. Убедитесь, что ввели число.")
        return SET_REAL_MARJA

# NEW: Полностью переработанный блок для ТЕСТОВЫХ расчетов
async def start_calc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🧮 Введите ТЕСТОВУЮ сумму для расчета (в USDT):\n"
        "(Это виртуальная сумма для симуляции)"
    )
    return CALC_TEST_SUM

async def set_test_sum(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data['test_sum'] = float(update.message.text)
        await update.message.reply_text(
            "📈 Введите ТЕСТОВУЮ маржу для расчета ROI (в USDT):\n"
            "(Это виртуальная маржа, не связанная с реальной)"
        )
        return CALC_TEST_MARJA
    except:
        await update.message.reply_text("❌ Некорректная сумма. Введите число:")
        return CALC_TEST_SUM

async def set_test_marja(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data['test_marja'] = float(update.message.text)
        await update.message.reply_text("↔️ Введите плечо для расчета (например, 5):")
        return CALC_PLECHO
    except:
        await update.message.reply_text("❌ Некорректная маржа. Введите число:")
        return CALC_TEST_MARJA

async def set_calc_plecho(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        test_sum = context.user_data['test_sum']
        test_marja = context.user_data['test_marja']
        plecho = float(update.message.text)
        position = test_sum * plecho

        if not latest_top_pairs:
            await update.message.reply_text("ℹ️ Сначала получите актуальные пары через '📊 Топ 5 funding-пар'")
            return ConversationHandler.END

        msg = (
            f"📊 <b>ТЕСТОВЫЙ РАСЧЁТ</b>\n"
            f"• Виртуальная сумма: {test_sum} USDT\n"
            f"• Тестовая маржа: {test_marja} USDT\n"
            f"• Плечо: {plecho}x\n\n"
        )
        
        for symbol, rate, _ in latest_top_pairs:
            gross = position * abs(rate)
            fees = position * 0.0006
            spread = position * 0.0002
            net = gross - fees - spread
            roi = (net / test_marja) * 100
            direction = "📈 LONG" if rate < 0 else "📉 SHORT"
            warn = "⚠️ Нерентабельно" if net < 0 else ""
            
            msg += (
                f"<b>{symbol}</b> → {direction}\n"
                f"  📊 Фандинг: {rate * 100:.4f}%\n"
                f"  💰 Чистая прибыль: {net:.2f} USDT\n"
                f"  📈 ROI: {roi:.2f}% {warn}\n\n"
            )
        
        # Добавляем информацию о реальной марже для сравнения
        real_marja = sniper_active.get(update.effective_chat.id, {}).get('real_marja')
        if real_marja:
            msg += f"ℹ️ <i>Ваша РЕАЛЬНАЯ маржа: {real_marja} USDT</i>"
        
        await update.message.reply_html(msg)
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"⚠️ Ошибка: {e}. Начните расчёт заново.")
        return ConversationHandler.END

# ... (остальные функции signal_menu, signal_callback, cancel, funding_sniper_loop остаются без изменений)

if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Regex("📊 Топ 5 funding-пар"), show_top_funding))
    app.add_handler(MessageHandler(filters.Regex("📡 Сигналы"), signal_menu))
    app.add_handler(CallbackQueryHandler(signal_callback))

    # NEW: Обновленный обработчик для тестовых расчетов
    conv_calc = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("📈 Расчёт прибыли"), start_calc)],
        states={
            CALC_TEST_SUM: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_test_sum)],
            CALC_TEST_MARJA: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_test_marja)],
            CALC_PLECHO: [MessageHandler(filters.TEXT & ~filters.COMMAND, set_calc_plecho)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv_calc)

    # NEW: Отдельный обработчик для установки РЕАЛЬНОЙ маржи
    conv_real_marja = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("🔧 Установить маржу"), set_real_marja)],
        states={
            SET_REAL_MARJA: [MessageHandler(filters.TEXT & ~filters.COMMAND, save_real_marja)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv_real_marja)

    async def on_startup(app):
        asyncio.create_task(funding_sniper_loop(app))

    app.post_init = on_startup
    app.run_polling()
