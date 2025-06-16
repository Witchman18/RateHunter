import requests
from decimal import Decimal
import time

def get_funding_rates_mexc():
    symbols = ["BTC_USDT", "ETH_USDT", "SOL_USDT"]  # можно расширить
    funding_data = []

    for symbol in symbols:
        try:
            url = f"https://contract.mexc.com/api/v1/private/funding/prev_funding_rate?symbol={symbol}"
            response = requests.get(url)
            data = response.json()

            funding_data.append({
                "symbol": symbol.replace("_", ""),  # как BTCUSDT
                "fundingRate": Decimal(data['data']['fundingRate']),
                "nextFundingTime": int(time.time()) + 8 * 3600  # раз в 8 ч
            })

        except Exception as e:
            print(f"Ошибка при получении {symbol}: {e}")

    return funding_data
