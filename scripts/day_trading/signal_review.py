#!/usr/bin/env python3
"""
進場訊號（▶ 建議做空/做多）的輸贏統計與失敗原因：讀 logs/signal_outcomes.jsonl。
    python3 signal_review.py                 # 全部：各檔、各方向的 勝/敗/平、5 分鐘內反向（估錯）比率、失敗原因排行
    python3 signal_review.py --code 3006 --detail
    python3 signal_review.py --src mis       # 只看 MIS 10 秒快照模式（沒永豐金鑰的機器）的訊號；預設全部，但分來源列一次
未成交 = 等回掛單 20 分鐘內沒回到進場區（不算輸贏）；贏 = 成交後 30 分鐘內先到目標 1；輸 = 先到停損；平 = 都沒到；fast_adverse = 5 分鐘內反向走掉停損距離一半（即使後來賺也算估錯）。
「原因提升度」= 某條件在輸單出現的比率 ÷ 在贏單出現的比率，>1.5 代表該條件出現時訊號特別容易錯，應該加進模型的過濾條件。
"""
import os
import json
import argparse
from collections import defaultdict
from config import LOG_DIR


def load():
    path = os.path.join(LOG_DIR, "signal_outcomes.jsonl")
    if not os.path.exists(path):
        raise SystemExit(f"還沒有訊號結果：{path}")
    rows = []
    for line in open(path, encoding="utf-8"):
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return rows


def suspects_all(r):
    """贏單也算一次「當時條件」，才能比對原因在輸/贏的出現率。"""
    from monitor import Monitor
    return Monitor._suspects(r["side"], r.get("features") or {})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", default=None)
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--detail", action="store_true")
    ap.add_argument("--src", choices=["shioaji", "mis", "unknown"], default=None, help="只看某資料來源（unknown = 加來源欄位前的舊紀錄）")
    a = ap.parse_args()
    rows = load()
    if a.code:
        rows = [r for r in rows if r["code"] == a.code]
    if a.src:
        rows = [r for r in rows if (r.get("src") or "unknown") == a.src]
    if a.days:
        days = sorted({r["date"] for r in rows})[-a.days:]
        rows = [r for r in rows if r["date"] in days]
    if not rows:
        raise SystemExit("沒有符合條件的訊號")

    def line(label, rs):
        w = sum(1 for r in rs if r["verdict"] == "win"); l = sum(1 for r in rs if r["verdict"] == "loss")
        nf = sum(1 for r in rs if r["verdict"] == "nofill"); fl = len(rs) - w - l - nf
        fa = sum(1 for r in rs if r.get("fast_adverse"))
        wr = w / (w + l) * 100 if (w + l) else float("nan")
        r30 = [r["ret_30m"] for r in rs if r.get("ret_30m") is not None]
        avg = sum(r30) / len(r30) if r30 else float("nan")
        print(f"{label:<22}{len(rs):>4}  勝 {w:>3} 敗 {l:>3} 平 {fl:>3} 未成交 {nf:>2}  勝率 {wr:5.0f}%  估錯(5分反向) {fa / len(rs) * 100:4.0f}%  30分均 {avg:+.2f}%")

    print(f"{len(rows)} 筆訊號，{len({r['date'] for r in rows})} 天\n")
    line("全部", rows)
    for side in ("short", "long"):
        rs = [r for r in rows if r["side"] == side]
        if rs:
            line("  建議做空" if side == "short" else "  建議做多", rs)
    srcs = sorted({r.get("src") or "unknown" for r in rows})
    if len(srcs) > 1:
        for sv in srcs:
            line({"shioaji": "  來源 永豐串流", "mis": "  來源 MIS 10 秒", "unknown": "  來源 舊紀錄"}[sv], [r for r in rows if (r.get("src") or "unknown") == sv])
    by = defaultdict(list)
    for r in rows:
        by[(r["code"], r.get("name"))].append(r)
    print()
    for (c, n), rs in sorted(by.items(), key=lambda x: -len(x[1])):
        line(f"{c} {n or ''}", rs)

    # 原因提升度
    bad = [r for r in rows if r["verdict"] == "loss" or r.get("fast_adverse")]
    good = [r for r in rows if r["verdict"] == "win" and not r.get("fast_adverse")]
    cb, cg = defaultdict(int), defaultdict(int)
    for r in bad:
        for s in suspects_all(r): cb[s] += 1
    for r in good:
        for s in suspects_all(r): cg[s] += 1
    if bad:
        print(f"\n失敗原因（輸或估錯 {len(bad)} 筆 vs 乾淨贏 {len(good)} 筆）")
        print(f"{'條件':<18}{'輸單出現%':>10}{'贏單出現%':>10}{'提升度':>8}")
        for s in sorted(set(cb) | set(cg), key=lambda s: -(cb[s] / max(1, len(bad)))):
            pb = cb[s] / len(bad) * 100; pg = cg[s] / len(good) * 100 if good else 0
            lift = (pb / pg) if pg else float("inf") if pb else 0
            print(f"{s:<18}{pb:>9.0f}%{pg:>9.0f}%{lift:>8.1f}")
    if a.detail:
        print()
        for r in sorted(rows, key=lambda r: (r["date"], r["time"])):
            mark = {"win": "✔", "loss": "✘", "flat": "—", "nofill": "○"}[r["verdict"]] + ("!" if r.get("fast_adverse") else " ")
            print(f"{mark} {r['date']} {r['time']} {(r.get('src') or '?')[:3]} {r['code']} {r.get('label')} @{r['price']}{f"（掛 {r['zone'][0]}–{r['zone'][1]}，訊號時 {r.get('alert_px')}）" if r.get('zone') and r.get('filled_at_alert') is False else ''} 停 {r['stop']} 目 {r['t1']}{f" 已走 {r['chase']:.0%}" if r.get('chase') is not None else ''} | 10分 {r.get('ret_10m') if r.get('ret_10m') is None else round(r['ret_10m'], 2)}% 30分 {r.get('ret_30m') if r.get('ret_30m') is None else round(r['ret_30m'], 2)}%"
                  + (f" | 可疑：{'、'.join(r['suspects'])}" if r.get("suspects") else ""))


if __name__ == "__main__":
    main()
