#!/usr/bin/env python3
"""
進場訊號（▶ 建議做空/做多）的輸贏統計與失敗原因：讀 logs/signal_outcomes*.jsonl（各台電腦合併）。
    python3 signal_review.py                 # 全部：各檔、各方向的 勝/敗/平、5 分鐘內反向（估錯）比率、失敗原因排行
    python3 signal_review.py --code 3006 --detail
    python3 signal_review.py --src mis       # 只看 MIS 10 秒快照模式（沒永豐金鑰的機器）的訊號；預設全部，但分來源列一次
判法（11/09 起，紀錄帶 scoring=2）：未成交 = 等回掛單 20 分鐘內沒回到進場區（不算輸贏）；
  成交後 60 分鐘內（最晚 13:25）先逆向超過 3 tick = 輸、先順向到 2 倍風險 = 贏，都沒到看那時扣成本 0.235% 後賺賠；不再有「平」。
  3 tick 超過價格 1% 時減少 tick 數。「均損益」= 扣成本後平均每筆 %，比勝率重要。
舊判法（11/09 前）：成交後 30 分鐘內先到模型目標 1 = 贏、先到模型停損 = 輸、都沒到 = 平（目標太遠，大多記平）。兩種分開列，不混算。
fast_adverse = 5 分鐘內反向走掉模型停損距離一半（即使後來賺也算估錯）。週末 / 休市日的紀錄一律不算。
「原因提升度」= 某條件在輸單出現的比率 ÷ 在贏單出現的比率，>1.5 代表該條件出現時訊號特別容易錯，應該加進模型的過濾條件。
"""
import os
import json
import argparse
from collections import defaultdict
from config import LOG_DIR, dev_files, read_jsonl_devices, one_device
import tw_calendar as cal


def load():
    if not dev_files(LOG_DIR, "signal_outcomes", ".jsonl"):
        raise SystemExit(f"還沒有訊號結果：{os.path.join(LOG_DIR, 'signal_outcomes')}*.jsonl")
    # 各台電腦合併；同一天同一檔兩台都有時只算一台（永豐串流優先），不重複計
    rows = [r for r in read_jsonl_devices(LOG_DIR, "signal_outcomes") if cal.is_trading_date_str(r.get("date"))]   # 丟掉週末/休市日的假訊號
    return one_device(rows, key=lambda r: (r.get("date"), r.get("code")))


def suspects_all(r):
    """贏單也算一次「當時條件」，才能比對原因在輸/贏的出現率。"""
    from monitor import Monitor
    return Monitor._suspects(r["side"], r.get("features") or {})


