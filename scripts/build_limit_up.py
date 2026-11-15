#!/usr/bin/env python3
"""
最新漲停個股頁（limit-up.html）的資料：最新一個交易日收在漲停價的上市、上櫃普通股，附當天的分時走勢（江波圖）。
頁面是給「當沖做空」找標的用的，所以順便標出不適合放空的兩種（頁面預設排除）：
  - 一字鎖：開盤就漲停，盤中最低價也沒離開過漲停價（開 = 低 = 漲停價）
  - 月線上多頭股（2026-11-16 前叫「強勢股」，跟 day_trading 選股頁撞名；JSON 鍵 strong 沿用）：
    收盤在月線（20 日均線 = 布林中軌）之上，而且還在布林上軌以內（中軌 < 收盤 ≤ 上軌）
輸出 data/limit_up.json。只用公開資料、標準函式庫，不需要任何金鑰。

  python scripts/build_limit_up.py

資料來源：
  - 上市：證交所「每日收盤行情」（MI_INDEX）＋「股價升降幅度」（TWT84U，當天的漲停價與參考價）
  - 上櫃：櫃買中心「上櫃股票行情」（當天行情；前一個交易日那份的「次日漲停價／次日參考價」就是當天的漲停價、參考價）
  - 日線（算月線、布林通道）與 1 分鐘分時：Yahoo Finance chart API（2330.TW、6488.TWO）
  - 隔日沖分點：當天的買賣超分點表（MoneyDJ 券商鏡站，買超、賣超各前 15 名），比對 scripts/day_trading 的動態隔日沖名單
    （daytraders.py：近 60 個交易日在大漲股（漲 ≥7%、成交 ≥3,000 張）大買、隔天倒回的分點；統計存在 scripts/day_trading/logs/daytraders_*.json，本機每天更新後推上來）。
    分點表傍晚才公布，所以 15:11 那次通常還沒有，20:11 那次才會補上。
收盤行情大約 14:30～15:00 公布，盤中、週末跑會抓到最近一個有資料的交易日。
抓不到新資料（假日、網站掛掉）時不覆蓋舊檔；同一天重跑時，這次沒抓到的上櫃或分時沿用上一份。
"""
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "limit_up.json")
UA = "Mozilla/5.0 (chive-terminal limit-up page)"
TWSE_QUOTES = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
TWSE_LIMITS = "https://www.twse.com.tw/rwd/zh/variation/TWT84U"
TPEX_QUOTES = "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes"
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/"
TZ = dt.timezone(dt.timedelta(hours=8))
MA_N, BB_K = 20, 2          # 月線 = 20 日均線；布林通道 = 20 日均線 ± 2 個標準差
EPS = 1e-6


def _get(url, params=None, timeout=40, tries=3):
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json,*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:          # noqa: BLE001 — 網路錯誤一律重試
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"{url}: {last}")


def _json(url, params=None):
    return json.loads(_get(url, params).decode("utf-8", "ignore"))


def _num(x):
    try:
        s = re.sub(r"<[^>]+>", "", str(x)).replace(",", "").strip()
        return float(s) if s not in ("", "-", "--", "---", "X") else None
    except ValueError:
        return None


def _is_stock(code):
    return bool(re.fullmatch(r"[1-9]\d{3}", code or ""))


def _same(a, b):
    return a is not None and b is not None and abs(round(a, 2) - round(b, 2)) < EPS


# ---------------------------------------------------------------- 上市

