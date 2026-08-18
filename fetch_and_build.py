import os
import re
import json
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta

DATA_DIR = "data"
JSON_FILE = os.path.join(DATA_DIR, "history.json")
BROKER_JSON_FILE = os.path.join(DATA_DIR, "broker_history.json")

# 指定監控的 7 大關鍵主力券商分點
BROKER_TARGETS = [
    {"name": "摩根大通", "a": "8440", "b": "8440", "c": "E"},
    {"name": "凱基-台北", "a": "9200", "b": "9268", "c": "E"},
    {"name": "元大-土城永寧", "a": "9800", "b": "9875", "c": "E"},
    {"name": "富邦-建國", "a": "9600", "b": "9658", "c": "E"},
    {"name": "美商高盛", "a": "1480", "b": "1480", "c": "E"},
    {"name": "新加坡商瑞銀", "a": "1650", "b": "1650", "c": "B"},
    {"name": "美林", "a": "1440", "b": "1440", "c": "E"}
]

def get_past_trading_days(count=20):
    """ 計算過去 N 個交易日的 (目標交易日, 前一交易日) """
    trading_days = []
    curr = datetime.now()
    
    if curr.weekday() == 5:   # Saturday
        curr = curr + timedelta(days=2)
    elif curr.weekday() == 6: # Sunday
        curr = curr + timedelta(days=1)

    while len(trading_days) < count:
        if curr.weekday() < 5:
            target_date_str = curr.strftime("%Y/%m/%d")
            prev = curr - timedelta(days=1)
            while prev.weekday() >= 5:
                prev -= timedelta(days=1)
            prev_date_str = prev.strftime("%Y/%m/%d")
            trading_days.append((target_date_str, prev_date_str))
        curr -= timedelta(days=1)
        
    return trading_days

def clean_int(text):
    if not text:
        return None
    text = text.strip().replace(',', '')
    is_negative = '-' in text or '▼' in text
    cleaned = re.sub(r'[^\d]', '', text)
    if cleaned:
        val = int(cleaned)
        return -val if is_negative else val
    return None

def clean_float(text):
    if not text:
        return None
    cleaned = text.strip().replace(',', '')
    try:
        return float(cleaned)
    except ValueError:
        return None

def is_etf(stock_text):
    """
    台股 ETF 判定規則：代號為 '00' 開頭即為 ETF
    例如: 0050, 0056, 00878, 00991A, 00981A
    """
    text = stock_text.replace('\xa0', '').strip()
    return text.startswith('00')

# ==============================================================================
# 期貨與籌碼爬蟲 (Futures & Institutional Scrapers)
# ==============================================================================

def fetch_night_market_data(session, target_date_str):
    url = "https://www.taifex.com.tw/cht/3/futDailyMarketReport"
    payload = {
        'queryType': '2', 'marketCode': '1', 'dateaddcnt': '',
        'commodity_id': 'TX', 'commodity_id2': '', 'queryDate': target_date_str,
        'MarketCode': '1', 'commodity_idt': 'TX', 'commodity_id2t': '', 'commodity_id2t2': ''
    }
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        resp = session.post(url, data=payload, headers=headers, timeout=5)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, 'html.parser')
            tables = soup.find_all('table', {'class': ['table_f', 'table_a']})
            for table in tables:
                for row in table.find_all('tr'):
                    cols = [td.text.strip() for td in row.find_all('td')]
                    if len(cols) >= 9 and cols[0] == 'TX':
                        return clean_int(cols[6]), clean_int(cols[8])
    except Exception as e:
        print(f"[{target_date_str}] Night market error: {e}")
    return None, None

def fetch_day_market_volume(session, prev_date_str):
    url = "https://www.taifex.com.tw/cht/3/futDailyMarketReport"
    payload = {
        'queryType': '2', 'marketCode': '0', 'dateaddcnt': '',
        'commodity_id': 'TX', 'commodity_id2': '', 'queryDate': prev_date_str,
        'MarketCode': '0', 'commodity_idt': 'TX', 'commodity_id2t': '', 'commodity_id2t2': ''
    }
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        resp = session.post(url, data=payload, headers=headers, timeout=5)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, 'html.parser')
            tables = soup.find_all('table', {'class': ['table_f', 'table_a']})
            for table in tables:
                day_vol_idx = 9
                headers_text = [th.text.strip() for th in table.find_all('th')]
                for idx, h_text in enumerate(headers_text):
                    if '一般交易時段' in h_text and '成交量' in h_text:
                        day_vol_idx = idx
                        break

                for row in table.find_all('tr'):
                    cols = [td.text.strip() for td in row.find_all('td')]
                    if len(cols) > day_vol_idx and cols[0] == 'TX':
                        volume = clean_int(cols[day_vol_idx])
                        if volume is not None:
                            return volume
    except Exception as e:
        print(f"[{prev_date_str}] Day volume error: {e}")
    return None

def fetch_institutional_positions_ah(session, target_date_str):
    url = "https://www.taifex.com.tw/cht/3/futContractsDateAh"
    payload = {
        'queryType': '1', 'goDay': '', 'doQuery': '1', 'dateaddcnt': '',
        'queryDate': target_date_str, 'commodityId': 'TXF', 'button': '送出查詢'
    }
    headers = {'User-Agent': 'Mozilla/5.0', 'Content-Type': 'application/x-www-form-urlencoded'}
    positions = {"foreign": None, "trust": None, "dealer": None}
    
    try:
        resp = session.post(url, data=payload, headers=headers, timeout=5)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, 'html.parser')
            tables = soup.find_all('table', {'class': ['table_f', 'table_a']})
            for table in tables:
                for row in table.find_all('tr'):
                    cols = [td.text.strip() for td in row.find_all('td')]
                    for idx, col in enumerate(cols):
                        if '自營商' in col and positions["dealer"] is None:
                            if len(cols) > idx + 5:
                                positions["dealer"] = clean_int(cols[idx + 5])
                        elif '投信' in col and positions["trust"] is None:
                            if len(cols) > idx + 5:
                                positions["trust"] = clean_int(cols[idx + 5])
                        elif '外資' in col and positions["foreign"] is None:
                            if len(cols) > idx + 5:
                                positions["foreign"] = clean_int(cols[idx + 5])
    except Exception as e:
        print(f"[{target_date_str}] Institutional AH position error: {e}")
        
    return positions

