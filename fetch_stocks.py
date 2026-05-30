# fetch_stocks.py
import yfinance as yf
import pandas as pd
import json
import os
import requests
from datetime import datetime

STOCKS_POOL_CFG = [
    {"symbol": "2330.TW", "code": "2330", "name": "TSMC (台積電)", "category": "AI"},
    {"symbol": "2317.TW", "code": "2317", "name": "Foxconn (鴻海)", "category": "AI"},
    {"symbol": "2454.TW", "code": "2454", "name": "MediaTek (聯發科)", "category": "AI"},
    {"symbol": "2382.TW", "code": "2382", "name": "Quanta (廣達)", "category": "AI"},
    {"symbol": "3231.TW", "code": "3231", "name": "Wistron (緯創)", "category": "AI"},
    {"symbol": "2356.TW", "code": "2356", "name": "Inventec (英業達)", "category": "AI"},
    {"symbol": "2357.TW", "code": "2357", "name": "ASUS (華碩)", "category": "AI"},
    {"symbol": "2376.TW", "code": "2376", "name": "Gigabyte (技嘉)", "category": "AI"},
    {"symbol": "6669.TW", "code": "6669", "name": "Wiwynn (緯穎)", "category": "AI"},
    {"symbol": "2301.TW", "code": "2301", "name": "Lite-On (光寶科)", "category": "AI"},
    {"symbol": "2345.TW", "code": "2345", "name": "Accton (智邦)", "category": "AI"},
    {"symbol": "3017.TW", "code": "3017", "name": "AMax (奇鋐)", "category": "AI"},
    {"symbol": "3037.TW", "code": "3037", "name": "Unimicron (欣興)", "category": "AI"},
    {"symbol": "2603.TW", "code": "2603", "name": "Evergreen (長榮)", "category": "Non-AI"},
    {"symbol": "2609.TW", "code": "2609", "name": "Yang Ming (陽明)", "category": "Non-AI"},
    {"symbol": "2881.TW", "code": "2881", "name": "Fubon Financial (富邦金)", "category": "Non-AI"},
    {"symbol": "2882.TW", "code": "2882", "name": "Cathay Financial (國泰金)", "category": "Non-AI"},
    {"symbol": "3481.TW", "code": "3481", "name": "Innolux (群創)", "category": "Non-AI"},
    {"symbol": "2409.TW", "code": "2409", "name": "AUO (友達)", "category": "Non-AI"}
]


def load_existing_cache():
    if os.path.exists("stock_data.json"):
        try:
            with open("stock_data.json", "r", encoding="utf-8") as f:
                data = json.load(f)
                return {item["symbol"]: item for item in data}
        except Exception as e:
            print(f"Failed to read existing cache: {e}")
    return {}


def get_field(item, keys_list):
    for key in keys_list:
        if key in item:
            return item[key]
    return None


def clean_float(val):
    if val is None:
        return 0.0
    try:
        return float(str(val).replace(",", "").strip())
    except ValueError:
        return 0.0


def fetch_twse_openapi():
    # Fallback endpoint that delivers the latest updates for all stocks in one response
    url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
    try:
        response = requests.get(url, timeout=15)
        if response.status_code == 200:
            return {item.get("Code", item.get("證券代號")): item for item in response.json()}
    except Exception as e:
        print(f"TWSE OpenAPI fetch failed: {e}")
    return None


def generate_line_history(price, symbol, now_ts):
    history = []
    step_price = price
    for i in range(50, 0, -1):
        seed = hash(symbol + str(i)) % 100 - 50
        step_price = step_price * (1 + (seed / 100000.0))
        history.append({
            "time": now_ts - (i * 10),
            "value": round(step_price, 2)
        })
    return history


