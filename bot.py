# --- START OF FILE bot (9)_debug_balance_check_commented.py ---

import os
import asyncio
import time
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN

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
session = HTTP(api_key=BYBIT_API_KEY, api_secret=BYBIT_API_SECRET, recv_window=20000)
keyboard = [
    ["📊 Топ-пары", "🧮 Калькулятор прибыли"],
    ["💰 Маржа", "⚖️ Плечо"],
    ["📡 Сигналы"]
]
latest_top_pairs = []
sniper_active = {}

# Состояния
SET_MARJA = 0
SET_PLECHO = 1

# Константы
ENTRY_WINDOW_START_SECONDS = 25
ENTRY_WINDOW_END_SECONDS = 10
POST_FUNDING_WAIT_SECONDS = 7
MAKER_ORDER_WAIT_SECONDS_ENTRY = 2
MAKER_ORDER_WAIT_SECONDS_EXIT = 5
SNIPER_LOOP_INTERVAL_SECONDS = 5
MIN_USDT_BALANCE_CHECK = Decimal("10")

# ... (Все функции от show_top_funding до quantize_price остаются БЕЗ ИЗМЕНЕНИЙ,
# я их скопирую из предыдущей версии для полноты) ...

async def show_top_funding(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; message = update.message; chat_id = update.effective_chat.id
    loading_message_id = None
    try:
        if query:
            await query.answer()
            try: await query.edit_message_text("🔄 Получаю топ пар..."); loading_message_id = query.message.message_id
            except Exception: sent_message = await context.bot.send_message(chat_id, "🔄 Получаю топ пар..."); loading_message_id = sent_message.message_id
        elif message: sent_message = await message.reply_text("🔄 Получаю топ пар..."); loading_message_id = sent_message.message_id
        else: return
        response = session.get_tickers(category="linear"); tickers = response.get("result", {}).get("list", [])
        if not tickers:
            result_msg = "⚠️ Не удалось получить данные тикеров."
            if loading_message_id: await context.bot.edit_message_text(chat_id=chat_id, message_id=loading_message_id, text=result_msg)
            return
        funding_data = []
        for t in tickers:
            symbol, rate_str, next_ts_str, _, turnover_str = t.get("symbol"), t.get("fundingRate"), t.get("nextFundingTime"), t.get("volume24h"), t.get("turnover24h")
            if not all([symbol, rate_str, next_ts_str, turnover_str]): continue
            try:
                 rate_f, next_time_int, turnover_f = float(rate_str), int(next_ts_str), float(turnover_str)
                 if turnover_f < 1_000_000 or abs(rate_f) < 0.0001: continue
                 funding_data.append((symbol, rate_f, next_time_int))
            except: continue
        funding_data.sort(key=lambda x: abs(x[1]), reverse=True)
        global latest_top_pairs; latest_top_pairs = funding_data[:5]
        if not latest_top_pairs: result_msg = "📊 Нет подходящих пар."
        else:
            result_msg = "📊 Топ ликвидных пар по фандингу:\n\n"; now_ts = datetime.utcnow().timestamp()
            for symbol, rate, ts in latest_top_pairs:
                try:
                    delta_sec = int(ts / 1000 - now_ts);
                    if delta_sec < 0: delta_sec = 0
                    h, rem = divmod(delta_sec, 3600); m, s = divmod(rem, 60)
                    time_left = f"{h:01d}ч {m:02d}м {s:02d}с"
                    direction = "📈 LONG (шорты платят)" if rate < 0 else "📉 SHORT (лонги платят)"
                    result_msg += (f"🎟️ *{symbol}*\n{direction}\n💹 Фандинг: `{rate * 100:.4f}%`\n⌛ Выплата через: `{time_left}`\n\n")
                except: result_msg += f"🎟️ *{symbol}* - _ошибка_\n\n"
        if loading_message_id: await context.bot.edit_message_text(chat_id=chat_id, message_id=loading_message_id, text=result_msg.strip(), parse_mode='Markdown', disable_web_page_preview=True)
    except Exception as e:
        print(f"Error show_top_funding: {e}"); import traceback; traceback.print_exc()
        error_message = f"❌ Ошибка топа: {e}"
        try:
            if loading_message_id: await context.bot.edit_message_text(chat_id=chat_id, message_id=loading_message_id, text=error_message)
            elif message: await message.reply_text(error_message)
            elif query: await query.message.reply_text(error_message)
        except: await context.bot.send_message(chat_id, "❌ Внутр. ошибка.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text("Привет! Я фандинг-бот RateHunter. Выбери действие:", reply_markup=reply_markup)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Действие отменено."); return ConversationHandler.END

async def set_real_marja(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("💰 Введите сумму РЕАЛЬНОЙ маржи для ОДНОЙ сделки (в USDT):"); return SET_MARJA

async def save_real_marja(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        marja = Decimal(update.message.text.strip().replace(",", "."))
        if marja <= 0: await update.message.reply_text("❌ Маржа должна быть > 0."); return ConversationHandler.END
        sniper_active.setdefault(chat_id, {})["real_marja"] = marja
        await update.message.reply_text(f"✅ Маржа: {marja} USDT")
    except: await update.message.reply_text("❌ Неверный формат."); return SET_MARJA
    return ConversationHandler.END

async def set_real_plecho(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⚖️ Введите размер плеча (например, 5 или 10):"); return SET_PLECHO

async def save_real_plecho(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        plecho = Decimal(update.message.text.strip().replace(",", "."))
        if not (0 < plecho <= 100): await update.message.reply_text("❌ Плечо (0, 100]."); return ConversationHandler.END
        sniper_active.setdefault(chat_id, {})["real_plecho"] = plecho
        await update.message.reply_text(f"✅ Плечо: {plecho}x")
    except: await update.message.reply_text("❌ Неверный формат."); return SET_PLECHO
    return ConversationHandler.END

async def signal_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id; is_active = sniper_active.get(chat_id, {}).get('active', False)
    status_text = "🟢 Активен" if is_active else "🔴 Остановлен"
    buttons = [[InlineKeyboardButton(f"Статус: {status_text}", callback_data="toggle_sniper")],
               [InlineKeyboardButton("📊 Показать топ пар", callback_data="show_top_pairs_inline")]]
    reply_markup = InlineKeyboardMarkup(buttons)
    await update.message.reply_text("📡 Меню управления снайпером:", reply_markup=reply_markup)

async def signal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; chat_id = query.message.chat_id; data = query.data
    # === ИЗМЕНЕНИЕ для отладки: query.answer() в начале + логи ===
    await query.answer() 
    print(f"Callback received: chat_id={chat_id}, data='{data}'")
    # ============================================================
    if data == "toggle_sniper":
        print(f"Processing 'toggle_sniper' for chat_id={chat_id}")
        try:
            sniper_active.setdefault(chat_id, {'active': False})
            print(f"Current sniper_active for {chat_id}: {sniper_active[chat_id]}")
            new_status = not sniper_active[chat_id]['active']
            sniper_active[chat_id]['active'] = new_status
            print(f"New status for {chat_id}: {new_status}")
            status_text = "🟢 Активен" if new_status else "🔴 Остановлен"
            action_text = "🚀 Снайпер запущен!" if new_status else "🛑 Снайпер остановлен."
            buttons = [[InlineKeyboardButton(f"Статус: {status_text}", callback_data="toggle_sniper")],
                       [InlineKeyboardButton("📊 Показать топ пар", callback_data="show_top_pairs_inline")]]
            reply_markup = InlineKeyboardMarkup(buttons)
            print(f"Attempting to edit message for {chat_id} with text: '{action_text}'")
            await query.edit_message_text(f"{action_text}\n📡 Меню:", reply_markup=reply_markup) # Убрал "управления снайпером" для краткости
            print(f"Message edited successfully for {chat_id}")
        except Exception as e:
            print(f"!!! ERROR in 'toggle_sniper' for {chat_id}: {e}"); import traceback; traceback.print_exc()
            await context.bot.send_message(chat_id, f"Произошла ошибка при изменении статуса.\n{e}")
    elif data == "show_top_pairs_inline": 
        print(f"Processing 'show_top_pairs_inline' for chat_id={chat_id}")
        await show_top_funding(update, context)

def get_position_direction(rate: float) -> str:
    if rate is None: return "NONE"
    return "Buy" if rate < 0 else ("Sell" if rate > 0 else "NONE")

def quantize_qty(raw_qty: Decimal, qty_step: Decimal) -> Decimal:
    return (raw_qty // qty_step) * qty_step if qty_step > 0 else raw_qty

def quantize_price(raw_price: Decimal, tick_size: Decimal) -> Decimal:
    return round(raw_price / tick_size) * tick_size if tick_size > 0 else raw_price

# ===================== ФОНДОВЫЙ СНАЙПЕР (ФАНДИНГ-БОТ) =====================

async def funding_sniper_loop(app: ApplicationBuilder):
    print(" Sniper loop started ".center(50, "="))
    while True:
        await asyncio.sleep(SNIPER_LOOP_INTERVAL_SECONDS)
        try:
            now_ts = time.time()
            response = session.get_tickers(category="linear")
            tickers = response.get("result", {}).get("list", [])
            if not tickers: print("No tickers."); continue
            funding_data = []
            for t in tickers:
                symbol, rate_str, next_ts_str, _, turnover_str = t.get("symbol"), t.get("fundingRate"), t.get("nextFundingTime"), t.get("volume24h"), t.get("turnover24h")
                if not all([symbol, rate_str, next_ts_str, turnover_str]): continue
                try:
                    rate_f, next_ts_val, turnover_f = float(rate_str), int(next_ts_str) / 1000, float(turnover_str)
                    if turnover_f < 1_000_000 or abs(rate_f) < 0.0001: continue
                    funding_data.append({"symbol": symbol, "rate": rate_f, "next_ts": next_ts_val})
                except: continue
            if not funding_data: print("No suitable pairs."); continue
            funding_data.sort(key=lambda x: abs(x["rate"]), reverse=True)
            top_pair = funding_data[0]
            top_symbol, rate, next_funding_ts = top_pair["symbol"], top_pair["rate"], top_pair["next_ts"]
            seconds_left = next_funding_ts - now_ts

            if ENTRY_WINDOW_END_SECONDS <= seconds_left <= ENTRY_WINDOW_START_SECONDS:
                print(f"Entering trade window for {top_symbol} ({seconds_left:.0f}s left)")
                open_side = get_position_direction(rate)
                if open_side == "NONE": print("Funding rate is zero, skipping."); continue

                for chat_id, data in list(sniper_active.items()):
                    if not data.get('active'): continue
                    if (data.get("last_entry_symbol") == top_symbol and data.get("last_entry_ts") == next_funding_ts): continue
                    marja, plecho = data.get('real_marja'), data.get('real_plecho')
                    if not marja or not plecho: continue
                    
                    # === ЗАКОММЕНТИРОВАНА ПРОВЕРКА БАЛАНСА ДЛЯ ТЕСТА КНОПКИ ===
                    # try:
                    #     wallet_info = session.get_wallet_balance(accountType="UNIFIED") 
                    #     usdt_balance_data = next((item for item in wallet_info.get("result",{}).get("list",[{}])[0].get("coin",[]) if item.get("coin") == "USDT"), None)
                    #     if usdt_balance_data and Decimal(usdt_balance_data.get("walletBalance", "0")) < MIN_USDT_BALANCE_CHECK:
                    #         print(f"Chat {chat_id}: Low USDT balance ({usdt_balance_data.get('walletBalance')}), skipping trade for {top_symbol}")
                    #         continue 
                    # except Exception as e:
                    #     print(f"Error checking wallet balance for chat {chat_id}: {e}")
                    #     continue
                    # ===========================================================

                    print(f"\n>>> Processing {top_symbol} for chat {chat_id} <<<")
                    # ... (остальная часть funding_sniper_loop как в bot (9).py) ...
                    # Этот блок остается таким же, как в предыдущей полной версии,
                    # включая новую проверку get_positions и исправленную проверку фандинга.
                    # Я не буду его здесь повторять для краткости, но он должен быть полностью скопирован.
                    # Главное, что выше ЗАКОММЕНТИРОВАНА проверка баланса.

                    # === Начало блока сделки из bot (9).py ===
                    await app.bot.send_message(chat_id, f"🎯 Вход: *{top_symbol}* ({'📈 L' if open_side == 'Buy' else '📉 S'}), F: `{rate*100:.4f}%`, T: `{seconds_left:.0f}с`", parse_mode='Markdown')
                    position_data = { "opened_qty": Decimal("0"), "avg_open_price": Decimal("0"), "total_open_fee": Decimal("0"), "closed_qty": Decimal("0"), "total_close_value": Decimal("0"), "total_close_fee": Decimal("0"), "actual_funding_fee": Decimal("0")}
                    opened_successfully_flags = {"maker": False, "market": False}
                    try:
                        info_resp = session.get_instruments_info(category="linear", symbol=top_symbol); instrument_info = info_resp.get("result", {}).get("list", [])[0]
                        min_qty, qty_step = Decimal(instrument_info["lotSizeFilter"]["minOrderQty"]), Decimal(instrument_info["lotSizeFilter"]["qtyStep"])
                        tick_size = Decimal(instrument_info["priceFilter"]["tickSize"])
                        ticker_resp = session.get_tickers(category="linear", symbol=top_symbol); last_price = Decimal(ticker_resp["result"]["list"][0]["lastPrice"])
                        raw_qty = (marja * plecho) / last_price; adjusted_qty = quantize_qty(raw_qty, qty_step)
                        if adjusted_qty < min_qty: await app.bot.send_message(chat_id, f"⚠️ Объем {adjusted_qty} < мин {min_qty}"); continue
                        try: session.set_leverage(category="linear", symbol=top_symbol, buyLeverage=str(plecho), sellLeverage=str(plecho))
                        except Exception as e:
                            if "110043" not in str(e): raise ValueError(f"Плечо: {e}")
                        open_qty_rem = adjusted_qty
                        try: # Maker Open
                            ob_resp = session.get_orderbook(category="linear", symbol=top_symbol, limit=1); ob = ob_resp['result']
                            mp = quantize_price(Decimal(ob['b'][0][0] if open_side=="Buy" else ob['a'][0][0]), tick_size)
                            resp = session.place_order(category="linear",symbol=top_symbol,side=open_side,order_type="Limit",qty=str(open_qty_rem),price=str(mp),time_in_force="PostOnly")
                            oid = resp["result"]["orderId"]; await app.bot.send_message(chat_id, f"⏳ Maker вх. @{mp} (ID: ...{oid[-6:]})"); await asyncio.sleep(MAKER_ORDER_WAIT_SECONDS_ENTRY)
                            hist_resp = session.get_order_history(category="linear", orderId=oid, limit=1); hist_list = hist_resp.get("result",{}).get("list",[])
                            if hist_list:
                                h = hist_list[0]; exec_q_str = h.get("cumExecQty","0"); exec_q = Decimal(exec_q_str)
                                if exec_q > 0: opened_successfully_flags["maker"] = True; position_data["total_open_fee"] += Decimal(h.get("cumExecFee","0")); open_qty_rem -= exec_q; await app.bot.send_message(chat_id, f"ℹ️ Maker вх. заявка обработана (исполнено: {exec_q})")
                                if h.get("orderStatus") not in ["Filled","Cancelled","Rejected"]: 
                                    try: session.cancel_order(category="linear",symbol=top_symbol,orderId=oid)
                                    except Exception as cancel_e: print(f"Minor cancel (Maker Open): {cancel_e}")
                        except Exception as e: print(f"Maker Open exc: {e}"); await app.bot.send_message(chat_id, f"⚠️ Maker вх. ошибка: {e}")
                        open_qty_rem = quantize_qty(open_qty_rem, qty_step) # Market Open
                        if open_qty_rem >= min_qty and not opened_successfully_flags["maker"]:
                            await app.bot.send_message(chat_id, f"🛒 Market вх. остаток: {open_qty_rem}")
                            try:
                                resp = session.place_order(category="linear",symbol=top_symbol,side=open_side,order_type="Market",qty=str(open_qty_rem),time_in_force="ImmediateOrCancel")
                                oid = resp["result"]["orderId"]; await asyncio.sleep(1.5)
                                hist_resp = session.get_order_history(category="linear",orderId=oid,limit=1); hist_list = hist_resp.get("result",{}).get("list",[])
                                if hist_list:
                                    h=hist_list[0]; exec_q_str = h.get("cumExecQty","0"); exec_q = Decimal(exec_q_str)
                                    if exec_q > 0: opened_successfully_flags["market"] = True; position_data["total_open_fee"] += Decimal(h.get("cumExecFee","0")); await app.bot.send_message(chat_id, f"ℹ️ Market вх. заявка обработана (исполнено: {exec_q})")
                            except Exception as e: print(f"Market Open exc: {e}"); await app.bot.send_message(chat_id, f"❌ Market вх. ошибка: {e}")
                        final_opened_qty = Decimal("0"); avg_open_price = Decimal("0"); await asyncio.sleep(1)
                        try: # Get Positions Check
                            pos_resp = session.get_positions(category="linear", symbol=top_symbol); pos_list = pos_resp.get("result", {}).get("list", [])
                            if pos_list:
                                current_pos = pos_list[0]; pos_size_str = current_pos.get("size", "0"); pos_side_bybit = current_pos.get("side")
                                if pos_side_bybit == open_side: final_opened_qty = Decimal(pos_size_str); avg_open_price = Decimal(current_pos.get("avgPrice", "0")); print(f"Pos check {top_symbol}: Size={final_opened_qty}, AvgPrice={avg_open_price}, Side={pos_side_bybit}")
                                else: print(f"Pos check {top_symbol}: Found pos but wrong side ({pos_side_bybit} vs {open_side}).")
                            else: print(f"Pos check {top_symbol}: No active position.")
                        except Exception as e: print(f"Error get_positions for {top_symbol}: {e}"); await app.bot.send_message(chat_id, f"⚠️ Ошибка проверки открытой позиции по {top_symbol}.")
                        position_data["opened_qty"] = final_opened_qty; position_data["avg_open_price"] = avg_open_price
                        if final_opened_qty < min_qty: await app.bot.send_message(chat_id, f"❌ Не удалось открыть позицию по *{top_symbol}* (API позиций). Открыто: {final_opened_qty}. Отмена.", parse_mode='Markdown'); continue
                        await app.bot.send_message(chat_id, f"✅ Позиция *{top_symbol}* ({'L' if open_side=='Buy' else 'S'}) открыта (API).\nОбъем: `{final_opened_qty}`, Ср.цена: `{avg_open_price}`, Ком.откр (из ордеров): `{position_data['total_open_fee']:.4f}`", parse_mode='Markdown')
                        data["last_entry_symbol"], data["last_entry_ts"] = top_symbol, next_funding_ts
                        wait_duration = max(0, next_funding_ts - time.time()) + POST_FUNDING_WAIT_SECONDS # Funding Wait
                        await app.bot.send_message(chat_id, f"⏳ Ожидаю фандинг (~{wait_duration:.0f} сек)..."); await asyncio.sleep(wait_duration)
                        print("Checking funding via Transaction Log...") # Funding Check
                        try:
                            start_ts_ms = int((next_funding_ts - 120)*1000); end_ts_ms = int((next_funding_ts + 120)*1000)
                            log_resp = session.get_transaction_log(category="linear",symbol=top_symbol,type="SETTLEMENT",startTime=start_ts_ms,endTime=end_ts_ms,limit=10)
                            log_list = log_resp.get("result",{}).get("list",[]); funding_val = Decimal("0")
                            if log_list:
                                for entry in log_list:
                                    if abs(int(entry.get("transactionTime","0"))/1000 - next_funding_ts) < 60: funding_val += Decimal(entry.get("change","0"))
                                if funding_val != Decimal("0"): position_data["actual_funding_fee"] = funding_val; await app.bot.send_message(chat_id, f"💰 Фандинг (лог): `{funding_val:.4f}` USDT", parse_mode='Markdown')
                                else: await app.bot.send_message(chat_id, f"⚠️ SETTLEMENT для {top_symbol} не найден.")
                            else: await app.bot.send_message(chat_id, f"⚠️ Лог транз. пуст для {top_symbol}.")
                        except Exception as e: print(f"Err funding log: {e}"); await app.bot.send_message(chat_id, f"❌ Ошибка лога фандинга: {e}")
                        close_side = "Buy" if open_side == "Sell" else "Sell"; close_qty_rem = final_opened_qty # Close Position
                        try: # Maker Close
                            ob_resp = session.get_orderbook(category="linear",symbol=top_symbol,limit=1); ob = ob_resp['result']
                            mp = quantize_price(Decimal(ob['b'][0][0] if close_side=="Buy" else ob['a'][0][0]), tick_size)
                            resp = session.place_order(category="linear",symbol=top_symbol,side=close_side,order_type="Limit",qty=str(close_qty_rem),price=str(mp),time_in_force="PostOnly",reduce_only=True)
                            oid = resp["result"]["orderId"]; await app.bot.send_message(chat_id, f"⏳ Maker вых. @{mp} (ID: ...{oid[-6:]})"); await asyncio.sleep(MAKER_ORDER_WAIT_SECONDS_EXIT)
                            hist_resp = session.get_order_history(category="linear",orderId=oid,limit=1); hist_list = hist_resp.get("result",{}).get("list",[])
                            if hist_list:
                                h=hist_list[0]; exec_q=Decimal(h.get("cumExecQty","0"))
                                if exec_q > 0: position_data["closed_qty"]+=exec_q; position_data["total_close_value"]+=Decimal(h.get("cumExecValue","0")); position_data["total_close_fee"]+=Decimal(h.get("cumExecFee","0")); close_qty_rem-=exec_q; await app.bot.send_message(chat_id, f"✅ Maker вых. исполн: {exec_q}")
                                if h.get("orderStatus") not in ["Filled","Cancelled","Rejected","Deactivated"]: 
                                    try: session.cancel_order(category="linear",symbol=top_symbol,orderId=oid)
                                    except Exception as cancel_e: print(f"Minor cancel (Maker Close): {cancel_e}")
                        except Exception as e: print(f"Maker Close exc: {e}"); await app.bot.send_message(chat_id, f"⚠️ Maker вых. ошибка: {e}")
                        close_qty_rem = quantize_qty(close_qty_rem, qty_step) # Market Close
                        if close_qty_rem >= min_qty:
                            await app.bot.send_message(chat_id, f"🛒 Market вых. остаток: {close_qty_rem}")
                            try:
                                resp = session.place_order(category="linear",symbol=top_symbol,side=close_side,order_type="Market",qty=str(close_qty_rem),time_in_force="ImmediateOrCancel",reduce_only=True)
                                oid = resp["result"]["orderId"]; await asyncio.sleep(1.5)
                                hist_resp = session.get_order_history(category="linear",orderId=oid,limit=1); hist_list = hist_resp.get("result",{}).get("list",[])
                                if hist_list:
                                    h=hist_list[0]; exec_q=Decimal(h.get("cumExecQty","0"))
                                    if exec_q > 0: position_data["closed_qty"]+=exec_q; position_data["total_close_value"]+=Decimal(h.get("cumExecValue","0")); position_data["total_close_fee"]+=Decimal(h.get("cumExecFee","0")); await app.bot.send_message(chat_id, f"✅ Market вых. исполн: {exec_q}")
                            except Exception as e: print(f"Market Close exc: {e}"); await app.bot.send_message(chat_id, f"❌ Market вых. ошибка: {e}")
                        final_closed_qty = position_data["closed_qty"] # PNL Calculation
                        if abs(final_closed_qty - final_opened_qty) > min_qty*Decimal("0.1"): await app.bot.send_message(chat_id, f"⚠️ Позиция *{top_symbol}* не полностью закрыта! Откр: `{final_opened_qty}`, Закр: `{final_closed_qty}`. ПРОВЕРЬТЕ!", parse_mode='Markdown')
                        else: await app.bot.send_message(chat_id, f"✅ Позиция *{top_symbol}* успешно закрыта ({final_closed_qty}).", parse_mode='Markdown')
                        price_pnl = Decimal("0")
                        if final_opened_qty > 0 and final_closed_qty > 0 and avg_open_price > 0:
                            avg_close_price = position_data["total_close_value"] / final_closed_qty if final_closed_qty > 0 else Decimal("0")
                            if avg_close_price > 0: price_pnl = (avg_close_price - avg_open_price if open_side == "Buy" else avg_open_price - avg_close_price) * final_closed_qty
                        funding_pnl = position_data["actual_funding_fee"]; total_fees = position_data["total_open_fee"] + position_data["total_close_fee"]
                        net_pnl = price_pnl + funding_pnl - total_fees; roi_pct = (net_pnl / marja) * 100 if marja != Decimal(0) else Decimal("0")
                        await app.bot.send_message(chat_id, f"📊 Результат: *{top_symbol}* ({'L' if open_side=='Buy' else 'S'})\n PNL (цена): `{price_pnl:+.4f}`\n PNL (фандинг): `{funding_pnl:+.4f}`\n Комиссии: `{-total_fees:.4f}`\n💰 *Чистая прибыль: {net_pnl:+.4f} USDT*\n📈 ROI ({marja} USDT): `{roi_pct:.2f}%`", parse_mode='Markdown')
                    except Exception as trade_e: print(f"CRITICAL TRADE ERROR for {chat_id}, {top_symbol}: {trade_e}"); import traceback; traceback.print_exc(); await app.bot.send_message(chat_id, f"❌ КРИТ. ОШИБКА сделки *{top_symbol}*:\n`{trade_e}`\n❗️ *ПРОВЕРЬТЕ СЧЕТ!*", parse_mode='Markdown')
                    finally: print(f">>> Finished processing {top_symbol} for chat {chat_id} <<<")
            # else:
            #     # print(f"Not in entry window for {top_symbol} ({seconds_left:.0f}s left).")
            #     pass
        except Exception as loop_e:
            print(f"UNHANDLED ERROR IN SNIPER LOOP: {loop_e}"); import traceback; traceback.print_exc()
            await asyncio.sleep(30)
    # === Конец блока сделки ===


# ===================== MAIN =====================
if __name__ == "__main__":
    print("Initializing bot...")
    app_builder = ApplicationBuilder().token(BOT_TOKEN)
    app = app_builder.build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(MessageHandler(filters.Regex("^📊 Топ-пары$"), show_top_funding))
    app.add_handler(MessageHandler(filters.Regex("^📡 Сигналы$"), signal_menu))
    app.add_handler(CallbackQueryHandler(signal_callback, pattern="^(toggle_sniper|show_top_pairs_inline)$"))
    conv_marja = ConversationHandler(entry_points=[MessageHandler(filters.Regex("^💰 Маржа$"), set_real_marja)], states={SET_MARJA: [MessageHandler(filters.TEXT&~filters.COMMAND, save_real_marja)]}, fallbacks=[CommandHandler("cancel", cancel)], conversation_timeout=60.0)
    app.add_handler(conv_marja)
    conv_plecho = ConversationHandler(entry_points=[MessageHandler(filters.Regex("^⚖️ Плечо$"), set_real_plecho)], states={SET_PLECHO: [MessageHandler(filters.TEXT&~filters.COMMAND, save_real_plecho)]}, fallbacks=[CommandHandler("cancel", cancel)], conversation_timeout=60.0)
    app.add_handler(conv_plecho)
    async def post_init_tasks(passed_app: ApplicationBuilder):
        print("Running post_init tasks..."); asyncio.create_task(funding_sniper_loop(passed_app)); print("Sniper loop task created.")
    app.post_init = post_init_tasks
    print("Starting bot polling...")
    try: app.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception as e: print(f"\nBot polling stopped due to error: {e}")
    finally: print("\nBot shutdown.")

# --- END OF FILE bot (9)_debug_balance_check_commented.py ---
