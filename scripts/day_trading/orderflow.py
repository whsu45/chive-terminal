#!/usr/bin/env python3
"""
撤單觀察與程式單偵測（2026-11-26 加）。只顯示、記錄，不加分、不擋訊號：還沒驗證這些現象之後股價怎麼走。
每次偵測都寫進 logs/orderflow_<裝置ID>.jsonl，訊號也把當下的狀態記進 signal_outcomes 的 orderflow 欄位，累積夠了再驗證。

一、撤單觀察（五檔：委買五檔 + 委賣五檔共十檔）
  資料：永豐 BidAsk 串流每一次變動（src_shioaji 存在記憶體，也每分鐘 append 到 .cache/book_live/<代碼>_<日期>.jsonl.gz，之後回放、調門檻用）；
        沒有永豐時用 MIS 每 10 秒的五檔快照（比較粗）。
  每次五檔變動，跟上一次比每個價位的張數：
    - 只比「前後都看得到」的價位：價格往反方向走、某個價位被擠到第 6 檔以外時，五檔看不到它不代表撤單（11/26 日月光 508 的誤報）。
    - 張數減少的部分先扣掉同一價位前後 1 秒內的成交（逐筆），剩下的才算撤單；沒有逐筆（MIS）時只算「前後都不是最佳一檔」的價位
      （連續交易只會在最佳一檔成交，其他價位減少一定是撤單）。
  警示：
    a. 大量撤單：同一價位 60 秒內撤掉 ≥ max(30 張, 該價位這段的高點一半)，且那個價位夠大（≥ max(20 張, 五檔中位數 2 倍)）。
       例：508 一分鐘前 182 張、現在 80 張、中間只成交 0 張 → 撤 102 張（56%）。
       回測（11/16–11/26 每分鐘五檔 + 逐筆，43 個股票日）：這個門檻平均每檔每天約 4.5 次（0050 這種有造市商的 ETF 比較多）。
    b. 整數撤單：一次撤掉整數張（50 的倍數，或 ≥100 張的 10 的倍數）且夠大：像一張大單直接抽掉。
    c. 固定張數反覆撤單 / 掛單：20 分鐘內同一邊反覆出現同樣張數的撤單（或掛單）；同樣張數又掛又撤 = 反覆掛撤。
       「同樣張數」要比鄰近張數明顯多（Poisson 檢定），10、20、50 這種整數本來就常見，要更多次才算。

二、程式單偵測（逐筆成交）
  a. 定時定量：同一邊、同樣張數的成交，時間間隔固定（例如每 30 秒一筆 15 張），連續 ≥5 筆，而且用隨機成交湊出這種規律的機率很低。
  b. 固定張數：同一個張數的成交 20 分鐘內比平常（同一檔前幾天 + 今天的張數分布、跟鄰近張數比）多很多，例如 27 張 9 筆。
  c. 大單拆單：499 張（台股單筆委託上限）20 分鐘內 ≥3 筆：大單被拆成上限一筆一筆送。
  d. 固定張數連發：2 秒內同一邊 ≥4 筆同樣張數（≥10 張）：固定張數的掛單被一口氣吃掉，或同一人連續送出（分不出來）。
  回測（.cache/ticks_live 11/16–11/26，65 個股票日）見 README。

    python3 orderflow.py 3711 2026-11-26     # 用本機存的逐筆（和五檔，如果有存）重播當天的偵測
"""
import bisect
import collections
import datetime as dt
import glob
import gzip
import json
import math
import os
import statistics
import sys

SESSION = (9 * 3600, 13 * 3600 + 25 * 60)       # 只看連續交易時段（試撮、收盤集合競價的五檔沒有成交、撤單很多，不看）
SIDE_WORD = {"b": "委買", "a": "委賣"}
TICK_WORD = {1: "外盤買進", 2: "內盤賣出", 0: "買賣兩邊"}


def _sod(ts):
    t = dt.datetime.fromtimestamp(ts)
    return t.hour * 3600 + t.minute * 60 + t.second


