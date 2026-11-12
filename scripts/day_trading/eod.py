#!/usr/bin/env python3
"""
收盤後復盤（儀表板「復盤」畫面與指令列共用）：把當天的紀錄整理成六塊
  1. 成績單：你自己的單（持倉面板記的）、模型發出的訊號、被過濾擋掉的訊號如果照做
  2. 訊號回顧：當天走勢（每分鐘一點，取自 monitor 紀錄）＋每個訊號的時間、進場價、結果
  3. 被擋掉的訊號：依過濾種類看「照做會怎樣」，當天與累計
  4. 模型方向：多空強度明顯偏一邊時，30 分鐘後價格有沒有往那邊走
  5. 警示回顧：每種警示發出後 30 分鐘的走勢，有預期方向的算順向率
  6. 明天的準備：今天的隔日沖分點（明天的庫存）、明天的選股名單（傍晚資料出來才有）

    python3 eod.py                # 今天
    python3 eod.py 2026-11-10     # 指定日期
    python3 eod.py --tomorrow     # 連「明天的準備」一起（要連線）

資料來源：logs/monitor_YYYYMMDD*.jsonl、signal_outcomes*.jsonl、alert_outcomes*.jsonl、trades.jsonl（各台電腦合併，同一檔只算一台）。
注意：一天的訊號只有十幾筆，單日數字不能下結論，所以每塊都同時給累計與筆數。
"""
import os
import re
import sys
import json
import glob
import datetime as dt
from collections import defaultdict

from config import LOG_DIR, dev_files, read_jsonl_devices, one_device
import tw_calendar as cal

COST = 0.235          # 當沖來回成本 %
HORIZON = 30          # 方向檢驗看幾分鐘後
EDGE = 8              # 多空強度差多少以上才算「模型有方向」
PHASES = (("早盤", "09:00", "10:30"), ("中盤", "10:30", "12:00"), ("尾盤", "12:00", "13:30"))

# 警示的預期方向：+1 = 看漲、−1 = 看跌、0 = 只是提醒（看之後動多大）
ALERT_DIR = {
    "認購權證急漲": 1, "主力急回補": 1, "主力加速吃貨": 1, "建議做多": 1, "反彈動能": 1, "賣盤竭盡": 1, "權證急漲+委買厚": 1,
    "認購權證急跌": -1, "認售權證急漲": -1, "主力急出": -1, "主力加速出貨": -1, "主力拉高出貨": -1, "建議做空": -1, "殺盤動能": -1, "買盤竭盡": -1,
}
for _t, _ in __import__("exhaust").alert_types():      # 竭盡跡象（觀察用的 👁 警示）：買盤用完 → 預期跌；賣盤用完 → 預期漲
    ALERT_DIR[_t] = -1 if _t.startswith("買盤") else 1
ALERT_DIR.update({"大盤急漲": 1, "大盤急跌": -1})          # 大盤 5 分鐘 ≥0.4% 或 10 分鐘 ≥0.6%：看個股之後有沒有跟著走
ALERT_DIR.update(__import__("combo").directions())     # 因子組合提醒（👁 組合·…）：三方同向、逆勢強弱、族群先動還沒跟
# 「其他」裡面其實有好幾種，照文字再分；訊號結案的訊息（贏 / 輸 / 未成交…）不是警示，不列
_OTHER = (
    (r"賣單牆.*撤單", "賣單牆撤單（假壓力）", 1), (r"買單牆.*撤單", "買單牆撤單（假支撐）", -1),
    (r"賣單牆.*被吃穿", "賣單牆被吃穿", 1), (r"買單牆.*被吃穿", "買單牆被吃穿", -1),
    (r"賣單牆.*被吃光但價沒過", "賣單牆吃光但沒過", -1), (r"買單牆.*被吃光但價守住", "買單牆吃光但守住", 1),
    (r"買單牆.*仍掛", "買單牆補單", 1), (r"賣單牆.*仍掛", "賣單牆補單", -1),
    (r"認購權證今日中位跌幅", "權證多方被洗出", 0), (r"認購權證今日中位漲幅", "權證普遍上漲", 0),
)
_NOT_ALERT = re.compile(r"逾時|記輸|記贏|未成交|— 平|模型假設|過濾驗證|撤單驗證|離開五檔|訊號取消")

_cache = {}


def _iso(d):
    if not d:
        return cal.today_tw().isoformat()
    d = str(d)
    return d if "-" in d else f"{d[:4]}-{d[4:6]}-{d[6:8]}"


def dates():
    """有 monitor 紀錄的日期（新到舊）。"""
    out = set()
    for p in glob.glob(os.path.join(LOG_DIR, "monitor_*.jsonl")):
        m = re.match(r"monitor_(\d{8})", os.path.basename(p))
        if m and cal.is_trading_date_str(_iso(m.group(1))):
            out.add(_iso(m.group(1)))
    return sorted(out, reverse=True)


def _mtimes(stem, ext=".jsonl"):
    return tuple(sorted((p, os.path.getmtime(p)) for _, p in dev_files(LOG_DIR, stem, ext)))


