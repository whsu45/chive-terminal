#!/usr/bin/env python3
"""
開盤策略（2026-11-18 加觀察；2026-11-20 起 A、B 兩組變成建議）：09:05 掃全市場，挑出建議放空的股票，13:25 前回補。

  建議（會叮一聲通知、寫在卡片最上面）：成立的 A、B 候選裡，昨日成交值最大的 5 檔（REC_N）。
    - A 開高 2～7% 放空：任何恐慌程度都建議（使用者 11/20 決定）。
    - B 隔日沖倒貨放空：前一天臺指 VIX <40 才建議；≥40（極度恐慌）只記錄。
    - C 恐慌開低做多：只記錄，不建議。
  建議的單：09:05 叮一聲（進場）；接近漲停（離漲停 1.5% 內）叮咚提醒考慮先回補（鎖死會買不回來，回測不設停損最好，所以只是提醒）；
  13:20 叮咚提醒 13:25 前回補。通知走 monitor 的 notify（不用開網頁）。

全市場、兩組的聯集（每筆用 tags 標它屬於哪一組）：
  共同條件：上市 + 上櫃的 4 位數普通股、可以現股當沖先賣（day_trade = Yes）、不是處置股或暫停交易，
            昨日成交值排在前 40%、09:05 的價格 ≤150 元；09:05 假設放空，13:25 前最後一筆假設回補，不設停損。
  A 開高 2～7%：開盤跳空漲 2%～7%。7% 以上不列，跟 monitor 的急漲過濾一致。
  B 隔日沖倒貨：昨天漲 ≥3%、今天開高 0～9%、開盤價離漲停 ≥3%。
  前 10%：B 組裡開高 ≥3.5% 且離漲停 ≥3% 的（研究裡每筆獲利最高的那一段，每天約 8 檔）。
  C 恐慌開低做多（2026-11-18 加，這一組是做多）：09:05 全市場（同一批可當沖的普通股）開盤跳空中位數 ≤ −2% 的日子，
    開低 ≥2% 的股票 09:05 假設買進、13:25 前最後一筆賣出。網路常見的做多選股條件測下來都賠，只有這個情境站得住：
    2026 年 2 天（6/8、11/6）每筆約 +4%；tw_stocker 2024/03–2026/04 共 15～20 天，每天平均 +0.3～0.5%、賺的天 50～67%（勝率只有五成左右）。

依據（2026/04/10–11/19 全市場日 K，150 元以內、昨日成交值前 40%、開盤空收盤補、2.8 折；「每天平均」是每天算一次再平均）：
  - A 開高 2～7%：前一天 VIX <30 勝率 57%、每筆 +0.50%；30～40 勝率 56%、+0.29%；≥40 勝率 47%、每筆 −0.42%，但每天平均 +0.43%、17 天裡 9 天賺，
    好的一天 +5.1%、差的一天 −4.7%（大反彈日會大賠，例如 11/09 開高的 149 檔平均 −4.7%）。tw_stocker 2024–2026 勝率 56%、每筆 +0.42%，≥40 的 7 天 +0.79%。
  - B 隔日沖倒貨：VIX <30 勝率 63%、每筆 +0.87%（每天平均 +0.76%，區間 +0.31～+1.22）；30～40 勝率 57%、+0.41%；≥40 勝率 50%、−0.28%（不建議）。
舊的說明（11/18 只是觀察時）：
  - A：tw_stocker 5 分 K（120 檔上市股、2024/02–2026/04），150 元以內、開高 2～7%、09:05 空 13:20 補、扣成本：
    2,027 筆、勝率 56%、每筆 +0.42%（以交易日重抽 90% 區間 +0.25～+0.64），測試段（2025/07 起）+0.28%。
    但今年全市場日 K（2026/03–11/16，開盤進收盤出）150 元以內：勝率 52%、每筆 +0.02%，9～11 月 −0.17%、11 月 −0.59%。
  - B：tw_stocker 勝率 60%、每筆 +0.34%（09:05 進 +0.32%）；2026 全市場日 K 勝率 58%、每筆 +0.56%（+0.26～+0.85），11 月也是正的。
    前 10%：勝率 57%、每筆約 +1.0%。但約 26% 收在漲停、平均 −5%，鎖死時可能買不回來。
  先在盤中照實記錄、累積實盤筆數再決定。

資料：永豐 snapshots（一次最多 500 檔）。09:05 掃一次全市場（約 4 次查詢），之後每 60 秒查一次候選（1 次），
  13:24:50 後第一次查詢的成交價當出場價（13:25 起是收盤集合競價，沒有成交）。沒有永豐就不跑。
  昨漲、5 日漲跌、昨天是否漲停用選股的全市場日 K（.cache/screen_daily.json，theme_screener.load_store）：
  09:05 前先補抓缺的那幾天（證交所 / 櫃買公開資料）；補不到、不是前一個交易日的 → 這些欄位留空、B 組今天不列，卡片註明。
紀錄：logs/gapfade_<裝置ID>.jsonl（每檔每天一筆，收盤後跟其他紀錄一起推）；當天進度存 .cache/gapfade_YYYYMMDD.json，
  monitor 盤中重開讀回來接著追蹤。09:07 後才啟動、又沒有當天 09:05 的紀錄 → 今天不記（進場價要 09:05 的，不補）。
限制：最大逆向、碰漲停是每分鐘取樣（加上 snapshots 的今高）判斷的，分鐘內的尖點可能漏掉；出場是 13:25 前最後一筆，不是 13:30 收盤價。
"""
import datetime as dt
import json
import os
import threading

