#!/usr/bin/env python3
"""
當沖多空監控：每 N 秒（預設 180）抓一次即時資料，重算多空勝率、支撐壓力與進出場計畫，直到 Ctrl+C。

    python3 monitor.py 3042                      # 用 groups.json 的族群
    python3 monitor.py 3042 --peers 2484,8081    # 自訂族群
    python3 monitor.py 3042 --once --json        # 跑一次、輸出 JSON（給程式/AI 讀）
    python3 monitor.py 3042 7704 --interval 120  # 同時監控多檔

每次評估都會 append 到 logs/monitor_YYYYMMDD.jsonl，收盤後可直接拿來復盤。
"""
import sys
import os
import json
import time
import argparse
import datetime as dt
import traceback

from config import LOG_DIR, load_env
import prep
import model
import src_mis
import src_warrants as W
import http_client as http
import glob
import subprocess
import threading
import webbrowser
import http.server
import urllib.parse
from config import CACHE_DIR, HERE

WEB_DIR = os.path.join(HERE, "web")


class WebState:
    """monitor 與網頁共用的狀態。"""
    def __init__(self):
        self.lock = threading.Lock()
        self.latest = {}      # code -> ev（去掉不可序列化欄位）
        self.history = {}     # code -> [{time, price, p_long, p_short, y}]
        self.daily = {}       # code -> 盤前資料（精簡）
        self.describe = {}    # code -> 盤前文字摘要
        self.index = {}       # t00/o00 -> {ref, chg}
        self.codes = []
        self.updated = None
        self.interval = 180
        self.shioaji = False

    def put(self, ev, quotes):
        with self.lock:
            rec = {k: v for k, v in ev.items() if k != "quote"}
            rec["quote"] = {k: ev["quote"].get(k) for k in ("ref", "o", "h", "l", "v", "y", "bids", "asks")}
            self.latest[ev["code"]] = rec
            h = self.history.setdefault(ev["code"], [])
            h.append({"time": ev["time"], "price": ev.get("price"), "p_long": ev["p_long"], "p_short": ev["p_short"], "y": ev["quote"].get("y")})
            if len(h) > 2000:
                del h[:500]
            for k in ("t00", "o00"):
                q = quotes.get(k)
                if q and q.get("ref") and q.get("y"):
                    self.index[k] = {"ref": q["ref"], "chg": (q["ref"] / q["y"] - 1) * 100}
            self.updated = dt.datetime.now().isoformat()

    def snapshot(self):
        with self.lock:
            return json.dumps({"codes": self.codes, "latest": self.latest, "daily": self.daily, "describe": self.describe,
                               "index": self.index, "updated": self.updated, "interval": self.interval, "shioaji": self.shioaji},
                              ensure_ascii=False, default=str)

    def history_json(self, code):
        with self.lock:
            return json.dumps(self.history.get(code, []), ensure_ascii=False, default=str)


WEB = WebState()


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):  # 不要把每次 fetch 印到終端
        pass

    def _send(self, body, ctype="application/json; charset=utf-8", code=200):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            with open(os.path.join(WEB_DIR, "index.html"), "rb") as f:
                return self._send(f.read(), "text/html; charset=utf-8")
        if path == "/api/state":
            return self._send(WEB.snapshot())
        if path.startswith("/api/history/"):
            return self._send(WEB.history_json(path.rsplit("/", 1)[-1]))
        self._send('{"error":"not found"}', code=404)


def start_web(port):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    print(f"[web] 儀表板：http://127.0.0.1:{port}/   （只綁本機，不對外）")
    return srv

load_env()
CALIBRATE_MAX_AGE_DAYS = int(os.environ.get("CALIBRATE_MAX_AGE_DAYS", "45") or 45)


def calibration_age_days():
    """最近一次 calibrate.py 的天數；沒跑過回 None。"""
    files = sorted(glob.glob(os.path.join(CACHE_DIR, "calibration_*.json")))
    if not files:
        return None
    d = os.path.basename(files[-1])[12:20]
    try:
        return (dt.date.today() - dt.datetime.strptime(d, "%Y%m%d").date()).days
    except ValueError:
        return None