def _monitor_rows(iso):
    stem = "monitor_" + iso.replace("-", "")
    key = ("mon", iso, _mtimes(stem))
    if _cache.get("mon_key_" + iso) != key:
        rows = one_device(read_jsonl_devices(LOG_DIR, stem), key=lambda r: r.get("code")) if dev_files(LOG_DIR, stem, ".jsonl") else []
        slim = []
        for r in rows:
            if not r.get("price") or not r.get("time"):
                continue
            inf = r.get("info") or {}
            q = r.get("quote") or {}
            slim.append({"code": r["code"], "name": r.get("name"), "time": r["time"], "price": r["price"],
                         "p_long": r.get("p_long"), "p_short": r.get("p_short"), "vwap": inf.get("vwap"), "chg_pct": inf.get("chg_pct"),
                         "src": r.get("src"), "h": q.get("h"), "l": q.get("l"), "vol_pace": inf.get("vol_pace")})
        _cache["mon_key_" + iso], _cache["mon_" + iso] = key, slim
    return _cache["mon_" + iso]


def _signals_all():
    key = _mtimes("signal_outcomes")
    if _cache.get("sig_key") != key:
        rows = [r for r in read_jsonl_devices(LOG_DIR, "signal_outcomes") if cal.is_trading_date_str(r.get("date"))]
        _cache["sig_key"], _cache["sig"] = key, one_device(rows, key=lambda r: (r.get("date"), r.get("code")))
    return _cache["sig"]


def _alerts_all():
    key = _mtimes("alert_outcomes")
    if _cache.get("al_key") != key:
        rows = [r for r in read_jsonl_devices(LOG_DIR, "alert_outcomes") if cal.is_trading_date_str(r.get("date"))]
        _cache["al_key"], _cache["al"] = key, one_device(rows, key=lambda r: (r.get("date"), r.get("code")))
    return _cache["al"]


def _mins(t):
    h, m = int(t[:2]), int(t[3:5])
    return (h - 9) * 60 + m


# ------------------------------------------------------------------ 2. 走勢與訊號

def series(rows):
    """每檔每分鐘一點（取那一分鐘最後一次評估）→ {code: {"name", "y", "pts": [[HH:MM, 價, 多強度, 空強度, VWAP]]}}"""
    out = {}
    for r in rows:
        o = out.setdefault(r["code"], {"name": r.get("name"), "y": None, "pts": {}, "src": r.get("src")})
        if o["y"] is None and r.get("chg_pct") is not None:
            o["y"] = round(r["price"] / (1 + r["chg_pct"] / 100), 2)
        o["pts"][r["time"][:5]] = [r["time"][:5], r["price"], r.get("p_long"), r.get("p_short"), r.get("vwap")]
    for o in out.values():
        o["pts"] = [o["pts"][k] for k in sorted(o["pts"])]
    return out


_SIG_KEYS = ("code", "name", "time", "side", "label", "alert_px", "zone", "price", "stop", "t1", "score_stop", "score_tgt", "verdict", "exit",
             "exit_px", "net_pct", "mae_ticks", "mfe_ticks", "fill_min", "first_stop_min", "first_t1_min", "ret_30m", "why", "blocked",
             "block_kind", "block_reason", "filled_at_alert", "fast_adverse", "scoring", "src", "run5", "run5_tk")

CHASE_PCT, CHASE_TICKS = 0.5, 2        # 跟 monitor.CHASE_* 一致：確認前 5 分鐘已順向走這麼多算「追價」
# 追價的訊號再依「當時離漲跌停多遠」分三段（順向漲跌幅：做多看漲幅、做空看跌幅；漲跌停是 10%）。
# 研究裡的磁吸效應講的是接近漲跌停時會加速往那邊走，只有這一段的追價有文獻支持；門檻是自己訂的，先拿來分格追蹤。
NEAR_LIMIT_PCT, STRONG_PCT = 7.0, 4.0


def _score_levels(side, px):
    """評分用的停損 / 停利價，直接用 monitor 那一套（在 monitor 行程裡就拿它自己的類別，不重複載入）。"""
    import sys
    M = getattr(sys.modules.get("__main__"), "Monitor", None) or __import__("monitor").Monitor
    return M.score_levels(side, px)


def _bars(iso):
    """code -> {第幾分鐘（_mins 的單位）: [開, 高, 低, 收]}。monitor 一分鐘記一筆，高低用這一分鐘與上一分鐘的價格、
    加上今高 / 今低有沒有被刷新來湊；跟實際逐筆判定的輸贏比，30 筆有 25 筆一致，只拿來算對照組。"""
    rows = _monitor_rows(iso)
    if _cache.get("bars_key_" + iso) == id(rows):
        return _cache["bars_" + iso]
    by = defaultdict(list)
    for r in rows:
        by[r["code"]].append(r)
    out = {}
    for c, rs in by.items():
        rs.sort(key=lambda r: r["time"])
        b = {}
        pc = ph = pl = None
        for r in rs:
            mm = _mins(r["time"]); p = r["price"]
            hi = max(p, pc if pc is not None else p); lo = min(p, pc if pc is not None else p)
            if ph is not None and r.get("h") and r["h"] > ph:
                hi = max(hi, r["h"])
            if pl is not None and r.get("l") and r["l"] < pl:
                lo = min(lo, r["l"])
            if mm in b:
                b[mm][1] = max(b[mm][1], hi); b[mm][2] = min(b[mm][2], lo); b[mm][3] = p
            else:
                b[mm] = [pc if pc is not None else p, hi, lo, p]
            pc = p; ph = r.get("h") or ph; pl = r.get("l") or pl
        out[c] = b
    _cache["bars_key_" + iso], _cache["bars_" + iso] = id(rows), out
    return out


