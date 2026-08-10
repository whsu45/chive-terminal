import os
import re
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta


def get_trading_dates():
    """
    計算預測目標交易日 (Target Trading Day) 與 前一交易日 (Previous Trading Day)
    - 若為週一：目標日為週一，前一交易日為週五。
    - 若為週六/週日：目標日自動推算至下一週一。
    """
    now = datetime.now()

    if now.weekday() == 5:  # 星期六 -> 目標日改為下週一
        target_date = now + timedelta(days=2)
    elif now.weekday() == 6:  # 星期日 -> 目標日改為下週一
        target_date = now + timedelta(days=1)
    else:
        target_date = now

    # 計算前一交易日
    if target_date.weekday() == 0:  # 週一的前一交易日是週五
        prev_date = target_date - timedelta(days=3)
    else:
        prev_date = target_date - timedelta(days=1)

    return target_date.strftime("%Y/%m/%d"), prev_date.strftime("%Y/%m/%d")


def clean_int(text):
    """ 清理字串並轉為整數 """
    if not text:
        return None
    cleaned = re.sub(r'[^\d\+\-\.]', '', text.strip())
    try:
        return int(float(cleaned))
    except ValueError:
        return None


def fetch_night_market_data(target_date_str):
    """
    抓取期交所夜盤行情 (盤後交易時段)
    回傳近月台指期的 (漲跌點數, 夜盤成交量)
    """
    url = "https://www.taifex.com.tw/cht/3/futDailyMarketReport"
    payload = {
        'queryType': '2',
        'marketCode': '1',  # 盤後交易時段
        'date': target_date_str,
        'CommodityID': 'TX'
    }
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }

    try:
        resp = requests.post(url, data=payload, headers=headers, timeout=10)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, 'html.parser')
            table = soup.find('table', {'class': 'table_f'})
            if table:
                rows = table.find_all('tr')
                for row in rows:
                    cols = [td.text.strip() for td in row.find_all('td')]
                    if len(cols) >= 10 and cols[0] == 'TX':
                        price_change = clean_int(cols[6])
                        volume = clean_int(cols[8])
                        return price_change, volume
    except Exception as e:
        print(f"Fetch Night Market Data Error: {e}")
    return None, None


def fetch_day_market_volume(prev_date_str):
    """
    抓取前一交易日日盤成交量 (一般交易時段)
    """
    url = "https://www.taifex.com.tw/cht/3/futDailyMarketReport"
    payload = {
        'queryType': '2',
        'marketCode': '0',  # 一般交易時段
        'date': prev_date_str,
        'CommodityID': 'TX'
    }
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }

    try:
        resp = requests.post(url, data=payload, headers=headers, timeout=10)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, 'html.parser')
            table = soup.find('table', {'class': 'table_f'})
            if table:
                rows = table.find_all('tr')
                for row in rows:
                    cols = [td.text.strip() for td in row.find_all('td')]
                    if len(cols) >= 10 and cols[0] == 'TX':
                        volume = clean_int(cols[8])
                        return volume
    except Exception as e:
        print(f"Fetch Day Market Volume Error: {e}")
    return None


def fetch_foreign_net_position(target_date_str):
    """
    抓取外資台指期夜盤多空淨額 (口數)
    """
    url = "https://www.taifex.com.tw/cht/3/futContractsDate"
    payload = {
        'queryDate': target_date_str,
        'commodityId': 'TX'
    }
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }

    try:
        resp = requests.post(url, data=payload, headers=headers, timeout=10)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, 'html.parser')
            tables = soup.find_all('table', {'class': 'table_f'})
            for table in tables:
                rows = table.find_all('tr')
                for row in rows:
                    text = row.text
                    if '外資' in text:
                        cols = [td.text.strip() for td in row.find_all('td')]
                        for col in cols:
                            val = clean_int(col)
                            if val is not None:
                                return val
    except Exception as e:
        print(f"Fetch Foreign Net Position Error: {e}")
    return None


