#!/usr/bin/env python3
"""
概念股主題當沖選股：抓 FinLab「台股概念股主題總覽」每個主題的成分股，
再補上證交所/櫃買的成交值、三大法人、融資券、處置/注意股與日K（ATR、量比、收盤位置），
挑出前 N 檔適合做多、前 N 檔適合做空的當沖標的，並列出理由與觀察價位。

    python3 theme_screener.py                  # 預設各取前 3 檔
    python3 theme_screener.py --top 5          # 各取前 5 檔
    python3 theme_screener.py --min-value 10   # 成交值門檻改 10 億
    python3 theme_screener.py --json           # 輸出 JSON（給程式/AI 讀）
    python3 theme_screener.py --refresh        # 不用快取，重抓 FinLab

資料以 FinLab 的收盤日（as_of）為準，適合盤前（前一晚到 8:45）跑；
盤中仍要用 monitor.py 的開盤量能、均價線確認後才進場。僅供研究，不構成投資建議。
"""
import re
import sys
import json
import argparse
import datetime as dt
from concurrent.futures import ThreadPoolExecutor

import http_client as http
import indicators as I
import src_twse
import src_tpex
import src_yahoo

BASE = "https://finlab.finance"
OVERVIEW = f"{BASE}/stocks/themes"
FINLAB_TTL = 1800


# ---------------------------------------------------------------- FinLab 抓取

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
            if v and isinstance(v[0], str) and v[0] in ("Date", "Set", "Map", "BigInt", "undefined", "Object"):
                r = v[1] if len(v) > 1 else None
            else:
                r = [h(x) for x in v]
        elif isinstance(v, dict):
            r = {k: h(x) for k, x in v.items()}
        else:
            r = v
        memo[i] = r
        return r

    return h(0)


def theme_slugs(refresh=False):
    html = http.get(OVERVIEW, cache_ttl=0 if refresh else FINLAB_TTL).decode("utf-8", "replace")
    seen = []
    for s in re.findall(r'href="/stocks/themes/([a-z0-9-]+)"', html):
        if s not in seen:
            seen.append(s)
    return seen


def fetch_theme(slug, refresh=False):
    j = http.get_json(f"{OVERVIEW}/{slug}/__data.json", cache_ttl=0 if refresh else FINLAB_TTL)
    for node in j.get("nodes") or []:
        if not node or node.get("type") != "data":
            continue
        o = _devalue(node.get("data") or [])
        if isinstance(o, dict) and "rows" in o and "theme" in o:
            th = o["theme"] or {}
            return {"slug": slug, "name": th.get("name") or slug,
                    "as_of": (th.get("data") or {}).get("as_of") or o.get("liveAsOf"),
                    "rows": o.get("rows") or []}
    return None


def load_themes(refresh=False):
    slugs = theme_slugs(refresh)
    if not slugs:
        raise RuntimeError("FinLab 主題總覽頁找不到任何主題連結（頁面改版？）")
    with ThreadPoolExecutor(max_workers=6) as ex:
        res = list(ex.map(lambda s: _try(lambda: fetch_theme(s, refresh), None, f"theme {s}"), slugs))
    return [t for t in res if t and t["rows"]]


def _try(fn, default=None, label=""):
    try:
        return fn()
    except Exception as e:
        print(f"  [warn] {label}: {str(e)[:120]}", file=sys.stderr)
        return default


# ---------------------------------------------------------------- 整理資料

def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _pct_rank(values):
    """{key: value} → {key: 0~1 百分位}（越大越高）。"""
    items = sorted((v, k) for k, v in values.items() if v is not None)
    n = len(items)
    return {k: (i / (n - 1) if n > 1 else 0.5) for i, (v, k) in enumerate(items)}


def build_universe(themes):
    tstats = {}
    stocks = {}
    for t in themes:
        rows = t["rows"]
        tstats[t["name"]] = {
            "avg1d": _mean([r.get("change1d_pct") for r in rows]),
            "avg5d": _mean([r.get("change5d_pct") for r in rows]),
            "up_ratio": sum(1 for r in rows if (r.get("change1d_pct") or 0) > 0) / len(rows),
            "n": len(rows),
        }
        for r in rows:
            code = str(r.get("symbol") or "").strip()
            if not code:
                continue
            s = stocks.setdefault(code, {"code": code, "name": r.get("name"), "themes": [], "roles": [], "fin": r})
            s["themes"].append(t["name"])
            if r.get("role"):
                s["roles"].append(r["role"])
    r1 = _pct_rank({k: v["avg1d"] for k, v in tstats.items()})
    r5 = _pct_rank({k: v["avg5d"] for k, v in tstats.items()})
    for k, v in tstats.items():
        v["rank1d"] = r1.get(k, 0.5)
        v["rank5d"] = r5.get(k, 0.5)
    return tstats, stocks


