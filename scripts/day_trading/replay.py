#!/usr/bin/env python3
"""
盤後重播：用永豐當日逐筆重建「如果 monitor 從 09:00 開始跑」每 N 分鐘會給出的判斷，供復盤。
    python3 replay.py 1815                 # 今天
    python3 replay.py 1815 --date 2026-10-30 --step 5
需要永豐金鑰（當日逐筆 1 次、指數與族群 K 線各 1 次）。輸出同 monitor 的時間線 + 主力/散戶/藏單分流。
"""
import argparse
import datetime as dt
import json
import os

from config import LOG_DIR, load_env
import prep
import model
import src_shioaji as S
import indicators as I


def build_quote(code, name, ex, ticks, y, upto, limit_up, limit_down):
    """用 <= upto 的逐筆重建 MIS 風格 quote。"""
    sub = [t for t in ticks if t[0] <= upto]
    if not sub:
        return None
    prices = [t[1] for t in sub]
    return {"code": code, "name": name, "ex": ex, "t": upto.strftime("%H:%M:%S"), "z": prices[-1], "ref": prices[-1],
            "o": prices[0], "h": max(prices), "l": min(prices), "y": y, "v": sum(t[2] for t in sub), "u": limit_up, "w": limit_down,
            "bids": [], "asks": []}


