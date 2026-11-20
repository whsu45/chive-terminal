#!/usr/bin/env python3
"""
恐慌指數（2026-11-20 加）：臺指選擇權波動率指數（臺指 VIX，期交所以 CBOE VIX 公式計算，預期未來 30 天大盤波動，越高越恐慌）。

資料來源（都是公開或你自己的永豐帳號，不用另外付費或安裝）：
  盤中即時：永豐 Shioaji 指數合約 Indexs.TAIFEX["TAIWANVIX"] 的 snapshots（每分鐘查一次）。
  每日收盤：期交所公開檔 https://www.taifex.com.tw/file/taifex/Dailydownload/vix/log2data/YYYYMMnew.txt（當月每天更新，只公開近 3 個月）。
  歷史分位：.cache/vix_daily.json（日期 → 收盤）。第一次有永豐時用 kbars 一次補 2024 年以後的日收盤（約 6 次查詢，只在盤後補），
            之後每天用期交所的公開檔接上。永豐的值跟期交所公布的收盤在 2026/08–11 的 59 天完全一樣。
  沒有永豐（Windows 只用 MIS）：只有前一天收盤，沒有盤中即時值。

級距（2024/01–2026/11 共 683 天：中位數 23.5、80 分位 31.1、95 分位 39.5、最高 52.1 = 2026/11/20）：
  <20 平靜｜20～30 正常｜30～40 緊張｜≥40 極度恐慌

用在哪裡（2026-11-20 驗證，「沒有適合的就不做」）：
  - 隔日沖倒貨放空（選股頁、開盤觀察 B 組）：前一天 VIX ≥40 時不做。
      2026 全市場日 K：VIX 20～30 勝率 63%、每筆 +0.92%；30～40 勝率 57%、+0.41%；≥40 勝率 50%、−0.28%（17 天）。
      tw_stocker 2024–2026：<40 各段勝率 57～61%、每筆 +0.26～+0.45%；≥40 勝率 45%（只有 4 天）。
  - 恐慌開低做多（開盤觀察 C 組）：前一天 VIX ≥40 時不做。
      2026 年賺錢的 6/8、11/6 前一天 VIX 36.3、39.9；11/20 前一天 49.3，開盤買到收盤 0050 −3.2%。
      tw_stocker 裡 VIX ≥30 時這組勝率 42～46%、每筆都賠——證據很薄，這組本來就只記錄。
  - 盤中支撐壓力訊號：VIX 高低對它沒有差別（各段每筆都是 −0.27～−0.28%）。盤中 VIX 比昨收高 ≥10%（恐慌升溫中）時，
      做多訊號抱到收盤每筆 −0.38%（平常 −0.32%）、做空 −0.13%（平常 −0.19%）：方向有、幅度很小，只在訊號上寫參考、不擋。

    python3 fear.py            # 印現在的恐慌指數（有永豐就是即時值）與歷史分位
"""
import datetime as dt
import json
import os
import re
import threading
import time

from config import CACHE_DIR
import http_client as http

DAILY_URL = "https://www.taifex.com.tw/file/taifex/Dailydownload/vix/log2data/{ym}new.txt"
HIST = os.path.join(CACHE_DIR, "vix_daily.json")
EXTREME = 40.0           # 前一天收盤 ≥ 這個 → 極度恐慌：隔日沖倒貨、恐慌開低做多都不做
SURGE_PCT = 10.0         # 盤中比昨收高這麼多 % → 恐慌升溫中（做多訊號寫參考）
LEVELS = ((0, 20, "平靜"), (20, 30, "正常"), (30, 40, "緊張"), (40, 1e9, "極度恐慌"))
_lock = threading.Lock()
_hist = None
_hist_t = 0.0


def level(v):
    if v is None:
        return None
    for lo, hi, name in LEVELS:
        if lo <= v < hi:
            return name
    return None


def _load():
    global _hist
    if _hist is None:
        try:
            with open(HIST, encoding="utf-8") as f:
                _hist = json.load(f)
        except Exception:
            _hist = {}
    return _hist


def _save(h):
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = HIST + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dict(sorted(h.items())), f)
    os.replace(tmp, HIST)


def parse_daily(text):
    """期交所 YYYYMMnew.txt → {YYYY-MM-DD: 收盤}。"""
    out = {}
    for line in text.splitlines():
        m = re.match(r"\s*(\d{8})\s+\S+\s+([\d.]+)", line)
        if m:
            d = m.group(1)
            out[f"{d[:4]}-{d[4:6]}-{d[6:]}"] = float(m.group(2))
    return out