def run_calibration(reason):
    """以子程序跑 calibrate.py --apply（只改 .env 的兩行金額），結果印到畫面。"""
    print(f"[calibrate] {reason}，重新校準大單/散戶金額門檻（歷史逐筆走快取，約 2~5 分鐘）...")
    try:
        r = subprocess.run([sys.executable, os.path.join(HERE, "calibrate.py"), "--apply"], cwd=HERE,
                           capture_output=True, text=True, timeout=1800)
        tail = [ln for ln in r.stdout.splitlines() if ln.startswith("  →") or ln.startswith("  已寫入") or "建議" in ln]
        print("\n".join(tail) or r.stdout[-800:])
        if r.returncode != 0:
            print(f"[calibrate] 失敗：{r.stderr[-400:]}")
        load_env()
        for k in ("BIG_LOT_AMOUNT", "SMALL_LOT_AMOUNT"):
            os.environ.pop(k, None)
        load_env()
    except Exception as e:
        print(f"[calibrate] 失敗：{e}")


def fmt_pct(x, nd=2):
    return "-" if x is None else f"{x:+.{nd}f}%"


def render(ev, d, brief=False):
    q = ev["quote"]; info = ev["info"]
    L = []
    L.append("─" * 100)
    L.append(f"[{ev['time']}] {ev['code']} {ev['name']}  {q.get('ref')}  {fmt_pct(info.get('chg_pct'))}  "
             f"開 {q.get('o')} 高 {q.get('h')} 低 {q.get('l')}  量 {q.get('v')} 張"
             + (f"  量比 {info['vol_pace']:.2f}" if info.get('vol_pace') else "")
             + (f"  VWAP {info['vwap']:.2f}" if info.get('vwap') else ""))
    idx = info.get("idx_chg_pct")
    gl = info.get("group_detail") or []
    idx_name = "加權" if d["market"] == "tse" else "櫃買"
    grp = (f"族群均 {fmt_pct(info.get('group_avg'))} ({info.get('group_up')}/{info.get('group_n')} 漲)  "
           + "  ".join(f"{n or c} {p:+.1f}%" for p, c, n in gl)) if gl else "族群：未定義（--peers 指定）"
    L.append(f"  {idx_name} {fmt_pct(idx)}  相對強弱 {fmt_pct(info.get('rs_vs_index'), 1)}  {grp}")
    bids = q.get("bids") or []; asks = q.get("asks") or []
    if bids or asks:
        L.append("  委買 " + " ".join(f"{p}×{v}" for p, v in bids[:5]) + "   委賣 " + " ".join(f"{p}×{v}" for p, v in asks[:5]))
    fl = ev.get("flow")
    if fl:
        th = fl.get("thresholds") or {}
        L.append(f"  主力淨 {fl['big_net']:+} 張 ({fl['big_net_pct']:+.1f}%)  其中藏單 {fl.get('hidden_net', 0):+}  "
                 f"散戶淨 {fl['small_net']:+} 張 ({fl['small_net_pct']:+.1f}%)  中實戶 {fl.get('mid_net', 0):+}  "
                 f"外盤 {fl['buy_vol']} / 內盤 {fl['sell_vol']}" + (f"  委買總 {fl['bid_total']} / 委賣總 {fl['ask_total']}" if fl.get('bid_total') else ""))
        if th:
            L.append(f"  門檻：大單 ≥{th['big_lots']} 張（{th['big_amount']/1e4:.0f} 萬→{th['big_by_amount']} 張"
                     + (f"，P95 {th['p95']} 張" if th.get('p95') else "") + (f"，固定 {th['fixed']}" if th.get('fixed') else "")
                     + f"）  散戶 <{th['small_lots']} 張（{th['small_amount']/1e4:.0f} 萬）")
        for i in (fl.get("iceberg") or [])[:2]:
            L.append(f"  規律小單：{i['size']} 張 × {i['count']} 筆 {i['side']}  共 {i['volume']} 張  佔筆數 {i['share_cnt']*100:.0f}%"
                     + (f"  平均間隔 {i['avg_gap_s']:.0f} 秒  cv {i['interval_cv']:.2f}" if i.get('interval_cv') is not None else "")
                     + ("  ← 機器單" if i.get('regular') else ""))
    wq = ev.get("warrants")
    if wq and wq.get("n"):
        wd_ = (d.get("warrants") or {})
        base = wd_.get("call_vol") or 0
        L.append(f"  權證：認購 {wq['call_vol']} 張（昨全日 {base}）漲 {wq['call_up']} 跌 {wq['call_down']} 檔  認售 {wq['put_vol']} 張  "
                 + "  ".join(f"{n} {v}張 {c:+.0f}%" for n, r, v, c in wq["tops"][:3] if c is not None))
    L.append(f"  ▶ 多 {ev['p_long']:.0f}%  空 {ev['p_short']:.0f}%   {ev['verdict']}   (盤前 {ev['daily_bias']:+.2f} / 盤中 {ev['intra_bias']:+.2f})")
    for w in ev["warnings"]:
        L.append(f"  !! {w}")
    if not brief:
        L.append("  盤前因子：" + ("  ".join(f"{n}({s:+.1f})" for n, s, _ in ev["daily_factors"]) or "無"))
        L.append("  盤中因子：" + ("  ".join(f"{n}({s:+.1f})" for n, s, _ in ev["intraday_factors"]) or "無"))
        pl = ev["plan"]
        if pl:
            L.append("  壓力 " + "  ".join(f"{n} {v}" for n, v in pl.get("resistance", [])) + "   支撐 " + "  ".join(f"{n} {v}" for n, v in pl.get("support", [])))
            s = pl.get("short")
            if s and ev["p_short"] > 0:
                L.append(f"  空：{s['entry']}  停損 {s['stop']} (風險 {s['risk_pct']:.1f}%)  目標 {s['t1']} → {s['t2']} (報酬 {s['reward_pct']:.1f}%)")
                if s.get("breakdown"):
                    L.append(f"      {s['breakdown']}")
            lg = pl.get("long")
            if lg:
                L.append(f"  多：{lg['entry']}  停損 {lg['stop']} (風險 {lg['risk_pct']:.1f}%)  目標 {lg['t1']} → {lg['t2']} (報酬 {lg['reward_pct']:.1f}%)")
                if lg.get("breakout"):
                    L.append(f"      {lg['breakout']}")
    return "\n".join(L)