def load_trades(code=None):
    """持倉面板記的出場事件（logs/trades.jsonl，action=exit），附上當初排隊時的模型建議價。被「復原」的出場不算。"""
    path = os.path.join(LOG_DIR, "trades.jsonl")
    if not os.path.exists(path):
        return []
    evs = []
    for line in open(path, encoding="utf-8"):
        try:
            evs.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    out, last_q, stack = [], {}, {}
    for e in evs:
        k = (e.get("date"), e.get("code"))
        if e.get("action") == "queue":
            last_q[k] = e.get("model_px")
        if e.get("action") == "undo":
            if stack.get(k) and stack[k][-1] is not None:
                out.remove(stack[k][-1])
            if stack.get(k):
                stack[k].pop()
            continue
        rec = None
        if e.get("action") == "exit" and e.get("net_pct") is not None and (code is None or e.get("code") == code):
            rec = dict(e, model_px=last_q.get(k))
            out.append(rec)
        stack.setdefault(k, []).append(rec)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", default=None)
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--detail", action="store_true")
    ap.add_argument("--old", action="store_true", help="看 11/09 前舊判法的紀錄")
    ap.add_argument("--src", choices=["shioaji", "mis", "unknown"], default=None, help="只看某資料來源（unknown = 加來源欄位前的舊紀錄）")
    a = ap.parse_args()
    rows = load()
    blocked = [r for r in rows if r.get("blocked")]
    cancelled = [r for r in rows if r.get("cancelled") and not r.get("blocked")]
    rows = [r for r in rows if not r.get("blocked") and not r.get("cancelled")]   # 被逆勢過濾擋掉、被大單砸穿撤單的不算訊號，最後另外列「擋對/撤對了沒」
    if a.code:
        blocked = [r for r in blocked if r["code"] == a.code]
        cancelled = [r for r in cancelled if r["code"] == a.code]
        rows = [r for r in rows if r["code"] == a.code]
    if a.src:
        rows = [r for r in rows if (r.get("src") or "unknown") == a.src]
    if a.days:
        days = sorted({r["date"] for r in rows})[-a.days:]
        rows = [r for r in rows if r["date"] in days]
    if not rows:
        raise SystemExit("沒有符合條件的訊號")
    new = [r for r in rows if r.get("scoring") == 2]
    old = [r for r in rows if r.get("scoring") != 2]
    if new:
        if old:
            print(f"（另有 {len(old)} 筆是舊判法的紀錄，判準不同不混算；要看加 --old）")
        rows = old if a.old else new
    else:
        print("（還沒有新判法的紀錄：以下是舊判法「30 分鐘先到模型目標 1 / 停損，都沒到記平」）")

    def line(label, rs):
        w = sum(1 for r in rs if r["verdict"] == "win"); l = sum(1 for r in rs if r["verdict"] == "loss")
        nf = sum(1 for r in rs if r["verdict"] == "nofill"); fl = len(rs) - w - l - nf
        fa = sum(1 for r in rs if r.get("fast_adverse"))
        wr = w / (w + l) * 100 if (w + l) else float("nan")
        nets = [r["net_pct"] for r in rs if r.get("net_pct") is not None]
        if nets:
            ex = defaultdict(int)
            for r in rs:
                if r.get("exit"):
                    ex[r["exit"]] += 1
            print(f"{label:<22}{len(rs):>4}  勝 {w:>3} 敗 {l:>3} 未成交 {nf:>2}  勝率 {wr:5.0f}%  均損益 {sum(nets) / len(nets):+.2f}%/筆"
                  f"  （停利 {ex['target']} / 停損 {ex['stop']} / 到時 {ex['time']}）  估錯 {fa / len(rs) * 100:3.0f}%")
            return
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
    is_surge = lambda r: any(k in (r.get("block_reason") or "") for k in ("軋空", "停損賣不掉"))
    kind_of = lambda r: r.get("block_kind") or ("急漲過濾" if is_surge(r) else "逆勢過濾")     # 舊紀錄沒有 block_kind，照理由判斷
    notes = {"急漲過濾": "（漲跌 7% / 離漲跌停 3%）", "空間過濾": "（到前方第一個支撐 / 壓力的報酬風險比不到 1.5）", "重複過濾": "（同價位發過、之後沒破新低 / 過新高）"}
    for kind, grp in ((k + notes.get(k, ""), [r for r in blocked if kind_of(r) == k]) for k in ("逆勢過濾", "急漲過濾", "空間過濾", "重複過濾")):
        if not grp:
            continue
        w = sum(1 for r in grp if r["verdict"] == "win"); l = sum(1 for r in grp if r["verdict"] == "loss")
        print(f"\n{kind}驗證：擋掉 {len(grp)} 筆，若照做會 贏 {w}（擋錯）、輸 {l}（擋對）、其餘平或未成交")
        for r in sorted(grp, key=lambda r: (r["date"], r["time"])):
            print(f"  {r['date']} {r['time']} {r['code']} {r.get('label')} @{r.get('alert_px', r['price'])} → {r['verdict']}  理由：{r.get('block_reason')}")
    if cancelled:
        w = sum(1 for r in cancelled if r["verdict"] == "win"); l = sum(1 for r in cancelled if r["verdict"] == "loss")
        print(f"\n撤單驗證（掛單被大單砸穿）：撤掉 {len(cancelled)} 筆，若沒撤會 贏 {w}（撤錯）、輸 {l}（撤對）、其餘平或未成交")
        for r in sorted(cancelled, key=lambda r: (r["date"], r["time"])):
            cx = r["cancelled"]
            print(f"  {r['date']} {r['time']} {r['code']} {r.get('label')} 掛 {r['price']} → {r['verdict']}  "
                  f"{cx.get('time')} 從 {cx.get('from')} 衝到 {cx.get('px')}（{cx.get('run_ticks')} tick，大單 {cx.get('big_vol')} 張）")
    trades = load_trades(a.code)
    if trades:
        net = [t["net_pct"] for t in trades]
        w = sum(1 for x in net if x > 0)
        better = [t for t in trades if t.get("model_px") and t.get("entry_px")]
        print(f"\n你的實際交易（持倉面板）：{len(trades)} 筆出場，扣成本後賺 {w} 筆、賠 {len(trades) - w} 筆，"
              f"合計 {sum(net):+.2f}%、每筆 {sum(net) / len(net):+.2f}%")
        if better:
            gain = [((t["model_px"] - t["entry_px"]) if t["side"] == "long" else (t["entry_px"] - t["model_px"])) / t["model_px"] * 100 for t in better]
            print(f"  成交價比模型建議價平均{'好' if sum(gain) >= 0 else '差'} {abs(sum(gain) / len(gain)):.2f}%（{len(better)} 筆）")
        for t in trades[-10:]:
            print(f"  {t['date']} {t['time']} {t['code']} {t.get('label')} {t.get('entry_px')}→{t['price']}  {t.get('ticks'):+} tick  扣成本 {t['net_pct']:+.2f}%")
    if a.detail:
        print()
        for r in sorted(rows, key=lambda r: (r["date"], r["time"])):
            mark = {"win": "✔", "loss": "✘", "flat": "—", "nofill": "○"}[r["verdict"]] + ("!" if r.get("fast_adverse") else " ")
            sc = (f" | {({'target': '停利', 'stop': '停損', 'time': '到時'}).get(r.get('exit'), '')} {r.get('exit_px')} 扣成本 {r['net_pct']:+.2f}%"
                  f" 逆 {r.get('mae_ticks')} / 順 {r.get('mfe_ticks')} tick") if r.get("net_pct") is not None else ""
            print(f"{mark} {r['date']} {r['time']} {(r.get('src') or '?')[:3]} {r['code']} {r.get('label')} @{r['price']}{f"（掛 {r['zone'][0]}–{r['zone'][1]}，訊號時 {r.get('alert_px')}）" if r.get('zone') and r.get('filled_at_alert') is False else ''} 停 {r['stop']} 目 {r['t1']}{f" 已走 {r['chase']:.0%}" if r.get('chase') is not None else ''} | 10分 {r.get('ret_10m') if r.get('ret_10m') is None else round(r['ret_10m'], 2)}% 30分 {r.get('ret_30m') if r.get('ret_30m') is None else round(r['ret_30m'], 2)}%"
                  + sc + (f" | 可疑：{'、'.join(r['suspects'])}" if r.get("suspects") else ""))


if __name__ == "__main__":
    main()