def fetch_institutional_positions_full(session, target_date_str):
    url = "https://www.taifex.com.tw/cht/3/futContractsDate"
    payload = {
        'queryType': '1', 'goDay': '', 'doQuery': '1', 'dateaddcnt': '',
        'queryDate': target_date_str, 'commodityId': 'TXF', 'button': '送出查詢'
    }
    headers = {'User-Agent': 'Mozilla/5.0', 'Content-Type': 'application/x-www-form-urlencoded'}
    positions = {"foreign": None, "trust": None, "dealer": None}
    
    try:
        resp = session.post(url, data=payload, headers=headers, timeout=5)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, 'html.parser')
            tables = soup.find_all('table', {'class': ['table_f', 'table_a']})
            for table in tables:
                for row in table.find_all('tr'):
                    cols = [td.text.strip() for td in row.find_all('td')]
                    for idx, col in enumerate(cols):
                        if '自營商' in col and positions["dealer"] is None:
                            if len(cols) > idx + 5:
                                positions["dealer"] = clean_int(cols[idx + 5])
                        elif '投信' in col and positions["trust"] is None:
                            if len(cols) > idx + 5:
                                positions["trust"] = clean_int(cols[idx + 5])
                        elif '外資' in col and positions["foreign"] is None:
                            if len(cols) > idx + 5:
                                positions["foreign"] = clean_int(cols[idx + 5])
    except Exception as e:
        print(f"[{target_date_str}] Institutional Full position error: {e}")
        
    return positions

def fetch_us_indices(session):
    symbols = {'DJI': '^DJI', 'IXIC': '^IXIC', 'SOX': '^SOX'}
    us_data_by_date = {}
    headers = {'User-Agent': 'Mozilla/5.0'}
    
    for key, symbol in symbols.items():
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1d&range=2mo"
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
                        prev_c = closes[idx-1] if idx > 0 and closes[idx-1] is not None else o_price
                        
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

def fetch_ohlc_3m(session, symbol):
    ohlc_list = []
    headers = {'User-Agent': 'Mozilla/5.0'}
    
    yf_symbol = "^TWII" if "TW" in symbol or "TAIEX" in symbol else "^IXIC"
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yf_symbol}?interval=1d&range=3mo"
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

    # 備援 Stooq API
    stooq_symbol = "^TWII" if "TW" in symbol or "TAIEX" in symbol else "^IXIC"
    stooq_url = f"https://stooq.com/q/d/l/?s={stooq_symbol}&i=d"
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

