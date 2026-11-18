"""
永豐 Shioaji 備援/強化資料源（選用）。
- 不裝套件或 .env 沒金鑰時整個模組靜默停用，主程式自動退回 MIS 公開報價。
- 盤中只用「訂閱串流」累積逐筆成交（大單/散戶分流），不輪詢 ticks/kbars：
  官方限制盤中 ticks 查詢 10 次/日，kbars 270 次/日，輪詢歷史查詢可能被停權。
- 開盤後才啟動時，用一次 api.ticks(AllDay) 回補當日逐筆（只佔 1 次額度）。
  額度是硬上限（fetch_ticks 用完就不再送查詢）；收到的逐筆每分鐘存到本機（.cache/ticks_live），重啟時先讀本機的，不重複查。
- 大單/散戶不是用固定張數分：用「單筆金額」加「該股當日成交張數分位」推算，見 lot_thresholds()；
  另外偵測「同張數、同方向、等間隔」的規律小單（主力切單藏量），見 detect_iceberg()。
"""
import os
import math
import time
import threading
import datetime as dt

from config import load_env, CACHE_DIR

# 永豐官方「使用限制」（https://sinotrade.github.io/zh/tutor/limit/，2026-11-13 查）：盤中查詢 ticks 次數不得超過 10 次、kbars 270 次；
# 超過 → 暫停服務一分鐘，當日連續多次超過 → 暫停該 IP 及 ID 使用權。限制是算在同一個身分證字號（person_id）上，不是每台電腦各 10 次。
TICKS_DAILY_LIMIT = 10
LIVE_DIR = os.path.join(CACHE_DIR, "ticks_live")     # 盤中收到的逐筆，每分鐘存一次；重啟時先讀這裡，不重複查（不進 git）
# 本機存的逐筆離現在不到這麼多秒就直接用、不花查詢額度。原本是 300 秒：2026-11-17 盤中重開 monitor，事欣科的存檔停在 11:14:42、
# 新程式 11:18 才接上串流，差 3 分多鐘照樣直接用，中間約 140 張逐筆就這樣沒了（大戶散戶跟籌碼K線對不上的原因之一）。
# 現在超過 20 秒、而且還有額度就重查全日逐筆（一次額度）；額度用完才用存檔，空檔標在儀表板上（flow()["partial"]["gaps"]）。
CACHE_GAP_OK = 20


class TicksQuota(Exception):
    """今天盤中的 ticks 查詢額度用完了，這次查詢沒有送出去。"""


def ticks_budget():
    """這台電腦今天盤中最多查幾次 ticks。預設 10（官方上限）；同一個帳號有兩台電腦在跑時，在 .env 設 SHIOAJI_TICKS_BUDGET=5 各分一半。"""
    load_env()
    try:
        return max(0, min(TICKS_DAILY_LIMIT, int(os.environ.get("SHIOAJI_TICKS_BUDGET", TICKS_DAILY_LIMIT))))
    except ValueError:
        return TICKS_DAILY_LIMIT


def _intraday(now=None):
    """官方限制的是「盤中」的查詢。週一到週五 08:30–13:35 都當盤中（不查假日，寧可多算）。"""
    now = now or dt.datetime.now()
    return now.weekday() < 5 and "08:30" <= now.strftime("%H:%M") < "13:35"


def _ticks_usage_path():
    return os.path.join(CACHE_DIR, f"ticks_usage_{dt.date.today():%Y%m%d}.json")


def ticks_usage():
    """今天這台機器在盤中呼叫過幾次 api.ticks（本機計數檔，跨程式累加；永豐沒有查詢次數的 API，另一台電腦用掉的這裡看不到）。"""
    try:
        import json
        with open(_ticks_usage_path()) as f:
            return int(json.load(f).get("count", 0))
    except Exception:
        return 0


def ticks_left():
    return max(0, ticks_budget() - ticks_usage())


def fetch_ticks(api, contract, date, why=""):
    """
    所有 api.ticks 都走這裡。盤中：額度用完就丟 TicksQuota、不送查詢（官方超過會停權）；還有額度就先記一次再查。
    收盤後的查詢（校準、回補歷史）不算盤中額度，也不擋。
    """
    import json
    code = getattr(contract, "code", "?")
    if not _intraday():
        print(f"[shioaji] ticks 歷史查詢 {code} {date}（{why or '回補'}，盤後，不計入盤中額度）")
        with _api_lock:
            return api.ticks(contract=contract, date=date)
    used, budget = ticks_usage(), ticks_budget()
    if used >= budget:
        raise TicksQuota(f"今天盤中 ticks 查詢已用 {used}/{budget}，不再送出（官方上限 {TICKS_DAILY_LIMIT} 次，超過會被暫停）")
    n = used + 1
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(_ticks_usage_path(), "w") as f:
            json.dump({"count": n, "last": dt.datetime.now().isoformat(), "why": why}, f)
    except Exception:
        pass
    flag = "  ⚠ 快到上限" if n >= budget - 2 else ""
    print(f"[shioaji] ticks 歷史查詢 {code} {date}（{why or '回補'}）→ 今日盤中 ticks 已用 {n}/{budget}{flag}")
    with _api_lock:
        return api.ticks(contract=contract, date=date)


