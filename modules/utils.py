import os
import re
from datetime import datetime, timedelta

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(MODULE_DIR)
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
JSON_FILE = os.path.join(DATA_DIR, "history.json")
BROKER_JSON_FILE = os.path.join(DATA_DIR, "broker_history.json")


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