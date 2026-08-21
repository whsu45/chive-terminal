import os
import re
import json
from datetime import datetime
from bs4 import BeautifulSoup
from .utils import clean_int, is_etf, extract_stock_code, BROKER_JSON_FILE, DATA_DIR
from .market_scraper import get_stock_price

BROKER_TARGETS = [
    {"name": "摩根大通", "a": "8440", "b": "8440", "c": "E", "group": "foreign"},
    {"name": "凱基-台北", "a": "9200", "b": "9268", "c": "E", "group": "domestic"},
    {"name": "元大-土城永寧", "a": "9800", "b": "9875", "c": "E", "group": "domestic"},
    {"name": "富邦-建國", "a": "9600", "b": "9658", "c": "E", "group": "domestic"},
    {"name": "美商高盛", "a": "1480", "b": "1480", "c": "E", "group": "foreign"},
    {"name": "新加坡商瑞銀", "a": "1650", "b": "1650", "c": "E", "group": "foreign"},
    {"name": "美林", "a": "1440", "b": "1440", "c": "E", "group": "foreign"}
]

DOMESTIC_NAMES = {"凱基-台北", "元大-土城永寧", "富邦-建國"}
FOREIGN_NAMES = {"摩根大通", "美商高盛", "新加坡商瑞銀", "美林"}


def fetch_single_broker_buy(session, broker_info, date_str):
    dt = datetime.strptime(date_str, "%Y/%m/%d")
    formatted_date = f"{dt.year}-{dt.month}-{dt.day}"

    url = f"https://fubon-ebrokerdj.fbs.com.tw/z/zg/zgb/zgb0.djhtm?a={broker_info['a']}&b={broker_info['b']}&c=E&e={formatted_date}&f={formatted_date}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Referer': 'https://fubon-ebrokerdj.fbs.com.tw/'
    }

    buy_map = {}
    try:
        resp = session.get(url, headers=headers, timeout=6)
        html_content = resp.content.decode('cp950', errors='ignore')
        soup = BeautifulSoup(html_content, 'html.parser')

        rows = soup.find_all('tr')
        for row in rows:
            row_html = str(row)
            match = re.search(r"GenLink2stk\('AS([0-9A-Z]{4,6})','([^']+)'\)", row_html)
            if match:
                stk_code = match.group(1)
                stk_name = match.group(2)
                full_stk = f"{stk_code}{stk_name}"

                cols = row.find_all('td')
                if len(cols) >= 4:
                    net_text = cols[3].text
                    net_val = clean_int(net_text)
                    if net_val is not None and net_val > 0:
                        buy_map[full_stk] = net_val
    except Exception as e:
        print(f"[{date_str}] Broker {broker_info['name']} fetch error: {e}")

    return buy_map