def generate_svg_kline_chart(ohlc_data, title, width=580, height=280):
    if not ohlc_data or len(ohlc_data) < 2:
        return f'''
        <div class="flex items-center justify-center h-[280px] bg-slate-50 text-slate-400 text-xs rounded-lg border border-slate-100">
            ⚠️ 數據載入中或休市無資料
        </div>
        '''

    highs = [d['high'] for d in ohlc_data]
    lows = [d['low'] for d in ohlc_data]
    min_price = min(lows)
    max_price = max(highs)
    price_range = max_price - min_price if max_price != min_price else 1.0

    padding_top, padding_bottom, padding_left, padding_right = 20, 30, 60, 15
    chart_w = width - padding_left - padding_right
    chart_h = height - padding_top - padding_bottom

    num_bars = len(ohlc_data)
    bar_spacing = chart_w / num_bars
    bar_width = max(2.5, bar_spacing * 0.65)

    def price_to_y(p):
        return padding_top + chart_h - ((p - min_price) / price_range) * chart_h

    svg_elements = []
    num_grid_lines = 4
    for i in range(num_grid_lines + 1):
        price_val = min_price + (price_range * i / num_grid_lines)
        y_pos = price_to_y(price_val)
        svg_elements.append(f'<line x1="{padding_left}" y1="{y_pos:.1f}" x2="{width - padding_right}" y2="{y_pos:.1f}" stroke="#f1f5f9" stroke-width="1" />')
        svg_elements.append(f'<text x="{padding_left - 8}" y="{y_pos + 3.5:.1f}" font-size="10" fill="#94a3b8" text-anchor="end">{price_val:,.0f}</text>')

    if num_bars >= 3:
        sample_indices = [0, num_bars // 2, num_bars - 1]
        for idx in sample_indices:
            x_pos = padding_left + (idx + 0.5) * bar_spacing
            date_label = ohlc_data[idx]['time']
            svg_elements.append(f'<text x="{x_pos:.1f}" y="{height - 8}" font-size="10" fill="#94a3b8" text-anchor="middle">{date_label}</text>')

    for idx, d in enumerate(ohlc_data):
        x_center = padding_left + (idx + 0.5) * bar_spacing
        x_left = x_center - (bar_width / 2)

        o_y = price_to_y(d['open'])
        c_y = price_to_y(d['close'])
        h_y = price_to_y(d['high'])
        l_y = price_to_y(d['low'])

        is_up = d['close'] >= d['open']
        color = "#ef4444" if is_up else "#22c55e"

        top_y = min(o_y, c_y)
        body_h = max(abs(c_y - o_y), 1.5)

        tooltip = f"{d['time']} &#10;開: {d['open']:,.1f} &#10;高: {d['high']:,.1f} &#10;低: {d['low']:,.1f} &#10;收: {d['close']:,.1f}"

        svg_elements.append(f'<line x1="{x_center:.1f}" y1="{h_y:.1f}" x2="{x_center:.1f}" y2="{l_y:.1f}" stroke="{color}" stroke-width="1.2" />')
        svg_elements.append(f'<rect x="{x_left:.1f}" y="{top_y:.1f}" width="{bar_width:.1f}" height="{body_h:.1f}" fill="{color}" rx="0.5"><title>{tooltip}</title></rect>')

    elements_str = "\n".join(svg_elements)
    return f'<svg viewBox="0 0 {width} {height}" class="w-full h-auto overflow-visible font-sans">{elements_str}</svg>'

def get_us_info_for_date(us_data_by_date, prev_date_str):
    if prev_date_str in us_data_by_date:
        return us_data_by_date[prev_date_str]
    avail = sorted([d for d in us_data_by_date.keys() if d <= prev_date_str], reverse=True)
    if avail:
        return us_data_by_date[avail[0]]
    return {}

def generate_svg_sparkline(prices):
    if not prices or len(prices) < 2:
        return '<span class="text-xs text-slate-400">無圖表</span>'
    open_price, close_price = prices[0], prices[-1]
    is_up = close_price >= open_price
    color = "#ef4444" if is_up else "#22c55e"
    
    min_p, max_p = min(prices), max(prices)
    price_range = (max_p - min_p) if max_p != min_p else 1
    width, height = 110, 28
    points = []
    for idx, p in enumerate(prices):
        x = (idx / (len(prices) - 1)) * width
        y = height - ((p - min_p) / price_range) * (height - 4) - 2
        points.append(f"{x:.1f},{y:.1f}")
        
    polyline_str = " ".join(points)
    return f'<svg width="{width}" height="{height}" class="inline-block overflow-visible" title="開: {open_price:.0f} | 收: {close_price:.0f}"><polyline fill="none" stroke="{color}" stroke-width="1.8" points="{polyline_str}" /></svg>'

def fetch_twse_intraday_taiex(session, target_date_str):
    formatted_date = target_date_str.replace('/', '')
    url = f"https://www.twse.com.tw/exchangeReport/MI_5MINS_INDEX?response=json&date={formatted_date}"
    headers = {'User-Agent': 'Mozilla/5.0'}
    
    try:
        resp = session.get(url, headers=headers, timeout=5)
        if resp.status_code == 200:
            res_json = resp.json()
            if res_json.get('stat') == 'OK' and 'data' in res_json:
                prices = []
                for row in res_json['data']:
                    val = clean_float(row[1])
                    if val is not None:
                        prices.append(val)
                
                if prices:
                    open_p, close_p = prices[0], prices[-1]
                    high_p, low_p = max(prices), min(prices)
                    sampled = prices[::60] if len(prices) > 60 else prices
                    sparkline_svg = generate_svg_sparkline(sampled)
                    actual_change = close_p - open_p
                    change_str = f"+{actual_change:.0f}點" if actual_change > 0 else f"{actual_change:.0f}點"
                    return {"open": open_p, "close": close_p, "high": high_p, "low": low_p, "change_str": change_str, "sparkline_svg": sparkline_svg, "prices": prices}
    except Exception as e:
        print(f"[{target_date_str}] TWSE TAIEX fetch error: {e}")
        
    return {"open": None, "close": None, "high": None, "low": None, "change_str": "NA", "sparkline_svg": '<span class="text-xs text-slate-400">未收盤</span>', "prices": []}

def verify_match(scenario, actual_info):
    open_p, close_p = actual_info.get("open"), actual_info.get("close")
    high_p, low_p = actual_info.get("high"), actual_info.get("low")
    prices = actual_info.get("prices", [])
    
    if not scenario or scenario == "NA" or None in [open_p, close_p, high_p, low_p] or len(prices) < 2:
        return "尚未驗證", "bg-slate-100 text-slate-500"
        
    total_pts = len(prices)
    idx_high, idx_low = prices.index(high_p), prices.index(low_p)
    high_time_ratio, low_time_ratio = idx_high / total_pts, idx_low / total_pts
    total_range = high_p - low_p if high_p != low_p else 1.0
    close_position = (close_p - low_p) / total_range
    change = close_p - open_p
    
    if scenario == "劇本一":
        return ("✅ 符合 (一路走高)", "bg-red-100 text-red-700 font-bold") if (change > 0 and close_position >= 0.4) else (("✅ 符合 (收紅上漲)", "bg-red-50 text-red-600") if change > 0 else ("❌ 走勢分歧", "bg-slate-100 text-slate-600"))
    elif scenario == "劇本二":
        return ("✅ 符合 (開高走低)", "bg-green-100 text-green-700 font-bold") if (change < 0 and high_time_ratio <= 0.6) else (("✅ 符合 (收黑走低)", "bg-green-50 text-green-600") if change < 0 else ("❌ 走勢分歧", "bg-slate-100 text-slate-600"))
    elif scenario == "劇本三":
        return ("✅ 符合 (跌勢延續)", "bg-green-100 text-green-700 font-bold") if (change < 0 and close_position <= 0.6) else (("✅ 符合 (收黑下跌)", "bg-green-50 text-green-600") if change < 0 else ("❌ 走勢分歧", "bg-slate-100 text-slate-600"))
    elif scenario == "劇本四":
        return ("✅ 符合 (開低反彈)", "bg-red-100 text-red-700 font-bold") if (change > 0 and low_time_ratio <= 0.6) else (("✅ 符合 (收紅反彈)", "bg-red-50 text-red-600") if change > 0 else ("❌ 走勢分歧", "bg-slate-100 text-slate-600"))
            
    return "NA", "bg-slate-100 text-slate-500"

# ==============================================================================
# 7 大主力券商買超個股與 ETF 獨立分流爬蟲 (已修復 \xa0 不可見字元問題)
# ==============================================================================

def fetch_single_broker_buy(session, broker_info, date_str):
    dt = datetime.strptime(date_str, "%Y/%m/%d")
    formatted_date = f"{dt.year}-{dt.month}-{dt.day}"
    
    url = f"https://fubon-ebrokerdj.fbs.com.tw/z/zg/zgb/zgb0.djhtm?a={broker_info['a']}&b={broker_info['b']}&c={broker_info['c']}&e={formatted_date}&f={formatted_date}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Referer': 'https://fubon-ebrokerdj.fbs.com.tw/'
    }
    
    buy_map = {}
    try:
        resp = session.get(url, headers=headers, timeout=6)
        resp.encoding = 'big5'
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        rows = soup.find_all('tr')
        for row in rows:
            cols = row.find_all('td')
            if len(cols) >= 4:
                # 徹底清除 \xa0 (non-breaking space) 與空白
                stk_raw = cols[0].text.replace('\xa0', '').replace('&nbsp;', '').strip()
                net_raw = cols[3].text.replace('\xa0', '').replace('&nbsp;', '').replace(',', '').strip()
                
                # 採用正規表達式完全萃取帶號與數值
                cleaned_net = re.sub(r'[^\d\-]', '', net_raw)
                
                if stk_raw and "名稱" not in stk_raw and "買超" not in stk_raw and "賣超" not in stk_raw and cleaned_net:
                    try:
                        net_val = int(cleaned_net)
                        if net_val > 0:
                            buy_map[stk_raw] = net_val
                    except ValueError:
                        pass
    except Exception as e:
        print(f"[{date_str}] Broker {broker_info['name']} fetch error: {e}")
        
    return buy_map

def aggregate_broker_buys_for_date(session, target_date_str):
    """
    分別聚合「個股 Top 10」與「ETF Top 10」
    """
    stocks_summary = {}
    etf_summary = {}
    
    for broker in BROKER_TARGETS:
        buy_map = fetch_single_broker_buy(session, broker, target_date_str)
        for stk, net_buy in buy_map.items():
            target_dict = etf_summary if is_etf(stk) else stocks_summary
            
            if stk not in target_dict:
                target_dict[stk] = {"stock": stk, "count": 0, "total_net_buy": 0, "brokers": []}
            target_dict[stk]["count"] += 1
            target_dict[stk]["total_net_buy"] += net_buy
            target_dict[stk]["brokers"].append(broker["name"])

    # 排序：1. 券商數 (desc), 2. 總張數 (desc)
    sorted_stocks = sorted(stocks_summary.values(), key=lambda x: (x["count"], x["total_net_buy"]), reverse=True)
    sorted_etfs = sorted(etf_summary.values(), key=lambda x: (x["count"], x["total_net_buy"]), reverse=True)
    
    return sorted_stocks[:10], sorted_etfs[:10]