def run():
    existing_cache = load_existing_cache()
    output_data = []
    now_ts = int(datetime.now().timestamp())
    today_str = datetime.now().strftime("%Y-%m-%d")

    # Try Route A: yfinance (Primary)
    symbols = [s["symbol"] for s in STOCKS_POOL_CFG]
    yf_success = False
    try:
        print("Attempting to fetch with yfinance...")
        data = yf.download(symbols, period="1mo", interval="1d", group_by="ticker")
        if not data.empty:
            yf_success = True
    except Exception as e:
        print(f"yfinance failed: {e}")

    # Try Route B: TWSE OpenAPI (Fallback)
    twse_data = None
    if not yf_success:
        print("Falling back to official TWSE OpenAPI...")
        twse_data = fetch_twse_openapi()

    for cfg in STOCKS_POOL_CFG:
        sym = cfg["symbol"]
        code = cfg["code"]
        cached_stock = existing_cache.get(sym, {})

        # Scenario 1: Process via yfinance
        if yf_success:
            try:
                ticker_data = data[sym].dropna() if len(symbols) > 1 else data.dropna()
                if not ticker_data.empty:
                    latest_row = ticker_data.iloc[-1]
                    prev_row = ticker_data.iloc[-2] if len(ticker_data) > 1 else latest_row

                    current_price = float(latest_row["Close"])
                    base_price = float(prev_row["Close"])
                    change = current_price - base_price
                    change_percent = (change / base_price) * 100 if base_price != 0 else 0.0
                    volume = int(latest_row["Volume"])

                    candles = []
                    for date, row in ticker_data.iterrows():
                        candles.append({
                            "time": date.strftime("%Y-%m-%d"),
                            "open": float(row["Open"]),
                            "high": float(row["High"]),
                            "low": float(row["Low"]),
                            "close": float(row["Close"])
                        })

                    history = generate_line_history(current_price, sym, now_ts)

                    output_data.append({
                        "symbol": sym,
                        "name": cfg["name"],
                        "category": cfg["category"],
                        "basePrice": round(base_price, 2),
                        "currentPrice": round(current_price, 2),
                        "change": round(change, 2),
                        "changePercent": round(change_percent, 2),
                        "volume": volume,
                        "history": history,
                        "candles": candles,
                        "source": "YahooFinance"
                    })
                    continue
            except Exception as e:
                print(f"Failed parsing yfinance for {sym}, switching to cached fallback...: {e}")

        # Scenario 2: Parse via TWSE OpenAPI fallback
        if twse_data and code in twse_data:
            try:
                item = twse_data[code]

                close_val = clean_float(get_field(item, ["ClosingPrice", "收盤價"]))
                open_val = clean_float(get_field(item, ["OpeningPrice", "開盤價"]))
                high_val = clean_float(get_field(item, ["HighestPrice", "最高價"]))
                low_val = clean_float(get_field(item, ["LowestPrice", "最低價"]))
                volume = int(clean_float(get_field(item, ["TradeVolume", "成交股數"])))
                change = clean_float(get_field(item, ["Change", "漲跌價差"]))

                # Check previous close to maintain percent change calculations
                base_price = close_val - change if change != 0 else close_val
                change_percent = (change / base_price) * 100 if base_price != 0 else 0.0

                # Reconstruct candles list from existing cache
                candles = cached_stock.get("candles", [])
                if not candles:
                    # If no cache exists, seed a single day
                    candles = [
                        {"time": today_str, "open": open_val, "high": high_val, "low": low_val, "close": close_val}]
                else:
                    if candles[-1]["time"] == today_str:
                        candles[-1] = {"time": today_str, "open": open_val, "high": high_val, "low": low_val,
                                       "close": close_val}
                    else:
                        candles.append(
                            {"time": today_str, "open": open_val, "high": high_val, "low": low_val, "close": close_val})
                        if len(candles) > 30:
                            candles.pop(0)

                history = generate_line_history(close_val, sym, now_ts)

                output_data.append({
                    "symbol": sym,
                    "name": cfg["name"],
                    "category": cfg["category"],
                    "basePrice": round(base_price, 2),
                    "currentPrice": round(close_val, 2),
                    "change": round(change, 2),
                    "changePercent": round(change_percent, 2),
                    "volume": volume,
                    "history": history,
                    "candles": candles,
                    "source": "TWSE-OpenAPI"
                })
                continue
            except Exception as e:
                print(f"Failed parsing TWSE for {sym}: {e}")

        # Scenario 3: Retain existing cache values if both primary and fallback calls fail
        if cached_stock:
            print(f"Restoring {sym} from local cache backup")
            cached_stock["source"] = "Cached-Backup"
            output_data.append(cached_stock)
        else:
            # Seed default values as a final safety measure
            base = 100.0
            history = generate_line_history(base, sym, now_ts)
            candles = [{"time": today_str, "open": base, "high": base, "low": base, "close": base}]
            output_data.append({
                "symbol": sym,
                "name": cfg["name"],
                "category": cfg["category"],
                "basePrice": base,
                "currentPrice": base,
                "change": 0.0,
                "changePercent": 0.0,
                "volume": 1000,
                "history": history,
                "candles": candles,
                "source": "Seeded-Fallback"
            })

    with open("stock_data.json", "w", encoding="utf-8") as f:
        json.dump(output_data, f, indent=4, ensure_ascii=False)
    print(f"Generation complete. Saved {len(output_data)} entries.")


if __name__ == "__main__":
    run()