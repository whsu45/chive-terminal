"""證交所（上市）日資料：日K、三大法人、融資融券、借券、處置/注意、全市場收盤。"""
import datetime as dt
import http_client as http

BASE = "https://www.twse.com.tw"
TTL_DAY = 86400 * 3


def _ymd(d):
    return d.strftime("%Y%m%d")


def stock_day(code, year, month):
    """月份日K：回 [{date, o,h,l,c,v(張), amt}]。"""
    j = http.get_json(f"{BASE}/rwd/zh/afterTrading/STOCK_DAY",
                      params={"date": f"{year}{month:02d}01", "stockNo": code, "response": "json"}, cache_ttl=TTL_DAY)
    rows = []
    for r in j.get("data", []):
        try:
            y, m, d = r[0].split("/")
            date = dt.date(int(y) + 1911, int(m), int(d))
        except Exception:
            continue
        rows.append({"date": date.isoformat(), "o": http.to_float(r[3]), "h": http.to_float(r[4]),
                     "l": http.to_float(r[5]), "c": http.to_float(r[6]), "v": http.to_int(r[1]) // 1000,
                     "amt": http.to_float(r[2])})
    return [r for r in rows if r["c"] is not None]


def daily_history(code, months=5):
    today = dt.date.today()
    out = []
    y, m = today.year, today.month
    for _ in range(months):
        try:
            out = stock_day(code, y, m) + out
        except http.HttpError:
            pass
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return out


