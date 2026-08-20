from .utils import clean_float
from .chart_generator import generate_svg_sparkline


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
                    return {"open": open_p, "close": close_p, "high": high_p, "low": low_p, "change_str": change_str,
                            "sparkline_svg": sparkline_svg, "prices": prices}
    except Exception as e:
        print(f"[{target_date_str}] TWSE TAIEX fetch error: {e}")

    return {"open": None, "close": None, "high": None, "low": None, "change_str": "NA",
            "sparkline_svg": '<span class="text-xs text-slate-400">未收盤</span>', "prices": []}


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
        return ("✅ 符合 (一路走高)", "bg-red-100 text-red-700 font-bold") if (
                    change > 0 and close_position >= 0.4) else (
            ("✅ 符合 (收紅上漲)", "bg-red-50 text-red-600") if change > 0 else ("❌ 走勢分歧",
                                                                                "bg-slate-100 text-slate-600"))
    elif scenario == "劇本二":
        return ("✅ 符合 (開高走低)", "bg-green-100 text-green-700 font-bold") if (
                    change < 0 and high_time_ratio <= 0.6) else (
            ("✅ 符合 (收黑走低)", "bg-green-50 text-green-600") if change < 0 else ("❌ 走勢分歧",
                                                                                    "bg-slate-100 text-slate-600"))
    elif scenario == "劇本三":
        return ("✅ 符合 (跌勢延續)", "bg-green-100 text-green-700 font-bold") if (
                    change < 0 and close_position <= 0.6) else (
            ("✅ 符合 (收黑下跌)", "bg-green-50 text-green-600") if change < 0 else ("❌ 走勢分歧",
                                                                                    "bg-slate-100 text-slate-600"))
    elif scenario == "劇本四":
        return ("✅ 符合 (開低反彈)", "bg-red-100 text-red-700 font-bold") if (
                    change > 0 and low_time_ratio <= 0.6) else (
            ("✅ 符合 (收紅反彈)", "bg-red-50 text-red-600") if change > 0 else ("❌ 走勢分歧",
                                                                                "bg-slate-100 text-slate-600"))

    return "NA", "bg-slate-100 text-slate-500"