def process_data():
    target_date_str, prev_date_str = get_trading_dates()

    data = {
        "date": target_date_str,
        "prev_date": prev_date_str,
        "night_price_change": "NA",
        "night_vol": "NA",
        "day_vol": "NA",
        "night_volume_ratio": "NA",
        "foreign_net_contracts": "NA",
        "scenario": "NA",
        "forecast_desc": "最新數據尚未準備就緒或目前為休市期間 (NA)",
        "trust_signal": "NA"
    }

    # 1. 抓取夜盤行情 (漲跌 & 夜盤量)
    night_price_change, night_vol = fetch_night_market_data(target_date_str)

    # 2. 抓取前一日日盤量
    day_vol = fetch_day_market_volume(prev_date_str)

    # 3. 抓取外資多空淨額
    foreign_net = fetch_foreign_net_position(target_date_str)

    if night_vol is not None:
        data["night_vol"] = f"{night_vol:,}"
    if day_vol is not None:
        data["day_vol"] = f"{day_vol:,}"

    # 4. 計算夜盤量佔比與可信度訊號
    if night_vol is not None and day_vol is not None and (night_vol + day_vol) > 0:
        ratio = (night_vol / (day_vol + night_vol)) * 100
        data["night_volume_ratio"] = f"{ratio:.1f}%"

        if ratio >= 40:
            data["trust_signal"] = "很強的訊號 (>40%)"
        elif ratio < 30:
            data["trust_signal"] = "參考價值較低 (<30%)"
        else:
            data["trust_signal"] = "中等強度 (30%~40%)"

    # 5. 格式化漲跌與外資口數
    if night_price_change is not None:
        data["night_price_change"] = f"+{night_price_change}" if night_price_change > 0 else str(night_price_change)

    if foreign_net is not None:
        data["foreign_net_contracts"] = foreign_net

    # 6. 比對四種實戰劇本
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