def aggregate_broker_buys_for_date(session, target_date_str, price_cache):
    """
    分別聚合 5 種分類（回傳 5 個清單）：
    1. 全 7 大券商個股 Top 10
    2. 全 7 大券商 ETF Top 10
    3. 隔日沖內資分點 Top 10 個股 (依券商數優先)
    4. 內資三大分點總買超張數 Top 10 個股 (純依買超張數高低)
    5. 隔日沖外資分點 Top 10 個股 (依券商數優先)
    """
    stocks_summary = {}
    etf_summary = {}
    domestic_stocks_summary = {}
    foreign_stocks_summary = {}

    for broker in BROKER_TARGETS:
        buy_map = fetch_single_broker_buy(session, broker, target_date_str)
        b_name = broker["name"]
        is_domestic = b_name in DOMESTIC_NAMES
        is_foreign = b_name in FOREIGN_NAMES

        for stk, net_buy in buy_map.items():
            code = extract_stock_code(stk)
            price = get_stock_price(session, code, price_cache)
            stk_is_etf = is_etf(stk)

            target_dict = etf_summary if stk_is_etf else stocks_summary
            if stk not in target_dict:
                target_dict[stk] = {"stock": stk, "code": code, "price": price, "count": 0, "total_net_buy": 0,
                                    "brokers": []}
            target_dict[stk]["count"] += 1
            target_dict[stk]["total_net_buy"] += net_buy
            target_dict[stk]["brokers"].append(b_name)

            if not stk_is_etf:
                if is_domestic:
                    if stk not in domestic_stocks_summary:
                        domestic_stocks_summary[stk] = {"stock": stk, "code": code, "price": price, "count": 0,
                                                        "total_net_buy": 0, "brokers": []}
                    domestic_stocks_summary[stk]["count"] += 1
                    domestic_stocks_summary[stk]["total_net_buy"] += net_buy
                    domestic_stocks_summary[stk]["brokers"].append(b_name)

                if is_foreign:
                    if stk not in foreign_stocks_summary:
                        foreign_stocks_summary[stk] = {"stock": stk, "code": code, "price": price, "count": 0,
                                                       "total_net_buy": 0, "brokers": []}
                    foreign_stocks_summary[stk]["count"] += 1
                    foreign_stocks_summary[stk]["total_net_buy"] += net_buy
                    foreign_stocks_summary[stk]["brokers"].append(b_name)

    sorted_stocks = sorted(stocks_summary.values(), key=lambda x: (x["count"], x["total_net_buy"]), reverse=True)
    sorted_etfs = sorted(etf_summary.values(), key=lambda x: (x["count"], x["total_net_buy"]), reverse=True)
    sorted_domestic = sorted(domestic_stocks_summary.values(), key=lambda x: (x["count"], x["total_net_buy"]),
                             reverse=True)
    sorted_domestic_volume = sorted(domestic_stocks_summary.values(), key=lambda x: x["total_net_buy"], reverse=True)
    sorted_foreign = sorted(foreign_stocks_summary.values(), key=lambda x: (x["count"], x["total_net_buy"]),
                            reverse=True)

    return sorted_stocks[:10], sorted_etfs[:10], sorted_domestic[:10], sorted_domestic_volume[:10], sorted_foreign[:10]


def update_broker_history_json(session, trading_days):
    os.makedirs(DATA_DIR, exist_ok=True)
    existing_records = {}
    price_cache = {}

    if os.path.exists(BROKER_JSON_FILE):
        try:
            with open(BROKER_JSON_FILE, "r", encoding="utf-8") as f:
                records_list = json.load(f)
                existing_records = {item["date"]: item for item in records_list}
            print(f"已成功載入 {len(existing_records)} 筆歷史券商 JSON 紀錄。")
        except Exception as e:
            print(f"載入 {BROKER_JSON_FILE} 失敗: {e}")

    for target_date_str, _ in trading_days:
        rec = existing_records.get(target_date_str)

        needs_update = (
                rec is None or
                not rec.get("top_stocks") or
                not rec.get("top_etfs") or
                "top_domestic_stocks" not in rec or
                "top_domestic_volume_stocks" not in rec or
                "top_foreign_stocks" not in rec or
                rec.get("version") != "v8"
        )

        if needs_update:
            print(f"抓取 7 大主力券商（全分流解析）：{target_date_str}...")
            # 解包修正為 5 個變數
            top_stocks, top_etfs, top_dom, top_dom_vol, top_for = aggregate_broker_buys_for_date(session,
                                                                                                 target_date_str,
                                                                                                 price_cache)
            existing_records[target_date_str] = {
                "date": target_date_str,
                "top_stocks": top_stocks,
                "top_etfs": top_etfs,
                "top_domestic_stocks": top_dom,
                "top_domestic_volume_stocks": top_dom_vol,
                "top_foreign_stocks": top_for,
                "version": "v8"
            }

    sorted_history = sorted(existing_records.values(), key=lambda x: x["date"], reverse=True)

    with open(BROKER_JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted_history, f, ensure_ascii=False, indent=2)

    print(f"成功更新券商買超歷史紀錄至：{BROKER_JSON_FILE}")
    return sorted_history