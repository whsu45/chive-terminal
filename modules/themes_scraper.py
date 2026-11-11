import re
import json
import time
import html
from datetime import datetime, timezone, timedelta
from .utils import DATA_SOURCES, THEMES_JSON_FILE


HEADERS = {'User-Agent': 'Mozilla/5.0 (chive-terminal theme tracker)'}
# 依 finlab robots.txt 精神控制抓取頻率
REQUEST_INTERVAL_SEC = 1.2


def _strip_tags(text):
    return html.unescape(re.sub(r'<[^>]+>', '', text)).strip()


def fetch_theme_list(session):
    """ 解析 finlab 族群總覽頁：回傳 [{slug, title, desc}] (依頁面順序) """
    url = DATA_SOURCES["finlab"]["themes_url"]
    resp = session.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    resp.encoding = 'utf-8'  # finlab 未宣告 charset
    page = re.sub(r'<!--.*?-->', '', resp.text, flags=re.S)

    themes = []
    for m in re.finditer(r'<a class="theme-card[^"]*"[^>]*href="/stocks/themes/([a-z0-9-]+)"[^>]*>(.*?)</a>', page, re.S):
        slug, body = m.group(1), m.group(2)
        title = re.search(r'class="tc-title[^"]*">(.*?)</span>', body, re.S)
        desc = re.search(r'class="tc-desc[^"]*">(.*?)</span>', body, re.S)
        themes.append({
            "slug": slug,
            "title": _strip_tags(title.group(1)) if title else slug,
            "desc": _strip_tags(desc.group(1)) if desc else "",
        })
    return themes


def fetch_theme_members(session, slug):
    """
    解析族群內頁的成分股名單。
    主要來源為頁面上的名單句：「…共 9 檔：南亞科（2408）、華邦電（2344）…。」
    若句型改版，退回解析名單表格中的 /stocks/{代號} 連結。
    """
    url = f"{DATA_SOURCES['finlab']['themes_url']}/{slug}"
    resp = session.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    resp.encoding = 'utf-8'  # finlab 未宣告 charset
    page = re.sub(r'<!--.*?-->', '', resp.text, flags=re.S)
    text = _strip_tags(page)

    members = []
    m = re.search(r'共\s*(\d+)\s*檔[：:](.+?)。', text)
    if m:
        expected = int(m.group(1))
        for name, code in re.findall(r'([^、（(]+?)[（(]([0-9A-Z]{4,6})[）)]', m.group(2)):
            members.append({"code": code, "name": name.strip()})
        if len(members) == expected:
            return members

    # 備援：名單表格連結
    seen = set()
    members = []
    for code, name in re.findall(r'href="/stocks/([0-9A-Z]{4,6})"[^>]*>([^<（(]+)', page):
        if code not in seen:
            seen.add(code)
            members.append({"code": code, "name": name.strip()})
    return members


def _roc_to_iso(roc):
    """ '1151022' → '2026-10-22' """
    roc = roc.strip()
    return f"{int(roc[:-4]) + 1911}-{roc[-4:-2]}-{roc[-2:]}"


def _to_float(text):
    try:
        return float(str(text).replace(',', '').strip())
    except (TypeError, ValueError):
        return None


def fetch_daily_quotes(session):
    """ 抓取上市 (TWSE) 與上櫃 (TPEx) 最新一個交易日全部個股收盤行情 → {code: quote} """
    quotes = {}

    try:
        resp = session.get(DATA_SOURCES["twse"]["daily_all_url"], headers=HEADERS, timeout=20)
        for r in resp.json():
            close, chg = _to_float(r.get("ClosingPrice")), _to_float(r.get("Change"))
            quotes[r["Code"].strip()] = {
                "market": "上市", "date": _roc_to_iso(r["Date"]),
                "open": _to_float(r.get("OpeningPrice")), "high": _to_float(r.get("HighestPrice")),
                "low": _to_float(r.get("LowestPrice")), "close": close, "change": chg,
                "volume": round((_to_float(r.get("TradeVolume")) or 0) / 1000),
            }
    except Exception as e:
        print(f"TWSE 全部個股行情抓取失敗: {e}")

    try:
        resp = session.get(DATA_SOURCES["tpex"]["daily_close_quotes_url"], headers=HEADERS, timeout=30)
        for r in resp.json():
            close, chg = _to_float(r.get("Close")), _to_float(r.get("Change"))
            quotes[r["SecuritiesCompanyCode"].strip()] = {
                "market": "上櫃", "date": _roc_to_iso(r["Date"]),
                "open": _to_float(r.get("Open")), "high": _to_float(r.get("High")),
                "low": _to_float(r.get("Low")), "close": close, "change": chg,
                "volume": round((_to_float(r.get("TradingShares")) or 0) / 1000),
            }
    except Exception as e:
        print(f"TPEx 全部個股行情抓取失敗: {e}")

    for q in quotes.values():
        prev = (q["close"] - q["change"]) if q["close"] is not None and q["change"] is not None else None
        q["pct"] = round(q["change"] / prev * 100, 2) if prev else None
    return quotes


