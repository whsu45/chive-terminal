"""MoneyDJ 券商分點（經券商鏡站）：個股買賣超券商、分點進出明細。"""
import re
import http_client as http
from config import MONEYDJ_MIRRORS, DAYTRADE_BRANCHES, FOREIGN_KEYWORDS

_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")


def _fetch(path, params=None, cache_ttl=1800):
    last = None
    for base in MONEYDJ_MIRRORS:
        try:
            raw = http.get(base + path, params=params, referer=base + "/", cache_ttl=cache_ttl, retries=1)
            if len(raw) > 1500:
                return raw.decode("cp950", errors="ignore")
            last = f"回應過短 {len(raw)} bytes（{base}）"
        except http.HttpError as e:
            last = str(e)
    raise http.HttpError(f"MoneyDJ 全部鏡站失敗: {last}")


def _clean(s):
    return _TAG.sub("", s).replace("&nbsp;", "").replace("\xa0", "").strip()


def _parse_zco(html):
    """
    zco 表格每列 10 格：買超券商/買進/賣出/買超/佔比 | 賣超券商/買進/賣出/賣超/佔比
    回 {"buyers":[{broker,buy,sell,net,pct}], "sellers":[...]}
    """
    buyers, sellers = [], []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I):
        tds = [_clean(x) for x in _TD.findall(tr)]
        if len(tds) != 10:
            continue
        if not re.search(r"\d", tds[1]) and not re.search(r"\d", tds[6]):
            continue
        if tds[0] and http.to_float(tds[3]) is not None:
            buyers.append({"broker": tds[0], "buy": http.to_int(tds[1]), "sell": http.to_int(tds[2]),
                           "net": http.to_int(tds[3]), "pct": http.to_float(tds[4].rstrip("%"))})
        if tds[5] and http.to_float(tds[8]) is not None:
            sellers.append({"broker": tds[5], "buy": http.to_int(tds[6]), "sell": http.to_int(tds[7]),
                            "net": -abs(http.to_int(tds[8])), "pct": http.to_float(tds[9].rstrip("%"))})
    return {"buyers": buyers, "sellers": sellers}


def broker_summary(code):
    """最近一個交易日的買賣超券商（zco_{code}）。"""
    html = _fetch(f"/z/zc/zco/zco_{code}.djhtm")
    m = re.search(r"(\d{4}/\d{2}/\d{2})", html)
    out = _parse_zco(html)
    out["date"] = m.group(1) if m else None
    return out


def broker_summary_range(code, start, end, cache_ttl=1800):
    """區間買賣超券商（start/end: date）。"""
    html = _fetch("/z/zc/zco/zco.djhtm", params={"a": code, "e": start.strftime("%Y-%m-%d"), "f": end.strftime("%Y-%m-%d")}, cache_ttl=cache_ttl)
    return _parse_zco(html)


_ZGB = re.compile(r"GenLink2stk\('AS(\w{4,6})','([^']+)'\).*?</td>\s*<td[^>]*>([^<]*)</td>\s*<td[^>]*>([^<]*)</td>\s*<td[^>]*>([^<]*)</td>", re.S)


def branch_detail(a, b, start=None, end=None):
    """分點買賣明細 zgb0 → [{code, name, buy, sell, net}]。"""
    params = {"a": a, "b": b, "c": "E", "e": start.strftime("%Y-%m-%d") if start else "", "f": end.strftime("%Y-%m-%d") if end else ""}
    html = _fetch("/z/zg/zgb/zgb0.djhtm", params=params)
    out = []
    for code, name, buy, sell, net in _ZGB.findall(html):
        out.append({"code": code, "name": name.strip(), "buy": http.to_int(buy), "sell": http.to_int(sell), "net": http.to_int(net)})
    return out


def is_foreign(broker_name):
    return any(k in broker_name for k in FOREIGN_KEYWORDS)


def is_daytrade_branch(broker_name):
    n = broker_name.replace(" ", "")
    return any(n.startswith(k) or k in n for k in DAYTRADE_BRANCHES.keys())


def classify(summary, volume=None):
    """
    把 zco 結果拆成：隔日沖分點淨額、外資淨額、前五買/賣集中度。
    volume（張）給了就算佔比。
    """
    rows = summary.get("buyers", []) + summary.get("sellers", [])
    dt_net = sum(r["net"] for r in rows if is_daytrade_branch(r["broker"]))
    dt_buy = sum(r["net"] for r in rows if is_daytrade_branch(r["broker"]) and r["net"] > 0)
    fg_net = sum(r["net"] for r in rows if is_foreign(r["broker"]))
    top5_buy = sum(r["net"] for r in summary.get("buyers", [])[:5])
    top5_sell = sum(r["net"] for r in summary.get("sellers", [])[:5])
    res = {"daytrade_net": dt_net, "daytrade_buy": dt_buy, "foreign_net": fg_net,
           "top5_buy": top5_buy, "top5_sell": top5_sell,
           "daytrade_names": [r["broker"] for r in rows if is_daytrade_branch(r["broker"])],
           "top_buyers": summary.get("buyers", [])[:5], "top_sellers": summary.get("sellers", [])[:5]}
    if volume:
        res["daytrade_buy_pct"] = dt_buy / volume * 100
        res["foreign_net_pct"] = fg_net / volume * 100
        res["top5_buy_pct"] = top5_buy / volume * 100
        res["top5_sell_pct"] = abs(top5_sell) / volume * 100
    return res