def baseline(iso):
    """
    對照組：同一檔、同一天、同方向，09:10–12:25 每一分鐘都照現行輸贏規則進場一次的勝率與平均淨損益。
    回 {(code, side): {"n", "win", "net"}}。那天那檔一路漲的話，隨便做多也會贏——訊號要比這個好才算有用。
    """
    bars = _bars(iso)
    if _cache.get("base_key_" + iso) == id(bars):
        return _cache["base_" + iso]
    out = {}
    for c, b in bars.items():
        t_a, t_b, t_end = _mins("09:10:00"), _mins("12:25:00"), _mins("13:25:00")
        ks = [m for m in sorted(b) if t_a <= m <= t_b]
        if len(ks) < 60:
            continue
        for side in ("long", "short"):
            sg = -1 if side == "short" else 1
            wins, nets = 0, []
            for m0 in ks:
                px = b[m0][3]
                sl = _score_levels(side, px)
                res = last = None
                for i in range(1, min(61, t_end - m0 + 1)):
                    x = b.get(m0 + i)
                    if not x:
                        continue
                    if (x[1] >= sl["stop"]) if side == "short" else (x[2] <= sl["stop"]):
                        res = sg * (sl["stop"] / px - 1) * 100 - COST; break
                    if (x[2] <= sl["tgt"]) if side == "short" else (x[1] >= sl["tgt"]):
                        res = sg * (sl["tgt"] / px - 1) * 100 - COST; break
                    last = x[3]
                if res is None and last is not None:
                    res = sg * (last / px - 1) * 100 - COST
                if res is not None:
                    nets.append(res); wins += res > 0
            if nets:
                out[(c, side)] = {"n": len(nets), "win": round(wins / len(nets) * 100, 1), "net": round(sum(nets) / len(nets), 3)}
    _cache["base_key_" + iso], _cache["base_" + iso] = id(bars), out
    return out


def _run5_from_rows(iso, r):
    """舊紀錄沒有 run5：用 monitor 每分鐘的價格補算（訊號當下的價位比 5 分鐘前順向走了多少 %、幾檔）。"""
    import indicators as I
    px = r.get("alert_px") or r.get("price")
    if not px or not r.get("time"):
        return None, None
    t0 = _mins(r["time"])
    prev = [x for x in _monitor_rows(iso) if x["code"] == r["code"] and t0 - 7 <= _mins(x["time"]) <= t0 - 5]
    if not prev:
        return None, None
    ref = max(prev, key=lambda x: x["time"])["price"]
    sg = -1 if r.get("side") == "short" else 1
    return round(sg * (px / ref - 1) * 100, 2), round(sg * (px - ref) / I.tick_size(px), 1)


def _day_run(iso, r):
    """訊號當下相對昨收已經順著訊號方向走了多少 %（做多 = 漲幅、做空 = 跌幅），以及當時的量比。用 monitor 每分鐘的紀錄回推昨收。"""
    px = r.get("alert_px") or r.get("price")
    if not px or not r.get("time"):
        return None, None
    t0 = _mins(r["time"])
    prev = [x for x in _monitor_rows(iso) if x["code"] == r["code"] and x.get("chg_pct") is not None and t0 - 3 <= _mins(x["time"]) <= t0 and x["time"] <= r["time"]]
    if not prev:
        return None, None
    x = max(prev, key=lambda x: x["time"])
    y = x["price"] / (1 + x["chg_pct"] / 100)
    sg = -1 if r.get("side") == "short" else 1
    return round(sg * (px / y - 1) * 100, 2), (round(x["vol_pace"], 2) if x.get("vol_pace") is not None else None)


def _limit_band(day_run):
    return None if day_run is None else "near" if day_run >= NEAR_LIMIT_PCT else "strong" if day_run >= STRONG_PCT else "far"


def signals(iso):
    out = []
    for r in _signals_all():
        if r.get("date") != iso:
            continue
        s = {k: r.get(k) for k in _SIG_KEYS}
        s["date"] = iso
        s["blocked"] = bool(r.get("blocked"))
        if s.get("run5") is None:
            s["run5"], s["run5_tk"] = _run5_from_rows(iso, r)
        s["chase"] = None if s["run5"] is None else bool(s["run5"] >= CHASE_PCT and (s.get("run5_tk") or 0) >= CHASE_TICKS)
        s["mode"] = "等回掛單" if r.get("filled_at_alert") is False else "現價進場"
        s["day_run"], s["vol_pace"] = _day_run(iso, r)
        s["limit_band"] = _limit_band(s["day_run"])
        bl = baseline(iso).get((r.get("code"), r.get("side")))
        s["base_win"], s["base_net"] = (bl["win"], bl["net"]) if bl else (None, None)
        fm = r.get("fill_min")
        ex = r.get("first_stop_min") if r.get("verdict") == "loss" else r.get("first_t1_min") if r.get("verdict") == "win" else None
        s["fill_at"] = _mins(r["time"]) + fm if fm is not None else None            # 進場在開盤後第幾分鐘
        s["exit_at"] = s["fill_at"] + ex if (s["fill_at"] is not None and ex is not None) else None
        out.append(s)
    return sorted(out, key=lambda s: (s["time"], s["code"]))


