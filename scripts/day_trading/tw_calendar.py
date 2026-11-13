"""交易日推算：以 TWSE 每日收盤行情 (MI_INDEX) 是否有資料判定，結果快取。"""
import datetime as dt
import json
import os

from config import CACHE_DIR
import http_client as http

_CAL_FILE = os.path.join(CACHE_DIR, "trading_days.json")


def _load():
    if os.path.exists(_CAL_FILE):
        with open(_CAL_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save(d):
    with open(_CAL_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f)


def is_trading_day(d: dt.date) -> bool:
    if d.weekday() >= 5:
        return False
    if d >= dt.date.today():
        return True  # 今天/未來視為交易日（盤中查不到收盤資料）
    cal = _load()
    k = d.strftime("%Y%m%d")
    if k in cal:
        return cal[k]
    try:
        j = http.get_json("https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX",
                          params={"date": k, "type": "MS", "response": "json"}, cache_ttl=86400 * 30)
        ok = j.get("stat") == "OK"
    except http.HttpError:
        ok = True
    cal[k] = ok
    _save(cal)
    return ok


def is_trading_date_str(s) -> bool:
    """紀錄裡的日期字串（2026-11-07 或 20261107）是不是交易日；讀紀錄時用來丟掉週末 / 休市日的假資料。
    舊版 monitor 週末照樣跑，拿凍結的報價發了一堆假訊號（11/07、11/08）。格式不對或查不到時當作交易日（不誤刪）。"""
    try:
        s = str(s).replace("-", "")[:8]
        d = dt.date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except (ValueError, TypeError):
        return True
    try:
        return is_trading_day(d)
    except Exception:
        return d.weekday() < 5


def prev_trading_day(d: dt.date, n=1) -> dt.date:
    cur = d
    while n > 0:
        cur -= dt.timedelta(days=1)
        if is_trading_day(cur):
            n -= 1
    return cur


def last_n_trading_days(ref: dt.date, n: int, include_ref=False):
    out = []
    cur = ref if include_ref else prev_trading_day(ref)
    while len(out) < n:
        if is_trading_day(cur):
            out.append(cur)
        cur -= dt.timedelta(days=1)
    return out


def next_trading_day(d: dt.date) -> dt.date:
    cur = d + dt.timedelta(days=1)
    while not is_trading_day(cur):
        cur += dt.timedelta(days=1)
    return cur


def roc_period(s):
    """處置期間字串「115/11/04～115/11/10」（民國年；上市用全形 ～、上櫃用 ~）→ (起日, 迄日)；解析不了回 None。"""
    import re
    m = re.findall(r"(\d{2,3})/(\d{1,2})/(\d{1,2})", str(s or ""))
    if len(m) < 2:
        return None
    try:
        a, b = (dt.date(int(y) + 1911, int(mo), int(dd)) for y, mo, dd in m[:2])
        return (a, b) if a <= b else None
    except ValueError:
        return None


def period_state(period, day):
    """處置期間相對於 day：'active'（當天在期間內）/ 'future'（還沒開始）/ 'past'（已經結束）/ 'unknown'（字串解析不了）。"""
    p = roc_period(period)
    if not p:
        return "unknown"
    return "past" if day > p[1] else "future" if day < p[0] else "active"


def today_tw() -> dt.date:
    return (dt.datetime.utcnow() + dt.timedelta(hours=8)).date()


def now_tw() -> dt.datetime:
    return dt.datetime.utcnow() + dt.timedelta(hours=8)
