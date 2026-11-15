"""
A/B 當沖模組：Model_A（現行價位反轉/回測進場）vs Model_B（噪音區突破 + 量能/內外盤確認 + VWAP 移動停損）。

兩個類別吃同一種 K 棒資料，輸出同一種訊號：
    資料：pandas.DataFrame 或 list[dict]，欄位
        ts（datetime，台北時間，K 棒「開始」時間）、open、high、low、close、volume
        選配 buy_vol / sell_vol（外盤 / 內盤量，逐筆才有；沒有就跳過內外盤濾網）
        可以是多天連在一起：前幾天用來算昨高低、噪音區與相對量，最後一天是要交易 / 監控的那天。
        1 分 K、5 分 K 都可以（自動判斷 K 棒長度）。
    訊號：{"ts", "action": BUY/SELL/HOLD, "intent": open_long/close_long/open_short/close_short/add_long/add_short/None,
           "price", "stop", "target", "size", "reason"}

    live:     Model_B().signal(df)      → 最後一根 K 棒收盤時該做什麼
    backtest: Model_B().run(df).trades  → 每筆部位（進出場、毛報酬 %）
只用標準函式庫；pandas 是選配（有傳 DataFrame 才會用到）。
"""
import math
import datetime as dt
from dataclasses import dataclass, field

# ---------------------------------------------------------------- 台股規則
def tick_size(p, fixed=None):
    if fixed:
        return fixed                       # 期貨：台指期 / 小台一檔 1 點
    if p < 10: return 0.01
    if p < 50: return 0.05
    if p < 100: return 0.1
    if p < 500: return 0.5
    if p < 1000: return 1.0
    return 5.0


def round_tick(p, direction="down", fixed=None):
    t = tick_size(p, fixed)
    q = p / t
    q = math.floor(q + 1e-9) if direction == "down" else math.ceil(q - 1e-9)
    return round(q * t, 2)


def limits(prev_close, fixed=None):
    return round_tick(prev_close * 1.10, "down", fixed), round_tick(prev_close * 0.90, "up", fixed)


@dataclass
class CostModel:
    """
    來回成本（% of 進場價）。
    現股當沖：手續費 0.1425% 兩邊 × 折扣 + 證交稅當沖 0.15%（施行到 2027-12-31）+ 每邊滑價 slip_ticks 檔。
    期貨（point_value 有值）：手續費每口每邊 fee_ntd 元 + 期交稅每邊 0.002% + 每邊滑價 slip_points 點，依進場點位換算 %。
    """
    fee: float = 0.1425
    discount: float = 0.3
    tax: float = 0.15
    slip_ticks: float = 1.0
    slip_pct: float | None = None          # 直接給每邊滑價 %（ETF 用；None = 用股票 tick 表）
    point_value: float | None = None       # 期貨每點價值（小台 50、大台 200、微台 10）
    fee_ntd: float = 0.0
    slip_points: float = 1.0

    def round_trip_pct(self, entry):
        if self.point_value:
            return 2 * self.fee_ntd / (entry * self.point_value) * 100 + 2 * 0.002 + 2 * self.slip_points / entry * 100
        slip = self.slip_pct if self.slip_pct is not None else self.slip_ticks * tick_size(entry) / entry * 100
        return 2 * self.fee * self.discount + self.tax + 2 * slip

    @classmethod
    def taiex_futures(cls, fee_ntd=30.0, point_value=50.0, slip_points=1.0):
        """預設小台（MTX）：每口每邊手續費 30 元、期交稅 0.002%、每邊滑價 1 點。"""
        return cls(point_value=point_value, fee_ntd=fee_ntd, slip_points=slip_points)