def market_data(as_of):
    d = dt.date.fromisoformat(as_of)
    tw_q = _try(lambda: src_twse.all_quotes(d), {}, "twse 收盤")
    tp_q = _try(lambda: src_tpex.daily_quotes(d), {}, "tpex 收盤")
    insti = {**_try(lambda: src_twse.t86(d), {}, "twse 法人"), **_try(lambda: src_tpex.insti(d), {}, "tpex 法人")}
    margin = {**_try(lambda: src_twse.margin(d), {}, "twse 融資券"), **_try(lambda: src_tpex.margin(d), {}, "tpex 融資券")}
    punish = {**_try(lambda: src_twse.punish(d - dt.timedelta(days=14), d + dt.timedelta(days=3)), {}, "twse 處置"),
              **_try(src_tpex.disposal, {}, "tpex 處置")}
    notice = _try(lambda: src_twse.notice(d), set(), "twse 注意") | _try(src_tpex.attention, set(), "tpex 注意")
    return {"tw": tw_q, "tp": tp_q, "insti": insti, "margin": margin, "punish": punish, "notice": notice}


def daily_rows(code, market):
    """官方月日K優先（有日快取）；Yahoo 很容易 429，只當備援。"""
    src = src_twse if market == "tse" else src_tpex
    rows = _try(lambda: src.daily_history(code, months=4), [], f"日K {code}")
    if len(rows) < 25:
        rows = _try(lambda: src_yahoo.tw_daily(code, market, months=4), [], f"yahoo {code}")
    return [r for r in rows if r.get("h") is not None and r.get("l") is not None]


# ---------------------------------------------------------------- 評分

