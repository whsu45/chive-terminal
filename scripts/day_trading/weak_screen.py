#!/usr/bin/env python3
"""
弱勢股篩選（收盤後跑，挑隔天要盯的放空候選）。條件出自 Threads 貼文，經回測驗證後採用：
  劣於大盤（10 日報酬 < 加權指數 10 日報酬）、10 日跌幅 > 10%、收盤 < 季線（MA60）、股價 > 100、外資與投信當天都賣超；
  另加流動性：當天成交 ≥ 300 張。
回測（2026/05/28–11/05，109 天，隔天開盤空、收盤補，未扣成本）：
  大盤弱（加權 < 20 日線，41 天）：五條件全中 +1.10% vs 所有 >100 元股票 +0.77%，多 0.33%；前後半段都成立，勝率 60%。
  大盤不弱（68 天）：−0.15% vs +0.22%，反而比隨便空差 → 只在大盤弱時有用。
同一套條件也給 prep.py 當盤前因子（model「弱勢股篩選」），所以兩邊共用 conditions()。

    python3 weak_screen.py                  # 最近一個有資料的交易日
    python3 weak_screen.py --date 2026-11-06
    python3 weak_screen.py --near           # 另列只差一個條件的
全市場資料（日 K + 法人）存在 .cache/screen_daily.json（不進 git），第一次要抓約 65 天、之後每天只補新的一天。
"""
import os
import sys
import json
import time
import argparse
import datetime as dt

from config import CACHE_DIR
import http_client as http
import tw_calendar as cal
import src_twse

STORE = os.path.join(CACHE_DIR, "screen_daily.json")
MIN_VOL = 300
MIN_PX = 100
DROP10 = -10.0
TTL = 86400 * 365


def conditions(closes, idx_closes, foreign, trust, vol=None):
    """
    closes：個股日收盤，舊到新，最後一筆 = 篩選日（至少 60 筆才算季線）；idx_closes：加權指數日收盤，同樣舊到新、最後一筆同一天（至少 20 筆）。
    foreign / trust：篩選日外資、投信買賣超（張）。回 dict（hit = 五條件全中；mkt_weak = 加權 < 20 日線）；資料不夠回 None。
    """
    if len(closes) < 60 or len(idx_closes) < 20:
        return None
    c, c10 = closes[-1], closes[-11]
    i, i10 = idx_closes[-1], idx_closes[-11]
    if not (c and c10 and i and i10):
        return None
    ma60 = sum(closes[-60:]) / 60
    ima20 = sum(idx_closes[-20:]) / 20
    r10 = (c / c10 - 1) * 100
    ir10 = (i / i10 - 1) * 100
    cond = {"under": r10 < ir10, "drop10": r10 < DROP10, "below_ma": c < ma60, "px100": c > MIN_PX,
            "sell": (foreign is not None and trust is not None and foreign < 0 and trust < 0)}
    out = {"close": c, "r10": round(r10, 2), "idx_r10": round(ir10, 2), "ma60": round(ma60, 2),
           "idx": i, "idx_ma20": round(ima20, 2), "mkt_weak": i < ima20, "foreign": foreign, "trust": trust,
           **cond, "n_ok": sum(cond.values()), "hit": all(cond.values())}
    if vol is not None:
        out["vol"] = vol
    return out


# ---------- 全市場資料（給清單用）----------
def _call(url, params):
    t = time.time()
    try:
        j = http.get_json(url, params=params, cache_ttl=TTL)
    except Exception as e:
        print(f"  [warn] {url.split('/')[-1]} {params.get('date')}: {str(e)[:100]}")
        j = {}
    if time.time() - t > 0.2:
        time.sleep(2.0)          # 證交所 / 櫃買有頻率限制
    return j or {}


