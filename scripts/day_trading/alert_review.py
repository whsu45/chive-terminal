#!/usr/bin/env python3
"""
警示結果統計：讀 logs/alert_outcomes*.jsonl（各台電腦合併）（monitor 快迴圈對每則 ⚠/▶ 警示記下當時狀態，10/30 分鐘後補上漲跌），
按警示類型算「之後漲的比率、平均報酬、期間最大上下行」，用來決定哪些警示值得當訊號、哪些只是提醒。
    python3 alert_review.py                  # 全部
    python3 alert_review.py --code 3006      # 只看一檔
    python3 alert_review.py --days 10        # 最近 10 個交易日
    python3 alert_review.py --type call-surge --detail      # 只看認購權證急漲，列出每一筆（--type 可用英文別名，見 TYPE_ALIASES）
條件欄位：range_pos（0=日低 1=日高）、big_net_10m（主力 10 分鐘淨額變化）、book_imb（五檔不平衡），
可用 --low（日低 25% 內）/--cover（主力 10 分鐘淨額上升）過濾，驗證「權證急漲 + 低檔 + 主力停賣」這種組合。
"""
import os
import json
import argparse
import datetime as dt
from collections import defaultdict
from config import LOG_DIR, dev_files, read_jsonl_devices, one_device
import tw_calendar as cal


# --type 可用英文別名（也接受中文全名；逗號分隔多個）
TYPE_ALIASES = {
    "call-surge": "認購權證急漲", "call-drop": "認購權證急跌", "put-surge": "認售權證急漲", "warrant-burst": "權證爆量",
    "cover": "主力急回補", "dump": "主力急出", "dump-accel": "主力加速出貨", "buy-accel": "主力加速吃貨", "pump-dump": "主力拉高出貨",
    "bounce": "反彈動能", "selloff": "殺盤動能", "call-book": "權證急漲+委買厚",
    "short": "建議做空", "long": "建議做多", "halt": "延緩撮合", "other": "其他",
}


def resolve_types(arg):
    out = []
    for t in (arg or "").split(","):
        t = t.strip()
        if not t:
            continue
        if t in TYPE_ALIASES:
            out.append(TYPE_ALIASES[t])
        elif t in TYPE_ALIASES.values():
            out.append(t)
        else:
            raise SystemExit(f"不認識的類型 {t!r}；可用：" + ", ".join(f"{k}={v}" for k, v in TYPE_ALIASES.items()))
    return out


def load():
    if not dev_files(LOG_DIR, "alert_outcomes", ".jsonl"):
        raise SystemExit(f"還沒有資料：{os.path.join(LOG_DIR, 'alert_outcomes')}*.jsonl（monitor --web 跑過一天後才會有）")
    # 各台電腦合併；同一天同一檔兩台都有時只算一台（永豐串流優先），不重複計
    rows = [r for r in read_jsonl_devices(LOG_DIR, "alert_outcomes") if cal.is_trading_date_str(r.get("date"))]   # 丟掉週末/休市日的假警示
    return one_device(rows, key=lambda r: (r.get("date"), r.get("code")))


def stat(rs, k):
    xs = [r["out"][k] for r in rs if k in r.get("out", {})]
    if not xs:
        return None
    n = len(xs)
    ret = [x["ret_pct"] for x in xs]
    return {"n": n, "up": sum(1 for v in ret if v > 0) / n * 100, "up1": sum(1 for v in ret if v >= 1) / n * 100,
            "dn1": sum(1 for v in ret if v <= -1) / n * 100, "avg": sum(ret) / n,
            "mfe": sum(x["max_pct"] for x in xs) / n, "mae": sum(x["min_pct"] for x in xs) / n}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", default=None)
    ap.add_argument("--type", default=None, help="只看某類警示，英文別名或中文，逗號分隔多個：" + ", ".join(f"{k}={v}" for k, v in TYPE_ALIASES.items()))
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--low", action="store_true", help="只看當時價在日內區間 25%% 以下的")
    ap.add_argument("--high", action="store_true", help="只看當時價在日內區間 75%% 以上的")
    ap.add_argument("--cover", action="store_true", help="只看主力 10 分鐘淨額上升（回補/停賣）的")
    ap.add_argument("--detail", action="store_true")
    ap.add_argument("--src", choices=["shioaji", "mis", "unknown"], default=None, help="只看某資料來源：shioaji 永豐串流、mis 公開報價 10 秒、unknown 舊紀錄")
    a = ap.parse_args()
    rows = load()
    if a.code:
        rows = [r for r in rows if r["code"] == a.code]
    if a.type:
        want = set(resolve_types(a.type))
        rows = [r for r in rows if r["type"] in want]
    if a.src:
        rows = [r for r in rows if (r.get("src") or "unknown") == a.src]
    if a.days:
        days = sorted({r["date"] for r in rows})[-a.days:]
        rows = [r for r in rows if r["date"] in days]
    if a.low:
        rows = [r for r in rows if r.get("range_pos") is not None and r["range_pos"] <= 0.25]
    if a.high:
        rows = [r for r in rows if r.get("range_pos") is not None and r["range_pos"] >= 0.75]
    if a.cover:
        rows = [r for r in rows if r.get("big_net_10m") is not None and r["big_net_10m"] > 0]
    if not rows:
        raise SystemExit("沒有符合條件的警示")
    by = defaultdict(list)
    for r in rows:
        by[r["type"]].append(r)
    print(f"{len(rows)} 則警示，{len({r['date'] for r in rows})} 個交易日，{len({r['code'] for r in rows})} 檔\n")
    print(f"{'類型':<14}{'n':>4} | {'10分 漲%':>8}{'≥+1%':>6}{'≤-1%':>6}{'均':>7}{'最高':>7}{'最低':>7} | {'30分 漲%':>8}{'≥+1%':>6}{'≤-1%':>6}{'均':>7}{'最高':>7}{'最低':>7}")
    for t, rs in sorted(by.items(), key=lambda x: -len(x[1])):
        s10, s30 = stat(rs, "10m"), stat(rs, "30m")
        def fmt(s):
            return f"{s['up']:>7.0f}%{s['up1']:>5.0f}%{s['dn1']:>5.0f}%{s['avg']:>+7.2f}{s['mfe']:>+7.2f}{s['mae']:>+7.2f}" if s else " " * 40
        print(f"{t:<14}{len(rs):>4} | {fmt(s10)} | {fmt(s30)}")
    print("\n漲% = 之後價格高於警示時的比率；均 = 平均報酬 %；最高/最低 = 期間最大上行/下行 %（做多看最高、做空看最低）")
    if a.detail:
        print()
        for r in sorted(rows, key=lambda r: (r["date"], r["time"])):
            o10, o30 = r["out"].get("10m", {}), r["out"].get("30m", {})
            print(f"{r['date']} {r['time']} {r['code']} {r['type']:<10} 價 {r['price']} 位置 {r.get('range_pos') if r.get('range_pos') is None else round(r['range_pos'], 2)} "
                  f"主力 {r.get('big_net')} (10分 {r.get('big_net_10m')}) 五檔 {r.get('book_imb') if r.get('book_imb') is None else round(r['book_imb'], 2)} | "
                  f"10分 {o10.get('ret_pct', float('nan')):+.1f}% (高 {o10.get('max_pct', float('nan')):+.1f} 低 {o10.get('min_pct', float('nan')):+.1f}) "
                  f"30分 {o30.get('ret_pct', float('nan')):+.1f}%{' partial' if o30.get('partial') else ''}")


if __name__ == "__main__":
    main()
