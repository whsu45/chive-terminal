#!/usr/bin/env python3
"""
期貨走勢頁（night-futures.html）的資料：台灣市值前 50 名裡，有夜盤（盤後交易時段）期貨的個股，以及它們每天夜盤的漲跌。
輸出 data/night_futures.json。只用公開資料、標準函式庫，不需要任何金鑰。

  python scripts/build_night_futures.py              # 補最近兩個月的資料（每天排程跑這個）
  python scripts/build_night_futures.py --months 9   # 第一次或要補更長的歷史

資料來源（都是官方）：
  - 哪些股票期貨有夜盤：期交所「股票期貨 / 股票選擇權交易標的」表（最後一欄是盤後交易時間，"-" = 沒有夜盤）
  - 每天的行情：期交所「期貨每日交易行情下載」（futDataDown，一般 / 盤後兩個交易時段都有），一個商品一個月查一次
  - 市值前 50：證交所 OpenAPI 的上市公司基本資料（已發行普通股數）× 每日收盤行情（收盤價）

日期的對齊（2026-11 用 171 個交易日驗證過）：期交所把「D 前一晚 15:00（個股期貨 17:25）～ D 清晨 05:00」的夜盤記在交易日期 D，
所以交易日期 D 的夜盤漲跌，要對 D 當天的日盤開盤。夜盤漲跌 % 是對前一個日盤（D 的前一個交易日）的結算價算的。
"""
import argparse
import csv
import datetime as dt
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "night_futures.json")
UA = "Mozilla/5.0 (chive-terminal night-futures page)"
TAIFEX_LIST = "https://www.taifex.com.tw/cht/2/stockLists"
TAIFEX_DOWN = "https://www.taifex.com.tw/cht/3/futDataDown"
TWSE_SHARES = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TWSE_PRICES = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TOP_N = 50
KEEP_DAYS = 160          # 每個商品最多留幾個交易日
PAUSE = 2.5              # 每次查詢期交所之間停幾秒（不要打太快）
LOOKAHEAD_DAYS = 14      # 查詢結束日往後多查幾天（夜盤記在下一個交易日，見 build_series）
TZ = dt.timezone(dt.timedelta(hours=8))
# 大盤參考：不是個股，但要放在頁面最上面當背景
CONTEXT = [("TX", "TX", "台指期", "加權指數期貨（大台）")]


def _get(url, data=None, referer=None, timeout=60, tries=3):
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=body, headers={"User-Agent": UA, "Accept": "*/*", **({"Referer": referer} if referer else {})})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:          # noqa: BLE001 — 網路錯誤一律重試
            last = e
            time.sleep(3 * (i + 1))
    raise RuntimeError(f"{url}: {last}")


def _num(x):
    try:
        s = str(x).replace(",", "").replace("%", "").replace("▲", "").replace("▼", "-").strip()
        return float(s) if s not in ("", "-", "--") else None
    except ValueError:
        return None


# ---------------------------------------------------------------- 哪些股票期貨有夜盤

def night_eligible():
    """→ [{"fut": "CDF", "code": "2330", "name": "台積電", "night_hours": "17:25~次日05:00"}]（只收有盤後交易時段的）"""
    html = _get(TAIFEX_LIST).decode("utf-8", "ignore")
    out = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I):
        cols = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", x).replace("&nbsp;", "")).strip()
                for x in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S | re.I)]
        if len(cols) < 6 or not re.fullmatch(r"[A-Z0-9]{2}", cols[0] or ""):
            continue
        night = cols[-1]
        if night and night != "-":
            out.append({"fut": cols[0] + "F", "code": cols[2], "name": cols[3], "night_hours": night, "day_hours": cols[-2]})
    if not out:
        raise RuntimeError("期交所標的表解析不到任何有夜盤的股票期貨（網頁格式可能改了）")
    return out


# ---------------------------------------------------------------- 市值前 50

def top_market_cap(n=TOP_N):
    """上市普通股市值前 n 名 → ([{"rank", "code", "name", "mcap_e8", "close"}], 收盤資料日)。ETF、特別股不算。"""
    shares = {}
    for r in json.loads(_get(TWSE_SHARES).decode("utf-8")):
        sh = _num(r.get("已發行普通股數或TDR原股發行股數"))
        code = str(r.get("公司代號", "")).strip()
        if sh and re.fullmatch(r"[1-9]\d{3}", code):
            shares[code] = sh
    rows, day = [], None
    for r in json.loads(_get(TWSE_PRICES).decode("utf-8")):
        code = str(r.get("Code", "")).strip()
        px = _num(r.get("ClosingPrice"))
        day = day or r.get("Date")
        if code in shares and px:
            rows.append({"code": code, "name": str(r.get("Name", "")).strip(), "close": px, "mcap_e8": round(shares[code] * px / 1e8)})
    if len(rows) < 500:
        raise RuntimeError(f"證交所資料不完整（只對到 {len(rows)} 檔）")
    rows.sort(key=lambda x: -x["mcap_e8"])
    for i, x in enumerate(rows[:n], 1):
        x["rank"] = i
    if day and re.fullmatch(r"\d{7}", day):            # 民國年 1151113 → 2026-11-13
        day = f"{int(day[:3]) + 1911}-{day[3:5]}-{day[5:]}"
    return rows[:n], day