KBARS_DAILY_LIMIT = 270  # 官方：盤中查詢 kbars 次數不得超過 270 次


def kbars_budget():
    """這台電腦今天盤中最多查幾次分 K。預設 120（官方上限 270 的一半不到，留給另一台電腦）；.env 的 SHIOAJI_KBARS_BUDGET 可以改。"""
    load_env()
    try:
        return max(0, min(KBARS_DAILY_LIMIT, int(os.environ.get("SHIOAJI_KBARS_BUDGET", 120))))
    except ValueError:
        return 120


def _kbars_usage_path():
    return os.path.join(CACHE_DIR, f"kbars_usage_{dt.date.today():%Y%m%d}.json")


def kbars_usage():
    try:
        import json
        with open(_kbars_usage_path()) as f:
            return int(json.load(f).get("count", 0))
    except Exception:
        return 0


def fetch_kbars(api, contract, day, why="", end=None):
    """所有 api.kbars 都走這裡：盤中先記一次、用到上限就丟 TicksQuota 不送查詢；盤後不計。回 Kbars（ts 是每根的「結束」時間）。
    end 有給就查 day～end 一段（一次查詢、一次額度）。"""
    import json
    if _intraday():
        used, budget = kbars_usage(), kbars_budget()
        if used >= budget:
            raise TicksQuota(f"今天盤中 kbars 查詢已用 {used}/{budget}，不再送出（官方上限 {KBARS_DAILY_LIMIT} 次）")
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(_kbars_usage_path(), "w") as f:
                json.dump({"count": used + 1, "last": dt.datetime.now().isoformat(), "why": why}, f)
        except Exception:
            pass
    with _api_lock:
        return api.kbars(contract=contract, start=day, end=end or day)


def backfill_bars(api, contract, code, now=None):
    """
    用分 K 查詢把今天稍早的線圖補回來：逐筆補不回來（額度用完、或本機存檔中間有空檔）時，江波圖至少要有完整的價格走勢。
    只補「已經走完、而且現在還沒有」的那些分鐘，不動逐筆——所以主力 / 散戶分流、大單統計還是只有逐筆涵蓋的那一段。
    永豐分 K 的 ts 是每根的結束時間（09:01 那根 = 09:00–09:01），這裡往前挪一分鐘跟逐筆做的分 K 對齊；量是張、金額是元。
    回補了幾根（失敗回 0）。
    """
    now = now or dt.datetime.now()
    try:
        k = fetch_kbars(api, contract, now.date().isoformat(), "補線圖")
    except Exception as e:
        print(f"[shioaji] {code} 分 K 回補失敗：{str(e)[:100]}")
        return 0
    cur = now.strftime("%H:%M")
    n = 0
    with _lock:
        acc = _state.setdefault(code, _new_acc())
        for ts, o, h, l, c, v, a in zip(k.ts, k.Open, k.High, k.Low, k.Close, k.Volume, k.Amount):
            t = ns_to_dt(ts)
            if t.date() != now.date():
                continue
            key = "13:30" if t.strftime("%H:%M") == "13:30" else (t - dt.timedelta(minutes=1)).strftime("%H:%M")
            if key >= cur or key in acc["bars"]:          # 現在這一分鐘交給串流；逐筆已經做出來的不覆蓋
                continue
            acc["bars"][key] = [float(o), float(h), float(l), float(c), int(v), float(a) / 1000.0]     # amt 的單位跟逐筆一樣：價 × 張
            acc.setdefault("kbar_keys", set()).add(key)
            acc["high"] = float(h) if acc["high"] is None else max(acc["high"], float(h))
            acc["low"] = float(l) if acc["low"] is None else min(acc["low"], float(l))
            n += 1
    return n


def _live_path(code, day):
    return os.path.join(LIVE_DIR, f"{code}_{day}.json")