# ---------------------------------------------------------------- 資料整理
def _rows(data):
    """DataFrame / list[dict] → 依時間排序、去重、價格對齊 tick 的 list[dict]。"""
    if hasattr(data, "to_dict"):                       # pandas.DataFrame
        df = data.reset_index() if "ts" not in data.columns and getattr(data.index, "name", None) == "ts" else data
        recs = df.to_dict("records")
    else:
        recs = list(data)
    out, seen = [], set()
    for r in recs:
        ts = r["ts"]
        if not isinstance(ts, dt.datetime):
            ts = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else dt.datetime.fromisoformat(str(ts))
        if ts.tzinfo is not None:
            ts = ts.astimezone(dt.timezone(dt.timedelta(hours=8))).replace(tzinfo=None)
        if ts in seen:
            continue
        seen.add(ts)
        o, h, l, c = (float(r[k]) for k in ("open", "high", "low", "close"))
        if not (o > 0 and h > 0 and l > 0 and c > 0):
            continue
        row = {"ts": ts, "open": round(o, 2), "high": round(h, 2), "low": round(l, 2), "close": round(c, 2),
               "volume": float(r.get("volume") or 0)}
        if r.get("buy_vol") is not None and r.get("sell_vol") is not None:
            row["buy_vol"], row["sell_vol"] = float(r["buy_vol"]), float(r["sell_vol"])
        out.append(row)
    out.sort(key=lambda x: x["ts"])
    return out


def _by_day(rows):
    days = {}
    for r in rows:
        days.setdefault(r["ts"].date(), []).append(r)
    return [(d, days[d]) for d in sorted(days)]


def _minute(ts):
    return ts.hour * 60 + ts.minute


def _bar_len(rows):
    gaps = {}
    for a, b in zip(rows, rows[1:]):
        if a["ts"].date() == b["ts"].date():
            g = _minute(b["ts"]) - _minute(a["ts"])
            if g > 0:
                gaps[g] = gaps.get(g, 0) + 1
    return max(gaps, key=gaps.get) if gaps else 1


@dataclass
class Trade:
    model: str
    date: dt.date
    side: str                 # long / short
    entry_ts: dt.datetime
    entry: float
    stop0: float
    target: float | None
    exit_ts: dt.datetime | None = None
    exit: float | None = None
    reason: str = ""
    legs: list = field(default_factory=list)      # [(進場價, 部位大小)]；加碼會多一筆

    def gross_pct(self):
        sgn = 1 if self.side == "long" else -1
        return sum(sz * sgn * (self.exit - px) / px * 100 for px, sz in self.legs)

    def net_pct(self, cost: CostModel):
        return self.gross_pct() - sum(sz * cost.round_trip_pct(px) for px, sz in self.legs)

    def r_multiple(self):
        risk = abs(self.entry - self.stop0) / self.entry * 100
        return self.gross_pct() / risk if risk else None


@dataclass
class Result:
    signals: list
    trades: list


def _sig(ts, action="HOLD", intent=None, price=None, stop=None, target=None, size=0.0, reason=""):
    return {"ts": ts, "action": action, "intent": intent, "price": price, "stop": stop, "target": target,
            "size": size, "reason": reason}


class _Base:
    name = "base"
    defaults = {}

    def __init__(self, **params):
        self.p = dict(self.defaults)
        unknown = set(params) - set(self.p)
        if unknown:
            raise ValueError(f"{self.name} 沒有這些參數：{sorted(unknown)}")
        self.p.update(params)

    def _tk(self, p):
        return tick_size(p, self.p.get("tick"))

    def _rt(self, p, direction="down"):
        return round_tick(p, direction, self.p.get("tick"))

    def run(self, data) -> Result:
        rows = _rows(data)
        if not rows:
            return Result([], [])
        self.bar = _bar_len(rows)
        days = _by_day(rows)
        signals, trades = [], []
        for k, (d, bars) in enumerate(days):
            hist = days[max(0, k - 30):k]
            if not hist:
                signals += [_sig(b["ts"], reason="沒有前一天資料") for b in bars]
                continue
            s, t = self._day(d, bars, hist)
            signals += s; trades += t
        return Result(signals, trades)

    def signal(self, data):
        """即時：用到最後一根 K 棒為止的資料，回最後一根收盤時的動作。"""
        res = self.run(data)
        return res.signals[-1] if res.signals else _sig(None, reason="沒有資料")

    # 子類別實作
    def _day(self, d, bars, hist):
        raise NotImplementedError


