import os
import re
import json
from datetime import datetime, timezone, timedelta

# 專案絕對路徑設定
MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(MODULE_DIR)
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
JSON_FILE = os.path.join(DATA_DIR, "history.json")
BROKER_JSON_FILE = os.path.join(DATA_DIR, "broker_history.json")
SOURCES_JSON_FILE = os.path.join(DATA_DIR, "sources.json")

def load_data_sources():
    """ 載入 data/sources.json 資料來源網址設定檔 (帶防呆備援) """
    if os.path.exists(SOURCES_JSON_FILE):
        try:
            with open(SOURCES_JSON_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading sources.json: {e}")
            
    return {
        "taifex": {
            "daily_market_report_url": "https://www.taifex.com.tw/cht/3/futDailyMarketReport",
            "institutional_positions_ah_url": "https://www.taifex.com.tw/cht/3/futContractsDateAh",
            "institutional_positions_full_url": "https://www.taifex.com.tw/cht/3/futContractsDate"
        },
        "twse": {
            "intraday_taiex_url": "https://www.twse.com.tw/exchangeReport/MI_5MINS_INDEX"
        },
        "yahoo_finance": {
            "chart_api_url": "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        },
        "stooq": {
            "daily_csv_url": "https://stooq.com/q/d/l/"
        },
        "moneydj": {
            "broker_detail_url": "https://fubon-ebrokerdj.fbs.com.tw/z/zg/zgb/zgb0.djhtm"
        }
    }

DATA_SOURCES = load_data_sources()

def get_past_trading_days(count=20):
    """
    計算期貨預測專用交易日對 (target_date_str, prev_date_str)
    期交所 (TAIFEX) 官方帳務日規則：
    週五夜盤在期交所系統歸屬於下週一的交易日 (例如 2026/08/31)。
    """
    utc_now = datetime.now(timezone.utc)
    tw_now = utc_now + timedelta(hours=8)
    curr = tw_now

    if curr.weekday() == 5:   # Saturday -> Monday (+2 天)
        curr = curr + timedelta(days=2)
    elif curr.weekday() == 6: # Sunday -> Monday (+1 天)
        curr = curr + timedelta(days=1)

    trading_days = []
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

def get_broker_trading_days(count=20):
    """
    券商買超專用交易日計算：
    台灣時間中午 12:00 前自動回推至前一交易日。
    """
    utc_now = datetime.now(timezone.utc)
    tw_now = utc_now + timedelta(hours=8)
    
    if tw_now.hour < 12:
        curr = tw_now - timedelta(days=1)
    else:
        curr = tw_now

    trading_days = []
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

def extract_stock_code(stock_text):
    text = stock_text.replace('\xa0', '').strip()
    match = re.match(r'^([0-9A-Z]{4,6})', text)
    if match:
        return match.group(1)
    return ""

def is_etf(stock_text):
    text = stock_text.replace('\xa0', '').strip()
    code = extract_stock_code(text)
    if code.startswith('00'):
        return True
    if any(kw in text for kw in ['ETF', '正2', '反1', '主動', '高息', '美債', '半導體']):
        return True
    return False

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