def update_broker_history_json(session, trading_days):
    os.makedirs(DATA_DIR, exist_ok=True)
    existing_records = {}
    
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
        # 強制修復：如果個股或 ETF 列表為空，強制覆蓋重新抓取
        needs_update = (
            rec is None or 
            not rec.get("top_stocks") or 
            not rec.get("top_etfs")
        )
        
        if needs_update:
            print(f"抓取 7 大主力券商（修復個股與 ETF 分流）：{target_date_str}...")
            top_stocks, top_etfs = aggregate_broker_buys_for_date(session, target_date_str)
            existing_records[target_date_str] = {
                "date": target_date_str,
                "top_stocks": top_stocks,
                "top_etfs": top_etfs
            }

    sorted_history = sorted(existing_records.values(), key=lambda x: x["date"], reverse=True)

    with open(BROKER_JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted_history, f, ensure_ascii=False, indent=2)
        
    print(f"成功更新券商買超歷史紀錄至：{BROKER_JSON_FILE}")
    return sorted_history

# ==============================================================================
# 期貨與盤勢處理流程
# ==============================================================================

def process_single_date(session, target_date_str, prev_date_str, us_data_by_date):
    data = {
        "date": target_date_str,
        "prev_date": prev_date_str,
        "night_price_change": "NA",
        "night_vol": "NA",
        "day_vol": "NA",
        "night_volume_ratio": "NA",
        "vol_formula_str": "NA",
        
        "foreign_net_ah": "NA",
        "trust_net_ah": "NA",
        "dealer_net_ah": "NA",
        
        "foreign_net_full": "NA",
        "trust_net_full": "NA",
        "dealer_net_full": "NA",
        
        "us_dji": "NA",
        "us_ixic": "NA",
        "us_sox": "NA",
        "scenario": "NA",
        "forecast_desc": "數據尚未準備就緒或目前為休市期間 (NA)",
        "trust_signal": "NA",
        "actual_change": "NA",
        "sparkline_svg": '<span class="text-xs text-slate-400">未收盤</span>',
        "verify_status": "尚未驗證",
        "verify_badge_class": "bg-slate-100 text-slate-500"
    }

    night_price_change, night_vol = fetch_night_market_data(session, target_date_str)
    day_vol = fetch_day_market_volume(session, prev_date_str)
    
    ah_pos = fetch_institutional_positions_ah(session, target_date_str)
    full_pos = fetch_institutional_positions_full(session, target_date_str)
    
    actual_info = fetch_twse_intraday_taiex(session, target_date_str)
    us_info = get_us_info_for_date(us_data_by_date, prev_date_str)

    if night_vol is not None:
        data["night_vol"] = f"{night_vol:,}"
    if day_vol is not None:
        data["day_vol"] = f"{day_vol:,}"

    if night_vol is not None and day_vol is not None and (night_vol + day_vol) > 0:
        total_vol = night_vol + day_vol
        ratio = (night_vol / total_vol) * 100
        data["night_volume_ratio"] = f"{ratio:.1f}%"
        data["vol_formula_str"] = f"{night_vol:,} ÷ ({day_vol:,} + {night_vol:,})"
        
        if ratio >= 40:
            data["trust_signal"] = "很強的訊號 (>40%)"
        elif ratio < 30:
            data["trust_signal"] = "參考價值較低 (<30%)"
        else:
            data["trust_signal"] = "中等強度 (30%~40%)"

    if night_price_change is not None:
        data["night_price_change"] = f"+{night_price_change}" if night_price_change > 0 else str(night_price_change)
    
    if ah_pos.get("foreign") is not None:
        data["foreign_net_ah"] = ah_pos["foreign"]
    if ah_pos.get("trust") is not None:
        data["trust_net_ah"] = ah_pos["trust"]
    if ah_pos.get("dealer") is not None:
        data["dealer_net_ah"] = ah_pos["dealer"]

    if full_pos.get("foreign") is not None:
        data["foreign_net_full"] = full_pos["foreign"]
    if full_pos.get("trust") is not None:
        data["trust_net_full"] = full_pos["trust"]
    if full_pos.get("dealer") is not None:
        data["dealer_net_full"] = full_pos["dealer"]

    if "DJI" in us_info:
        data["us_dji"] = us_info["DJI"]["pts"]
    if "IXIC" in us_info:
        data["us_ixic"] = us_info["IXIC"]["pts"]
    if "SOX" in us_info:
        data["us_sox"] = us_info["SOX"]["pts"]

    foreign_ah = data["foreign_net_ah"]
    if night_price_change is not None and isinstance(foreign_ah, int):
        is_up = night_price_change > 0
        is_foreign_bullish = foreign_ah > 0

        if is_up and is_foreign_bullish:
            data["scenario"] = "劇本一"
            data["forecast_desc"] = "漲勢紮實，開盤後容易持續往上走。"
        elif is_up and not is_foreign_bullish:
            data["scenario"] = "劇本二"
            data["forecast_desc"] = "力道不足，容易出現開高走低，隨後殺下來。"
        elif not is_up and not is_foreign_bullish:
            data["scenario"] = "劇本三"
            data["forecast_desc"] = "跌勢延續，不建議貿然抄底，因為可能會持續下跌。"
        else:
            data["scenario"] = "劇本四"
            data["forecast_desc"] = "開低反彈，外資未跟著殺盤，反彈機率高。"

    data["actual_change"] = actual_info["change_str"]
    data["sparkline_svg"] = actual_info["sparkline_svg"]
    
    v_status, v_badge = verify_match(data["scenario"], actual_info)
    data["verify_status"] = v_status
    data["verify_badge_class"] = v_badge

    return data

def update_history_json():
    os.makedirs(DATA_DIR, exist_ok=True)
    existing_records = {}
    
    if os.path.exists(JSON_FILE):
        try:
            with open(JSON_FILE, "r", encoding="utf-8") as f:
                records_list = json.load(f)
                existing_records = {item["date"]: item for item in records_list}
            print(f"已成功載入 {len(existing_records)} 筆歷史期貨 JSON 紀錄。")
        except Exception as e:
            print(f"載入 {JSON_FILE} 失敗: {e}")

    session = requests.Session()
    us_data_by_date = fetch_us_indices(session)
    trading_days = get_past_trading_days(count=20)

    for target_date_str, prev_date_str in trading_days:
        rec = existing_records.get(target_date_str)
        
        needs_update = (
            rec is None or 
            rec.get("verify_status", "尚未驗證") == "尚未驗證" or 
            rec.get("scenario") == "NA" or
            rec.get("foreign_net_full") == "NA" or
            "foreign_net_ah" not in rec
        )
        
        if needs_update:
            print(f"抓取與分析資料：{target_date_str}...")
            new_record = process_single_date(session, target_date_str, prev_date_str, us_data_by_date)
            existing_records[target_date_str] = new_record

    sorted_history = sorted(existing_records.values(), key=lambda x: x["date"], reverse=True)

    with open(JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted_history, f, ensure_ascii=False, indent=2)
        
    print(f"成功更新歷史紀錄至：{JSON_FILE}")
    return sorted_history, session, trading_days