# ---------------------------------------------------------------- 模組 A
class Model_A(_Base):
    """
    現行模型的機械核心（model.build_plan + monitor 訊號，與 backtest_entry.py 同構）：
      價位 = 昨收 / 昨高 / 昨低 / 今開 / 開盤 15 分鐘高低 / VWAP / 盤中高低（到前一根）
      做多：最近 12 分鐘碰過最近支撐 S（±1 tick），收盤 ≥ S + 1 tick → 收盤價買；停損 S − max(2 tick, 0.15×昨日振幅)，
            目標 = 上方第一個離進場 ≥ max(3 tick, 0.5%, 0.2×昨日振幅) 的價位。做空對稱（碰壓力不過）。
      09:10–13:05 發訊號；同方向 30 分鐘不重發；持有最多 30 分鐘；13:25 強制平倉。
    盤前籌碼因子（法人、分點、權證）歷史上無法逐日還原，所以這裡只比「進出場機制」。
    """
    name = "A"
    defaults = dict(start="09:10", last_entry="13:05", flat="13:25", touch_min=12, cooldown_min=30,
                    hold_min=30, pad_range=0.15, gain_pct=0.5, gain_range=0.2, rr_min=None, tick=None, orb_end="09:15")

    @staticmethod
    def _hm(s):
        h, m = s.split(":"); return int(h) * 60 + int(m)

    @classmethod
    def for_futures(cls, **kw):
        """台指期 / 小台：一檔 1 點，08:45 開盤 → 08:55 起發訊號、開盤區間到 09:00、13:20 後不進、13:40 平倉。"""
        p = dict(tick=1.0, start="08:55", last_entry="13:20", flat="13:40", orb_end="09:00")
        p.update(kw)
        return cls(**p)

    def _levels(self, bars, i, prev):
        done = bars[:i + 1]
        lv = [prev["close"], prev["high"], prev["low"], bars[0]["open"]]
        oe = self._hm(self.p["orb_end"])
        if _minute(done[-1]["ts"]) + self.bar > oe:
            orb = [b for b in bars if _minute(b["ts"]) < oe]
            if orb:
                lv += [max(b["high"] for b in orb), min(b["low"] for b in orb)]
        vol = sum(b["volume"] for b in done)
        if vol:
            lv.append(sum((b["high"] + b["low"] + b["close"]) / 3 * b["volume"] for b in done) / vol)
        if i >= 1:
            lv += [max(b["high"] for b in done[:-1]), min(b["low"] for b in done[:-1])]
        return sorted(set(round(x, 2) for x in lv if x))

    def _day(self, d, bars, hist):
        p, B = self.p, self.bar
        pb = hist[-1][1]
        prev = {"close": pb[-1]["close"], "high": max(b["high"] for b in pb), "low": min(b["low"] for b in pb)}
        rng = prev["high"] - prev["low"]
        t0, t1_, tf = self._hm(p["start"]), self._hm(p["last_entry"]), self._hm(p["flat"])
        sigs, trades = [], []
        last = {"long": -999, "short": -999}
        pos = None
        for i, b in enumerate(bars):
            m_close = _minute(b["ts"]) + B                       # 這根收盤的時間
            s = _sig(b["ts"])
            # ---- 持倉管理
            if pos:
                if _minute(b["ts"]) >= tf:
                    pos.exit, pos.exit_ts, pos.reason = b["open"], b["ts"], "13:25 平倉"
                else:
                    short = pos.side == "short"
                    hit_stop = b["high"] >= pos.stop0 if short else b["low"] <= pos.stop0
                    hit_t = b["low"] <= pos.target if short else b["high"] >= pos.target
                    if hit_stop:
                        pos.exit, pos.exit_ts, pos.reason = pos.stop0, b["ts"], "停損"
                    elif hit_t:
                        pos.exit, pos.exit_ts, pos.reason = pos.target, b["ts"], "目標"
                    elif m_close - _minute(pos.entry_ts) - B >= p["hold_min"]:
                        pos.exit, pos.exit_ts, pos.reason = b["close"], b["ts"], "持有到時"
                if pos.exit is not None:
                    s = _sig(b["ts"], "BUY" if pos.side == "short" else "SELL",
                             "close_short" if pos.side == "short" else "close_long", pos.exit, reason=pos.reason)
                    trades.append(pos); pos = None
                sigs.append(s)
                continue
            # ---- 找進場
            if i < 4 or not (t0 <= m_close - B < t1_) or m_close > tf:
                sigs.append(s); continue
            c = b["close"]; tick = self._tk(c)
            pad = max(2 * tick, round(p["pad_range"] * rng, 2))
            min_gain = max(3 * tick, c * p["gain_pct"] / 100, p["gain_range"] * rng)
            lv = self._levels(bars, i, prev)
            nb = max(1, p["touch_min"] // B)
            recent = bars[max(0, i - nb + 1):i + 1]
            below = [x for x in lv if x < c - tick / 2]
            above = [x for x in lv if x > c + tick / 2]
            mnow = _minute(b["ts"])
            cand = None
            if below and mnow - last["long"] >= p["cooldown_min"]:
                sp = below[-1]
                entry = self._rt(sp + tick, "up"); stop = self._rt(sp - pad, "down")
                tgt = next((x for x in above if x - entry >= min_gain), None) or self._rt(entry + max(min_gain, 0.5 * rng), "up")
                if any(r["low"] <= sp + tick for r in recent) and entry <= c < tgt and c > stop:
                    cand = ("long", c, stop, tgt, f"回測支撐 {sp} 守住")
            if cand is None and above and mnow - last["short"] >= p["cooldown_min"]:
                rp = above[0]
                entry = self._rt(rp - tick, "down"); stop = self._rt(rp + pad, "up")
                tgt = next((x for x in reversed(below) if entry - x >= min_gain), None) or self._rt(entry - max(min_gain, 0.5 * rng), "down")
                if any(r["high"] >= rp - tick for r in recent) and tgt < c <= entry and c < stop:
                    cand = ("short", c, stop, tgt, f"碰壓力 {rp} 不過")
            if cand:
                side, px, stop, tgt, why = cand
                rr = abs(tgt - px) / abs(px - stop) if px != stop else 0
                if p["rr_min"] is None or rr >= p["rr_min"]:
                    last[side] = mnow
                    pos = Trade(self.name, d, side, b["ts"], px, stop, tgt, legs=[(px, 1.0)])
                    s = _sig(b["ts"], "BUY" if side == "long" else "SELL", "open_" + side, px, stop, tgt, 1.0,
                             f"{why}，報酬風險比 {rr:.1f}")
            sigs.append(s)
        if pos:                                                   # 資料在 13:25 前就結束（例如 5 分 K 沒有 13:25 那根）
            lb = bars[-1]
            pos.exit, pos.exit_ts, pos.reason = lb["close"], lb["ts"], "收盤前平倉"
            trades.append(pos)
        return sigs, trades


# ---------------------------------------------------------------- 模組 B
class Model_B(_Base):
    """
    噪音區突破（Zarattini–Aziz–Barbon 2024 的日內動能，改成台股個股版）+「Stocks in Play」相對量 + 內外盤確認：
      σ(t)   = 前 N 天「同一時刻收盤 / 當天開盤 − 1」絕對值的平均（這個時間點平常會晃多遠）
      上緣 UB(t) = max(今開, 昨收) × (1 + k·σ(t))；下緣 LB(t) = min(今開, 昨收) × (1 − k·σ(t))（跳空調整）
      區間內 = 供需平衡、沒有趨勢 → 不交易（盤整雙巴的來源直接關掉）
    進場（只在 09:30 起每 15 分鐘的檢查點、12:30 前）：
      多：收盤 > UB 且 > VWAP，相對量 RVOL(t) = 今日累積量 / 前 N 天同時刻累積量平均 ≥ rvol_min，
          有內外盤資料時近 15 分鐘 (外−內)/(外+內) ≥ ofi_min；離漲停 ≥ limit_buf%；前 N 天平均振幅 ≥ min_adr%（成本門檻）
      空：對稱（收盤 < LB 且 < VWAP …，離跌停 ≥ limit_buf%）
      個股另加 tick 成本濾網：一檔 / 股價 ≤ max_tick_pct%（台股級距在 100、500 元上方最貴，例如 101 元一檔 0.5 = 0.50%）
    停損：移動停損，個股 = VWAP、指數 = max(UB(t), VWAP)（空單對稱），每根收盤檢查；盤中硬停損 = 初始停損再外推 0.5R
    加碼（預設關閉，add_r=1 打開）：浮盈 ≥ 1R 且內外盤仍同向 → 加 0.5 單位，整個部位停損拉到進場價
    出場：移動停損、13:20 收盤前平倉（避開 13:25 收盤試撮）；同方向一天只做一次。
          目標價只是參考（2 倍噪音帶），不在目標出場（讓獲利單跑，提高盈虧比）。
    回測（ab/backtest_ab.py）：個股版扣成本後仍為負；指數期貨版扣成本後小幅為正但不顯著，見 system_state.yaml。
    """
    name = "B"
    # 預設 = 個股版（2024-02~2025-02 前段 48 組小網格選出，後段只驗證）；指數期貨版用 Model_B.for_index()
    # add_r=99 = 不加碼：48 組裡加碼版本全部比不加碼差（多一次成本，毛利沒有跟著變多）
    defaults = dict(lookback=14, k=1.0, first_check="09:30", every_min=15, last_entry="12:30", flat="13:20",
                    rvol_min=1.2, ofi_min=0.10, ofi_win_min=15, limit_buf=1.5, min_adr=2.5, min_price=10.0,
                    max_gap=9.0, add_r=99.0, add_size=0.5, add_last="12:45", hard_r=0.5,
                    trail="vwap", max_tick_pct=0.2, tick=None, max_risk_pct=0.0)

    @classmethod
    def for_index(cls, **kw):
        """台指期 / 小台版（原論文就是指數）：不看相對量與振幅門檻，移動停損 max(邊界, VWAP)。"""
        p = dict(rvol_min=0.0, min_adr=0.0, min_price=0.0, max_gap=5.0, max_tick_pct=0.0, trail="band_vwap")
        p.update(kw)
        return cls(**p)

    @classmethod
    def for_futures(cls, **kw):
        """台指期 / 小台日盤 08:45–13:45：開盤 30 分後（09:15）起每 15 分檢查、13:00 後不進、13:40 平倉；一檔 1 點。"""
        p = dict(tick=1.0, first_check="09:15", last_entry="13:00", flat="13:40", add_last="13:15")
        p.update(kw)
        return cls.for_index(**p)

    @staticmethod
    def _hm(s):
        h, m = s.split(":"); return int(h) * 60 + int(m)

    def _trail(self, long_, ub, lb, vwap):
        t = self.p["trail"]
        if t == "none":
            return None
        if t == "vwap":
            return vwap
        if t == "band":
            return ub if long_ else lb
        return max(ub, vwap) if long_ else min(lb, vwap)

    def _profile(self, bars):
        """一天的 {收盤時間(分): (收盤/開盤 − 1 的絕對值, 累積量)}。"""
        o = bars[0]["open"]; cum = 0.0; out = {}
        for b in bars:
            cum += b["volume"]
            out[_minute(b["ts"]) + self.bar] = (abs(b["close"] / o - 1), cum)
        return out

    @staticmethod
    def _at(prof, m):
        """取 ≤ m 的最後一筆（缺 K 棒時往前補）。"""
        best = None
        for k in prof:
            if k <= m and (best is None or k > best):
                best = k
        return prof[best] if best is not None else None

    def _day(self, d, bars, hist):
        p, B = self.p, self.bar
        hist = hist[-p["lookback"]:]
        pb = hist[-1][1]
        y = pb[-1]["close"]
        o = bars[0]["open"]
        up_lim, dn_lim = limits(y, p["tick"])
        profs = [self._profile(hb) for _, hb in hist]
        adr = sum((max(b["high"] for b in hb) - min(b["low"] for b in hb)) / hb[0]["open"] * 100 for _, hb in hist) / len(hist)
        gap = (o / y - 1) * 100
        skip = None
        if len(hist) < max(5, p["lookback"] // 2):
            skip = f"歷史只有 {len(hist)} 天"
        elif o < p["min_price"]:
            skip = f"股價 {o} 太低"
        elif adr < p["min_adr"]:
            skip = f"前 {len(hist)} 天平均振幅 {adr:.1f}% < {p['min_adr']}%，扣成本後空間不夠"
        elif abs(gap) > p["max_gap"]:
            skip = f"跳空 {gap:+.1f}%（接近漲跌停或除權息 / 資料異常）"
        f_chk, l_ent, l_add, flat = (self._hm(p[k]) for k in ("first_check", "last_entry", "add_last", "flat"))
        ref_hi, ref_lo = max(o, y), min(o, y)
        self.last_info = {"ts": None, "skip": skip}
        sigs, trades = [], []
        done_side = set()
        pos = None
        stop = hard = None
        risk = None
        cum_v = cum_pv = 0.0
        prof_cache = {}
        for i, b in enumerate(bars):
            m_close = _minute(b["ts"]) + B
            cum_v += b["volume"]; cum_pv += (b["high"] + b["low"] + b["close"]) / 3 * b["volume"]
            vwap = cum_pv / cum_v if cum_v else b["close"]
            if m_close not in prof_cache:
                vals = [self._at(pr, m_close) for pr in profs]
                vals = [v for v in vals if v]
                prof_cache[m_close] = ((sum(v[0] for v in vals) / len(vals)) if vals else None,
                                       (sum(v[1] for v in vals) / len(vals)) if vals else None)
            sigma, avg_cum = prof_cache[m_close]
            c = b["close"]
            s = _sig(b["ts"])
            if sigma is None:
                sigs.append(s); continue
            ub, lb = ref_hi * (1 + p["k"] * sigma), ref_lo * (1 - p["k"] * sigma)
            # 內外盤：近 ofi_win_min 分鐘
            nb = max(1, p["ofi_win_min"] // B)
            win = [r for r in bars[max(0, i - nb + 1):i + 1] if "buy_vol" in r]
            ofi = None
            if win:
                bv, sv = sum(r["buy_vol"] for r in win), sum(r["sell_vol"] for r in win)
                ofi = (bv - sv) / (bv + sv) if bv + sv else 0.0
            # 給即時儀表板：這一根收盤時的噪音帶、VWAP、相對量、離開盤價幾倍噪音（≥1 才算出帶）
            self.last_info = {"ts": b["ts"], "close": c, "ub": round(ub, 2), "lb": round(lb, 2), "vwap": round(vwap, 2),
                              "sigma_pct": round(sigma * 100, 3), "rvol": round(cum_v / avg_cum, 2) if avg_cum else None,
                              "ofi": round(ofi, 2) if ofi is not None else None, "ref_hi": ref_hi, "ref_lo": ref_lo,
                              "pos_sigma": round(((c / ref_hi - 1) if c >= ref_hi else (c / ref_lo - 1) if c <= ref_lo else 0.0) / (p["k"] * sigma), 2) if sigma else None,
                              "skip": skip, "up_lim": up_lim, "dn_lim": dn_lim}
            # ---- 持倉管理
            if pos:
                long_ = pos.side == "long"
                trail = self._trail(long_, ub, lb, vwap)
                if trail is not None:
                    stop = max(stop, trail) if long_ else min(stop, trail)
                ex = why = None
                if (b["low"] <= hard) if long_ else (b["high"] >= hard):
                    ex, why = hard, "盤中硬停損"
                elif m_close >= flat:
                    ex, why = c, f"{p['flat']} 收盤前平倉"
                elif (c < stop) if long_ else (c > stop):
                    ex, why = c, f"收盤跌破移動停損 {stop:.2f}" if long_ else f"收盤站上移動停損 {stop:.2f}"
                if ex is not None:
                    pos.exit, pos.exit_ts, pos.reason = ex, b["ts"], why
                    trades.append(pos)
                    s = _sig(b["ts"], "SELL" if long_ else "BUY", "close_long" if long_ else "close_short", ex, reason=why)
                    pos = None
                elif len(pos.legs) == 1 and m_close <= l_add:
                    gain = (c - pos.entry) if long_ else (pos.entry - c)
                    flow_ok = ofi is None or (ofi >= p["ofi_min"] if long_ else ofi <= -p["ofi_min"])
                    if risk and gain >= p["add_r"] * risk and flow_ok:
                        pos.legs.append((c, p["add_size"]))
                        stop = max(stop, pos.entry) if long_ else min(stop, pos.entry)
                        s = _sig(b["ts"], "BUY" if long_ else "SELL", "add_long" if long_ else "add_short", c,
                                 round(stop, 2), None, p["add_size"], f"浮盈 {gain / risk:.1f}R、內外盤同向，加碼 {p['add_size']}，停損拉到進場價")
                    else:
                        s = _sig(b["ts"], reason=f"持有{'多' if long_ else '空'}單，移動停損 {stop:.2f}")
                else:
                    s = _sig(b["ts"], reason=f"持有{'多' if long_ else '空'}單，移動停損 {stop:.2f}")
                sigs.append(s); continue
            # ---- 找進場
            check = m_close >= f_chk and m_close <= l_ent and (m_close - f_chk) % p["every_min"] == 0
            if skip or not check:
                s["reason"] = skip or ""
                sigs.append(s); continue
            rvol = cum_v / avg_cum if avg_cum else None
            why_not = []
            side = None
            if c > ub and c > vwap and "long" not in done_side:
                side = "long"
            elif c < lb and c < vwap and "short" not in done_side:
                side = "short"
            if side is None:
                s["reason"] = f"噪音區內（{lb:.2f}–{ub:.2f}）或已做過，不交易"
                sigs.append(s); continue
            if rvol is None or rvol < p["rvol_min"]:
                why_not.append(f"相對量 {rvol or 0:.2f} < {p['rvol_min']}")
            if ofi is not None and (ofi < p["ofi_min"] if side == "long" else ofi > -p["ofi_min"]):
                why_not.append(f"內外盤 {ofi:+.2f} 沒同向")
            if side == "long" and c >= up_lim * (1 - p["limit_buf"] / 100):
                why_not.append(f"離漲停 {up_lim} 不到 {p['limit_buf']}%")
            if side == "short" and c <= dn_lim * (1 + p["limit_buf"] / 100):
                why_not.append(f"離跌停 {dn_lim} 不到 {p['limit_buf']}%")
            if p["max_tick_pct"] and self._tk(c) / c * 100 > p["max_tick_pct"]:
                why_not.append(f"一檔 = {self._tk(c) / c * 100:.2f}% > {p['max_tick_pct']}%，滑價成本太高")
            stop0 = self._trail(side == "long", ub, lb, vwap)
            if stop0 is None:                                     # trail=none：初始停損仍用 max(邊界, VWAP)
                stop0 = max(ub, vwap) if side == "long" else min(lb, vwap)
            stop0 = self._rt(stop0, "down" if side == "long" else "up")
            risk = abs(c - stop0)
            if risk < self._tk(c):
                risk = self._tk(c) * 2
                stop0 = self._rt(c - risk, "down") if side == "long" else self._rt(c + risk, "up")
            if p["max_risk_pct"] and risk / c * 100 > p["max_risk_pct"]:
                why_not.append(f"停損 {stop0} 離進場 {risk / c * 100:.1f}% > {p['max_risk_pct']}%，風險太大")
            if why_not:
                s["reason"] = f"突破{'上緣' if side == 'long' else '下緣'}但 " + "、".join(why_not)
                sigs.append(s); continue
            stop = stop0
            hard = stop0 - p["hard_r"] * risk if side == "long" else stop0 + p["hard_r"] * risk
            done_side.add(side)
            # 參考目標 = 2 倍噪音帶（今天到這個時刻「平常不會到」的兩倍距離），不碰漲跌停；只供參考，出場靠移動停損
            tgt = ref_hi * (1 + 2 * p["k"] * sigma) if side == "long" else ref_lo * (1 - 2 * p["k"] * sigma)
            tgt = min(self._rt(tgt, "down"), up_lim) if side == "long" else max(self._rt(tgt, "up"), dn_lim)
            pos = Trade(self.name, d, side, b["ts"], c, stop0, tgt, legs=[(c, 1.0)])
            fl = f"、內外盤 {ofi:+.2f}" if ofi is not None else ""
            s = _sig(b["ts"], "BUY" if side == "long" else "SELL", "open_" + side, c, stop0, tgt, 1.0,
                     f"收盤 {c} {'突破噪音區上緣' if side == 'long' else '跌破噪音區下緣'} {ub if side == 'long' else lb:.2f}、"
                     f"{'站上' if side == 'long' else '跌破'} VWAP {vwap:.2f}、相對量 {rvol:.2f}{fl}；移動停損 max(邊界, VWAP)，13:20 前出場")
            sigs.append(s)
        if pos:
            lb_ = bars[-1]
            pos.exit, pos.exit_ts, pos.reason = lb_["close"], lb_["ts"], "資料結束平倉"
            trades.append(pos)
        return sigs, trades