def score_stock(s, tstats, md):
    """回 (long_score, long_reasons, short_score, short_reasons, plan)。s 需已含 snap。"""
    f, snap = s["fin"], s["snap"]
    chg1 = f.get("change1d_pct") or 0
    chg5 = f.get("change5d_pct") or 0
    ts = [tstats[t] for t in s["themes"]]
    t_avg1 = _mean([t["avg1d"] for t in ts]) or 0
    t_rank1 = _mean([t["rank1d"] for t in ts])
    t_rank5 = _mean([t["rank5d"] for t in ts])
    t_up = _mean([t["up_ratio"] for t in ts])
    tname = "、".join(s["themes"][:2])
    L, Lr, S, Sr = 0.0, [], 0.0, []

    # 1. 族群強弱（族群同步比單一個股可靠）
    L += 2 * t_rank1 + t_rank5
    S += 2 * (1 - t_rank1) + (1 - t_rank5)
    if t_rank1 >= 0.7:
        Lr.append(f"族群「{tname}」昨日均 {t_avg1:+.1f}%，強度排前段（上漲家數 {t_up:.0%}）")
    if t_rank1 <= 0.3:
        Sr.append(f"族群「{tname}」昨日均 {t_avg1:+.1f}%，強度排後段（上漲家數 {t_up:.0%}）")

    # 2. 相對族群強弱：多找領漲、空找落後
    rs = chg1 - t_avg1
    if rs > 0:
        L += min(rs / 2, 2)
        if rs >= 1:
            Lr.append(f"昨日 {chg1:+.1f}%，強於族群 {rs:+.1f} 個百分點（領漲股）")
    else:
        S += min(-rs / 2, 2)
        if rs <= -1:
            Sr.append(f"昨日 {chg1:+.1f}%，弱於族群 {rs:+.1f} 個百分點（弱勢股）")

    # 3. 收盤位置
    h, l, c = snap["high"], snap["low"], snap["close"]
    pos = (c - l) / (h - l) if h and l and h > l else 0.5
    if pos >= 0.75:
        L += 1.5; Lr.append(f"收在當日高檔（K 棒位置 {pos:.0%}），買盤收尾強")
    elif pos <= 0.25:
        S += 1.5; Sr.append(f"收在當日低檔（K 棒位置 {pos:.0%}），賣壓收尾重")
    upper = (h - max(c, snap["open"])) / c * 100 if c else 0
    if upper >= 2.5 and pos < 0.5:
        S += 0.5; Sr.append(f"留長上影線 {upper:.1f}%，高檔有人出貨")

    # 4. 量能
    vr = s.get("vol_ratio")
    if vr and vr >= 1.5:
        if chg1 > 0:
            L += 1.5; Lr.append(f"量增上漲：成交量為 20 日均量 {vr:.1f} 倍")
        elif chg1 < 0:
            S += 1.5; Sr.append(f"爆量下跌：成交量為 20 日均量 {vr:.1f} 倍")

    # 5. 趨勢 / 關鍵價
    ma20, hi20, lo20 = snap.get("ma20"), s.get("prior_high20"), s.get("prior_low20")
    if ma20:
        if c > ma20:
            L += 0.5
        else:
            S += 0.5
    if hi20 and c >= hi20:
        L += 1; Lr.append(f"收盤突破前 20 日高點 {hi20:g}")
    if lo20 and c <= lo20:
        S += 1; Sr.append(f"收盤跌破前 20 日低點 {lo20:g}")
    if ma20 and c > ma20 and chg5 < 0 and chg1 > 0:
        Lr.append(f"站在月線 {ma20:.1f} 之上，回檔後轉強")
    if ma20 and c < ma20 and chg5 > 0 and chg1 < 0:
        Sr.append(f"跌破月線 {ma20:.1f}，漲多後轉弱")

    # 6. 法人籌碼
    ins = md["insti"].get(s["code"])
    vol = s.get("vol") or 0
    if ins and vol:
        net = (ins.get("foreign") or 0) + (ins.get("trust") or 0)
        ratio = net / vol
        if ratio >= 0.05:
            L += 1; Lr.append(f"外資+投信買超 {net:,} 張（占成交量 {ratio:.0%}）")
        elif ratio <= -0.05:
            S += 1; Sr.append(f"外資+投信賣超 {-net:,} 張（占成交量 {-ratio:.0%}）")
    tag = f.get("tagline") or ""
    if "外資買超" in tag:
        L += 0.5
    if "外資賣超" in tag:
        S += 0.5

    # 7. 融資券：券資比高 → 做多有軋空助力、做空有軋空風險
    mg = md["margin"].get(s["code"])
    if mg and mg.get("fin_now"):
        sr = mg["short_now"] / mg["fin_now"]
        s["short_ratio"] = sr
        if sr >= 0.2:
            L += 0.5; Lr.append(f"券資比 {sr:.0%}，有軋空助力")
            S -= 1; Sr.append(f"⚠ 券資比 {sr:.0%}，空單擁擠、留意軋空")
        if mg.get("fin_chg", 0) > 0 and chg1 < 0 and mg["fin_chg"] > vol * 0.03:
            S += 0.5; Sr.append(f"下跌中融資增加 {mg['fin_chg']:,} 張，散戶接刀、籌碼不穩")

    # 8. 波動度：當沖要有振幅
    atrp = snap.get("atr_pct") or 0
    bonus = max(min((atrp - 3) / 2, 1), 0)
    L += bonus; S += bonus
    vol_note = f"ATR {atrp:.1f}%，日均振幅足夠" if atrp >= 3 else None
    if vol_note:
        Lr.append(vol_note); Sr.append(vol_note)

    # 9. 風險扣分
    if chg1 >= 9.5:
        L -= 0.5; Lr.append("⚠ 昨日漲停，今天容易開高，追價風險大，等拉回再接")
        S -= 2
    if chg1 <= -9.5:
        S -= 0.5; Sr.append("⚠ 昨日跌停，今天容易開低，追空風險大，等反彈再空")
        L -= 2
    streak = snap.get("streak") or 0
    if streak <= -4 or chg5 <= -20:
        S -= 1; Sr.append(f"⚠ 連跌 {-streak} 天、5 日 {chg5:+.1f}%，乖離大，隨時可能跌深反彈")
    if streak >= 4 or chg5 >= 20:
        L -= 1; Lr.append(f"⚠ 連漲 {streak} 天、5 日 {chg5:+.1f}%，乖離大，留意獲利了結")
    if s["code"] in md["notice"]:
        L -= 1; S -= 1
        Lr.append("⚠ 列入注意股"); Sr.append("⚠ 列入注意股")

    # 觀察價位（開盤後仍要用量能、均價線確認）
    # 停損取「昨低/昨高」與「收盤 ± 半個 ATR」較近者，避免長 K 棒讓停損太遠
    half = (snap.get("atr14") or 0) * 0.5
    long_stop = I.round_tick(max(l, c - half), "down")
    short_stop = I.round_tick(min(h, c + half), "up")
    short_trig = f"跌破昨低 {l:g}" if l < c else f"跌破昨收（=昨低）{c:g}"
    plan = {
        "long": f"開盤後守住昨收 {c:g}、站上均價線且量能放大才進場；突破昨高 {h:g} 可加碼，跌破 {long_stop:g} 停損",
        "short": f"開盤後{short_trig}，或開高走低跌破平盤、反彈站不回均價線才放空；站回 {short_stop:g} 停損",
        "ref": {"close": c, "prev_high": h, "prev_low": l, "ma20": round(ma20, 2) if ma20 else None,
                "atr": round(snap["atr14"], 2) if snap.get("atr14") else None,
                "long_stop": long_stop, "short_stop": short_stop},
    }
    return round(L, 2), Lr, round(S, 2), Sr, plan