def explain(ev):
    """因子說明（--verbose 時印）。"""
    L = ["  ── 因子說明"]
    for n, s, txt in ev["daily_factors"] + ev["intraday_factors"]:
        L.append(f"    {s:+.2f}  {n}：{txt}")
    return "\n".join(L)


class Monitor:
    def __init__(self, codes, peers_arg, interval, use_shioaji, big_lot, refresh, web=False):
        self.codes = codes
        self.interval = interval
        self.web = web
        WEB.codes = list(codes); WEB.interval = interval
        self.daily = {}
        self.samples = {c: [] for c in codes}
        self.w_prev = {}
        self.flow_hist = {}   # code -> [[epoch, big_net, small_net, price], ...]，給 model._flow_dynamics 看主力淨額的變化
        self.flow_src = None
        import src_shioaji
        for c in codes:
            self.daily[c] = prep.build(c, peers_arg, refresh)
            print(prep.describe(self.daily[c]))
            dd = self.daily[c]
            WEB.daily[c] = {k: dd.get(k) for k in ("code", "name", "market", "group", "peers", "limit_up", "limit_down", "tech", "flags", "insti_sum", "margin", "sbl")}
            WEB.daily[c]["warrants"] = {k: v for k, v in (dd.get("warrants") or {}).items() if k != "list"}
            WEB.describe[c] = prep.describe(dd)
            pc = (self.daily[c].get("tech") or {}).get("close")
            if pc:
                th = src_shioaji.lot_thresholds(pc)
                print(f"  大單門檻（以昨收 {pc} 推算）：≥{th['big_lots']} 張 ≈ {th['big_amount']/1e4:.0f} 萬；散戶 <{th['small_lots']} 張 ≈ {th['small_amount']/1e4:.0f} 萬；開盤後再以當日成交分布修正")
        self.ex_codes = set()
        for c in codes:
            d = self.daily[c]
            self.ex_codes.add(f"{d['market']}_{c}.tw")
            for p, pi in d["peer_info"].items():
                self.ex_codes.add(f"{pi['ex']}_{p}.tw")
        self.ex_codes |= set(src_mis.index_codes())
        self.calibrate_after_close = False
        if use_shioaji:
            import src_shioaji
            if src_shioaji.available():
                # 校準過舊（預設 45 天，CALIBRATE_MAX_AGE_DAYS）就重跑：盤前/盤後立刻跑，盤中等收盤後再跑
                age = calibration_age_days()
                if age is None or age > CALIBRATE_MAX_AGE_DAYS:
                    reason = "尚未校準" if age is None else f"上次校準已 {age} 天（上限 {CALIBRATE_MAX_AGE_DAYS} 天；兩個月前的盤勢基期可能已不同）"
                    hhmm = dt.datetime.now().strftime("%H:%M")
                    if "08:45" <= hhmm < "13:35":
                        print(f"[calibrate] {reason}，盤中不抓歷史逐筆，收盤後自動重跑")
                        self.calibrate_after_close = True
                    else:
                        run_calibration(reason)
                else:
                    print(f"[calibrate] 門檻校準距今 {age} 天（上限 {CALIBRATE_MAX_AGE_DAYS} 天），沿用 .env 設定")
                if big_lot:
                    os.environ["BIG_LOT_THRESHOLD"] = str(big_lot)
                ok = src_shioaji.start_stream(codes)
                if ok:
                    self.flow_src = src_shioaji
                    WEB.shioaji = True
                    u = src_shioaji.usage()
                    print(f"[shioaji] 串流已啟動 {ok}" + (f"，今日流量 {u['used_mb']:.0f}/{u['limit_mb']:.0f} MB" if u else ""))
            else:
                print("[shioaji] 未安裝或 .env 無金鑰，大單/散戶分流停用（其餘功能正常）")

    @staticmethod
    def _aggregate(snap):
        """把 intraday_detail 的快照彙總成 intraday() 同格式。"""
        res = {"call_vol": 0, "put_vol": 0, "call_up": 0, "call_down": 0, "put_up": 0, "put_down": 0, "n": len(snap), "tops": []}
        for s in snap.values():
            k = s["right"]
            res[f"{k}_vol"] += s["v"]
            if s["chg_pct"] is not None and s["v"] > 0 and s["chg_pct"] != 0:
                res[f"{k}_up" if s["chg_pct"] > 0 else f"{k}_down"] += 1
            if s["v"] > 0:
                res["tops"].append((s["name"], k, s["v"], s["chg_pct"]))
        tot = res["call_vol"] + res["put_vol"]
        res["call_share"] = res["call_vol"] / tot if tot else None
        res["tops"].sort(key=lambda x: -x[2]); res["tops"] = res["tops"][:5]
        return res

    def poll(self):
        quotes = src_mis.fetch(sorted(self.ex_codes))
        now = dt.datetime.now()
        results = []
        for c in self.codes:
            d = self.daily[c]
            q = quotes.get(c)
            if not q:
                print(f"[{now:%H:%M:%S}] {c} 無報價"); continue
            if q.get("ref") and q.get("v") is not None:
                self.samples[c].append((now, q["ref"], q["v"]))
            idx = {k: quotes[k] for k in ("t00", "o00") if k in quotes}
            peers_q = {p: quotes[p] for p in d["peers"] if p in quotes}
            flow = self.flow_src.flow(c, price=q.get("ref")) if self.flow_src else None
            if flow and flow.get("volume"):
                hh = self.flow_hist.setdefault(c, [])
                hh.append([now.timestamp(), flow["big_net"], flow["small_net"], q.get("ref")])
                del hh[:-60]
                flow["hist"] = list(hh)
            wq = None
            wl = (d.get("warrants") or {}).get("list") or []
            if wl:
                try:
                    wdict = {"call": [w for w in wl if w["right"] == "call"], "put": [w for w in wl if w["right"] == "put"]}
                    snap = W.intraday_detail(wdict)
                    wq = self._aggregate(snap)
                    stock_chg = ((q["ref"] / q["y"] - 1) * 100) if (q.get("ref") and q.get("y")) else None
                    wq["alerts"] = W.alerts(snap, self.w_prev.get(c), stock_chg, (d.get("warrants") or {}).get("call_vol"),
                                            max(1, round(self.interval / 60)))
                    self.w_prev[c] = snap
                except Exception as e:
                    print(f"  [warn] 權證盤中: {str(e)[:80]}")
            ev = model.evaluate(d, q, idx, peers_q, flow, self.samples[c], now, warrants=wq)
            if wq and wq.get("alerts"):
                ev["warnings"] = wq["alerts"] + ev["warnings"]
            WEB.put(ev, quotes)
            results.append(ev)
        return results

    def log(self, ev):
        path = os.path.join(LOG_DIR, f"monitor_{dt.date.today():%Y%m%d}.jsonl")
        rec = {k: v for k, v in ev.items() if k not in ("quote",)}
        rec["quote"] = {k: ev["quote"].get(k) for k in ("ref", "o", "h", "l", "v", "bids", "asks")}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    def run(self, once=False, as_json=False, verbose=False, brief=False):
        while True:
            t0 = time.time()
            try:
                results = self.poll()
                for ev in results:
                    self.log(ev)
                    if as_json:
                        print(json.dumps({k: v for k, v in ev.items() if k != "quote"}, ensure_ascii=False, default=str))
                    elif self.web:
                        inf = ev["info"]
                        print(f"[{ev['time']}] {ev['code']} {ev['name']} {ev['quote'].get('ref')} {fmt_pct(inf.get('chg_pct'))}  多 {ev['p_long']:.0f}% 空 {ev['p_short']:.0f}%  {ev['verdict']}"
                              + "".join(f"\n   {w}" for w in ev["warnings"] if w.startswith("⚠")))
                    else:
                        print(render(ev, self.daily[ev["code"]], brief))
                        if verbose:
                            print(explain(ev))
                sys.stdout.flush()
            except http.HttpError as e:
                print(f"[{dt.datetime.now():%H:%M:%S}] 抓取失敗：{e}")
            except Exception:
                traceback.print_exc()
            if once:
                break
            hhmm = dt.datetime.now().strftime("%H:%M")
            if hhmm >= "13:35" or hhmm < "08:30":
                if self.calibrate_after_close:
                    run_calibration("收盤後補跑校準")
                    self.calibrate_after_close = False
                if not self.web:
                    print("已收盤，監控結束。")
                    break
                # 網頁模式：收盤後維持服務，每 5 分鐘輕量更新一次
                time.sleep(300)
                continue
            time.sleep(max(5, self.interval - (time.time() - t0)))


