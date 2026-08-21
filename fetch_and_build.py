import os
import json
import requests
from modules.utils import get_past_trading_days, DATA_DIR, JSON_FILE, BROKER_JSON_FILE
from modules.taifex_scraper import (
    fetch_night_market_data,
    fetch_day_market_volume,
    fetch_institutional_positions_ah,
    fetch_institutional_positions_full
)
from modules.twse_scraper import fetch_twse_intraday_taiex, verify_match
from modules.market_scraper import fetch_us_indices, get_us_info_for_date, fetch_ohlc_3m
from modules.broker_scraper import update_broker_history_json
from modules.html_generator import generate_index_html, generate_broker_html


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


if __name__ == "__main__":
    history_records, session, trading_days = update_history_json()

    print("正在抓取與更新 7 大主力券商買超歷史紀錄...")
    broker_records = update_broker_history_json(session, trading_days)

    print("正在抓取近 3 個月台股與美股 K 線 OHLC 數據...")
    tw_kline_data = fetch_ohlc_3m(session, "^TWII")
    us_kline_data = fetch_ohlc_3m(session, "^IXIC")

    generate_index_html(history_records, tw_kline_data, us_kline_data)
    generate_broker_html(broker_records)
    print("所有頁面 (index.html, broker.html) 與模組已順利執行完成！")