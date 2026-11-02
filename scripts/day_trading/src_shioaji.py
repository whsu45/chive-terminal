"""
永豐 Shioaji 備援/強化資料源（選用）。
- 不裝套件或 .env 沒金鑰時整個模組靜默停用，主程式自動退回 MIS 公開報價。
- 盤中只用「訂閱串流」累積逐筆成交（大單/散戶分流），不輪詢 ticks/kbars：
  官方限制盤中 ticks 查詢 10 次/日，kbars 270 次/日，輪詢歷史查詢可能被停權。
- 開盤後才啟動時，用一次 api.ticks(AllDay) 回補當日逐筆（只佔 1 次額度）。
- 大單/散戶不是用固定張數分：用「單筆金額」加「該股當日成交張數分位」推算，見 lot_thresholds()；
  另外偵測「同張數、同方向、等間隔」的規律小單（主力切單藏量），見 detect_iceberg()。
"""
import os
import math
import time
import threading
import datetime as dt

from config import load_env

_api = None
_lock = threading.Lock()
_state = {}  # code -> accumulator


def available():
    load_env()
    if not (os.environ.get("SHIOAJI_API_KEY") and os.environ.get("SHIOAJI_SECRET_KEY")):
        return False
    try:
        import shioaji  # noqa: F401
        return True
    except ImportError:
        return False


def _new_acc():
    return {"ticks": [], "last": None, "high": None, "low": None,
            "bid_total": None, "ask_total": None, "bid_cnt": None, "ask_cnt": None}


def ns_to_dt(ts):
    """Shioaji 1.7 歷史逐筆的 ts 是奈秒整數，已經是台灣時間（當 UTC 解讀即可）；串流 callback 的 datetime 已是 datetime。"""
    if isinstance(ts, dt.datetime):
        return ts
    return dt.datetime.utcfromtimestamp(int(ts) / 1e9)


def get_contract(api, code):
    try:
        c = api.contracts.get(code)
        if c is not None:
            return c
    except Exception:
        pass
    return api.Contracts.Stocks[code]


def _ingest(code, ts, price, vol, tick_type, big_lot=None):
    """只存逐筆 (ts, price, vol, tick_type)；大單/散戶分類在 flow() 時依當下價格與分布決定。"""
    acc = _state.setdefault(code, _new_acc())
    if vol <= 0 or price is None:
        return
    acc["last"] = price
    acc["high"] = price if acc["high"] is None else max(acc["high"], price)
    acc["low"] = price if acc["low"] is None else min(acc["low"], price)
    acc["ticks"].append((ts, float(price), int(vol), int(tick_type)))


def lot_thresholds(price, sizes=None):
    """
    大單/散戶門檻（張）。張數本身沒有意義，金額才有：買 1 張台積電的錢可以買幾百張友達。
      大單 = 單筆金額 >= BIG_LOT_AMOUNT（預設 300 萬，calibrate.py 校準值），且不低於當日單筆成交張數的 95 分位
             （流動性好的股票 500 萬可能只是常態單，分位數把門檻抬到該股真正的「異常大單」）
      散戶 = 單筆金額 <  SMALL_LOT_AMOUNT（預設 100 萬），1 張單一律算散戶
      中間 = 中實戶，不計入兩邊
    BIG_LOT_THRESHOLD 有填（>0）就固定用該張數，不自動推算。
    """
    fixed = int(os.environ.get("BIG_LOT_THRESHOLD", "0") or 0)
    big_amt = float(os.environ.get("BIG_LOT_AMOUNT", "3000000") or 3000000)
    small_amt = float(os.environ.get("SMALL_LOT_AMOUNT", "1000000") or 1000000)
    per_lot = max(price, 0.01) * 1000
    big_by_amt = max(1, math.ceil(big_amt / per_lot))
    small_lots = max(1, math.ceil(small_amt / per_lot))
    p95 = None
    if sizes and len(sizes) >= 200:
        srt = sorted(sizes)
        p95 = srt[int(len(srt) * 0.95)]
    if fixed > 0:
        big_lots = fixed
    else:
        big_lots = max(big_by_amt, p95 or 0, 2)
    small_lots = min(small_lots, big_lots - 1) if big_lots > 1 else 1
    return {"big_lots": big_lots, "small_lots": small_lots, "big_by_amount": big_by_amt, "p95": p95,
            "big_amount": big_amt, "small_amount": small_amt, "fixed": fixed or None}


