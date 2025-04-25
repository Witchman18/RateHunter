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

# Конфигурация
BOT_TOKEN = os.getenv("BOT_TOKEN")
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")

# Инициализация
session = HTTP(api_key=BYBIT_API_KEY, api_secret=BYBIT_API_SECRET)
keyboard = [
    ["📊 Топ-пары", "🧮 Калькулятор прибыли"],
    ["💰 Маржа", "⚖ Плечо"],
    ["📡 Сигналы"]
]
latest_top_pairs = []
sniper_active = {}

# Состояния
SET_MARJA = 0
SET_PLECHO = 1

def get_position_direction(funding_rate: float) -> str:
    """Определяет направление позиции для получения фандинга"""
    if funding_rate > 0:
        return "SHORT"  # Положительная ставка → получаем выплату в SHORT
    elif funding_rate < 0:
        return "LONG"   # Отрицательная ставка → получаем выплату в LONG
    else:
        return "NONE"

# ===================== ОСНОВНЫЕ ФУНКЦИИ =====================

async def show_top_funding(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает топ-5 пар по funding rate"""
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

        msg = "📊 Топ пары:\n\n"
        now_ts = datetime.utcnow().timestamp()
        for symbol, rate, ts in latest_top_pairs:
            delta_sec = int(ts / 1000 - now_ts)
            h, m = divmod(delta_sec // 60, 60)
            time_left = f"{h}ч {m}м"
            direction = get_position_direction(rate)
            if direction == "NONE":
                continue

            msg += (
                f"🎟 {symbol}\n"
                f"{'📉 SHORT' if direction == 'SHORT' else '📈 LONG'} | 📊 {rate * 100:.4f}%\n"
                f"⌛ Выплата через: {time_left}\n\n"
            )

        await update.message.reply_text(msg.strip())
    except Exception as e:
        await update.message.reply_text(f"Ошибка при получении топа: {e}")

# ... (остальные функции start, cancel, set_real_marja, save_real_marja остаются без изменений)

# ===================== ФОНОВАЯ ЗАДАЧА =====================

async def funding_sniper_loop(app):
    while True:
        try:
            now_ts = datetime.utcnow().timestamp()
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

            if not latest_top_pairs:
                await asyncio.sleep(30)
                continue

            top_symbol, rate, next_ts = latest_top_pairs[0]
            minutes_left = int((next_ts / 1000 - now_ts) / 60)

            if 0 <= minutes_left <= 1:
                direction = get_position_direction(rate)
                if direction == "NONE":
                    await asyncio.sleep(30)
                    continue

                for chat_id, data in sniper_active.items():
                    if not data.get('active'):
                        continue

                    if (data.get("last_entry_symbol") == top_symbol and data.get("last_entry_ts") == next_ts):
                        continue

                    marja = data.get('real_marja')
                    plecho = data.get('real_plecho')
                    if not marja or not plecho:
                        continue

                    position_size = marja * plecho
                    gross = position_size * abs(rate)
                    fees = position_size * 0.0006
                    spread = position_size * 0.0002
                    net = gross - fees - spread
                    roi = (net / marja) * 100

                    await app.bot.send_message(
                        chat_id,
                        f"📡 Сигнал обнаружен: {top_symbol}\n"
                        f"{'📉 SHORT' if direction == 'SHORT' else '📈 LONG'} | 📊 {rate * 100:.4f}%\n"
                        f"💼 {marja} USDT x{plecho} | 💰 Доход: {net:.2f} USDT\n"
                        f"⏱ Вход через 1 минуту"
                    )

                    try:
                        # Получаем информацию о символе
                        info = session.get_instruments_info(category="linear", symbol=top_symbol)
                        filters = info["result"]["list"][0]["lotSizeFilter"]
                        min_qty = float(filters["minOrderQty"])
                        step = float(filters["qtyStep"])

                        # Получаем текущую цену
                        ticker_info = session.get_tickers(category="linear", symbol=top_symbol)
                        last_price = float(ticker_info["result"]["list"][0]["lastPrice"])
                        raw_qty = position_size / last_price
                        adjusted_qty = raw_qty - (raw_qty % step)

                        if adjusted_qty < min_qty:
                            await app.bot.send_message(
                                chat_id,
                                f"⚠️ Сделка по {top_symbol} не открыта: объём {adjusted_qty:.6f} меньше минимального ({min_qty})"
                            )
                            continue

                        # Устанавливаем плечо (с обработкой ошибки)
                        try:
                            session.set_leverage(
                                category="linear",
                                symbol=top_symbol,
                                buyLeverage=str(plecho),
                                sellLeverage=str(plecho)
                            )
                        except Exception as e:
                            if "leverage not modified" in str(e):
                                # Плечо уже установлено - пропускаем ошибку
                                pass
                            else:
                                raise e

                        # Открываем позицию
                        session.place_order(
                            category="linear",
                            symbol=top_symbol,
                            side="Sell" if direction == "SHORT" else "Buy",
                            order_type="Market",
                            qty=str(adjusted_qty),  # Важно передавать как строку
                            time_in_force="FillOrKill"
                        )

                        sniper_active[chat_id]["last_entry_symbol"] = top_symbol
                        sniper_active[chat_id]["last_entry_ts"] = next_ts

                        # Ждём момент выплаты фандинга
                        now = datetime.utcnow().timestamp()
                        delay = (next_ts / 1000) - now
                        if delay > 0:
                            await asyncio.sleep(delay)

                        await asyncio.sleep(10)  # Дополнительное ожидание

                        # Закрываем позицию
                        close_side = "Buy" if direction == "SHORT" else "Sell"
                        session.place_order(
                            category="linear",
                            symbol=top_symbol,
                            side=close_side,
                            order_type="Market",
                            qty=str(adjusted_qty),
                            time_in_force="FillOrKill"
                        )

                        await app.bot.send_message(
                            chat_id,
                            f"✅ Сделка завершена: {top_symbol} ({direction})\n"
                            f"💸 Профит: {net:.2f} USDT | 📈 ROI: {roi:.2f}%"
                        )

                    except Exception as e:
                        await app.bot.send_message(
                            chat_id,
                            f"❌ Ошибка при открытии/закрытии сделки по {top_symbol}:\n{str(e)}"
                        )

        except Exception as e:
            print(f"[Sniper Error] {e}")

        await asyncio.sleep(30)

# ... (остальной код остаётся без изменений)