def save_ticks_cache():
    """把記憶體裡今天的逐筆存到本機（每檔一個檔，整檔覆寫）。monitor 的快迴圈每分鐘叫一次、登出前再叫一次。回存了幾檔。"""
    import json
    today = dt.date.today()
    with _lock:
        snap = {c: (list(a["ticks"]), {k: list(v) for k, v in (a.get("auction") or {}).items()}) for c, a in _state.items() if a.get("ticks")}
    n = 0
    for code, (ticks, auction) in snap.items():
        rows = [[round(t.hour * 3600 + t.minute * 60 + t.second + t.microsecond / 1e6, 3), p, v, tt]
                for t, p, v, tt in ticks if isinstance(t, dt.datetime) and t.date() == today]
        if not rows:
            continue
        try:
            os.makedirs(LIVE_DIR, exist_ok=True)
            path = _live_path(code, today.isoformat()); tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"code": code, "date": today.isoformat(), "saved": dt.datetime.now().strftime("%H:%M:%S"), "auction": auction, "ticks": rows}, f)
            os.replace(tmp, path)
            n += 1
        except Exception as e:
            print(f"[shioaji] {code} 逐筆存檔失敗：{str(e)[:80]}")
    return n


def _load_ticks_cache(code, day=None):
    """讀本機存的今天逐筆 → ([(datetime, 價, 量, 內外盤)], auction dict)；沒有或壞掉回 (None, {})。"""
    import json
    day = day or dt.date.today()
    try:
        with open(_live_path(code, day.isoformat())) as f:
            j = json.load(f)
        if j.get("date") != day.isoformat() or not j.get("ticks"):
            return None, {}
        base = dt.datetime.combine(day, dt.time())
        return [(base + dt.timedelta(seconds=r[0]), float(r[1]), int(r[2]), int(r[3])) for r in j["ticks"]], (j.get("auction") or {})
    except Exception:
        return None, {}


def backfill_today(api, contract, code, now=None):
    """
    盤中啟動時把今天已經成交的逐筆補回來，回一句說明（給啟動訊息印）。順序：
      1. 還沒開盤（09:00 前）：沒有東西要補，不查。
      2. 本機有存而且離現在不到 CACHE_GAP_OK（20 秒）：直接用，不花額度。
      3. 還有額度：查一次 api.ticks（完整的全日逐筆，連存檔之後那段一起補齊）；查失敗而本機有存就退回用本機的。
      4. 額度用完：本機有存就用（存檔到現在那段沒有逐筆，線圖用分 K 補、儀表板標出空檔），沒有就只有啟動之後的逐筆。
    start_stream 會先處理沒有存檔的代碼（不查就什麼都沒有），再處理有存檔、只差一段的，額度不夠時先犧牲後者。
    """
    now = now or dt.datetime.now()
    if now.strftime("%H:%M") < "09:00":
        with _lock:
            _state[code] = _new_acc()
        return "還沒開盤，不用回補"
    cached, auction = _load_ticks_cache(code, now.date())
    gap = (now - cached[-1][0]).total_seconds() if cached else None

    def use_cache(note):
        with _lock:
            _state[code] = _new_acc()
            for ts, p, v, tt in cached:
                _ingest(code, ts, p, v, tt)
            if auction:
                _state[code]["auction"] = {k: list(v) for k, v in auction.items()}
            if gap is not None and gap > CACHE_GAP_OK:
                # 存檔最後一筆到現在（接上串流）這段沒有逐筆：記下來，flow()["partial"]["gaps"] 讓儀表板標出來
                _state[code]["tick_gaps"] = [[cached[-1][0].strftime("%H:%M:%S"), now.strftime("%H:%M:%S")]]
        return f"用本機存的逐筆 {len(cached)} 筆（到 {cached[-1][0]:%H:%M:%S}，{note}）"

    def bars(msg):
        """逐筆不完整：用分 K 查詢把線圖缺的那幾分鐘補起來（分流還是只有逐筆涵蓋的那一段）。"""
        n = backfill_bars(api, contract, code, now)
        return msg + (f"；線圖用分 K 補了 {n} 分鐘" if n else "；線圖缺的那段補不回來")

    if cached and gap is not None and gap <= CACHE_GAP_OK:
        return use_cache(f"離現在 {gap:.0f} 秒，不花額度")
    if _intraday(now) and ticks_left() <= 0:
        if cached:
            return bars(use_cache(f"額度用完；{cached[-1][0]:%H:%M} 到現在這段沒有逐筆"))
        return bars(f"逐筆查詢額度用完（{ticks_usage()}/{ticks_budget()}）又沒有本機存檔：逐筆只有現在開始的，今天的主力 / 散戶分流不完整")
    try:
        t = fetch_ticks(api, contract, now.date().isoformat(), "盤中啟動回補")
        with _lock:
            _state[code] = _new_acc()
            for ts, c, v, tt in zip(t.ts, t.close, t.volume, t.tick_type):
                _ingest(code, ns_to_dt(ts), float(c), int(v), int(tt))
            n = len(_state[code]["ticks"])
        return f"查詢回補 {n} 筆"
    except Exception as e:
        if cached:
            return bars(use_cache(f"查詢失敗（{str(e)[:60]}）；{cached[-1][0]:%H:%M} 到現在這段沒有逐筆"))
        return bars(f"回補逐筆失敗：{str(e)[:100]}")

