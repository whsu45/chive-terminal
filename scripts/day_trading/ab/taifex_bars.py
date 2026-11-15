#!/usr/bin/env python3
"""
期交所「前 30 個交易日期貨每筆成交」（Daily_YYYY_MM_DD.zip，CSV 版）→ 台指期近月 1 分 K。
    python3 ab/taifex_bars.py            # 讀 .cache/taifex_ticks/zip/*.zip，寫 .cache/taifex_ticks/TX_1m.json
欄位：成交日期, 商品代號, 到期月份(週別), 成交時間, 成交價格, 成交數量(B+S), 近月價格, 遠月價格, 開盤集合競價
- 價差單（近月 / 遠月價格不是 "-"）剔除；週選 / 週期貨（到期月份帶 W）剔除；每天取成交量最大的月合約 = 近月。
- 成交數量是買賣雙邊合計（B+S），除以 2 才是口數。
- 日盤 08:45–13:45 歸成交日期；夜盤 15:00–次日 05:00 另存（session="night"）。
- 期交所逐筆沒有內外盤標記，所以沒有 buy_vol / sell_vol。
"""
import os
import io
import csv
import sys
import json
import glob
import zipfile
import datetime as dt
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(os.path.dirname(HERE), ".cache", "taifex_ticks")


def parse_zip(path, product="TX"):
    """回 {(session_date, session): {contract: [(datetime, price, qty)]}}。"""
    out = defaultdict(lambda: defaultdict(list))
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            with z.open(name) as f:
                for row in csv.reader(io.TextIOWrapper(f, encoding="big5", errors="ignore")):
                    if len(row) < 8 or row[1].strip() != product:
                        continue
                    exp = row[2].strip()
                    if "W" in exp or not exp.isdigit() or row[6].strip() != "-" or row[7].strip() != "-":
                        continue
                    try:
                        d = dt.datetime.strptime(row[0].strip() + row[3].strip().zfill(6), "%Y%m%d%H%M%S")
                        px, qty = float(row[4]), int(row[5]) / 2
                    except ValueError:
                        continue
                    hm = d.hour * 60 + d.minute
                    if 525 <= hm <= 825:                       # 08:45–13:45 日盤
                        key = (d.date(), "day")
                    elif hm >= 900 or hm <= 300:                # 15:00–05:00 夜盤，歸「開始那天」
                        start = d.date() if hm >= 900 else d.date() - dt.timedelta(days=1)
                        key = (start, "night")
                    else:
                        continue
                    out[key][exp].append((d, px, qty))
    return out


def to_bars(ticks):
    bars = {}
    for d, px, q in sorted(ticks):
        k = d.replace(second=0)
        if d.hour == 13 and d.minute == 45:                   # 13:45:00 收盤那筆併到 13:44
            k = d.replace(minute=44, second=0)
        b = bars.get(k)
        if b is None:
            bars[k] = {"ts": k.isoformat(), "open": px, "high": px, "low": px, "close": px, "volume": q}
        else:
            b["high"] = max(b["high"], px); b["low"] = min(b["low"], px); b["close"] = px; b["volume"] += q
    return [bars[k] for k in sorted(bars)]


def main():
    zips = sorted(glob.glob(os.path.join(OUT_DIR, "zip", "Daily_*.zip")))
    sessions = defaultdict(lambda: defaultdict(list))
    for p in zips:
        for key, by_c in parse_zip(p).items():
            for c, t in by_c.items():
                sessions[key][c] += t
    out = {"day": [], "night": [], "contracts": {}}
    for (d, sess), by_c in sorted(sessions.items()):
        c = max(by_c, key=lambda k: sum(q for _, _, q in by_c[k]))
        bars = to_bars(by_c[c])
        if sess == "day" and len(bars) < 200:                 # 不完整的日盤（例如最後一天只有夜盤）
            continue
        out[sess] += bars
        out["contracts"][f"{d} {sess}"] = c
    with open(os.path.join(OUT_DIR, "TX_1m.json"), "w") as f:
        json.dump(out, f)
    days = sorted({b["ts"][:10] for b in out["day"]})
    print(f"{len(zips)} 個檔 → 日盤 {len(days)} 天（{days[0]} ~ {days[-1]}）、{len(out['day'])} 根；夜盤 {len(out['night'])} 根")


if __name__ == "__main__":
    sys.exit(main())