def _tally(sigs):
    """
    一組訊號的統計：成交的才算輸贏與損益（net_pct 已扣成本）。
    同一天、同一檔、同方向、同一個進場價、同一個出場價的訊號是同一筆交易被提示了好幾次（被「重複過濾」擋掉的多半是這種），
    只算一次，不然贏一次會被算成贏好幾次；重複的個數記在 dup。
    """
    filled, seen, dup = [], set(), 0
    for s in sigs:
        if s.get("verdict") not in ("win", "loss", "flat"):
            continue
        k = (s.get("date"), s.get("code"), s.get("side"), s.get("price"), s.get("exit_px"), s.get("verdict"))
        if k in seen:
            dup += 1
            continue
        seen.add(k)
        filled.append(s)
    nets = [s["net_pct"] for s in filled if s.get("net_pct") is not None]
    # 對照：這些成交的訊號所在的「同一檔同一天同方向任意進場」平均起來是多少（baseline）
    bw = [s["base_win"] for s in filled if s.get("base_win") is not None]
    bn = [s["base_net"] for s in filled if s.get("base_net") is not None]
    net_avg = round(sum(nets) / len(nets), 2) if nets else None
    base_net = round(sum(bn) / len(bn), 2) if bn else None
    return {"n": len(sigs), "filled": len(filled), "dup": dup, "win": sum(1 for s in filled if s["verdict"] == "win"),
            "loss": sum(1 for s in filled if s["verdict"] == "loss"), "nofill": sum(1 for s in sigs if s.get("verdict") == "nofill"),
            "net_sum": round(sum(nets), 2) if nets else None, "net_avg": net_avg, "net_n": len(nets),
            "base_win": round(sum(bw) / len(bw)) if bw else None, "base_net": base_net,
            "edge": round(net_avg - base_net, 2) if (net_avg is not None and base_net is not None) else None}


# ------------------------------------------------------------------ 1. 成績單

def trades(iso):
    """持倉面板記的單（logs/trades.jsonl，只在這台電腦，不進 git）→ 當天已出場的每一筆。"""
    try:
        import signal_review
        rows = [t for t in signal_review.load_trades() if t.get("date") == iso]
    except Exception:
        rows = []
    out = [{k: t.get(k) for k in ("code", "name", "time", "side", "label", "entry_px", "price", "stop", "t1", "ret_pct", "net_pct", "ticks", "model_px", "sig_since")}
           for t in rows]
    # 成交時間：出場紀錄裡沒有，從同一筆（日期、代碼、訊號時間）的「成交」事件找，畫圖用
    fills = {}
    try:
        with open(os.path.join(LOG_DIR, "trades.jsonl"), encoding="utf-8") as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("date") == iso and e.get("action") == "fill":
                    fills[(e.get("code"), e.get("sig_since"))] = e.get("time")
    except OSError:
        pass
    for t in out:
        t["fill_time"] = fills.get((t["code"], t.get("sig_since")))
        # 有沒有照計畫出場：出場價剛好在停損或目標（差 1 檔內算是）
        px, tick = t.get("price"), None
        if px and t.get("ticks") and t.get("entry_px"):
            tick = abs(px - t["entry_px"]) / abs(t["ticks"]) if t["ticks"] else None
        near = lambda a: bool(a and px and tick and abs(px - a) <= tick * 1.01)
        t["how"] = "停損出場" if near(t.get("stop")) else "到目標出場" if near(t.get("t1")) else "自己決定出場"
    nets = [t["net_pct"] for t in out if t.get("net_pct") is not None]
    return {"list": out, "n": len(out), "win": sum(1 for x in nets if x > 0), "loss": sum(1 for x in nets if x <= 0),
            "net_sum": round(sum(nets), 2) if nets else None, "net_avg": round(sum(nets) / len(nets), 2) if nets else None}


# ------------------------------------------------------------------ 4. 模型方向

