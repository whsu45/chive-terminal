#!/usr/bin/env python3
"""
用歷史估算紀錄演化因子權重（簡單基因演算法）。
資料：logs/monitor_YYYYMMDD.jsonl 每一輪估算的因子（名稱、權重）與價格；同一檔同一天往後 30 分鐘的價格變化當答案。
基因：每個因子一個「權重倍數」（0 = 關掉、1 = 原樣、最高 2.5）+ 全域 scale（勝率對 bias 的斜率，原 11）+ daily_w（盤前因子權重，原 0.8）。
適應度：預測「30 分鐘後漲」的機率 p 對實際漲跌的 Brier 分數（越低越好），加上倍數偏離 1 的懲罰（資料少時不要亂動）。
    python3 evolve.py                    # 跑一次，印結果，寫 model_params.candidate.json
    python3 evolve.py --apply            # 覆寫 model_params.json（monitor 下一輪估算自動載入）
    python3 evolve.py --horizon 10 --gens 200 --min-n 20
注意：幾天的資料就幾天的盤勢，樣本 <500 時只能當方向參考；每次 --apply 前先看 walk-forward（最後一天當測試）有沒有變差。
"""
import os
import json
import glob
import math
import random
import argparse
import datetime as dt
from collections import defaultdict
from config import LOG_DIR, read_jsonl_devices, one_device

HERE = os.path.dirname(os.path.abspath(__file__))


def load_rows(include_mis=False):
    """MIS 10 秒快照模式（沒永豐串流）的估算缺分流因子、K 線也粗，預設不拿來演化。"""
    rows = []
    dates = sorted({os.path.basename(p)[8:16] for p in glob.glob(os.path.join(LOG_DIR, "monitor_*.jsonl"))})
    for date in dates:
        # 同一天各台電腦的紀錄合併；同一檔兩台都有時只用一台（永豐串流優先），不重複計
        day = [r for r in read_jsonl_devices(LOG_DIR, f"monitor_{date}") if r.get("price") and r.get("time") and r.get("code")]
        for r in one_device(day, key=lambda r: r["code"]):
            if r.get("src") == "mis" and not include_mis:
                continue
            rows.append({"date": date, "code": r["code"], "time": r["time"][:8], "price": float(r["price"]),
                         "DF": [(f[0], float(f[1])) for f in (r.get("daily_factors") or [])],
                         "IF": [(f[0], float(f[1])) for f in (r.get("intraday_factors") or [])]})
    return rows


def with_forward(rows, horizon_min):
    """同一檔同一天往後 horizon 分鐘（取 ≥ 的最近一筆）的報酬 %。"""
    by = defaultdict(list)
    for r in rows:
        by[(r["date"], r["code"])].append(r)
    out = []
    for key, rs in by.items():
        rs.sort(key=lambda r: r["time"])
        secs = [int(r["time"][:2]) * 3600 + int(r["time"][3:5]) * 60 + int(r["time"][6:8]) for r in rs]
        for i, r in enumerate(rs):
            if not ("09:05:00" <= r["time"] < "12:50:00"):
                continue
            target = secs[i] + horizon_min * 60
            j = next((k for k in range(i + 1, len(rs)) if secs[k] >= target), None)
            if j is None or secs[j] - target > 600:
                continue
            fwd = (rs[j]["price"] / r["price"] - 1) * 100
            if abs(fwd) < 1e-9:
                continue
            out.append({**r, "fwd": fwd, "y": 1.0 if fwd > 0 else 0.0})
    return out


def predict(genes, names, row, clamp=3.5):
    m = genes["m"]
    db = sum(w * m.get(n, 1.0) for n, w in row["DF"])
    ib = sum(w * m.get(n, 1.0) for n, w in row["IF"])
    hhmm = row["time"][:5]
    w_intra = 0.5 if hhmm < "09:10" else 1.0
    bias = max(-clamp, min(clamp, db * genes["daily_w"] + ib * w_intra))
    p = max(15.0, min(85.0, 50 + genes["scale"] * bias)) / 100
    return p, bias


def fitness(genes, names, data, lam):
    brier = 0.0
    for row in data:
        p, _ = predict(genes, names, row)
        brier += (p - row["y"]) ** 2
    brier /= max(1, len(data))
    pen = lam * sum((genes["m"].get(n, 1.0) - 1.0) ** 2 for n in names) / max(1, len(names))
    pen += lam * (((genes["scale"] - 11) / 11) ** 2 + ((genes["daily_w"] - 0.8) / 0.8) ** 2)
    return -(brier + pen), brier