def detect_iceberg(ticks, min_count=8, min_share=0.12, max_cv=0.9):
    """
    規律小單偵測：主力用演算法把大單切成固定張數、固定方向、近似等間隔的小單慢慢買或賣。
    找出 (張數, 方向) 組合中出現次數 >= min_count、佔筆數 >= min_share、
    且同方向比例 >= 75% 的；間隔變異係數 (cv) 越低越像機器單。
    回 [{size, side, count, volume, share_cnt, interval_cv, regular}]，依量排序。
    """
    if len(ticks) < min_count * 2:
        return []
    from collections import defaultdict
    groups = defaultdict(list)
    for ts, p, v, tt in ticks:
        if tt in (1, 2):
            groups[(v, tt)].append(ts)
    n = sum(1 for _, _, _, tt in ticks if tt in (1, 2)) or 1
    out = []
    for (size, side), tss in groups.items():
        cnt = len(tss)
        if cnt < min_count or cnt / n < min_share:
            continue
        # 同張數反方向的筆數：要明顯單邊
        other = len(groups.get((size, 3 - side), []))
        if cnt / (cnt + other) < 0.75:
            continue
        # 間隔規律性
        tss = sorted(tss)
        gaps = [(b - a).total_seconds() for a, b in zip(tss, tss[1:]) if hasattr(b - a, "total_seconds")]
        cv = None
        if len(gaps) >= 5:
            m = sum(gaps) / len(gaps)
            if m > 0:
                sd = (sum((g - m) ** 2 for g in gaps) / len(gaps)) ** 0.5
                cv = sd / m
        out.append({"size": size, "side": "買" if side == 1 else "賣", "count": cnt, "volume": size * cnt,
                    "share_cnt": cnt / n, "interval_cv": cv, "regular": (cv is not None and cv <= max_cv),
                    "avg_gap_s": (sum(gaps) / len(gaps)) if gaps else None})
    out.sort(key=lambda x: -x["volume"])
    return out[:3]


def login():
    """登入並回傳 api；失敗回 None。"""
    global _api
    if _api is not None:
        return _api
    if not available():
        return None
    import shioaji as sj
    sim = os.environ.get("SHIOAJI_SIMULATION", "1") != "0"
    try:
        api = sj.Shioaji(simulation=sim)
        api.login(api_key=os.environ["SHIOAJI_API_KEY"], secret_key=os.environ["SHIOAJI_SECRET_KEY"], subscribe_trade=False)
        # 1.7 版合約在背景下載，等它完成
        for _ in range(60):
            try:
                if get_contract(api, "2330") is not None:
                    break
            except Exception:
                pass
            time.sleep(0.5)
        _api = api
        return api
    except Exception as e:  # 登入失敗不應影響主流程
        print(f"[shioaji] 登入失敗，退回公開資料源：{e}")
        return None


def usage():
    try:
        u = _api.usage()
        return {"used_mb": u.bytes / 1048576, "limit_mb": u.limit_bytes / 1048576, "remaining_mb": u.remaining_bytes / 1048576}
    except Exception:
        return None