# ---------------------------------------------------------------- 期交所每日行情

def month_rows(commodity, year, month, until):
    """一個商品一個月的每日行情 → {(交易日期, "day"|"night", 到期月份): {...}}（只收一般月份契約、有成交的）。"""
    a = dt.date(year, month, 1)
    b = min(dt.date(year + (month == 12), month % 12 + 1, 1) - dt.timedelta(days=1), until)
    raw = _get(TAIFEX_DOWN, {"down_type": "1", "commodity_id": commodity, "commodity_id2": "",
                             "queryStartDate": a.strftime("%Y/%m/%d"), "queryEndDate": b.strftime("%Y/%m/%d")},
               referer="https://www.taifex.com.tw/cht/3/futDailyMarketView")
    rows = list(csv.reader(io.StringIO(raw.decode("cp950", "ignore"))))
    if not rows or "契約" not in [h.strip() for h in rows[0]]:
        return {}                                            # 這個月沒有資料（例如還沒到）會回一個網頁，不是 CSV
    h = [x.strip() for x in rows[0]]
    ix = {k: i for i, k in enumerate(h)}
    out = {}
    for r in rows[1:]:
        if len(r) <= ix["交易時段"] or r[ix["契約"]].strip() != commodity:
            continue
        exp = r[ix["到期月份(週別)"]].strip()
        if not re.fullmatch(r"\d{6}", exp):                 # 價差、週契約不要
            continue
        vol = _num(r[ix["成交量"]]) or 0
        d = r[ix["交易日期"]].strip().replace("/", "-")
        sess = "night" if "盤後" in r[ix["交易時段"]] else "day"
        out[(d, sess, exp)] = {"o": _num(r[ix["開盤價"]]), "h": _num(r[ix["最高價"]]), "l": _num(r[ix["最低價"]]), "c": _num(r[ix["收盤價"]]),
                               "chg": _num(r[ix["漲跌價"]]), "chg_pct": _num(r[ix["漲跌%"]]), "vol": int(vol), "settle": _num(r[ix["結算價"]])}
    return out


def build_series(commodity, months, old):
    """
    一個商品的逐日資料（舊到新）：每個交易日 D 一筆 {date, exp, night: {...}, day: {...}, gap_pct, prev_settle}。
      近月 = 那天日盤有成交、到期月份最小的契約；夜盤也取同一個到期月份（結算日換月時才不會混到兩個契約）。
      gap_pct = D 日盤開盤 / 前一個交易日同一契約的結算價 − 1：夜盤漲跌有沒有反映在開盤，直接拿這個比。
    old：之前存的那份（只增不減，保留 KEEP_DAYS 天）。
    """
    today = dt.datetime.now(TZ).date()
    # 查詢的結束日不能停在今天：期交所把夜盤記在「下一個」交易日，週五晚上的夜盤記在下週一、連假前一晚的記在收假那天，
    # 只查到今天的話，週末、連假期間永遠只會看到前一晚。往後多查兩週（涵蓋春節長假），還沒發生的日子期交所本來就不會有資料。
    until = today + dt.timedelta(days=LOOKAHEAD_DAYS)
    raw = {}
    y, m = until.year, until.month
    n = months + (until.year - today.year) * 12 + until.month - today.month
    for _ in range(n):
        raw.update(month_rows(commodity, y, m, until))
        time.sleep(PAUSE)
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    by_date = {}
    for (d, sess, exp), v in raw.items():
        by_date.setdefault(d, {}).setdefault(sess, {})[exp] = v
    settle = {}                                              # (日期, 到期月份) -> 日盤結算價
    for d, s in by_date.items():
        for exp, v in (s.get("day") or {}).items():
            if v.get("settle"):
                settle[(d, exp)] = v["settle"]
    dates = sorted(by_date)
    rows = {r["date"]: r for r in (old or [])}
    for i, d in enumerate(dates):
        s = by_date[d]
        day_ok = {e: v for e, v in (s.get("day") or {}).items() if v["vol"] > 0 and v["c"] is not None}
        night_ok = {e: v for e, v in (s.get("night") or {}).items() if v["vol"] > 0 and v["c"] is not None}
        exp = min(day_ok) if day_ok else (min(night_ok) if night_ok else None)
        if exp is None:
            continue
        nt = night_ok.get(exp) or (night_ok[min(night_ok)] if night_ok else None)
        dy = day_ok.get(exp)
        prev = next((settle[(dates[j], exp)] for j in range(i - 1, -1, -1) if (dates[j], exp) in settle), None)
        gap = round((dy["o"] / prev - 1) * 100, 2) if (dy and dy.get("o") and prev) else None
        rows[d] = {"date": d, "exp": exp, "prev_settle": prev, "gap_pct": gap,
                   "night": {k: nt[k] for k in ("o", "h", "l", "c", "chg", "chg_pct", "vol")} if nt else None,
                   "day": {k: dy[k] for k in ("o", "h", "l", "c", "chg", "chg_pct", "vol", "settle")} if dy else None}
    out = sorted(rows.values(), key=lambda r: r["date"])
    return out[-KEEP_DAYS:]


