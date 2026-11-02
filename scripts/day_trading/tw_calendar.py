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


def today_tw() -> dt.date:
    return (dt.datetime.utcnow() + dt.timedelta(hours=8)).date()


def now_tw() -> dt.datetime:
    return dt.datetime.utcnow() + dt.timedelta(hours=8)
