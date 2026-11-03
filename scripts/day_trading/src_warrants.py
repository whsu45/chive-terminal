"""
權證資料：某標的的認購/認售（含牛熊證）清單、日成交統計、發行商避險買賣、盤中彙總。
- 上市：TWSE MI_INDEX type=0999(認購) / 0999P(認售) / 0999C(牛) / 0999B(熊)，表內有「標的代號」欄。
- 上櫃：TPEx dailyQuotes 把 7xxxxx 權證混在股票表裡、沒有標的欄 → 用名稱前綴配對；有 Shioaji 時用合約的 target_code。
- 避險：T86「自營商買賣超股數(避險)」(TWSE r[17]) / TPEx insti r[19]，這就是發行商為權證部位在現股的對沖。
"""
import re
import http_client as http
import src_mis

TWSE = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
TPEX = "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes"
TTL = 86400 * 30

_TYPES = {"0999": "call", "0999P": "put", "0999C": "call", "0999B": "put"}


def _twse_table(date, typ):
    j = http.get_json(TWSE, params={"date": date.strftime("%Y%m%d"), "type": typ, "response": "json"}, cache_ttl=TTL)
    for t in j.get("tables", []):
        if t.get("data"):
            return t.get("fields") or [], t["data"]
    return [], []


def twse_warrants_daily(date):
    """全市場 → {target_code: {"call": [w...], "put": [w...]}}；w = {code,name,vol(張),amt,close,chg_pct,right}"""
    out = {}
    for typ, right in _TYPES.items():
        try:
            fields, rows = _twse_table(date, typ)
        except http.HttpError:
            continue
        if not rows:
            continue
        fi = {f: i for i, f in enumerate(fields)}
        ic, iname, ivol, iamt, iclose, isign, ichg, itgt = (fi.get("證券代號"), fi.get("證券名稱"), fi.get("成交股數"), fi.get("成交金額"),
                                                            fi.get("收盤價"), fi.get("漲跌(+/-)"), fi.get("漲跌價差"), fi.get("標的代號"))
        for r in rows:
            try:
                tgt = (r[itgt] or "").strip()
                if not tgt:
                    continue
                close = http.to_float(r[iclose]); chg = http.to_float(r[ichg])
                neg = "-" in (r[isign] or "")
                prev = (close + chg) if neg else (close - chg) if (close is not None and chg is not None) else None
                w = {"code": r[ic].strip(), "name": r[iname].strip(), "vol": http.to_int(r[ivol]) // 1000,
                     "amt": http.to_float(r[iamt]) or 0.0, "close": close,
                     "chg_pct": ((close / prev - 1) * 100) if (prev and close) else None, "right": right, "ex": "tse"}
                out.setdefault(tgt, {"call": [], "put": []})[right].append(w)
            except Exception:
                continue
    return out


def tpex_warrants_daily(date, code, name):
    """上櫃權證：名稱以標的名稱開頭的 7 開頭代碼。"""
    j = http.get_json(TPEX, params={"date": date.strftime("%Y/%m/%d"), "response": "json"}, cache_ttl=TTL)
    out = {"call": [], "put": []}
    for t in j.get("tables", []):
        if "上櫃股票行情" not in (t.get("title") or ""):
            continue
        for r in t.get("data", []):
            c = str(r[0]).strip(); n = str(r[1]).strip()
            if not c.startswith("7") or len(c) != 6 or not n.startswith(name):
                continue
            tail = n[len(name):]
            right = "put" if ("售" in tail or "熊" in tail) else ("call" if ("購" in tail or "牛" in tail) else None)
            if not right:
                continue
            close = http.to_float(r[2]); chg = http.to_float(r[3])
            prev = close - chg if (close is not None and chg is not None) else None
            out[right].append({"code": c, "name": n, "vol": http.to_int(r[8]) // 1000, "amt": http.to_float(r[9]) or 0.0,
                               "close": close, "chg_pct": ((close / prev - 1) * 100) if (prev and close) else None,
                               "right": right, "ex": "otc"})
    return out


def warrants_for(code, name, market, date, twse_cache=None):
    if market == "tse":
        tbl = twse_cache if twse_cache is not None else twse_warrants_daily(date)
        return tbl.get(code, {"call": [], "put": []})
    return tpex_warrants_daily(date, code, name)


