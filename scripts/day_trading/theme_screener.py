#!/usr/bin/env python3
"""
當沖盤前選股（repo 唯一的選股腳本）：收盤後 / 盤前跑，從全市場挑隔天的放空、做多候選，
FinLab「概念股主題」只拿來標族群與看族群強弱，不參與選股（回測主題評分沒幫助）。

  做空「弱勢股」（只在大盤弱 = 加權 < 20 日線 時有效）：
    劣於大盤（10 日報酬 < 加權 10 日報酬）、10 日跌 >10%、收盤 < 季線、股價 >100、外資與投信都賣超（五條件出自 Threads 貼文），
    加成交值 ≥ 5 億，依 10 日跌幅最深取前 N；開盤跳空跌 >3% 不空（跳空大跌後常反彈）。
  做多「強勢股急殺」（只在大盤不弱時有效）：
    昨日跌 >7%、收盤仍在季線上、ATR ≥ 2.5%、成交值 ≥ 5 億，依 20 日報酬最強取前 N。
  兩邊都排除處置股、開盤鎖漲跌停（買不到 / 空不到）。

回測（--backtest；2026/06/01–11/05，107 天，隔天開盤進、收盤出，扣成本 0.25%）：
  做空：大盤弱 36 天 99 筆 每筆 +1.78%、勝率 70%、t 4.0；大盤不弱時照做 −0.02% → 不做。
  做多：大盤不弱 59 天 139 筆 每筆 +0.59%、勝率 52%、t 1.6（證據較弱）；大盤弱時照做 −1.76% → 不做。
  舊版「昨日大漲 + 收高檔 + 量增」做多每筆 −1.06%（t −3.3），是反指標，已移除。

當沖適合度（2026-11-10 加，出自權證小哥「不要當沖權值股」的說法）：每檔候選標出是不是權值股（市值前 50 名）、近 5 日平均振幅
（及它是來回成本的幾倍）、股本、權證檔數與成交、前五大買賣超佔量、隔日沖分點昨天買多少。只標註，不改排序也不排除：
  同一段回測拿這些條件當濾網，結果都沒有比較好——
    做空：現行每筆 +1.80%；排除市值前 50 → +1.51%；只留股本 10–50 億 → +1.12%；振幅 ≥3% → +1.78%（幾乎沒差，候選本來就都 ≥3%）
    做多：現行 +0.84%；排除市值前 50 → +0.71%；股本 ≥10 億 → +1.18%（t 1.7，不顯著）；只做市值前 50 → +0.35%（31 筆，扣成本剩 +0.10%）
  近 60 日成交值 ≥5 億的股票日：市值前 50 名平均振幅 4.4%（67% 的日子 ≥3%、只有 6% <1.5%），400 名以後 8.1%。
  權值股振幅確實比較小，但這段期間沒有小到吃不掉成本；想照原則硬性排除可加 --skip-heavy。

    python3 theme_screener.py                  # 最近一個有資料的交易日，多空各 3 檔
    python3 theme_screener.py --skip-heavy     # 候選直接排除權值股（市值前 50 名）
    python3 theme_screener.py --top 5 --json
    python3 theme_screener.py --date 2026-11-06
    python3 theme_screener.py --backtest       # 用快取裡的全市場資料回測（--days 170 先補資料）

全市場資料（日 K + 法人）快取在 .cache/screen_daily.json（不進 git），第一次抓約 65 天、之後每天補一天。
prep.py 的盤前因子「弱勢股篩選」也用這裡的 short_conditions()。僅供研究，不構成投資建議。
"""
import os
import re
import sys
import json
import math
import time
import argparse
import datetime as dt
import statistics as st
from concurrent.futures import ThreadPoolExecutor

from config import CACHE_DIR
import http_client as http
import indicators as I
import tw_calendar as cal
import src_twse
import src_tpex

STORE = os.path.join(CACHE_DIR, "screen_daily.json")
KEEP_DAYS = 180          # 快取最多留幾個交易日（回測用）
TTL = 86400 * 365
FINLAB = "https://finlab.finance/stocks/themes"
FINLAB_TTL = 1800

MIN_VOL = 300            # 張，short_conditions 的流動性下限（prep 因子用）
MIN_VALUE = 5.0          # 億，選股名單的成交值下限
SHORT_PX = 100
SHORT_DROP10 = -10.0
SHORT_SKIP_GAP = -3.0    # 開盤跳空跌超過這個 % 不空
LONG_DROP1 = -7.0
LONG_MIN_ATR = 2.5
COST = 0.25              # 當沖來回成本 %

# 當沖適合度（權證小哥的選股條件）：只標註與警告，不改排序、不排除（回測見檔頭說明）
CAP_FILE = os.path.join(CACHE_DIR, "company_cap.json")
HEAVY_RANK = 50          # 市值前幾名算權值股
FIT_AMP = 3.0            # 近 5 日平均振幅 % 的及格線
FIT_CAP = (10.0, 50.0)   # 股本（億）適中的範圍
FIT_TOP5 = 20.0          # 前五大買超或賣超佔成交量 % 以上算籌碼集中


# 權證昨日大量買進（2026-11-12 加）：認購權證成交金額 ≥ 前 5 個交易日平均的 WARR_SURGE 倍，而且金額夠大（倍數的分母太小沒有意義）
WARR_SURGE = 3.0
WARR_MIN_AMT = 5e6       # 元
WARR_LIST = 5            # 名單最多列幾檔
WARR_BIG_X = 2.0         # 「大買日」：認購金額 ≥ 前 20 日中位數的幾倍（連續幾天大買用這個定義）
WARR_BIG = 10.0          # 倍數到這裡另外標出來（回測 23 次，隔天開→收平均 −1.3%，樣本少）
# 回測（2026-11-12；上市標的、成交值 ≥5 億、2026/03–11 共 110 個訊號日 19,487 個股票日，其中符合這個條件的 752 個；隔天開盤進、收盤出）：
#   符合的：隔天跳空 +0.61%、開→收 −0.20%（中位 −0.48%）；不符合的：開→收 −0.31%。同一天兩組相減：開→收 −0.01%（t −0.1）、跳空 −0.11%（t −0.8）。
#   依當天漲幅分組比（爆量 / 對照）：漲 2–5% −0.37 / −0.52；漲 5–9.4% −0.31 / −0.21；漲停 −0.02 / −0.45。隔天振幅也跟對照一樣。
#   加上發行商避險買超 ≥2%：−0.24%；避險賣超：+0.21%。開盤放空扣成本 0.25%：−0.05%。
#   唯二看起來不一樣、但次數太少的：倍數 ≥10（23 次）開→收 −1.26%；隔天開高 ≥3%（78 次）開→收 −0.91%、中位 −0.16%。
#   精確度：同日差的標準誤 0.15%，95% 區間 −0.30% ～ +0.29%——能排除的是「效果比 0.3% 大」，±0.3% 以內的小效果這個樣本分辨不出來。
#   量的是權證「成交金額」（買賣都算），不是淨買進；淨買進只能用發行商避險買賣超間接看。只測了開盤進、收盤出。
# → 看不出「權證前一天被大量買進」讓隔天的方向、跳空、開高走低、振幅有差別（有的話也小於來回成本），所以只列成觀察名單，不當做多或做空的候選。
WARR_NOTE = ("觀察名單，不是做多或做空的建議。回測 110 個交易日、752 個符合的股票日（上市標的）：隔天平均開高 0.6%、開盤到收盤 −0.2%，"
             "跟同一天沒有權證爆量的股票幾乎一樣（差 −0.01%，誤差範圍 ±0.3%），開盤放空扣成本後是 −0.05%。也就是「昨天權證被大買」這件事本身看不出隔天會怎麼走；"
             "列出來是讓你知道這幾檔有權證資金和發行商避險部位在裡面，盤中照 monitor 的訊號做。")
