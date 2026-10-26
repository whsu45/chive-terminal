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
      - 漲跌來自證交所 / 櫃買中心官方收盤行情
      - 族群依成分股平均漲跌幅排序，族群內個股依漲跌幅排序
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

    quotes = fetch_daily_quotes(session)
    quote_date = max((q["date"] for q in quotes.values()), default=None)

    out_themes = []
    for t in theme_defs:
        members = []
        for m in t["members"]:
            q = quotes.get(m["code"], {})
            members.append({**m, **{k: q.get(k) for k in ("market", "open", "high", "low", "close", "change", "pct", "volume")}})
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
        "members_fetched": members_fetched,
        "source": DATA_SOURCES["finlab"]["themes_url"],
        "themes": out_themes,
    }
    with open(THEMES_JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    print(f"成功更新族群漲跌：{len(out_themes)} 個族群，行情日 {quote_date}")
    return result
