from .utils import clean_float, DATA_SOURCES
from .chart_generator import generate_svg_sparkline


def fetch_twse_intraday_taiex(session, target_date_str):
    """
    從台灣證券交易所 (TWSE) 抓取目標日期的台股加權指數 5 秒逐筆盤中走勢資料。
    """
    formatted_date = target_date_str.replace('/', '')
    base_url = DATA_SOURCES["twse"]["intraday_taiex_url"]
    url = f"{base_url}?response=json&date={formatted_date}"
    headers = {'User-Agent': 'Mozilla/5.0'}

    try:
        resp = session.get(url, headers=headers, timeout=5)
        if resp.status_code == 200:
            res_json = resp.json()
            if res_json.get('stat') == 'OK' and 'data' in res_json:
                prices = []
                prev_close = None
                for row in res_json['data']:
                    # 09:00:00 那筆是開盤前、尚無成交時的指數 = 前一日收盤，不是開盤價；
                    # 不能算進盤中走勢（11/6 開低反彈因此誤判走勢分歧），但正好拿來當昨收算當日漲跌
                    if str(row[0]).strip() == "09:00:00":
                        prev_close = clean_float(row[1])
                        continue
                    val = clean_float(row[1])
                    if val is not None:
                        prices.append(val)

                if prices:
                    open_p, close_p = prices[0], prices[-1]
                    high_p, low_p = max(prices), min(prices)
                    sampled = prices[::60] if len(prices) > 60 else list(prices)
                    if (len(prices) - 1) % 60:          # 每 60 筆取一筆會漏掉 13:30 收盤那筆，補上（不然走勢圖的「收」是 13:25 的值）
                        sampled.append(close_p)
                    sparkline_svg = generate_svg_sparkline(sampled)
                    # 顯示的點數 = 收盤 − 昨收（跟證交所公布的漲跌一致）。
                    # 2026-11-25 前顯示的是收盤 − 開盤第一筆：11/24 開盤跳空大漲 2,337 點，畫面只剩 +973 點，看起來像漲很少
                    day_change = (close_p - prev_close) if prev_close else None
                    intraday = close_p - open_p          # 開盤後的漲跌：劇本驗證（verify_match）看的是這個
                    return {
                        "open": open_p,
                        "close": close_p,
                        "high": high_p,
                        "low": low_p,
                        "prev_close": prev_close,
                        "change_str": _pts(day_change) if day_change is not None else "NA",
                        "intraday_str": _pts(intraday),
                        "sparkline_svg": sparkline_svg,
                        "prices": prices
                    }
    except Exception as e:
        print(f"[{target_date_str}] TWSE TAIEX fetch error: {e}")

    return {
        "open": None,
        "close": None,
        "high": None,
        "low": None,
        "prev_close": None,
        "change_str": "NA",
        "intraday_str": "NA",
        "sparkline_svg": '<span class="text-xs text-slate-400">未收盤</span>',
        "prices": []
    }


def _pts(x):
    r = int(round(x))          # int 沒有 −0，避免 −0.4 顯示成「-0點」
    return f"+{r}點" if r > 0 else f"{r}點"


def verify_match(scenario, actual_info):
    """
    依據當日實際開高低收點位及盤中時間權重，檢驗是否符合預測劇本。
    已優化「留長上影線衝高回落（劇本二）」與「留長下影線探底拉升（劇本四）」之判定。
    """
    open_p, close_p = actual_info.get("open"), actual_info.get("close")
    high_p, low_p = actual_info.get("high"), actual_info.get("low")
    prices = actual_info.get("prices", [])

    if not scenario or scenario == "NA" or None in [open_p, close_p, high_p, low_p] or len(prices) < 2:
        return "尚未驗證", "bg-slate-100 text-slate-500"

    total_pts = len(prices)
    idx_high, idx_low = prices.index(high_p), prices.index(low_p)
    high_time_ratio, low_time_ratio = idx_high / total_pts, idx_low / total_pts
    total_range = high_p - low_p if high_p != low_p else 1.0

    # 收盤價在當天全日高低振幅中的相對位置 (0.0: 收在最低, 1.0: 收在最高)
    close_position = (close_p - low_p) / total_range
    change = close_p - open_p

    # --- 劇本一：開高走高 / 漲勢紮實 ---
    if scenario == "劇本一":
        if change > 0 and close_position >= 0.4:
            return "✅ 符合 (一路走高)", "bg-red-100 text-red-700 font-bold"
        elif change > 0:
            return "✅ 符合 (收紅上漲)", "bg-red-50 text-red-600"
        return "❌ 走勢分歧", "bg-slate-100 text-slate-600"

    # --- 劇本二：力道不足 / 開高走低 / 衝高回落 ---
    elif scenario == "劇本二":
        # 情況 A: 標準收黑開高走低
        if change < 0 and high_time_ratio <= 0.6:
            return "✅ 符合 (開高走低)", "bg-green-100 text-green-700 font-bold"
        # 情況 B: 盤中衝高後大幅殺回，收在當日振幅中下緣 (如 9/9 假突破長上影線盤型)
        elif high_time_ratio <= 0.6 and close_position <= 0.5:
            return "✅ 符合 (衝高回落)", "bg-yellow-100 text-yellow-800 font-bold"
        elif change < 0:
            return "✅ 符合 (收黑走低)", "bg-green-50 text-green-600"
        return "❌ 走勢分歧", "bg-slate-100 text-slate-600"

    # --- 劇本三：跌勢延續 ---
    elif scenario == "劇本三":
        if change < 0 and close_position <= 0.6:
            return "✅ 符合 (跌勢延續)", "bg-green-100 text-green-700 font-bold"
        elif change < 0:
            return "✅ 符合 (收黑下跌)", "bg-green-50 text-green-600"
        return "❌ 走勢分歧", "bg-slate-100 text-slate-600"

    # --- 劇本四：開低反彈 / 探底拉升 ---
    elif scenario == "劇本四":
        # 情況 A: 標準收紅開低反彈
        if change > 0 and low_time_ratio <= 0.6:
            return "✅ 符合 (開低反彈)", "bg-red-100 text-red-700 font-bold"
        # 情況 B: 早盤下殺探底後大幅拉回，收在當日振幅中上緣
        elif low_time_ratio <= 0.6 and close_position >= 0.5:
            return "✅ 符合 (探底回升)", "bg-blue-100 text-blue-800 font-bold"
        elif change > 0:
            return "✅ 符合 (收紅反彈)", "bg-red-50 text-red-600"
        return "❌ 走勢分歧", "bg-slate-100 text-slate-600"

    return "NA", "bg-slate-100 text-slate-500"