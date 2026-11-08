#!/usr/bin/env python3
"""
當沖盤前選股（repo 唯一的選股腳本）：收盤後 / 盤前跑，從全市場挑隔天的放空、做多候選，
FinLab「概念股主題」只拿來標族群與看族群強弱，不參與選股（回測主題評分沒幫助）。

  做空「弱勢股」（只在大盤弱 = 加權 < 20 日線 時有效）：
    劣於大盤（10 日報酬 < 加權 10 日報酬）、10 日跌 >10%、收盤 < 季線、股價 >100、外資與投信都賣超（五條件出自 Threads 貼文），
    加成交值 ≥ 5 億，依 10 日跌幅最深取前 N；開盤跳空跌 >3% 不空（跳空大跌後常反彈）。
  做多「強勢股急殺」（只在大盤不弱時有效）：
    昨日跌 >7%、收盤仍在季線上、ATR ≥ 2.5%、成交值 ≥ 5 億，依 20 日報酬最強取前 N。
  兩邊都排除處置股、開盤鎖漲跌停（買不到 / 空不到）。

回測（--backtest；2026/06/01–11/05，107 天，隔天開盤進、收盤出，扣成本 0.25%）：
  做空：大盤弱 36 天 99 筆 每筆 +1.78%、勝率 70%、t 4.0；大盤不弱時照做 −0.02% → 不做。
  做多：大盤不弱 59 天 139 筆 每筆 +0.59%、勝率 52%、t 1.6（證據較弱）；大盤弱時照做 −1.76% → 不做。
  舊版「昨日大漲 + 收高檔 + 量增」做多每筆 −1.06%（t −3.3），是反指標，已移除。

    python3 theme_screener.py                  # 最近一個有資料的交易日，多空各 3 檔
    python3 theme_screener.py --top 5 --json
    python3 theme_screener.py --date 2026-11-06
    python3 theme_screener.py --backtest       # 用快取裡的全市場資料回測（--days 170 先補資料）

全市場資料（日 K + 法人）快取在 .cache/screen_daily.json（不進 git），第一次抓約 65 天、之後每天補一天。
prep.py 的盤前因子「弱勢股篩選」也用這裡的 short_conditions()。僅供研究，不構成投資建議。
"""
import os
import re
import sys
import json
import math
import time
import argparse
import datetime as dt
import statistics as st
from concurrent.futures import ThreadPoolExecutor

from config import CACHE_DIR
import http_client as http
import indicators as I
import tw_calendar as cal
import src_twse
import src_tpex

STORE = os.path.join(CACHE_DIR, "screen_daily.json")
KEEP_DAYS = 180          # 快取最多留幾個交易日（回測用）
TTL = 86400 * 365
FINLAB = "https://finlab.finance/stocks/themes"
FINLAB_TTL = 1800

MIN_VOL = 300            # 張，short_conditions 的流動性下限（prep 因子用）
MIN_VALUE = 5.0          # 億，選股名單的成交值下限
SHORT_PX = 100
SHORT_DROP10 = -10.0
SHORT_SKIP_GAP = -3.0    # 開盤跳空跌超過這個 % 不空
LONG_DROP1 = -7.0
LONG_MIN_ATR = 2.5
COST = 0.25              # 當沖來回成本 %


# ================================================================ 條件（prep.py 也用）

def short_conditions(closes, idx_closes, foreign, trust, vol=None):
    """
    弱勢股五條件。closes：個股日收盤，舊到新，最後一筆 = 篩選日（至少 60 筆）；idx_closes：加權指數，同樣舊到新、最後一筆同一天（至少 20 筆）。
    foreign / trust：篩選日外資、投信買賣超（張）。回 dict（hit = 五條件全中；mkt_weak = 加權 < 20 日線）；資料不夠回 None。
    """
    if len(closes) < 60 or len(idx_closes) < 20:
        return None
    c, c10 = closes[-1], closes[-11]
    i, i10 = idx_closes[-1], idx_closes[-11]
    if not (c and c10 and i and i10):
        return None
    ma60 = sum(closes[-60:]) / 60
    ima20 = sum(idx_closes[-20:]) / 20
    r10 = (c / c10 - 1) * 100
    ir10 = (i / i10 - 1) * 100
    cond = {"under": r10 < ir10, "drop10": r10 < SHORT_DROP10, "below_ma": c < ma60, "px100": c > SHORT_PX,
            "sell": (foreign is not None and trust is not None and foreign < 0 and trust < 0)}
    out = {"close": c, "r10": round(r10, 2), "idx_r10": round(ir10, 2), "ma60": round(ma60, 2),
           "idx": i, "idx_ma20": round(ima20, 2), "mkt_weak": i < ima20, "foreign": foreign, "trust": trust,
           **cond, "n_ok": sum(cond.values()), "hit": all(cond.values())}
    if vol is not None:
        out["vol"] = vol
    return out


