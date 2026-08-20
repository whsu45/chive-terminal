import re
from datetime import datetime
from bs4 import BeautifulSoup
from .utils import clean_int, is_etf, extract_stock_code
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
    sorted_foreign = sorted(foreign_stocks_summary.values(), key=lambda x: (x["count"], x["total_net_buy"]),
                            reverse=True)

    return sorted_stocks[:10], sorted_etfs[:10], sorted_domestic[:10], sorted_foreign[:10]