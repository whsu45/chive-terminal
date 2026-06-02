# fetch_data.py
import yfinance as yf
import json
from datetime import datetime

# Consolidated pools of target tickers
tw_tickers = [
    "2330.TW", "2454.TW", "2317.TW", "2603.TW", "2308.TW", "2382.TW",
    "2881.TW", "2882.TW", "3008.TW", "3711.TW", "2609.TW", "2303.TW",
    "3481.TW", "2409.TW", "2891.TW", "2892.TW", "2002.TW", "2352.TW",
    "2324.TW", "2357.TW", "2890.TW", "2886.TW", "1303.TW", "1301.TW"
]

us_ai_tickers = [
    "NVDA", "AMD", "MSFT", "GOOGL", "META", "AVGO", "TSM", "SMCI", "PLTR", "AMZN"
]


def fetch_ticker_data(symbol):
    try:
        t = yf.Ticker(symbol)
        # Attempt to gather standard market stats
        info = t.info
        current_price = info.get("regularMarketPrice") or info.get("currentPrice")
        prev_close = info.get("regularMarketPreviousClose")

        # Fallback to historical extraction if metadata endpoints are restricted
        if not current_price or not prev_close:
            hist = t.history(period="2d")
            if len(hist) >= 2:
                current_price = hist['Close'].iloc[-1]
                prev_close = hist['Close'].iloc[-2]
            elif len(hist) == 1:
                current_price = hist['Close'].iloc[-1]
                prev_close = current_price
            else:
                return None

        change = current_price - prev_close
        pct_change = (change / prev_close) * 100 if prev_close else 0
        volume = info.get("regularMarketVolume") or info.get("volume") or 0
        name = info.get("shortName") or symbol.split(".")[0]

        # Retrieve actual 1d intraday ticks at 15m intervals
        ticks_df = t.history(period="1d", interval="15m")
        history_ticks = []
        for index, row in ticks_df.iterrows():
            history_ticks.append({
                "time": index.strftime("%H:%M"),
                "price": round(float(row["Close"]), 2)
            })

        if not history_ticks:
            history_ticks = [{"time": "Close", "price": round(current_price, 2)}]

        clean_symbol = symbol.split(".")[0]
        return {
            "symbol": clean_symbol,
            "name": name,
            "price": round(current_price, 2),
            "change": round(change, 2),
            "changePercent": round(pct_change, 2),
            "volume": int(volume),
            "history": history_ticks
        }
    except Exception as e:
        print(f"Bypassed {symbol}: {e}")
        return None


def main():
    print("Initiating global Yahoo Finance data synchronization pipeline...")

    # Fetch US AI Infrastructure
    us_ai_data = []
    for sym in us_ai_tickers:
        data = fetch_ticker_data(sym)
        if data:
            us_ai_data.append(data)

    # Fetch NASDAQ Composite Index
    nasdaq_data = fetch_ticker_data("^IXIC")
    if nasdaq_data:
        nasdaq_data["symbol"] = "NASDAQ"

    # Fetch Taiwan Pool
    tw_data_pool = []
    for sym in tw_tickers:
        data = fetch_ticker_data(sym)
        if data:
            tw_data_pool.append(data)

    # Sort leaderboards dynamically
    gainers = sorted(tw_data_pool, key=lambda x: x["changePercent"], reverse=True)[:10]
    losers = sorted(tw_data_pool, key=lambda x: x["changePercent"])[:10]

    # Map all parsed TWSE stocks for quick watchlist lookups
    all_tw = {item["symbol"]: item for item in tw_data_pool}

    payload = {
        "gainers": gainers,
        "losers": losers,
        "us_ai": us_ai_data,
        "nasdaq": nasdaq_data,
        "all_tw": all_tw,
        "updatedAt": datetime.utcnow().isoformat() + "Z"
    }

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print("Data compilation completed. data.json written successfully.")


if __name__ == "__main__":
    main()