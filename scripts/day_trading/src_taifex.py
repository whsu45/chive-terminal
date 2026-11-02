"""期交所：台指期夜盤、外資期貨未平倉。"""
import re
import datetime as dt
import http_client as http

REPORT = "https://www.taifex.com.tw/cht/3/futDailyMarketReport"
OI = "https://www.taifex.com.tw/cht/3/futContractsDate"
_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")


def _rows(html):
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I):
        yield [_TAG.sub("", x).replace("&nbsp;", "").strip() for x in _TD.findall(tr)]


def tx_session(query_date: dt.date, night=True):
    """
    台指期近月行情。期交所把週五夜盤歸到下週一交易日：要看 10/30 夜盤，query_date 用 11/02。
    回 {contract, o,h,l,c, chg, chg_pct, vol, title}
    """
    payload = {"queryType": "2", "marketCode": "1" if night else "0", "dateaddcnt": "", "commodity_id": "TX",
               "commodity_id2": "", "queryDate": query_date.strftime("%Y/%m/%d"), "MarketCode": "1" if night else "0",
               "commodity_idt": "TX", "commodity_id2t": "", "commodity_id2t2": ""}
    html = http.post(REPORT, payload, referer=REPORT, cache_ttl=1800).decode("utf-8", errors="ignore")
    m = re.search(r"(\d{4}/\d{1,2}/\d{1,2})(?:&nbsp;|\s)*15:00\s*~\s*次日", html)
    title = m.group(1) if m else None  # 夜盤實際開盤日
    for cols in _rows(html):
        if len(cols) >= 9 and cols[0] == "TX" and re.match(r"\d{6}", cols[1] or ""):
            o, h, l, c = (http.to_float(cols[i]) for i in (2, 3, 4, 5))
            chg = http.to_float(re.sub(r"[^\d.\-]", "", cols[6] or ""))
            pct = http.to_float(re.sub(r"[^\d.\-]", "", cols[7] or ""))
            return {"contract": cols[1], "o": o, "h": h, "l": l, "c": c, "chg": chg, "chg_pct": pct,
                    "vol": http.to_int(cols[8]), "title": title}
    return None


def foreign_tx_oi(date: dt.date):
    """外資台指期未平倉淨額（口）。"""
    payload = {"queryType": "1", "goDay": "", "doQuery": "1", "dateaddcnt": "", "queryDate": date.strftime("%Y/%m/%d"),
               "commodityId": "TXF"}
    html = http.post(OI, payload, referer=OI, cache_ttl=1800).decode("utf-8", errors="ignore")
    for cols in _rows(html):
        if any("外資" in c for c in cols[:3]):
            nums = [http.to_int(c) for c in cols if re.fullmatch(r"-?[\d,]+", c or "")]
            if len(nums) >= 11:
                return {"net_oi": nums[10], "raw": nums}
    return None
