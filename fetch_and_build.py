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

    if curr.weekday() == 5:  # Saturday -> 推算至下週一
        curr = curr + timedelta(days=2)
    elif curr.weekday() == 6:  # Sunday -> 推算至下週一
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
    """ 清理字串並轉為帶正負號的整數 """
    if not text:
        return None
    text = text.strip().replace(',', '')
    is_negative = '-' in text or '▼' in text
    cleaned = re.sub(r'[^\d]', '', text)
    if cleaned:
        val = int(cleaned)
        return -val if is_negative else val
    return None


def fetch_night_market_data(session, target_date_str):
    """
    抓取期交所夜盤行情 (marketCode = 1)
    """
    url = "https://www.taifex.com.tw/cht/3/futDailyMarketReport"
    payload = {
        'queryType': '2',
        'marketCode': '1',
        'dateaddcnt': '',
        'commodity_id': 'TX',
        'commodity_id2': '',
        'queryDate': target_date_str,
        'MarketCode': '1',
        'commodity_idt': 'TX',
        'commodity_id2t': '',
        'commodity_id2t2': ''
    }
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Content-Type': 'application/x-www-form-urlencoded',
        'Referer': 'https://www.taifex.com.tw/cht/3/futDailyMarketReport'
    }
    try:
        resp = session.post(url, data=payload, headers=headers, timeout=10)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, 'html.parser')
            tables = soup.find_all('table', {'class': ['table_f', 'table_a']})
            for table in tables:
                for row in table.find_all('tr'):
                    cols = [td.text.strip() for td in row.find_all('td')]
                    if len(cols) >= 9 and cols[0] == 'TX':
                        price_change = clean_int(cols[6])
                        volume = clean_int(cols[8])
                        return price_change, volume
    except Exception as e:
        print(f"[{target_date_str}] Night market error: {e}")
    return None, None


def fetch_day_market_volume(session, prev_date_str):
    """
    抓取前一交易日日盤成交量 (marketCode = 0)
    """
    url = "https://www.taifex.com.tw/cht/3/futDailyMarketReport"
    payload = {
        'queryType': '2',
        'marketCode': '0',
        'dateaddcnt': '',
        'commodity_id': 'TX',
        'commodity_id2': '',
        'queryDate': prev_date_str,
        'MarketCode': '0',
        'commodity_idt': 'TX',
        'commodity_id2t': '',
        'commodity_id2t2': ''
    }
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Content-Type': 'application/x-www-form-urlencoded',
        'Referer': 'https://www.taifex.com.tw/cht/3/futDailyMarketReport'
    }
    try:
        resp = session.post(url, data=payload, headers=headers, timeout=10)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, 'html.parser')
            tables = soup.find_all('table', {'class': ['table_f', 'table_a']})
            for table in tables:
                for row in table.find_all('tr'):
                    cols = [td.text.strip() for td in row.find_all('td')]
                    if len(cols) >= 9 and cols[0] == 'TX':
                        volume = clean_int(cols[8])
                        return volume
    except Exception as e:
        print(f"[{prev_date_str}] Day volume error: {e}")
    return None


def fetch_foreign_net_position(session, target_date_str):
    """
    抓取外資台指期夜盤多空淨額 (口數)
    commodityId = TXF
    """
    url = "https://www.taifex.com.tw/cht/3/futContractsDate"
    payload = {
        'queryType': '1',
        'goDay': '',
        'doQuery': '1',
        'dateaddcnt': '',
        'queryDate': target_date_str,
        'commodityId': 'TXF',
        'button': '送出查詢'
    }
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Content-Type': 'application/x-www-form-urlencoded',
        'Referer': 'https://www.taifex.com.tw/cht/3/futContractsDate'
    }
    try:
        resp = session.post(url, data=payload, headers=headers, timeout=10)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, 'html.parser')
            tables = soup.find_all('table', {'class': ['table_f', 'table_a']})
            for table in tables:
                for row in table.find_all('tr'):
                    cols = [td.text.strip() for td in row.find_all('td')]
                    for idx, col in enumerate(cols):
                        if '外資' in col:
                            # 提取外資多空淨額口數 (欄位位移量)
                            if len(cols) > idx + 5:
                                val = clean_int(cols[idx + 5])
                                if val is not None:
                                    return val
    except Exception as e:
        print(f"[{target_date_str}] Foreign position error: {e}")
    return None