def twse_quotes(day):
    """證交所某一天的收盤行情 → {code: {...}}；那天沒開盤（或還沒公布）回 {}"""
    j = _json(TWSE_QUOTES, {"response": "json", "date": day.strftime("%Y%m%d"), "type": "ALLBUT0999"})
    if j.get("stat") != "OK":
        return {}
    tables = [t for t in j.get("tables") or [] if len(t.get("data") or []) > 500 and "證券代號" in (t.get("fields") or [])]
    if not tables:
        return {}
    f = tables[0]["fields"]
    ix = {k: i for i, k in enumerate(f)}
    out = {}
    for r in tables[0]["data"]:
        code = str(r[ix["證券代號"]]).strip()
        if not _is_stock(code):
            continue
        out[code] = {"code": code, "name": str(r[ix["證券名稱"]]).strip(), "market": "上市",
                     "vol": _num(r[ix["成交股數"]]) or 0, "value": _num(r[ix["成交金額"]]) or 0,
                     "trades": _num(r[ix["成交筆數"]]) or 0,
                     "o": _num(r[ix["開盤價"]]), "h": _num(r[ix["最高價"]]), "l": _num(r[ix["最低價"]]), "c": _num(r[ix["收盤價"]])}
    return out


def twse_limits(day):
    """證交所某一天的漲停價、參考價 → {code: (漲停價, 參考價)}"""
    j = _json(TWSE_LIMITS, {"response": "json", "date": day.strftime("%Y%m%d"), "selectType": "ALL"})
    if j.get("stat") != "OK":
        return {}
    out = {}
    for r in j.get("data") or []:
        code = str(r[0]).strip()
        if _is_stock(code):
            out[code] = (_num(r[2]), _num(r[3]))
    return out


# ---------------------------------------------------------------- 上櫃

def tpex_rows(day):
    """櫃買中心某一天的上櫃股票行情 → {code: {...}}；沒資料回 {}"""
    j = _json(TPEX_QUOTES, {"date": day.strftime("%Y/%m/%d"), "id": "", "response": "json"})
    if str(j.get("date", "")).replace("/", "")[-8:] != day.strftime("%Y%m%d"):
        return {}                                  # 那天沒資料時，有時會回別天的
    tables = [t for t in j.get("tables") or [] if len(t.get("data") or []) > 300]
    if not tables:
        return {}
    f = [re.sub(r"\s+", "", x) for x in tables[0]["fields"]]
    ix = {k: i for i, k in enumerate(f)}
    out = {}
    for r in tables[0]["data"]:
        code = str(r[ix["代號"]]).strip()
        if not _is_stock(code):
            continue
        out[code] = {"code": code, "name": str(r[ix["名稱"]]).strip(), "market": "上櫃",
                     "vol": _num(r[ix["成交股數"]]) or 0, "value": _num(r[ix["成交金額(元)"]]) or 0,
                     "trades": _num(r[ix["成交筆數"]]) or 0,
                     "o": _num(r[ix["開盤"]]), "h": _num(r[ix["最高"]]), "l": _num(r[ix["最低"]]), "c": _num(r[ix["收盤"]]),
                     "next_ref": _num(r[ix["次日參考價"]]), "next_up": _num(r[ix["次日漲停價"]])}
    return out


# ---------------------------------------------------------------- Yahoo

def yahoo(sym, params):
    j = _json(YAHOO + urllib.parse.quote(sym), params)
    res = ((j.get("chart") or {}).get("result") or [None])[0]
    if not res or not res.get("timestamp"):
        return [], {}
    q = res["indicators"]["quote"][0]
    rows = [(dt.datetime.fromtimestamp(t, TZ), q["open"][i], q["high"][i], q["low"][i], q["close"][i], q["volume"][i])
            for i, t in enumerate(res["timestamp"])]
    return rows, res.get("meta") or {}