def direction(rows, horizon=HORIZON, edge=EDGE):
    """
    模型明顯偏一邊（多空強度差 ≥ edge）時，horizon 分鐘後價格有沒有往那邊走。
    每檔取了一個樣本後要隔 horizon 分鐘才取下一個，樣本之間不重疊（原本每分鐘算一次，同一段走勢被重複算幾十次，
    一天九百多「次」其實只有二十幾個獨立樣本，數字看起來比實際可靠得多）。
    回 {n, hit, miss, flat, hit_rate（對 ÷（對＋錯），價格沒動的不算）, base_up（一律猜漲的對照）, avg（順向報酬 %）, by_phase, by_side, by_code, strong}。
    """
    by_code = defaultdict(dict)
    for r in rows:
        if r.get("p_long") is None or r.get("p_short") is None:
            continue
        by_code[r["code"]][r["time"][:5]] = r
    recs = []
    for code, pts in by_code.items():
        ts = sorted(pts)
        mm = {t: _mins(t) for t in ts}
        free = -999                                   # 取了一個樣本之後，要過 horizon 分鐘才取下一個（樣本之間不重疊）
        for t in ts:
            r = pts[t]
            if not ("09:00" <= t < "13:25") or mm[t] < free or abs(r["p_short"] - r["p_long"]) < edge:
                continue
            later = next((pts[u] for u in ts if mm[u] >= mm[t] + horizon), None)
            if not later:
                continue
            free = mm[t] + horizon
            sg = -1 if r["p_short"] > r["p_long"] else 1
            raw = (later["price"] / r["price"] - 1) * 100
            recs.append({"code": code, "t": t, "side": "空" if sg < 0 else "多", "strength": max(r["p_short"], r["p_long"]), "ret": sg * raw, "raw": raw})

    def agg(L):
        if not L:
            return {"n": 0}
        hit, miss = sum(1 for x in L if x["ret"] > 0), sum(1 for x in L if x["ret"] < 0)
        # 價格完全沒動（鎖漲跌停、沒成交）的不算對也不算錯，另外列
        up = sum(1 for x in L if x["raw"] > 0)
        return {"n": len(L), "hit": hit, "miss": miss, "flat": len(L) - hit - miss, "up": up,
                "hit_rate": round(hit / (hit + miss) * 100, 1) if hit + miss else None, "avg": round(sum(x["ret"] for x in L) / len(L), 3),
                # 基準：同樣這些時點如果一律猜漲會對幾 %（那幾天剛好一路漲的話，偏多的模型看起來就會很準）
                "base_up": round(up / (hit + miss) * 100, 1) if hit + miss else None}
    out = agg(recs)
    out["by_phase"] = {nm: agg([x for x in recs if a <= x["t"] < b]) for nm, a, b in PHASES}
    out["by_side"] = {s: agg([x for x in recs if x["side"] == s]) for s in ("多", "空")}
    out["by_code"] = {c: agg([x for x in recs if x["code"] == c]) for c in by_code}
    out["strong"] = agg([x for x in recs if x["strength"] >= 65])
    return out


# ------------------------------------------------------------------ 5. 警示

def alert_kind(r):
    """警示 → (種類, 預期方向)；不是警示（訊號結案訊息等）回 (None, 0)。"""
    t, text = r.get("type") or "其他", r.get("text") or ""
    if t in ("建議做空", "建議做多") or "過濾" in t or t == "掛單成交":
        return None, 0                   # 訊號本身在第 2、3 塊看
    if t != "其他":
        return t, ALERT_DIR.get(t, 0)
    if _NOT_ALERT.search(text):
        return None, 0
    for pat, name, d in _OTHER:
        if re.search(pat, text):
            return name, d
    return "其他", 0


def alerts(rows):
    """依種類統計 30 分鐘後的走勢 → [{kind, dir, n, flat（價格沒動的則數）, with_dir（順向率 %，沒動的不算）, base（基準）, edge（順向率 − 基準）,
    avg（順向報酬 %）, up_rate, avg_abs, base_up}]，n 多到少。"""
    g = defaultdict(list)
    every = []
    for r in rows:
        kind, d = alert_kind(r)
        o = (r.get("out") or {}).get("30m") or {}
        if kind is None or o.get("ret_pct") is None:
            continue
        g[(kind, d)].append(o["ret_pct"])
        every.append(o["ret_pct"])
    # 基準：這批警示（不分種類）發出後有動的裡面，上漲的佔幾 %。盯的股票那幾天剛好在漲，任何「看漲」警示的順向率都會高，
    # 所以要跟基準比才知道這種警示本身有沒有用。
    moved_all = [x for x in every if x != 0]
    base_up = sum(1 for x in moved_all if x > 0) / len(moved_all) * 100 if moved_all else None
    out = []
    for (kind, d), xs in g.items():
        n = len(xs)
        moved = [x for x in xs if x != 0]          # 價格完全沒動（鎖漲跌停）的不算進順向率
        m = len(moved)
        row = {"kind": kind, "dir": d, "n": n, "flat": n - m, "up_rate": round(sum(1 for x in moved if x > 0) / m * 100) if m else None,
               "avg_raw": round(sum(xs) / n, 2), "avg_abs": round(sum(abs(x) for x in xs) / n, 2)}
        if d:
            row["with_dir"] = round(sum(1 for x in moved if x * d > 0) / m * 100) if m else None
            row["avg"] = round(sum(x * d for x in xs) / n, 2)
            if base_up is not None and row["with_dir"] is not None:
                row["base"] = round(base_up if d > 0 else 100 - base_up)
                row["edge"] = row["with_dir"] - row["base"]          # 比基準多幾個百分點
        out.append(row)
    for r in out:
        r["base_up"] = round(base_up) if base_up is not None else None
    return sorted(out, key=lambda r: -r["n"])


# ------------------------------------------------------------------ 3. 被擋掉的訊號