def prelim(s, tstats):
    """抓日K前的粗分，用來縮小要抓日K的名單。"""
    f = s["fin"]
    t1 = _mean([tstats[t]["rank1d"] for t in s["themes"]])
    t_avg1 = _mean([tstats[t]["avg1d"] for t in s["themes"]]) or 0
    rs = (f.get("change1d_pct") or 0) - t_avg1
    return 2 * t1 + rs / 2, 2 * (1 - t1) - rs / 2


def screen(top=3, min_value=5.0, min_price=10.0, min_atr=2.5, pool=25, refresh=False):
    themes = load_themes(refresh)
    tstats, stocks = build_universe(themes)
    as_of = max(t["as_of"] for t in themes if t.get("as_of"))
    print(f"  [finlab] {len(themes)} 個主題、{len(stocks)} 檔成分股，資料日 {as_of}", file=sys.stderr)
    md = market_data(as_of)

    excluded = []
    cands = []
    for code, s in stocks.items():
        q = md["tw"].get(code) or md["tp"].get(code)
        s["market"] = "tse" if code in md["tw"] else ("otc" if code in md["tp"] else None)
        if not q or not q.get("c") or s["market"] is None:
            continue
        s["vol"] = q.get("v") or 0
        s["value_e8"] = s["vol"] * 1000 * q["c"] / 1e8
        if code in md["punish"]:
            excluded.append(f"{code} {s['name']}：處置股（{md['punish'][code]}）"); continue
        if q["c"] < min_price:
            continue
        if s["value_e8"] < min_value:
            continue
        cands.append(s)

    # 只對粗分前段抓日K（Yahoo 有限流）
    by_long = sorted(cands, key=lambda s: -prelim(s, tstats)[0])[:pool]
    by_short = sorted(cands, key=lambda s: -prelim(s, tstats)[1])[:pool]
    pick = {s["code"]: s for s in by_long + by_short}
    print(f"  [screen] 流動性達標 {len(cands)} 檔，抓 {len(pick)} 檔日K", file=sys.stderr)

    scored = []
    for s in pick.values():
        rows = daily_rows(s["code"], s["market"])
        rows = [r for r in rows if r["date"] <= as_of]
        if len(rows) < 25:
            continue
        snap = I.daily_snapshot(rows)
        if (snap.get("atr_pct") or 0) < min_atr:
            continue
        prev20 = rows[-21:-1]
        s["snap"] = snap
        s["prior_high20"] = max(r["h"] for r in prev20)
        s["prior_low20"] = min(r["l"] for r in prev20)
        vma = _mean([r["v"] for r in prev20])
        s["vol_ratio"] = (rows[-1]["v"] / vma) if vma else None
        s["long"], s["long_why"], s["short"], s["short_why"], s["plan"] = score_stock(s, tstats, md)
        scored.append(s)

    longs = sorted(scored, key=lambda s: -s["long"])
    shorts = sorted(scored, key=lambda s: -s["short"])
    top_long, top_short, used = [], [], set()
    for s in longs:
        if len(top_long) >= top:
            break
        if s["long"] > s["short"] and s["code"] not in used:
            top_long.append(s); used.add(s["code"])
    for s in shorts:
        if len(top_short) >= top:
            break
        if s["short"] > s["long"] and s["code"] not in used:
            top_short.append(s); used.add(s["code"])

    themes_rank = sorted(tstats.items(), key=lambda kv: -(kv[1]["avg1d"] or 0))
    return {"as_of": as_of, "themes": themes_rank, "long": top_long, "short": top_short,
            "excluded": excluded, "n_themes": len(themes), "n_stocks": len(stocks), "n_cands": len(cands)}


