#!/usr/bin/env python3
"""
A/B 回測：同一批 K 棒同時跑 Model_A 與 Model_B，扣台股現股當沖成本後比較。
    python3 ab/backtest_ab.py local              # 本機逐筆（.cache/ticks，9 檔 × 42 天，1 分 K + 內外盤）
    python3 ab/backtest_ab.py stocker            # tw_stocker 5 分 K（.cache/tw_stocker/csv，2024-02 ~ 2026-04）
    python3 ab/backtest_ab.py index              # 0050 / 006208 5 分 K 當台指代理，用小台期貨成本（B 用 for_index 版）
    python3 ab/backtest_ab.py futures            # 永豐 TXFR1 台指期近月 1 分 K 日盤（2020-03 起，ab/sj_tx_bars.py 抓）
    python3 ab/backtest_ab.py taifex             # 期交所前 30 日逐筆 → 台指期近月 1 分 K（ab/taifex_bars.py）
    python3 ab/backtest_ab.py stocker --discount 0.28 --slip 1 --detail
成本：手續費 0.1425%×2×折扣 + 當沖證交稅 0.15% + 每邊滑價 N 檔。t 值用「同一天所有交易先平均」再算（同日多檔高度相關）。
"""
import os
import sys
import csv
import json
import glob
import math
import argparse
import datetime as dt
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from ab_models import Model_A, Model_B, CostModel  # noqa: E402

CACHE = os.path.join(os.path.dirname(HERE), ".cache")