def blocked_table(sigs):
    """依過濾種類：照做會怎樣。回 [{kind, n, filled, win, loss, nofill, net_avg, net_sum}]，第一列是「發出去的」當對照。"""
    out = [dict(_tally([s for s in sigs if not s["blocked"]]), kind="發出去的訊號")]
    kinds = sorted({s.get("block_kind") or "其他過濾" for s in sigs if s["blocked"]})
    for k in kinds:
        out.append(dict(_tally([s for s in sigs if s["blocked"] and (s.get("block_kind") or "其他過濾") == k]), kind=k))
    if len(kinds) > 1:
        out.append(dict(_tally([s for s in sigs if s["blocked"]]), kind="被擋掉的合計"))
    return out


def entry_table(sigs):
    """
    依「進場方式 × 確認前有沒有追價」分四格（發出的和被擋的都算，被擋的是假設照做）：
    追價 = 確認前 5 分鐘已順向走 ≥0.5% 且 ≥2 檔（monitor 的追價過濾，2026-11-11 起這類只追蹤不發）。
    拿來回答「等回掛單到底有沒有害」：歷史回測沒追價的等回並不差，實際紀錄太少還分不出來。
    """
    out = []
    for chase, cn in ((False, "沒追價"), (True, "追價")):
        for mode in ("現價進場", "等回掛單"):
            out.append(dict(_tally([s for s in sigs if s.get("chase") is chase and s.get("mode") == mode]), kind=f"{cn}・{mode}", chase=chase))
    unk = [s for s in sigs if s.get("chase") is None]
    if unk:
        out.append(dict(_tally(unk), kind="沒有前 5 分鐘的資料", chase=None))
    return out


def limit_table(sigs):
    """
    追價的訊號（確認前 5 分鐘已順向走 ≥0.5% 且 ≥2 檔）再依「當時離漲跌停多遠」× 進場方式分格，沒有訊號的格不列。
    用來追蹤一個特例：一般的追價在回測裡比任意進場差，但接近漲跌停時可能會被吸過去（磁吸效應）。
    這些現在都被追價過濾擋掉，只追蹤假設照做的結果，不影響發不發訊號。
    """
    bands = (("near", f"離漲跌停 {10 - NEAR_LIMIT_PCT:.0f}% 內"), ("strong", f"已走 {STRONG_PCT:.0f}～{NEAR_LIMIT_PCT:.0f}%"), ("far", f"已走不到 {STRONG_PCT:.0f}%"))
    ch = [s for s in sigs if s.get("chase") is True]
    out = []
    for key, name in bands:
        for mode in ("現價進場", "等回掛單"):
            grp = [s for s in ch if s.get("limit_band") == key and s.get("mode") == mode]
            if grp:
                vp = [s["vol_pace"] for s in grp if s.get("vol_pace") is not None]
                out.append(dict(_tally(grp), kind=f"{name}・{mode}", band=key, vol_pace=round(sum(vp) / len(vp), 1) if vp else None))
    unk = [s for s in ch if s.get("limit_band") is None]
    if unk:
        out.append(dict(_tally(unk), kind="沒有當時漲跌幅的資料", band=None, vol_pace=None))
    return out


# ------------------------------------------------------------------ 組起來