def _fetch_day(d):
    """一天的全市場 {q: {code: [o,h,l,c,v張]}, ins: {code: [外資,投信]}, idx, names}；證交所還沒出資料回 None。"""
    q, ins, names, idx = {}, {}, {}, None
    j = _call("https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX", {"date": d.strftime("%Y%m%d"), "type": "ALLBUT0999", "response": "json"})
    for t in j.get("tables", []):
        title = t.get("title") or ""
        if "每日收盤行情" in title:
            for r in t.get("data", []):
                c = r[0].strip()
                if len(c) == 4 and c.isdigit() and http.to_float(r[8]):
                    q[c] = [http.to_float(r[5]), http.to_float(r[6]), http.to_float(r[7]), http.to_float(r[8]), http.to_int(r[2]) // 1000]
                    names[c] = r[1].strip()
        if "價格指數" in title and idx is None:
            for r in t.get("data", []):
                if (r[0] or "").strip() == "發行量加權股價指數":
                    idx = http.to_float(r[1])
    if not q:
        return None
    j = _call("https://www.twse.com.tw/rwd/zh/fund/T86", {"date": d.strftime("%Y%m%d"), "selectType": "ALL", "response": "json"})
    for r in j.get("data", []):
        c = r[0].strip()
        if len(c) == 4 and c.isdigit():
            ins[c] = [http.to_int(r[4]) // 1000, http.to_int(r[10]) // 1000]
    j = _call("https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes", {"date": d.strftime("%Y/%m/%d"), "response": "json"})
    for t in j.get("tables", []):
        if "上櫃股票行情" not in (t.get("title") or ""):
            continue
        for r in t.get("data", []):
            c = r[0].strip()
            if len(c) == 4 and c.isdigit() and http.to_float(r[2]):
                q[c] = [http.to_float(r[4]), http.to_float(r[5]), http.to_float(r[6]), http.to_float(r[2]), http.to_int(r[8]) // 1000]
                names[c] = r[1].strip()
    j = _call("https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade", {"type": "Daily", "sect": "EW", "date": d.strftime("%Y/%m/%d"), "response": "json"})
    for t in j.get("tables", []):
        for r in t.get("data", []):
            if len(r) >= 24 and len(r[0].strip()) == 4 and r[0].strip().isdigit():
                ins[r[0].strip()] = [http.to_int(r[10]) // 1000, http.to_int(r[13]) // 1000]
    if not ins:
        return None              # 法人資料還沒出（通常 15:00 後）
    return {"q": q, "ins": ins, "idx": idx, "names": names}


def load_store(ref, n=65):
    """確保 ref（含）往前 n 個交易日都有資料；ref 當天還沒出就略過。回 (store, 有資料的日期舊到新)。"""
    try:
        with open(STORE, encoding="utf-8") as f:
            store = json.load(f)
    except (OSError, ValueError):
        store = {}
    days = sorted(cal.last_n_trading_days(ref, n, include_ref=True))
    todo = [d for d in days if d.isoformat() not in store]
    if todo:
        print(f"[screen] 補抓 {len(todo)} 天全市場資料（第一次較久，之後每天只補一天）...")
    for k, d in enumerate(todo, 1):
        day = _fetch_day(d)
        if day is None:
            if d == days[-1]:
                print(f"[screen] {d} 證交所/櫃買資料還沒出，用前一個交易日")
            continue
        store[d.isoformat()] = day
        if k % 10 == 0 or k == len(todo):
            with open(STORE + ".tmp", "w", encoding="utf-8") as f:
                json.dump(store, f)
            os.replace(STORE + ".tmp", STORE)
    keep = {d.isoformat() for d in cal.last_n_trading_days(ref, n + 10, include_ref=True)}
    store = {k: v for k, v in store.items() if k in keep}
    with open(STORE + ".tmp", "w", encoding="utf-8") as f:
        json.dump(store, f)
    os.replace(STORE + ".tmp", STORE)
    return store, sorted(store)


def screen(store, dates):
    d = dates[-1]
    idx_closes = [store[x].get("idx") for x in dates]
    if any(v is None for v in idx_closes[-20:]):
        # MI_INDEX 沒給指數的日子用 FMTQIK 補
        tx = dict(src_twse.taiex_history(dt.date.fromisoformat(d) + dt.timedelta(days=1), months=4))
        idx_closes = [store[x].get("idx") or tx.get(x) for x in dates]
    day = store[d]
    rows = []
    for code, q in day["q"].items():
        if q[4] < MIN_VOL:
            continue
        closes = [store[x]["q"].get(code, [None] * 4)[3] for x in dates]
        if any(v is None for v in closes[-60:]):
            continue
        f, t = (day["ins"].get(code) or [None, None])
        r = conditions(closes, idx_closes, f, t, vol=q[4])
        if r:
            r.update(code=code, name=day.get("names", {}).get(code, ""))
            rows.append(r)
    return d, rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="篩選日 YYYY-MM-DD（預設：最近一個有資料的交易日）")
    ap.add_argument("--near", action="store_true", help="另列五條件中四條成立的")
    a = ap.parse_args()
    ref = dt.date.fromisoformat(a.date) if a.date else cal.today_tw()
    if not cal.is_trading_day(ref):
        ref = cal.prev_trading_day(ref)
    store, dates = load_store(ref)
    if len(dates) < 60:
        raise SystemExit(f"資料不足 60 個交易日（只有 {len(dates)} 天），稍後再試")
    d, rows = screen(store, dates)
    if not rows:
        raise SystemExit("沒有可篩的股票（資料異常？）")
    r0 = rows[0]
    weak = r0["mkt_weak"]
    print(f"\n=== 弱勢股篩選 {d}（隔天開盤後的放空候選）")
    print(f"大盤：加權 {r0['idx']:.0f}，20 日線 {r0['idx_ma20']:.0f} → {'偏弱 ✔ 篩選有效的環境' if weak else '不弱 ✘'}；10 日 {r0['idx_r10']:+.1f}%")
    if not weak:
        print("  ⚠ 回測：大盤不弱時，這份清單隔天放空反而比隨便空還差（弱勢股常反彈）。只當參考，不要因為弱就空。")
    hits = sorted((r for r in rows if r["hit"]), key=lambda r: r["r10"])
    print(f"\n五條件全中 {len(hits)} 檔（劣於大盤、10 日跌 >10%、季線下、>100 元、外資+投信賣超；量 ≥{MIN_VOL} 張）：")
    print(f"{'代碼':6} {'名稱':8} {'收盤':>8} {'10日%':>7} {'季線':>8} {'外資':>7} {'投信':>6} {'量(張)':>8}")
    for r in hits:
        print(f"{r['code']:6} {r['name'][:8]:8} {r['close']:8.1f} {r['r10']:+7.1f} {r['ma60']:8.1f} {r['foreign']:+7d} {r['trust']:+6d} {r['vol']:8d}")
    if a.near:
        names = {"under": "劣於大盤", "drop10": "跌>10%", "below_ma": "季線下", "px100": ">100元", "sell": "法人賣"}
        near = sorted((r for r in rows if r["n_ok"] == 4 and r["px100"]), key=lambda r: r["r10"])
        print(f"\n差一個條件 {len(near)} 檔：")
        for r in near[:40]:
            miss = next(v for k, v in names.items() if not r[k])
            print(f"{r['code']:6} {r['name'][:8]:8} {r['close']:8.1f} {r['r10']:+7.1f}  缺：{miss}")
    print("\n用法：挑幾檔隔天 `python3 monitor.py 代碼 --web`；開盤後照模型訊號進場，不是開盤直接空（回測是開盤空、收盤補的平均，波動大）。")
    print("以上為技術面與籌碼面推算，不構成投資建議。")


if __name__ == "__main__":
    main()