from config import LOG_DIR, CACHE_DIR, dev_path, read_jsonl_devices, one_device
import indicators as I

SCAN_AT, SCAN_LATE = "09:05:00", "09:07:00"      # 09:05 掃描；09:07 之後才第一次跑、又沒有當天紀錄 → 今天不記
EXIT_AT, EXIT_END = "13:24:50", "13:30:00"       # 13:24:50 後第一次查詢的成交價當出場價；13:30 後才查到的是收盤集合競價的價，另外標
POLL = 60                                        # 追蹤中幾秒查一次
PX_MAX = 150.0                                   # 09:05 價格上限（元）
TURNOVER_TOP = 0.40                              # 昨日成交值排前 40%（跟今年全市場日 K 驗證用的同一個門檻）
GAP_MIN, GAP_MAX = 2.0, 7.0                      # A 組：開盤跳空 %（含下限、不含上限）
B_RET1, B_GAP, B_ROOM = 3.0, (0.0, 9.0), 3.0     # B 組：昨漲 ≥3%、開高 0～9%（不含 0）、開盤價離漲停 ≥3%
TOP_GAP, TOP_ROOM = 3.5, 3.0                     # 前 10%：B 組裡開高 ≥3.5% 且離漲停 ≥3%
C_MKT_GAP, C_GAP = -2.0, -2.0                    # C 組（做多）：全市場開盤跳空中位數 ≤ −2% 的日子，開低 ≥2% 的
CHUNK = 500                                      # snapshots 一次最多幾檔（永豐文件）
COST_PCT = 0.235                                 # 預設成本；monitor 會傳 Monitor.COST_PCT 進來
GROUPS = (("rec", "建議的 5 檔"), ("all", "全部"), ("A", "A 開高 2～7%"), ("B", "B 隔日沖倒貨"), ("top10", "前 10%"), ("C", "C 恐慌開低做多"))
REC_N = 5                                        # 每天建議幾檔（成立的 A、B 裡昨日成交值最大的）
NEAR_LIMIT_PCT = 1.5                             # 建議的單離漲停這麼近（%）就叮咚提醒考慮先回補
COVER_REMIND = "13:20:00"                        # 叮咚提醒 13:25 前回補

BASIS = [
    "建議：成立的 A、B 裡昨日成交值最大的 5 檔，09:05 放空、13:25 前回補、不設停損（接近漲停只提醒）。A 任何恐慌程度都建議；B 前一天臺指 VIX <40 才建議；C 只記錄",
    "A 2026 全市場依前一天 VIX：<30 勝率 57%/每筆 +0.50%、30～40 56%/+0.29%、≥40 47%/−0.42%（每天平均 +0.43%、17 天 9 天賺，大反彈日會大賠）",
    "B 2026 全市場依前一天 VIX：<30 勝率 63%/+0.87%、30～40 57%/+0.41%、≥40 50%/−0.28%（≥40 不建議）",
    "舊依據 A：回測 tw_stocker（120 檔上市股、2024/02–2026/04）150 元以內、09:05 空 13:20 補，2,027 筆、勝率 56%、每筆 +0.42%，測試段（2025/07 起）+0.28%；"
    "今年全市場日 K（2026/03–11/16，開盤進收盤出）150 元以內勝率 52%、每筆 +0.02%（看不出優勢），9～11 月 −0.17%、11 月 −0.59%",
    "隔日沖倒貨：昨漲≥3%、今天開高放空，tw_stocker 勝率 60%/每筆 +0.34%、2026 全市場 58%/+0.56%；前 10%（開高≥3.5%、離漲停≥3%）57%/+1.0%，"
    "但約 26% 收漲停、平均 −5%，鎖死時可能買不回來。只記錄，不建議下單",
    "C 恐慌開低做多（做多）：全市場開盤跳空中位數 ≤ −2% 的日子買開低 ≥2% 的，2026 年 2 天每筆約 +4%；tw_stocker 15～20 天每天平均 +0.3～0.5%、賺的天 50～67%，勝率約五成",
]


def side_of(r):
    """C 組是做多，其餘放空（加 side 之前的紀錄都是放空）。"""
    return r.get("side") or ("long" if "C" in (r.get("tags") or []) else "short")