def main():
    ap = argparse.ArgumentParser(description="台股當沖多空勝率監控")
    ap.add_argument("codes", nargs="+", help="股票代碼（可多檔）")
    ap.add_argument("--peers", default=None, help="自訂同族群代碼，逗號分隔；不給則查 groups.json")
    ap.add_argument("--interval", type=int, default=180, help="輪詢秒數（預設 180 = 3 分鐘）")
    ap.add_argument("--once", action="store_true", help="只跑一次")
    ap.add_argument("--json", action="store_true", help="輸出 JSON 行（給程式讀）")
    ap.add_argument("--verbose", action="store_true", help="印出每個因子的說明")
    ap.add_argument("--brief", action="store_true", help="精簡輸出")
    ap.add_argument("--no-shioaji", action="store_true", help="不使用永豐 API")
    ap.add_argument("--big-lot", type=int, default=0, help="固定大單門檻（張）；預設 0 = 依金額與當日分布自動推算")
    ap.add_argument("--big-amount", type=float, default=None, help="大單金額門檻（元），預設 .env 的 BIG_LOT_AMOUNT 或 500 萬")
    ap.add_argument("--refresh", action="store_true", help="重抓盤前資料（忽略今日快取）")
    ap.add_argument("--web", nargs="?", const=8770, type=int, default=None, metavar="PORT",
                    help="啟動本機網頁儀表板（預設 port 8770，只綁 127.0.0.1）")
    ap.add_argument("--no-open", action="store_true", help="--web 時不要自動開瀏覽器")
    a = ap.parse_args()
    if a.big_amount:
        os.environ["BIG_LOT_AMOUNT"] = str(int(a.big_amount))
    if a.web:
        start_web(a.web)
        if not a.no_open:
            try:
                webbrowser.open(f"http://127.0.0.1:{a.web}/")  # macOS / Windows / Linux 都用系統預設瀏覽器
            except Exception:
                pass
        if a.interval == 180:
            a.interval = 60  # 盯盤用：網頁模式預設 1 分鐘一輪
    m = Monitor(a.codes, a.peers, a.interval, not a.no_shioaji, a.big_lot, a.refresh, web=bool(a.web))
    try:
        m.run(a.once, a.json, a.verbose, a.brief)
    except KeyboardInterrupt:
        print("\n使用者中止。")
    finally:
        if m.flow_src:
            m.flow_src.logout()


if __name__ == "__main__":
    main()
