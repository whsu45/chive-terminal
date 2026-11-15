"""
盤中 A/B 對決：模組 B（ab/ab_models.Model_B，噪音區突破）在 monitor 裡的即時版。模組 A 就是 model.py。

- 歷史：前 14 個交易日的 5 分 K（算噪音帶 σ(t) 與相對量）。有永豐就用一次 kbars（1 分 K 合成 5 分 K，盤中算 1 次額度），
  沒有就用 Yahoo 5 分 K（量是股，換成張）。每檔每天抓一次，存 .cache/ab_hist/。
- 今天：monitor 的 1 分 K（永豐串流 / 分 K 回補，或 MIS 湊的）合成「已經走完」的 5 分 K，跟回測（tw_stocker 5 分 K）同一個解析度。
- 每完成一根 5 分 K 重算一次；B 自己模擬進出（收盤價進、移動停損出、13:20 平），平倉的單寫 logs/ab_trades_<裝置>.jsonl，
  給下週的 A/B 對照（A 的訊號結果在 signal_outcomes_<裝置>.jsonl）。
- B 只是觀察用的挑戰者：回測（120 檔 × 515 天）扣成本後每筆 −0.52%，不改 A 的多空強度、不發進場警示。
"""
import os
import sys
import json
import threading
import datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "ab"))
from ab_models import Model_B, CostModel  # noqa: E402
from config import CACHE_DIR, LOG_DIR, dev_path  # noqa: E402

HIST_DIR = os.path.join(CACHE_DIR, "ab_hist")
HIST_DAYS = 14
BT_NOTE = "回測 120 檔 × 515 天：扣成本後每筆 −0.52%（勝率 25%、盈虧比 1.3）；觀察用，不計入 A 的強度"


def _bucket5(hhmm):
    h, m = int(hhmm[:2]), int(hhmm[3:5])
    return h * 60 + (m // 5) * 5


def _resample5(rows):
    """[{ts, open, high, low, close, volume}]（1 分 K，ts = 開始時間）→ 5 分 K。"""
    out = {}
    for r in rows:
        t = r["ts"]
        k = t.replace(minute=(t.minute // 5) * 5, second=0, microsecond=0)
        b = out.get(k)
        if b is None:
            out[k] = {"ts": k, "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"], "volume": r["volume"]}
        else:
            b["high"] = max(b["high"], r["high"]); b["low"] = min(b["low"], r["low"]); b["close"] = r["close"]; b["volume"] += r["volume"]
    return [out[k] for k in sorted(out)]


def _session(rows):
    return [r for r in rows if 540 <= r["ts"].hour * 60 + r["ts"].minute < 810]   # 09:00–13:30


def _from_shioaji(code, today):
    import src_shioaji as S
    api = S.login()
    if api is None:
        return None
    contract = S.get_contract(api, code)
    if contract is None:
        return None
    start = (today - dt.timedelta(days=30)).isoformat()
    end = (today - dt.timedelta(days=1)).isoformat()
    k = S.fetch_kbars(api, contract, start, "B 噪音帶歷史", end=end)
    rows = []
    for ts, o, h, l, c, v in zip(k.ts, k.Open, k.High, k.Low, k.Close, k.Volume):
        t = S.ns_to_dt(ts) - dt.timedelta(minutes=1)        # 永豐 ts 是結束時間
        if t.date() < today:
            rows.append({"ts": t, "open": float(o), "high": float(h), "low": float(l), "close": float(c), "volume": float(v)})
    return _resample5(_session(rows))


def _from_yahoo(code, market, today):
    import src_yahoo
    sym = f"{code}.TW" if market == "tse" else f"{code}.TWO"
    rows = []
    for r in src_yahoo.chart(sym, "5m", "1mo", cache_ttl=3600):
        t = r["ts"]
        if t.date() < today and r.get("o") and r.get("h") and r.get("l"):
            rows.append({"ts": t, "open": float(r["o"]), "high": float(r["h"]), "low": float(r["l"]), "close": float(r["c"]),
                         "volume": float(r["v"] or 0) / 1000.0})
    return _session(rows)


def load_history(code, market, today=None, use_shioaji=True):
    """前 HIST_DAYS 個交易日的 5 分 K + 來源。快取一天一份。"""
    today = today or dt.date.today()
    os.makedirs(HIST_DIR, exist_ok=True)
    path = os.path.join(HIST_DIR, f"{code}_{today:%Y%m%d}.json")
    if os.path.exists(path):
        try:
            d = json.load(open(path, encoding="utf-8"))
            return [dict(r, ts=dt.datetime.fromisoformat(r["ts"])) for r in d["rows"]], d["src"]
        except Exception:
            pass
    rows, src, err = None, None, []
    if use_shioaji:
        try:
            rows, src = _from_shioaji(code, today), "永豐分 K"
        except Exception as e:
            err.append(f"永豐：{str(e)[:60]}")
    if not rows:
        try:
            rows, src = _from_yahoo(code, market, today), "Yahoo 5 分 K"
        except Exception as e:
            err.append(f"Yahoo：{str(e)[:60]}")
    if not rows:
        raise RuntimeError("；".join(err) or "沒有歷史分 K")
    days = sorted({r["ts"].date() for r in rows})[-HIST_DAYS:]
    rows = [r for r in rows if r["ts"].date() in set(days)]
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"src": src, "rows": [dict(r, ts=r["ts"].isoformat()) for r in rows]}, f)
    return rows, src