_api = None
_lock = threading.Lock()
# 永豐 api 物件（Rust 核心）不能兩條執行緒同時用：一條在 kbars/ticks 等回應（最久 30 秒）時，另一條去註冊 callback 或訂閱，
# 會丟「RuntimeError: Already borrowed」（2026-11-16 當時 monitor 裡的模組 B 背景抓歷史分 K、主程式同時開串流，monitor 因此當掉；
# 模組 B 之後移出 monitor，改成收盤後跑 ab_compare.py。盤中加入股票、指數串流等還是會有兩條執行緒碰 api）。
# 所以所有碰 api 的呼叫都排隊走這把鎖；可重入，fetch_ticks 包在 start_stream 裡也不會自己卡住。
# callback（_on_tick 等）不碰 api，不要在 callback 裡拿這把鎖。
_api_lock = threading.RLock()
_state = {}  # code -> accumulator
# 盤中移除監控（stop_stream）的代碼：取消訂閱後永豐可能還會送來幾筆，callback 看到就丟掉。
# 不擋的話 _state 會被那幾筆重新建出來，下一次 save_ticks_cache 就用「只有尾巴幾筆」蓋掉本機逐筆存檔（重啟/再加入時回補用）。
_muted = set()


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
    return {"ticks": [], "last": None, "high": None, "low": None, "open": None, "total_volume": None, "last_t": None,
            "bid_total": None, "ask_total": None, "bid_cnt": None, "ask_cnt": None,
            "bids": [], "asks": [], "book_t": None,     # 五檔 [[price, vol] ×5]（BidAsk 串流）
            "bars": {},   # "HH:MM" -> [o, h, l, c, vol, amt]（分 K，給江波圖）
            "auction": {}}   # 集合競價那一筆：{"open": [價, 張, 永豐標的內外盤], "close": [...]}，不算進買賣分流


def _bar_update(acc, ts, price, vol):
    key = ts.strftime("%H:%M") if isinstance(ts, dt.datetime) else str(ts)[:5]
    b = acc["bars"].get(key)
    if b is None:
        acc["bars"][key] = [price, price, price, price, vol, price * vol]
    else:
        b[1] = max(b[1], price); b[2] = min(b[2], price); b[3] = price; b[4] += vol; b[5] += price * vol


def ns_to_dt(ts):
    """Shioaji 1.7 歷史逐筆的 ts 是奈秒整數，已經是台灣時間（當 UTC 解讀即可）；串流 callback 的 datetime 已是 datetime。"""
    if isinstance(ts, dt.datetime):
        return ts
    return dt.datetime.utcfromtimestamp(int(ts) / 1e9)


def get_contract(api, code):
    with _api_lock:
        try:
            c = api.contracts.get(code)
            if c is not None:
                return c
        except Exception:
            pass
        return api.Contracts.Stocks[code]


OPEN_AUCTION_END = 9 * 3600 + 5 * 60      # 當天第一筆、而且在 09:05 前 → 開盤集合競價（延緩開盤也涵蓋；歷史 339 個股票日最晚 09:02）
CLOSE_AUCTION_START = 13 * 3600 + 25 * 60  # 13:25 之後只剩 13:30 收盤集合競價和 14:30 盤後定價
DIST_AMOUNTS = (200000, 500000)           # flow()["dist"] 散戶那一段再細分的級距下緣（單筆金額）；再上去就是散戶 / 主力門檻


def _auction_kind(ts, first):
    """這一筆是不是集合競價撮合出來的。集合競價是所有委託一次撮合成一個價，沒有「誰主動」，但永豐照樣標外盤 / 內盤，
    而且常常一筆就是幾百張——會被當成一張大戶主動買賣單，一整天掛在大戶累積買賣超上（2026-11-12 台郡：開盤那筆約 520 張標外盤，
    dashboard 整天顯示大戶買超，籌碼K線是賣超）。"""
    if not isinstance(ts, dt.datetime):
        return None
    tod = ts.hour * 3600 + ts.minute * 60 + ts.second
    if tod >= CLOSE_AUCTION_START:
        return "close"
    if first and tod < OPEN_AUCTION_END:
        return "open"
    return None