MIS_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
MIS_HOME = "https://mis.twse.com.tw/stock/index.jsp"
MIS_BATCH = 60            # 一次查幾檔
MIS_MIN_TRADED = 0.3      # 今天有成交的檔數要佔這個比例以上才採用（開盤前試撮時大家都還沒成交，不能當成今天的行情）


def _mis_price(r):
    """MIS 一檔的最新成交價：z 這 5 秒有成交才有值，沒有就用 trade.z（今天最後一筆）。兩者都沒有 = 今天還沒成交。"""
    z = _to_float(r.get("z"))
    if z:
        return z
    tr = r.get("trade") if isinstance(r.get("trade"), dict) else {}
    return _to_float(tr.get("z")) or None


def fetch_intraday_quotes(session, markets):
    """
    證交所 MIS 即時報價（上市、上櫃同一個來源，盤中約 5 秒更新；收盤後留著當天最後的價格）。
    markets: {code: "上市"|"上櫃"|None}，不知道市場的兩邊都問。
    回 ({code: quote}, 行情日 "YYYY-MM-DD", 最新一筆的時間 "HH:MM:SS")；整批失敗或今天還沒開始成交回 ({}, None, None)。
    quote 的欄位跟收盤檔一樣，漲跌 = 最新成交價 − 昨收；今天還沒成交的股票記成平盤、量 0、no_trade=True。
    """
    ex = []
    for code, mk in markets.items():
        if mk in ("上市", None):
            ex.append(f"tse_{code}.tw")
        if mk in ("上櫃", None):
            ex.append(f"otc_{code}.tw")
    hdr = {**HEADERS, "Referer": MIS_HOME}
    try:
        session.get(MIS_HOME, headers=HEADERS, timeout=10)      # 先拿 session cookie，有些環境沒有 cookie 會回空陣列
    except Exception:
        pass
    rows, failed = {}, 0
    for i in range(0, len(ex), MIS_BATCH):
        try:
            resp = session.get(MIS_URL, params={"ex_ch": "|".join(ex[i:i + MIS_BATCH]), "json": "1", "delay": "0"}, headers=hdr, timeout=15)
            for r in resp.json().get("msgArray") or []:
                if r.get("c") and r.get("d") and _to_float(r.get("y")):
                    rows[r["c"].strip()] = r
        except Exception as e:
            failed += 1
            print(f"  MIS 即時報價第 {i // MIS_BATCH + 1} 批失敗: {str(e)[:100]}")
        time.sleep(0.4)
    if not rows:
        print(f"MIS 即時報價沒有拿到資料（{failed} 批失敗），改用收盤行情。")
        return {}, None, None

    day = max(r["d"] for r in rows.values())
    today = [r for r in rows.values() if r["d"] == day]
    traded = [r for r in today if _mis_price(r)]
    if len(traded) < MIS_MIN_TRADED * len(today):
        print(f"MIS 行情日 {day} 只有 {len(traded)}/{len(today)} 檔有成交（開盤前或資料還沒進來），改用收盤行情。")
        return {}, None, None

    iso = f"{day[:4]}-{day[4:6]}-{day[6:]}"
    quotes = {}
    for code, r in rows.items():
        if r["d"] != day:
            continue
        y, px = _to_float(r["y"]), _mis_price(r)
        q = {"market": "上市" if r.get("ex") == "tse" else "上櫃", "date": iso, "time": r.get("t"),
             "open": _to_float(r.get("o")), "high": _to_float(r.get("h")), "low": _to_float(r.get("l"))}
        if px:
            chg = round(px - y, 2)
            q.update(close=px, change=chg, pct=round(chg / y * 100, 2), volume=int(_to_float(r.get("v")) or 0))
        else:
            q.update(close=y, change=0.0, pct=0.0, volume=0, no_trade=True, open=None, high=None, low=None)
        quotes[code] = q
    last_t = max((r.get("t") or "" for r in traded), default=None)
    return quotes, iso, last_t