def bands(sym, day, close):
    """月線、布林通道（含當天收盤）。Yahoo 的日線只取 day 以前，當天用交易所的收盤價補上，避免 Yahoo 還沒更新。"""
    rows, _ = yahoo(sym, {"interval": "1d", "range": "6mo"})
    closes = [c for t, _o, _h, _l, c, _v in rows if c is not None and t.date() < day]
    closes.append(close)
    if len(closes) < MA_N + 1:
        return None
    w = closes[-MA_N:]
    ma = sum(w) / MA_N
    sd = (sum((x - ma) ** 2 for x in w) / MA_N) ** 0.5
    ma_prev = sum(closes[-MA_N - 1:-1]) / MA_N
    return {"ma20": round(ma, 2), "ma20_prev": round(ma_prev, 2), "bb_up": round(ma + BB_K * sd, 2), "bb_dn": round(ma - BB_K * sd, 2),
            "prev_close": round(closes[-2], 2)}


def intraday(sym, day, limit, vol_lots=0):
    """當天 09:00～13:30 的 1 分鐘走勢 → {"t": [距 09:00 幾分鐘], "c": [收], "v": [張]} 與漲停相關的統計"""
    start = dt.datetime(day.year, day.month, day.day, 9, 0, tzinfo=TZ)
    rows, _ = yahoo(sym, {"interval": "1m", "period1": int(start.timestamp()), "period2": int(start.timestamp()) + 5 * 3600})
    rows = [r for r in rows if r[0].date() == day and r[4] is not None]
    if len(rows) < 10:
        return None
    t = [int((r[0] - start).total_seconds() // 60) for r in rows]
    tol = 0.004                         # Yahoo 的價格是浮點數（74.19999…），比對漲停價要留一點誤差
    hi = [max(x for x in (r[2], r[4]) if x is not None) for r in rows]
    lo = [min(x for x in (r[3], r[4]) if x is not None) for r in rows]
    first = next((i for i, h in enumerate(hi) if h >= limit - tol), None)
    opened = 0                          # 第一次碰到漲停之後，有幾分鐘最低價跌破漲停（漲停被打開）
    if first is not None:
        opened = sum(1 for x in lo[first + 1:] if x < limit - tol)
    v = [round((r[5] or 0) / 1000) for r in rows]
    if vol_lots and t[-1] >= 270 and v[-1] == 0:   # Yahoo 沒給 13:30 收盤集合競價的量：用全天成交量扣掉盤中的補上
        v[-1] = max(0, vol_lots - sum(v[:-1]))
    return {"t": t, "c": [round(r[4], 2) for r in rows], "v": v,
            "first_limit": t[first] if first is not None else None, "opened_min": opened, "bars": len(rows)}


# ---------------------------------------------------------------- 隔日沖分點

def daytrade_all(stocks, day):
    """
    每檔漲停股當天的隔日沖分點買超 → 直接寫進 s["dt"]：
      {"status": "ok", "buy": 張, "buy_pct": 佔量%, "expect_sell": 照過去習慣隔天約倒回幾張, "list": [{broker, net, pct, n, rate}]}
      status = "pending"（分點表還沒公布）/ "error"（連不到）。回名單的統計資訊（給頁面說明用）。
    """
    sys.path.insert(0, os.path.join(ROOT, "scripts", "day_trading"))
    os.environ.setdefault("DEVICE_ID", "ci")        # 只讀不寫：不要在 CI 上產生 .device_id
    try:
        import daytraders
        import src_moneydj
        tbl = daytraders.table(day)
    except Exception as e:                          # noqa: BLE001 — 名單載入失敗：整段略過，頁面顯示抓不到
        print(f"[warn] 隔日沖名單載入失敗：{str(e)[:150]}", file=sys.stderr)
        for s in stocks:
            s["dt"] = {"status": "error"}
        return None
    for s in stocks:
        try:
            summ = src_moneydj.broker_summary_range(s["code"], day, day, cache_ttl=0)
        except Exception as e:                      # noqa: BLE001
            print(f"[warn] {s['code']} 分點表：{str(e)[:120]}", file=sys.stderr)
            s["dt"] = {"status": "error"}
            continue
        finally:
            time.sleep(0.3)
        if not (summ.get("buyers") or summ.get("sellers")):
            s["dt"] = {"status": "pending"}
            continue
        d = daytraders.for_stock(summ, s["vol_lots"], tbl)
        rows = [{"broker": r["broker"], "net": r["net"], "pct": r["pct"], "n": r["n"],
                 "rate": r["rate"]} for r in d["list"]]
        s["dt"] = {"status": "ok", "buy": d["buy"], "buy_pct": round(d["buy"] / s["vol_lots"] * 100, 2) if s["vol_lots"] else None,
                   "expect_sell": d["expect_sell"], "list": rows}
    meta = tbl["_meta"]
    n_dt = sum(1 for k, v in tbl.items() if k != "_meta" and v.get("kind") == "dt")
    return {"source": meta["source"], "days": meta["days"], "first": meta["first"], "last": meta["last"], "branches": n_dt}


# ---------------------------------------------------------------- 主程式

def latest_day(today):
    """往回找最近一個有上市收盤行情的交易日"""
    for i in range(15):
        d = today - dt.timedelta(days=i)
        if d.weekday() >= 5:
            continue
        q = twse_quotes(d)
        time.sleep(1)
        if q:
            return d, q
    raise SystemExit("最近兩週找不到證交所收盤行情")


def load_old():
    try:
        with open(OUT, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def main():
    now = dt.datetime.now(TZ)
    old = load_old()
    day, tw = latest_day(now.date())
    if old.get("date") and old["date"] > day.isoformat():
        raise SystemExit(f"抓到的 {day} 比現有資料 {old['date']} 舊，保留現有資料")
    limits = twse_limits(day)
    if not limits:
        raise SystemExit(f"{day} 證交所漲停價（TWT84U）抓不到")
    cands = []
    for code, q in tw.items():
        up, ref = limits.get(code, (None, None))
        if up and up < 9999 and _same(q["c"], up) and q["vol"] > 0:
            cands.append({**q, "limit": up, "ref": ref})

    tpex_ok = False
    try:
        tp = tpex_rows(day)
        prev = {}
        for i in range(1, 15):                   # 前一個交易日的「次日漲停價」= 當天的漲停價
            d = day - dt.timedelta(days=i)
            if d.weekday() < 5:
                prev = tpex_rows(d)
                time.sleep(1)
                if prev:
                    break
        if tp and prev:
            tpex_ok = True
            for code, q in tp.items():
                p = prev.get(code) or {}
                up, ref = p.get("next_up"), p.get("next_ref")
                if up and up < 9999 and _same(q["c"], up) and q["vol"] > 0:
                    cands.append({**{k: v for k, v in q.items() if not k.startswith("next_")}, "limit": up, "ref": ref})
        else:
            print(f"[warn] 上櫃 {day} 行情還沒公布或抓不到", file=sys.stderr)
    except Exception as e:                       # noqa: BLE001 — 上櫃失敗時仍輸出上市
        print(f"[warn] 上櫃：{str(e)[:150]}", file=sys.stderr)

    cands.sort(key=lambda x: -x["vol"])
    stocks = []
    for q in cands:
        sym = q["code"] + (".TW" if q["market"] == "上市" else ".TWO")
        s = {"code": q["code"], "name": q["name"], "market": q["market"], "o": q["o"], "h": q["h"], "l": q["l"], "c": q["c"],
             "limit": q["limit"], "ref": q["ref"], "chg_pct": round((q["c"] / q["ref"] - 1) * 100, 2) if q["ref"] else None,
             "vol_lots": round(q["vol"] / 1000), "value_e8": round(q["value"] / 1e8, 2), "trades": int(q["trades"])}
        # 一字鎖：開盤就漲停，盤中也沒跌下來
        s["locked"] = _same(q["o"], q["limit"]) and _same(q["l"], q["limit"])
        try:
            b = bands(sym, day, q["c"])
        except Exception as e:                   # noqa: BLE001
            print(f"[warn] {sym} 日線：{str(e)[:120]}", file=sys.stderr)
            b = None
        s.update(b or {})
        if b:
            s["pos"] = "above" if q["c"] > b["bb_up"] + EPS else ("upper" if q["c"] > b["ma20"] + EPS else "below")
            s["strong"] = s["pos"] == "upper"    # 收在月線之上、布林上軌以內
        else:
            s["pos"], s["strong"] = None, None   # 上市不到一個月，算不出來
        time.sleep(0.3)
        try:
            s["intraday"] = intraday(sym, day, q["limit"], s["vol_lots"])
        except Exception as e:                   # noqa: BLE001
            print(f"[warn] {sym} 分時：{str(e)[:120]}", file=sys.stderr)
            s["intraday"] = None
        time.sleep(0.3)
        it = s["intraday"] or {}
        print(f"{q['market']} {q['code']} {q['name']}: 量 {s['vol_lots']} 張 一字鎖={s['locked']} 月線上多頭={s['strong']}"
              f" 分時 {it.get('bars', 0)} 根", file=sys.stderr)
        stocks.append(s)

    dt_meta = daytrade_all(stocks, day)
    n_ok = sum(1 for s in stocks if (s.get("dt") or {}).get("status") == "ok")
    print(f"隔日沖分點：{n_ok}/{len(stocks)} 檔有分點表", file=sys.stderr)

    # 同一天重跑時，這次沒抓到的部分（上櫃還沒公布、Yahoo 偶發失敗、分點表連不到）沿用上一份
    if old.get("date") == day.isoformat():
        prev = {x["code"]: x for x in old.get("stocks") or []}
        for s in stocks:
            p = prev.get(s["code"])
            if not p:
                continue
            if not s.get("intraday") and p.get("intraday"):
                s["intraday"] = p["intraday"]
            if s.get("ma20") is None and p.get("ma20") is not None:
                for k in ("ma20", "ma20_prev", "bb_up", "bb_dn", "prev_close", "pos", "strong"):
                    s[k] = p.get(k)
            if (s.get("dt") or {}).get("status") != "ok" and (p.get("dt") or {}).get("status") == "ok":
                s["dt"] = p["dt"]
        if not dt_meta and old.get("dt_meta"):
            dt_meta = old["dt_meta"]
        if not tpex_ok and old.get("tpex_ok"):
            kept = [x for x in old.get("stocks") or [] if x.get("market") == "上櫃"]
            stocks = sorted(stocks + kept, key=lambda x: -x["vol_lots"])
            tpex_ok = True
            print(f"[info] 上櫃沿用上一份的 {len(kept)} 檔", file=sys.stderr)
    if not stocks and old.get("stocks"):
        raise SystemExit(f"{day} 一檔漲停都沒抓到，保留現有資料（{old.get('date')}）")

    out = {"updated_at": now.strftime("%Y-%m-%d %H:%M"), "date": day.isoformat(), "tpex_ok": tpex_ok, "dt_meta": dt_meta,
           "counts": {"twse": sum(x["market"] == "上市" for x in stocks), "tpex": sum(x["market"] == "上櫃" for x in stocks)},
           "stocks": stocks,
           "sources": {"twse": "https://www.twse.com.tw/zh/trading/historical/mi-index.html",
                       "twse_limits": "https://www.twse.com.tw/zh/trading/variation/twt84u.html",
                       "tpex": "https://www.tpex.org.tw/zh-tw/mainboard/trading/info/pricing.html",
                       "yahoo": "https://finance.yahoo.com/",
                       "broker": "https://5850web.moneydj.com/z/zc/zco/zco.djhtm"}}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT + ".tmp", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(OUT + ".tmp", OUT)
    print(f"寫入 {OUT}：{day} 漲停 {len(stocks)} 檔（上市 {out['counts']['twse']}、上櫃 {out['counts']['tpex']}）", file=sys.stderr)


if __name__ == "__main__":
    main()