def _ingest(code, ts, price, vol, tick_type, big_lot=None):
    """只存逐筆 (ts, price, vol, tick_type)；大單/散戶分類在 flow() 時依當下價格與分布決定。
    集合競價那一筆（開盤、收盤、盤後定價）內外盤改記 0：照樣算進成交量和 VWAP，但不算進任何一邊的買賣。"""
    acc = _state.setdefault(code, _new_acc())
    if vol <= 0 or price is None:
        return
    kind = _auction_kind(ts, not acc["ticks"]) if int(tick_type) in (1, 2) else None
    if kind:
        a = acc.setdefault("auction", {}).setdefault(kind, [float(price), 0, int(tick_type)])
        a[0] = float(price); a[1] += int(vol)
        tick_type = 0
    acc["last"] = price
    acc["high"] = price if acc["high"] is None else max(acc["high"], price)
    acc["low"] = price if acc["low"] is None else min(acc["low"], price)
    acc["ticks"].append((ts, float(price), int(vol), int(tick_type)))
    _bar_update(acc, ts, float(price), int(vol))


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
    with _api_lock:          # 好幾條執行緒同時叫 login 時只登入一次（以前會各自建一個連線）
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
        with _api_lock:
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

    def _on_tick(exchange, tick):
        try:
            if getattr(tick, "simtrade", 0):  # 試撮不計
                return
            with _lock:
                if tick.code in _muted:           # 已移除監控（stop_stream）：取消訂閱前後漏進來的不收
                    return
                _ingest(tick.code, tick.datetime, float(tick.close), int(tick.volume), int(tick.tick_type))
                acc = _state[tick.code]
                acc["last_t"] = tick.datetime
                acc["recv_t"] = time.time()          # 本機收到的時間：跟交易所時間的差 = 行情送到這台電腦花了多久
                for src, dst in (("open", "open"), ("total_volume", "total_volume")):
                    v = getattr(tick, src, None)
                    if v:
                        acc[dst] = float(v) if dst == "open" else int(v)
                acc["bid_total"] = int(getattr(tick, "bid_side_total_vol", 0) or 0)
                acc["ask_total"] = int(getattr(tick, "ask_side_total_vol", 0) or 0)
                acc["bid_cnt"] = int(getattr(tick, "bid_side_total_cnt", 0) or 0)
                acc["ask_cnt"] = int(getattr(tick, "ask_side_total_cnt", 0) or 0)
        except Exception:
            pass

    def _on_bidask(exchange, ba):
        try:
            if getattr(ba, "simtrade", 0):
                return
            bids = [[float(p), int(v)] for p, v in zip(ba.bid_price, ba.bid_volume) if p]
            asks = [[float(p), int(v)] for p, v in zip(ba.ask_price, ba.ask_volume) if p]
            with _lock:
                if ba.code in _muted:
                    return
                acc = _state.setdefault(ba.code, _new_acc())
                acc["bids"], acc["asks"], acc["book_t"] = bids, asks, getattr(ba, "datetime", None)
                acc["book_recv_t"] = time.time()
        except Exception:
            pass

    with _api_lock:          # 等於 @api.on_tick_stk_v1() 裝飾；包在鎖裡，背景執行緒在用 api 時不會撞上
        api.on_tick_stk_v1()(_on_tick)
        api.on_bidask_stk_v1()(_on_bidask)

    def _has_cache(code):
        cached, _ = _load_ticks_cache(code)
        return bool(cached)

    # 沒有本機存檔的先回補（不查就整段都沒有），有存檔、只差一段的排後面：查詢額度不夠時先犧牲後者（它至少有存檔）
    for code in sorted(codes, key=_has_cache):
        try:
            contract = get_contract(api, code)
            if contract is None:
                continue
            with _lock:
                _muted.discard(code)                 # 之前移除過、現在又加回來
            # 回補今日逐筆：本機存檔夠新（20 秒內）就用，否則花 1 次額度查全日；額度用完才用存檔、標出空檔（backfill_today）
            print(f"[shioaji] {code} {backfill_today(api, contract, code)}")
            with _api_lock:
                api.quote.subscribe(contract, quote_type=sj.constant.QuoteType.Tick, version=sj.constant.QuoteVersion.v1)
            try:
                with _api_lock:
                    api.quote.subscribe(contract, quote_type=sj.constant.QuoteType.BidAsk, version=sj.constant.QuoteVersion.v1)
            except Exception as e:
                print(f"[shioaji] {code} 五檔訂閱失敗（改用 MIS 五檔）：{str(e)[:80]}")
            ok.append(code)
        except Exception as e:
            print(f"[shioaji] {code} 訂閱失敗：{e}")
    return ok


def stop_stream(codes):
    """
    盤中移除監控：這幾檔的逐筆、五檔取消訂閱（省永豐流量與這台的處理）。回成功取消的代碼。
    順序：先標成 muted（之後漏進來的不收）→ 存一次本機逐筆（這檔到現在為止的完整逐筆，再加回來時回補用）→ 從記憶體拿掉 → 取消訂閱。
    指數串流與其他股票不受影響。
    """
    api = _api
    with _lock:
        for code in codes:
            _muted.add(code)
    try:
        save_ticks_cache()
    except Exception as e:
        print(f"[shioaji] 逐筆存檔失敗：{str(e)[:80]}")
    with _lock:
        for code in codes:
            _state.pop(code, None)
    if api is None:
        return []
    import shioaji as sj
    ok = []
    for code in codes:
        try:
            contract = get_contract(api, code)
            if contract is None:
                continue
            with _api_lock:
                api.quote.unsubscribe(contract, quote_type=sj.constant.QuoteType.Tick, version=sj.constant.QuoteVersion.v1)
            try:
                with _api_lock:
                    api.quote.unsubscribe(contract, quote_type=sj.constant.QuoteType.BidAsk, version=sj.constant.QuoteVersion.v1)
            except Exception as e:
                print(f"[shioaji] {code} 五檔取消訂閱失敗：{str(e)[:80]}")
            ok.append(code)
        except Exception as e:
            print(f"[shioaji] {code} 取消訂閱失敗：{str(e)[:80]}")
    return ok