def today_rows(series, day, now):
    """monitor 的 1 分 K [[HH:MM, o,h,l,c,vol,(amt)], ...] → 今天已走完的 5 分 K。"""
    rows = []
    for b in series or []:
        hhmm = b[0]
        if not ("09:00" <= hhmm < "13:30") or b[4] is None:
            continue
        t = dt.datetime.combine(day, dt.time(int(hhmm[:2]), int(hhmm[3:5])))
        rows.append({"ts": t, "open": float(b[1]), "high": float(b[2]), "low": float(b[3]), "close": float(b[4]), "volume": float(b[5] or 0)})
    five = _resample5(rows)
    cur = now.hour * 60 + now.minute
    return [r for r in five if r["ts"].hour * 60 + r["ts"].minute + 5 <= cur]


class LiveB:
    def __init__(self, use_shioaji=True):
        self.use_shioaji = use_shioaji
        self.model = Model_B()
        self.cost = CostModel()
        self.hist = {}          # code -> (rows, src) | ("error", msg) | ("loading", None)
        self.cache = {}         # code -> (最後一根 5 分 K 的 ts, 結果)
        self.logged = set()
        self.lock = threading.Lock()
        self._load_logged()

    def _log_path(self):
        return dev_path(LOG_DIR, "ab_trades", ".jsonl")

    def _load_logged(self):
        try:
            with open(self._log_path(), encoding="utf-8") as f:
                for line in f:
                    j = json.loads(line)
                    self.logged.add((j["code"], j["entry_ts"]))
        except Exception:
            pass

    def prepare(self, code, market):
        """背景抓歷史（啟動、盤中加入時呼叫）。"""
        with self.lock:
            if code in self.hist and self.hist[code][0] != "error":
                return
            self.hist[code] = ("loading", None)

        def work():
            try:
                rows, src = load_history(code, market, use_shioaji=self.use_shioaji)
                with self.lock:
                    self.hist[code] = (rows, src)
                print(f"[B] {code} 噪音帶歷史 {len({r['ts'].date() for r in rows})} 天（{src}）")
            except Exception as e:
                with self.lock:
                    self.hist[code] = ("error", str(e)[:120])
                print(f"[B] {code} 歷史分 K 抓不到：{str(e)[:120]}")
        threading.Thread(target=work, daemon=True).start()

    def evaluate(self, code, series, now, name=None):
        """回儀表板用的 dict；每完成一根 5 分 K 才重算。"""
        with self.lock:
            h = self.hist.get(code)
        if h is None:
            return {"status": "wait", "label": "尚未準備", "note": BT_NOTE}
        if h[0] == "loading":
            return {"status": "wait", "label": "抓前 14 天 5 分 K 中…", "note": BT_NOTE}
        if h[0] == "error":
            return {"status": "error", "label": f"歷史分 K 抓不到：{h[1]}", "note": BT_NOTE}
        rows, src = h
        today = today_rows(series, now.date(), now)
        if not today:
            return {"status": "wait", "label": "09:05 第一根 5 分 K 走完後開始", "src": src, "note": BT_NOTE}
        if today[0]["ts"].strftime("%H:%M") > "09:05":
            return {"status": "error", "label": f"今天的分 K 從 {today[0]['ts']:%H:%M} 才開始（缺開盤那段），噪音帶與 VWAP 算不準，B 今天暫停",
                    "src": src, "note": BT_NOTE}
        key = today[-1]["ts"]
        cached = self.cache.get(code)
        if cached and cached[0] == key:
            return cached[1]
        res = self.model.run(rows + today)
        info = getattr(self.model, "last_info", None) or {}
        sig_today = [s for s in res.signals if s["ts"] and s["ts"].date() == now.date()]
        tr_today = [t for t in res.trades if t.date == now.date()]
        out = self._panel(code, name, info, sig_today, tr_today, src, now)
        self.cache[code] = (key, out)
        self._log(code, name, tr_today, now)
        return out

    def _panel(self, code, name, info, sigs, trades, src, now):
        p = self.model.p
        last = sigs[-1] if sigs else None
        opened = [s for s in sigs if s["intent"] in ("open_long", "open_short")]
        cur = None
        if opened:
            o = opened[-1]
            closed = any(t.entry_ts == o["ts"] for t in trades if t.exit is not None and t.reason != "資料結束平倉")
            if not closed:
                cur = o
        hhmm = now.strftime("%H:%M")
        out = {"src": src, "note": BT_NOTE, "time": info.get("ts").strftime("%H:%M") if info.get("ts") else None,
               "ub": info.get("ub"), "lb": info.get("lb"), "vwap": info.get("vwap"), "sigma_pct": info.get("sigma_pct"),
               "rvol": info.get("rvol"), "rvol_min": p["rvol_min"], "pos_sigma": info.get("pos_sigma"),
               "rules": f"{p['first_check']} 起每 {p['every_min']} 分檢查、{p['last_entry']} 後不進、{p['flat']} 平倉；相對量 ≥ {p['rvol_min']}、一檔 ≤ {p['max_tick_pct']}%、前 14 天振幅 ≥ {p['min_adr']}%",
               "trades": [{"side": t.side, "entry_t": t.entry_ts.strftime("%H:%M"), "entry": t.entry, "exit_t": t.exit_ts.strftime("%H:%M"),
                           "exit": t.exit, "reason": t.reason, "gross": round(t.gross_pct(), 2), "net": round(t.net_pct(self.cost), 2)}
                          for t in trades if t.exit is not None and t.reason != "資料結束平倉"]}
        # 進行中的部位：用最新那根的移動停損
        if cur:
            side = "long" if cur["intent"] == "open_long" else "short"
            stop = cur["stop"]
            if last and last["intent"] is None and "移動停損" in (last["reason"] or ""):
                try:
                    stop = float(last["reason"].rsplit(" ", 1)[-1])
                except ValueError:
                    pass
            px = info.get("close")
            pnl = ((px - cur["price"]) if side == "long" else (cur["price"] - px)) / cur["price"] * 100 if px else None
            out.update(status="long" if side == "long" else "short", side=side,
                       label=f"{cur['ts']:%H:%M} {'突破上緣做多' if side == 'long' else '跌破下緣做空'} {cur['price']}，持有中",
                       entry=cur["price"], stop=stop, target=cur["target"], entry_t=cur["ts"].strftime("%H:%M"),
                       pnl=round(pnl, 2) if pnl is not None else None, reason=cur["reason"])
            return out
        if info.get("skip"):
            out.update(status="off", label=f"今天不交易：{info['skip']}")
        elif hhmm >= p["last_entry"]:
            out.update(status="off", label=f"{p['last_entry']} 後不開新倉" + (f"；今天做了 {len(out['trades'])} 筆，已出場" if out["trades"] else "，今天沒有出現突破"))
        elif last and last["reason"]:
            st = "blocked" if last["reason"].startswith("突破") else "range"
            out.update(status=st, label=last["reason"])
        else:
            out.update(status="range", label="等下一個檢查點（每 15 分鐘）")
        # 尚未進場時的觸發價：收盤站上上緣（且在 VWAP 之上）做多、跌破下緣做空
        if out["status"] in ("range", "blocked") and info.get("ub"):
            out["trigger_long"] = max(info["ub"], info.get("vwap") or 0)
            out["trigger_short"] = min(info["lb"], info.get("vwap") or 1e18)
        return out

    def _log(self, code, name, trades, now):
        new = [t for t in trades if t.exit is not None and t.reason != "資料結束平倉" and (code, t.entry_ts.isoformat()) not in self.logged]
        if not new:
            return
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(self._log_path(), "a", encoding="utf-8") as f:
                for t in new:
                    f.write(json.dumps({"model": "B", "code": code, "name": name, "date": t.date.isoformat(), "side": t.side,
                                        "entry_ts": t.entry_ts.isoformat(), "entry": t.entry, "stop0": t.stop0, "target": t.target,
                                        "exit_ts": t.exit_ts.isoformat(), "exit": t.exit, "reason": t.reason,
                                        "gross_pct": round(t.gross_pct(), 3), "net_pct": round(t.net_pct(self.cost), 3),
                                        "params": {k: self.model.p[k] for k in ("k", "rvol_min", "trail", "max_tick_pct", "every_min")}},
                                       ensure_ascii=False) + "\n")
                    self.logged.add((code, t.entry_ts.isoformat()))
        except Exception as e:
            print(f"[B] 寫 ab_trades 失敗：{str(e)[:80]}")