def build(date=None):
    iso = _iso(date)
    if not date:                         # 沒指定：今天有紀錄就今天，否則最近一個有紀錄的交易日（盤前、休市日、還沒開始監控時打開復盤）
        ds = dates()
        if ds and iso not in ds:
            iso = ds[0]
    rows = _monitor_rows(iso)
    ser = series(rows)
    sigs = signals(iso)
    codes = sorted(set(ser) | {s["code"] for s in sigs})
    names = {c: (ser.get(c) or {}).get("name") or next((s["name"] for s in sigs if s["code"] == c), "") for c in codes}
    al_day = [r for r in _alerts_all() if r.get("date") == iso]
    tr = trades(iso)

    # 累計（訊號只算新判法 scoring=2，舊判法大多記平、不能比）
    sig_days = sorted({r.get("date") for r in _signals_all() if r.get("scoring") == 2})
    all_sigs = [s for d in sig_days for s in signals(d) if s.get("scoring") == 2]
    cum_dir_rows = []
    for d in dates():
        cum_dir_rows.append(direction(_monitor_rows(d)))
    cum_dir = {k: sum(x.get(k, 0) for x in cum_dir_rows) for k in ("n", "hit", "miss", "flat", "up")}
    cum_dir["days"] = len([x for x in cum_dir_rows if x.get("n")])
    cum_dir["hit_rate"] = round(cum_dir["hit"] / (cum_dir["hit"] + cum_dir["miss"]) * 100, 1) if cum_dir["hit"] + cum_dir["miss"] else None
    cum_dir["base_up"] = round(cum_dir["up"] / (cum_dir["hit"] + cum_dir["miss"]) * 100, 1) if cum_dir["hit"] + cum_dir["miss"] else None
    for key in ("by_phase", "by_side"):
        cum_dir[key] = {}
        for nm in sorted({k for x in cum_dir_rows for k in (x.get(key) or {})}):
            parts = [x[key][nm] for x in cum_dir_rows if (x.get(key) or {}).get(nm, {}).get("n")]
            h, m, n = sum(p["hit"] for p in parts), sum(p["miss"] for p in parts), sum(p["n"] for p in parts)
            cum_dir[key][nm] = {"n": n, "hit": h, "miss": m, "hit_rate": round(h / (h + m) * 100, 1) if h + m else None,
                                "avg": round(sum(p["avg"] * p["n"] for p in parts) / n, 3) if n else None}
    cum_dir["avg"] = round(sum(x["avg"] * x["n"] for x in cum_dir_rows if x.get("n")) / cum_dir["n"], 3) if cum_dir["n"] else None

    by_code = {}
    for c in codes:
        cs = [s for s in sigs if s["code"] == c]
        by_code[c] = {"name": names[c], "y": (ser.get(c) or {}).get("y"), "pts": (ser.get(c) or {}).get("pts") or [], "src": (ser.get(c) or {}).get("src"),
                      "signals": cs, "issued": _tally([s for s in cs if not s["blocked"]]), "blocked": _tally([s for s in cs if s["blocked"]]),
                      "trades": [t for t in tr["list"] if t["code"] == c],
                      "direction": direction([r for r in rows if r["code"] == c]),
                      "alerts": alerts([r for r in al_day if r.get("code") == c])}
    first = min((r["time"] for r in rows), default=None)
    last = max((r["time"] for r in rows), default=None)
    return {
        "date": iso, "dates": dates(), "codes": codes, "names": names, "first": first, "last": last, "cost": COST, "horizon": HORIZON, "edge": EDGE,
        "by_code": by_code,
        "scorecard": {"mine": tr, "issued": _tally([s for s in sigs if not s["blocked"]]), "blocked": _tally([s for s in sigs if s["blocked"]])},
        "blocked": {"day": blocked_table(sigs), "cum": blocked_table(all_sigs), "cum_days": len(sig_days), "cum_first": sig_days[0] if sig_days else None},
        "entry": {"day": entry_table(sigs), "cum": entry_table(all_sigs), "chase_pct": CHASE_PCT, "chase_ticks": CHASE_TICKS,
                  "limit": limit_table(all_sigs), "near_pct": NEAR_LIMIT_PCT, "strong_pct": STRONG_PCT},
        "direction": {"day": direction(rows), "cum": cum_dir},
        "alerts": {"day": alerts(al_day), "cum": alerts(_alerts_all()), "cum_days": len({r.get("date") for r in _alerts_all()})},
    }


# ------------------------------------------------------------------ 6. 明天的準備（要連線）

def ref_day(now=None):
    """「明天的準備」用哪一天收盤後的資料：最近一個已經收盤的交易日。
    交易日 13:30 以後是今天；開盤前、盤中、休市日是前一個交易日（那時候看到的是「今天的準備」）。"""
    now = now or cal.now_tw()
    d = now.date()
    if not cal.is_trading_day(d) or now.strftime("%H:%M") < "13:30":
        d = cal.prev_trading_day(d)
    return d


def tomorrow(codes, names=None):
    """
    最近一個交易日收盤後的資料 → 下一個交易日的準備。分點表通常傍晚才出、全市場行情下午三點後才有，還沒出就標 pending。
    回 {"date"（資料是哪一天的）, "for_today"（下一個交易日是不是就是今天：隔天開盤前打開時）, "brokers": {code: {...}}, "screener": {...}}
    """
    import src_moneydj
    import daytraders
    today = ref_day()
    iso = today.isoformat()
    out = {"date": iso, "for_today": iso != cal.today_tw().isoformat() and cal.is_trading_day(cal.today_tw()),
           "brokers": {}, "screener": None, "built": dt.datetime.now().strftime("%H:%M:%S")}
    for c in codes:
        try:
            s = src_moneydj.broker_summary(c)
            d = (s.get("date") or "").replace("/", "-")
            if d != iso:
                out["brokers"][c] = {"pending": True, "have": d}
                continue
            dts = daytraders.for_stock(s, None)
            out["brokers"][c] = {"date": d, "dt": dts, "buyers": [{"broker": r["broker"], "net": r["net"], "pct": r.get("pct")} for r in s["buyers"][:5]],
                                 "sellers": [{"broker": r["broker"], "net": r["net"], "pct": r.get("pct")} for r in s["sellers"][:5]]}
        except Exception as e:
            out["brokers"][c] = {"error": str(e)[:120]}
    try:
        import theme_screener as TS
        res = TS.to_dict(TS.screen(TS.default_ref(iso), 3))
        out["screener"] = res if res["date"] == iso else {"pending": True, "have": res["date"]}
    except BaseException as e:           # screen() 資料不足時會 SystemExit
        out["screener"] = {"error": str(e)[:160] or e.__class__.__name__}
    return out


# ------------------------------------------------------------------ 指令列

