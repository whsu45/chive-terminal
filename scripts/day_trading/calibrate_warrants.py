#!/usr/bin/env python3
"""
驗證「權證做多標的容易漲、做空容易跌」：
對每檔、每個交易日取
  call_share   = 認購成交張數 / (認購+認售)
  call_surge   = 當日認購成交金額 / 前 5 日平均
  hedge_pct    = 自營商(避險)買賣超 / 當日成交量
對照 (1) 同一天標的漲跌（同步關係）、(2) 隔日標的漲跌（預測力）。
    python3 calibrate_warrants.py            # 預設股票池、近 42 個交易日
    python3 calibrate_warrants.py 3042 2409 --days 42
"""
import argparse
import math
import src_warrants as W
import src_twse, src_tpex, src_mis
import tw_calendar as cal
from calibrate import spearman, DEFAULT_CODES


def bucket(sig, ret, lo, hi):
    xs = [r for s, r in zip(sig, ret) if s is not None and r is not None and lo <= s < hi]
    if not xs:
        return "-"
    return f"n={len(xs)} 漲{sum(1 for r in xs if r > 0)/len(xs)*100:.0f}% 均{sum(xs)/len(xs):+.2f}%"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("codes", nargs="*", default=DEFAULT_CODES)
    ap.add_argument("--days", type=int, default=42)
    a = ap.parse_args()
    today = cal.today_tw()
    days = sorted(cal.last_n_trading_days(today, a.days))
    mk = {}
    for c in a.codes:
        q = src_mis.fetch([f"tse_{c}.tw", f"otc_{c}.tw"]).get(c)
        mk[c] = (q["ex"], q["name"]) if q else ("tse", c)
    twse_tables = {}
    rows_all = []  # (code, date, call_share, call_surge, hedge_pct, r0, r1)
    for c in a.codes:
        ex, name = mk[c]
        hist = (src_twse.daily_history if ex == "tse" else src_tpex.daily_history)(c, 4)
        closes = {r["date"]: r for r in hist}
        ds = [d for d in days if d.isoformat() in closes]
        amts = []
        for i, d in enumerate(ds):
            if ex == "tse":
                if d not in twse_tables:
                    twse_tables[d] = W.twse_warrants_daily(d)
                wd = W.warrants_for(c, name, ex, d, twse_tables[d])
            else:
                wd = W.warrants_for(c, name, ex, d)
            s = W.summarize(wd)
            hp = W.hedge_net(d, c, ex)
            k = closes[d.isoformat()]
            prev = closes.get(ds[i - 1].isoformat()) if i > 0 else None
            nxt = closes.get(ds[i + 1].isoformat()) if i + 1 < len(ds) else None
            r0 = (k["c"] / prev["c"] - 1) * 100 if prev else None
            r1 = (nxt["c"] / k["c"] - 1) * 100 if nxt else None
            surge = (s["call_amt"] / (sum(amts[-5:]) / len(amts[-5:]))) if len(amts) >= 3 and sum(amts[-5:]) > 0 else None
            amts.append(s["call_amt"])
            rows_all.append((c, d.isoformat(), s["call_share"], surge, hp / k["v"] * 100 if k["v"] else None, r0, r1, s["call_vol"], s["put_vol"], hp))
        tot_c = sum(r[7] for r in rows_all if r[0] == c); tot_p = sum(r[8] for r in rows_all if r[0] == c)
        print(f"  {c} {name}: {len(ds)} 天，認購 {tot_c} 張 / 認售 {tot_p} 張（認購佔 {tot_c/(tot_c+tot_p)*100 if tot_c+tot_p else 0:.0f}%），避險淨合計 {sum(r[9] for r in rows_all if r[0]==c):+} 張")

    def report(title, idx, same_day_desc):
        print(f"\n=== {title}")
        for tgt_i, tgt_name in ((5, "同日標的漲跌"), (6, "隔日標的漲跌")):
            pairs = [(r[idx], r[tgt_i]) for r in rows_all if r[idx] is not None and r[tgt_i] is not None]
            ic = spearman([p[0] for p in pairs], [p[1] for p in pairs])
            print(f"  vs {tgt_name}: n={len(pairs)}  Spearman IC = {ic:+.3f}" if ic is not None else f"  vs {tgt_name}: 樣本不足")
        sig = [r[idx] for r in rows_all]; r1 = [r[6] for r in rows_all]; r0 = [r[5] for r in rows_all]
        return sig, r0, r1

    sig, r0, r1 = report("認購佔比 call_share（認購張數 /(認購+認售)）", 2, "")
    print("  分桶 → 隔日：", " | ".join(f"{lo:.1f}~{hi:.1f} {bucket(sig, r1, lo, hi)}" for lo, hi in ((0, .5), (.5, .8), (.8, .95), (.95, 1.01))))
    print("  分桶 → 同日：", " | ".join(f"{lo:.1f}~{hi:.1f} {bucket(sig, r0, lo, hi)}" for lo, hi in ((0, .5), (.5, .8), (.8, .95), (.95, 1.01))))
    sig, r0, r1 = report("認購金額爆量 call_surge（當日認購金額 / 前 5 日均）", 3, "")
    print("  分桶 → 隔日：", " | ".join(f"{lo}~{hi} {bucket(sig, r1, lo, hi)}" for lo, hi in ((0, .7), (.7, 1.5), (1.5, 3), (3, 99))))
    print("  分桶 → 同日：", " | ".join(f"{lo}~{hi} {bucket(sig, r0, lo, hi)}" for lo, hi in ((0, .7), (.7, 1.5), (1.5, 3), (3, 99))))
    sig, r0, r1 = report("發行商避險淨買 hedge_pct（自營商避險買賣超 / 成交量 %）", 4, "")
    print("  分桶 → 隔日：", " | ".join(f"{lo}~{hi} {bucket(sig, r1, lo, hi)}" for lo, hi in ((-99, -2), (-2, 0), (0, 2), (2, 99))))
    print("  分桶 → 同日：", " | ".join(f"{lo}~{hi} {bucket(sig, r0, lo, hi)}" for lo, hi in ((-99, -2), (-2, 0), (0, 2), (2, 99))))


if __name__ == "__main__":
    main()