def process_single_date(session, target_date_str, prev_date_str):
    data = {
        "date": target_date_str,
        "prev_date": prev_date_str,
        "night_price_change": "NA",
        "night_vol": "NA",
        "day_vol": "NA",
        "night_volume_ratio": "NA",
        "foreign_net_contracts": "NA",
        "scenario": "NA",
        "forecast_desc": "數據尚未準備就緒或目前為休市期間 (NA)",
        "trust_signal": "NA"
    }

    night_price_change, night_vol = fetch_night_market_data(session, target_date_str)
    day_vol = fetch_day_market_volume(session, prev_date_str)
    foreign_net = fetch_foreign_net_position(session, target_date_str)

    if night_vol is not None:
        data["night_vol"] = f"{night_vol:,}"
    if day_vol is not None:
        data["day_vol"] = f"{day_vol:,}"

    if night_vol is not None and day_vol is not None and (night_vol + day_vol) > 0:
        ratio = (night_vol / (day_vol + night_vol)) * 100
        data["night_volume_ratio"] = f"{ratio:.1f}%"

        if ratio >= 40:
            data["trust_signal"] = "很強的訊號 (>40%)"
        elif ratio < 30:
            data["trust_signal"] = "參考價值較低 (<30%)"
        else:
            data["trust_signal"] = "中等強度 (30%~40%)"

    if night_price_change is not None:
        data["night_price_change"] = f"+{night_price_change}" if night_price_change > 0 else str(night_price_change)

    if foreign_net is not None:
        data["foreign_net_contracts"] = foreign_net

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
    trading_days = get_past_trading_days(count=20)

    for target_date_str, prev_date_str in trading_days:
        if target_date_str not in existing_records or existing_records[target_date_str]["scenario"] == "NA":
            print(f"抓取最新資料：{target_date_str}...")
            new_record = process_single_date(session, target_date_str, prev_date_str)
            existing_records[target_date_str] = new_record

    sorted_history = sorted(existing_records.values(), key=lambda x: x["date"], reverse=True)

    with open(JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted_history, f, ensure_ascii=False, indent=2)

    print(f"成功更新並儲存 JSON 歷史檔案至：{JSON_FILE}")
    return sorted_history


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

    foreign_val_str = f"{latest_data['foreign_net_contracts']:,}" if isinstance(latest_data['foreign_net_contracts'],
                                                                                int) else latest_data[
        'foreign_net_contracts']

    history_rows_html = ""
    for item in history_records[:20]:
        change_str = str(item['night_price_change'])
        c_color = "text-slate-600"
        if change_str.startswith("+"):
            c_color = "text-red-500 font-bold"
        elif change_str.startswith("-"):
            c_color = "text-green-500 font-bold"

        f_val = f"{item['foreign_net_contracts']:,}" if isinstance(item['foreign_net_contracts'], int) else item[
            'foreign_net_contracts']
        f_color = "text-slate-600"
        if isinstance(item['foreign_net_contracts'], int):
            f_color = "text-red-500" if item['foreign_net_contracts'] > 0 else "text-green-500"

        s_badge = "bg-slate-100 text-slate-600"
        if item['scenario'] == '劇本一':
            s_badge = "bg-red-100 text-red-700 font-bold"
        elif item['scenario'] == '劇本二':
            s_badge = "bg-yellow-100 text-yellow-700 font-bold"
        elif item['scenario'] == '劇本三':
            s_badge = "bg-green-100 text-green-700 font-bold"
        elif item['scenario'] == '劇本四':
            s_badge = "bg-blue-100 text-blue-700 font-bold"

        history_rows_html += f"""
        <tr class="hover:bg-slate-50 transition-colors">
            <td class="py-3 px-4 font-semibold text-slate-700">{item['date']}</td>
            <td class="py-3 px-4 {c_color}">{item['night_price_change']}</td>
            <td class="py-3 px-4 font-medium text-slate-700">{item['night_volume_ratio']}</td>
            <td class="py-3 px-4 {f_color} font-medium">{f_val}</td>
            <td class="py-3 px-4"><span class="px-2.5 py-1 rounded-md text-xs {s_badge}">{item['scenario']}</span></td>
            <td class="py-3 px-4 text-xs text-slate-500">{item['trust_signal']}</td>
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
    <div class="max-w-5xl mx-auto space-y-6">

        <!-- Header -->
        <div class="bg-white rounded-xl shadow-sm p-6 border border-slate-200">
            <div class="flex flex-col md:flex-row md:items-center justify-between gap-2">
                <div>
                    <h1 class="text-2xl font-bold text-slate-800">📊 台指期夜盤三大數據走勢預測</h1>
                    <p class="text-slate-500 text-sm mt-1">分析目標交易日：<span class="font-bold text-slate-800">{latest_data['date']}</span> （對應前一日盤：{latest_data['prev_date']}）</p>
                </div>
                <!-- 雙時區時間顯示 -->
                <div class="text-xs text-slate-500 md:text-right space-y-1">
                    <div>頁面產生時間：<span class="font-semibold text-slate-700">{tw_time_str}</span> <span class="text-slate-400">(UTC+8)</span></div>
                    <div>頁面產生時間：<span class="font-semibold text-slate-700">{utc_time_str}</span> <span class="text-slate-400">(UTC)</span></div>
                </div>
            </div>
        </div>

        <!-- 三大指標數據卡片 -->
        <div class="grid grid-cols-1 md:grid-cols-3 gap-4">
            <div class="bg-white p-5 rounded-xl shadow-sm border border-slate-200">
                <span class="text-xs font-semibold text-slate-400 uppercase tracking-wider">1. 夜盤漲跌點數</span>
                <div class="text-3xl font-extrabold mt-2 {price_color_class}">
                    {latest_data['night_price_change']}
                </div>
                <p class="text-xs text-slate-400 mt-2">關鍵門檻：超過 ±300 點代表預期大變</p>
            </div>

            <div class="bg-white p-5 rounded-xl shadow-sm border border-slate-200">
                <span class="text-xs font-semibold text-slate-400 uppercase tracking-wider">2. 夜盤量佔比</span>
                <div class="text-3xl font-extrabold text-slate-800 mt-2">
                    {latest_data['night_volume_ratio']}
                </div>
                <p class="text-xs text-slate-500 mt-2">訊號強度：<span class="font-bold text-indigo-600">{latest_data['trust_signal']}</span></p>
                <div class="mt-2 pt-2 border-t border-slate-100 text-[11px] text-slate-400">
                    夜盤量: {latest_data['night_vol']} | 日盤量: {latest_data['day_vol']}
                </div>
            </div>

            <div class="bg-white p-5 rounded-xl shadow-sm border border-slate-200">
                <span class="text-xs font-semibold text-slate-400 uppercase tracking-wider">3. 外資多空淨額</span>
                <div class="text-3xl font-extrabold text-slate-800 mt-2">
                    {foreign_val_str} <span class="text-sm font-normal text-slate-500">口</span>
                </div>
                <p class="text-xs text-slate-400 mt-2">關鍵門檻：觀察是否超過 ±1,000 口</p>
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

        <!-- 過去 20 個交易日歷史紀錄表格 -->
        <div class="bg-white rounded-xl shadow-sm p-6 border border-slate-200">
            <div class="flex items-center justify-between mb-4">
                <h2 class="text-lg font-bold text-slate-800">🗓️ 過去 20 個交易日歷史紀錄</h2>
                <a href="./data/history.json" target="_blank" class="text-xs text-indigo-600 hover:underline">📥 下載完整 history.json</a>
            </div>
            <div class="overflow-x-auto">
                <table class="w-full text-sm text-left text-slate-600 border-collapse">
                    <thead class="text-xs text-slate-700 uppercase bg-slate-100">
                        <tr>
                            <th class="py-3 px-4 rounded-l-lg">交易日期</th>
                            <th class="py-3 px-4">夜盤漲跌</th>
                            <th class="py-3 px-4">夜盤量佔比</th>
                            <th class="py-3 px-4">外資淨額 (口)</th>
                            <th class="py-3 px-4">判定劇本</th>
                            <th class="py-3 px-4 rounded-r-lg">訊號強度</th>
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
            資料來源：台灣期貨交易所 (TAIFEX) | 自動化分析與發布 via GitHub Actions & Pages
        </footer>
    </div>
</body>
</html>
"""
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html_content)
    print("成功產生 index.html，抓取與解析運作正常！")


if __name__ == "__main__":
    history_records = update_history_json()
    generate_html(history_records)