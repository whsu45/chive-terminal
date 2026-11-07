#!/usr/bin/env python3
"""
用永豐歷史逐筆校準「大單 / 散戶」金額門檻（BIG_LOT_AMOUNT / SMALL_LOT_AMOUNT）。

對每檔、每個交易日抓逐筆，掃一組金額門檻，算：
  A) 開盤 09:00–09:20 的主力淨買% / 散戶淨買%  → 預測 09:20→09:50 報酬（當沖 09:50 出場的視角）
  B) 全日主力淨買% / 散戶淨買%                → 預測隔日收盤報酬
用 Spearman 等級相關 (IC) 與命中率評分；主力訊號要「正相關」，散戶訊號要「負相關」（反指標）。

    python3 calibrate.py                          # 預設股票池，近兩個月
    python3 calibrate.py 3042 2344 2409 --days 40
    python3 calibrate.py --apply                  # 把建議值寫進 .env（只改那兩行）

逐筆快取在 .cache/ticks/，重跑不再耗流量。歷史逐筆不受「盤中 10 次」限制，但有每日流量上限；
程式每 20 次請求檢查一次剩餘流量，低於 30MB 自動停止。
"""
import os
import sys
import json
import time
import math
import argparse
import datetime as dt

from config import CACHE_DIR, ENV_FILE, load_env, DEVICE_ID
import tw_calendar as cal
import src_shioaji as S

TICK_DIR = os.path.join(CACHE_DIR, "ticks")
os.makedirs(TICK_DIR, exist_ok=True)

DEFAULT_CODES = ["3042", "2344", "3707", "7704", "2301", "2352", "2409", "2330", "2454"]
BIG_GRID = [1e6, 2e6, 3e6, 5e6, 8e6, 1e7, 2e7]
SMALL_GRID = [2e5, 5e5, 1e6, 1.5e6, 2e6]


def load_ticks(api, code, date):
    """回 [(sec_of_day, price, vol, tick_type)]，快取成 JSON。"""
    path = os.path.join(TICK_DIR, f"{code}_{date.isoformat()}.json")
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    c = S.get_contract(api, code)
    t = S.fetch_ticks(api, c, date.isoformat(), "校準") if hasattr(S, "fetch_ticks") else api.ticks(contract=c, date=date.isoformat())
    rows = []
    for ts, p, v, tt in zip(t.ts, t.close, t.volume, t.tick_type):
        d = S.ns_to_dt(ts)
        rows.append([d.hour * 3600 + d.minute * 60 + d.second, float(p), int(v), int(tt)])
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rows, f)
    return rows


def price_at(rows, sec):
    """<= sec 的最後成交價。"""
    last = None
    for s, p, v, tt in rows:
        if s > sec:
            break
        last = p
    return last


def flow_pct(rows, s0, s1, big_amt, small_amt):
    """區間內主力淨買%、散戶淨買%（以金額分類，1 張單一律散戶）。"""
    bb = bs = sb = ss = vol = 0
    for s, p, v, tt in rows:
        if s < s0 or s >= s1 or tt not in (1, 2):
            continue
        vol += v
        amt = p * v * 1000
        if amt >= big_amt:
            if tt == 1: bb += v
            else: bs += v
        elif amt < small_amt or v == 1:
            if tt == 1: sb += v
            else: ss += v
    if vol == 0:
        return None, None
    return (bb - bs) / vol * 100, (sb - ss) / vol * 100