_idx = {}   # "t00"/"o00" -> {"last": float, "bars": {"HH:MM": [o,h,l,c,0,0]}}
_IDX_CONTRACTS = {"t00": ("TSE", "IX0001"), "o00": ("OTC", "IX0043")}   # 加權、櫃買


def _idx_contract(api, key):
    ex, code = _IDX_CONTRACTS[key]
    try:
        with _api_lock:
            return getattr(api.Contracts.Indexs, ex)[code]
    except Exception:
        return None


def start_index_stream():
    """訂閱加權/櫃買指數即時值（on_quote_idx_v1），並用一次 kbars 回補今日分 K。回傳成功的 key。"""
    api = login()
    if api is None:
        return []
    import shioaji as sj
    ok = []

    def _on_idx(*args):
        try:
            quote = args[-1]
            code = str(getattr(quote, "code", "") or "")
            key = next((k for k, (_, c) in _IDX_CONTRACTS.items() if c == code), None)
            if key is None:
                return
            close = getattr(quote, "close", None)
            if close is None:
                return
            ts = getattr(quote, "datetime", None) or dt.datetime.now()
            with _lock:
                acc = _idx.setdefault(key, {"last": None, "bars": {}})
                acc["last"] = float(close)
                _bar_update(acc, ts, float(close), 0)
        except Exception:
            pass

    with _api_lock:
        api.on_quote_idx_v1()(_on_idx)

    today = dt.date.today().isoformat()
    for key in _IDX_CONTRACTS:
        contract = _idx_contract(api, key)
        if contract is None:
            continue
        try:
            k = fetch_kbars(api, contract, today, f"指數 {key}")       # 1 次額度，回補開盤到現在
            with _lock:
                acc = _idx.setdefault(key, {"last": None, "bars": {}})
                for ts, c in zip(k.ts, k.Close):
                    t = ns_to_dt(ts)
                    if t.date() == dt.date.today():
                        acc["bars"][t.strftime("%H:%M")] = [float(c)] * 4 + [0, 0]
                        acc["last"] = float(c)
        except Exception as e:
            print(f"[shioaji] 指數 {key} 回補分K失敗：{str(e)[:80]}")
        try:
            with _api_lock:
                api.quote.subscribe(contract, quote_type=sj.QuoteType.Tick, version=sj.QuoteVersion.v1)
            ok.append(key)
        except Exception as e:
            print(f"[shioaji] 指數 {key} 訂閱失敗：{str(e)[:80]}")
    return ok


def series(code):
    """個股分 K：[[HH:MM, o, h, l, c, vol, amt], ...]（給江波圖；amt 可算 VWAP）。"""
    with _lock:
        acc = _state.get(code)
        if not acc:
            return []
        return [[k] + list(v) for k, v in sorted(acc["bars"].items())]


def index_series(key):
    """指數分 K：[[HH:MM, close], ...]。沒訂閱到回 []。"""
    with _lock:
        acc = _idx.get(key)
        if not acc:
            return []
        return [[k, v[3]] for k, v in sorted(acc["bars"].items())]


def last_price(code):
    with _lock:
        acc = _state.get(code)
        return acc["last"] if acc else None


def book(code):
    """五檔 {"bids": [[p,v]×5], "asks": [[p,v]×5], "t": datetime}；沒訂到回 None。"""
    with _lock:
        acc = _state.get(code)
        if not acc or not (acc["bids"] or acc["asks"]):
            return None
        return {"bids": list(acc["bids"]), "asks": list(acc["asks"]), "t": acc["book_t"]}


def stats(code):
    """逐筆累積出的今日開高低收量 {"last","open","high","low","total_volume","t"}；沒資料回 None。"""
    with _lock:
        acc = _state.get(code)
        if not acc or acc["last"] is None:
            return None
        tv = acc["total_volume"] or sum(b[4] for b in acc["bars"].values())
        return {"last": acc["last"], "open": acc["open"] or (acc["bars"][min(acc["bars"])][0] if acc["bars"] else None),
                "high": acc["high"], "low": acc["low"], "total_volume": tv, "t": acc["last_t"]}


