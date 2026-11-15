#!/usr/bin/env python3
"""
永豐 Shioaji：台指期近月連續（TXFR1）1 分 K，2020-03-02 起（永豐歷史 K 棒最早到這天）。
    python3 ab/sj_tx_bars.py                 # 逐月抓，已抓過的月份跳過；寫 .cache/sj_kbars/TXFR1_YYYY-MM.json
    python3 ab/sj_tx_bars.py --merge         # 合併成 .cache/sj_kbars/TXFR1_1m.json（日盤 / 夜盤分開）
- 只在盤後跑：盤中 kbars 有每日 270 次的官方上限（src_shioaji.fetch_kbars 會擋）。流量每月約 2MB，每日上限 500MB。
- Shioaji K 棒的 ts 是「結束」時間，這裡減 1 分鐘改成開始時間，和期交所逐筆、tw_stocker 一致。
- R1 是近月連續、沒有還原：換月那天的開盤對昨收會多出近遠月價差。
"""
import os
import sys
import json
import time
import argparse
import datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, ".cache", "sj_kbars")


def months(start, end):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        a = dt.date(y, m, 1)
        b = (dt.date(y + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1))
        yield max(a, start), min(b, end)
        y, m = y + (m == 12), m % 12 + 1


def fetch(end):
    sys.path.insert(0, ROOT)
    import src_shioaji as S
    if S._intraday():
        sys.exit("盤中不抓歷史 K 棒（官方 kbars 每日上限），收盤後再跑")
    api = S.login()
    if api is None:
        sys.exit("永豐登入失敗")
    c = None
    for _ in range(40):
        try:
            c = api.Contracts.Futures.TXF.TXFR1
            if c is not None:
                break
        except Exception:
            pass
        time.sleep(0.5)
    os.makedirs(OUT, exist_ok=True)
    n = 0
    for a, b in months(dt.date(2020, 3, 2), end):
        path = os.path.join(OUT, f"TXFR1_{a:%Y-%m}.json")
        done = b < end and os.path.exists(path)            # 當月（還沒過完）每次重抓
        if done:
            continue
        k = api.kbars(contract=c, start=a.isoformat(), end=b.isoformat())
        rows = []
        for ts, o, h, l, cl, v in zip(k.ts, k.Open, k.High, k.Low, k.Close, k.Volume):
            t = S.ns_to_dt(ts) - dt.timedelta(minutes=1)
            rows.append({"ts": t.isoformat(), "open": float(o), "high": float(h), "low": float(l), "close": float(cl), "volume": float(v)})
        with open(path, "w") as f:
            json.dump(rows, f)
        n += 1
        print(f"{a:%Y-%m} {len(rows)} 根", flush=True)
        time.sleep(1.0)
    print("流量", S.usage())
    api.logout()
    return n


def merge():
    import glob
    day, night = [], []
    for p in sorted(glob.glob(os.path.join(OUT, "TXFR1_????-??.json"))):
        for r in json.load(open(p)):
            t = dt.datetime.fromisoformat(r["ts"])
            hm = t.hour * 60 + t.minute
            if 525 <= hm < 825:
                day.append(r)
            elif hm >= 900 or hm < 300:
                night.append(r)
    seen = set()
    day = [r for r in sorted(day, key=lambda r: r["ts"]) if not (r["ts"] in seen or seen.add(r["ts"]))]
    with open(os.path.join(OUT, "TXFR1_1m.json"), "w") as f:
        json.dump({"day": day, "night": night}, f)
    ds = sorted({r["ts"][:10] for r in day})
    print(f"日盤 {len(ds)} 天（{ds[0]} ~ {ds[-1]}）、{len(day)} 根；夜盤 {len(night)} 根")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--merge", action="store_true")
    ap.add_argument("--end", default=None, help="抓到哪天（預設昨天）")
    a = ap.parse_args()
    if a.merge:
        merge()
    else:
        fetch(dt.date.fromisoformat(a.end) if a.end else dt.date.today() - dt.timedelta(days=1))
        merge()