def generate_html(data):
    """ 生成完整的 index.html 頁面 """

    scenario_badge_class = "bg-slate-100 text-slate-700 border-slate-200"
    if data["scenario"] == "劇本一":
        scenario_badge_class = "bg-red-50 text-red-700 border-red-200"
    elif data["scenario"] == "劇本二":
        scenario_badge_class = "bg-yellow-50 text-yellow-700 border-yellow-200"
    elif data["scenario"] == "劇本三":
        scenario_badge_class = "bg-green-50 text-green-700 border-green-200"
    elif data["scenario"] == "劇本四":
        scenario_badge_class = "bg-blue-50 text-blue-700 border-blue-200"

    price_color_class = "text-slate-400"
    if str(data["night_price_change"]).startswith("+"):
        price_color_class = "text-red-500"
    elif str(data["night_price_change"]).startswith("-"):
        price_color_class = "text-green-500"

    foreign_val_str = f"{data['foreign_net_contracts']:,}" if isinstance(data['foreign_net_contracts'], int) else data[
        'foreign_net_contracts']

    html_content = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>台指期每日開盤走勢預測儀表板</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-slate-50 min-h-screen p-4 md:p-8 font-sans">
    <div class="max-w-4xl mx-auto space-y-6">

        <!-- Header -->
        <div class="bg-white rounded-xl shadow-sm p-6 border border-slate-200">
            <div class="flex flex-col md:flex-row md:items-center justify-between gap-2">
                <div>
                    <h1 class="text-2xl font-bold text-slate-800">📊 台指期夜盤三大數據走勢預測</h1>
                    <p class="text-slate-500 text-sm mt-1">分析目標交易日：<span class="font-bold text-slate-800">{data['date']}</span> （對應前一日盤：{data['prev_date']}）</p>
                </div>
                <div class="text-xs text-slate-400">
                    頁面產生時間：{datetime.now().strftime('%Y/%m/%d %H:%M:%S')} (CST)
                </div>
            </div>
        </div>

        <!-- 三大指標數據卡片 -->
        <div class="grid grid-cols-1 md:grid-cols-3 gap-4">
            <!-- 指標 1: 夜盤漲跌 -->
            <div class="bg-white p-5 rounded-xl shadow-sm border border-slate-200">
                <span class="text-xs font-semibold text-slate-400 uppercase tracking-wider">1. 夜盤漲跌點數</span>
                <div class="text-3xl font-extrabold mt-2 {price_color_class}">
                    {data['night_price_change']}
                </div>
                <p class="text-xs text-slate-400 mt-2">關鍵門檻：超過 ±300 點代表預期大變</p>
            </div>

            <!-- 指標 2: 夜盤量佔比 -->
            <div class="bg-white p-5 rounded-xl shadow-sm border border-slate-200">
                <span class="text-xs font-semibold text-slate-400 uppercase tracking-wider">2. 夜盤量佔比</span>
                <div class="text-3xl font-extrabold text-slate-800 mt-2">
                    {data['night_volume_ratio']}
                </div>
                <p class="text-xs text-slate-500 mt-2">訊號強度：<span class="font-bold text-indigo-600">{data['trust_signal']}</span></p>
                <div class="mt-2 pt-2 border-t border-slate-100 text-[11px] text-slate-400">
                    夜盤量: {data['night_vol']} | 日盤量: {data['day_vol']}
                </div>
            </div>

            <!-- 指標 3: 外資多空淨額 -->
            <div class="bg-white p-5 rounded-xl shadow-sm border border-slate-200">
                <span class="text-xs font-semibold text-slate-400 uppercase tracking-wider">3. 外資多空淨額</span>
                <div class="text-3xl font-extrabold text-slate-800 mt-2">
                    {foreign_val_str} <span class="text-sm font-normal text-slate-500">口</span>
                </div>
                <p class="text-xs text-slate-400 mt-2">關鍵門檻：觀察是否超過 ±1,000 口</p>
            </div>
        </div>

        <!-- 劇本判讀與結果卡片 -->
        <div class="bg-white rounded-xl shadow-sm p-6 border border-slate-200">
            <h2 class="text-lg font-bold text-slate-800 mb-4">🔮 今日開盤預測劇本</h2>

            <div class="p-5 rounded-xl border-2 {scenario_badge_class} mb-6">
                <div class="flex items-center space-x-2">
                    <span class="font-bold text-xl">{data['scenario']}</span>
                </div>
                <p class="mt-2 text-base font-semibold">{data['forecast_desc']}</p>
            </div>

            <!-- 四種劇本對照表 -->
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
                            <tr class="hover:bg-slate-50 {'bg-red-50/60 font-medium' if data['scenario'] == '劇本一' else ''}">
                                <td class="py-3 px-4 font-bold text-red-600">劇本一</td>
                                <td class="py-3 px-4 text-red-500">上漲</td>
                                <td class="py-3 px-4 text-red-500">正數（看多）</td>
                                <td class="py-3 px-4">漲勢紮實，開盤後容易持續往上走。</td>
                            </tr>
                            <tr class="hover:bg-slate-50 {'bg-yellow-50/60 font-medium' if data['scenario'] == '劇本二' else ''}">
                                <td class="py-3 px-4 font-bold text-yellow-600">劇本二</td>
                                <td class="py-3 px-4 text-red-500">上漲</td>
                                <td class="py-3 px-4 text-green-500">負數（看空）</td>
                                <td class="py-3 px-4">力道不足，容易出現開高走低，隨後殺下來。</td>
                            </tr>
                            <tr class="hover:bg-slate-50 {'bg-green-50/60 font-medium' if data['scenario'] == '劇本三' else ''}">
                                <td class="py-3 px-4 font-bold text-green-600">劇本三</td>
                                <td class="py-3 px-4 text-green-500">下跌</td>
                                <td class="py-3 px-4 text-green-500">負數（看空）</td>
                                <td class="py-3 px-4">跌勢延續，不建議貿然抄底，因為可能會持續下跌。</td>
                            </tr>
                            <tr class="hover:bg-slate-50 {'bg-blue-50/60 font-medium' if data['scenario'] == '劇本四' else ''}">
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
    print("成功產生 index.html，所有資料均已寫入靜態頁面！")


if __name__ == "__main__":
    market_data = process_data()
    generate_html(market_data)