def quote_snapshot(code):
    """報價快速通道（monitor.quote_json）用：一次鎖把最新成交、今日高低量、五檔和時間都拿出來；沒有成交過回 None。
    t = 最後一筆成交的交易所時間（epoch）、recv = 本機收到它的時間；回補的逐筆沒有這兩個。"""
    with _lock:
        acc = _state.get(code)
        if not acc or acc["last"] is None:
            return None
        lt = acc.get("last_t")
        return {"last": acc["last"], "open": acc["open"], "high": acc["high"], "low": acc["low"],
                "total_volume": acc["total_volume"] or sum(b[4] for b in acc["bars"].values()),
                "t": lt.timestamp() if isinstance(lt, dt.datetime) else None, "recv": acc.get("recv_t"),
                "bids": list(acc["bids"]), "asks": list(acc["asks"]), "book_recv": acc.get("book_recv_t")}


# 籌碼K線（CMoney）盤中大戶散戶的口徑：單筆金額 ≥ 門檻算大戶、其餘算散戶，只分兩級，門檻依個股歷史動態調整、沒有公布。
# 2026-11-17 事欣科（當天 96～107.5 元）用同一份逐筆對出來：門檻要在 32.25 萬（107.5 元 3 張）到 38.4 萬（96 元 4 張）之間，
# ≥35 萬算出大 9,857 / 散 4,696，它是 9,874 / 4,672（40 萬會把 100 元以下的 4 張單算成散戶，大 9,709 / 散 4,844，比較差）。
# 只有一檔的資料點，門檻是推估；儀表板大戶散戶卡附這個口徑只供對照（回測它的大戶淨額對未來走勢沒有比 300 萬主力好，見 README）。
CHIPK_AMOUNT = float(os.environ.get("CHIPK_LOT_AMOUNT", "350000") or 350000)