def describe(r):
    L = [f"══ 復盤 {r['date']}（紀錄 {r['first'] or '-'} ～ {r['last'] or '-'}，{len(r['codes'])} 檔：{'、'.join(c + ' ' + (r['names'][c] or '') for c in r['codes'])}）"]
    f = lambda v, d=2: "-" if v is None else f"{v:+.{d}f}%"
    sc = r["scorecard"]
    m = sc["mine"]
    L.append("\n1. 成績單")
    L.append(f"   你的單：{m['n']} 筆" + (f"，{m['win']} 賺 {m['loss']} 賠，合計 {f(m['net_sum'])}（已扣成本）" if m["n"] else "（持倉面板當天沒有記錄）"))
    for t in m["list"]:
        L.append(f"     {t['time'][:5]} {t['code']} {t['label']} {t['entry_px']} → {t['price']}  {f(t['net_pct'])}  {t['how']}")
    for k, nm in (("issued", "模型發出的訊號"), ("blocked", "被擋掉的訊號（如果照做）")):
        s = sc[k]
        L.append(f"   {nm}：{s['n']} 個，成交 {s['filled']}（{s['win']} 贏 {s['loss']} 輸）、沒成交 {s['nofill']}，成交的合計 {f(s['net_sum'])}、每筆 {f(s['net_avg'])}")
    L.append("\n2. 訊號回顧")
    for c in r["codes"]:
        for s in r["by_code"][c]["signals"]:
            res = {"win": "贏", "loss": "輸", "nofill": "沒成交", "flat": "平"}.get(s["verdict"], s["verdict"] or "?")
            L.append(f"   {s['time'][:5]} {c} {s['label']} @{s['price']}  {res} {f(s['net_pct']) if s['net_pct'] is not None else ''}"
                     + (f"  [擋：{s['block_kind']}]" if s["blocked"] else ""))
    L.append("\n3. 被擋掉的訊號：照做會怎樣")
    for title, tb in (("當天", r["blocked"]["day"]), (f"累計 {r['blocked']['cum_days']} 天", r["blocked"]["cum"])):
        L.append(f"   {title}")
        for b in tb:
            L.append(f"     {b['kind']:<10} {b['n']:>3} 個｜成交 {b['filled']:>2}：{b['win']} 贏 {b['loss']} 輸｜沒成交 {b['nofill']}｜每筆 {f(b['net_avg'])}"
                     + (f"（另有 {b['dup']} 個是同一筆重複提示，不重複算）" if b.get("dup") else ""))
    d, cd = r["direction"]["day"], r["direction"]["cum"]
    L.append(f"\n4. 模型方向（強度差 ≥{r['edge']}，{r['horizon']} 分鐘後）")
    if d.get("n"):
        L.append(f"   當天 {d['n']} 個不重疊的樣本（對 {d['hit']}、錯 {d['miss']}、價格沒動 {d['flat']}），方向對 {d['hit_rate']}%（一律猜漲會對 {d['base_up']}%），順向平均 {f(d['avg'], 3)}；"
                 + "、".join(f"{k} {v['hit_rate']}%（{v['n']}）" for k, v in d["by_phase"].items() if v.get("n")))
    L.append(f"   累計 {cd['days']} 天 {cd['n']} 個樣本（對 {cd['hit']}、錯 {cd['miss']}、沒動 {cd['flat']}），方向對 {cd['hit_rate']}%（一律猜漲會對 {cd['base_up']}%），順向平均 {f(cd['avg'], 3)}"
             + "；" + "、".join(f"偏{k} {v['hit_rate']}%（{v['n']}）" for k, v in cd["by_side"].items() if v.get("n")))
    L.append("\n5. 警示回顧（30 分鐘後）")
    cum = {a["kind"]: a for a in r["alerts"]["cum"]}
    for a in r["alerts"]["day"]:
        c = cum.get(a["kind"], {})
        day = f"順向 {a['with_dir']}%、平均 {f(a['avg'])}" if a["dir"] else f"之後漲的佔 {a['up_rate']}%、平均動 {a['avg_abs']:.2f}%"
        cu = (f"順向 {c['with_dir']}%（基準 {c.get('base')}%，差 {c.get('edge', 0):+d} 點）" if c.get("dir") and c.get("with_dir") is not None
              else f"漲的佔 {c.get('up_rate')}%") if c else "-"
        L.append(f"   {a['kind']:<14} 當天 {a['n']:>3} 則：{day}｜累計 {c.get('n', 0)} 則：{cu}")
    return "\n".join(L)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    r = build(args[0] if args else None)
    print(describe(r))
    if "--tomorrow" in sys.argv:
        t = tomorrow(r["codes"])
        print("\n6. 明天的準備")
        for c, b in t["brokers"].items():
            if b.get("pending"):
                print(f"   {c}：今天的分點表還沒出（目前只有 {b.get('have')}）")
            elif b.get("dt"):
                print(f"   {c}：隔日沖分點今天買超 {b['dt']['buy']} 張（{b['dt'].get('buy_pct') or 0:.1f}% 量），明天約倒回 {b['dt']['expect_sell']} 張："
                      + "、".join(f"{x['broker']} {x['net']}" for x in b["dt"]["list"][:4]))
        s = t["screener"] or {}
        if s.get("pending"):
            print(f"   選股：今天的全市場資料還沒出（目前只有 {s.get('have')}）")
        elif s.get("short") is not None:
            print("   明天候選：做空 " + ("、".join(f"{x['code']} {x['name']}" for x in s["short"][:3]) or "無") + "｜做多 " + ("、".join(f"{x['code']} {x['name']}" for x in s["long"][:3]) or "無"))
