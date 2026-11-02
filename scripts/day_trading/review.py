#!/usr/bin/env python3
"""
收盤後復盤：讀 logs/monitor_YYYYMMDD.jsonl，列出每檔的勝率曲線與價格走勢，
並檢驗「當時模型偏空/偏多」在 N 分鐘後是否正確。
    python3 review.py                 # 今天
    python3 review.py 20261102 3042   # 指定日期/代碼
    python3 review.py --horizon 20    # 以 20 分鐘後的價格驗證（預設 30）
"""
import sys
import os
import json
import argparse
import datetime as dt
from config import LOG_DIR


def load(date_str):
    path = os.path.join(LOG_DIR, f"monitor_{date_str}.jsonl")
    if not os.path.exists(path):
        raise SystemExit(f"找不到 {path}")
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("date", nargs="?", default=dt.date.today().strftime("%Y%m%d"))
    ap.add_argument("code", nargs="?", default=None)
    ap.add_argument("--horizon", type=int, default=30, help="驗證用的分鐘數")
    a = ap.parse_args()
    rows = load(a.date)
    codes = sorted({r["code"] for r in rows}) if not a.code else [a.code]
    for code in codes:
        rs = [r for r in rows if r["code"] == code and r.get("price")]
        if not rs:
            continue
        print(f"=== {code} {rs[0]['name']}  {a.date}  共 {len(rs)} 次評估")
        print(f"{'時間':8} {'價':>8} {'漲跌%':>7} {'大盤%':>7} {'族群%':>7} {'多%':>4} {'空%':>4}  判斷")
        last_min = None
        for r in rs:
            hm = r["time"][:5]
            if hm == last_min:
                continue
            last_min = hm
            inf = r.get("info") or {}
            f = lambda x: "-" if x is None else f"{x:+.2f}"
            print(f"{r['time']:8} {r['price']:>8} {f(inf.get('chg_pct')):>7} {f(inf.get('idx_chg_pct')):>7} {f(inf.get('group_avg')):>7} "
                  f"{r['p_long']:>4.0f} {r['p_short']:>4.0f}  {r['verdict']}")
        # 驗證
        def at(t):
            return dt.datetime.strptime(t, "%H:%M:%S")
        hits = tot = 0
        detail = []
        for i, r in enumerate(rs):
            if r["time"] >= "09:50:00" or r["time"] < "09:00:00":
                continue
            side = "空" if r["p_short"] > r["p_long"] else "多"
            edge = abs(r["p_short"] - r["p_long"])
            if edge < 8:
                continue
            target_t = at(r["time"]) + dt.timedelta(minutes=a.horizon)
            later = [x for x in rs if at(x["time"]) >= target_t]
            if not later:
                continue
            p0, p1 = r["price"], later[0]["price"]
            ok = (p1 < p0) if side == "空" else (p1 > p0)
            tot += 1; hits += ok
            detail.append(f"    {r['time'][:5]} {side} {max(r['p_short'], r['p_long']):.0f}%  {p0} → {p1} ({(p1/p0-1)*100:+.2f}%)  {'✓' if ok else '✗'}")
        if tot:
            print(f"  09:00–09:50 有方向的訊號 {tot} 次，{a.horizon} 分鐘後方向正確 {hits} 次（{hits/tot*100:.0f}%）")
            print("\n".join(detail))
        else:
            print("  09:00–09:50 區間沒有足夠訊號可驗證（或今天是盤後才啟動）")
        print()


if __name__ == "__main__":
    main()
