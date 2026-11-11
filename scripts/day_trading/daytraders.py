"""
隔日沖分點的動態名單：不靠寫死的名單，看每個分點實際怎麼做。

做法（每個交易日收盤後補一天）：
  1. 挑當天的強勢股：漲 ≥7%、成交 ≥3,000 張、收盤 ≥15 元，取成交值最大的 15 檔（隔日沖專挑這種）。
  2. 「大買」＝某分點在強勢股當天買超 ≥ 成交量 1%（且 ≥30 張）。
  3. 「倒回」＝隔天這個分點在同一檔賣超，賣掉前一天買超的一半以上。
  4. 近 60 個交易日大買 ≥5 次、倒回率的保守估計 ≥50% → 隔日沖分點（次數少的要更高：5 次要倒回 4 次、10 次要 7 次、
     30 次要 18 次，免得靠運氣入選）；≥8 次、倒回率 <30% → 會留倉的分點。

回測（8/7–11/6 強勢股 6,384 次大買，前三分之一只用來累積統計）：照這個規則當時選出的分點，之後的大買有 66% 隔天倒回；
名單外的 36%。原本寫死的 8 個分點只有凱基-台北、凱基-城中還常出現，其他 6 個 61 天合計 22 次。

每天的統計存在 logs/daytraders_<裝置ID>.json（只有各分點的次數，進 git，另一台 pull 下來就能用）；
原始分點表存在 .cache/zco/（不進 git）。資料不到 20 天時退回 config.DAYTRADE_BRANCHES 的固定名單。

    python3 daytraders.py              # 補到最新、印出目前名單
    python3 daytraders.py --backfill   # 第一次用：補滿 60 天（約一千頁，10 分鐘左右）
    python3 daytraders.py 3105         # 這檔昨天有哪些隔日沖分點買進

限制：分點表只列買超、賣超各前 15 名；倒回的量很小、排不進隔天前 15 名賣超時會被算成沒倒回（倒回率偏低估）。
"""
import os
import sys
import json
import threading
import datetime as dt

from config import LOG_DIR, CACHE_DIR, DAYTRADE_BRANCHES, dev_path, dev_files
import tw_calendar as cal
import src_moneydj

MIN_CHG = 7.0        # 強勢股：當天漲幅 %
MIN_VOL = 3000       # 張
MIN_PX = 15.0
TOP_N = 15           # 每天取成交值最大的幾檔
EV_PCT = 1.0         # 大買：買超佔成交量 %
EV_LOTS = 30
FLIP = 0.5           # 隔天賣回這個比例以上算倒回
WINDOW = 60          # 統計視窗（交易日）
MIN_N = 5            # 至少幾次大買才下結論
DT_LOW = 0.5         # 倒回率的保守估計（扣掉次數少的誤差）≥ 這個 = 隔日沖。5 次要倒回 4 次、10 次要 7 次、30 次要 18 次
HOLD_RATE = 0.3      # 倒回率 < 這個 = 會留倉
MIN_DAYS = 20        # 資料少於這麼多天就退回固定名單

PATH = dev_path(LOG_DIR, "daytraders", ".json")
SCREEN_STORE = os.path.join(CACHE_DIR, "screen_daily.json")
_lock = threading.Lock()
_cache = {"key": None, "table": None}
_tried = set()