def format_signed_num(val):
    if val is None or val == "NA":
        return "NA", "text-slate-400"
    if isinstance(val, (int, float)):
        color = "text-red-500 font-semibold" if val > 0 else "text-green-500 font-semibold" if val < 0 else "text-slate-600"
        text = f"+{val:,}" if val > 0 else f"{val:,}"
        return text, color
    return str(val), "text-slate-400"

def format_pts_str(val_str):
    if not val_str or val_str == "NA":
        return "NA", "text-slate-400"
    
    val_s = str(val_str)
    val_display = f"{val_s}點" if not val_s.endswith("點") else val_s
    
    if val_s.startswith("+"):
        return val_display, "text-red-500 font-semibold"
    elif val_s.startswith("-"):
        return val_display, "text-green-500 font-semibold"
    return val_display, "text-slate-600"

# ==============================================================================
# HTML 網頁生成器 (index.html & broker.html)
# ==============================================================================

def generate_index_html(history_records, tw_kline_data, us_kline_data):
    latest_data = history_records[0]
    
    utc_now = datetime.now(timezone.utc)
    tw_now = utc_now + timedelta(hours=8)
    utc_time_str = utc_now.strftime('%Y/%m/%d %H:%M:%S')
    tw_time_str = tw_now.strftime('%Y/%m/%d %H:%M:%S')

    tw_kline_svg = generate_svg_kline_chart(tw_kline_data, "台股加權指數")
    us_kline_svg = generate_svg_kline_chart(us_kline_data, "美股 NASDAQ 指數")

    scenario_badge_class = "bg-slate-100 text-slate-700 border-slate-200"
    if latest_data["scenario"] == "劇本一":
        scenario_badge_class = "bg-red-50 text-red-700 border-red-200"
    elif latest_data["scenario"] == "劇本二":
        scenario_badge_class = "bg-yellow-50 text-yellow-700 border-yellow-200"
    elif latest_data["scenario"] == "劇本三":
        scenario_badge_class = "bg-green-50 text-green-700 border-green-200"
    elif latest_data["scenario"] == "劇本四":
        scenario_badge_class = "bg-blue-50 text-blue-700 border-blue-200"

    price_color_class = "text-slate-400"
    if str(latest_data["night_price_change"]).startswith("+"):
        price_color_class = "text-red-500"
    elif str(latest_data["night_price_change"]).startswith("-"):
        price_color_class = "text-green-500"

    f_ah_str, f_ah_color = format_signed_num(latest_data.get('foreign_net_ah'))
    t_ah_str, t_ah_color = format_signed_num(latest_data.get('trust_net_ah'))
    d_ah_str, d_ah_color = format_signed_num(latest_data.get('dealer_net_ah'))
    f_full_str, f_full_color = format_signed_num(latest_data.get('foreign_net_full'))

    latest_dji, latest_dji_c = format_pts_str(latest_data.get('us_dji'))
    latest_ixic, latest_ixic_c = format_pts_str(latest_data.get('us_ixic'))
    latest_sox, latest_sox_c = format_pts_str(latest_data.get('us_sox'))

    history_rows_html = ""
    for item in history_records[:20]:
        change_str = str(item['night_price_change'])
        c_color = "text-slate-600"
        if change_str.startswith("+"):
            c_color = "text-red-500 font-bold"
        elif change_str.startswith("-"):
            c_color = "text-green-500 font-bold"

        haf_str, haf_color = format_signed_num(item.get('foreign_net_ah'))
        hat_str, hat_color = format_signed_num(item.get('trust_net_ah'))
        had_str, had_color = format_signed_num(item.get('dealer_net_ah'))

        hff_str, hff_color = format_signed_num(item.get('foreign_net_full'))
        hft_str, hft_color = format_signed_num(item.get('trust_net_full'))
        hfd_str, hfd_color = format_signed_num(item.get('dealer_net_full'))

        hdji, hdji_c = format_pts_str(item.get('us_dji'))
        hixic, hixic_c = format_pts_str(item.get('us_ixic'))
        hsox, hsox_c = format_pts_str(item.get('us_sox'))

        s_badge = "bg-slate-100 text-slate-600"
        if item['scenario'] == '劇本一':
            s_badge = "bg-red-100 text-red-700 font-bold"
        elif item['scenario'] == '劇本二':
            s_badge = "bg-yellow-100 text-yellow-700 font-bold"
        elif item['scenario'] == '劇本三':
            s_badge = "bg-green-100 text-green-700 font-bold"
        elif item['scenario'] == '劇本四':
            s_badge = "bg-blue-100 text-blue-700 font-bold"

        act_change = str(item.get('actual_change', 'NA'))
        act_color = "text-red-500 font-semibold" if act_change.startswith("+") else "text-green-500 font-semibold" if act_change.startswith("-") else "text-slate-400"

        history_rows_html += f"""
        <tr class="hover:bg-slate-50 transition-colors">
            <td class="py-3 px-4 font-semibold text-slate-700">{item['date']}</td>
            <td class="py-3 px-4 {c_color}">{item['night_price_change']}</td>
            <td class="py-3 px-4">
                <div class="font-semibold text-slate-700">{item['night_volume_ratio']}</div>
                <div class="text-[11px] text-slate-400 mt-0.5">{item.get('vol_formula_str', '')}</div>
            </td>
            <td class="py-3 px-4">
                <div class="text-xs space-y-0.5">
                    <div><span class="text-slate-400">外資:</span> <span class="{haf_color}">{haf_str}</span></div>
                    <div><span class="text-slate-400">投信:</span> <span class="{hat_color}">{hat_str}</span></div>
                    <div><span class="text-slate-400">自營:</span> <span class="{had_color}">{had_str}</span></div>
                </div>
            </td>
            <td class="py-3 px-4">
                <div class="text-xs space-y-0.5">
                    <div><span class="text-slate-400">外資:</span> <span class="{hff_color}">{hff_str}</span></div>
                    <div><span class="text-slate-400">投信:</span> <span class="{hft_color}">{hft_str}</span></div>
                    <div><span class="text-slate-400">自營:</span> <span class="{hfd_color}">{hfd_str}</span></div>
                </div>
            </td>
            <td class="py-3 px-4">
                <div class="text-xs space-y-0.5">
                    <div><span class="text-slate-400">道瓊:</span> <span class="{hdji_c}">{hdji}</span></div>
                    <div><span class="text-slate-400">那指:</span> <span class="{hixic_c}">{hixic}</span></div>
                    <div><span class="text-slate-400">費半:</span> <span class="{hsox_c}">{hsox}</span></div>
                </div>
            </td>
            <td class="py-3 px-4"><span class="px-2.5 py-1 rounded-md text-xs {s_badge}">{item['scenario']}</span></td>
            <td class="py-3 px-4 text-center">{item.get('sparkline_svg', '')}<div class="text-[11px] {act_color} mt-0.5">{act_change}</div></td>
            <td class="py-3 px-4"><span class="px-2.5 py-1 rounded-md text-xs {item.get('verify_badge_class', 'bg-slate-100')}">{item.get('verify_status', 'NA')}</span></td>
        </tr>
        """

    html_content = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>台指期開盤走勢預測儀表板</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-slate-50 min-h-screen p-4 md:p-8 font-sans">
    <div class="max-w-[1400px] mx-auto space-y-6">
        
        <!-- Header & Nav Tabs -->
        <div class="bg-white rounded-xl shadow-sm p-6 border border-slate-200">
            <div class="flex flex-col md:flex-row md:items-center justify-between gap-4">
                <div>
                    <h1 class="text-2xl font-bold text-slate-800">📊 台指期夜盤三大數據走勢預測</h1>
                    <p class="text-slate-500 text-sm mt-1">分析目標交易日：<span class="font-bold text-slate-800">{latest_data['date']}</span> （對應前一日盤：{latest_data['prev_date']}）</p>
                </div>
                <div class="text-xs text-slate-500 md:text-right space-y-1">
                    <div>頁面產生時間：<span class="font-semibold text-slate-700">{tw_time_str}</span> <span class="text-slate-400">(UTC+8)</span></div>
                    <div>頁面產生時間：<span class="font-semibold text-slate-700">{utc_time_str}</span> <span class="text-slate-400">(UTC)</span></div>
                </div>
            </div>
            
            <div class="flex space-x-2 mt-6 border-b border-slate-100 pb-2">
                <a href="./index.html" class="px-4 py-2 text-sm font-bold text-indigo-600 bg-indigo-50 rounded-lg border border-indigo-100">📊 台指期夜盤預測</a>
                <a href="./broker.html" class="px-4 py-2 text-sm font-medium text-slate-600 hover:bg-slate-100 rounded-lg transition-colors">🏦 主力券商買超分析</a>
            </div>
        </div>

        <!-- 3個月台股與美股原生 SVG K 線走勢圖區塊 -->
        <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div class="bg-white p-5 rounded-xl shadow-sm border border-slate-200">
                <div class="flex items-center justify-between mb-3">
                    <h2 class="text-sm font-bold text-slate-800">🇹🇼 台股加權指數 (TAIEX) 近 3 個月 K 線圖</h2>
                    <span class="text-xs font-semibold text-slate-400">3 Months Candlestick</span>
                </div>
                {tw_kline_svg}
            </div>

            <div class="bg-white p-5 rounded-xl shadow-sm border border-slate-200">
                <div class="flex items-center justify-between mb-3">
                    <h2 class="text-sm font-bold text-slate-800">🇺🇸 美股 NASDAQ 指數 (IXIC) 近 3 個月 K 線圖</h2>
                    <span class="text-xs font-semibold text-slate-400">3 Months Candlestick</span>
                </div>
                {us_kline_svg}
            </div>
        </div>

        <!-- 四大核心指標卡片 Grid -->
        <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
            <div class="bg-white p-5 rounded-xl shadow-sm border border-slate-200">
                <span class="text-xs font-semibold text-slate-400 uppercase tracking-wider">1. 夜盤漲跌點數</span>
                <div class="text-3xl font-extrabold mt-2 {price_color_class}">
                    {latest_data['night_price_change']}
                </div>
                <p class="text-xs text-slate-400 mt-2">門檻：超過 ±300 點代表預期大變</p>
            </div>

            <div class="bg-white p-5 rounded-xl shadow-sm border border-slate-200">
                <span class="text-xs font-semibold text-slate-400 uppercase tracking-wider">2. 夜盤量佔比</span>
                <div class="text-3xl font-extrabold text-slate-800 mt-2">
                    {latest_data['night_volume_ratio']}
                </div>
                <p class="text-xs text-slate-500 mt-2">訊號強度：<span class="font-bold text-indigo-600">{latest_data['trust_signal']}</span></p>
                <div class="mt-2 pt-2 border-t border-slate-100 text-[11px] text-slate-400">
                    計算算式：{latest_data.get('vol_formula_str', 'NA')}
                </div>
            </div>

            <div class="bg-white p-5 rounded-xl shadow-sm border border-slate-200">
                <span class="text-xs font-semibold text-slate-400 uppercase tracking-wider">3. 三大法人多空淨額 (夜盤)</span>
                <div class="text-3xl font-extrabold text-slate-800 mt-2">
                    <span class="{f_ah_color}">{f_ah_str}</span> <span class="text-sm font-normal text-slate-500">口 (外資夜盤)</span>
                </div>
                <p class="text-xs text-slate-400 mt-1">劇本對照門檻：超過 ±1,000 口</p>
                <div class="mt-2 pt-2 border-t border-slate-100 flex justify-between text-xs text-slate-500">
                    <div>投信(夜): <span class="{t_ah_color} font-medium">{t_ah_str}</span></div>
                    <div>外資(全): <span class="{f_full_color} font-medium">{f_full_str}</span></div>
                </div>
            </div>

            <div class="bg-white p-5 rounded-xl shadow-sm border border-slate-200">
                <span class="text-xs font-semibold text-slate-400 uppercase tracking-wider">4. 美股三大指數 (點數)</span>
                <div class="mt-2 space-y-1">
                    <div class="flex justify-between items-center text-sm">
                        <span class="text-slate-500 font-medium">道瓊 (DJI):</span>
                        <span class="{latest_dji_c}">{latest_dji}</span>
                    </div>
                    <div class="flex justify-between items-center text-sm">
                        <span class="text-slate-500 font-medium">NASDAQ:</span>
                        <span class="{latest_ixic_c}">{latest_ixic}</span>
                    </div>
                    <div class="flex justify-between items-center text-sm">
                        <span class="text-slate-500 font-medium">費半 (SOX):</span>
                        <span class="{latest_sox_c}">{latest_sox}</span>
                    </div>
                </div>
                <p class="text-[11px] text-slate-400 mt-2">同夜盤時段美股漲跌點數</p>
            </div>
        </div>

        <!-- 今日開盤預測劇本 -->
        <div class="bg-white rounded-xl shadow-sm p-6 border border-slate-200">
            <h2 class="text-lg font-bold text-slate-800 mb-4">🔮 今日開盤預測劇本</h2>
            
            <div class="p-5 rounded-xl border-2 {scenario_badge_class} mb-6">
                <div class="flex items-center space-x-2">
                    <span class="font-bold text-xl">{latest_data['scenario']}</span>
                </div>
                <p class="mt-2 text-base font-semibold">{latest_data['forecast_desc']}</p>
            </div>

            <!-- 四種劇本對照指南 -->
            <div class="mt-6">
                <h3 class="text-sm font-semibold text-slate-600 mb-3">📋 四種實戰劇本對照指南</h3>
                <div class="overflow-x-auto">
                    <table class="w-full text-sm text-left text-slate-600 border-collapse">
                        <thead class="text-xs text-slate-700 uppercase bg-slate-100">
                            <tr>
                                <th class="py-3 px-4 rounded-l-lg">劇本</th>
                                <th class="py-3 px-4">夜盤漲跌</th>
                                <th class="py-3 px-4">外資多空淨額 (夜盤)</th>
                                <th class="py-3 px-4 rounded-r-lg">預期走勢</th>
                            </tr>
                        </thead>
                        <tbody class="divide-y divide-slate-100">
                            <tr class="hover:bg-slate-50 {'bg-red-50/60 font-medium' if latest_data['scenario'] == '劇本一' else ''}">
                                <td class="py-3 px-4 font-bold text-red-600">劇本一</td>
                                <td class="py-3 px-4 text-red-500">上漲</td>
                                <td class="py-3 px-4 text-red-500">正數（看多）</td>
                                <td class="py-3 px-4">漲勢紮實，開盤後容易持續往上走。</td>
                            </tr>
                            <tr class="hover:bg-slate-50 {'bg-yellow-50/60 font-medium' if latest_data['scenario'] == '劇本二' else ''}">
                                <td class="py-3 px-4 font-bold text-yellow-600">劇本二</td>
                                <td class="py-3 px-4 text-red-500">上漲</td>
                                <td class="py-3 px-4 text-green-500">負數（看空）</td>
                                <td class="py-3 px-4">力道不足，容易出現開高走低，隨後殺下來。</td>
                            </tr>
                            <tr class="hover:bg-slate-50 {'bg-green-50/60 font-medium' if latest_data['scenario'] == '劇本三' else ''}">
                                <td class="py-3 px-4 font-bold text-green-600">劇本三</td>
                                <td class="py-3 px-4 text-green-500">下跌</td>
                                <td class="py-3 px-4 text-green-500">負數（看空）</td>
                                <td class="py-3 px-4">跌勢延續，不建議貿然抄底，因為可能會持續下跌。</td>
                            </tr>
                            <tr class="hover:bg-slate-50 {'bg-blue-50/60 font-medium' if latest_data['scenario'] == '劇本四' else ''}">
                                <td class="py-3 px-4 font-bold text-blue-600">劇本四</td>
                                <td class="py-3 px-4 text-green-500">下跌</td>
                                <td class="py-3 px-4 text-red-500">正數（看多）</td>
                                <td class="py-3 px-4">開低反彈，外資未跟著殺盤，反彈機率高。</td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- 過去 20 個交易日歷史紀錄與當日走勢比對表格 -->
        <div class="bg-white rounded-xl shadow-sm p-6 border border-slate-200">
            <div class="flex items-center justify-between mb-4">
                <h2 class="text-lg font-bold text-slate-800">🗓️ 過去 20 個交易日歷史紀錄與實測比對</h2>
                <a href="./data/history.json" target="_blank" class="text-xs text-indigo-600 hover:underline">📥 下載完整 history.json</a>
            </div>
            <div class="overflow-x-auto">
                <table class="w-full text-sm text-left text-slate-600 border-collapse">
                    <thead class="text-xs text-slate-700 uppercase bg-slate-100">
                        <tr>
                            <th class="py-3 px-4 rounded-l-lg">交易日期</th>
                            <th class="py-3 px-4">夜盤漲跌</th>
                            <th class="py-3 px-4">夜盤量佔比 (算式)</th>
                            <th class="py-3 px-4">三大法人淨額 (夜盤)</th>
                            <th class="py-3 px-4">三大法人淨額 (全日)</th>
                            <th class="py-3 px-4">美股指數 (對應夜盤)</th>
                            <th class="py-3 px-4">預測劇本</th>
                            <th class="py-3 px-4 text-center">當日加權指數走勢 (09:00~13:30)</th>
                            <th class="py-3 px-4 rounded-r-lg">型態驗證</th>
                        </tr>
                    </thead>
                    <tbody class="divide-y divide-slate-100">
                        {history_rows_html}
                    </tbody>
                </table>
            </div>
        </div>

        <!-- Footer -->
        <footer class="text-center text-xs text-slate-400 py-4">
            資料來源：台灣期貨交易所 (TAIFEX) & 台灣證券交易所 (TWSE) & Yahoo Finance | 自動化發布 via GitHub Actions & Pages
        </footer>
    </div>