def refresh_daily(today=None, max_age=3600):
    """用期交所公開檔補當月與上個月的日收盤（最多一小時抓一次）。回歷史 dict。"""
    global _hist_t
    today = today or dt.date.today()
    with _lock:
        h = _load()
        if time.time() - _hist_t < max_age and h:
            return h
        _hist_t = time.time()
        first = today.replace(day=1)
        for d in (first - dt.timedelta(days=1), today):
            try:
                raw = http.get(DAILY_URL.format(ym=d.strftime("%Y%m")), retries=1, timeout=15)
                h.update(parse_daily(raw.decode("big5", errors="replace")))
            except Exception as e:
                print(f"[恐慌指數] 期交所日資料抓不到：{str(e)[:80]}")
        if h:
            _save(h)
        return h


def backfill_shioaji(api, since="2024-01-01"):
    """永豐 kbars 補歷史日收盤（盤後才用；每半年一次查詢）。已經有 300 天以上就不補。"""
    import src_shioaji as S
    with _lock:
        h = _load()
        if len(h) >= 300:
            return 0
    c = api.Contracts.Indexs.TAIFEX["TAIWANVIX"]
    start = dt.date.fromisoformat(since)
    added = 0
    while start <= dt.date.today():
        end = min(start + dt.timedelta(days=182), dt.date.today())
        k = S.fetch_kbars(api, c, start.isoformat(), "恐慌指數歷史", end=end.isoformat())
        day_close = {}
        for ts, cl in zip(k.ts, k.Close):
            day_close[(S.ns_to_dt(ts) - dt.timedelta(minutes=1)).date().isoformat()] = float(cl)
        with _lock:
            h = _load()
            for d, v in day_close.items():
                if d not in h:
                    h[d] = v; added += 1
        start = end + dt.timedelta(days=1)
    with _lock:
        _save(_load())
    return added


def live_value(api):
    """永豐即時值（snapshots）；拿不到回 None。"""
    try:
        c = api.Contracts.Indexs.TAIFEX["TAIWANVIX"]
        s = api.snapshots([c])
        return float(s[0].close) if s and s[0].close else None
    except Exception:
        return None


def state(now=None, api=None, live=None):
    """現在的恐慌狀態：now（盤中即時，沒有永豐為 None）、prev（前一個交易日收盤）、級距、分位、是否極度恐慌 / 升溫中，以及各規則的開關。"""
    now = now or dt.datetime.now()
    h = refresh_daily(now.date())
    today = now.date().isoformat()
    past = sorted(d for d in h if d < today)
    prev = h[past[-1]] if past else None
    prev_date = past[-1] if past else None
    cur = live if live is not None else (live_value(api) if api is not None else None)
    if cur is None and today in h:
        cur = h[today]                                  # 收盤後：今天的收盤已經公布
    ref = cur if cur is not None else prev
    vals = sorted(h.values())
    pct = (sum(1 for x in vals if x <= ref) / len(vals) * 100) if (ref is not None and len(vals) >= 200) else None
    chg = ((cur / prev - 1) * 100) if (cur is not None and prev) else None
    extreme = prev is not None and prev >= EXTREME
    surge = chg is not None and chg >= SURGE_PCT
    out = {"now": cur, "prev": prev, "prev_date": prev_date, "chg_pct": round(chg, 2) if chg is not None else None,
           "level": level(ref), "pct": round(pct) if pct is not None else None, "hist_days": len(vals),
           "extreme": extreme, "surge": surge, "src": "shioaji" if ((api is not None or live is not None) and cur is not None and today not in h) else "taifex",
           "allow_nds": not extreme, "allow_panic_long": not extreme, "time": now.strftime("%H:%M:%S")}
    out["text"] = describe(out)
    return out


def describe(s):
    v = s.get("now") if s.get("now") is not None else s.get("prev")
    if v is None:
        return "恐慌指數（臺指 VIX）抓不到"
    t = f"臺指 VIX {v:.2f}（{s['level']}"
    if s.get("pct") is not None:
        t += f"，2024 年以來第 {s['pct']} 百分位"
    t += "）"
    if s.get("now") is not None and s.get("prev"):
        t += f"，昨收 {s['prev']:.2f}（{s['chg_pct']:+.1f}%）"
    elif s.get("prev"):
        t += "（前一天收盤，沒有盤中即時值）"
    if s.get("extreme"):
        t += f"；前一天 ≥{EXTREME:g} 極度恐慌：隔日沖倒貨、恐慌開低做多今天不做"
    if s.get("surge"):
        t += f"；盤中恐慌升溫（比昨收高 ≥{SURGE_PCT:g}%）：做多訊號回測更差"
    return t


if __name__ == "__main__":
    api = None
    try:
        from config import load_env
        load_env()
        import src_shioaji as S
        if S.available():
            api = S.login()
            hm = dt.datetime.now().strftime("%H:%M")
            if api is not None and (hm >= "13:35" or hm < "08:30"):
                n = backfill_shioaji(api)
                if n:
                    print(f"[恐慌指數] 用永豐補了 {n} 天歷史收盤")
    except Exception as e:
        print(f"[恐慌指數] 永豐不能用：{str(e)[:80]}")
    try:
        print(describe(state(api=api)))
    finally:
        if api is not None:
            import src_shioaji as S
            S.logout()