# ---------------------------------------------------------------- 輸出

def _row_json(s, side):
    f, snap = s["fin"], s["snap"]
    return {"code": s["code"], "name": s["name"], "market": s["market"], "themes": s["themes"],
            "score": s[side], "other_score": s["short" if side == "long" else "long"],
            "close": snap["close"], "chg1d": f.get("change1d_pct"), "chg5d": f.get("change5d_pct"),
            "value_e8": round(s["value_e8"], 1), "atr_pct": round(snap.get("atr_pct") or 0, 2),
            "vol_ratio": round(s["vol_ratio"], 2) if s.get("vol_ratio") else None,
            "finlab_tag": f.get("tagline"), "reasons": s[f"{side}_why"], "plan": s["plan"][side], "ref": s["plan"]["ref"]}


def print_report(r):
    print(f"\n══ 概念股主題當沖選股 ══  資料日 {r['as_of']}（{r['n_themes']} 個主題 / {r['n_stocks']} 檔，流動性達標 {r['n_cands']} 檔）\n")
    th = r["themes"]
    fmt = lambda kv: f"{kv[0]} {kv[1]['avg1d']:+.1f}%（5日 {kv[1]['avg5d']:+.1f}%）"
    print("最強族群：" + "、".join(fmt(kv) for kv in th[:3]))
    print("最弱族群：" + "、".join(fmt(kv) for kv in th[-3:][::-1]))
    up = sum(1 for _, v in th if (v["avg1d"] or 0) > 0)
    print(f"族群多空：{up}/{len(th)} 個族群昨日收紅" + ("，盤勢偏空，做多要嚴格、部位減半" if up < len(th) * 0.3 else
                                                   "，盤勢偏多，放空要挑最弱的" if up > len(th) * 0.7 else ""))

    for side, title in (("long", "▲ 做多候選"), ("short", "▼ 做空候選")):
        print(f"\n{title}")
        if not r[side]:
            print("  （沒有符合條件的標的）")
        for i, s in enumerate(r[side], 1):
            j = _row_json(s, side)
            print(f"\n  {i}. {j['code']} {j['name']}  [{'、'.join(j['themes'][:3])}]  分數 {j['score']:.1f}")
            print(f"     收 {j['close']:g}｜昨 {j['chg1d']:+.2f}%｜5日 {j['chg5d']:+.1f}%｜成交值 {j['value_e8']:.0f} 億｜"
                  f"ATR {j['atr_pct']:.1f}%｜量比 {j['vol_ratio'] or 0:.1f}")
            for why in j["reasons"]:
                print(f"     • {why}")
            print(f"     → {j['plan']}")
    if r["excluded"]:
        print("\n排除（處置股）：" + "；".join(r["excluded"]))
    print("\n※ 盤前名單只是候選，開盤 9:00–9:30 仍要用 monitor.py 確認量能與均價線再進場；"
          "放空前確認可先賣後買、平盤下可否放空。僅供研究，不構成投資建議。\n")


def main():
    ap = argparse.ArgumentParser(description="FinLab 概念股主題當沖選股")
    ap.add_argument("--top", type=int, default=3, help="多、空各取幾檔（預設 3）")
    ap.add_argument("--min-value", type=float, default=5.0, help="昨日成交值門檻，億元（預設 5）")
    ap.add_argument("--min-price", type=float, default=10.0, help="股價下限（預設 10）")
    ap.add_argument("--min-atr", type=float, default=2.5, help="ATR%% 下限（預設 2.5）")
    ap.add_argument("--json", action="store_true", help="輸出 JSON")
    ap.add_argument("--refresh", action="store_true", help="不用快取重抓 FinLab")
    a = ap.parse_args()
    r = screen(top=a.top, min_value=a.min_value, min_price=a.min_price, min_atr=a.min_atr, refresh=a.refresh)
    if a.json:
        out = {"as_of": r["as_of"],
               "themes": [{"name": k, **{kk: (round(vv, 3) if isinstance(vv, float) else vv) for kk, vv in v.items()}}
                          for k, v in r["themes"]],
               "long": [_row_json(s, "long") for s in r["long"]],
               "short": [_row_json(s, "short") for s in r["short"]],
               "excluded": r["excluded"]}
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        print_report(r)


if __name__ == "__main__":
    main()