def _hms(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def _ptail(k, lam):
    """Poisson P(X ≥ k)。"""
    if k <= 0:
        return 1.0
    if lam <= 0:
        return 0.0
    s, term = 0.0, math.exp(-lam)
    for i in range(k):
        s += term
        term *= lam / (i + 1)
    return max(0.0, 1.0 - s)


def _is_round(v):
    return v % 5 == 0


def _neighbor_rate(counts, v, total, round_boost=5.0):
    """張數 v 的「平常」機率：鄰近張數（±1、±2）的平均，整數張再乘 round_boost（整數本來就比較多人用）。"""
    if total <= 0:
        return None
    nb = [counts.get(v + j, 0) for j in (-2, -1, 1, 2) if v + j > 0]
    p = max(0.5, statistics.mean(nb) if nb else 0.5) / total
    return min(1.0, p * (round_boost if _is_round(v) else 1.0))


# ====================================================================== 撤單觀察
class BookWatch:
    WIN = 60            # 大量撤單：看最近幾秒
    SHARE = 0.5         # 撤掉該價位這段高點的幾成
    MIN_ABS = 30        # 至少撤幾張
    SIG_MIN = 20        # 價位至少幾張才看（另外要 ≥ 五檔中位數 2 倍）
    LAG = 2.0           # 五檔減少後等幾秒再結算（等逐筆進來扣成交）
    MATCH = 1.0         # 成交與五檔變化的時間差容許（秒）
    FIX_WIN = 1200      # 固定張數看最近 20 分鐘
    FIX_MIN = 5         # 固定張數至少幾張才看
    COOL = 180          # 同一價位大量撤單的冷卻（秒）

    def __init__(self, has_ticks=True):
        self.has_ticks = has_ticks
        self.prev = None            # {"b": {px: v}, "a": {...}, "far_b", "far_a", "best_b", "best_a", "n_b", "n_a"}
        self.prev_ts = 0.0
        self.pend = collections.deque()      # 還沒結算的減少 (ts, 上一份五檔的時間, side, px, lots, before, after, 價格可能碰過)
        self.trades = collections.deque()    # [ts, px, 剩下可扣的張數]（最近 300 秒）
        self.cancels = collections.deque()   # 結算後的撤單 (ts, side, px, lots, before, after)
        self.adds = collections.deque()      # 掛單增加 (ts, side, px, lots)
        self.sizes = collections.Counter()   # 今天所有撤單 / 掛單事件的張數分布（固定張數的基準）
        self.n_sizes = 0
        self.level_hi = {}          # (side, px) -> [(ts, 張數)]：60 秒內看過的張數（算高點）
        self.cool = {}
        self.last_ts = 0.0
        self.recent = collections.deque(maxlen=12)   # 儀表板：最近的大量撤單

    @staticmethod
    def _norm(levels):
        return {float(p): int(v) for p, v in (levels or []) if p and v is not None}

    def _snap(self, bids, asks):
        b, a = self._norm(bids), self._norm(asks)
        return {"b": b, "a": a, "n_b": len(b), "n_a": len(a),
                "far_b": min(b) if len(b) >= 5 else None, "far_a": max(a) if len(a) >= 5 else None,
                "best_b": max(b) if b else None, "best_a": min(a) if a else None}

    @staticmethod
    def _visible(s, side, px):
        """這個價位在這份五檔裡「看得到」：在最遠一檔以內（五檔沒滿就全部看得到）。比最佳價更靠內的價位 = 0 張，也算看得到。"""
        far = s["far_" + side]
        if far is None:
            return True
        return px >= far if side == "b" else px <= far

    def feed_book(self, ts, bids, asks):
        if ts < self.last_ts:          # 同一毫秒的多次變動照樣比（時間倒退的才丟）
            return
        self.last_ts = ts
        s = self._snap(bids, asks)
        if not (s["b"] or s["a"]):
            return
        p, t_prev = self.prev, self.prev_ts
        self.prev, self.prev_ts = s, ts
        sod = _sod(ts)
        if p is None or not (SESSION[0] <= sod < SESSION[1]):
            return
        for side in ("b", "a"):
            for px in set(p[side]) | set(s[side]):
                if not (self._visible(p, side, px) and self._visible(s, side, px)):
                    continue
                v0, v1 = p[side].get(px, 0), s[side].get(px, 0)
                if v0:
                    self.level_hi.setdefault((side, px), []).append((ts, v0))
                if v1 == v0:
                    continue
                if v1 > v0:
                    self.adds.append((ts, side, px, v1 - v0))
                    self.sizes[v1 - v0] += 1
                    self.n_sizes += 1
                else:
                    # 價格可能碰過這個價位（前後任一份的最佳價到了或越過它）：沒有逐筆時分不出成交或撤單
                    bests = [x for x in (p["best_" + side], s["best_" + side]) if x is not None]
                    touched = bool(bests) and (px <= max(bests) if side == "a" else px >= min(bests))
                    self.pend.append((ts, t_prev, side, px, v0 - v1, v0, v1, touched))

    def feed_trade(self, ts, px, vol):
        self.trades.append([ts, float(px), int(vol)])

    def _settle(self, now_ts):
        while self.trades and self.trades[0][0] < now_ts - 300:
            self.trades.popleft()
        out = []
        while self.pend and self.pend[0][0] <= now_ts - self.LAG:
            ts, t_prev, side, px, lots, v0, v1, touched = self.pend.popleft()
            if self.has_ticks:
                # 扣掉「上一份五檔到這一份」之間（前後各放寬 MATCH 秒）在這個價位的成交：串流每次變動間隔很短，MIS 快照間隔 10 秒
                need = lots
                for tr in self.trades:
                    if tr[0] > ts + self.MATCH:
                        break
                    if tr[0] >= min(ts, t_prev or ts) - self.MATCH and abs(tr[1] - px) < 1e-6 and tr[2] > 0:
                        use = min(need, tr[2])
                        tr[2] -= use
                        need -= use
                        if need <= 0:
                            break
                c = need
            else:
                c = 0 if touched else lots           # 沒有逐筆：價格碰過的價位分不出成交或撤單，不算
            if c > 0:
                self.cancels.append((ts, side, px, c, v0, v1, t_prev or ts))
                self.sizes[c] += 1
                self.n_sizes += 1
                out.append((ts, side, px, c, v0, v1))
        return out

    def _sig_level(self):
        s = self.prev or {}
        vals = list((s.get("b") or {}).values()) + list((s.get("a") or {}).values())
        return max(self.SIG_MIN, 2 * statistics.median(vals)) if vals else self.SIG_MIN

    def _traded_at(self, px, t0, t1):
        return sum(v for ts, p, v in self._trade_log if t0 <= ts <= t1 and abs(p - px) < 1e-6)

    def check(self, now_ts, trade_log=None):
        """結算到 now_ts - LAG 為止的變化，回新的偵測 [dict]。trade_log：最近的逐筆 [(ts, px, vol)]（算這段在該價位成交幾張）。"""
        self._trade_log = trade_log or []
        new = self._settle(now_ts)
        alerts = []
        sig = self._sig_level()
        lim = now_ts - self.WIN - self.LAG
        while self.cancels and self.cancels[0][0] < now_ts - self.FIX_WIN:
            self.cancels.popleft()
        while self.adds and self.adds[0][0] < now_ts - self.FIX_WIN:
            self.adds.popleft()
        for k in list(self.level_hi):
            self.level_hi[k] = [x for x in self.level_hi[k] if x[0] >= lim]
            if not self.level_hi[k]:
                del self.level_hi[k]
        # a. 大量撤單（同一價位 60 秒內）
        touched = {(side, px) for ts, side, px, *_ in new}
        for side, px in touched:
            evs = [e for e in self.cancels if e[1] == side and e[2] == px and e[0] >= lim]
            C = sum(e[3] for e in evs)
            hi = max([v for _, v in self.level_hi.get((side, px), [])] + [e[4] for e in evs] or [0])
            if hi < sig or C < max(self.MIN_ABS, sig) or C < self.SHARE * hi:
                continue
            key = ("cancel", side, px)
            if now_ts - self.cool.get(key, 0) < self.COOL:
                continue
            self.cool[key] = now_ts
            cur = (self.prev or {}).get(side, {}).get(px, 0)
            traded = self._traded_at(px, min(e[6] for e in evs) - self.MATCH, now_ts) if self.has_ticks else None
            big1 = max(e[3] for e in evs)
            rnd = big1 if (big1 >= 50 and (big1 % 50 == 0 or (big1 >= 100 and big1 % 10 == 0))) else None
            secs = int(evs[-1][0] - evs[0][0])
            when = "一次" if len(evs) == 1 else "一分鐘內" if secs >= 45 else f"{max(1, secs)} 秒內"
            txt = (f"⚠ 撤單觀察：{SIDE_WORD[side]} {px:g} {when}撤掉 {C} 張（{hi} → {cur}"
                   + (f"，這段只成交 {traded} 張" if traded is not None else "") + f"；撤 {C / hi:.0%}）"
                   + (f"，其中一次就撤整 {rnd} 張" if rnd else "")
                   + ("：下方支撐變薄" if side == "b" else "：上方壓力變輕"))
            a = {"kind": "cancel", "side": side, "px": px, "lots": C, "before": hi, "after": cur, "traded": traded,
                 "share": round(C / hi, 2), "round": rnd, "t": _hms(now_ts), "text": txt}
            alerts.append(a)
            self.recent.append(a)
        # b. 整數撤單（一次撤掉整數張、而且夠大；已經算在大量撤單裡的不重複報）
        for ts, side, px, c, v0, v1 in new:
            if c >= max(50, sig) and (c % 50 == 0 or (c >= 100 and c % 10 == 0)):
                key = ("round", side, px)
                if now_ts - self.cool.get(key, 0) < self.COOL or now_ts - self.cool.get(("cancel", side, px), 0) < 5:
                    continue
                self.cool[key] = now_ts
                txt = f"⚠ 整數撤單：{SIDE_WORD[side]} {px:g} 一次撤掉整 {c} 張（{v0} → {v1}），像一張大單直接抽掉"
                a = {"kind": "round", "side": side, "px": px, "lots": c, "before": v0, "after": v1, "t": _hms(ts), "text": txt}
                alerts.append(a)
                self.recent.append(a)
        # c. 固定張數反覆撤單 / 掛單 / 掛撤
        alerts += self._fixed(now_ts)
        return alerts

    def _fixed(self, now_ts):
        out = []
        total = self.n_sizes
        if total < 200:
            return out
        for side in ("b", "a"):
            cs = collections.defaultdict(list)
            for ts, sd, px, c, *_ in self.cancels:
                if sd == side and c >= self.FIX_MIN:
                    cs[c].append((ts, px))
            ad = collections.defaultdict(list)
            for ts, sd, px, c in self.adds:
                if sd == side and c >= self.FIX_MIN:
                    ad[c].append((ts, px))
            n_win = sum(1 for e in self.cancels if e[1] == side) + sum(1 for e in self.adds if e[1] == side)
            for kind, dd in (("fix_cancel", cs), ("fix_add", ad)):
                for v, evs in dd.items():
                    k = len(evs)
                    if k < (5 if _is_round(v) else 3):
                        continue
                    p = _neighbor_rate(self.sizes, v, total)
                    lam = n_win * p
                    if _ptail(k, lam) >= 1e-3:
                        continue
                    both = kind == "fix_cancel" and len(ad.get(v, [])) >= 3
                    key = ("fixboth" if both else kind, side, v)
                    seen = self.cool.get(key)
                    self.cool[key] = now_ts                 # 同一個型態持續成立只報一次；停超過 20 分鐘再出現才重新報
                    if seen is not None and now_ts - seen <= self.FIX_WIN:
                        continue
                    pxs = "、".join(f"{x:g}" for x in sorted({px for _, px in evs})[:4])
                    word = "反覆掛撤" if both else ("反覆撤" if kind == "fix_cancel" else "反覆掛")
                    txt = (f"⚠ 固定張數{'掛撤' if both else '撤單' if kind == 'fix_cancel' else '掛單'}：{SIDE_WORD[side]} 20 分鐘內{word} {v} 張 {k} 次"
                           f"（{pxs}；平常約 {lam:.1f} 次），像同一個人或程式在下")
                    out.append({"kind": "fixboth" if both else kind, "side": side, "v": v, "n": k, "exp": round(lam, 2),
                                "t0": _hms(evs[0][0]), "t1": _hms(evs[-1][0]), "t": _hms(now_ts), "text": txt})
        return out


# ====================================================================== 程式單偵測（逐筆）
class ProgramWatch:
    WIN = 1200          # 看最近 20 分鐘
    FIX_K = 6           # 固定張數至少幾筆
    FIX_P = 1e-5        # 固定張數的 Poisson 門檻（張數很多種，門檻要嚴）
    FIX_MIN = 5
    PER_MIN_N = 5       # 定時至少連續幾筆
    PER_RANGE = (10, 600)
    PER_MAX_EVENTS = 200
    BURST_N, BURST_V, BURST_S = 4, 10, 2.0
    SLICE_V, SLICE_K = 499, 3
    def __init__(self, hist=None):
        self.hist = collections.Counter(hist or {})       # 前幾天這一檔的成交張數分布（事件數）
        self.n_hist = sum(self.hist.values())
        self.today = collections.Counter()
        self.ev = collections.deque()       # 事件 (ts, v, tt)：同一時間（50 毫秒內）同張數同邊的連續成交算一個事件
        self.burst = []                     # 正在累積的連發 [ts0, v, tt, n, px]
        self.last = None
        self.cool = {}
        self.active = {}                    # 儀表板：目前成立的型態 key -> dict
        self._pending_burst = []

    def feed(self, ts, px, vol, tt):
        if tt not in (1, 2) or vol <= 0:
            return
        if not (SESSION[0] <= _sod(ts) < SESSION[1]):
            return
        L = self.last
        if L and L[1] == vol and L[2] == tt and ts - L[0] <= 0.05:
            L[3] += 1
            L[0] = ts
        else:
            self.last = [ts, vol, tt, 1]
            self.ev.append((ts, vol, tt))
            self.today[vol] += 1
        # 連發：2 秒內同邊同張數
        B = self.burst
        if B and B[1] == vol and B[2] == tt and ts - B[0] <= self.BURST_S:
            B[3] += 1
            B[5] = ts
        else:
            if B and B[3] >= self.BURST_N and self.BURST_V <= B[1] < self.SLICE_V:
                self._pending_burst.append(tuple(B))
            self.burst = [ts, vol, tt, 1, px, ts]

    def _episode(self, key, now_ts, a):
        """同一個型態（同張數、同邊）持續成立時只在開始報一次，之後只更新筆數；停超過 WIN（20 分鐘）再出現才重新報。回 True = 要報。"""
        cur = self.active.get(key)
        if cur and now_ts - cur["_seen"] <= self.WIN:
            keep = {k: cur[k] for k in ("first", "t0") if k in cur}
            cur.clear()
            cur.update(a, **keep, _seen=now_ts)
            cur["t0"] = min(cur.get("t0") or a.get("t0") or a["t"], a.get("t0") or a["t"])
            return False
        a.update(first=a["t"])
        self.active[key] = dict(a, _seen=now_ts)        # 存一份複本：之後更新筆數不會改到已經報出去的那一則
        return True

    def _rate(self, v):
        c = self.hist + self.today
        n = self.n_hist + sum(self.today.values())
        if _is_round(v):
            if self.n_hist < 2000:
                return None                 # 沒有前幾天的資料就不判斷整數張（整數本來就常見）
            return min(1.0, 2.0 * max(0.5, self.hist.get(v, 0)) / self.n_hist)
        return _neighbor_rate(c, v, n)

    @staticmethod
    def _chain(times, pmin, pmax):
        """最長的等間隔序列 → (週期, [時間], 檢查過的配對數, 容許誤差)；沒有回 None。"""
        n = len(times)
        best, pairs = None, 0
        for i in range(n):
            for j in range(i + 1, n):
                P = times[j] - times[i]
                if P < pmin:
                    continue
                if P > pmax:
                    break
                pairs += 1
                tol = max(1.0, 0.1 * P)
                chain = [times[i], times[j]]
                last = times[j]
                while True:
                    tgt = last + P
                    k = bisect.bisect_left(times, tgt - tol)
                    if k < n and times[k] <= tgt + tol:
                        last = times[k]
                        chain.append(last)
                    else:
                        break
                if best is None or len(chain) > len(best[1]):
                    best = (P, chain, tol)
        return best and (best[0], best[1], pairs, best[2])

    def check(self, now_ts):
        out = []
        while self.ev and self.ev[0][0] < now_ts - self.WIN:
            self.ev.popleft()
        B = self.burst
        if B and now_ts - B[5] > self.BURST_S and B[3] >= self.BURST_N and self.BURST_V <= B[1] < self.SLICE_V:
            self._pending_burst.append(tuple(B))
            self.burst = []
        # d. 固定張數連發
        for ts0, v, tt, n, px, ts1 in self._pending_burst:
            key = ("burst", v, tt)
            cur = self.active.get(key)
            if cur and now_ts - cur["_seen"] <= 600:
                cur.update(times=cur.get("times", 1) + 1, n_total=cur.get("n_total", cur["n"]) + n, t=_hms(ts0), _seen=now_ts)
                continue
            txt = (f"⚠ 固定張數連發：{_hms(ts0)} {TICK_WORD[tt]}同時 {n} 筆 {v} 張（共 {n * v:,} 張，{px:g}），"
                   "可能是固定張數的掛單被一口氣吃掉，或同一人連續送出")
            a = {"kind": "burst", "tt": tt, "v": v, "n": n, "px": px, "t": _hms(ts0), "t0": _hms(ts0), "text": txt,
                 "times": 1, "n_total": n, "first": _hms(ts0)}
            out.append(a)
            self.active[key] = dict(a, _seen=now_ts)
        self._pending_burst = []
        by = collections.defaultdict(list)
        for ts, v, tt in self.ev:
            by[v].append((ts, tt))
        n_win = len(self.ev)
        for v, evs in by.items():
            k = len(evs)
            sides = collections.Counter(tt for _, tt in evs)
            dom, cnt = sides.most_common(1)[0]
            tt = dom if cnt / k >= 0.75 else 0
            # c. 大單拆單（499 張）
            if v == self.SLICE_V and k >= self.SLICE_K:
                txt = f"⚠ 大單拆單：{TICK_WORD[tt]} 499 張（單筆委託上限）20 分鐘內 {k} 筆、共 {k * 499:,} 張：有人用上限一筆一筆送大單"
                a = {"kind": "slice", "tt": tt, "v": v, "n": k, "lots": k * v, "t0": _hms(evs[0][0]), "t1": _hms(evs[-1][0]), "t": _hms(now_ts), "text": txt}
                if self._episode(("slice", v), now_ts, a):
                    out.append(a)
                continue
            # b. 固定張數
            if v >= self.FIX_MIN and k >= self.FIX_K:
                p = self._rate(v)
                if p is not None:
                    lam = n_win * p
                    if _ptail(k, lam) < self.FIX_P:
                        txt = (f"⚠ 固定張數成交：{TICK_WORD[tt]} {v} 張 20 分鐘內 {k} 筆（{_hms(evs[0][0])[:5]}–{_hms(evs[-1][0])[:5]}，"
                               f"平常約 {lam:.1f} 筆），像同一個人或程式在下")
                        a = {"kind": "fixed", "tt": tt, "v": v, "n": k, "exp": round(lam, 2), "lots": k * v,
                             "t0": _hms(evs[0][0]), "t1": _hms(evs[-1][0]), "t": _hms(now_ts), "text": txt}
                        if self._episode(("fixed", v), now_ts, a):
                            out.append(a)
            # a. 定時定量（每一邊分開找）
            if v < 2:
                continue
            for side in (1, 2):
                ts_ = [t for t, x in evs if x == side]
                if not (self.PER_MIN_N <= len(ts_) <= self.PER_MAX_EVENTS):
                    continue
                r = self._chain(ts_, *self.PER_RANGE)
                if not r:
                    continue
                P, chain, pairs, tol = r
                L = len(chain)
                if L < self.PER_MIN_N or now_ts - chain[-1] > max(3 * P, 120):
                    continue
                # 隨機成交剛好落在預期時間 ±tol 內的機率：用這串規律成交期間（前後各放一個週期）的實際密度算，開盤那種密集時段才不會被低估
                lo_, hi_ = chain[0] - P, chain[-1] + P
                dens = sum(1 for x in ts_ if lo_ <= x <= hi_) / max(60.0, hi_ - lo_)
                q = 1 - math.exp(-dens * 2 * tol)
                if pairs * q ** (L - 2) >= 0.01:          # 隨機成交湊出這麼長的規律序列，期望次數要 < 0.01
                    continue
                txt = (f"⚠ 疑似程式單：{TICK_WORD[side]}固定 {v} 張、約每 {P:.0f} 秒一筆（{_hms(chain[0])[:5]} 起連續 {L} 筆、共 {L * v:,} 張）")
                a = {"kind": "periodic", "tt": side, "v": v, "n": L, "period": round(P, 1), "lots": L * v,
                     "t0": _hms(chain[0]), "t1": _hms(chain[-1]), "t": _hms(now_ts), "text": txt}
                if self._episode(("periodic", v, side), now_ts, a):
                    out.append(a)
        for key in [k for k, a in self.active.items() if now_ts - a.get("_seen", 0) > self.WIN]:
            del self.active[key]
        return out


def load_tick_hist(code, before_day, cache_dir, days=10):
    """前幾天本機存的逐筆（.cache/ticks_live）→ 成交張數分布（同一時間同張數同邊的連續成交算一個事件）。"""
    files = sorted(f for f in glob.glob(os.path.join(cache_dir, "ticks_live", f"{code}_*.json"))
                   if os.path.basename(f)[len(code) + 1:len(code) + 11] < before_day)[-days:]
    c = collections.Counter()
    for f in files:
        try:
            t = json.load(open(f))["ticks"]
        except Exception:
            continue
        last = None
        for s, p, v, tt in t:
            if tt not in (1, 2) or not (SESSION[0] <= s < SESSION[1]):
                continue
            if last and last[1] == v and last[2] == tt and s - last[0] <= 0.05:
                last = (s, v, tt)
                continue
            last = (s, v, tt)
            c[v] += 1
    return c


# ====================================================================== monitor 用的外殼
class OrderFlow:
    """每檔一組 BookWatch + ProgramWatch；monitor 快迴圈每 3 秒餵新的五檔變動與逐筆，回新的偵測與儀表板狀態。"""
    RATE = (6, 600)      # 同一檔 10 分鐘內最多進幾則到警示清單（其餘照樣記錄、顯示在五檔卡）

    def __init__(self, cache_dir, log_path=None):
        self.cache_dir = cache_dir
        self.log_path = log_path
        self.st = {}
        self.sent = collections.defaultdict(collections.deque)

    def _get(self, code, has_ticks, day):
        s = self.st.get(code)
        if s is None or s["day"] != day:
            s = {"day": day, "book": BookWatch(has_ticks), "prog": ProgramWatch(load_tick_hist(code, day, self.cache_dir)),
                 "tick_i": 0, "book_ts": 0.0, "trades": collections.deque(), "last_prog": 0.0, "log": collections.deque(maxlen=40)}
            self.st[code] = s
        return s

    def reset(self, code):
        self.st.pop(code, None)

    def tick_index(self, code):
        s = self.st.get(code)
        return s["tick_i"] if s else 0

    def book_since(self, code):
        s = self.st.get(code)
        return s["book_ts"] if s else 0.0

    def update(self, code, now, book_events, ticks, tick_i=None, has_ticks=True, price=None):
        """book_events: [(ts, bids, asks)]（時間由舊到新）；ticks: [(ts, px, vol, tt)]（新的）；tick_i：餵完後逐筆的位置。
        回 (要進警示清單的 [文字], 全部新偵測 [dict], 儀表板狀態)。"""
        day = now.date().isoformat()
        s = self._get(code, has_ticks, day)
        bw, pw = s["book"], s["prog"]
        bw.has_ticks = has_ticks
        for ts, px, vol, tt in ticks:
            bw.feed_trade(ts, px, vol)
            pw.feed(ts, px, vol, tt)
            s["trades"].append((ts, float(px), int(vol)))
        if tick_i is not None:
            s["tick_i"] = tick_i
        for ts, bids, asks in book_events:
            bw.feed_book(ts, bids, asks)
            s["book_ts"] = max(s["book_ts"], ts)
        now_ts = now.timestamp()
        while s["trades"] and s["trades"][0][0] < now_ts - 180:
            s["trades"].popleft()
        new = bw.check(now_ts, s["trades"])
        if now_ts - s["last_prog"] >= 15:
            s["last_prog"] = now_ts
            new += pw.check(now_ts)
        q = self.sent[code]
        while q and q[0] < now_ts - self.RATE[1]:
            q.popleft()
        texts = []
        for a in new:
            a.update(code=code, date=day, time=now.strftime("%H:%M:%S"), price=price)
            s["log"].append(a)
            if len(q) < self.RATE[0]:
                q.append(now_ts)
                texts.append(a["text"])
            self._write(a)
        return texts, new, self.view(code)

    def _write(self, a):
        if not self.log_path:
            return
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({k: v for k, v in a.items() if k != "text"} | {"text": a["text"]}, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def view(self, code):
        s = self.st.get(code)
        if not s:
            return None
        prog = [{k: v for k, v in a.items() if not k.startswith("_")} for a in s["prog"].active.values()]
        return {"cancels": list(s["book"].recent)[-8:], "prog": prog[-8:],
                "log": list(s["log"])[-12:], "has_ticks": s["book"].has_ticks, "note": "只顯示、未驗證"}

    def summary(self, code, now, minutes=10):
        """訊號當下的狀態（記進 signal_outcomes 的 orderflow 欄位）：最近 minutes 分鐘的撤單觀察、20 分鐘內成立的程式單型態。"""
        s = self.st.get(code)
        if not s:
            return None
        lim = (now - dt.timedelta(minutes=minutes)).strftime("%H:%M:%S")
        keep = ("kind", "side", "px", "lots", "share", "v", "n", "tt", "period", "t")
        return {"book": [{k: a.get(k) for k in keep if a.get(k) is not None} for a in s["book"].recent if a.get("t", "") >= lim],
                "prog": [{k: a.get(k) for k in keep if a.get(k) is not None} for a in s["prog"].active.values()]}


# ====================================================================== 重播
def replay(code, day, cache_dir, quiet=False):
    """用本機存的逐筆（.cache/ticks_live）與五檔（.cache/book_live，有存的話）重播一天，回全部偵測。"""
    tf = os.path.join(cache_dir, "ticks_live", f"{code}_{day}.json")
    base = dt.datetime.combine(dt.date.fromisoformat(day), dt.time()).timestamp()
    ticks = []
    if os.path.exists(tf):
        ticks = [(base + s, p, v, tt) for s, p, v, tt in json.load(open(tf))["ticks"]]
    books = []
    bf = os.path.join(cache_dir, "book_live", f"{code}_{day}.jsonl.gz")
    if os.path.exists(bf):
        with gzip.open(bf, "rt") as f:
            for line in f:
                try:
                    books.append(tuple(json.loads(line)))
                except Exception:
                    pass
    of = OrderFlow(cache_dir)
    out = []
    ti = bi = 0
    t = base + SESSION[0]
    while t <= base + SESSION[1] + 5:
        nt = []
        while ti < len(ticks) and ticks[ti][0] <= t:
            nt.append(ticks[ti]); ti += 1
        nb = []
        while bi < len(books) and books[bi][0] <= t:
            nb.append(books[bi]); bi += 1
        _, new, _ = of.update(code, dt.datetime.fromtimestamp(t), nb, nt, has_ticks=True)
        for a in new:
            out.append(a)
            if not quiet:
                print(a["time"], a["text"])
        t += 3
    return out


if __name__ == "__main__":
    from config import CACHE_DIR
    code = sys.argv[1]
    day = sys.argv[2] if len(sys.argv) > 2 else dt.date.today().isoformat()
    res = replay(code, day, CACHE_DIR)
    print(f"{code} {day}：{len(res)} 則（" + "、".join(f"{k} {v}" for k, v in collections.Counter(a['kind'] for a in res).items()) + "）")