def summarize(wd):
    """日統計：認購/認售 成交張數、金額、檔數、有量檔數、認購佔比。"""
    cv = sum(w["vol"] for w in wd["call"]); pv = sum(w["vol"] for w in wd["put"])
    ca = sum(w["amt"] for w in wd["call"]); pa = sum(w["amt"] for w in wd["put"])
    tot = cv + pv
    return {"call_vol": cv, "put_vol": pv, "call_amt": ca, "put_amt": pa,
            "n_call": len(wd["call"]), "n_put": len(wd["put"]),
            "n_call_active": sum(1 for w in wd["call"] if w["vol"] > 0), "n_put_active": sum(1 for w in wd["put"] if w["vol"] > 0),
            "call_share": (cv / tot) if tot else None,
            "amt_share": ((ca - pa) / (ca + pa)) if (ca + pa) else None}


def hedge_net(date, code, market):
    """自營商(避險)買賣超（張）。發行商賣權證給散戶後，為對沖在現股的買賣。"""
    if market == "tse":
        j = http.get_json("https://www.twse.com.tw/rwd/zh/fund/T86", params={"date": date.strftime("%Y%m%d"), "selectType": "ALL", "response": "json"},
                          cache_ttl=TTL)
        for r in j.get("data", []):
            if r[0].strip() == code:
                return http.to_int(r[17]) // 1000
        return 0
    j = http.get_json("https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade", params={"type": "Daily", "sect": "EW", "date": date.strftime("%Y/%m/%d"), "response": "json"},
                      cache_ttl=TTL)
    for t in j.get("tables", []):
        for r in t.get("data", []):
            if len(r) > 19 and r[0].strip() == code:
                return http.to_int(r[19]) // 1000
    return 0


def intraday(wd):
    """
    盤中：用 MIS 抓該標的所有權證的即時量價。回
    {call_vol, put_vol, call_share, call_up, call_down, put_up, put_down, n, top_calls:[(name, vol, chg%)]}
    MIS 一次最多約 50 檔，分批。
    """
    items = [(w, f"{w['ex']}_{w['code']}.tw") for w in wd["call"] + wd["put"]]
    quotes = {}
    for i in range(0, len(items), 45):
        chunk = items[i:i + 45]
        try:
            quotes.update(src_mis.fetch([ex for _, ex in chunk]))
        except http.HttpError:
            return {}   # 少一批就是不完整的快照：回空讓呼叫端沿用上一輪，否則下一輪會把整天的量當成「這一分鐘」
    res = {"call_vol": 0, "put_vol": 0, "call_up": 0, "call_down": 0, "put_up": 0, "put_down": 0, "n": 0, "tops": []}
    for w, _ in items:
        q = quotes.get(w["code"])
        if not q:
            continue
        res["n"] += 1
        v = q.get("v") or 0
        key = "call" if w["right"] == "call" else "put"
        res[f"{key}_vol"] += v
        chg = ((q["ref"] / q["y"] - 1) * 100) if (q.get("ref") and q.get("y")) else None
        if chg is not None and v > 0:
            res[f"{key}_up" if chg > 0 else f"{key}_down"] += 1 if chg != 0 else 0
        if v > 0:
            res["tops"].append((w["name"], w["right"], v, chg))
    tot = res["call_vol"] + res["put_vol"]
    res["call_share"] = res["call_vol"] / tot if tot else None
    res["tops"].sort(key=lambda x: -x[2])
    res["tops"] = res["tops"][:5]
    return res


def shioaji_list(api, code):
    """用 Shioaji 合約的 target_code 找權證（上櫃權證最可靠的來源）。"""
    out = {"call": [], "put": []}
    try:
        for ex in ("TSE", "OTC"):
            for c in getattr(api.Contracts.Stocks, ex):
                if getattr(c, "target_code", "") == code and len(c.code) == 6:
                    right = "put" if str(getattr(c, "option_right", "")).lower().endswith("put") or "售" in c.name or "熊" in c.name else "call"
                    out[right].append({"code": c.code, "name": c.name, "vol": 0, "amt": 0.0, "close": None, "chg_pct": None,
                                       "right": right, "ex": "tse" if ex == "TSE" else "otc",
                                       "strike": getattr(c, "strike_price", None), "maturity": getattr(c, "delivery_date", None)})
    except Exception:
        pass
    return out


# ---------------------------------------------------------------- 盤中異動警示
def _median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2] if xs else None


def intraday_detail(wd):
    """
    盤中每檔權證的 {code: {name, right, ref, y, v, chg_pct}}，給 alerts() 跟上一輪比。
    """
    items = [(w, f"{w['ex']}_{w['code']}.tw") for w in wd["call"] + wd["put"]]
    quotes = {}
    for i in range(0, len(items), 45):
        chunk = items[i:i + 45]
        try:
            quotes.update(src_mis.fetch([ex for _, ex in chunk]))
        except http.HttpError:
            return {}   # 少一批就是不完整的快照：回空讓呼叫端沿用上一輪，否則下一輪會把整天的量當成「這一分鐘」
    snap = {}
    for w, _ in items:
        q = quotes.get(w["code"])
        if not q or not q.get("ref"):
            continue
        y = q.get("y")
        snap[w["code"]] = {"code": w["code"], "name": w["name"], "right": w["right"], "ref": q["ref"], "y": y, "v": q.get("v") or 0,
                           "chg_pct": ((q["ref"] / y - 1) * 100) if y else None}
    return snap