def _low(k, n, z=1.0):
    """倒回率的保守估計（Wilson 下界）：次數越少扣得越多。"""
    if not n:
        return 0.0
    p = k / n
    return (p + z * z / (2 * n) - z * (p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / (1 + z * z / n)


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) and isinstance(d.get("days"), dict) else {"days": {}, "ids": {}}
    except (OSError, ValueError):
        return {"days": {}, "ids": {}}


def _merged():
    """所有裝置的檔合併：同一天取大買事件比較多的那份。"""
    days, ids = {}, {}
    for _, path in dev_files(LOG_DIR, "daytraders", ".json"):
        d = _read(path)
        ids.update(d.get("ids") or {})
        for iso, rec in d["days"].items():
            n = sum(v[0] for v in (rec.get("b") or {}).values())
            if iso not in days or n > sum(v[0] for v in (days[iso].get("b") or {}).values()):
                days[iso] = rec
    return {"days": days, "ids": ids}


def _save(db):
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(PATH + ".tmp", "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    os.replace(PATH + ".tmp", PATH)
    _cache["key"] = None


def _quotes(days):
    """全市場日 K（theme_screener 的快取）→ {iso: {code: [o,h,l,c,v張]}}；缺的日子補抓。"""
    import theme_screener as TS
    try:
        with open(SCREEN_STORE, encoding="utf-8") as f:
            store = json.load(f)
    except (OSError, ValueError):
        store = {}
    if any(d.isoformat() not in store for d in days):
        store, _ = TS.load_store(max(days), n=min(len(days) + 1, 70))
    return {k: v.get("q") or {} for k, v in store.items()}


def strong_codes(q, q_prev):
    """當天的強勢股（成交值大到小，最多 TOP_N 檔）。"""
    out = []
    for c, row in q.items():
        p = q_prev.get(c)
        if len(c) != 4 or c.startswith("00") or not p or not p[3] or not row[3]:
            continue
        if (row[3] / p[3] - 1) * 100 >= MIN_CHG and row[4] >= MIN_VOL and row[3] >= MIN_PX:
            out.append((row[3] * row[4], c))
    return [c for _, c in sorted(out, reverse=True)[:TOP_N]]


def day_events(code, d, d_next, vol):
    """這檔在 d 的大買事件 → [(分點, 買超佔量%, 隔天賣回比例, bhid, bid)]；兩天的表有一天沒存檔就回 None。"""
    a = src_moneydj.zco_day(code, d, fetch=False)
    b = src_moneydj.zco_day(code, d_next, fetch=False)
    if not a or not b or not (b["buyers"] or b["sellers"]) or not vol:
        return None
    nxt = {}
    for r in b["buyers"] + b["sellers"]:
        nxt[r["broker"]] = nxt.get(r["broker"], 0) + r["net"]
    out = []
    for r in a["buyers"]:
        pct = r["net"] / vol * 100
        if pct >= EV_PCT and r["net"] >= EV_LOTS:
            out.append((r["broker"], pct, max(0.0, min(1.0, -nxt.get(r["broker"], 0) / r["net"])), r.get("bhid"), r.get("bid")))
    return out


def update(ref=None, max_days=3, verbose=True):
    """
    把名單補到最新。事件日 e 要等 e 的下一個交易日的分點表出來才算得出倒回，所以最新只能補到「前兩個交易日」。
    max_days：這次最多補幾天（盤前只補最近幾天，免得啟動卡太久；第一次請跑 --backfill）。回這次補了幾天。
    """
    ref = ref or cal.today_tw()
    if max_days <= 5:
        # 盤前例行更新：同一次執行、同一天只試一次（多檔股票各自建盤前資料時不重複連線；抓不到就用現有名單）
        if ref in _tried:
            return 0
        _tried.add(ref)
    days = sorted(cal.last_n_trading_days(ref, WINDOW + 2))          # 舊到新，最後一天 = 前一交易日
    nxt = {days[i]: days[i + 1] for i in range(len(days) - 1)}
    merged = _merged()
    todo = [e for e in sorted(nxt, reverse=True) if e.isoformat() not in merged["days"]][:max_days]
    if not todo:
        return 0
    try:
        Q = _quotes(sorted(set(todo) | {cal.prev_trading_day(e) for e in todo}))     # 挑強勢股只需要事件日與它前一天的日 K
    except Exception as ex:
        if verbose:
            print(f"  [warn] 隔日沖名單：全市場日 K 抓不到（{str(ex)[:80]}），這次不更新")
        return 0
    plan = {}
    for e in todo:
        q, qp = Q.get(e.isoformat()), Q.get(cal.prev_trading_day(e).isoformat())
        if q and qp:
            plan[e] = strong_codes(q, qp)
    pairs = [(c, d) for e, codes in plan.items() for c in codes for d in (e, nxt[e])]
    if verbose and pairs:
        print(f"[daytraders] 補 {len(plan)} 天的隔日沖統計（強勢股分點表，最多 {len(pairs)} 頁）...", file=sys.stderr)
    src_moneydj.zco_fetch_many(pairs)
    mine = _read(PATH)
    done = 0
    for e, codes in plan.items():
        b, n_ok = {}, 0
        for c in codes:
            evs = day_events(c, e, nxt[e], (Q[e.isoformat()].get(c) or [0] * 5)[4])
            if evs is None:
                continue
            n_ok += 1
            for name, pct, f, bhid, bid in evs:
                s = b.setdefault(name, [0, 0, 0.0])
                s[0] += 1; s[1] += 1 if f >= FLIP else 0; s[2] = round(s[2] + f, 3)
                if bid:
                    mine["ids"][name] = [bhid, bid]
        if codes and n_ok < len(codes) * 0.6:
            continue                       # 大半沒抓到：這天先不記，下次再補
        mine["days"][e.isoformat()] = {"stocks": n_ok, "b": b}
        done += 1
    keep = {d.isoformat() for d in days}
    mine["days"] = {k: v for k, v in mine["days"].items() if k in keep}
    mine["version"] = 1
    _save(mine)
    return done


def table(ref=None):
    """
    目前的分點統計 → {分點: {n, flip, rate, mean_f, days, last, kind}}；kind = "dt"（隔日沖）/ "hold"（會留倉）/ ""（次數不夠或介於中間）。
    另有 "_meta": {days, first, last, source}；source = "dynamic" 或 "fixed"（資料不足，用固定名單）。
    """
    ref = ref or cal.today_tw()
    key = (ref.isoformat(), tuple(sorted((p, os.path.getmtime(p)) for _, p in dev_files(LOG_DIR, "daytraders", ".json"))))
    with _lock:
        if _cache["key"] == key:
            return _cache["table"]
    m = _merged()
    win = {d.isoformat() for d in cal.last_n_trading_days(ref, WINDOW + 2)}
    used = sorted(k for k in m["days"] if k in win)
    agg = {}
    for iso in used:
        for name, (n, k, sf) in (m["days"][iso].get("b") or {}).items():
            s = agg.setdefault(name, {"n": 0, "flip": 0, "sum_f": 0.0, "days": 0, "last": iso})
            s["n"] += n; s["flip"] += k; s["sum_f"] += sf; s["days"] += 1; s["last"] = max(s["last"], iso)
    dynamic = len(used) >= MIN_DAYS
    out = {}
    for name, s in agg.items():
        rate = s["flip"] / s["n"]
        kind = ""
        if dynamic and s["n"] >= MIN_N:
            kind = "dt" if _low(s["flip"], s["n"]) >= DT_LOW else ("hold" if (rate < HOLD_RATE and s["n"] >= 8) else "")
        out[name] = {"n": s["n"], "flip": s["flip"], "rate": round(rate, 3), "mean_f": round(s["sum_f"] / s["n"], 3),
                     "days": s["days"], "last": s["last"], "kind": kind, "ids": m["ids"].get(name)}
    if not dynamic:
        for name in DAYTRADE_BRANCHES:
            out.setdefault(name, {"n": 0, "flip": 0, "rate": None, "mean_f": None, "days": 0, "last": None, "ids": None})["kind"] = "dt"
    out["_meta"] = {"days": len(used), "first": used[0] if used else None, "last": used[-1] if used else None,
                    "source": "dynamic" if dynamic else "fixed"}
    with _lock:
        _cache["key"], _cache["table"] = key, out
    return out


def lookup(name, tbl=None):
    """分點名稱 → 統計 dict（沒資料回 None）。"""
    tbl = tbl or table()
    return tbl.get((name or "").replace(" ", ""))


def is_daytrader(name, tbl=None):
    s = lookup(name, tbl)
    return bool(s and s.get("kind") == "dt")


def for_stock(summary, volume, tbl=None):
    """
    一張單日分點表（src_moneydj 的 summary）→ 這檔昨天的隔日沖庫存：
    {"buy": 張, "buy_pct": 佔量%, "expect_sell": 預估今天倒回幾張, "list": [{broker, net, pct, n, rate, mean_f}...], "source"}
    list 只列買超的隔日沖分點，買超大到小。佔量優先用分點表自己的「佔成交比重」欄（單日表才有），沒有才用 volume 算。
    """
    tbl = tbl or table()
    rows = []
    for r in summary.get("buyers", []):
        s = lookup(r["broker"], tbl)
        if r["net"] > 0 and s and s.get("kind") == "dt":
            rows.append({"broker": r["broker"], "net": r["net"], "buy": r.get("buy"), "sell": r.get("sell"),
                         "pct": r["pct"] if (r.get("pct") is not None and summary.get("date")) else (round(r["net"] / volume * 100, 2) if volume else None),
                         "n": s["n"], "rate": s["rate"], "mean_f": s["mean_f"]})
    rows.sort(key=lambda x: -x["net"])
    buy = sum(x["net"] for x in rows)
    exp = sum(x["net"] * (x["mean_f"] if x["mean_f"] is not None else 0.7) for x in rows)
    pcts = [x["pct"] for x in rows]
    return {"buy": buy, "buy_pct": round(sum(pcts), 2) if rows and all(p is not None for p in pcts) else None, "expect_sell": round(exp),
            "list": rows, "source": tbl["_meta"]["source"], "stat_days": tbl["_meta"]["days"], "stat_last": tbl["_meta"]["last"]}


def add_local_habit(dts, broker_days, vols):
    """
    補上每個隔日沖分點「在這一檔」的紀錄：broker_days = {iso: {分點: 買賣超}}（winners.broker_daily_history）、vols = {iso: 成交張數}。
    here_n = 近 20 日在這檔大買幾次（不含昨天）、here_flip = 其中隔天倒回幾次。直接寫回 dts["list"]。
    """
    days = sorted(broker_days)
    for r in dts.get("list", []):
        n = k = 0
        for i, d in enumerate(days[:-1]):
            net = broker_days[d].get(r["broker"], 0)
            v = vols.get(d) or 0
            if v and net >= EV_LOTS and net / v * 100 >= EV_PCT:
                n += 1
                if -broker_days[days[i + 1]].get(r["broker"], 0) >= FLIP * net:
                    k += 1
        r["here_n"], r["here_flip"] = n, k
    return dts


def describe_stock(dts):
    """一檔股票的隔日沖庫存 → 幾行文字（啟動摘要用）。"""
    if not dts:
        return ""
    src = "" if dts.get("source") == "dynamic" else "（動態統計不足，用固定名單）"
    if not dts.get("list"):
        return f"  隔日沖分點：昨天沒有隔日沖分點買超{src}"
    L = [f"  隔日沖分點{src}：昨天買超 {dts['buy']} 張（{dts.get('buy_pct') or 0:.1f}% 量），照過去習慣今天約倒回 {dts['expect_sell']} 張"]
    for r in dts["list"]:
        habit = f"近 {WINDOW} 日大買 {r['n']} 次、隔天倒回 {r['rate'] * 100:.0f}%" if r.get("rate") is not None else "固定名單"
        here = f"；這檔 {r['here_n']} 次倒回 {r['here_flip']} 次" if r.get("here_n") else ""
        L.append(f"    {r['broker']:<10} 買超 {r['net']:>5} 張（{r.get('pct') or 0:.1f}% 量）  {habit}{here}")
    return "\n".join(L)


def describe_list(tbl=None, top=30):
    tbl = tbl or table()
    meta = tbl["_meta"]
    L = [f"隔日沖分點名單（{'動態' if meta['source'] == 'dynamic' else '固定名單，動態資料只有 ' + str(meta['days']) + ' 天'}"
         + (f"，統計 {meta['first']} ～ {meta['last']} 共 {meta['days']} 天" if meta["days"] else "") + "）"]
    dts = sorted(((v["n"], k, v) for k, v in tbl.items() if k != "_meta" and v["kind"] == "dt"), reverse=True)
    for n, name, v in dts[:top]:
        L.append(f"  {name:<12} 大買 {n:>3} 次，隔天倒回 {v['flip']:>3} 次（{v['rate'] * 100:.0f}%）" if v["rate"] is not None else f"  {name:<12} （固定名單）")
    holds = sorted(((v["n"], k, v) for k, v in tbl.items() if k != "_meta" and v["kind"] == "hold"), reverse=True)
    if holds:
        L.append("  會留倉的大戶（大買後隔天多半不賣）：" + "、".join(f"{k} {v['rate'] * 100:.0f}%/{n}次" for n, k, v in holds[:10]))
    return "\n".join(L)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    n = update(max_days=WINDOW if "--backfill" in sys.argv else 3)
    print(f"這次補了 {n} 天")
    print(describe_list())
    for code in args:
        s = src_moneydj.broker_summary(code)
        d = for_stock(s, None)
        print(f"\n{code} {s.get('date')} 的分點表：隔日沖分點買超 {d['buy']} 張（{d.get('buy_pct') or 0:.1f}% 量），照習慣下一個交易日約倒回 {d['expect_sell']} 張")
        for r in d["list"]:
            print(f"  {r['broker']:<12} 買超 {r['net']} 張（大買 {r['n']} 次倒回 {r['rate'] * 100:.0f}%）" if r["rate"] is not None else f"  {r['broker']} 買超 {r['net']} 張")
