from datetime import datetime, timezone
from .utils import DATA_SOURCES


def fetch_us_indices(session):
    symbols = {'DJI': '^DJI', 'IXIC': '^IXIC', 'SOX': '^SOX'}
    us_data_by_date = {}
    headers = {'User-Agent': 'Mozilla/5.0'}
    base_url = DATA_SOURCES["yahoo_finance"]["chart_api_url"]

    for key, symbol in symbols.items():
        url = f"{base_url.format(symbol=symbol)}?interval=1d&range=2mo"
        try:
            resp = session.get(url, headers=headers, timeout=5)
            if resp.status_code == 200:
                res_json = resp.json()
                result = res_json.get('chart', {}).get('result', [])[0]
                timestamps = result.get('timestamp', [])
                quote = result.get('indicators', {}).get('quote', [])[0]
                closes = quote.get('close', [])
                opens = quote.get('open', [])

                for idx in range(len(timestamps)):
                    if idx < len(closes) and closes[idx] is not None:
                        ts = timestamps[idx]
                        date_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                        date_str = date_dt.strftime("%Y/%m/%d")

                        c_price = closes[idx]
                        o_price = opens[idx] if idx < len(opens) and opens[idx] is not None else c_price
                        prev_c = closes[idx - 1] if idx > 0 and closes[idx - 1] is not None else o_price

                        pt_change = c_price - prev_c
                        pt_str = f"+{pt_change:.0f}" if pt_change > 0 else f"{pt_change:.0f}"

                        if date_str not in us_data_by_date:
                            us_data_by_date[date_str] = {}

                        us_data_by_date[date_str][key] = {
                            "pts": pt_str,
                            "raw_pts": pt_change
                        }
        except Exception as e:
            print(f"Fetch US index {symbol} error: {e}")

    return us_data_by_date


def get_us_info_for_date(us_data_by_date, prev_date_str):
    if prev_date_str in us_data_by_date:
        return us_data_by_date[prev_date_str]
    avail = sorted([d for d in us_data_by_date.keys() if d <= prev_date_str], reverse=True)
    if avail:
        return us_data_by_date[avail[0]]
    return {}


def fetch_ohlc_3m(session, symbol):
    ohlc_list = []
    headers = {'User-Agent': 'Mozilla/5.0'}

    yf_symbol = "^TWII" if "TW" in symbol or "TAIEX" in symbol else "^IXIC"
    base_url = DATA_SOURCES["yahoo_finance"]["chart_api_url"]
    url = f"{base_url.format(symbol=yf_symbol)}?interval=1d&range=3mo"
    try:
        resp = session.get(url, headers=headers, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            result = data.get('chart', {}).get('result', [])[0]
            timestamps = result.get('timestamp', [])
            quote = result.get('indicators', {}).get('quote', [])[0]
            opens = quote.get('open', [])
            highs = quote.get('high', [])
            lows = quote.get('low', [])
            closes = quote.get('close', [])

            for i in range(len(timestamps)):
                if (i < len(opens) and opens[i] is not None and
                        i < len(highs) and highs[i] is not None and
                        i < len(lows) and lows[i] is not None and
                        i < len(closes) and closes[i] is not None):
                    ts = timestamps[i]
                    date_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")

                    ohlc_list.append({
                        "time": date_str,
                        "open": round(opens[i], 1),
                        "high": round(highs[i], 1),
                        "low": round(lows[i], 1),
                        "close": round(closes[i], 1)
                    })
            if len(ohlc_list) > 10:
                return ohlc_list
    except Exception as e:
        print(f"Yahoo OHLC fetch error for {yf_symbol}: {e}")

    stooq_symbol = "^TWII" if "TW" in symbol or "TAIEX" in symbol else "^IXIC"
    stooq_base_url = DATA_SOURCES["stooq"]["daily_csv_url"]
    stooq_url = f"{stooq_base_url}?s={stooq_symbol}&i=d"
    try:
        resp = session.get(stooq_url, headers=headers, timeout=5)
        if resp.status_code == 200 and "Date,Open" in resp.text:
            lines = resp.text.strip().split('\n')[1:]
            for line in lines[-65:]:
                parts = line.split(',')
                if len(parts) >= 5:
                    date_str, o, h, l, c = parts[0], parts[1], parts[2], parts[3], parts[4]
                    if all(x.replace('.', '').isdigit() for x in [o, h, l, c]):
                        ohlc_list.append({
                            "time": date_str,
                            "open": round(float(o), 1),
                            "high": round(float(h), 1),
                            "low": round(float(l), 1),
                            "close": round(float(c), 1)
                        })
            if len(ohlc_list) > 10:
                return ohlc_list
    except Exception as e:
        print(f"Stooq OHLC fetch error for {stooq_symbol}: {e}")

    return ohlc_list


def get_stock_price(session, code, price_cache):
    if not code or code in price_cache:
        return price_cache.get(code)

    headers = {'User-Agent': 'Mozilla/5.0'}
    base_url = DATA_SOURCES["yahoo_finance"]["chart_api_url"]

    for suffix in ['.TW', '.TWO']:
        sym = f"{code}{suffix}"
        url = f"{base_url.format(symbol=sym)}?interval=1d&range=1d"
        try:
            resp = session.get(url, headers=headers, timeout=3)
            if resp.status_code == 200:
                data = resp.json()
                res = data.get('chart', {}).get('result', [])
                if res:
                    meta = res[0].get('meta', {})
                    price = meta.get('regularMarketPrice')
                    if price is not None:
                        price_cache[code] = round(price, 2)
                        return price_cache[code]
        except Exception:
            pass

    price_cache[code] = None
    return None