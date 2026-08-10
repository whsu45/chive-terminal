import os
import re
import json
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta

DATA_DIR = "data"
JSON_FILE = os.path.join(DATA_DIR, "history.json")


def get_past_trading_days(count=20):
    """ 計算過去 N 個交易日的 (目標交易日, 前一交易日) """
    trading_days = []
    curr = datetime.now()

    if curr.weekday() == 5:  # Saturday
        curr = curr + timedelta(days=2)
    elif curr.weekday() == 6:  # Sunday
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


def fetch_all_institutional_positions(session, target_date_str):
    url = "https://www.taifex.com.tw/cht/3/futContractsDate"
    payload = {
        'queryType': '1', 'goDay': '', 'doQuery': '1', 'dateaddcnt': '',
        'queryDate': target_date_str, 'commodityId': 'TXF', 'button': '送出查詢'
    }
    headers = {'User-Agent': 'Mozilla/5.0'}
    positions = {"foreign_net": None, "trust_net": None, "dealer_net": None}

    try:
        resp = session.post(url, data=payload, headers=headers, timeout=5)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, 'html.parser')
            tables = soup.find_all('table', {'class': ['table_f', 'table_a']})
            for table in tables:
                for row in table.find_all('tr'):
                    cols = [td.text.strip() for td in row.find_all('td')]
                    for idx, col in enumerate(cols):
                        if '自營商' in col and positions["dealer_net"] is None:
                            if len(cols) > idx + 5:
                                positions["dealer_net"] = clean_int(cols[idx + 5])
                        elif '投信' in col and positions["trust_net"] is None:
                            if len(cols) > idx + 5:
                                positions["trust_net"] = clean_int(cols[idx + 5])
                        elif '外資' in col and positions["foreign_net"] is None:
                            if len(cols) > idx + 5:
                                positions["foreign_net"] = clean_int(cols[idx + 5])
    except Exception as e:
        print(f"[{target_date_str}] Institutional position error: {e}")

    return positions


def fetch_us_indices(session):
    """
    抓取美股三大指數 (^DJI 道瓊, ^IXIC 那指, ^SOX 費半) 的漲跌點數 (Points)
    """
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


def generate_svg_sparkline(prices):
    if not prices or len(prices) < 2:
        return '<span class="text-xs text-slate-400">無圖表</span>'

    open_price = prices[0]
    close_price = prices[-1]
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

    svg = f'''
    <svg width="{width}" height="{height}" class="inline-block overflow-visible" title="開: {open_price:.0f} | 收: {close_price:.0f}">
        <polyline fill="none" stroke="{color}" stroke-width="1.8" points="{polyline_str}" />
    </svg>
    '''
    return svg


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
                    open_p = prices[0]
                    close_p = prices[-1]
                    high_p = max(prices)
                    low_p = min(prices)

                    sampled = prices[::60] if len(prices) > 60 else prices
                    sparkline_svg = generate_svg_sparkline(sampled)

                    actual_change = close_p - open_p
                    change_str = f"+{actual_change:.0f}點" if actual_change > 0 else f"{actual_change:.0f}點"

                    return {
                        "open": open_p, "close": close_p, "high": high_p, "low": low_p,
                        "change_str": change_str,
                        "sparkline_svg": sparkline_svg,
                        "prices": prices
                    }
    except Exception as e:
        print(f"[{target_date_str}] TWSE TAIEX fetch error: {e}")

    return {
        "open": None, "close": None, "high": None, "low": None,
        "change_str": "NA",
        "sparkline_svg": '<span class="text-xs text-slate-400">未收盤</span>',
        "prices": []
    }


