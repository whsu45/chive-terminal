"""櫃買中心（上櫃）日資料。"""
import datetime as dt
import http_client as http

BASE = "https://www.tpex.org.tw/www/zh-tw"
TTL_DAY = 86400 * 3


def _roc(d):
    return f"{d.year - 1911}/{d.month:02d}/{d.day:02d}"


def _ad(d):
    return d.strftime("%Y/%m/%d")


def daily_quotes(date):
    """全市場收盤 → {code: {name, c, chg_pct, v(張), up_limit, down_limit}}。"""
    j = http.get_json(f"{BASE}/afterTrading/dailyQuotes", params={"date": _ad(date), "response": "json"}, cache_ttl=TTL_DAY)
    out = {}
    for t in j.get("tables", []):
        if "上櫃股票行情" not in (t.get("title") or ""):
            continue
        for r in t.get("data", []):
            code = r[0].strip()
            c = http.to_float(r[2]); chg = http.to_float(r[3])
            prev = c - chg if (c is not None and chg is not None) else None
            out[code] = {"name": r[1].strip(), "c": c, "chg_pct": (c / prev - 1) * 100 if (prev and c) else None,
                         "v": http.to_int(r[8]) // 1000,
                         "up_limit": http.to_float(r[17]) if len(r) > 18 else None,
                         "down_limit": http.to_float(r[18]) if len(r) > 18 else None}
    return out


def trading_stock(code, year, month):
    """個股月日K（成交仟股→張）。"""
    j = http.get_json(f"{BASE}/afterTrading/tradingStock", params={"code": code, "date": f"{year}/{month:02d}/01", "response": "json"},
                      cache_ttl=TTL_DAY)
    rows = []
    for t in j.get("tables", []):
        for r in t.get("data", []):
            try:
                y, m, d = r[0].split("/")
                date = dt.date(int(y) + 1911, int(m), int(d))
            except Exception:
                continue
            rows.append({"date": date.isoformat(), "v": http.to_int(r[1]), "amt": http.to_float(r[2]),
                         "o": http.to_float(r[3]), "h": http.to_float(r[4]), "l": http.to_float(r[5]), "c": http.to_float(r[6])})
    return [r for r in rows if r["c"] is not None]


def daily_history(code, months=5):
    today = dt.date.today()
    out = []
    y, m = today.year, today.month
    for _ in range(months):
        try:
            out = trading_stock(code, y, m) + out
        except http.HttpError:
            pass
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return out


def insti(date):
    """三大法人（張）→ {code: {foreign, trust, dealer, total}}。"""
    j = http.get_json(f"{BASE}/insti/dailyTrade", params={"type": "Daily", "sect": "EW", "date": _ad(date), "response": "json"},
                      cache_ttl=TTL_DAY)
    out = {}
    for t in j.get("tables", []):
        for r in t.get("data", []):
            if len(r) < 24:
                continue
            out[r[0].strip()] = {"foreign": http.to_int(r[10]) // 1000, "trust": http.to_int(r[13]) // 1000,
                                 "dealer": http.to_int(r[22]) // 1000, "total": http.to_int(r[23]) // 1000}
    return out


def margin(date):
    """融資融券（張）。"""
    j = http.get_json(f"{BASE}/margin/balance", params={"date": _ad(date), "response": "json"}, cache_ttl=TTL_DAY)
    out = {}
    for t in j.get("tables", []):
        for r in t.get("data", []):
            if len(r) < 18:
                continue
            code = r[0].strip()
            fin_prev, fin_now = http.to_int(r[2]), http.to_int(r[6])
            sh_prev, sh_now = http.to_int(r[10]), http.to_int(r[14])
            out[code] = {"fin_prev": fin_prev, "fin_now": fin_now, "fin_chg": fin_now - fin_prev,
                         "short_prev": sh_prev, "short_now": sh_now, "short_chg": sh_now - sh_prev,
                         "fin_limit": http.to_int(r[17]), "fin_use_pct": http.to_float(r[8])}
    return out


def disposal():
    j = http.get_json(f"{BASE}/bulletin/disposal", params={"response": "json"}, cache_ttl=3600 * 6)
    out = {}
    for t in j.get("tables", []):
        for r in t.get("data", []):
            if len(r) > 5:
                out[str(r[2]).strip()] = str(r[5]).strip()  # 處置起訖
    return out


def attention():
    j = http.get_json(f"{BASE}/bulletin/attention", params={"response": "json"}, cache_ttl=3600 * 6)
    out = set()
    for t in j.get("tables", []):
        for r in t.get("data", []):
            if len(r) > 1:
                out.add(str(r[1]).strip())
    return out