def alerts(snap, prev_snap, stock_chg_pct, base_call_vol, interval_min):
    """
    權證異動 → 現股風險提醒。只提醒，不進分數。
    規則（都以「有量」的權證、成交量加權）：
      1. 認購這一輪急漲 ≥ +12% / 急跌 ≤ −12%（相對上一輪）
      2. 認售這一輪急漲 ≥ +12%
      3. 認購對昨收中位漲幅 ≥ +30% 或 ≤ −30%（全日累積極端）
      4. 這一輪權證成交量 ≥ 昨日全日認購量的 8%（瞬間爆量）
      5. 現股幾乎沒動（|漲跌| < 1%）但認購中位已動 ≥ 15%：發行商在調隱含波動率或市場搶先定價
    回 [str]
    """
    out = []
    if not snap:
        return out
    calls = [s for s in snap.values() if s["right"] == "call" and s["v"] > 0]
    puts = [s for s in snap.values() if s["right"] == "put" and s["v"] > 0]

    def vw_change(items):
        """相對上一輪的成交量加權漲跌%（只算這一輪有新成交的）。"""
        num = den = 0.0
        for s in items:
            p = prev_snap.get(s["code"]) if prev_snap else None
            if not p or not p.get("ref"):
                continue
            dv = s["v"] - p["v"]
            if dv <= 0:
                continue
            num += (s["ref"] / p["ref"] - 1) * 100 * dv
            den += dv
        return (num / den if den else None), den

    c_chg, c_dv = vw_change(calls)
    p_chg, p_dv = vw_change(puts)
    sc = stock_chg_pct if stock_chg_pct is not None else 0

    if c_chg is not None and c_dv >= 50:
        if c_chg >= 12:
            out.append(f"⚠ 認購權證 {interval_min} 分鐘內急漲 {c_chg:+.0f}%（{c_dv:.0f} 張換手）：散戶追價、發行商避險買盤同步進場。"
                       + ("現股空單有軋空風險，反彈不過前高再空、停損要掛好。" if sc > 0 else "現股未漲而權證先漲，留意現股隨後急拉。"))
        elif c_chg <= -12:
            out.append(f"⚠ 認購權證 {interval_min} 分鐘內急跌 {c_chg:+.0f}%（{c_dv:.0f} 張換手）：權證多單在停損，發行商回補避險部位會賣現股。"
                       + ("現股多單風險升高，守不住開盤價就走。" if sc >= 0 else "現股跌勢可能加速，空單不要急著回補。"))
    if p_chg is not None and p_dv >= 30 and p_chg >= 12:
        out.append(f"⚠ 認售權證 {interval_min} 分鐘內急漲 {p_chg:+.0f}%（{p_dv:.0f} 張換手）：空方透過權證進場，發行商要放空現股避險，現股多單風險升高。")

    med = _median([s["chg_pct"] for s in calls if s["chg_pct"] is not None])
    if med is not None:
        if med >= 30:
            out.append(f"⚠ 認購權證今日中位漲幅 {med:+.0f}%（{len(calls)} 檔有量）：權證獲利盤隨時出，發行商反手賣現股；現股追多勝率降，空單等轉弱訊號。")
        elif med <= -30:
            out.append(f"⚠ 認購權證今日中位跌幅 {med:+.0f}%：權證多方已被洗出，發行商避險賣壓多半釋放；現股再殺空間變小，空單注意反彈。")
        if abs(sc) < 1 and abs(med) >= 15:
            out.append(f"⚠ 現股只動 {sc:+.1f}% 但認購權證中位已 {med:+.0f}%：發行商在調隱含波動率或市場搶先定價，現股接下來波動可能放大。")

    if base_call_vol and prev_snap:
        # 只算上一輪也有的權證；上一輪沒抓到的不能當成「這一輪全新成交」
        dv_all = sum(max(0, s["v"] - prev_snap[s["code"]]["v"]) for s in calls + puts if s["code"] in prev_snap)
        if dv_all >= base_call_vol * 0.08:
            out.append(f"⚠ 權證 {interval_min} 分鐘成交 {dv_all} 張 = 昨日全日認購量 {dv_all/base_call_vol*100:.0f}%：瞬間爆量，現股即將或正在劇烈波動，部位先減半。")
    return out