def report(genes, names, data):
    n = len(data)
    hits = conf = 0
    xs, ys = [], []
    for row in data:
        p, bias = predict(genes, names, row)
        xs.append(bias); ys.append(row["fwd"])
        if abs(p - 0.5) >= 0.10:
            conf += 1
            if (p > 0.5) == (row["fwd"] > 0):
                hits += 1
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    vx = math.sqrt(sum((a - mx) ** 2 for a in xs)) or 1e-9
    vy = math.sqrt(sum((b - my) ** 2 for b in ys)) or 1e-9
    _, brier = fitness(genes, names, data, 0)
    return {"n": n, "brier": brier, "hit_rate": hits / conf * 100 if conf else None, "confident": conf, "ic": cov / (vx * vy)}


def evolve(names, data, base, gens=120, pop_n=80, lam=0.02, seed=42, log=print):
    rnd = random.Random(seed)

    def rand_genes():
        return {"m": {n: max(0.0, min(2.5, rnd.gauss(1.0, 0.35))) for n in names},
                "scale": max(5.0, min(20.0, rnd.gauss(11, 2.5))), "daily_w": max(0.2, min(1.5, rnd.gauss(0.8, 0.25)))}

    def mutate(g):
        h = {"m": dict(g["m"]), "scale": g["scale"], "daily_w": g["daily_w"]}
        for n in names:
            if rnd.random() < 0.2:
                h["m"][n] = max(0.0, min(2.5, h["m"][n] + rnd.gauss(0, 0.15)))
        if rnd.random() < 0.3:
            h["scale"] = max(5.0, min(20.0, h["scale"] + rnd.gauss(0, 1.0)))
        if rnd.random() < 0.3:
            h["daily_w"] = max(0.2, min(1.5, h["daily_w"] + rnd.gauss(0, 0.1)))
        return h

    def cross(a, b):
        return {"m": {n: (a["m"][n] if rnd.random() < 0.5 else b["m"][n]) for n in names},
                "scale": a["scale"] if rnd.random() < 0.5 else b["scale"],
                "daily_w": a["daily_w"] if rnd.random() < 0.5 else b["daily_w"]}

    pop = [base] + [rand_genes() for _ in range(pop_n - 1)]
    scored = sorted(((fitness(g, names, data, lam)[0], g) for g in pop), key=lambda x: -x[0])
    for gen in range(gens):
        elite = [g for _, g in scored[:4]]
        children = list(elite)
        while len(children) < pop_n:
            a = max(rnd.sample(scored, 3), key=lambda x: x[0])[1]
            b = max(rnd.sample(scored, 3), key=lambda x: x[0])[1]
            children.append(mutate(cross(a, b)))
        scored = sorted(((fitness(g, names, data, lam)[0], g) for g in children), key=lambda x: -x[0])
        if gen % 20 == 0 or gen == gens - 1:
            log(f"  世代 {gen:3d}  最佳適應度 {scored[0][0]:.5f}")
    return scored[0][1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", type=int, default=30, help="答案：幾分鐘後的漲跌（預設 30）")
    ap.add_argument("--min-n", type=int, default=15, help="因子至少出現幾次才給它基因（否則固定 1.0）")
    ap.add_argument("--gens", type=int, default=120)
    ap.add_argument("--pop", type=int, default=80)
    ap.add_argument("--lam", type=float, default=0.02, help="偏離 1.0 的懲罰（資料少時調大）")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--apply", action="store_true", help="寫入 model_params.json（否則只寫 candidate）")
    ap.add_argument("--include-mis", action="store_true", help="連 MIS 10 秒快照模式的估算一起用（預設排除）")
    a = ap.parse_args()

    rows = load_rows(a.include_mis)
    data = with_forward(rows, a.horizon)
    if len(data) < 60:
        raise SystemExit(f"可用樣本只有 {len(data)} 筆，至少要 60 筆（多跑幾天 monitor 再來）")
    cnt = defaultdict(int)
    for r in data:
        for n, _ in r["DF"] + r["IF"]:
            cnt[n] += 1
    names = sorted(n for n, c in cnt.items() if c >= a.min_n)
    days = sorted({r["date"] for r in data})
    print(f"樣本 {len(data)} 筆（{len(days)} 天：{days[0]}–{days[-1]}，{len({r['code'] for r in data})} 檔），答案 = {a.horizon} 分鐘後漲跌；可演化因子 {len(names)} 個（出現 ≥{a.min_n} 次）")

    cur_path = os.path.join(HERE, "model_params.json")
    base = {"m": {n: 1.0 for n in names}, "scale": 11.0, "daily_w": 0.8}
    if os.path.exists(cur_path):
        try:
            cur = json.load(open(cur_path, encoding="utf-8"))
            base["m"].update({n: float(v) for n, v in (cur.get("multipliers") or {}).items() if n in base["m"]})
            base["scale"] = float(cur.get("scale") or 11); base["daily_w"] = float(cur.get("daily_w") or 0.8)
            print(f"已載入現行 model_params.json（{cur.get('generated', '?')}）當起點")
        except Exception:
            pass

    # walk-forward：最後一天當測試
    train = [r for r in data if r["date"] != days[-1]] if len(days) >= 3 else data
    test = [r for r in data if r["date"] == days[-1]] if len(days) >= 3 else []
    print(f"\n[1] 訓練 {len(train)} 筆{f'，測試（{days[-1]}）{len(test)} 筆' if test else ''}")
    best_tr = evolve(names, train, base, a.gens, a.pop, a.lam, a.seed)
    b0, b1 = report(base, names, train), report(best_tr, names, train)
    print(f"  訓練：Brier {b0['brier']:.4f} → {b1['brier']:.4f}，命中率(|p-50|≥10) {b0['hit_rate'] and round(b0['hit_rate'])}% → {b1['hit_rate'] and round(b1['hit_rate'])}%，IC {b0['ic']:+.3f} → {b1['ic']:+.3f}")
    if test:
        t0, t1 = report(base, names, test), report(best_tr, names, test)
        print(f"  測試：Brier {t0['brier']:.4f} → {t1['brier']:.4f}，命中率 {t0['hit_rate'] and round(t0['hit_rate'])}% → {t1['hit_rate'] and round(t1['hit_rate'])}%，IC {t0['ic']:+.3f} → {t1['ic']:+.3f}"
              + ("   ⚠ 測試變差：過擬合，不要 --apply" if t1["brier"] > t0["brier"] + 0.002 else "   ✔ 測試沒變差"))

    print(f"\n[2] 全部資料再演化一次（產出用）")
    best = evolve(names, data, base, a.gens, a.pop, a.lam, a.seed + 1)
    f0, f1 = report(base, names, data), report(best, names, data)
    print(f"  全部：Brier {f0['brier']:.4f} → {f1['brier']:.4f}，命中率 {f0['hit_rate'] and round(f0['hit_rate'])}% → {f1['hit_rate'] and round(f1['hit_rate'])}%，IC {f0['ic']:+.3f} → {f1['ic']:+.3f}")

    # 因子表
    wsum = defaultdict(float); fsum = defaultdict(float)
    for r in data:
        for n, w in r["DF"] + r["IF"]:
            wsum[n] += w; fsum[n] += (1 if (w > 0) == (r["fwd"] > 0) else 0) if w else 0
    print(f"\n{'因子':<12}{'n':>5}{'原權重':>8}{'方向命中':>9}{'倍數':>7}  備註")
    for n in sorted(names, key=lambda n: -abs(best['m'][n] - 1)):
        c = cnt[n]; avgw = wsum[n] / c
        hit = fsum[n] / c * 100 if avgw else float("nan")
        mult = best["m"][n]
        note = "關掉" if mult < 0.15 else ("加重" if mult > 1.4 else ("減輕" if mult < 0.6 else ""))
        print(f"{n:<12}{c:>5}{avgw:>+8.2f}{hit:>8.0f}%{mult:>7.2f}  {note}")
    print(f"\nscale {base['scale']:.1f} → {best['scale']:.1f}，daily_w {base['daily_w']:.2f} → {best['daily_w']:.2f}")

    out = {"generated": dt.datetime.now().isoformat(timespec="seconds"), "horizon_min": a.horizon, "n_samples": len(data), "days": days,
           "multipliers": {n: round(v, 3) for n, v in best["m"].items()}, "scale": round(best["scale"], 2), "daily_w": round(best["daily_w"], 3),
           "fit": {"before": f0, "after": f1, "test_before": (report(base, names, test) if test else None), "test_after": (report(best_tr, names, test) if test else None)}}
    cand = os.path.join(HERE, "model_params.candidate.json")
    json.dump(out, open(cand, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n候選參數已寫 {cand}")
    if a.apply:
        if len(data) < 300:
            print("樣本 <300，拒絕 --apply（先多跑幾天）")
        else:
            json.dump(out, open(cur_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            print(f"已套用 → {cur_path}（monitor 下一輪估算自動載入；要還原就刪掉這個檔）")


if __name__ == "__main__":
    main()
