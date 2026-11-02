"""Yahoo Finance chart API：台股日K/5分K備援、美股與費半夜盤參考。注意 TW 分K約延遲 20 分鐘。"""
import time
import datetime as dt
import http_client as http

URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"


def chart(symbol, interval="1d", range_="6mo", cache_ttl=0):
    """Yahoo 會對頻繁請求回 429，所有呼叫都走快取；失敗就拋 HttpError 讓上層改用官方來源。"""
    time.sleep(0.4)
    j = http.get_json(URL.format(symbol=symbol), params={"interval": interval, "range": range_}, cache_ttl=cache_ttl, retries=0)
    res = (j.get("chart") or {}).get("result") or []
    if not res:
        return []
    r = res[0]
    ts = r.get("timestamp") or []
    q = (r.get("indicators") or {}).get("quote") or [{}]
    q = q[0]
    out = []
    for i, t in enumerate(ts):
        c = q.get("close", [None])[i] if i < len(q.get("close", [])) else None
        if c is None:
            continue
        local = dt.datetime.utcfromtimestamp(t) + dt.timedelta(hours=8)
        out.append({"ts": local, "date": local.date().isoformat(), "o": q["open"][i], "h": q["high"][i], "l": q["low"][i],
                    "c": c, "v": (q["volume"][i] or 0)})
    return out


def tw_daily(code, market, months=6):
    sym = f"{code}.TW" if market == "tse" else f"{code}.TWO"
    rows = chart(sym, "1d", f"{months}mo", cache_ttl=3600)
    today = dt.date.today().isoformat()
    return [{"date": r["date"], "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"], "v": int(r["v"] // 1000)} for r in rows if r["date"] < today]


def tw_intraday(code, market, interval="5m"):
    sym = f"{code}.TW" if market == "tse" else f"{code}.TWO"
    return chart(sym, interval, "1d")


def us_overnight():
    """費半、那斯達克、台積ADR、EWT 最近兩根日K → 漲跌幅%。"""
    out = {}
    for key, sym in {"SOX": "^SOX", "NASDAQ": "^IXIC", "TSM_ADR": "TSM", "EWT": "EWT", "SPX": "^GSPC"}.items():
        try:
            rows = chart(sym, "1d", "5d", cache_ttl=1800)
            if len(rows) >= 2:
                out[key] = {"date": rows[-1]["date"], "c": rows[-1]["c"], "pct": (rows[-1]["c"] / rows[-2]["c"] - 1) * 100}
        except http.HttpError:
            pass
    return out