def spearman(x, y):
    n = len(x)
    if n < 8:
        return None
    def rank(a):
        order = sorted(range(n), key=lambda i: a[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and a[order[j + 1]] == a[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r
    rx, ry = rank(x), rank(y)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = math.sqrt(sum((a - mx) ** 2 for a in rx)); vy = math.sqrt(sum((b - my) ** 2 for b in ry))
    return cov / (vx * vy) if vx and vy else None


def hit_rate(sig, ret, thr):
    pairs = [(s, r) for s, r in zip(sig, ret) if abs(s) >= thr and r != 0]
    if len(pairs) < 5:
        return None, len(pairs)
    return sum(1 for s, r in pairs if (s > 0) == (r > 0)) / len(pairs), len(pairs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("codes", nargs="*", default=DEFAULT_CODES)
    ap.add_argument("--days", type=int, default=42, help="往前幾個交易日（預設約兩個月）")
    ap.add_argument("--start", default=None, help="起始日 YYYY-MM-DD（預設兩個月前）")
    ap.add_argument("--apply", action="store_true", help="把建議值寫入 .env")
    a = ap.parse_args()
    load_env()
    api = S.login()
    if api is None:
        raise SystemExit("永豐登入失敗，無法校準（需要 .env 金鑰與 shioaji 套件）。")
    today = cal.today_tw()
    days = sorted(cal.last_n_trading_days(today, a.days))
    if a.start:
        days = [d for d in days if d.isoformat() >= a.start]
    print(f"股票 {a.codes}，交易日 {days[0]} ~ {days[-1]}（{len(days)} 天）")

    # ---- 抓逐筆 ----
    data = {}  # code -> {date: rows}
    req = 0
    for code in a.codes:
        data[code] = {}
        for d in days:
            path = os.path.join(TICK_DIR, f"{code}_{d.isoformat()}.json")
            cached = os.path.exists(path)
            try:
                rows = load_ticks(api, code, d)
            except Exception as e:
                print(f"  [warn] {code} {d}: {str(e)[:80]}")
                continue
            if rows:
                data[code][d.isoformat()] = rows
            if not cached:
                req += 1
                time.sleep(0.3)
                if req % 20 == 0:
                    u = S.usage()
                    print(f"  ...已請求 {req} 次，流量 {u['used_mb']:.1f}/{u['limit_mb']:.0f} MB" if u else f"  ...已請求 {req} 次")
                    if u and u["remaining_mb"] < 30:
                        print("  剩餘流量不足 30MB，停止抓取，以已取得資料計算。")
                        break
        n = len(data[code])
        sizes = sorted(v for rows in data[code].values() for _, _, v, _ in rows) or [0]
        px = next((rows[-1][1] for rows in reversed(list(data[code].values())) if rows), 0)
        q = lambda f: sizes[min(len(sizes) - 1, int(len(sizes) * f))]
        print(f"  {code}: {n} 天逐筆，單筆張數 P50 {q(.5)} / P90 {q(.9)} / P95 {q(.95)} / P99 {q(.99)}，1 張 ≈ {px*1000/1e4:.0f} 萬 → 500萬 = {math.ceil(5e6/(px*1000)) if px else '-'} 張")
    S.logout()

    # ---- 建樣本 ----
    T0920, T0950, T0900 = 9 * 3600 + 20 * 60, 9 * 3600 + 50 * 60, 9 * 3600
    results = {"A": {}, "B": {}}
    for code, bydate in data.items():
        ds = sorted(bydate)
        for i, d in enumerate(ds):
            rows = bydate[d]
            p0920, p0950 = price_at(rows, T0920), price_at(rows, T0950)
            rA = (p0950 / p0920 - 1) * 100 if p0920 and p0950 else None
            close = rows[-1][1]
            rB = (bydate[ds[i + 1]][-1][1] / close - 1) * 100 if i + 1 < len(ds) and close else None
            for big in BIG_GRID:
                for small in SMALL_GRID:
                    if small >= big:
                        continue
                    key = (big, small)
                    if rA is not None:
                        b, s = flow_pct(rows, T0900, T0920, big, small)
                        if b is not None:
                            results["A"].setdefault(key, []).append((b, s, rA))
                    if rB is not None:
                        b, s = flow_pct(rows, T0900, 13 * 3600 + 31 * 60, big, small)
                        if b is not None:
                            results["B"].setdefault(key, []).append((b, s, rB))

    # ---- 評分 ----
    def table(name, res, desc):
        print(f"\n=== {name}：{desc}")
        print(f"{'大單門檻':>8} {'散戶門檻':>8} {'樣本':>5} {'IC主力':>7} {'命中%(|主力|≥5)':>15} {'IC散戶':>7} {'命中%(散戶反向≥5)':>17} {'綜合':>6}")
        scored = []
        for (big, small), rows in sorted(res.items()):
            B = [r[0] for r in rows]; Sm = [r[1] for r in rows]; R = [r[2] for r in rows]
            icb = spearman(B, R); ics = spearman(Sm, R)
            hb, nb = hit_rate(B, R, 5)
            hs, ns_ = hit_rate([-x for x in Sm], R, 5)
            comp = (icb or 0) - (ics or 0)
            scored.append((comp, big, small, len(rows), icb, hb, nb, ics, hs, ns_))
        for comp, big, small, n, icb, hb, nb, ics, hs, ns_ in sorted(scored, key=lambda x: -x[0]):
            print(f"{big/1e4:>7.0f}萬 {small/1e4:>7.0f}萬 {n:>5} {icb if icb is None else f'{icb:+.3f}':>7} "
                  f"{'-' if hb is None else f'{hb*100:.0f}% ({nb})':>15} {ics if ics is None else f'{ics:+.3f}':>7} "
                  f"{'-' if hs is None else f'{hs*100:.0f}% ({ns_})':>17} {comp:+.3f}")
        return sorted(scored, key=lambda x: -x[0])

    bestA = table("A 當沖視角", results["A"], "09:00–09:20 分流 → 09:20→09:50 報酬  (IC = Spearman；綜合 = IC主力 − IC散戶)")
    bestB = table("B 隔日視角", results["B"], "全日分流 → 隔日收盤報酬")

    # 大單門檻看主力 IC、散戶門檻看散戶 IC，兩者分開選（當沖視角優先）
    def pick(scored, idx_ic, sign):
        agg = {}
        for row in scored:
            k = row[1] if idx_ic == 4 else row[2]
            v = row[idx_ic]
            if v is not None:
                agg.setdefault(k, []).append(sign * v)
        return sorted(((sum(v) / len(v), k) for k, v in agg.items()), reverse=True)
    big_rank = pick(bestA, 4, +1)
    small_rank = pick(bestA, 7, -1)
    rec_big, rec_small = big_rank[0][1], small_rank[0][1]
    if rec_small >= rec_big:
        rec_small = max(s for s in SMALL_GRID if s < rec_big)
    print("\n=== 建議（當沖視角 A）")
    print("  大單門檻各值的平均 IC：" + "  ".join(f"{k/1e4:.0f}萬 {v:+.3f}" for v, k in sorted(big_rank, key=lambda x: x[1])))
    print("  散戶門檻各值的平均反向 IC：" + "  ".join(f"{k/1e4:.0f}萬 {v:+.3f}" for v, k in sorted(small_rank, key=lambda x: x[1])))
    print(f"  → BIG_LOT_AMOUNT={int(rec_big)}  SMALL_LOT_AMOUNT={int(rec_small)}")
    out = {"date": today.isoformat(), "codes": a.codes, "days": [d.isoformat() for d in days],
           "A": [list(r) for r in bestA], "B": [list(r) for r in bestB], "rec_big": rec_big, "rec_small": rec_small}
    with open(os.path.join(CACHE_DIR, f"calibration_{today:%Y%m%d}_{DEVICE_ID}.json"), "w") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, default=str)
    if a.apply and os.path.exists(ENV_FILE):
        lines = open(ENV_FILE, encoding="utf-8").read().splitlines()
        seen = set()
        for i, ln in enumerate(lines):
            if ln.startswith("BIG_LOT_AMOUNT="):
                lines[i] = f"BIG_LOT_AMOUNT={int(rec_big)}"; seen.add("b")
            elif ln.startswith("SMALL_LOT_AMOUNT="):
                lines[i] = f"SMALL_LOT_AMOUNT={int(rec_small)}"; seen.add("s")
        if "b" not in seen: lines.append(f"BIG_LOT_AMOUNT={int(rec_big)}")
        if "s" not in seen: lines.append(f"SMALL_LOT_AMOUNT={int(rec_small)}")
        with open(ENV_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print("  已寫入 .env（只更動這兩行）")


if __name__ == "__main__":
    main()