WARR_PLAN = ("不因為這個條件預設方向。隔天開高 3% 以上的那一小部分，回測開盤到收盤平均 −0.9%（78 次、中位數 −0.2%，次數少）：開高不追，"
             "要做等 monitor 的訊號。")


# 強勢股（2026-11-13 加）：認購權證連續幾天大買 + 法人（外資 + 投信）買超。全市場的「大戶」只有法人資料可用，逐筆的大戶分流要盤中才有。
STRONG_WARR_DAYS = 2     # 權證連續幾天都是大買日（warrant_big）
STRONG_INST_PCT = 3.0    # 篩選日 外資 + 投信 買超占成交量 % 至少這麼多
STRONG_LIST = 5
# 回測（2026-11-13；上市標的、成交值 ≥5 億、2026/04–11 共 137 個訊號日 24,016 個股票日；數字是「同一天符合 − 其餘」的差與 95% 區間，以日為單位）：
#   兩個條件都符合（1,148 個）：隔天跳空 +0.43% [+0.28,+0.58]、收→收 +0.42% [+0.04,+0.80]、振幅 +0.97% [+0.72,+1.22]，但開→收 −0.01% [−0.36,+0.34]。
#   只看權證連 ≥2 天（2,443 個）：開→收 −0.19% [−0.41,+0.02]、振幅 +1.08% [+0.91,+1.26]；大盤不弱時開→收 −0.28% [−0.48,−0.08]。
#   只看法人買超 ≥10% 量（7,035 個）：跳空 +0.27%、收→收 +0.2～0.3%、開→收 ±0；「外資投信都買」收→收 +0.36% [+0.20,+0.53]、開→收 +0.03%。
#   兩個條件 × 大盤：不弱（91 天）開→收 −0.32% [−0.65,−0.00]；弱（36 天、145 個）開→收 +0.78% [−0.11,+1.67]、收→收 +1.28% [+0.35,+2.21]。
#   法人買超的效果前半段明顯（收→收 +0.52%）、後半段消失（+0.02%），不穩定。
# → 這兩個條件挑得出「隔天容易開高、波動比較大」的股票，但強的部分在開盤那一下就反映完了，開盤後平均沒有續漲。
STRONG_NOTE = ("條件：認購權證連續 2 天以上大買（每天 ≥ 前 20 日中位數的 2 倍且 ≥ 500 萬）＋ 外資投信合計買超 ≥ 成交量 3%。"
               "回測 137 個交易日、1,148 個符合的股票日（上市標的）：隔天平均開高 0.9%（比同一天其他股票多 0.43%）、振幅多約 1%，"
               "但開盤到收盤跟其他股票沒有差別（−0.01%，誤差 ±0.35%）。強的部分多半在開盤那一下就反映了：大盤不弱時開盤後還略弱（−0.32%），"
               "大盤弱時才有續強的跡象（+0.78%，只有 145 次、還不確定）。")
STRONG_PLAN = ("這是「隔天容易開高、波動大」的名單，不是開盤追買的名單：開高不追，等拉回守住、照 monitor 的訊號再進；"
               "大盤在 20 日線下時續強的機會比較大。")


# ================================================================ 條件（prep.py 也用）