def load_local():
    """.cache/ticks/CODE_DATE.json：[秒, 價, 量, 內外盤(2 外 / 1 內 / 0 競價)] → 每檔 1 分 K（含 buy_vol / sell_vol）。"""
    by = defaultdict(list)
    for p in sorted(glob.glob(os.path.join(CACHE, "ticks", "*.json"))):
        code, date = os.path.basename(p)[:-5].split("_")
        d = dt.date.fromisoformat(date)
        try:
            rows = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        bars = {}
        for sec, px, v, *rest in rows:
            if not (32400 <= sec <= 48600):
                continue
            m = min(sec // 60, 809)
            side = rest[0] if rest else 0
            b = bars.get(m)
            if b is None:
                b = bars[m] = {"ts": dt.datetime.combine(d, dt.time(m // 60, m % 60)), "open": px, "high": px,
                               "low": px, "close": px, "volume": 0.0, "buy_vol": 0.0, "sell_vol": 0.0}
            b["high"] = max(b["high"], px); b["low"] = min(b["low"], px); b["close"] = px; b["volume"] += v
            if side == 2:
                b["buy_vol"] += v
            elif side == 1:
                b["sell_vol"] += v
        if len(bars) >= 60:
            by[code] += [bars[k] for k in sorted(bars)]
    return by


def load_index():
    """tw_stocker 的 0050 / 006208（.cache/tw_stocker/idx）當加權指數代理。"""
    by = {}
    for t in ("0050", "006208"):
        p = os.path.join(CACHE, "tw_stocker", "idx", t + ".csv")
        if not os.path.exists(p):
            continue
        out = []
        with open(p, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                try:
                    out.append({"ts": dt.datetime.fromisoformat(r["Datetime"]), "open": float(r["Open"]), "high": float(r["High"]),
                                "low": float(r["Low"]), "close": float(r["Close"]), "volume": float(r["Volume"] or 0)})
                except (ValueError, KeyError):
                    continue
        by[t] = out
    return by


def load_futures(src="sj"):
    """台指期日盤 1 分 K：sj = 永豐 TXFR1（.cache/sj_kbars/TXFR1_1m.json）；taifex = 期交所逐筆（.cache/taifex_ticks/TX_1m.json）。"""
    p = os.path.join(CACHE, "sj_kbars", "TXFR1_1m.json") if src == "sj" else os.path.join(CACHE, "taifex_ticks", "TX_1m.json")
    d = json.load(open(p))
    return {"TX": [dict(r, ts=dt.datetime.fromisoformat(r["ts"])) for r in d["day"]]}


def load_stocker(limit=None, codes=None):
    """tw_stocker CSV（yfinance 5 分 K，時區混 +08:00 / +00:00、2025-05/06 重複十倍）→ 每檔 list[dict]。"""
    by = {}
    files = sorted(glob.glob(os.path.join(CACHE, "tw_stocker", "csv", "*.csv")))
    for p in files[:limit]:
        code = os.path.basename(p)[:-4]
        if codes is not None and code not in codes:
            continue
        out = []
        with open(p, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                try:
                    ts = dt.datetime.fromisoformat(r["Datetime"])
                    out.append({"ts": ts, "open": float(r["Open"]), "high": float(r["High"]), "low": float(r["Low"]),
                                "close": float(r["Close"]), "volume": float(r["Volume"] or 0)})
                except (ValueError, KeyError):
                    continue
        if out:
            by[code] = out
    return by


def tstat(xs):
    n = len(xs)
    if n < 3:
        return float("nan")
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    return m / (sd / math.sqrt(n)) if sd else float("nan")


def summarize(trades, cost):
    if not trades:
        return None
    net = [t.net_pct(cost) for t in trades]
    gross = [t.gross_pct() for t in trades]
    wins = [x for x in net if x > 0]; losses = [x for x in net if x <= 0]
    byday = defaultdict(list)
    for t, x in zip(trades, net):
        byday[t.date].append(x)
    daily = [sum(v) / len(v) for v in byday.values()]
    pf = sum(wins) / -sum(losses) if losses and sum(losses) < 0 else float("inf")
    return {"n": len(net), "days": len(byday), "wr": len(wins) / len(net) * 100,
            "avg_win": sum(wins) / len(wins) if wins else 0.0, "avg_loss": sum(losses) / len(losses) if losses else 0.0,
            "payoff": (sum(wins) / len(wins)) / -(sum(losses) / len(losses)) if wins and losses and sum(losses) < 0 else float("nan"),
            "gross": sum(gross) / len(gross), "net": sum(net) / len(net), "pf": pf, "t_day": tstat(daily),
            "cost": sum(g - n for g, n in zip(gross, net)) / len(net)}


def fmt(name, s):
    if not s:
        return f"{name:<16} 沒有交易"
    return (f"{name:<16}{s['n']:>6}{s['days']:>6}{s['wr']:>7.1f}%{s['avg_win']:>+8.2f}%{s['avg_loss']:>+8.2f}%"
            f"{s['payoff']:>7.2f}{s['pf']:>6.2f}{s['gross']:>+8.3f}%{s['cost']:>7.3f}%{s['net']:>+8.3f}%{s['t_day']:>7.2f}")


HDR = f"{'':<16}{'筆數':>6}{'天數':>6}{'勝率':>8}{'均賺':>9}{'均賠':>9}{'盈虧比':>7}{'PF':>6}{'毛/筆':>9}{'成本':>8}{'淨/筆':>9}{'t(日)':>7}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset", choices=["local", "stocker", "index", "futures", "taifex"])
    ap.add_argument("--discount", type=float, default=0.3, help="手續費折扣（預設 3 折）")
    ap.add_argument("--slip", type=float, default=1.0, help="每邊滑價幾檔（預設 1）")
    ap.add_argument("--limit", type=int, default=None, help="stocker 只跑前幾檔（測試用）")
    ap.add_argument("--split", default="2025-03-01", help="stocker 前後段切點（看參數是否只在某段有效）")
    ap.add_argument("--b", default="", help="Model_B 參數覆寫，例如 'k=1.2,rvol_min=1.5'")
    ap.add_argument("--slip-points", type=float, default=1.0, help="期貨每邊滑價幾點（預設 1）")
    ap.add_argument("--fee-ntd", type=float, default=30.0, help="期貨每口每邊手續費（預設小台 30 元）")
    ap.add_argument("--by-year", action="store_true", help="依年份拆開看")
    ap.add_argument("--detail", action="store_true")
    a = ap.parse_args()
    fut = a.dataset in ("index", "futures", "taifex")
    cost = CostModel.taiex_futures(fee_ntd=a.fee_ntd, slip_points=a.slip_points) if fut else CostModel(discount=a.discount, slip_ticks=a.slip)
    bkw = {}
    for kv in filter(None, a.b.split(",")):
        k, v = kv.split("=")
        bkw[k] = type(Model_B.defaults[k])(v)
    data = {"local": load_local, "stocker": lambda: load_stocker(a.limit), "index": load_index,
            "futures": lambda: load_futures("sj"), "taifex": lambda: load_futures("taifex")}[a.dataset]()
    if a.dataset in ("futures", "taifex"):
        models = [Model_A.for_futures(), Model_B.for_futures(**bkw)]
    elif a.dataset == "index":
        models = [Model_A(), Model_B.for_index(**bkw)]
    else:
        models = [Model_A(), Model_B(**bkw)]
    allt = {m.name: [] for m in models}
    for code, rows in sorted(data.items()):
        for m in models:
            for t in m.run(rows).trades:
                t.code = code
                allt[m.name].append(t)
    ndays = sum(len({r["ts"].date() if isinstance(r["ts"], dt.datetime) else None for r in rows}) for rows in data.values())
    if fut:
        lvl = 45000 if a.dataset != "index" else 190
        print(f"資料：{a.dataset}，{ndays} 個日；成本 = 小台每口每邊 {a.fee_ntd:g} 元 + 期交稅 0.002%×2 + 每邊 {a.slip_points:g} 點滑價"
              f"（指數 45,000 點時來回 {CostModel.taiex_futures(a.fee_ntd, 50, a.slip_points).round_trip_pct(45000):.4f}%）")
    else:
        print(f"資料：{a.dataset}，{len(data)} 檔、約 {ndays} 個股票日；成本 = 手續費 0.1425%×2×{a.discount} + 證交稅 0.15% + 每邊 {a.slip:g} 檔滑價")
    if bkw:
        print(f"Model_B 參數覆寫：{bkw}")
    print(HDR)
    for name, ts in allt.items():
        print(fmt(f"Model_{name}", summarize(ts, cost)))
        for side in ("long", "short"):
            print(fmt(f"  {'多' if side == 'long' else '空'}", summarize([t for t in ts if t.side == side], cost)))
        if a.by_year:
            for y in sorted({t.date.year for t in ts}):
                print(fmt(f"  {y}", summarize([t for t in ts if t.date.year == y], cost)))
        elif a.dataset in ("stocker", "index", "futures"):
            sp = dt.date.fromisoformat(a.split)
            print(fmt(f"  {a.split} 前", summarize([t for t in ts if t.date < sp], cost)))
            print(fmt(f"  {a.split} 後", summarize([t for t in ts if t.date >= sp], cost)))
        reasons = defaultdict(list)
        for t in ts:
            reasons[t.reason.split(" ")[0][:8]].append(t)
        for r, rt in sorted(reasons.items(), key=lambda x: -len(x[1])):
            print(fmt(f"  出場:{r}", summarize(rt, cost)))
        if name == "B":
            adds = [t for t in ts if len(t.legs) > 1]
            print(fmt("  有加碼", summarize(adds, cost)))
            print(fmt("  沒加碼", summarize([t for t in ts if len(t.legs) == 1], cost)))
        print()
    print("淨/筆 = 扣成本後每筆平均報酬（以進場價 %，加碼的 0.5 單位一起算進該筆）；t(日) = 每天平均後的 t 值；PF = 總賺 / 總賠")
    if a.detail:
        for name, ts in allt.items():
            for t in ts[:400]:
                print(f"{name} {t.code} {t.entry_ts:%Y-%m-%d %H:%M} {t.side:<5} {t.entry:>8} 停 {t.stop0:>8} → {t.exit_ts:%H:%M} {t.exit:>8} "
                      f"{t.reason:<14} 毛 {t.gross_pct():+.2f}% 淨 {t.net_pct(cost):+.2f}%")


if __name__ == "__main__":
    main()