def taiex_history(ref, months=3):
    """加權指數日收盤（FMTQIK，每月一張表）→ [(YYYY-MM-DD, 收盤)]，舊到新，只取 ref 之前（不含 ref）。"""
    out = []
    y, m = ref.year, ref.month
    months_list = []
    for _ in range(months):
        months_list.append((y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    for y, m in reversed(months_list):
        j = http.get_json(f"{BASE}/rwd/zh/afterTrading/FMTQIK", params={"date": f"{y}{m:02d}01", "response": "json"},
                          cache_ttl=TTL_DAY)
        for r in j.get("data", []) or []:
            try:
                ry, rm, rd = (int(x) for x in r[0].strip().split("/"))
                d = f"{ry + 1911:04d}-{rm:02d}-{rd:02d}"
            except (ValueError, AttributeError):
                continue
            c = http.to_float(r[4])
            if c and d < ref.isoformat():
                out.append((d, c))
    return sorted(set(out))


def t86(date):
    """三大法人買賣超（股）→ {code: {foreign, trust, dealer, total}}（張）。"""
    j = http.get_json(f"{BASE}/rwd/zh/fund/T86", params={"date": _ymd(date), "selectType": "ALL", "response": "json"},
                      cache_ttl=TTL_DAY)
    out = {}
    for r in j.get("data", []):
        out[r[0].strip()] = {"foreign": http.to_int(r[4]) // 1000, "trust": http.to_int(r[10]) // 1000,
                             "dealer": http.to_int(r[11]) // 1000, "total": http.to_int(r[18]) // 1000}
    return out


def bfi82u(date):
    """全市場三大法人買賣金額（元）。"""
    j = http.get_json(f"{BASE}/rwd/zh/fund/BFI82U", params={"dayDate": _ymd(date), "type": "day", "response": "json"},
                      cache_ttl=TTL_DAY)
    out = {}
    for r in j.get("data", []):
        out[r[0].strip()] = http.to_float(r[3])  # 買賣差額
    return out


def margin(date):
    """融資融券 → {code: {fin_prev, fin_now, fin_chg, short_prev, short_now, short_chg, fin_limit}}（張）。"""
    j = http.get_json(f"{BASE}/rwd/zh/marginTrading/MI_MARGN", params={"date": _ymd(date), "selectType": "ALL", "response": "json"},
                      cache_ttl=TTL_DAY)
    tables = j.get("tables") or []
    out = {}
    if len(tables) < 2:
        return out
    for r in tables[1].get("data", []):
        code = r[0].strip()
        fin_prev, fin_now = http.to_int(r[5]), http.to_int(r[6])
        sh_prev, sh_now = http.to_int(r[11]), http.to_int(r[12])
        out[code] = {"fin_prev": fin_prev, "fin_now": fin_now, "fin_chg": fin_now - fin_prev,
                     "short_prev": sh_prev, "short_now": sh_now, "short_chg": sh_now - sh_prev,
                     "fin_limit": http.to_int(r[7])}
    return out


def sbl(date):
    """借券賣出（TWT93U）→ {code: {sbl_sell, sbl_return, sbl_bal}}（張）。"""
    j = http.get_json(f"{BASE}/rwd/zh/marginTrading/TWT93U", params={"date": _ymd(date), "response": "json"}, cache_ttl=TTL_DAY)
    out = {}
    for r in j.get("data", []):
        out[r[0].strip()] = {"sbl_sell": http.to_int(r[9]) // 1000, "sbl_return": http.to_int(r[10]) // 1000,
                             "sbl_bal": http.to_int(r[12]) // 1000}
    return out


def punish(start, end):
    """處置股 → {code: 處置期間}。"""
    j = http.get_json(f"{BASE}/rwd/zh/announcement/punish",
                      params={"startDate": _ymd(start), "endDate": _ymd(end), "response": "json"}, cache_ttl=3600 * 6)
    return {r[2].strip(): r[6] for r in j.get("data", []) if len(r) > 6}


def punish_on(day):
    """
    day 那一天的處置狀況 → (正在處置 {code: 期間}, 還沒開始 {code: 期間})。
    公告清單裡同一檔可能有好幾筆（已經結束的、接著公告的），要一筆一筆對期間；原本只看「代號有沒有在清單裡」，
    處置已經結束的也被當成處置股（2026-11-13 冠西電：兩筆分別到 11/05、11/10，當天其實已經解除）。
    期間解析不了的當成正在處置（寧可多擋）。
    """
    import datetime as dt
    import tw_calendar as cal
    j = http.get_json(f"{BASE}/rwd/zh/announcement/punish",
                      params={"startDate": _ymd(day - dt.timedelta(days=35)), "endDate": _ymd(day + dt.timedelta(days=10)), "response": "json"}, cache_ttl=3600 * 6)
    active, future = {}, {}
    for r in j.get("data", []):
        if len(r) <= 6:
            continue
        code, period = r[2].strip(), r[6]
        stt = cal.period_state(period, day)
        if stt in ("active", "unknown"):
            active[code] = period
        elif stt == "future" and (code not in future or cal.roc_period(period)[0] < cal.roc_period(future[code])[0]):
            future[code] = period
    return active, {c: p for c, p in future.items() if c not in active}


def notice(date):
    """注意股 → set(code)。"""
    j = http.get_json(f"{BASE}/rwd/zh/announcement/notice",
                      params={"startDate": _ymd(date), "endDate": _ymd(date), "response": "json"}, cache_ttl=3600 * 6)
    out = set()
    for r in j.get("data", []):
        if len(r) > 1 and r[1]:
            out.add(str(r[1]).strip())
    return out


def all_quotes(date):
    """全市場收盤 → {code: {name, v(張), c, chg_pct}}。"""
    j = http.get_json(f"{BASE}/rwd/zh/afterTrading/MI_INDEX", params={"date": _ymd(date), "type": "ALLBUT0999", "response": "json"},
                      cache_ttl=TTL_DAY)
    out = {}
    for t in j.get("tables", []):
        if "每日收盤行情" not in (t.get("title") or ""):
            continue
        for r in t.get("data", []):
            code = r[0].strip()
            c = http.to_float(r[8])
            chg = http.to_float(r[10])
            sign = -1 if "-" in (r[9] or "") else 1
            prev = c - sign * chg if (c is not None and chg is not None) else None
            out[code] = {"name": r[1].strip(), "v": http.to_int(r[2]) // 1000, "c": c,
                         "chg_pct": (c / prev - 1) * 100 if (prev and c) else None}
    return out


def market_breadth(date):
    """漲跌家數 → {up, down, flat}。"""
    j = http.get_json(f"{BASE}/rwd/zh/afterTrading/MI_INDEX", params={"date": _ymd(date), "type": "MS", "response": "json"}, cache_ttl=TTL_DAY)
    out = {}
    for t in j.get("tables", []):
        for r in t.get("data", []):
            s = r[0] if r else ""
            if "上漲" in s:
                out["up"] = http.to_int(str(r[2]).split("(")[0])
            elif "下跌" in s:
                out["down"] = http.to_int(str(r[2]).split("(")[0])
    return out
