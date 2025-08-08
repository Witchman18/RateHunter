# data_collector.py
import requests
import pandas as pd
from datetime import datetime, timedelta

# --- НАСТРОЙКИ ---
SYMBOL = "MYX_USDT"  # Монета, которую анализируем. Можешь поменять на любую другую.
DAYS_AGO = 3         # 1 = вчера, 2 = позавчера, и т.д.
# -----------------

def fetch_funding_history(symbol, start_time, end_time):
    """Получает историю ставок финансирования с MEXC."""
    print(f"Запрашиваю историю фандинга для {symbol}...")
    url = f"https://contract.mexc.com/api/v1/contract/funding_rate/history"
    params = {
        'symbol': symbol,
        'page_size': 100, # Обычно за день не больше 24 выплат
        'start_time': start_time,
        'end_time': end_time
    }
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        if data.get("success"):
            print(f"✅ Успешно получено {len(data.get('data', []))} записей о фандинге.")
            return data.get('data', [])
        else:
            print(f"❌ Ошибка API MEXC (фандинг): {data.get('message')}")
            return []
    except requests.exceptions.RequestException as e:
        print(f"❌ Сетевая ошибка (фандинг): {e}")
        return []

def fetch_klines(symbol, start_time, end_time):
    """Получает 1-минутные свечи с MEXC."""
    print(f"Запрашиваю 1-минутные свечи для {symbol} (это может занять некоторое время)...")
    url = f"https://contract.mexc.com/api/v1/contract/kline/{symbol}"
    all_klines = []
    current_time = start_time
    
    # API MEXC отдает до 1000 свечей за раз. 
    # За сутки 1440 минутных свечей, поэтому нужно сделать 2 запроса.
    while current_time < end_time:
        params = {
            'symbol': symbol,
            'interval': 'Min1',
            'start': int(current_time / 1000),
            'end': int(end_time / 1000)
        }
        try:
            response = requests.get(url, params=params, timeout=20)
            response.raise_for_status()
            data = response.json()
            if data.get("success"):
                klines = data.get('data', {})
                if klines.get('time'):
                    # Собираем данные в удобный формат
                    for i in range(len(klines['time'])):
                        all_klines.append([
                            klines['time'][i] * 1000, # Приводим к миллисекундам
                            klines['open'][i],
                            klines['high'][i],
                            klines['low'][i],
                            klines['close'][i],
                            klines['vol'][i]
                        ])
                    # Сдвигаем время для следующего запроса
                    last_time = klines['time'][-1] * 1000
                    current_time = last_time + 60000 # +1 минута
                else:
                    break # Данных больше нет
            else:
                print(f"❌ Ошибка API MEXC (свечи): {data.get('message')}")
                break
        except requests.exceptions.RequestException as e:
            print(f"❌ Сетевая ошибка (свечи): {e}")
            break
            
    print(f"✅ Успешно получено {len(all_klines)} минутных свечей.")
    return all_klines

if __name__ == "__main__":
    # Вычисляем временной диапазон для вчерашнего дня
    today = datetime.utcnow().date()
    end_of_yesterday = datetime.combine(today, datetime.min.time())
    start_of_yesterday = end_of_yesterday - timedelta(days=DAYS_AGO)
    
    # Переводим в миллисекунды (timestamp) для API
    start_ts_ms = int(start_of_yesterday.timestamp() * 1000)
    end_ts_ms = int(end_of_yesterday.timestamp() * 1000) - 1 # до 23:59:59.999
    
    print(f"--- Сбор данных для {SYMBOL} за {start_of_yesterday.strftime('%Y-%m-%d')} ---")
    
    # 1. Получаем и сохраняем историю фандинга
    funding_data = fetch_funding_history(SYMBOL, start_ts_ms, end_ts_ms)
    if funding_data:
        df_funding = pd.DataFrame(funding_data)
        df_funding.to_json("funding_history.json", orient="records", indent=4)
        print("💾 История фандинга сохранена в `funding_history.json`")

    # 2. Получаем и сохраняем историю свечей
    kline_data = fetch_klines(SYMBOL, start_ts_ms, end_ts_ms)
    if kline_data:
        df_klines = pd.DataFrame(kline_data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df_klines.to_json("klines_1m.json", orient="records", indent=4)
        print("💾 1-минутные свечи сохранены в `klines_1m.json`")
        
    print("\n--- Готово! ---")
