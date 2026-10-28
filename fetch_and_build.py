import os
import json
import requests
from datetime import datetime, timezone, timedelta
from modules.utils import clean_int, get_past_trading_days, DATA_DIR, JSON_FILE, BROKER_JSON_FILE
from modules.taifex_scraper import (
    find_night_session,
    is_taifex_trading_day,
    fetch_day_market_volume,
    fetch_institutional_positions_ah,
    fetch_institutional_positions_full
)
from modules.twse_scraper import fetch_twse_intraday_taiex, verify_match
from modules.market_scraper import fetch_us_indices, get_us_info_for_date, fetch_ohlc_3m
from modules.broker_scraper import update_broker_history_json
from modules.html_generator import generate_index_html, generate_broker_html
from modules.themes_scraper import update_themes_json

# 資料結構版本：變更判讀邏輯時遞增，強制重新計算歷史紀錄
DATA_VERSION = "v13"


def _cached_int(prev_rec, key):
    """ 讀取舊紀錄中已成功抓到的數值（"+774"、"27,131"、684 等），沒有則回傳 None """
    if not prev_rec:
        return None
    val = prev_rec.get(key)
    if isinstance(val, int):
        return val
    if isinstance(val, str) and val != "NA":
        return clean_int(val)
    return None


def process_single_date(session, target_date_str, prev_date_str, us_data_by_date, today_str, prev_rec=None):
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
        "verify_badge_class": "bg-slate-100 text-slate-500",
        "version": DATA_VERSION
    }

    # 夜盤：找出 prev_date_str 15:00 開盤的夜盤。期交所遇休市會把夜盤歸到之後的營業日，
    # 因此以夜盤實際開盤日比對，而非直接用 target_date_str 查詢（否則連假後會拿到數天前的夜盤）
    night = find_night_session(session, target_date_str, prev_date_str, today_str)
    night_price_change = night["price_change"] if night else None
    night_vol = night["volume"] if night else None
    day_vol = fetch_day_market_volume(session, prev_date_str)

    # 夜盤三大法人與夜盤行情同屬期交所交易日 night["trade_date"]
    ah_pos = fetch_institutional_positions_ah(session, night["trade_date"]) if night else {}
    full_pos = fetch_institutional_positions_full(session, target_date_str)

    # 重新計算（例如等待收盤驗證）時若期交所暫時連不上，沿用之前已成功抓到的數值，
    # 避免整筆紀錄從空白重建而把已抓到的夜盤資料蓋成 NA。已結束的交易時段數值不會再變動。
    if prev_rec and prev_rec.get("prev_date") == prev_date_str and prev_rec.get("version") == DATA_VERSION:
        if night_price_change is None:
            night_price_change = _cached_int(prev_rec, "night_price_change")
        if night_vol is None:
            night_vol = _cached_int(prev_rec, "night_vol")
        if day_vol is None:
            day_vol = _cached_int(prev_rec, "day_vol")
        ah_pos = dict(ah_pos)
        full_pos = dict(full_pos)
        for k in ("foreign", "trust", "dealer"):
            if ah_pos.get(k) is None:
                ah_pos[k] = _cached_int(prev_rec, f"{k}_net_ah")
            if full_pos.get(k) is None:
                full_pos[k] = _cached_int(prev_rec, f"{k}_net_full")

    # 已過去的日期若期交所查無日盤行情 → 休市
    is_past = target_date_str < today_str
    target_closed = is_past and not is_taifex_trading_day(session, target_date_str)
    prev_closed = night_price_change is None and not is_taifex_trading_day(session, prev_date_str)
    if prev_closed:
        data["forecast_desc"] = f"前一日 ({prev_date_str}) 休市，沒有夜盤交易，無法預測。"

    
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

    if target_closed:
        data["actual_change"] = "NA"
        data["sparkline_svg"] = '<span class="text-xs text-slate-400">休市</span>'
        data["verify_status"] = "休市 (無日盤可驗證)"
        data["verify_badge_class"] = "bg-slate-100 text-slate-500"
        return data

    data["actual_change"] = actual_info["change_str"]
    data["sparkline_svg"] = actual_info["sparkline_svg"]
    
    v_status, v_badge = verify_match(data["scenario"], actual_info)
    data["verify_status"] = v_status
    data["verify_badge_class"] = v_badge

    # 前一日休市沒有夜盤：當日收盤後即為最終狀態，不需再重抓
    if prev_closed and data["scenario"] == "NA" and actual_info.get("close") is not None:
        data["verify_status"] = "無夜盤 (前日休市)"
        data["verify_badge_class"] = "bg-slate-100 text-slate-500"

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
    today_str = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y/%m/%d")

    for target_date_str, prev_date_str in trading_days:
        rec = existing_records.get(target_date_str)
        
        # 版本不符、尚未驗證或尚無劇本的紀錄重新計算（休市紀錄為最終狀態）
        needs_update = (
            rec is None or 
            rec.get("verify_status", "尚未驗證") == "尚未驗證" or 
            (rec.get("scenario") == "NA" and not rec.get("verify_status", "").startswith(("休市", "無夜盤"))) or
            rec.get("version") != DATA_VERSION
        )
        
        if needs_update:
            print(f"抓取與分析資料 (TAIFEX {DATA_VERSION})：{target_date_str}...")
            new_record = process_single_date(session, target_date_str, prev_date_str, us_data_by_date, today_str, prev_rec=rec)
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
    
    print("正在抓取 FinLab 族群名單與上市櫃收盤行情...")
    try:
        update_themes_json(session)
    except Exception as e:
        print(f"族群漲跌更新失敗: {e}")

    generate_index_html(history_records, tw_kline_data, us_kline_data)
    generate_broker_html(broker_records)
    print("所有頁面與 TAIFEX 官方對齊已順利執行完成！")