def kbar_quote(kb, y, upto):
    """指數/族群用 1 分 K 重建。kb: [(ts, o,h,l,c,v)]"""
    sub = [k for k in kb if k[0] <= upto]
    if not sub:
        return None
    return {"ref": sub[-1][4], "y": y, "o": sub[0][1], "h": max(k[2] for k in sub), "l": min(k[3] for k in sub), "v": sum(k[5] for k in sub)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("code")
    ap.add_argument("--date", default=None)
    ap.add_argument("--step", type=int, default=3, help="分鐘")
    ap.add_argument("--peers", default=None)
    ap.add_argument("--until", default="13:30")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    load_env()
    date = dt.date.fromisoformat(a.date) if a.date else dt.date.today()
    d = prep.build(a.code, a.peers)
    print(prep.describe(d))
    api = S.login()
    if api is None:
        raise SystemExit("需要永豐金鑰才能重播。")
    code = a.code
    c = S.get_contract(api, code)
    t = api.ticks(contract=c, date=date.isoformat())
    ticks = [(S.ns_to_dt(ts), float(p), int(v), int(tt)) for ts, p, v, tt in zip(t.ts, t.close, t.volume, t.tick_type)]
    ticks = [x for x in ticks if x[0].time() >= dt.time(9, 0)]
    if not ticks:
        raise SystemExit("當日無逐筆資料")
    y = (d.get("tech") or {}).get("close")
    # 指數與族群 1 分 K
    def kbars(contract):
        try:
            k = api.kbars(contract=contract, start=date.isoformat(), end=date.isoformat())
            return [(dt.datetime.fromisoformat(str(ts)) if not isinstance(ts, (int, float)) else S.ns_to_dt(ts), o, h, l, cl, v)
                    for ts, o, h, l, cl, v in zip(k.ts, k.Open, k.High, k.Low, k.Close, k.Volume)]
        except Exception as e:
            print(f"  [warn] kbars {getattr(contract, 'code', '?')}: {str(e)[:80]}")
            return []
    idx_contract = api.Contracts.Indexs.TSE["IX0001"] if d["market"] == "tse" else api.Contracts.Indexs.OTC["IX0043"]
    idx_kb = kbars(idx_contract)
    idx_y = None
    try:
        idx_y = float(idx_contract.reference)
    except Exception:
        pass
    peers_kb = {}
    for p in d["peers"]:
        pc = S.get_contract(api, p)
        if pc is not None:
            peers_kb[p] = (kbars(pc), float(getattr(pc, "reference", 0) or 0) or d["peer_info"].get(p, {}).get("y"), d["peer_info"].get(p, {}).get("name", p))
    S.logout()

    # 把逐筆塞進 src_shioaji 的累積器，讓 flow() 能依時間窗計算
    S._state[code] = S._new_acc()
    acc = S._state[code]
    out_path = os.path.join(LOG_DIR, f"replay_{date:%Y%m%d}_{code}.jsonl")
    samples = []
    print(f"\n=== 重播 {code} {d['name']} {date}  每 {a.step} 分鐘")
    print(f"{'時間':6} {'價':>7} {'漲跌%':>7} {'大盤%':>7} {'族群%':>7} {'量':>7} {'主力淨%':>7} {'散戶淨%':>7} {'藏單':>6} {'多%':>4} {'空%':>4}  判斷 / 主要因子")
    cur = dt.datetime.combine(date, dt.time(9, a.step))
    end = dt.datetime.combine(date, dt.time(*map(int, a.until.split(":"))))
    with open(out_path, "w", encoding="utf-8") as f:
        while cur <= end:
            acc["ticks"] = [x for x in ticks if x[0] <= cur]
            acc["last"] = acc["ticks"][-1][1] if acc["ticks"] else None
            q = build_quote(code, d["name"], d["market"], ticks, y, cur, d["limit_up"], d["limit_down"])
            if q is None:
                cur += dt.timedelta(minutes=a.step); continue
            samples.append((cur, q["ref"], q["v"]))
            idx = {}
            iq = kbar_quote(idx_kb, idx_y, cur) if idx_kb else None
            if iq:
                idx["t00" if d["market"] == "tse" else "o00"] = iq
            peers_q = {}
            for p, (kb, py, pname) in peers_kb.items():
                pq = kbar_quote(kb, py, cur)
                if pq:
                    pq["name"] = pname; peers_q[p] = pq
            flow = S.flow(code, price=q["ref"])
            ev = model.evaluate(d, q, idx, peers_q, flow, samples, cur)
            inf = ev["info"]
            fac = "  ".join(f"{n}({s:+.1f})" for n, s, _ in ev["intraday_factors"][:5])
            fmt = lambda x: "-" if x is None else f"{x:+.2f}"
            print(f"{cur:%H:%M}  {q['ref']:>7} {fmt(inf.get('chg_pct')):>7} {fmt(inf.get('idx_chg_pct')):>7} {fmt(inf.get('group_avg')):>7} {q['v']:>7} "
                  f"{fmt(flow['big_net_pct']) if flow else '-':>7} {fmt(flow['small_net_pct']) if flow else '-':>7} {(flow or {}).get('hidden_net', 0):>+6} "
                  f"{ev['p_long']:>4.0f} {ev['p_short']:>4.0f}  {fac}")
            if a.verbose:
                for n, s_, txt in ev["intraday_factors"]:
                    print(f"           {s_:+.2f} {n}: {txt}")
                for w in ev["warnings"]:
                    print(f"           !! {w}")
                pl = ev["plan"]
                if pl.get("short"):
                    print(f"           空：{pl['short']['entry']} 停損 {pl['short']['stop']} 目標 {pl['short']['t1']}→{pl['short']['t2']}")
            rec = {k: v for k, v in ev.items() if k != "quote"}
            rec["quote"] = {k: q[k] for k in ("ref", "o", "h", "l", "v")}
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            cur += dt.timedelta(minutes=a.step)
    # 全日分流總結
    acc["ticks"] = ticks
    fl = S.flow(code, price=ticks[-1][1])
    if fl:
        th = fl["thresholds"]
        print(f"\n全日分流：主力淨 {fl['big_net']:+} 張 ({fl['big_net_pct']:+.1f}%)  藏單 {fl['hidden_net']:+}  散戶淨 {fl['small_net']:+} 張 ({fl['small_net_pct']:+.1f}%)  中實戶 {fl['mid_net']:+}  外盤/內盤 {fl['buy_vol']}/{fl['sell_vol']}  VWAP {fl['vwap']:.2f}")
        print(f"門檻：大單 ≥{th['big_lots']} 張（{th['big_amount']/1e4:.0f} 萬→{th['big_by_amount']} 張，P95 {th['p95']}）  散戶 <{th['small_lots']} 張")
        for i in fl["iceberg"]:
            print(f"規律小單：{i['size']} 張 × {i['count']} 筆 {i['side']}  共 {i['volume']} 張  佔筆數 {i['share_cnt']*100:.0f}%" + (f"  平均間隔 {i['avg_gap_s']:.0f} 秒 cv {i['interval_cv']:.2f}" if i.get('interval_cv') is not None else "") + ("  ← 機器單" if i['regular'] else ""))
    # 分時段分流
    print("\n分時段：")
    for s0, s1 in (("09:00", "09:20"), ("09:20", "09:50"), ("09:50", "11:00"), ("11:00", "12:30"), ("12:30", "13:30")):
        t0 = dt.datetime.combine(date, dt.time(*map(int, s0.split(":")))); t1 = dt.datetime.combine(date, dt.time(*map(int, s1.split(":"))))
        acc["ticks"] = [x for x in ticks if t0 <= x[0] < t1]
        fl = S.flow(code, price=acc["ticks"][-1][1] if acc["ticks"] else None)
        if fl:
            p0 = acc["ticks"][0][1]; p1 = acc["ticks"][-1][1]
            print(f"  {s0}-{s1}  {p0}→{p1} ({(p1/p0-1)*100:+.2f}%)  量 {fl['volume']}  主力淨 {fl['big_net']:+} ({fl['big_net_pct']:+.1f}%)  散戶淨 {fl['small_net']:+} ({fl['small_net_pct']:+.1f}%)  藏單 {fl['hidden_net']:+}  外/內 {fl['buy_vol']}/{fl['sell_vol']}")
    print(f"\n重播紀錄：{out_path}")


if __name__ == "__main__":
    main()