</body>
</html>
"""
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html_content)
    print("成功產生 index.html！")

def render_top_cards(items_list, category_label):
    if not items_list:
        return f'<div class="col-span-full p-8 text-center bg-white rounded-xl text-slate-400 text-sm">今日無{category_label}買超紀錄或目前為休市期間 (NA)</div>'
    
    cards_html = ""
    for rank, item in enumerate(items_list, start=1):
        brokers_badge = "".join([f'<span class="px-2 py-0.5 text-[11px] bg-indigo-50 text-indigo-700 rounded border border-indigo-100 font-medium">{b}</span>' for b in item["brokers"]])
        cards_html += f"""
        <div class="bg-white p-5 rounded-xl shadow-sm border border-slate-200 flex flex-col justify-between">
            <div>
                <div class="flex items-center justify-between mb-2">
                    <span class="px-2.5 py-0.5 text-xs font-bold bg-slate-800 text-white rounded-full">第 {rank} 名</span>
                    <span class="text-xs font-bold text-indigo-600 bg-indigo-50 px-2 py-1 rounded-md">{item['count']} 家主力券商</span>
                </div>
                <h3 class="text-lg font-extrabold text-slate-800 my-1">{item['stock']}</h3>
                <div class="flex flex-wrap gap-1 my-3">
                    {brokers_badge}
                </div>
            </div>
            <div class="pt-3 border-t border-slate-100 flex justify-between items-center text-xs">
                <span class="text-slate-400">總買超淨張數:</span>
                <span class="text-base font-extrabold text-red-500">+{item['total_net_buy']:,} 張</span>
            </div>
        </div>
        """
    return cards_html

def generate_broker_html(broker_records):
    latest_data = broker_records[0] if broker_records else {"date": "NA", "top_stocks": [], "top_etfs": []}
    
    utc_now = datetime.now(timezone.utc)
    tw_now = utc_now + timedelta(hours=8)
    utc_time_str = utc_now.strftime('%Y/%m/%d %H:%M:%S')
    tw_time_str = tw_now.strftime('%Y/%m/%d %H:%M:%S')

    top_stocks_html = render_top_cards(latest_data.get("top_stocks", []), "個股")
    top_etfs_html = render_top_cards(latest_data.get("top_etfs", []), "ETF")

    history_broker_rows = ""
    for record in broker_records[:20]:
        stock_str_list = []
        for s in record.get("top_stocks", [])[:5]:
            stock_str_list.append(f'<span class="inline-block bg-slate-50 border border-slate-200 px-2 py-1 rounded text-xs mr-1 mb-1"><b>{s["stock"]}</b> ({s["count"]}家: +{s["total_net_buy"]:,}張)</span>')
        stocks_display = "".join(stock_str_list) if stock_str_list else '<span class="text-slate-400 text-xs">無紀錄</span>'

        etf_str_list = []
        for e in record.get("top_etfs", [])[:5]:
            etf_str_list.append(f'<span class="inline-block bg-indigo-50/50 border border-indigo-100 px-2 py-1 rounded text-xs mr-1 mb-1 text-indigo-900"><b>{e["stock"]}</b> ({e["count"]}家: +{e["total_net_buy"]:,}張)</span>')
        etfs_display = "".join(etf_str_list) if etf_str_list else '<span class="text-slate-400 text-xs">無紀錄</span>'

        history_broker_rows += f"""
        <tr class="hover:bg-slate-50 transition-colors">
            <td class="py-3 px-4 font-semibold text-slate-700 whitespace-nowrap">{record['date']}</td>
            <td class="py-3 px-4">{stocks_display}</td>
            <td class="py-3 px-4">{etfs_display}</td>
        </tr>
        """

    html_content = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>主力券商買超個股與 ETF 分析</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-slate-50 min-h-screen p-4 md:p-8 font-sans">
    <div class="max-w-[1400px] mx-auto space-y-6">
        
        <!-- Header & Nav Tabs -->
        <div class="bg-white rounded-xl shadow-sm p-6 border border-slate-200">
            <div class="flex flex-col md:flex-row md:items-center justify-between gap-4">
                <div>
                    <h1 class="text-2xl font-bold text-slate-800">🏦 7 大主力券商分點聯合買超分析</h1>
                    <p class="text-slate-500 text-sm mt-1">監控分點：摩根大通、凱基台北、元大土城永寧、富邦建國、高盛、瑞銀、美林</p>
                </div>
                <div class="text-xs text-slate-500 md:text-right space-y-1">
                    <div>頁面產生時間：<span class="font-semibold text-slate-700">{tw_time_str}</span> <span class="text-slate-400">(UTC+8)</span></div>
                    <div>頁面產生時間：<span class="font-semibold text-slate-700">{utc_time_str}</span> <span class="text-slate-400">(UTC)</span></div>
                </div>
            </div>
            
            <div class="flex space-x-2 mt-6 border-b border-slate-100 pb-2">
                <a href="./index.html" class="px-4 py-2 text-sm font-medium text-slate-600 hover:bg-slate-100 rounded-lg transition-colors">📊 台指期夜盤預測</a>
                <a href="./broker.html" class="px-4 py-2 text-sm font-bold text-indigo-600 bg-indigo-50 rounded-lg border border-indigo-100">🏦 主力券商買超分析</a>
            </div>
        </div>

        <!-- 區塊一：今日 Top 10 主力券商買超個股 (不含 ETF) -->
        <div class="bg-white rounded-xl shadow-sm p-6 border border-slate-200">
            <div class="flex items-center justify-between mb-4">
                <div>
                    <h2 class="text-lg font-bold text-slate-800">🔥 今日 7 大主力券商聯合買超 Top 10 個股</h2>
                    <p class="text-xs text-slate-400 mt-0.5">資料日期：{latest_data['date']} | 排除 ETF | 優先比對「買超券商數」，數量相同時比對「總買超張數」</p>
                </div>
            </div>
            <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5 gap-4">
                {top_stocks_html}
            </div>
        </div>

        <!-- 區塊二：今日 Top 10 主力券商買超 ETF -->
        <div class="bg-white rounded-xl shadow-sm p-6 border border-slate-200">
            <div class="flex items-center justify-between mb-4">
                <div>
                    <h2 class="text-lg font-bold text-indigo-900">📊 今日 7 大主力券商聯合買超 Top 10 ETF</h2>
                    <p class="text-xs text-slate-400 mt-0.5">資料日期：{latest_data['date']} | 專屬 ETF 排名 | 優先比對「買超券商數」，數量相同時比對「總買超張數」</p>
                </div>
            </div>
            <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-5 gap-4">
                {top_etfs_html}
            </div>
        </div>

        <!-- 區塊三：歷史紀錄表格 -->
        <div class="bg-white rounded-xl shadow-sm p-6 border border-slate-200">
            <div class="flex items-center justify-between mb-4">
                <h2 class="text-lg font-bold text-slate-800">🗓️ 過去 20 個交易日主力買超歷史紀錄 (個股與 ETF 分流)</h2>
                <a href="./data/broker_history.json" target="_blank" class="text-xs text-indigo-600 hover:underline">📥 下載完整 broker_history.json</a>
            </div>
            <div class="overflow-x-auto">
                <table class="w-full text-sm text-left text-slate-600 border-collapse">
                    <thead class="text-xs text-slate-700 uppercase bg-slate-100">
                        <tr>
                            <th class="py-3 px-4 rounded-l-lg whitespace-nowrap">交易日期</th>
                            <th class="py-3 px-4">主力買超個股 Top 5 (個股名稱 / 買超券商數 / 總張數)</th>
                            <th class="py-3 px-4 rounded-r-lg">主力買超 ETF Top 5 (ETF 名稱 / 買超券商數 / 總張數)</th>
                        </tr>
                    </thead>
                    <tbody class="divide-y divide-slate-100">
                        {history_broker_rows}
                    </tbody>
                </table>
            </div>
        </div>

        <!-- Footer -->
        <footer class="text-center text-xs text-slate-400 py-4">
            資料來源：富邦證券 MoneyDJ 分點明細查詢 | 自動化發布 via GitHub Actions & Pages
        </footer>
    </div>
</body>
</html>
"""
    with open("broker.html", "w", encoding="utf-8") as f:
        f.write(html_content)
    print("成功產生獨立分流個股與 ETF 的 broker.html！")

# ==============================================================================
# 主程式 (Main)
# ==============================================================================

if __name__ == "__main__":
    history_records, session, trading_days = update_history_json()
    
    print("正在抓取與更新 7 大主力券商買超歷史紀錄 (修復字元解析與分流)...")
    broker_records = update_broker_history_json(session, trading_days)
    
    print("正在抓取近 3 個月台股與美股 K 線 OHLC 數據...")
    tw_kline_data = fetch_ohlc_3m(session, "^TWII")
    us_kline_data = fetch_ohlc_3m(session, "^IXIC")
    
    generate_index_html(history_records, tw_kline_data, us_kline_data)
    generate_broker_html(broker_records)
    print("所有頁面 (index.html, broker.html) 與 JSON 數據皆已更新完成！")