def verify_match(scenario, actual_info):
    open_p = actual_info.get("open")
    close_p = actual_info.get("close")
    high_p = actual_info.get("high")
    low_p = actual_info.get("low")
    prices = actual_info.get("prices", [])

    if not scenario or scenario == "NA" or None in [open_p, close_p, high_p, low_p] or len(prices) < 2:
        return "尚未驗證", "bg-slate-100 text-slate-500"

    total_pts = len(prices)
    idx_high = prices.index(high_p)
    idx_low = prices.index(low_p)

    high_time_ratio = idx_high / total_pts
    low_time_ratio = idx_low / total_pts

    total_range = high_p - low_p if high_p != low_p else 1.0
    close_position = (close_p - low_p) / total_range
    change = close_p - open_p

    if scenario == "劇本一":
        if change > 0 and close_position >= 0.4:
            return "✅ 符合 (一路走高)", "bg-red-100 text-red-700 font-bold"
        elif change > 0:
            return "✅ 符合 (收紅上漲)", "bg-red-50 text-red-600"
        else:
            return "❌ 走勢分歧", "bg-slate-100 text-slate-600"

    elif scenario == "劇本二":
        if change < 0 and high_time_ratio <= 0.6:
            return "✅ 符合 (開高走低)", "bg-green-100 text-green-700 font-bold"
        elif change < 0:
            return "✅ 符合 (收黑走低)", "bg-green-50 text-green-600"
        else:
            return "❌ 走勢分歧", "bg-slate-100 text-slate-600"

    elif scenario == "劇本三":
        if change < 0 and close_position <= 0.6:
            return "✅ 符合 (跌勢延續)", "bg-green-100 text-green-700 font-bold"
        elif change < 0:
            return "✅ 符合 (收黑下跌)", "bg-green-50 text-green-600"
        else:
            return "❌ 走勢分歧", "bg-slate-100 text-slate-600"

    elif scenario == "劇本四":
        if change > 0 and low_time_ratio <= 0.6:
            return "✅ 符合 (開低反彈)", "bg-red-100 text-red-700 font-bold"
        elif change > 0:
            return "✅ 符合 (收紅反彈)", "bg-red-50 text-red-600"
        else:
            return "❌ 走勢分歧", "bg-slate-100 text-slate-600"

    return "NA", "bg-slate-100 text-slate-500"