def _ts_str(ts):
    """snapshots 的 ts 是奈秒整數、已經是台灣時間（當 UTC 解讀）→ HH:MM:SS。"""
    try:
        return (dt.datetime(1970, 1, 1) + dt.timedelta(microseconds=int(ts) // 1000)).strftime("%H:%M:%S")
    except Exception:
        return None


def _secs_until(now, hms):
    h, m, s = (int(x) for x in hms.split(":"))
    t = now.replace(hour=h, minute=m, second=s, microsecond=0)
    return (t - now).total_seconds()


def _r(x, n=2):
    return None if x is None else round(x, n)


def _hit_up(r):
    return bool(r.get("hit_up", r.get("hit_limit_up")))


def stats(rows):
    """結案紀錄的勝率、賠率、每筆平均、1 張合計（贏 = 扣成本後真的賺）。rows 已經去重。"""
    rows = [r for r in rows if r.get("net_pct") is not None]
    if not rows:
        return {"n": 0, "days": 0}
    nets = [r["net_pct"] for r in rows]
    w = [x for x in nets if x > 0]
    l = [x for x in nets if x <= 0]
    aw = sum(w) / len(w) if w else None
    al = sum(l) / len(l) if l else None
    pnls = [r["pnl"] for r in rows if r.get("pnl") is not None]
    opens = [r["net_open_pct"] for r in rows if r.get("net_open_pct") is not None]
    return {"n": len(nets), "days": len({r.get("date") for r in rows}),
            "win": len(w), "loss": len(l), "win_rate": round(len(w) / len(nets) * 100, 1),
            "avg_win": _r(aw, 3), "avg_loss": _r(al, 3), "payoff": round(abs(aw / al), 2) if (aw is not None and al) else None,
            "avg": round(sum(nets) / len(nets), 3), "pnl_sum": sum(pnls) if pnls else None,
            "avg_open": round(sum(opens) / len(opens), 3) if opens else None,
            "win_rate_open": round(sum(1 for x in opens if x > 0) / len(opens) * 100, 1) if opens else None,
            "hit_up": sum(1 for r in rows if _hit_up(r)), "close_up": sum(1 for r in rows if r.get("close_up")),
            "first": min(r.get("date") for r in rows), "last": max(r.get("date") for r in rows)}


def in_group(r, g):
    tags = r.get("tags") or ["A"]                 # 加 tags 之前的紀錄只有 A 組
    if g == "rec":
        return bool(r.get("rec"))
    return g == "all" or (g == "top10" and bool(r.get("top10"))) or g in tags


def group_stats(rows):
    """全部 / A / B / 前 10% 各自的統計。"""
    return {g: stats([r for r in rows if in_group(r, g)]) for g, _ in GROUPS}


def load_rows(log_dir=None):
    """各台電腦的 gapfade 紀錄合併；同一天同一檔兩台都有只算一台（交易日才算）。"""
    log_dir = log_dir or LOG_DIR
    try:
        import tw_calendar as cal
        ok = cal.is_trading_date_str
    except Exception:
        ok = lambda s: True
    rows = [r for r in read_jsonl_devices(log_dir, "gapfade") if r.get("date") and r.get("code") and ok(r["date"])]
    rows = one_device(rows, key=lambda r: (r.get("date"), r.get("code")))
    rows.sort(key=lambda r: (r["date"], r["code"]))
    return rows


def _default_daily(d):
    """選股的全市場日 K：先補到前一個交易日（缺幾天抓幾天），回 (store, 日期舊到新, 前一個交易日)。"""
    import theme_screener as TS
    import tw_calendar as cal
    ref = cal.prev_trading_day(d)
    store, dates = TS.load_store(ref, n=7)
    return store, dates, ref


class GapFade:
    """
    api_fn：回永豐 api（沒有就回 None）；is_trading(date)：是不是交易日；now_fn：現在時間（測試可以換）；
    daily_fn(date) → (store, 日期, 前一個交易日)：昨漲 / 5 日漲跌的來源（預設 theme_screener.load_store）。
    主迴圈 run(stop) 在背景執行緒跑；step(now) 做一步、回下次幾秒後再來；view() 給儀表板。
    """

    def __init__(self, api_fn, log_dir=None, cache_dir=None, cost=COST_PCT, is_trading=None, now_fn=None, lock=None, log=print, daily_fn=None,
                 notify=None):
        self.api_fn = api_fn
        self.log_dir = log_dir or LOG_DIR
        self.cache_dir = cache_dir or CACHE_DIR
        self.cost = cost
        self.is_trading = is_trading or (lambda d: d.weekday() < 5)
        self.now_fn = now_fn or dt.datetime.now
        self.lock = lock                     # 永豐 api 的鎖（src_shioaji._api_lock）；測試不用
        self.log = log
        self.daily_fn = daily_fn or _default_daily
        self.notify = notify                 # notify(kind, title, msg)：建議的單要通知（monitor 傳 notify.notify）；測試不傳
        self.st = None
        self.cum = group_stats([])
        self._daily_for = None               # 哪一天準備好的昨漲資料
        self._daily = {}                     # code -> {ret1, ret5, lim_prev}
        self._daily_note = None
        self._mx = threading.Lock()

    # ---------------------------------------------------------------- 狀態檔
    def _state_path(self, d):
        return os.path.join(self.cache_dir, f"gapfade_{d:%Y%m%d}.json")

    def _load(self, d):
        try:
            with open(self._state_path(d), encoding="utf-8") as f:
                st = json.load(f)
            return st if st.get("date") == d.isoformat() else None
        except Exception:
            return None

    def _save(self):
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            d = dt.date.fromisoformat(self.st["date"])
            tmp = self._state_path(d) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.st, f, ensure_ascii=False)
            os.replace(tmp, self._state_path(d))
        except Exception as e:
            self.log(f"[開高放空] 狀態存檔失敗：{str(e)[:80]}")

    def _refresh_cum(self):
        try:
            self.cum = group_stats(load_rows(self.log_dir))
        except Exception as e:
            self.cum = dict(group_stats([]), error=str(e)[:80])

    # ---------------------------------------------------------------- 昨漲、5 日漲跌（全市場日 K）
    def _prep_daily(self, d):
        """準備 d 這天要用的昨漲 / 5 日漲跌 / 昨天是否漲停（只算一次）。"""
        self._daily_for, self._daily, self._daily_note = d, {}, None
        try:
            store, dates, ref = self.daily_fn(d)
        except Exception as e:
            self._daily_note = f"抓全市場日 K 失敗（{e.__class__.__name__}: {str(e)[:60]}）：昨漲、5 日漲跌留空，B 組今天不列"
            self.log(f"[開高放空] {self._daily_note}")
            return
        past = [x for x in dates if x < d.isoformat()]
        if not past or past[-1] != ref.isoformat():
            self._daily_note = (f"全市場日 K（screen_daily.json）最新只到 {past[-1] if past else '（沒有）'}，不是前一個交易日 {ref}："
                                "昨漲、5 日漲跌、昨天是否漲停留空，B 組（隔日沖倒貨）今天不列")
            self.log(f"[開高放空] {self._daily_note}")
            return
        y = store[past[-1]].get("q") or {}
        y2 = (store[past[-2]].get("q") or {}) if len(past) >= 2 else {}
        y6 = (store[past[-6]].get("q") or {}) if len(past) >= 6 else {}
        out = {}
        for code, v in y.items():
            try:
                yc = v[3]
            except (TypeError, IndexError):
                continue
            c2 = (y2.get(code) or [None] * 4)[3]
            c6 = (y6.get(code) or [None] * 4)[3]
            out[code] = {"ret1": _r((yc / c2 - 1) * 100) if (yc and c2) else None,
                         "ret5": _r((yc / c6 - 1) * 100) if (yc and c6) else None,
                         "lim_prev": (yc >= I.limits(c2)[0] - 1e-9) if (yc and c2) else None}
        self._daily = out

    # ---------------------------------------------------------------- 永豐
    def _snapshots(self, api, contracts):
        out = []
        for i in range(0, len(contracts), CHUNK):
            part = contracts[i:i + CHUNK]
            if self.lock is not None:
                with self.lock:
                    out += list(api.snapshots(part))
            else:
                out += list(api.snapshots(part))
        return out

    @staticmethod
    def _universe(api):
        """上市 + 上櫃 4 位數普通股、可以現股當沖先賣、不是處置股或暫停交易。"""
        out = []
        for ex in (api.Contracts.Stocks.TSE, api.Contracts.Stocks.OTC):
            for c in ex:
                code = str(getattr(c, "code", "") or "")
                if len(code) != 4 or not code.isdigit() or code[0] == "0":
                    continue
                if not str(getattr(c, "day_trade", "")).endswith("Yes"):
                    continue
                if getattr(c, "trading_suspended", False):
                    continue
                try:
                    if int(getattr(c, "disposition_level", 0) or 0) > 0:
                        continue
                except (TypeError, ValueError):
                    pass
                out.append(c)
        return out

    # ---------------------------------------------------------------- 一步
    def step(self, now=None):
        now = now or self.now_fn()
        d = now.date()
        hms = now.strftime("%H:%M:%S")
        # 09:05 前先把昨漲資料準備好（要連線補抓，在鎖外面做，不卡儀表板）
        if hms < SCAN_AT and self._daily_for != d and self.is_trading(d):
            self._prep_daily(d)
        with self._mx:
            if self.st is None or self.st.get("date") != d.isoformat():
                self.st = self._load(d) or {"date": d.isoformat(), "status": "waiting", "cands": []}
                self._refresh_cum()
            st = self.st
            if not self.is_trading(d):
                st.update(status="off", note="今天休市")
                return 3600
            if st["status"] in ("closed", "late", "no_shioaji", "error_scan"):
                return 600
            if hms < SCAN_AT:
                st.update(status="waiting", note="09:05 掃描全市場", daily_note=self._daily_note)
                return max(1.0, min(30.0, _secs_until(now, SCAN_AT)))
            if not st.get("scanned"):
                if hms >= SCAN_LATE:
                    st.update(status="late", note=("今天 09:05 時這台 monitor 沒開，沒有開高放空觀察的紀錄（下面是累計）" if hms >= EXIT_AT else
                                                   "monitor 在 09:07 之後才開、又沒有今天 09:05 的紀錄：今天沒有開高放空觀察（進場價要 09:05 的，不補）"))
                    self._save()
                    return 600
                api = self.api_fn()
                if api is None:
                    st.update(status="no_shioaji", note="這台沒有永豐（或用 --no-shioaji 開）：開高放空觀察要用永豐 snapshots 掃全市場")
                    return 600
                try:
                    self._scan(api, now)
                except Exception as e:
                    msg = f"09:05 掃描失敗：{e.__class__.__name__}: {str(e)[:100]}"
                    self.log(f"[開高放空] {msg}")
                    if hms < SCAN_LATE:     # 09:07 前再試（15 秒後）
                        st["note"] = msg + "，15 秒後再試"
                        return 15
                    st.update(status="error_scan", note=msg)
                    self._save()
                    return 600
                self._save()
                if not st["cands"]:
                    return 600
                return POLL
            # 追蹤中
            final = hms >= EXIT_AT
            last = st.get("last_poll_epoch") or 0
            if not final and now.timestamp() - last < POLL - 0.5:
                return max(1.0, min(POLL - (now.timestamp() - last), _secs_until(now, EXIT_AT)))
            api = self.api_fn()
            if api is None:
                st["note"] = "永豐暫時連不上，下一分鐘再試"
                return 30
            try:
                self._poll(api, now, final)
            except Exception as e:
                st["note"] = f"查詢失敗，下一分鐘再試：{str(e)[:80]}"
                self.log(f"[開高放空] {st['note']}")
                return 30
            self._save()
            if final:
                return 600
            return max(1.0, min(POLL, _secs_until(now, EXIT_AT) if hms < EXIT_AT else POLL))

    def _scan(self, api, now):
        st = self.st
        uni = self._universe(api)
        by_code = {c.code: c for c in uni}
        snaps = self._snapshots(api, uni)              # 先拿進場價，再去準備昨漲資料（如果 09:05 前沒準備好）
        if self._daily_for != now.date():
            self._prep_daily(now.date())
        rows = []
        for s in snaps:
            c = by_code.get(getattr(s, "code", None))
            o, px, chgp = getattr(s, "open", 0) or 0, getattr(s, "close", 0) or 0, getattr(s, "change_price", 0) or 0
            if not (c and o and px):
                continue
            prev = round(px - chgp, 2)
            if prev <= 0:
                continue
            yv = float(getattr(s, "yesterday_volume", 0) or 0)
            up = float(getattr(c, "limit_up", 0) or 0) or I.limits(prev)[0]
            dn = float(getattr(c, "limit_down", 0) or 0) or I.limits(prev)[1]
            dy = self._daily.get(c.code) or {}
            rows.append({"code": c.code, "name": getattr(c, "name", ""), "ex": str(getattr(c, "exchange", "")).replace("Exchange.", ""),
                         "prev_close": prev, "open": float(o), "px": float(px), "gap": round((o / prev - 1) * 100, 2),
                         "room": round((up / o - 1) * 100, 2), "val": yv * 1000 * prev,
                         "ret1": dy.get("ret1"), "ret5": dy.get("ret5"), "lim_prev": dy.get("lim_prev"),
                         "hi0": float(getattr(s, "high", px) or px), "lo0": float(getattr(s, "low", px) or px),
                         "ts": _ts_str(getattr(s, "ts", 0)), "limit_up": up, "limit_down": dn})
        # 恐慌指數（fear.py）：前一天臺指 VIX ≥40 極度恐慌時，B 隔日沖倒貨、C 恐慌開低做多不成立（照樣記錄，valid = False）
        try:
            import fear
            fz = fear.state(now)
        except Exception:
            fz = {}
        vix_prev = fz.get("prev")
        extreme = bool(fz.get("extreme"))
        gaps = sorted(r["gap"] for r in rows)
        mkt_gap = round(gaps[len(gaps) // 2], 2) if gaps else None    # 全市場（可當沖普通股）開盤跳空中位數：C 組要恐慌開低才成立
        panic = mkt_gap is not None and mkt_gap <= C_MKT_GAP
        vals = sorted((r["val"] for r in rows if r["val"] > 0), reverse=True)
        cut = vals[max(0, int(len(vals) * TURNOVER_TOP) - 1)] if vals else None
        cands = []
        for r in rows:
            if cut is None or r["val"] < cut or r["px"] > PX_MAX:
                continue
            if panic and r["gap"] <= C_GAP and r["px"] > r["limit_down"] + 1e-9:
                cands.append(self._cand(r, now, ["C"], False, "long"))
                continue
            if r["px"] >= r["limit_up"] - 1e-9:
                continue
            tags = []
            if GAP_MIN <= r["gap"] < GAP_MAX:
                tags.append("A")
            if r["ret1"] is not None and r["ret1"] >= B_RET1 and B_GAP[0] < r["gap"] < B_GAP[1] and r["room"] >= B_ROOM:
                tags.append("B")
            if not tags:
                continue
            top10 = "B" in tags and r["gap"] >= TOP_GAP and r["room"] >= TOP_ROOM
            cands.append(self._cand(r, now, tags, top10, "short"))
        for c in cands:
            c["vix_prev"] = vix_prev
            c["valid"] = not (extreme and ("B" in c["tags"] or "C" in c["tags"]))
            # 建議的候選：A 不看恐慌程度；B 要前一天 VIX <40；C（做多）不建議
            c["active"] = c["side"] == "short" and ("A" in c["tags"] or ("B" in c["tags"] and not extreme))
        # 國安基金護盤期、大盤開高（全市場開盤跳空中位數 ≥ +0.3%）：開盤策略不建議放空（support.py）
        try:
            import support
            sup_blk = support.open_block(now.date(), mkt_gap)
        except Exception:
            sup_blk = None
        st["support_block"] = sup_blk
        if sup_blk:
            for c in cands:
                c["active"] = False
        recs = sorted((c for c in cands if c["active"]), key=lambda c: -(c.get("val") or 0))[:REC_N]
        for k, c in enumerate(recs, 1):
            c["rec"] = True; c["rec_rank"] = k
        cands.sort(key=lambda x: (x["side"] == "long", not x["top10"], -abs(x["gap"])))
        na = sum(1 for c in cands if "A" in c["tags"]); nb = sum(1 for c in cands if "B" in c["tags"]); nt = sum(1 for c in cands if c["top10"])
        nc = sum(1 for c in cands if "C" in c["tags"])
        mg = f"全市場開盤跳空中位數 {mkt_gap:+.2f}%" if mkt_gap is not None else "全市場開盤跳空中位數算不出來"
        st.update(scanned=True, scan_time=now.strftime("%H:%M:%S"), universe=len(uni), quoted=len(rows), cut=cut, cands=cands,
                  last_poll_epoch=now.timestamp(), daily_note=self._daily_note, mkt_gap=mkt_gap, panic=panic,
                  vix_prev=vix_prev, vix_extreme=extreme,
                  counts={"A": na, "B": nb, "top10": nt, "C": nc},
                  status="tracking" if cands else "closed",
                  note=(f"09:05 掃 {len(rows)} 檔，{len(cands)} 檔符合（A {na}、B {nb}、前 10% {nt}、C {nc}），13:25 前最後一筆結算；{mg}"
                        + ("（恐慌開低：C 組做多成立）" if panic else "（沒有恐慌開低，C 組做多不成立）") if cands
                        else f"09:05 掃 {len(rows)} 檔，今天沒有符合的；{mg}"))
        st["rec_n"] = len(recs)
        if recs:
            txt = "、".join(f"{c['code']} {c['name']} @{c['entry']}（開高 {c['gap']:+.1f}%，{'/'.join(t for t in c['tags'] if t in ('A', 'B'))}）" for c in recs)
            st["rec_note"] = f"09:05 建議放空 {len(recs)} 檔：{txt}；13:25 前回補，不設停損（接近漲停會提醒）"
            self._notify("entry", f"開盤策略：建議放空 {len(recs)} 檔", "、".join(f"{c['code']} {c['name']} @{c['entry']}" for c in recs))
        elif sup_blk:
            st["rec_note"] = sup_blk
        else:
            st["rec_note"] = "09:05 沒有成立的開盤策略（A 開高 2～7%、B 隔日沖倒貨" + ("；前一天 VIX ≥40，B 不成立" if extreme else "") + "）：今天不做"
        self.log(f"[開高放空] {st['note']}" + (("：" + "、".join(f"{c['code']} {c['name']} {c['gap']:+.1f}%{'★' if c['top10'] else ''}{'（做多）' if c['side'] == 'long' else ''}" for c in cands[:8])
                                                + ("…" if len(cands) > 8 else "")) if cands else ""))

    def _notify(self, kind, title, msg=""):
        if self.notify:
            try:
                self.notify(kind, title, msg)
            except Exception as e:
                self.log(f"[開盤策略] 通知失敗：{str(e)[:80]}")

    @staticmethod
    def _cand(r, now, tags, top10, side):
        return {"code": r["code"], "name": r["name"], "ex": r["ex"], "tags": tags, "top10": top10, "side": side,
                "prev_close": r["prev_close"], "open": r["open"], "gap": r["gap"], "room": r["room"],
                "ret1": r["ret1"], "ret5": r["ret5"], "lim_prev": r["lim_prev"], "val": round(r["val"]),
                "entry": r["px"], "entry_ts": r["ts"] or now.strftime("%H:%M:%S"),
                "limit_up": r["limit_up"], "limit_down": r.get("limit_down"), "hi0": r["hi0"], "lo0": r["lo0"],
                "last": r["px"], "last_ts": r["ts"], "high_after": r["px"], "low_after": r["px"], "hit_up": False, "hit_dn": False,
                "samples": 1, "status": "tracking"}

    def _poll(self, api, now, final):
        st = self.st
        live = [c for c in st["cands"] if c["status"] == "tracking"]
        if not live:
            st["status"] = "closed"
            return
        cmap = {}
        for c in live:
            try:
                cmap[c["code"]] = api.Contracts.Stocks[c["code"]]
            except Exception:
                pass
        snaps = self._snapshots(api, [x for x in cmap.values() if x is not None])
        hms = now.strftime("%H:%M:%S")
        got = {getattr(s, "code", None): s for s in snaps}
        for c in live:
            s = got.get(c["code"])
            if s is None:
                continue
            px = float(getattr(s, "close", 0) or 0)
            if not px:
                continue
            hi, lo = float(getattr(s, "high", px) or px), float(getattr(s, "low", px) or px)
            c["last"], c["last_ts"] = px, _ts_str(getattr(s, "ts", 0)) or hms
            c["samples"] = c.get("samples", 0) + 1
            # 進場後的最高 / 最低：每分鐘的成交價，加上「今高比 09:05 時更高」（那一定是進場後創的）
            c["high_after"] = max(c["high_after"], px, hi if hi > c["hi0"] else px)
            c["low_after"] = min(c["low_after"], px, lo if lo < c["lo0"] else px)
            if c.get("limit_up") and c["high_after"] >= c["limit_up"] - 1e-9:
                c["hit_up"] = True
            if c.get("limit_down") and c["low_after"] <= c["limit_down"] + 1e-9:
                c["hit_dn"] = True
            if c.get("rec") and not final and not c.get("near_flag") and c.get("limit_up") and px >= c["limit_up"] * (1 - NEAR_LIMIT_PCT / 100) - 1e-9:
                c["near_flag"] = hms
                self._notify("exit", f"開盤策略：{c['code']} {c['name']} 接近漲停 {c['limit_up']}（現價 {px}）",
                             f"放空 {c['entry']}，考慮先回補——漲停鎖死會買不回來（回測不設停損最好，這只是提醒）")
            if final:
                sg = 1 if side_of(c) == "long" else -1
                c["exit"], c["exit_ts"] = px, c["last_ts"]
                c["exit_kind"] = "1325" if hms < EXIT_END else "1330"
                c["close_up"] = bool(c.get("limit_up") and px >= c["limit_up"] - 1e-9)
                c["close_dn"] = bool(c.get("limit_down") and px <= c["limit_down"] + 1e-9)
                c["net_pct"] = round(sg * (px / c["entry"] - 1) * 100 - self.cost, 3)
                c["pnl"] = round(c["net_pct"] / 100 * c["entry"] * 1000)
                c["net_open_pct"] = round(sg * (px / c["open"] - 1) * 100 - self.cost, 3)    # 用開盤價進場（研究裡 09:00 進場）
                c["pnl_open"] = round(c["net_open_pct"] / 100 * c["open"] * 1000)
                c["status"] = "closed"
        st["last_poll_epoch"] = now.timestamp()
        st["last_poll"] = hms
        if not final and hms >= COVER_REMIND and not st.get("cover_reminded"):
            open_rec = [c for c in st["cands"] if c.get("rec") and c["status"] == "tracking"]
            st["cover_reminded"] = hms
            if open_rec:
                def now_net(c):
                    return -(c["last"] / c["entry"] - 1) * 100 - self.cost
                self._notify("exit", f"開盤策略：13:25 前回補 {len(open_rec)} 檔",
                             "、".join(f"{c['code']} {c['name']} 現價 {c['last']}（{now_net(c):+.1f}%）" for c in open_rec))
        if final:
            for c in st["cands"]:
                if c["status"] == "tracking":       # 這次沒查到（停牌、資料缺）：不結算、不寫紀錄
                    c["status"] = "nodata"
            st["status"] = "closed"
            n = sum(1 for c in st["cands"] if c["status"] == "closed")
            st["note"] = f"{hms} 結案 {n} 檔" + ("（13:30 後才查到，用的是收盤集合競價的價）" if hms >= EXIT_END else "")
            self._write_log(now)
            self._refresh_cum()
            day = [dict(c, date=st["date"]) for c in st["cands"] if c.get("net_pct") is not None]
            if day:
                gs = group_stats(day)
                self.log(f"[開高放空] 今天 {gs['all']['n']} 檔：勝 {gs['all']['win']} 輸 {gs['all']['loss']}，每筆 {gs['all']['avg']:+.2f}%，1 張合計 {gs['all']['pnl_sum']:+,} 元"
                         + (f"；前 10% {gs['top10']['n']} 檔每筆 {gs['top10']['avg']:+.2f}%" if gs["top10"]["n"] else "") + "（只是記錄，沒有下單）")
                if gs["rec"]["n"]:
                    self.log(f"[開盤策略] 建議的 {gs['rec']['n']} 檔：勝 {gs['rec']['win']} 輸 {gs['rec']['loss']}，每筆 {gs['rec']['avg']:+.2f}%，1 張合計 {gs['rec']['pnl_sum']:+,} 元")

    def _write_log(self, now):
        st = self.st
        if st.get("logged"):
            return
        recs = []
        for c in st["cands"]:
            if c.get("net_pct") is None:
                continue
            recs.append({"date": st["date"], "code": c["code"], "name": c["name"], "ex": c.get("ex"), "tags": c["tags"], "top10": c["top10"],
                         "side": side_of(c), "mkt_gap": st.get("mkt_gap"), "vix_prev": c.get("vix_prev"), "valid": c.get("valid", True),
                         "active": c.get("active"), "rec": bool(c.get("rec")), "rec_rank": c.get("rec_rank"), "near_flag": c.get("near_flag"),
                         "gap": c["gap"], "prev_close": c["prev_close"], "open": c["open"], "ret1": c.get("ret1"), "ret5": c.get("ret5"),
                         "room": c.get("room"), "lim_prev": c.get("lim_prev"), "val": c.get("val"),
                         "entry": c["entry"], "entry_ts": c["entry_ts"], "exit": c["exit"], "exit_ts": c.get("exit_ts"), "exit_kind": c.get("exit_kind"),
                         "high_after": c["high_after"], "low_after": c["low_after"],
                         "mae_pct": _mae(c), "mfe_pct": _mfe(c),
                         "hit_up": c["hit_up"], "close_up": c.get("close_up"), "hit_dn": c.get("hit_dn"), "close_dn": c.get("close_dn"),
                         "net_pct": c["net_pct"], "pnl": c["pnl"], "net_open_pct": c.get("net_open_pct"), "pnl_open": c.get("pnl_open"),
                         "samples": c.get("samples"), "cost": self.cost, "daily_ok": st.get("daily_note") is None, "src": "shioaji"})
        if recs:
            os.makedirs(self.log_dir, exist_ok=True)
            with open(dev_path(self.log_dir, "gapfade", ".jsonl"), "a", encoding="utf-8") as f:
                for r in recs:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
        st["logged"] = True

    # ---------------------------------------------------------------- 給儀表板
    def view(self):
        with self._mx:
            st = dict(self.st or {})
            cands = []
            for c in st.get("cands") or []:
                x = {k: c.get(k) for k in ("code", "name", "tags", "top10", "gap", "room", "ret1", "ret5", "lim_prev", "val", "prev_close", "open",
                                           "entry", "entry_ts", "last", "last_ts", "status", "hit_up", "close_up", "exit", "exit_ts", "exit_kind",
                                           "net_pct", "pnl", "net_open_pct", "pnl_open", "limit_up", "hit_dn", "close_dn", "valid", "vix_prev",
                                           "active", "rec", "rec_rank", "near_flag")}
                x["side"] = side_of(c)
                e = c.get("entry")
                if e:
                    x["mae_pct"] = _mae(c)
                    if c.get("status") == "tracking" and c.get("last"):      # 追蹤中才算「現在平倉的話」；沒查到報價的不算
                        sg = 1 if x["side"] == "long" else -1
                        x["net_now"] = round(sg * (c["last"] / e - 1) * 100 - self.cost, 3)
                        x["pnl_now"] = round(x["net_now"] / 100 * e * 1000)
                cands.append(x)
            return {"date": st.get("date"), "status": st.get("status"), "note": st.get("note"), "daily_note": st.get("daily_note", self._daily_note),
                    "mkt_gap": st.get("mkt_gap"), "panic": st.get("panic"), "vix_prev": st.get("vix_prev"), "vix_extreme": st.get("vix_extreme"),
                    "rec_n": st.get("rec_n"), "rec_note": st.get("rec_note"), "cover_reminded": st.get("cover_reminded"), "support_block": st.get("support_block"),
                    "scan_time": st.get("scan_time"), "last_poll": st.get("last_poll"), "universe": st.get("universe"), "quoted": st.get("quoted"),
                    "cut": st.get("cut"), "counts": st.get("counts"), "cands": cands, "cum": self.cum, "cost": self.cost, "basis": BASIS,
                    "groups": [list(g) for g in GROUPS], "rule": rule()}

    def run(self, stop, on_update=None):
        """背景執行緒的主迴圈。stop：threading.Event。on_update(view)：每一步之後呼叫（monitor 用來放進 WEB.gap）。"""
        while not stop.is_set():
            try:
                wait = self.step()
            except Exception as e:           # 不讓觀察功能的錯誤影響 monitor
                self.log(f"[開高放空] 錯誤：{e.__class__.__name__}: {str(e)[:100]}")
                wait = 60
            if on_update:
                try:
                    on_update(self.view())
                except Exception:
                    pass
            stop.wait(max(1.0, min(wait, 60.0)))


def _mae(c):
    """最大逆向 %：放空看進場後最高、做多看進場後最低。"""
    e = c["entry"]
    return round(((1 - c["low_after"] / e) if side_of(c) == "long" else (c["high_after"] / e - 1)) * 100, 2)


def _mfe(c):
    e = c["entry"]
    return round(((c["high_after"] / e - 1) if side_of(c) == "long" else (1 - c["low_after"] / e)) * 100, 2)


def rule():
    return {"px_max": PX_MAX, "top": TURNOVER_TOP, "gap_min": GAP_MIN, "gap_max": GAP_MAX, "b_ret1": B_RET1, "b_gap": list(B_GAP),
            "b_room": B_ROOM, "top_gap": TOP_GAP, "top_room": TOP_ROOM, "c_mkt_gap": C_MKT_GAP, "c_gap": C_GAP,
            "rec_n": REC_N, "near_limit": NEAR_LIMIT_PCT, "cover_remind": COVER_REMIND}


if __name__ == "__main__":
    rows = load_rows()
    gs = group_stats(rows)
    if not gs["all"]["n"]:
        print("還沒有開高放空觀察的紀錄（logs/gapfade_*.jsonl）")
    else:
        a = gs["all"]
        print(f"開高放空觀察（{a['first']} ～ {a['last']}，{a['days']} 天；只記錄、沒有下單）")
        for g, name in GROUPS:
            s = gs[g]
            if not s["n"]:
                print(f"  {name:<12} 還沒有紀錄"); continue
            line = f"  {name:<12} {s['n']:>4} 筆 勝率 {s['win_rate']}%（{s['win']} 賺 {s['loss']} 賠） 賠率 {s['payoff']} 每筆 {s['avg']:+.3f}%"
            if s.get("avg_open") is not None:
                line += f"（開盤價進 {s['avg_open']:+.3f}%）"
            print(line + f" 1 張合計 {s['pnl_sum']:+,} 元 碰漲停 {s['hit_up']} 收漲停 {s['close_up']}")
        for r in rows[-20:]:
            print(f"  {r['date']} {r['code']} {r['name']:<6} [{'/'.join(r.get('tags') or ['A'])}{'★' if r.get('top10') else ''}]{'做多' if side_of(r) == 'long' else '放空'} 開盤 {r['gap']:+.1f}%"
                  f" 昨漲 {r['ret1'] if r.get('ret1') is not None else '-'}%  {r['entry']}→{r['exit']}  {r['net_pct']:+.2f}%  {r['pnl']:+,} 元"
                  + ("  碰漲停" if _hit_up(r) else "") + ("  收漲停" if r.get("close_up") else ""))