def start_stream(codes, big_lot=None):
    """訂閱逐筆成交 + 五檔，並回補今日已成交逐筆。回傳成功訂閱的代碼清單。"""
    api = login()
    if api is None:
        return []
    import shioaji as sj
    ok = []

    @api.on_tick_stk_v1()
    def _on_tick(exchange, tick):
        try:
            if getattr(tick, "simtrade", 0):  # 試撮不計
                return
            with _lock:
                _ingest(tick.code, tick.datetime, float(tick.close), int(tick.volume), int(tick.tick_type))
                acc = _state[tick.code]
                acc["bid_total"] = int(getattr(tick, "bid_side_total_vol", 0) or 0)
                acc["ask_total"] = int(getattr(tick, "ask_side_total_vol", 0) or 0)
                acc["bid_cnt"] = int(getattr(tick, "bid_side_total_cnt", 0) or 0)
                acc["ask_cnt"] = int(getattr(tick, "ask_side_total_cnt", 0) or 0)
        except Exception:
            pass

    today = dt.date.today().isoformat()
    for code in codes:
        try:
            contract = get_contract(api, code)
            if contract is None:
                continue
            # 回補今日逐筆（1 次額度）
            try:
                t = api.ticks(contract=contract, date=today)
                with _lock:
                    _state[code] = _new_acc()
                    for ts, c, v, tt in zip(t.ts, t.close, t.volume, t.tick_type):
                        _ingest(code, ns_to_dt(ts), float(c), int(v), int(tt))
            except Exception as e:
                print(f"[shioaji] {code} 回補逐筆失敗：{e}")
            api.quote.subscribe(contract, quote_type=sj.constant.QuoteType.Tick, version=sj.constant.QuoteVersion.v1)
            ok.append(code)
        except Exception as e:
            print(f"[shioaji] {code} 訂閱失敗：{e}")
    return ok


def flow(code, window_min=None, price=None):
    """
    目前累積的主力/散戶分流。window_min 給了就只算最近 N 分鐘。
    分類規則見 lot_thresholds()；規律小單（藏單）會從散戶改歸主力，見 detect_iceberg()。
    回 {big_net, small_net, mid_net, buy_vol, sell_vol, volume, vwap, big_net_pct, small_net_pct,
        thresholds, iceberg, hidden_net, bid_total, ask_total, ticks}
    """
    with _lock:
        acc = _state.get(code)
        if not acc or not acc["ticks"]:
            return None
        ticks = list(acc["ticks"])
        bid_total, ask_total, bid_cnt, ask_cnt, last = acc["bid_total"], acc["ask_total"], acc["bid_cnt"], acc["ask_cnt"], acc["last"]
    if window_min:
        cutoff = dt.datetime.now() - dt.timedelta(minutes=window_min)
        ticks = [t for t in ticks if t[0] >= cutoff]
        if not ticks:
            return None
    ref = price or last or ticks[-1][1]
    th = lot_thresholds(ref, [v for _, _, v, _ in ticks])
    big, small = th["big_lots"], th["small_lots"]
    ice = detect_iceberg(ticks)
    ice_keys = {(i["size"], 1 if i["side"] == "買" else 2) for i in ice if i["regular"] or i["share_cnt"] >= 0.2}

    bb = bs = sb = ss = mb = ms = hb = hs = 0
    vol = 0; amt = 0.0
    for ts, p, v, tt in ticks:
        vol += v; amt += p * v
        if tt not in (1, 2):
            continue
        is_buy = tt == 1
        if (v, tt) in ice_keys:            # 規律小單 → 主力藏單
            if is_buy: hb += v
            else: hs += v
        elif v >= big:
            if is_buy: bb += v
            else: bs += v
        elif v < small or v == 1:          # 1 張單一律視為散戶（高價股 1 張就超過散戶金額門檻）
            if is_buy: sb += v
            else: ss += v
        else:
            if is_buy: mb += v
            else: ms += v
    if vol == 0:
        return None
    big_net = (bb - bs) + (hb - hs)
    return {"big_net": big_net, "small_net": sb - ss, "mid_net": mb - ms, "hidden_net": hb - hs,
            "big_buy": bb + hb, "big_sell": bs + hs, "small_buy": sb, "small_sell": ss,
            "buy_vol": bb + sb + mb + hb, "sell_vol": bs + ss + ms + hs, "volume": vol, "vwap": amt / vol,
            "big_net_pct": big_net / vol * 100, "small_net_pct": (sb - ss) / vol * 100,
            "hidden_net_pct": (hb - hs) / vol * 100,
            "thresholds": th, "iceberg": ice, "window_min": window_min,
            "bid_total": bid_total, "ask_total": ask_total, "bid_cnt": bid_cnt, "ask_cnt": ask_cnt, "ticks": len(ticks)}


def logout():
    global _api
    if _api is not None:
        try:
            _api.logout()
        except Exception:
            pass
        _api = None