def flow_history(code, step_sec=60):
    """
    用回補的逐筆重建「每分鐘的主力/散戶淨額」：
      [[epoch, big_net, small_net, price, cum_vol, ck_big_net, ck_small_net, ck_big_vol, ck_small_vol], ...]
    前 5 欄是主力 / 散戶（lot_thresholds，與 flow() 一致）；後 4 欄是籌碼K線口徑（單筆 ≥ CHIPK_AMOUNT 算大戶、其餘散戶）的累積淨額與累積成交量。
    盤中才打開 monitor 時，model._flow_dynamics 需要的歷史與 09:00 起的警示就靠這份重放，不會因為 09:30 才啟動就看不到 09:05 的主力急出
    （它只用前幾欄，後面加欄位不影響）。規律小單不另外歸類（回補用途夠了）。集合競價那筆內外盤是 0，兩種口徑都不算。
    """
    with _lock:
        acc = _state.get(code)
        if not acc or not acc["ticks"]:
            return []
        ticks = list(acc["ticks"])
    th = lot_thresholds(acc["last"] or ticks[-1][1], [v for _, _, v, _ in ticks])
    big, small = th["big_lots"], th["small_lots"]
    out = []
    bn = sn = vol = 0
    cbn = csn = cbv = csv = 0
    bucket = None
    for ts, p, v, tt in ticks:
        t = ts if isinstance(ts, dt.datetime) else ns_to_dt(ts)
        e = t.timestamp()
        b = int(e // step_sec)
        if bucket is not None and b != bucket:
            out.append([bucket * step_sec + step_sec, bn, sn, last_p, vol, cbn, csn, cbv, csv])
        bucket = b
        vol += v
        if tt in (1, 2):
            sgn = 1 if tt == 1 else -1
            if v >= big:
                bn += sgn * v
            elif v < small or v == 1:
                sn += sgn * v
            if p * v * 1000 >= CHIPK_AMOUNT:
                cbn += sgn * v; cbv += v
            else:
                csn += sgn * v; csv += v
        last_p = p
    if bucket is not None:
        out.append([bucket * step_sec + step_sec, bn, sn, last_p, vol, cbn, csn, cbv, csv])
    return out


def traded_at(code, price, since_ts):
    """自 since_ts（epoch）起在 price 這個價位成交的張數 (總, 外盤買, 內盤賣)。給掛單牆追蹤算「牆被吃了多少」。"""
    with _lock:
        acc = _state.get(code)
        if not acc:
            return (0, 0, 0)
        ticks = acc["ticks"]
        tot = b = s = 0
        for ts, p, v, tt in reversed(ticks):
            t = ts if isinstance(ts, dt.datetime) else ns_to_dt(ts)
            if t.timestamp() < since_ts:
                break
            if abs(p - price) < 1e-6:
                tot += v
                if tt == 1: b += v
                elif tt == 2: s += v
        return (tot, b, s)


def recent_ticks(code, seconds=30):
    """最近 seconds 秒的逐筆 [(epoch, price, vol, tick_type)]（時間由舊到新）與大單門檻（張，跟 flow() 同一套）。
    給「掛單被大單砸穿」撤單警告用；沒資料回 ([], None)。"""
    with _lock:
        acc = _state.get(code)
        if not acc or not acc["ticks"]:
            return [], None
        ticks = acc["ticks"]
        cutoff = time.time() - seconds
        out = []
        for ts, p, v, tt in reversed(ticks):
            e = (ts if isinstance(ts, dt.datetime) else ns_to_dt(ts)).timestamp()
            if e < cutoff:
                break
            out.append((e, p, v, tt))
        if not out:
            return [], None
        sizes = [v for _, _, v, _ in ticks]
        last = acc["last"] or out[0][1]
    return out[::-1], lot_thresholds(last, sizes)["big_lots"]


def index_last(key):
    with _lock:
        acc = _idx.get(key)
        return acc["last"] if acc else None


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
    vol = 0; amt = 0.0; unsided = 0
    # 單筆大小分佈：每一級的買 / 賣張數。大戶的門檻各家不一樣（籌碼K線只分兩級、門檻依個股調整），有了分佈就能換任何門檻來看
    per_lot = max(ref, 0.01) * 1000
    lows = [1]
    for lo in [max(1, math.ceil(a / per_lot)) for a in DIST_AMOUNTS if a < th["small_amount"]] + [small, big]:   # 最上面兩級對齊散戶 / 主力門檻
        if lo > lows[-1]:
            lows.append(lo)
    dist = [{"lo": lo, "hi": (lows[i + 1] - 1 if i + 1 < len(lows) else None), "buy": 0, "sell": 0, "n": 0} for i, lo in enumerate(lows)]
    for ts, p, v, tt in ticks:
        vol += v; amt += p * v
        if tt not in (1, 2):
            unsided += v
            continue
        is_buy = tt == 1
        for d in reversed(dist):
            if v >= d["lo"]:
                d["buy" if is_buy else "sell"] += v; d["n"] += 1
                break
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
    # 逐筆不完整：今天稍早、或重開時存檔到串流之間那段，是用分 K 補的（或補不到）。全日成交量與 VWAP 用分 K 算，
    # 並標出逐筆從幾點開始（head）、中間哪幾段沒有逐筆（gaps），讓畫面講清楚「分流不含這幾段」。window_min 有給（只看最近 N 分鐘）時不處理。
    partial = None
    if not window_min:
        with _lock:
            a2 = _state.get(code) or {}
            kk = set(a2.get("kbar_keys") or ())
            bars_all = {k: (b[4], b[5]) for k, b in (a2.get("bars") or {}).items()}
            gaps = [list(g) for g in (a2.get("tick_gaps") or [])]
        if kk or gaps:
            bv = sum(x[0] for x in bars_all.values()); ba = sum(x[1] for x in bars_all.values())
            t0 = ticks[0][0]
            t0k = t0.strftime("%H:%M") if isinstance(t0, dt.datetime) else None
            partial = {"ticks_from": t0k, "kbar_minutes": len(kk), "day_volume": bv, "ticks_volume": vol,
                       "head": bool(t0k and any(k < t0k for k in kk)),     # 分 K 補的是開頭（逐筆從 ticks_from 才開始）
                       "gaps": [{"from": g[0], "to": g[1],                   # 中間的空檔：那幾分鐘分 K 補到的量（補不到就是 0）
                                 "kbar_vol": sum(bars_all[k][0] for k in kk if g[0][:5] <= k <= g[1][:5])} for g in gaps]}
            if bv > vol and bv > 0:
                amt, vol_day = ba, bv
            else:
                vol_day = vol
        else:
            vol_day = vol
    else:
        vol_day = vol
    big_net = (bb - bs) + (hb - hs)
    return {"big_net": big_net, "small_net": sb - ss, "mid_net": mb - ms, "hidden_net": hb - hs,
            "big_buy": bb + hb, "big_sell": bs + hs, "small_buy": sb, "small_sell": ss,
            "buy_vol": bb + sb + mb + hb, "sell_vol": bs + ss + ms + hs, "volume": vol, "vwap": amt / vol_day, "partial": partial,
            "big_net_pct": big_net / vol * 100, "small_net_pct": (sb - ss) / vol * 100,
            "hidden_net_pct": (hb - hs) / vol * 100,
            "thresholds": th, "iceberg": ice, "window_min": window_min,
            "dist": dist, "unsided_vol": unsided, "auction": {k: list(v) for k, v in (acc.get("auction") or {}).items()},
            "bid_total": bid_total, "ask_total": ask_total, "bid_cnt": bid_cnt, "ask_cnt": ask_cnt, "ticks": len(ticks)}


def logout():
    global _api
    with _api_lock:
        if _api is not None:
            try:
                _api.logout()
            except Exception:
                pass
            _api = None