def process_single_date(session, target_date_str, prev_date_str, us_data_by_date):
    data = {
        "date": target_date_str,
        "prev_date": prev_date_str,
        "night_price_change": "NA",
        "night_vol": "NA",
        "day_vol": "NA",
        "night_volume_ratio": "NA",
        "vol_formula_str": "NA",
        "foreign_net_contracts": "NA",
        "trust_net_contracts": "NA",
        "dealer_net_contracts": "NA",
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
    inst_positions = fetch_all_institutional_positions(session, target_date_str)
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

    foreign_net = inst_positions.get("foreign_net")
    trust_net = inst_positions.get("trust_net")
    dealer_net = inst_positions.get("dealer_net")

    if foreign_net is not None:
        data["foreign_net_contracts"] = foreign_net
    if trust_net is not None:
        data["trust_net_contracts"] = trust_net
    if dealer_net is not None:
        data["dealer_net_contracts"] = dealer_net

    # 改為儲存漲跌點數 (Points)
    if "DJI" in us_info:
        data["us_dji"] = us_info["DJI"]["pts"]
    if "IXIC" in us_info:
        data["us_ixic"] = us_info["IXIC"]["pts"]
    if "SOX" in us_info:
        data["us_sox"] = us_info["SOX"]["pts"]

    if night_price_change is not None and foreign_net is not None:
        is_up = night_price_change > 0
        is_foreign_bullish = foreign_net > 0

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
            print(f"已成功載入 {len(existing_records)} 筆歷史 JSON 紀錄。")
        except Exception as e:
            print(f"載入 {JSON_FILE} 失敗: {e}")

    session = requests.Session()
    us_data_by_date = fetch_us_indices(session)
    trading_days = get_past_trading_days(count=20)

    for target_date_str, prev_date_str in trading_days:
        rec = existing_records.get(target_date_str)

        # 若為舊格式 (%) 則自動覆蓋重抓點數格式
        needs_update = (
                rec is None or
                rec.get("verify_status", "尚未驗證") == "尚未驗證" or
                rec.get("scenario") == "NA" or
                "%" in str(rec.get("us_dji", ""))
        )

        if needs_update:
            print(f"抓取與分析資料：{target_date_str}...")
            new_record = process_single_date(session, target_date_str, prev_date_str, us_data_by_date)
            existing_records[target_date_str] = new_record

    sorted_history = sorted(existing_records.values(), key=lambda x: x["date"], reverse=True)

    with open(JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted_history, f, ensure_ascii=False, indent=2)

    print(f"成功更新含美股漲跌點數之歷史紀錄至：{JSON_FILE}")
    return sorted_history


def format_signed_num(val):
    if val is None or val == "NA":
        return "NA", "text-slate-400"
    if isinstance(val, (int, float)):
        color = "text-red-500 font-semibold" if val > 0 else "text-green-500 font-semibold" if val < 0 else "text-slate-600"
        text = f"+{val:,}" if val > 0 else f"{val:,}"
        return text, color
    return str(val), "text-slate-400"


def format_pts_str(val_str):
    """ 格式化美股漲跌點數 (例如: +118點) """
    if not val_str or val_str == "NA":
        return "NA", "text-slate-400"

    val_s = str(val_str)
    val_display = f"{val_s}點" if not val_s.endswith("點") else val_s

    if val_s.startswith("+"):
        return val_display, "text-red-500 font-semibold"
    elif val_s.startswith("-"):
        return val_display, "text-green-500 font-semibold"
    return val_display, "text-slate-600"


def generate_html(history_records):
    latest_data = history_records[0]

    utc_now = datetime.now(timezone.utc)
    tw_now = utc_now + timedelta(hours=8)

    utc_time_str = utc_now.strftime('%Y/%m/%d %H:%M:%S')
    tw_time_str = tw_now.strftime('%Y/%m/%d %H:%M:%S')

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

    f_str, f_color = format_signed_num(latest_data.get('foreign_net_contracts'))
    t_str, t_color = format_signed_num(latest_data.get('trust_net_contracts'))
    d_str, d_color = format_signed_num(latest_data.get('dealer_net_contracts'))

    # 美股點數格式化
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

        hf_str, hf_color = format_signed_num(item.get('foreign_net_contracts'))
        ht_str, ht_color = format_signed_num(item.get('trust_net_contracts'))
        hd_str, hd_color = format_signed_num(item.get('dealer_net_contracts'))

        # 歷史表格美股點數格式化
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
        act_color = "text-red-500 font-semibold" if act_change.startswith(
            "+") else "text-green-500 font-semibold" if act_change.startswith("-") else "text-slate-400"

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
                    <div><span class="text-slate-400">外資:</span> <span class="{hf_color}">{hf_str}</span></div>
                    <div><span class="text-slate-400">投信:</span> <span class="{ht_color}">{ht_str}</span></div>
                    <div><span class="text-slate-400">自營:</span> <span class="{hd_color}">{hd_str}</span></div>
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
    <div class="max-w-7xl mx-auto space-y-6">

        <!-- Header -->
        <div class="bg-white rounded-xl shadow-sm p-6 border border-slate-200">
            <div class="flex flex-col md:flex-row md:items-center justify-between gap-2">
                <div>
                    <h1 class="text-2xl font-bold text-slate-800">📊 台指期夜盤三大數據走勢預測</h1>
                    <p class="text-slate-500 text-sm mt-1">分析目標交易日：<span class="font-bold text-slate-800">{latest_data['date']}</span> （對應前一日盤：{latest_data['prev_date']}）</p>
                </div>
                <div class="text-xs text-slate-500 md:text-right space-y-1">
                    <div>頁面產生時間：<span class="font-semibold text-slate-700">{tw_time_str}</span> <span class="text-slate-400">(UTC+8)</span></div>
                    <div>頁面產生時間：<span class="font-semibold text-slate-700">{utc_time_str}</span> <span class="text-slate-400">(UTC)</span></div>
                </div>
            </div>
        </div>

        <!-- 四大核心指標卡片 Grid -->
        <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
            <!-- 1. 夜盤漲跌 -->
            <div class="bg-white p-5 rounded-xl shadow-sm border border-slate-200">
                <span class="text-xs font-semibold text-slate-400 uppercase tracking-wider">1. 夜盤漲跌點數</span>
                <div class="text-3xl font-extrabold mt-2 {price_color_class}">
                    {latest_data['night_price_change']}
                </div>
                <p class="text-xs text-slate-400 mt-2">門檻：超過 ±300 點代表預期大變</p>
            </div>

            <!-- 2. 夜盤量佔比 -->
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

            <!-- 3. 三大法人 -->
            <div class="bg-white p-5 rounded-xl shadow-sm border border-slate-200">
                <span class="text-xs font-semibold text-slate-400 uppercase tracking-wider">3. 三大法人多空淨額</span>
                <div class="text-3xl font-extrabold text-slate-800 mt-2">
                    <span class="{f_color}">{f_str}</span> <span class="text-sm font-normal text-slate-500">口 (外資)</span>
                </div>
                <p class="text-xs text-slate-400 mt-1">關鍵門檻：超過 ±1,000 口</p>
                <div class="mt-2 pt-2 border-t border-slate-100 flex justify-between text-xs text-slate-500">
                    <div>投信: <span class="{t_color} font-medium">{t_str}</span></div>
                    <div>自營: <span class="{d_color} font-medium">{d_str}</span></div>
                </div>
            </div>

            <!-- 4. 美股三大指數 (點數版) -->
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
                                <th class="py-3 px-4">外資多空淨額</th>
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
                            <th class="py-3 px-4">三大法人淨額</th>
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
    print("成功產生含美股漲跌點數的 index.html！")


if __name__ == "__main__":
    history_records = update_history_json()
    generate_html(history_records)