def _load_previous():
    try:
        with open(THEMES_JSON_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def update_themes_json(session):
    """
    產生 data/themes.json：
      - 族群與成分股名單來自 finlab（每天最多重抓一次，名單抓取失敗時沿用上次結果）
      - 漲跌優先用證交所 MIS 即時報價（盤中是當下的成交價對昨收；收盤後是當天收盤價）
      - MIS 拿不到（連不上、開盤前還沒成交）的股票才退回證交所 / 櫃買中心的收盤行情檔（盤中那是前一個交易日）
      - 族群依成分股平均漲跌幅排序，族群內個股依漲跌幅排序
    輸出多了 quote_time / quote_source / intraday / date_counts，網頁用來標明「這是幾點的盤中報價」或「這是哪一天的收盤」。
    """
    tw_today = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d")
    prev = _load_previous()

    if prev and prev.get("members_fetched") == tw_today and prev.get("themes"):
        print("族群名單今日已更新，沿用快取。")
        theme_defs = [{k: t[k] for k in ("slug", "title", "desc", "members")} for t in prev["themes"]]
        members_fetched = prev["members_fetched"]
    else:
        theme_defs = []
        members_fetched = tw_today
        try:
            themes = fetch_theme_list(session)
            print(f"finlab 族群共 {len(themes)} 個，開始抓取成分股…")
            prev_by_slug = {t["slug"]: t for t in (prev or {}).get("themes", [])}
            for t in themes:
                time.sleep(REQUEST_INTERVAL_SEC)
                try:
                    t["members"] = [{"code": m["code"], "name": m["name"]} for m in fetch_theme_members(session, t["slug"])]
                except Exception as e:
                    print(f"  {t['slug']} 成分股抓取失敗: {e}")
                    t["members"] = [{"code": m["code"], "name": m["name"]} for m in prev_by_slug.get(t["slug"], {}).get("members", [])]
                theme_defs.append(t)
        except Exception as e:
            print(f"finlab 族群總覽抓取失敗: {e}")
            if prev:
                theme_defs = [{k: t[k] for k in ("slug", "title", "desc", "members")} for t in prev["themes"]]
                members_fetched = prev.get("members_fetched")

    if not theme_defs:
        print("無族群資料，略過 themes.json。")
        return prev

    closing = fetch_daily_quotes(session)
    all_codes = list(dict.fromkeys(m["code"] for t in theme_defs for m in t["members"]))
    live, live_date, live_time = {}, None, None
    try:
        live, live_date, live_time = fetch_intraday_quotes(session, {c: (closing.get(c) or {}).get("market") for c in all_codes})
    except Exception as e:
        print(f"MIS 即時報價失敗，改用收盤行情: {e}")
    closing_date = max((q["date"] for q in closing.values()), default=None)
    # 即時報價的行情日不比收盤檔舊才採用（正常情況：盤中與收盤後都是今天；收盤檔通常晚幾個小時才更新）
    if live and closing_date and live_date < closing_date:
        print(f"MIS 行情日 {live_date} 比收盤檔 {closing_date} 舊，不採用。")
        live = {}
    quotes = {c: (live.get(c) or closing.get(c) or {}) for c in all_codes}
    quote_date = max((q["date"] for q in quotes.values() if q.get("date")), default=None)
    now_tw = datetime.now(timezone.utc) + timedelta(hours=8)
    # 盤中 = 用的是今天的即時報價，而且現在還沒過 13:35（收盤後 MIS 留著的就是收盤價）
    intraday = bool(live) and live_date == now_tw.strftime("%Y-%m-%d") and now_tw.strftime("%H:%M") < "13:35"
    date_counts = {}
    for q in quotes.values():
        if q.get("pct") is not None:
            date_counts[q["date"]] = date_counts.get(q["date"], 0) + 1

    out_themes = []
    for t in theme_defs:
        members = []
        for m in t["members"]:
            q = quotes.get(m["code"], {})
            row = {**m, **{k: q.get(k) for k in ("market", "open", "high", "low", "close", "change", "pct", "volume")}}
            if q.get("no_trade"):
                row["no_trade"] = True                       # 今天還沒成交：以平盤計
            if q.get("date") and q["date"] != quote_date:
                row["date"] = q["date"]                      # 這檔的行情日跟整頁不同（沒拿到今天的報價）
            members.append(row)
        priced = [m for m in members if m["pct"] is not None]
        members.sort(key=lambda m: (m["pct"] is None, -(m["pct"] or 0)))
        avg = round(sum(m["pct"] for m in priced) / len(priced), 2) if priced else None
        out_themes.append({
            **{k: t[k] for k in ("slug", "title", "desc")},
            "members": members,
            "avg_pct": avg,
            "up": sum(1 for m in priced if m["pct"] > 0),
            "down": sum(1 for m in priced if m["pct"] < 0),
            "flat": sum(1 for m in priced if m["pct"] == 0),
        })
    out_themes.sort(key=lambda t: (t["avg_pct"] is None, -(t["avg_pct"] or 0)))

    result = {
        "quote_date": quote_date,
        "quote_time": live_time if live else None,
        "quote_source": "mis" if live else "close",
        "intraday": intraday,
        "date_counts": date_counts,
        "members_fetched": members_fetched,
        "source": DATA_SOURCES["finlab"]["themes_url"],
        "themes": out_themes,
    }
    with open(THEMES_JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    src = f"MIS 即時報價 {live_time}（{'盤中' if intraday else '收盤後'}，{len(live)} 檔）" if live else "收盤行情檔"
    print(f"成功更新族群漲跌：{len(out_themes)} 個族群，行情日 {quote_date}，來源 {src}，各行情日檔數 {date_counts}")
    return result