def short_conditions(closes, idx_closes, foreign, trust, vol=None):
    """
    弱勢股五條件。closes：個股日收盤，舊到新，最後一筆 = 篩選日（至少 60 筆）；idx_closes：加權指數，同樣舊到新、最後一筆同一天（至少 20 筆）。
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
    cond = {"under": r10 < ir10, "drop10": r10 < SHORT_DROP10, "below_ma": c < ma60, "px100": c > SHORT_PX,
            "sell": (foreign is not None and trust is not None and foreign < 0 and trust < 0)}
    out = {"close": c, "r10": round(r10, 2), "idx_r10": round(ir10, 2), "ma60": round(ma60, 2),
           "idx": i, "idx_ma20": round(ima20, 2), "mkt_weak": i < ima20, "foreign": foreign, "trust": trust,
           **cond, "n_ok": sum(cond.values()), "hit": all(cond.values())}
    if vol is not None:
        out["vol"] = vol
    return out


def long_conditions(f):
    """強勢股急殺：f 為 features() 的一筆。"""
    return f["chg1"] < LONG_DROP1 and f["c"] > f["ma60"] and f["atrp"] >= LONG_MIN_ATR


# ================================================================ 全市場資料快取

def _call(url, params):
    t = time.time()
    try:
        j = http.get_json(url, params=params, cache_ttl=TTL)
    except Exception as e:
        print(f"  [warn] {url.split('/')[-1]} {params.get('date')}: {str(e)[:100]}", file=sys.stderr)
        j = {}
    if time.time() - t > 0.2:
        time.sleep(2.0)          # 證交所 / 櫃買有頻率限制
    return j or {}


def _fetch_day(d):
    """一天的全市場 {q: {code: [o,h,l,c,v張]}, ins: {code: [外資,投信]}, idx, names}；資料還沒出回 None。"""
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


def _save(store):
    with open(STORE + ".tmp", "w", encoding="utf-8") as f:
        json.dump(store, f)
    os.replace(STORE + ".tmp", STORE)


def load_store(ref, n=65):
    """確保 ref（含）往前 n 個交易日都有資料（ref 當天還沒出就略過）；最多留 KEEP_DAYS 天。回 (store, 有資料的日期舊到新)。"""
    try:
        with open(STORE, encoding="utf-8") as f:
            store = json.load(f)
    except (OSError, ValueError):
        store = {}
    days = sorted(cal.last_n_trading_days(ref, n, include_ref=True))
    todo = [d for d in days if d.isoformat() not in store]
    if todo:
        print(f"[screen] 補抓 {len(todo)} 天全市場資料（第一次較久，之後每天只補一天）...", file=sys.stderr)
    for k, d in enumerate(todo, 1):
        day = _fetch_day(d)
        if day is None:
            if d == days[-1]:
                print(f"[screen] {d} 證交所/櫃買資料還沒出，用前一個交易日", file=sys.stderr)
            continue
        store[d.isoformat()] = day
        if k % 10 == 0:
            _save(store)
    keep = {d.isoformat() for d in cal.last_n_trading_days(ref, max(n, KEEP_DAYS) + 10, include_ref=True)}
    store = {k: v for k, v in store.items() if k in keep}
    _save(store)
    return store, sorted(store)


def index_closes(store, dates):
    idx = [store[x].get("idx") for x in dates]
    if any(v is None for v in idx):
        # MI_INDEX 沒給指數的日子用 FMTQIK 補
        months = max(3, len(dates) // 20 + 2)
        tx = dict(src_twse.taiex_history(dt.date.fromisoformat(dates[-1]) + dt.timedelta(days=1), months=months))
        idx = [v or tx.get(x) for v, x in zip(idx, dates)]
    return idx


# ================================================================ 特徵與選股（即時與回測共用）

def features(store, dates, i, idx):
    """第 i 天收盤後每檔的特徵 → (大盤 dict, {code: feat})。"""
    ic = idx[: i + 1]
    if len(ic) < 20 or any(v is None for v in ic[-20:]):
        return None, {}
    mkt = {"idx": ic[-1], "ma20": sum(ic[-20:]) / 20, "r10": (ic[-1] / ic[-11] - 1) * 100 if len(ic) > 10 and ic[-11] else None}
    mkt["weak"] = mkt["idx"] < mkt["ma20"]
    day = store[dates[i]]
    out = {}
    for code, q in day["q"].items():
        if q[4] < MIN_VOL or not q[3]:
            continue
        win = [store[x]["q"].get(code) for x in dates[max(0, i - 60): i + 1]]
        if len(win) < 61 or any(w is None or not w[3] or w[1] is None or w[2] is None for w in win):
            continue
        cl = [w[3] for w in win]
        c, h, l = q[3], q[1], q[2]
        trs = [max(win[k][1] - win[k][2], abs(win[k][1] - win[k - 1][3]), abs(win[k][2] - win[k - 1][3])) for k in range(-14, 0)]
        f, t = day["ins"].get(code) or [None, None]
        sc = short_conditions(cl, ic, f, t, vol=q[4])
        if not sc:
            continue
        inst_pct = ((f or 0) + (t or 0)) / q[4] * 100 if (f is not None and q[4]) else None
        inst_streak = 0                              # 外資 + 投信合計連續幾天買超（最多往回看 5 天）
        for x in reversed(dates[max(0, i - 4): i + 1]):
            pair = store[x]["ins"].get(code)
            if not pair or pair[0] is None or (pair[0] + (pair[1] or 0)) <= 0:
                break
            inst_streak += 1
        out[code] = {"code": code, "name": day.get("names", {}).get(code, ""), "c": c, "h": h, "l": l, "vol": q[4],
                     "value": q[4] * c / 1e5, "chg1": (c / cl[-2] - 1) * 100, "r20": (c / cl[-21] - 1) * 100,
                     "ma20": sum(cl[-20:]) / 20, "ma60": sc["ma60"], "atr": sum(trs) / 14, "atrp": sum(trs) / 14 / c * 100,
                     "foreign": f, "trust": t, "r10": sc["r10"], "weak5": sc["hit"],
                     "inst_pct": inst_pct, "inst_streak": inst_streak}
    return mkt, out


def pick(feats, exclude=(), min_value=MIN_VALUE):
    """回 (做空排序名單, 做多排序名單)，不含開盤條件。"""
    pool = [f for f in feats.values() if f["value"] >= min_value and f["code"] not in exclude]
    shorts = sorted((f for f in pool if f["weak5"]), key=lambda f: f["r10"])
    longs = sorted((f for f in pool if long_conditions(f)), key=lambda f: -f["r20"])
    return shorts, longs


WARR_STORE = os.path.join(CACHE_DIR, "warrant_daily.json")
WARR_DAYS = 26           # 存檔至少要涵蓋幾個交易日（20 日中位數 + 連續幾天）
WARR_KEEP = 40


def load_warrant_store(ref, n=WARR_DAYS, max_new=60):
    """
    全市場權證每日彙總的本機存檔（.cache/warrant_daily.json，不進 git）：{日期: {標的: [認購金額, 認購張數, 認購有量檔數, 認購檔數, 認售金額, 認售張數]}}。
    確保 ref（含）往前 n 個交易日都試過一次；某一天上市或上櫃抓不到就不存那一天（之後再跑會重試），不存半份。
    證交所的權證日表一張要 30 秒左右才回，所以第一次建 26 天約 20 分鐘，之後每天只補一天。回 (store, 有資料的日期舊到新)。
    """
    import src_warrants as W
    try:
        with open(WARR_STORE, encoding="utf-8") as f:
            store = json.load(f)
    except (OSError, ValueError):
        store = {}
    days = sorted(cal.last_n_trading_days(ref, n, include_ref=True), reverse=True)        # 新的先抓：中途停掉也先有最近幾天
    todo = [d for d in days if d.isoformat() not in store][:max_new]
    if todo:
        print(f"[screen] 補抓 {len(todo)} 天全市場權證日成交（證交所一張表約 30 秒）...", file=sys.stderr)
    for d in todo:
        try:
            sm = W.market_summary(d)
        except Exception as e:
            print(f"  [warn] 權證 {d}: {str(e)[:100]}", file=sys.stderr)
            continue
        store[d.isoformat()] = {c: [x["call_amt"], x["call_vol"], x["n_call_active"], x["n_call"], x["put_amt"], x["put_vol"]] for c, x in sm.items()}
        keep = {x.isoformat() for x in cal.last_n_trading_days(ref, max(n, WARR_KEEP), include_ref=True)}
        store = {k: v for k, v in store.items() if k in keep}
        with open(WARR_STORE + ".tmp", "w", encoding="utf-8") as f:
            json.dump(store, f)
        os.replace(WARR_STORE + ".tmp", WARR_STORE)
    return store, sorted(store)


def warrant_activity(date):
    """
    篩選日全市場每個標的的權證成交 → {code: {call_amt, call_vol, n_call, n_call_active, put_amt, put_vol, avg5, surge, hedge, med20, big, streak}}。
      call_amt / put_amt：當天認購 / 認售權證成交金額（元）；avg5：前 5 個交易日認購金額的平均；surge = call_amt / avg5（avg5 = 0 時 None）
      hedge：自營商(避險)買賣超（張）——發行商為權證部位在現股的對沖
    來源：上市 = 證交所每日收盤行情（認購 / 認售 / 牛熊證，表內有標的代號）；上櫃 = 櫃買中心行情表 + 權證發行基本資料的標的代號。
      med20：前 20 個交易日（有資料的，至少 10 天）認購金額的中位數；big：當天是不是「大買日」（見 warrant_big）；
      streak：到篩選日為止連續幾天都是大買日（中間缺資料就中斷，最多往回看 6 天）
    篩選日和前五天任何一天抓不到就整個丟例外：少一天平均就會錯，寧可不列也不要給錯的倍數。
    """
    import src_warrants as W
    store, have = load_warrant_store(date)
    iso = date.isoformat()
    prev5 = [d.isoformat() for d in cal.last_n_trading_days(date, 5)]
    miss = [d for d in [iso] + prev5 if d not in store]
    if miss:
        raise http.HttpError("權證日成交缺 " + "、".join(miss))
    back = [d.isoformat() for d in cal.last_n_trading_days(date, WARR_DAYS - 1)]          # 新到舊，不含篩選日
    hedge = W.hedge_all(date)
    amt = lambda d, c: (store[d].get(c) or [0.0])[0] if d in store else None

    def is_big(k, c):                   # k = 0 篩選日，1 前一天…；None = 資料不夠判斷
        d = iso if k == 0 else back[k - 1]
        a = amt(d, c)
        base = [x for x in (amt(x, c) for x in back[k:k + 20]) if x is not None]
        if a is None or len(base) < 10:
            return None, None
        med = st.median(base)
        return warrant_big(a, med), med

    out = {}
    for code, r in store[iso].items():
        avg5 = sum((store[d].get(code) or [0.0])[0] for d in prev5) / 5
        big, med = is_big(0, code)
        streak = 0
        for k in range(0, 6):
            b = big if k == 0 else is_big(k, code)[0]
            if not b:
                break
            streak += 1
        out[code] = {"call_amt": r[0], "call_vol": r[1], "n_call_active": r[2], "n_call": r[3], "put_amt": r[4], "put_vol": r[5], "avg5": avg5,
                     "surge": (r[0] / avg5) if avg5 > 0 else None, "hedge": hedge.get(code), "med20": med, "big": big, "streak": streak}
    return out


def warrant_big(amt, med20):
    """權證大買日：認購成交金額 ≥ WARR_MIN_AMT，且 ≥ 前 20 日中位數的 WARR_BIG_X 倍（用中位數當基準，連續大買的那幾天才不會把自己的基準墊高）。"""
    return bool(amt >= WARR_MIN_AMT and med20 and med20 > 0 and amt >= WARR_BIG_X * med20)


def warrant_hit(w):
    """權證昨日大量買進：認購金額 ≥ WARR_MIN_AMT，且是前 5 日平均的 WARR_SURGE 倍以上（前 5 日完全沒成交也算）。"""
    return bool(w and w["call_amt"] >= WARR_MIN_AMT and (w["surge"] is None or w["surge"] >= WARR_SURGE))


def warrant_pick(feats, wa, exclude=(), min_value=MIN_VALUE):
    """權證昨日大量買進的名單：成交值夠大、不是處置股、符合 warrant_hit，依「認購金額占現股成交值」由大到小。"""
    out = []
    for f in feats.values():
        w = wa.get(f["code"])
        if f["value"] >= min_value and f["code"] not in exclude and warrant_hit(w):
            out.append(dict(f, warr=dict(w, ratio=w["call_amt"] / (f["value"] * 1e8) * 100, hedge_pct=(w["hedge"] / f["vol"] * 100) if (w["hedge"] is not None and f["vol"]) else None)))
    return sorted(out, key=lambda f: -f["warr"]["ratio"])


def strong_hit(f, w):
    """強勢股：權證連續 STRONG_WARR_DAYS 天以上大買，而且篩選日外資 + 投信買超 ≥ 成交量 STRONG_INST_PCT%。"""
    return bool(w and (w.get("streak") or 0) >= STRONG_WARR_DAYS and f.get("inst_pct") is not None and f["inst_pct"] >= STRONG_INST_PCT)


def _warr_of(f, w):
    return dict(w, ratio=w["call_amt"] / (f["value"] * 1e8) * 100, hedge_pct=(w["hedge"] / f["vol"] * 100) if (w["hedge"] is not None and f["vol"]) else None)


def strong_pick(feats, wa, exclude=(), min_value=MIN_VALUE):
    """強勢股名單：成交值夠大、不是處置股、符合 strong_hit；權證連續天數多的在前，同天數看法人買超占量。"""
    out = [dict(f, warr=_warr_of(f, wa[f["code"]])) for f in feats.values()
           if f["value"] >= min_value and f["code"] not in exclude and strong_hit(f, wa.get(f["code"]))]
    return sorted(out, key=lambda f: (-f["warr"]["streak"], -f["inst_pct"]))


def open_ok(side, f, o):
    """開盤條件（9:00 才知道）：鎖漲跌停不做；做空遇跳空大跌不空。"""
    up, dn = I.limits(f["c"])
    if o >= up or o <= dn:
        return False
    if side == "short" and (o / f["c"] - 1) * 100 < SHORT_SKIP_GAP:
        return False
    return True


# ================================================================ 回測

def backtest(store, dates, top=3):
    idx = index_closes(store, dates)
    res = {k: {"trades": [], "daily": [], "skip_regime": 0} for k in ("short", "long", "short_off", "long_off")}
    first = None
    for i in range(60, len(dates) - 1):
        mkt, feats = features(store, dates, i, idx)
        if not feats:
            continue
        first = first or dates[i]
        nxt = store[dates[i + 1]]["q"]
        shorts, longs = pick(feats)
        for side, cands in (("short", shorts), ("long", longs)):
            active = mkt["weak"] if side == "short" else not mkt["weak"]
            key = side if active else side + "_off"     # _off = 大盤環境不符時照做的結果（對照）
            rets = []
            for f in cands:
                n = nxt.get(f["code"])
                if not n or not n[0] or not n[3] or not open_ok(side, f, n[0]):
                    continue
                o, h, l, c = n[0], n[1], n[2], n[3]
                ret = (o - c) / o * 100 if side == "short" else (c - o) / o * 100
                mae = (h - o) / o * 100 if side == "short" else (o - l) / o * 100
                rets.append(ret)
                res[key]["trades"].append({"d": dates[i], "code": f["code"], "ret": ret, "mae": mae})
                if len(rets) >= top:
                    break
            if rets:
                res[key]["daily"].append(sum(rets) / len(rets))
    return first, dates[-2], res


def print_backtest(first, last, res, top):
    print(f"\n══ 回測 訊號日 {first}～{last}：隔天開盤進、收盤出，每天各取前 {top} 檔，成本 {COST}%")
    names = {"short": "做空・大盤弱（採用）", "long": "做多・大盤不弱（採用）", "short_off": "做空・大盤不弱（不做，對照）",
             "long_off": "做多・大盤弱（不做，對照）"}
    print(f"{'':26}{'天數':>5}{'筆數':>6}{'每筆':>8}{'扣成本':>8}{'勝率':>6}{'t值':>6}{'前半':>8}{'後半':>8}{'MAE':>6}")
    for k, nm in names.items():
        tr, dl = res[k]["trades"], res[k]["daily"]
        if not tr:
            print(f"{nm:26}  (無)"); continue
        xs = [t["ret"] for t in tr]
        half = sorted({t["d"] for t in tr})
        cut = half[len(half) // 2]
        a = [t["ret"] for t in tr if t["d"] < cut]; b = [t["ret"] for t in tr if t["d"] >= cut]
        sd = st.pstdev(dl) if len(dl) > 2 else 0
        tv = (sum(dl) / len(dl)) / (sd / math.sqrt(len(dl))) if sd else 0
        m = sum(xs) / len(xs)
        print(f"{nm:26}{len(dl):5d}{len(xs):6d}{m:+8.2f}{m - COST:+8.2f}{sum(x > COST for x in xs) / len(xs):6.0%}{tv:6.1f}"
              f"{sum(a) / max(len(a), 1):+8.2f}{sum(b) / max(len(b), 1):+8.2f}{sum(t['mae'] for t in tr) / len(tr):6.2f}")
    print("MAE = 開盤後最大逆向幅度（%），越大越容易先被停損洗掉。")


# ================================================================ FinLab 族群標註

def _devalue(arr):
    """SvelteKit __data.json 用 devalue 扁平化：陣列裡放值，物件/陣列的成員是索引。"""
    memo = {}

    def h(i):
        if not isinstance(i, int) or i < 0:
            return None
        if i in memo:
            return memo[i]
        v = arr[i]
        if isinstance(v, list):
            r = (v[1] if len(v) > 1 else None) if v and isinstance(v[0], str) and v[0] in ("Date", "Set", "Map", "BigInt", "undefined", "Object") \
                else [h(x) for x in v]
        elif isinstance(v, dict):
            r = {k: h(x) for k, x in v.items()}
        else:
            r = v
        memo[i] = r
        return r

    return h(0)


def _fetch_theme(slug):
    j = http.get_json(f"{FINLAB}/{slug}/__data.json", cache_ttl=FINLAB_TTL)
    for node in j.get("nodes") or []:
        if node and node.get("type") == "data":
            o = _devalue(node.get("data") or [])
            if isinstance(o, dict) and "rows" in o and "theme" in o:
                return {"name": (o["theme"] or {}).get("name") or slug, "rows": o.get("rows") or []}
    return None


def load_themes():
    """FinLab 概念股主題 → (code → [主題], [(主題, 昨日均漲跌%, 5 日均漲跌%)])；抓不到回空，不影響選股。"""
    try:
        html = http.get(FINLAB, cache_ttl=FINLAB_TTL).decode("utf-8", "replace")
        slugs = list(dict.fromkeys(re.findall(r'href="/stocks/themes/([a-z0-9-]+)"', html)))
        with ThreadPoolExecutor(max_workers=6) as ex:
            themes = [t for t in ex.map(lambda s: _safe_theme(s), slugs) if t and t["rows"]]
    except Exception as e:
        print(f"  [warn] FinLab 主題: {str(e)[:120]}", file=sys.stderr)
        return {}, []
    member, stats = {}, []
    for t in themes:
        c1 = [r.get("change1d_pct") for r in t["rows"] if r.get("change1d_pct") is not None]
        c5 = [r.get("change5d_pct") for r in t["rows"] if r.get("change5d_pct") is not None]
        if c1:
            stats.append((t["name"], sum(c1) / len(c1), sum(c5) / len(c5) if c5 else 0))
        for r in t["rows"]:
            member.setdefault(str(r.get("symbol")), []).append(t["name"])
    return member, sorted(stats, key=lambda x: -x[1])


def _safe_theme(slug):
    try:
        return _fetch_theme(slug)
    except Exception as e:
        print(f"  [warn] theme {slug}: {str(e)[:80]}", file=sys.stderr)
        return None


# ================================================================ 當沖適合度

def load_caps(max_age_days=7):
    """
    股本與發行股數 → {code: [股本(億), 發行股數, "tse"|"otc"]}。來源：證交所、櫃買的公開資料（上市/上櫃公司基本資料），
    存 .cache/company_cap.json，7 天更新一次；抓不到就用舊的，都沒有回 {}。
    """
    old = {}
    try:
        with open(CAP_FILE, encoding="utf-8") as f:
            j = json.load(f)
        old = j.get("caps") or {}
        if old and (cal.today_tw() - dt.date.fromisoformat(j["date"])).days < max_age_days:
            return old
    except (OSError, ValueError, KeyError):
        pass
    caps = {}
    try:
        for r in http.get_json("https://openapi.twse.com.tw/v1/opendata/t187ap03_L", headers={"Accept": "application/json"}):
            cap, sh = http.to_float(r.get("實收資本額")), http.to_float(r.get("已發行普通股數或TDR原股發行股數"))
            if cap:
                caps[str(r.get("公司代號", "")).strip()] = [round(cap / 1e8, 2), sh or cap / 10, "tse"]
        for r in http.get_json("https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O", headers={"Accept": "application/json"}):
            cap = http.to_float(r.get("Paidin.Capital.NTDollars"))
            sh = next((http.to_float(v) for k, v in r.items() if "issue" in k.lower() and http.to_float(v)), None)
            if cap:
                caps[str(r.get("SecuritiesCompanyCode", "")).strip()] = [round(cap / 1e8, 2), sh or cap / 10, "otc"]
    except Exception as e:
        print(f"  [warn] 股本資料: {str(e)[:100]}", file=sys.stderr)
    if len(caps) < 500:          # 只抓到一邊或都失敗：不覆蓋舊檔
        return old or caps
    try:
        with open(CAP_FILE + ".tmp", "w", encoding="utf-8") as f:
            json.dump({"date": cal.today_tw().isoformat(), "caps": caps}, f, ensure_ascii=False)
        os.replace(CAP_FILE + ".tmp", CAP_FILE)
    except OSError:
        pass
    return caps


def mcap_ranks(q, caps):
    """某一天的市值排名 {code: 名次}（市值 = 發行股數 × 收盤）。"""
    mc = sorted(((caps[c][1] * v[3], c) for c, v in q.items() if c in caps and v[3]), reverse=True)
    return {c: k + 1 for k, (_, c) in enumerate(mc)}


def amp_avg(rows, n=5):
    """近 n 日平均振幅 %：(最高 − 最低) ÷ 前一天收盤。rows = [(h, l, c)]，舊到新，至少 n+1 筆；不夠回 None。"""
    rows = [r for r in rows if r and r[0] and r[1] and r[2]]
    if len(rows) < n + 1:
        return None
    return sum((rows[k][0] - rows[k][1]) / rows[k - 1][2] * 100 for k in range(len(rows) - n, len(rows))) / n


def fit_info(code, hlc_rows, caps, ranks):
    """一檔股票的當沖適合度基本資料：股本、市值排名、是不是權值股、近 5 日平均振幅與它是成本的幾倍。"""
    cap = caps.get(code)
    amp = amp_avg(hlc_rows)
    rank = (ranks or {}).get(code)
    return {"cap_e8": cap[0] if cap else None, "market": cap[2] if cap else None, "mcap_rank": rank,
            "heavy": bool(rank and rank <= HEAVY_RANK), "amp5": round(amp, 2) if amp is not None else None,
            "amp_x_cost": round(amp / COST, 1) if amp is not None else None}


def fit_tags(fit, side=None):
    """
    適合度 → [[狀態, 文字]]，狀態 "ok" / "no" / "warn" / "info"。對照權證小哥說的當沖標的四個特徵
    （股本適中、有權證且成交活躍、籌碼集中、近幾日振幅大）加上「不是權值股」。
    """
    T = []
    if fit.get("heavy"):
        T.append(["warn", f"權值股（市值第 {fit['mcap_rank']} 名）：期現套利、ETF 調整的程式單多，分點籌碼不好判讀"])
    a = fit.get("amp5")
    if a is not None:
        T.append(["ok" if a >= FIT_AMP else "no", f"近 5 日平均振幅 {a:.1f}%（來回成本的 {fit['amp_x_cost']:.0f} 倍）" + ("" if a >= FIT_AMP else "：空間小，停損一次要賺好幾次才補得回")])
    c = fit.get("cap_e8")
    if c is not None:
        T.append(["ok" if FIT_CAP[0] <= c < FIT_CAP[1] else "info", f"股本 {c:,.0f} 億" + ("" if FIT_CAP[0] <= c < FIT_CAP[1] else ("（偏小）" if c < FIT_CAP[0] else "（偏大）"))])
    w = fit.get("warrants")
    if w is not None:
        if not (w.get("n_call") or w.get("n_put")):
            T.append(["no", "沒有權證"])
        else:
            act = (w.get("n_call_active") or 0) + (w.get("n_put_active") or 0)
            T.append(["ok" if act >= 10 else "info", f"權證 {w['n_call'] + w['n_put']} 檔，昨天有成交的 {act} 檔、認購 {w['call_vol']:,} 張" + ("" if act >= 10 else "（冷清）")])
    b = fit.get("top5")
    if b is not None:
        conc = max(b[0], b[1]) >= FIT_TOP5
        T.append(["ok" if conc else "info", f"前五大買超佔 {b[0]:.0f}% 量、賣超佔 {b[1]:.0f}%" + ("（集中）" if conc else "（分散）")])
    d = fit.get("dt")
    if d and d.get("buy"):
        note = {"short": "他們今天多半要賣，對空方有利", "long": "他們今天多半要賣，多一股賣壓"}.get(side, "他們今天多半要賣")
        T.append(["warn" if side == "long" else "info", f"隔日沖分點昨買 {d['buy']:,} 張（{d.get('buy_pct') or 0:.1f}% 量，" + "、".join(x["broker"] for x in d["list"][:3]) + f"）：{note}"])
    return T


def _enrich(f, date, caps, twse_w):
    """候選股補上要連線才有的資料：權證（昨天成交）、前五大買賣超、隔日沖分點。抓不到就略過那一項。"""
    fit = f["fit"]
    try:
        import src_warrants as W
        mk = fit.get("market")
        if mk:
            wd = W.warrants_for(f["code"], f["name"], mk, date, twse_w if mk == "tse" else None)
            fit["warrants"] = {k: v for k, v in W.summarize(wd).items() if k in ("n_call", "n_put", "n_call_active", "n_put_active", "call_vol", "put_vol")}
    except Exception as e:
        print(f"  [warn] 權證 {f['code']}: {str(e)[:80]}", file=sys.stderr)
    try:
        import src_moneydj, daytraders
        summ = src_moneydj.zco_day(f["code"], date) if date < cal.today_tw() else src_moneydj.broker_summary_range(f["code"], date, date)
        if summ and (summ.get("buyers") or summ.get("sellers")) and f["vol"]:
            fit["top5"] = [round(sum(r["net"] for r in summ["buyers"][:5]) / f["vol"] * 100, 1),
                           round(-sum(r["net"] for r in summ["sellers"][:5]) / f["vol"] * 100, 1)]
            d = daytraders.for_stock(summ, f["vol"])
            fit["dt"] = {"buy": d["buy"], "buy_pct": d["buy_pct"], "expect_sell": d["expect_sell"], "list": d["list"][:5], "source": d["source"]}
    except Exception as e:
        print(f"  [warn] 分點 {f['code']}: {str(e)[:80]}", file=sys.stderr)


def fit_for(code, hist):
    """
    給 prep.py 用：單一股票的適合度（hist = 日 K [{h,l,c}]，舊到新）。市值排名用選股快取裡最新一天的全市場收盤，
    沒有快取就不排名（不為了這個去連線）。
    """
    caps = load_caps()
    ranks = None
    try:
        with open(STORE, encoding="utf-8") as f:
            store = json.load(f)
        ranks = mcap_ranks(store[max(store)]["q"], caps) if store else None
    except (OSError, ValueError):
        pass
    fit = fit_info(code, [(r.get("h"), r.get("l"), r.get("c")) for r in hist[-6:]], caps, ranks)
    fit["tags"] = fit_tags(fit)
    return fit


# ================================================================ 即時名單

def screen(ref, top=3, skip_heavy=False):
    """skip_heavy=True：候選直接排除權值股（市值前 HEAVY_RANK 名）。預設不排除，只標註。"""
    store, dates = load_store(ref)
    if len(dates) < 61:
        raise SystemExit(f"資料不足 61 個交易日（只有 {len(dates)} 天），稍後再試")
    idx = index_closes(store, dates)
    mkt, feats = features(store, dates, len(dates) - 1, idx)
    if not feats:
        raise SystemExit("沒有可篩的股票（資料異常？）")
    d = dt.date.fromisoformat(dates[-1])
    punish = {}
    target = cal.next_trading_day(d)            # 名單是給下一個交易日用的：只排除那一天真的在處置期間內的
    try:
        punish.update(src_twse.punish_on(target)[0])
    except Exception as e:
        print(f"  [warn] twse 處置: {str(e)[:80]}", file=sys.stderr)
    try:
        punish.update(src_tpex.disposal_on(target)[0])
    except Exception as e:
        print(f"  [warn] tpex 處置: {str(e)[:80]}", file=sys.stderr)
    caps = load_caps()
    ranks = mcap_ranks(store[dates[-1]]["q"], caps) if caps else {}
    heavy = {c for c, k in ranks.items() if k <= HEAVY_RANK}
    shorts, longs = pick(feats, exclude=set(punish) | (heavy if skip_heavy else set()))
    member, tstats = load_themes()
    # 權證昨日大量買進：全市場算一次（六天的權證日成交 + 避險），抓不到就不列、把原因帶出去
    wa, warr, strong, warr_err = {}, [], [], None
    try:
        wa = warrant_activity(d)
        warr = warrant_pick(feats, wa, exclude=set(punish))[:WARR_LIST]
        strong = strong_pick(feats, wa, exclude=set(punish))[:STRONG_LIST]
    except Exception as e:
        warr_err = str(e)[:160] or e.__class__.__name__
        print(f"  [warn] 權證昨日成交: {warr_err}", file=sys.stderr)
    for f in shorts[: top + 3] + longs[: top + 3]:              # 做空 / 做多候選剛好也是權證大買的，一併標出來
        w = wa.get(f["code"])
        if warrant_hit(w) or (w and (w.get("streak") or 0) >= STRONG_WARR_DAYS):
            f["warr"] = _warr_of(f, w)
    # 當沖適合度：只算會列出來的那幾檔（權證與分點要連線）
    shown = shorts[: top + 3] + longs[: top + 3] + warr + strong
    twse_w = None
    if any((caps.get(f["code"]) or [0, 0, ""])[2] == "tse" for f in shown):
        try:
            import src_warrants as W
            twse_w = W.twse_warrants_daily(d)
        except Exception as e:
            print(f"  [warn] 上市權證: {str(e)[:80]}", file=sys.stderr)
    for side, lst in (("short", shorts[: top + 3]), ("long", longs[: top + 3]), ("warrant", warr), ("strong", strong)):
        for f in lst:
            win = [store[x]["q"].get(f["code"]) for x in dates[-6:]]
            f["fit"] = fit_info(f["code"], [(w[1], w[2], w[3]) if w else None for w in win], caps, ranks)
            _enrich(f, d, caps, twse_w)
            f["fit"]["tags"] = fit_tags(f["fit"], side)
    # 多留幾檔備用：開盤跳空不符時往下遞補
    return {"date": dates[-1], "mkt": mkt, "short": shorts[: top + 3], "long": longs[: top + 3], "top": top,
            "warrant": warr, "strong": strong, "warrant_error": warr_err,
            "member": member, "themes": tstats, "punished": [c for c in punish if c in feats],
            "skipped_heavy": sorted(c for c in heavy if c in feats and (feats[c]["weak5"] or long_conditions(feats[c]))) if skip_heavy else []}


def _warr_text(w):
    """權證昨日成交的一句話：金額、倍數、占現股成交值、避險。"""
    mul = "前 5 日幾乎沒成交" if w["surge"] is None else f"前 5 日平均（{w['avg5'] / 1e4:,.0f} 萬）的 {w['surge']:.1f} 倍"
    if w["surge"] is not None and w["surge"] >= WARR_BIG:
        mul += f"（≥{WARR_BIG:g} 倍：回測 23 次，隔天開盤到收盤平均 −1.3%，次數少、只當提醒）"
    hg = "" if w.get("hedge") is None else f"；發行商避險{'買超' if w['hedge'] >= 0 else '賣超'} {abs(w['hedge']):,} 張" + (f"（{w['hedge_pct']:+.1f}% 量）" if w.get("hedge_pct") is not None else "")
    return (f"認購權證昨天成交 {w['call_amt'] / 1e4:,.0f} 萬（{w['call_vol']:,} 張、{w['n_call_active']}/{w['n_call']} 檔有量），是{mul}，"
            f"約占現股成交值 {w['ratio']:.2f}%{hg}")


def _extra_reasons(side, f):
    """做空 / 做多候選剛好也有權證大買或法人買超時，多列一行（強勢股名單自己的理由已經有了）。"""
    w, out = f.get("warr"), []
    if side in ("short", "long") and w:
        if warrant_hit(w):
            out.append(_warr_text(w) + "（權證昨日大量買進）")
        if (w.get("streak") or 0) >= STRONG_WARR_DAYS:
            out.append(f"認購權證已經連續 {w['streak']} 天大買" + ("，而且法人買超占量 %.1f%%（符合強勢股條件）" % f["inst_pct"] if strong_hit(f, w) else ""))
    if side == "warrant" and w and (w.get("streak") or 0) >= STRONG_WARR_DAYS:
        out.append(f"這已經是連續第 {w['streak']} 天大買（基準：前 20 日中位數的 {WARR_BIG_X:g} 倍）")
    return out


def _plan(side, f):
    """停損用進場價 ± 半個 ATR（進場價開盤後才知道；昨日收在跌停時「昨低 = 收盤」不能當停損）。"""
    half = round(f["atr"] * 0.5, 1 if f["c"] >= 50 else 2)
    if side == "warrant":
        return WARR_PLAN
    if side == "strong":
        return STRONG_PLAN
    if side == "short":
        return (f"開盤跳空跌超過 {-SHORT_SKIP_GAP:.0f}%（低於 {I.round_tick(f['c'] * (1 + SHORT_SKIP_GAP / 100), 'down'):g}）或開盤鎖跌停不空；"
                f"開盤後反彈站不回均價線、跌破開盤價再空，停損設進場價上方約 {half:g} 元（半個 ATR）")
    return f"開盤鎖漲跌停不做；開盤後守住開盤價、站上均價線再買，停損設進場價下方約 {half:g} 元（半個 ATR）"


def _reasons(side, f, mkt):
    ins = f"外資 {f['foreign']:+,} / 投信 {f['trust']:+,} 張" if f["foreign"] is not None else "法人資料缺"
    if side == "strong":
        w = f["warr"]
        return [f"認購權證連續 {w['streak']} 天大買：昨天成交 {w['call_amt'] / 1e4:,.0f} 萬，是前 20 日中位數（{(w['med20'] or 0) / 1e4:,.0f} 萬）的 {w['call_amt'] / w['med20']:.1f} 倍"
                + ("" if w.get("hedge") is None else f"；發行商避險{'買超' if w['hedge'] >= 0 else '賣超'} {abs(w['hedge']):,} 張"),
                f"法人買超占成交量 {f['inst_pct']:.1f}%（{ins}），合計連續 {f['inst_streak']} 天買超",
                f"昨天 {f['chg1']:+.1f}%、20 日 {f['r20']:+.1f}%，收盤 {f['c']:g} 在月線 {f['ma20']:.1f} {'之上' if f['c'] > f['ma20'] else '之下'}；成交值 {f['value']:.0f} 億、ATR {f['atrp']:.1f}%"]
    if side == "warrant":
        w = f["warr"]
        put = f"認售 {w['put_amt'] / 1e4:,.0f} 萬" if w["put_amt"] else "認售沒有成交"
        return [_warr_text(w), f"昨天 {f['chg1']:+.1f}%，{put}；法人：{ins}", f"成交值 {f['value']:.0f} 億、ATR {f['atrp']:.1f}%"]
    if side == "short":
        return [f"10 日 {f['r10']:+.1f}%，劣於大盤（{mkt['r10']:+.1f}%）且跌超過 10%",
                f"收盤 {f['c']:g} 在季線 {f['ma60']:.1f} 之下", f"外資、投信同步賣超（{ins}）",
                f"成交值 {f['value']:.0f} 億、ATR {f['atrp']:.1f}%"]
    return [f"昨日急殺 {f['chg1']:+.1f}%，但收盤 {f['c']:g} 仍在季線 {f['ma60']:.1f} 之上（強勢股錯殺）",
            f"20 日 {f['r20']:+.1f}%" + ("，中期仍強" if f["r20"] > 0 else "，同條件裡相對最強（排序依 20 日報酬）"), f"法人：{ins}", f"成交值 {f['value']:.0f} 億、ATR {f['atrp']:.1f}%"]


def _row(side, f, r):
    return {"code": f["code"], "name": f["name"], "themes": r["member"].get(f["code"], []), "close": f["c"],
            "chg1d": round(f["chg1"], 2), "r10": f["r10"], "r20": round(f["r20"], 2), "value_e8": round(f["value"], 1),
            "atr_pct": round(f["atrp"], 2), "foreign": f["foreign"], "trust": f["trust"],
            "reasons": _reasons(side, f, r["mkt"]) + _extra_reasons(side, f),
            "plan": _plan(side, f), "fit": f.get("fit") or {},
            "inst_pct": round(f["inst_pct"], 1) if f.get("inst_pct") is not None else None, "inst_streak": f.get("inst_streak"),
            "warr": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in f["warr"].items()} if f.get("warr") else None}


def print_report(r):
    m, top = r["mkt"], r["top"]
    print(f"\n══ 當沖盤前選股 {r['date']}（隔天的候選）")
    print(f"大盤：加權 {m['idx']:.0f}，20 日線 {m['ma20']:.0f} → {'偏弱：做空有效、不做多' if m['weak'] else '不弱：做多有效、不做空'}；10 日 {m['r10']:+.1f}%")
    if r["themes"]:
        fmt = lambda t: f"{t[0]} {t[1]:+.1f}%"
        print("最強族群：" + "、".join(fmt(t) for t in r["themes"][:3]) + "｜最弱族群：" + "、".join(fmt(t) for t in r["themes"][-3:][::-1]))
    for side, title, active in (("short", "▼ 做空：弱勢股", m["weak"]), ("long", "▲ 做多：強勢股急殺", not m["weak"])):
        print(f"\n{title}" + ("" if active else "　⚠ 大盤環境不符，回測此時這組會虧，只列參考、不建議做"))
        if not r[side]:
            print("  （沒有符合條件的標的）")
        for k, f in enumerate(r[side], 1):
            j = _row(side, f, r)
            tag = "" if k <= top else "（備用：前面開盤條件不符時遞補）"
            th = f"[{'、'.join(j['themes'][:3])}]" if j["themes"] else ""
            print(f"\n  {k}. {j['code']} {j['name']} {th}{tag}")
            print(f"     收 {j['close']:g}｜昨 {j['chg1d']:+.2f}%｜10日 {j['r10']:+.1f}%｜20日 {j['r20']:+.1f}%｜成交值 {j['value_e8']:.0f} 億｜ATR {j['atr_pct']:.1f}%")
            for why in j["reasons"]:
                print(f"     • {why}")
            mark = {"ok": "✓", "no": "✗", "warn": "⚠", "info": "·"}
            if j["fit"].get("tags"):
                print("     當沖適合度：" + "；".join(f"{mark[k]} {t}" for k, t in j["fit"]["tags"]))
            print(f"     → {j['plan']}")
    print(f"\n🔥 強勢股：權證連續大買 × 法人買超")
    print(f"  {STRONG_NOTE}")
    if r.get("warrant_error"):
        print(f"  ⚠ 權證資料抓不到，這一區今天不列：{r['warrant_error']}")
    elif not r.get("strong"):
        print("  （沒有符合條件的標的）")
    for k, f in enumerate(r.get("strong") or [], 1):
        j = _row("strong", f, r)
        th = f"[{'、'.join(j['themes'][:3])}]" if j["themes"] else ""
        print(f"\n  {k}. {j['code']} {j['name']} {th}")
        print(f"     收 {j['close']:g}｜昨 {j['chg1d']:+.2f}%｜10日 {j['r10']:+.1f}%｜20日 {j['r20']:+.1f}%｜成交值 {j['value_e8']:.0f} 億｜ATR {j['atr_pct']:.1f}%")
        for why in j["reasons"]:
            print(f"     • {why}")
        print(f"     → {j['plan']}")
    print(f"\n⚡ 權證昨日大量買進（認購金額 ≥ 前 5 日平均 {WARR_SURGE:g} 倍且 ≥ {WARR_MIN_AMT / 1e4:.0f} 萬；依占現股成交值排序）")
    print(f"  {WARR_NOTE}")
    if r.get("warrant_error"):
        print(f"  ⚠ 權證資料抓不到，這一區今天不列：{r['warrant_error']}")
    elif not r.get("warrant"):
        print("  （沒有符合條件的標的）")
    for k, f in enumerate(r.get("warrant") or [], 1):
        j = _row("warrant", f, r)
        th = f"[{'、'.join(j['themes'][:3])}]" if j["themes"] else ""
        print(f"\n  {k}. {j['code']} {j['name']} {th}")
        print(f"     收 {j['close']:g}｜昨 {j['chg1d']:+.2f}%｜10日 {j['r10']:+.1f}%｜20日 {j['r20']:+.1f}%｜成交值 {j['value_e8']:.0f} 億｜ATR {j['atr_pct']:.1f}%")
        for why in j["reasons"]:
            print(f"     • {why}")
        if j["fit"].get("tags"):
            print("     當沖適合度：" + "；".join(f"{ {'ok': '✓', 'no': '✗', 'warn': '⚠', 'info': '·'}[k2]} {t}" for k2, t in j["fit"]["tags"]))
        print(f"     → {j['plan']}")
    if r["punished"]:
        print("\n排除（處置股）：" + "、".join(r["punished"]))
    if r.get("skipped_heavy"):
        print("排除（權值股，--skip-heavy）：" + "、".join(r["skipped_heavy"]))
    print("\n※ 回測是開盤進、收盤出的平均；實際進場仍用 monitor.py 看開盤量能與均價線。放空前確認可先賣後買。僅供研究，不構成投資建議。\n")


def default_ref(date=None):
    """篩選基準日：給 YYYY-MM-DD 就用它，否則今天；不是交易日往前找。當天資料還沒出時 load_store 會自動用前一個交易日。"""
    ref = dt.date.fromisoformat(date) if date else cal.today_tw()
    return ref if cal.is_trading_day(ref) else cal.prev_trading_day(ref)


def to_dict(r):
    """screen() 結果 → 可序列化 dict（--json 與 monitor 網頁選股共用）。rank ≤ top 為正選，其餘為備用。"""
    def rows(side):
        return [{**_row(side, f, r), "rank": k} for k, f in enumerate(r[side], 1)]
    return {"date": r["date"], "top": r["top"],
            "market": {k: (round(v, 2) if isinstance(v, float) else v) for k, v in r["mkt"].items()},
            "short_active": r["mkt"]["weak"], "long_active": not r["mkt"]["weak"],
            "short": rows("short"), "long": rows("long"), "punished": r["punished"], "skipped_heavy": r.get("skipped_heavy") or [],
            "warrant": rows("warrant") if r.get("warrant") else [], "warrant_error": r.get("warrant_error"), "warrant_note": WARR_NOTE,
            "strong": rows("strong") if r.get("strong") else [], "strong_note": STRONG_NOTE,
            "strong_rule": f"認購權證連續 ≥{STRONG_WARR_DAYS} 天大買 且 法人買超 ≥ 成交量 {STRONG_INST_PCT:g}%",
            "warrant_rule": f"認購成交金額 ≥ 前 5 日平均 {WARR_SURGE:g} 倍且 ≥ {WARR_MIN_AMT / 1e4:.0f} 萬",
            "themes": [{"name": n, "chg1d": round(a, 2), "chg5d": round(b, 2)} for n, a, b in r["themes"]]}


# ================================================================ 預先算好的結果（GitHub Actions 每天收盤後跑，monitor 啟動時直接讀）

SCHEMA = 2                                                    # 輸出欄位有不相容的變動就加 1，舊的預算結果會被當成過期
PRE_REL = "scripts/day_trading/precomputed/screen.json"       # 在 repo 裡的位置
PRE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "precomputed", "screen.json")


def expected_date(now=None):
    """現在打開選股，應該看到哪一個交易日的資料：交易日 16:00 之後是今天（法人、權證資料大致出齊），其餘是前一個交易日。"""
    now = now or cal.now_tw()                    # 一律用台北時間（GitHub Actions 的機器是 UTC）
    today = now.date()
    if cal.is_trading_day(today) and now.strftime("%H:%M") >= "16:00":
        return today
    return cal.prev_trading_day(today)


def _repo_slug():
    """owner/repo：先看環境變數 DAYTRADE_REPO，否則從 git remote origin 讀；讀不到回 None（就只用本機檔）。"""
    slug = os.environ.get("DAYTRADE_REPO")
    if slug:
        return slug
    try:
        import subprocess
        url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=os.path.dirname(os.path.abspath(__file__)),
                             capture_output=True, text=True, timeout=5).stdout.strip()
        m = re.search(r"github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?$", url)
        return m.group(1) if m else None
    except Exception:
        return None


def write_precomputed(res, path=PRE_FILE):
    """把 to_dict() 的結果寫成預算檔。內容（不含產生時間）跟現有的一樣就不重寫，排程重跑才不會每次都多一個 commit。回 True = 有寫。"""
    body = dict(res, schema=SCHEMA)
    try:
        with open(path, encoding="utf-8") as f:
            old = json.load(f)
        if {k: v for k, v in old.items() if k not in ("generated_at", "generated_by")} == json.loads(json.dumps(body, ensure_ascii=False)):
            return False
    except (OSError, ValueError):
        pass
    body["generated_at"] = cal.now_tw().strftime("%Y-%m-%d %H:%M")
    body["generated_by"] = "GitHub Actions" if os.environ.get("GITHUB_ACTIONS") else "本機"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(body, f, ensure_ascii=False, indent=1)
    os.replace(path + ".tmp", path)
    return True


def load_precomputed(now=None):
    """
    找預先算好的選股結果：GitHub 上的（raw.githubusercontent.com，不用先 git pull）和本機 checkout 裡的都看，
    只收「版本對、資料日 = expected_date()」的；兩個都合格用比較晚產生的。沒有合格的回 None（呼叫端就自己算）。
    設環境變數 DAYTRADE_SCREEN_LOCAL=1 可以略過、一律本機重算。
    """
    if os.environ.get("DAYTRADE_SCREEN_LOCAL") == "1":
        return None
    want = expected_date(now).isoformat()
    found = []
    slug = _repo_slug()
    if slug:
        try:
            raw = http.get(f"https://raw.githubusercontent.com/{slug}/{os.environ.get('DAYTRADE_BRANCH', 'main')}/{PRE_REL}", retries=0, timeout=8)
            found.append(json.loads(raw.decode("utf-8")))
        except Exception as e:
            print(f"[選股] 讀不到 GitHub 上的預算結果（{str(e)[:80]}），看本機的", file=sys.stderr)
    try:
        with open(PRE_FILE, encoding="utf-8") as f:
            found.append(json.load(f))
    except (OSError, ValueError):
        pass
    ok = [j for j in found if isinstance(j, dict) and j.get("schema") == SCHEMA and j.get("date") == want and "short" in j and "long" in j]
    return max(ok, key=lambda j: j.get("generated_at") or "") if ok else None


def main():
    ap = argparse.ArgumentParser(description="當沖盤前選股（弱勢股做空 / 強勢股急殺做多）")
    ap.add_argument("--top", type=int, default=3, help="多、空各取幾檔（預設 3）")
    ap.add_argument("--date", default=None, help="篩選日 YYYY-MM-DD（預設：最近一個有資料的交易日）")
    ap.add_argument("--json", action="store_true", help="輸出 JSON")
    ap.add_argument("--out", default=None, help="把結果寫成預算檔（給 GitHub Actions 用；monitor 啟動時會讀）。不給路徑值時用 --out default")
    ap.add_argument("--backtest", action="store_true", help="用快取的全市場資料回測")
    ap.add_argument("--days", type=int, default=None, help="回測前先確保快取有這麼多交易日（最多 %d）" % KEEP_DAYS)
    ap.add_argument("--skip-heavy", action="store_true", help=f"候選直接排除權值股（市值前 {HEAVY_RANK} 名）；預設只標註不排除")
    a = ap.parse_args()
    ref = default_ref(a.date)
    if a.backtest:
        store, dates = load_store(ref, n=min(a.days or 65, KEEP_DAYS))
        first, last, res = backtest(store, dates, a.top)
        if not first:
            raise SystemExit("資料不足，先用 --days 170 補資料")
        print_backtest(first, last, res, a.top)
        return
    r = screen(ref, a.top, a.skip_heavy)
    if a.out:
        path = PRE_FILE if a.out == "default" else a.out
        wrote = write_precomputed(to_dict(r), path)
        print(f"[選股] 資料日 {r['date']}：{'已寫入' if wrote else '內容沒變，沒有重寫'} {path}"
              + (f"；⚠ 權證資料缺：{r['warrant_error']}" if r.get("warrant_error") else ""), file=sys.stderr)
        return
    if a.json:
        print(json.dumps(to_dict(r), ensure_ascii=False, indent=1))
    else:
        print_report(r)


if __name__ == "__main__":
    main()