def stats(series):
    """這段期間「夜盤漲跌 → 當天日盤開盤跳空」的相關係數、斜率、方向一致率（夜盤 ≥±0.3% 的日子），給頁面顯示用。"""
    pts = [(r["night"]["chg_pct"], r["gap_pct"]) for r in series if r.get("night") and r["night"].get("chg_pct") is not None and r.get("gap_pct") is not None]
    if len(pts) < 20:
        return None
    xs, ys = zip(*pts)
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs); syy = sum((y - my) ** 2 for y in ys); sxy = sum((x - mx) * (y - my) for x, y in pts)
    big = [(x, y) for x, y in pts if abs(x) >= 0.3]
    return {"n": len(pts), "r": round(sxy / (sxx * syy) ** 0.5, 2) if sxx and syy else None, "slope": round(sxy / sxx, 2) if sxx else None,
            "same_dir_pct": round(sum(1 for x, y in big if (x > 0) == (y > 0)) / len(big) * 100) if big else None,
            "from": series[0]["date"], "to": series[-1]["date"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=2, help="往回補幾個月（第一次建議 9）")
    a = ap.parse_args()
    try:
        with open(OUT, encoding="utf-8") as f:
            old = json.load(f)
    except (OSError, ValueError):
        old = {}
    old_series = {c["fut"]: c.get("series") for c in (old.get("contracts") or [])}

    elig = night_eligible()
    top, top_day = top_market_cap()
    top_codes = {x["code"] for x in top}
    # 頁面要的：市值前 50 裡有夜盤期貨的個股，加上台灣 50 ETF（0050）的期貨當大盤參考
    want = [e for e in elig if e["code"] in top_codes] + [e for e in elig if e["code"] == "0050"]
    contracts = []
    for fut, code, name, desc in CONTEXT:
        contracts.append({"fut": fut, "code": code, "name": name, "desc": desc, "kind": "index", "night_hours": "15:00~次日05:00"})
    for e in want:
        contracts.append({"fut": e["fut"], "code": e["code"], "name": e["name"], "kind": "etf" if e["code"].startswith("00") else "stock",
                          "night_hours": e["night_hours"], "desc": ""})
    for c in contracts:
        try:
            c["series"] = build_series(c["fut"], a.months, old_series.get(c["fut"]))
        except Exception as e:                   # noqa: BLE001 — 一個商品失敗不影響其他的；沿用舊資料
            print(f"[warn] {c['fut']}: {str(e)[:150]}", file=sys.stderr)
            c["series"] = old_series.get(c["fut"]) or []
        c["stats"] = stats(c["series"])
        last = c["series"][-1] if c["series"] else None
        print(f"{c['fut']:4} {c['name']}: {len(c['series'])} 天，最新 {last and last['date']}"
              f" 夜盤 {last and last['night'] and last['night']['chg_pct']}%", file=sys.stderr)
    # 同一檔股票有好幾個期貨（台積電期 / 小型台積電期）：頁面卡片用夜盤成交量大的那一個
    vol = lambda c: sum((r.get("night") or {}).get("vol", 0) for r in c["series"][-20:])
    best = {}
    for c in contracts:
        if c["kind"] != "index" and (c["code"] not in best or vol(c) > vol(best[c["code"]])):
            best[c["code"]] = c
    for c in contracts:
        c["primary"] = c["kind"] == "index" or best.get(c["code"]) is c
    fut_of = {}
    for e in elig:
        fut_of.setdefault(e["code"], []).append(e["fut"])
    for x in top:
        x["night_futures"] = fut_of.get(x["code"], [])
    out = {"updated_at": dt.datetime.now(TZ).strftime("%Y-%m-%d %H:%M"), "top_date": top_day,
           "night_eligible": elig, "top50": top, "contracts": contracts,
           "sources": {"list": TAIFEX_LIST, "daily": "https://www.taifex.com.tw/cht/3/futDailyMarketReport",
                       "live": "https://mis.taifex.com.tw/futures/", "shares": TWSE_SHARES, "prices": TWSE_PRICES}}
    if not any(c["series"] for c in contracts):
        raise SystemExit("沒有抓到任何期貨行情，不覆蓋舊檔")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT + ".tmp", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(OUT + ".tmp", OUT)
    print(f"寫入 {OUT}：前 50 名裡有夜盤期貨的 {len(want)} 個契約", file=sys.stderr)


if __name__ == "__main__":
    main()
