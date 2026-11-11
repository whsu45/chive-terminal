"""MoneyDJ 券商分點（經券商鏡站）：個股買賣超券商、分點進出明細。"""
import os
import re
import json
import threading
import datetime as dt
import http_client as http
import tw_calendar as cal
from config import MONEYDJ_MIRRORS, DAYTRADE_BRANCHES, FOREIGN_KEYWORDS, CACHE_DIR

_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_BID = re.compile(r"[?&]b=([0-9A-Za-z]+)&(?:amp;)?BHID=([0-9A-Za-z]+)", re.I)   # 分點代號 b、所屬券商 BHID（zco0 / zgb0 查詢用）
ZCO_DIR = os.path.join(CACHE_DIR, "zco")     # 單日分點表的永久存檔（.cache 不進 git）


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
    回 {"buyers":[{broker,bid,bhid,buy,sell,net,pct}], "sellers":[...]}（賣超的 net 為負）
    """
    buyers, sellers = [], []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I):
        raw = _TD.findall(tr)
        tds = [_clean(x) for x in raw]
        if len(tds) != 10:
            continue
        if not re.search(r"\d", tds[1]) and not re.search(r"\d", tds[6]):
            continue
        for off, dst, sign in ((0, buyers, 1), (5, sellers, -1)):
            if tds[off] and http.to_float(tds[off + 3]) is not None:
                m = _BID.search(raw[off])
                dst.append({"broker": tds[off], "bid": m.group(1) if m else None, "bhid": m.group(2) if m else None,
                            "buy": http.to_int(tds[off + 1]), "sell": http.to_int(tds[off + 2]),
                            "net": sign * abs(http.to_int(tds[off + 3])), "pct": http.to_float(tds[off + 4].rstrip("%"))})
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


# ---- 單日分點表的永久存檔 ----
_zco_lock = threading.Lock()
_zco_mem = {}
_ZCO_KEYS = ("broker", "bhid", "bid", "buy", "sell", "net", "pct")


def _zco_load(code):
    if code not in _zco_mem:
        try:
            with open(os.path.join(ZCO_DIR, f"{code}.json"), encoding="utf-8") as f:
                _zco_mem[code] = json.load(f)
        except (OSError, ValueError):
            _zco_mem[code] = {}
    return _zco_mem[code]


def _zco_save(code):
    os.makedirs(ZCO_DIR, exist_ok=True)
    path = os.path.join(ZCO_DIR, f"{code}.json")
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(_zco_mem[code], f, ensure_ascii=False, separators=(",", ":"))
    os.replace(path + ".tmp", path)


def _zco_unpack(rec):
    return {"buyers": [dict(zip(_ZCO_KEYS, r)) for r in rec.get("b", [])],
            "sellers": [dict(zip(_ZCO_KEYS, r)) for r in rec.get("s", [])]}


def zco_put(code, d, summ):
    """把某一天的分點表寫進永久存檔。"""
    with _zco_lock:
        db = _zco_load(code)
        db[d.isoformat()] = {"b": [[r.get(k) for k in _ZCO_KEYS] for r in summ.get("buyers", [])],
                             "s": [[r.get(k) for k in _ZCO_KEYS] for r in summ.get("sellers", [])]}
        _zco_save(code)


def zco_day(code, d, fetch=True):
    """
    某一個交易日的分點表（買超、賣超各前 15 名）→ {"buyers": [...], "sellers": [...]}；沒有存檔又不准連線時回 None。
    過去的交易日不會再變，抓一次就存在 .cache/zco/{code}.json，之後不再連線。
    （http 檔案快取跨日就失效，原本常勝主力每檔每天都要重抓 20 頁。）當天的表可能還沒出齊，不存檔。
    """
    iso = d.isoformat()
    with _zco_lock:
        rec = _zco_load(code).get(iso)
    if rec is not None:
        return _zco_unpack(rec)
    if not fetch:
        return None
    today = cal.today_tw()
    summ = broker_summary_range(code, d, d, cache_ttl=0 if d < today else 1800)
    has_rows = bool(summ.get("buyers") or summ.get("sellers"))
    # 空表：三天前的日期才當成「那天真的沒資料」存起來，近兩天的可能只是還沒公布
    if d < today and (has_rows or (today - d).days >= 3):
        zco_put(code, d, summ)
    return summ


def zco_fetch_many(pairs, pause=0.3, progress=None):
    """
    一次補很多張單日分點表：pairs = [(code, date)]，已存檔的跳過，只抓過去的交易日。回 (存了幾張, 沒抓到幾張)。
    三個鏡站各開一條線、每站一次只抓一頁再停 pause 秒，所以對任何一站都不比原本單線快，只是總時間縮成三分之一。
    """
    import queue
    today = cal.today_tw()
    todo = queue.Queue()
    n_todo = 0
    for code, d in dict.fromkeys(pairs):
        if d < today and d.isoformat() not in _zco_load(code):
            todo.put((code, d, 0)); n_todo += 1
    stat = {"ok": 0, "miss": 0}
    if not n_todo:
        return 0, 0

    def worker(base):
        import time
        while True:
            try:
                code, d, tries = todo.get_nowait()
            except queue.Empty:
                return
            try:
                iso = d.strftime("%Y-%m-%d")
                raw = http.get(base + "/z/zc/zco/zco.djhtm", params={"a": code, "e": iso, "f": iso}, referer=base + "/", retries=0)
                if len(raw) <= 1500:
                    raise http.HttpError("回應過短")
                summ = _parse_zco(raw.decode("cp950", errors="ignore"))
                if summ["buyers"] or summ["sellers"] or (today - d).days >= 3:
                    zco_put(code, d, summ)
                stat["ok" if (summ["buyers"] or summ["sellers"]) else "miss"] += 1
            except Exception:
                if tries < 2:
                    todo.put((code, d, tries + 1))     # 換別的鏡站（或同一站稍後）再試，最多三次
                else:
                    stat["miss"] += 1
                time.sleep(2)
            if progress:
                progress(stat["ok"] + stat["miss"], n_todo)
            time.sleep(pause)

    ths = [threading.Thread(target=worker, args=(b,), daemon=True) for b in MONEYDJ_MIRRORS]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    return stat["ok"], stat["miss"]


def zco_stored(code):
    """這檔股票已存檔的日期（ISO 字串，舊到新）。"""
    with _zco_lock:
        return sorted(_zco_load(code))


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
    """是不是隔日沖分點：用 daytraders 的動態名單（看分點實際的大買、隔天倒回紀錄）；動態資料不足時才用 config 的固定名單。"""
    try:
        import daytraders
        return daytraders.is_daytrader(broker_name)
    except Exception:
        n = broker_name.replace(" ", "")
        return any(n.startswith(k) or k in n for k in DAYTRADE_BRANCHES.keys())


def classify(summary, volume=None):
    """
    把 zco 結果拆成：隔日沖分點淨額、外資淨額、前五買/賣集中度。
    volume（張）給了就算佔比。
    """
    rows = summary.get("buyers", []) + summary.get("sellers", [])
    dts = [r for r in rows if is_daytrade_branch(r["broker"])]
    dt_net = sum(r["net"] for r in dts)
    dt_buyers = [r for r in dts if r["net"] > 0]
    dt_buy = sum(r["net"] for r in dt_buyers)
    fg_net = sum(r["net"] for r in rows if is_foreign(r["broker"]))
    top5_buy = sum(r["net"] for r in summary.get("buyers", [])[:5])
    top5_sell = sum(r["net"] for r in summary.get("sellers", [])[:5])
    res = {"daytrade_net": dt_net, "daytrade_buy": dt_buy, "foreign_net": fg_net,
           "top5_buy": top5_buy, "top5_sell": top5_sell,
           "daytrade_names": [r["broker"] for r in dt_buyers],       # 只列買超的（原本連賣超的也列進來）
           "top_buyers": summary.get("buyers", [])[:5], "top_sellers": summary.get("sellers", [])[:5]}
    if volume:
        res["daytrade_buy_pct"] = dt_buy / volume * 100
        res["foreign_net_pct"] = fg_net / volume * 100
        res["top5_buy_pct"] = top5_buy / volume * 100
        res["top5_sell_pct"] = abs(top5_sell) / volume * 100
    # 單日表自己有「佔成交比重」欄，比用外面傳進來的成交量準（傍晚重建盤前資料時，最新的表是今天的、傳進來的量卻是昨天的）
    if dt_buyers and all(r.get("pct") is not None for r in dt_buyers) and summary.get("date"):
        res["daytrade_buy_pct"] = sum(r["pct"] for r in dt_buyers)
    return res