def long_conditions(f):
    """強勢股急殺：f 為 features() 的一筆。"""
    return f["chg1"] < LONG_DROP1 and f["c"] > f["ma60"] and f["atrp"] >= LONG_MIN_ATR


# ================================================================ 全市場資料快取

def _call(url, params):
    t = time.time()
    try:
        j = http.get_json(url, params=params, cache_ttl=TTL)
    except Exception as e:
        print(f"  [warn] {url.split('/')[-1]} {params.get('date')}: {str(e)[:100]}", file=sys.stderr)
        j = {}
    if time.time() - t > 0.2:
        time.sleep(2.0)          # 證交所 / 櫃買有頻率限制
    return j or {}


def _fetch_day(d):
    """一天的全市場 {q: {code: [o,h,l,c,v張]}, ins: {code: [外資,投信]}, idx, names}；資料還沒出回 None。"""
    q, ins, names, idx = {}, {}, {}, None
    j = _call("https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX", {"date": d.strftime("%Y%m%d"), "type": "ALLBUT0999", "response": "json"})
    for t in j.get("tables", []):
        title = t.get("title") or ""
        if "每日收盤行情" in title:
            for r in t.get("data", []):
                c = r[0].strip()
                if len(c) == 4 and c.isdigit() and http.to_float(r[8]):
                    q[c] = [http.to_float(r[5]), http.to_float(r[6]), http.to_float(r[7]), http.to_float(r[8]), http.to_int(r[2]) // 1000]
                    names[c] = r[1].strip()
        if "價格指數" in title and idx is None:
            for r in t.get("data", []):
                if (r[0] or "").strip() == "發行量加權股價指數":
                    idx = http.to_float(r[1])
    if not q:
        return None
    j = _call("https://www.twse.com.tw/rwd/zh/fund/T86", {"date": d.strftime("%Y%m%d"), "selectType": "ALL", "response": "json"})
    for r in j.get("data", []):
        c = r[0].strip()
        if len(c) == 4 and c.isdigit():
            ins[c] = [http.to_int(r[4]) // 1000, http.to_int(r[10]) // 1000]
    j = _call("https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes", {"date": d.strftime("%Y/%m/%d"), "response": "json"})
    for t in j.get("tables", []):
        if "上櫃股票行情" not in (t.get("title") or ""):
            continue
        for r in t.get("data", []):
            c = r[0].strip()
            if len(c) == 4 and c.isdigit() and http.to_float(r[2]):
                q[c] = [http.to_float(r[4]), http.to_float(r[5]), http.to_float(r[6]), http.to_float(r[2]), http.to_int(r[8]) // 1000]
                names[c] = r[1].strip()
    j = _call("https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade", {"type": "Daily", "sect": "EW", "date": d.strftime("%Y/%m/%d"), "response": "json"})
    for t in j.get("tables", []):
        for r in t.get("data", []):
            if len(r) >= 24 and len(r[0].strip()) == 4 and r[0].strip().isdigit():
                ins[r[0].strip()] = [http.to_int(r[10]) // 1000, http.to_int(r[13]) // 1000]
    if not ins:
        return None              # 法人資料還沒出（通常 15:00 後）
    return {"q": q, "ins": ins, "idx": idx, "names": names}


def _save(store):
    with open(STORE + ".tmp", "w", encoding="utf-8") as f:
        json.dump(store, f)
    os.replace(STORE + ".tmp", STORE)


def load_store(ref, n=65):
    """確保 ref（含）往前 n 個交易日都有資料（ref 當天還沒出就略過）；最多留 KEEP_DAYS 天。回 (store, 有資料的日期舊到新)。"""
    try:
        with open(STORE, encoding="utf-8") as f:
            store = json.load(f)
    except (OSError, ValueError):
        store = {}
    days = sorted(cal.last_n_trading_days(ref, n, include_ref=True))
    todo = [d for d in days if d.isoformat() not in store]
    if todo:
        print(f"[screen] 補抓 {len(todo)} 天全市場資料（第一次較久，之後每天只補一天）...", file=sys.stderr)
    for k, d in enumerate(todo, 1):
        day = _fetch_day(d)
        if day is None:
            if d == days[-1]:
                print(f"[screen] {d} 證交所/櫃買資料還沒出，用前一個交易日", file=sys.stderr)
            continue
        store[d.isoformat()] = day
        if k % 10 == 0:
            _save(store)
    keep = {d.isoformat() for d in cal.last_n_trading_days(ref, max(n, KEEP_DAYS) + 10, include_ref=True)}
    store = {k: v for k, v in store.items() if k in keep}
    _save(store)
    return store, sorted(store)


def index_closes(store, dates):
    idx = [store[x].get("idx") for x in dates]
    if any(v is None for v in idx):
        # MI_INDEX 沒給指數的日子用 FMTQIK 補
        months = max(3, len(dates) // 20 + 2)
        tx = dict(src_twse.taiex_history(dt.date.fromisoformat(dates[-1]) + dt.timedelta(days=1), months=months))
        idx = [v or tx.get(x) for v, x in zip(idx, dates)]
    return idx


# ================================================================ 特徵與選股（即時與回測共用）

def features(store, dates, i, idx):
    """第 i 天收盤後每檔的特徵 → (大盤 dict, {code: feat})。"""
    ic = idx[: i + 1]
    if len(ic) < 20 or any(v is None for v in ic[-20:]):
        return None, {}
    mkt = {"idx": ic[-1], "ma20": sum(ic[-20:]) / 20, "r10": (ic[-1] / ic[-11] - 1) * 100 if len(ic) > 10 and ic[-11] else None}
    mkt["weak"] = mkt["idx"] < mkt["ma20"]
    day = store[dates[i]]
    out = {}
    for code, q in day["q"].items():
        if q[4] < MIN_VOL or not q[3]:
            continue
        win = [store[x]["q"].get(code) for x in dates[max(0, i - 60): i + 1]]
        if len(win) < 61 or any(w is None or not w[3] or w[1] is None or w[2] is None for w in win):
            continue
        cl = [w[3] for w in win]
        c, h, l = q[3], q[1], q[2]
        trs = [max(win[k][1] - win[k][2], abs(win[k][1] - win[k - 1][3]), abs(win[k][2] - win[k - 1][3])) for k in range(-14, 0)]
        f, t = day["ins"].get(code) or [None, None]
        sc = short_conditions(cl, ic, f, t, vol=q[4])
        if not sc:
            continue
        out[code] = {"code": code, "name": day.get("names", {}).get(code, ""), "c": c, "h": h, "l": l, "vol": q[4],
                     "value": q[4] * c / 1e5, "chg1": (c / cl[-2] - 1) * 100, "r20": (c / cl[-21] - 1) * 100,
                     "ma20": sum(cl[-20:]) / 20, "ma60": sc["ma60"], "atr": sum(trs) / 14, "atrp": sum(trs) / 14 / c * 100,
                     "foreign": f, "trust": t, "r10": sc["r10"], "weak5": sc["hit"]}
    return mkt, out


def pick(feats, exclude=(), min_value=MIN_VALUE):
    """回 (做空排序名單, 做多排序名單)，不含開盤條件。"""
    pool = [f for f in feats.values() if f["value"] >= min_value and f["code"] not in exclude]
    shorts = sorted((f for f in pool if f["weak5"]), key=lambda f: f["r10"])
    longs = sorted((f for f in pool if long_conditions(f)), key=lambda f: -f["r20"])
    return shorts, longs


def open_ok(side, f, o):
    """開盤條件（9:00 才知道）：鎖漲跌停不做；做空遇跳空大跌不空。"""
    up, dn = I.limits(f["c"])
    if o >= up or o <= dn:
        return False
    if side == "short" and (o / f["c"] - 1) * 100 < SHORT_SKIP_GAP:
        return False
    return True


# ================================================================ 回測

def backtest(store, dates, top=3):
    idx = index_closes(store, dates)
    res = {k: {"trades": [], "daily": [], "skip_regime": 0} for k in ("short", "long", "short_off", "long_off")}
    first = None
    for i in range(60, len(dates) - 1):
        mkt, feats = features(store, dates, i, idx)
        if not feats:
            continue
        first = first or dates[i]
        nxt = store[dates[i + 1]]["q"]
        shorts, longs = pick(feats)
        for side, cands in (("short", shorts), ("long", longs)):
            active = mkt["weak"] if side == "short" else not mkt["weak"]
            key = side if active else side + "_off"     # _off = 大盤環境不符時照做的結果（對照）
            rets = []
            for f in cands:
                n = nxt.get(f["code"])
                if not n or not n[0] or not n[3] or not open_ok(side, f, n[0]):
                    continue
                o, h, l, c = n[0], n[1], n[2], n[3]
                ret = (o - c) / o * 100 if side == "short" else (c - o) / o * 100
                mae = (h - o) / o * 100 if side == "short" else (o - l) / o * 100
                rets.append(ret)
                res[key]["trades"].append({"d": dates[i], "code": f["code"], "ret": ret, "mae": mae})
                if len(rets) >= top:
                    break
            if rets:
                res[key]["daily"].append(sum(rets) / len(rets))
    return first, dates[-2], res


def print_backtest(first, last, res, top):
    print(f"\n══ 回測 訊號日 {first}～{last}：隔天開盤進、收盤出，每天各取前 {top} 檔，成本 {COST}%")
    names = {"short": "做空・大盤弱（採用）", "long": "做多・大盤不弱（採用）", "short_off": "做空・大盤不弱（不做，對照）",
             "long_off": "做多・大盤弱（不做，對照）"}
    print(f"{'':26}{'天數':>5}{'筆數':>6}{'每筆':>8}{'扣成本':>8}{'勝率':>6}{'t值':>6}{'前半':>8}{'後半':>8}{'MAE':>6}")
    for k, nm in names.items():
        tr, dl = res[k]["trades"], res[k]["daily"]
        if not tr:
            print(f"{nm:26}  (無)"); continue
        xs = [t["ret"] for t in tr]
        half = sorted({t["d"] for t in tr})
        cut = half[len(half) // 2]
        a = [t["ret"] for t in tr if t["d"] < cut]; b = [t["ret"] for t in tr if t["d"] >= cut]
        sd = st.pstdev(dl) if len(dl) > 2 else 0
        tv = (sum(dl) / len(dl)) / (sd / math.sqrt(len(dl))) if sd else 0
        m = sum(xs) / len(xs)
        print(f"{nm:26}{len(dl):5d}{len(xs):6d}{m:+8.2f}{m - COST:+8.2f}{sum(x > COST for x in xs) / len(xs):6.0%}{tv:6.1f}"
              f"{sum(a) / max(len(a), 1):+8.2f}{sum(b) / max(len(b), 1):+8.2f}{sum(t['mae'] for t in tr) / len(tr):6.2f}")
    print("MAE = 開盤後最大逆向幅度（%），越大越容易先被停損洗掉。")


# ================================================================ FinLab 族群標註

def _devalue(arr):
    """SvelteKit __data.json 用 devalue 扁平化：陣列裡放值，物件/陣列的成員是索引。"""
    memo = {}

    def h(i):
        if not isinstance(i, int) or i < 0:
            return None
        if i in memo:
            return memo[i]
        v = arr[i]
        if isinstance(v, list):
            r = (v[1] if len(v) > 1 else None) if v and isinstance(v[0], str) and v[0] in ("Date", "Set", "Map", "BigInt", "undefined", "Object") \
                else [h(x) for x in v]
        elif isinstance(v, dict):
            r = {k: h(x) for k, x in v.items()}
        else:
            r = v
        memo[i] = r
        return r

    return h(0)


def _fetch_theme(slug):
    j = http.get_json(f"{FINLAB}/{slug}/__data.json", cache_ttl=FINLAB_TTL)
    for node in j.get("nodes") or []:
        if node and node.get("type") == "data":
            o = _devalue(node.get("data") or [])
            if isinstance(o, dict) and "rows" in o and "theme" in o:
                return {"name": (o["theme"] or {}).get("name") or slug, "rows": o.get("rows") or []}
    return None


def load_themes():
    """FinLab 概念股主題 → (code → [主題], [(主題, 昨日均漲跌%, 5 日均漲跌%)])；抓不到回空，不影響選股。"""
    try:
        html = http.get(FINLAB, cache_ttl=FINLAB_TTL).decode("utf-8", "replace")
        slugs = list(dict.fromkeys(re.findall(r'href="/stocks/themes/([a-z0-9-]+)"', html)))
        with ThreadPoolExecutor(max_workers=6) as ex:
            themes = [t for t in ex.map(lambda s: _safe_theme(s), slugs) if t and t["rows"]]
    except Exception as e:
        print(f"  [warn] FinLab 主題: {str(e)[:120]}", file=sys.stderr)
        return {}, []
    member, stats = {}, []
    for t in themes:
        c1 = [r.get("change1d_pct") for r in t["rows"] if r.get("change1d_pct") is not None]
        c5 = [r.get("change5d_pct") for r in t["rows"] if r.get("change5d_pct") is not None]
        if c1:
            stats.append((t["name"], sum(c1) / len(c1), sum(c5) / len(c5) if c5 else 0))
        for r in t["rows"]:
            member.setdefault(str(r.get("symbol")), []).append(t["name"])
    return member, sorted(stats, key=lambda x: -x[1])


def _safe_theme(slug):
    try:
        return _fetch_theme(slug)
    except Exception as e:
        print(f"  [warn] theme {slug}: {str(e)[:80]}", file=sys.stderr)
        return None


# ================================================================ 即時名單

def screen(ref, top=3):
    store, dates = load_store(ref)
    if len(dates) < 61:
        raise SystemExit(f"資料不足 61 個交易日（只有 {len(dates)} 天），稍後再試")
    idx = index_closes(store, dates)
    mkt, feats = features(store, dates, len(dates) - 1, idx)
    if not feats:
        raise SystemExit("沒有可篩的股票（資料異常？）")
    d = dt.date.fromisoformat(dates[-1])
    punish = {}
    try:
        punish.update(src_twse.punish(d - dt.timedelta(days=14), d + dt.timedelta(days=7)))
    except Exception as e:
        print(f"  [warn] twse 處置: {str(e)[:80]}", file=sys.stderr)
    try:
        punish.update(src_tpex.disposal())
    except Exception as e:
        print(f"  [warn] tpex 處置: {str(e)[:80]}", file=sys.stderr)
    shorts, longs = pick(feats, exclude=punish)
    member, tstats = load_themes()
    # 多留幾檔備用：開盤跳空不符時往下遞補
    return {"date": dates[-1], "mkt": mkt, "short": shorts[: top + 3], "long": longs[: top + 3], "top": top,
            "member": member, "themes": tstats, "punished": [c for c in punish if c in feats]}


def _plan(side, f):
    """停損用進場價 ± 半個 ATR（進場價開盤後才知道；昨日收在跌停時「昨低 = 收盤」不能當停損）。"""
    half = round(f["atr"] * 0.5, 1 if f["c"] >= 50 else 2)
    if side == "short":
        return (f"開盤跳空跌超過 {-SHORT_SKIP_GAP:.0f}%（低於 {I.round_tick(f['c'] * (1 + SHORT_SKIP_GAP / 100), 'down'):g}）或開盤鎖跌停不空；"
                f"開盤後反彈站不回均價線、跌破開盤價再空，停損設進場價上方約 {half:g} 元（半個 ATR）")
    return f"開盤鎖漲跌停不做；開盤後守住開盤價、站上均價線再買，停損設進場價下方約 {half:g} 元（半個 ATR）"


def _reasons(side, f, mkt):
    ins = f"外資 {f['foreign']:+,} / 投信 {f['trust']:+,} 張" if f["foreign"] is not None else "法人資料缺"
    if side == "short":
        return [f"10 日 {f['r10']:+.1f}%，劣於大盤（{mkt['r10']:+.1f}%）且跌超過 10%",
                f"收盤 {f['c']:g} 在季線 {f['ma60']:.1f} 之下", f"外資、投信同步賣超（{ins}）",
                f"成交值 {f['value']:.0f} 億、ATR {f['atrp']:.1f}%"]
    return [f"昨日急殺 {f['chg1']:+.1f}%，但收盤 {f['c']:g} 仍在季線 {f['ma60']:.1f} 之上（強勢股錯殺）",
            f"20 日 {f['r20']:+.1f}%" + ("，中期仍強" if f["r20"] > 0 else "，同條件裡相對最強（排序依 20 日報酬）"), f"法人：{ins}", f"成交值 {f['value']:.0f} 億、ATR {f['atrp']:.1f}%"]


def _row(side, f, r):
    return {"code": f["code"], "name": f["name"], "themes": r["member"].get(f["code"], []), "close": f["c"],
            "chg1d": round(f["chg1"], 2), "r10": f["r10"], "r20": round(f["r20"], 2), "value_e8": round(f["value"], 1),
            "atr_pct": round(f["atrp"], 2), "foreign": f["foreign"], "trust": f["trust"],
            "reasons": _reasons(side, f, r["mkt"]), "plan": _plan(side, f)}


def print_report(r):
    m, top = r["mkt"], r["top"]
    print(f"\n══ 當沖盤前選股 {r['date']}（隔天的候選）")
    print(f"大盤：加權 {m['idx']:.0f}，20 日線 {m['ma20']:.0f} → {'偏弱：做空有效、不做多' if m['weak'] else '不弱：做多有效、不做空'}；10 日 {m['r10']:+.1f}%")
    if r["themes"]:
        fmt = lambda t: f"{t[0]} {t[1]:+.1f}%"
        print("最強族群：" + "、".join(fmt(t) for t in r["themes"][:3]) + "｜最弱族群：" + "、".join(fmt(t) for t in r["themes"][-3:][::-1]))
    for side, title, active in (("short", "▼ 做空：弱勢股", m["weak"]), ("long", "▲ 做多：強勢股急殺", not m["weak"])):
        print(f"\n{title}" + ("" if active else "　⚠ 大盤環境不符，回測此時這組會虧，只列參考、不建議做"))
        if not r[side]:
            print("  （沒有符合條件的標的）")
        for k, f in enumerate(r[side], 1):
            j = _row(side, f, r)
            tag = "" if k <= top else "（備用：前面開盤條件不符時遞補）"
            th = f"[{'、'.join(j['themes'][:3])}]" if j["themes"] else ""
            print(f"\n  {k}. {j['code']} {j['name']} {th}{tag}")
            print(f"     收 {j['close']:g}｜昨 {j['chg1d']:+.2f}%｜10日 {j['r10']:+.1f}%｜20日 {j['r20']:+.1f}%｜成交值 {j['value_e8']:.0f} 億｜ATR {j['atr_pct']:.1f}%")
            for why in j["reasons"]:
                print(f"     • {why}")
            print(f"     → {j['plan']}")
    if r["punished"]:
        print("\n排除（處置股）：" + "、".join(r["punished"]))
    print("\n※ 回測是開盤進、收盤出的平均；實際進場仍用 monitor.py 看開盤量能與均價線。放空前確認可先賣後買。僅供研究，不構成投資建議。\n")


def default_ref(date=None):
    """篩選基準日：給 YYYY-MM-DD 就用它，否則今天；不是交易日往前找。當天資料還沒出時 load_store 會自動用前一個交易日。"""
    ref = dt.date.fromisoformat(date) if date else cal.today_tw()
    return ref if cal.is_trading_day(ref) else cal.prev_trading_day(ref)


def to_dict(r):
    """screen() 結果 → 可序列化 dict（--json 與 monitor 網頁選股共用）。rank ≤ top 為正選，其餘為備用。"""
    def rows(side):
        return [{**_row(side, f, r), "rank": k} for k, f in enumerate(r[side], 1)]
    return {"date": r["date"], "top": r["top"],
            "market": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in r["mkt"].items()},
            "short_active": r["mkt"]["weak"], "long_active": not r["mkt"]["weak"],
            "short": rows("short"), "long": rows("long"), "punished": r["punished"],
            "themes": [{"name": n, "chg1d": round(a, 2), "chg5d": round(b, 2)} for n, a, b in r["themes"]]}


def main():
    ap = argparse.ArgumentParser(description="當沖盤前選股（弱勢股做空 / 強勢股急殺做多）")
    ap.add_argument("--top", type=int, default=3, help="多、空各取幾檔（預設 3）")
    ap.add_argument("--date", default=None, help="篩選日 YYYY-MM-DD（預設：最近一個有資料的交易日）")
    ap.add_argument("--json", action="store_true", help="輸出 JSON")
    ap.add_argument("--backtest", action="store_true", help="用快取的全市場資料回測")
    ap.add_argument("--days", type=int, default=None, help="回測前先確保快取有這麼多交易日（最多 %d）" % KEEP_DAYS)
    a = ap.parse_args()
    ref = default_ref(a.date)
    if a.backtest:
        store, dates = load_store(ref, n=min(a.days or 65, KEEP_DAYS))
        first, last, res = backtest(store, dates, a.top)
        if not first:
            raise SystemExit("資料不足，先用 --days 170 補資料")
        print_backtest(first, last, res, a.top)
        return
    r = screen(ref, a.top)
    if a.json:
        print(json.dumps(to_dict(r), ensure_ascii=False, indent=1))
    else:
        print_report(r)


if __name__ == "__main__":
    main()
