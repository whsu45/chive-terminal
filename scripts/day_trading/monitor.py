#!/usr/bin/env python3
"""
當沖多空監控：每 N 秒（預設 180）重算多空勝率、支撐壓力與進出場計畫，直到 Ctrl+C；
--web 模式另有 10 秒快迴圈更新江波圖、報價、分流與即時警示。

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
import http_client as hc   # 別名不能叫 http：下面 import http.server 會把它蓋掉
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
        self.live = {}        # 10 秒快迴圈：報價、分時序列、分流、即時警示

    def put_live(self, obj):
        with self.lock:
            self.live = obj

    def live_json(self):
        with self.lock:
            return json.dumps(self.live, ensure_ascii=False, default=str)

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
                    self.index[k] = {"ref": q["ref"], "y": q["y"], "chg": (q["ref"] / q["y"] - 1) * 100}
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
        if path == "/api/live":
            return self._send(WEB.live_json())
        if path.startswith("/api/history/"):
            return self._send(WEB.history_json(path.rsplit("/", 1)[-1]))
        self._send('{"error":"not found"}', code=404)


def _port_holder(port):
    """誰佔了這個 port → (pid, 指令) 或 None。macOS/Linux 用 lsof+ps，Windows 用 netstat+PowerShell。"""
    try:
        if os.name == "nt":
            out = subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True, timeout=5).stdout
            pid = None
            for ln in out.splitlines():
                parts = ln.split()
                if len(parts) >= 5 and parts[1].endswith(f":{port}") and parts[3].upper() == "LISTENING":
                    pid = parts[4]; break
            if not pid:
                return None
            cmd = subprocess.run(["powershell", "-NoProfile", "-Command",
                                  f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
                                 capture_output=True, text=True, timeout=8).stdout.strip()
            return int(pid), cmd
        out = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"], capture_output=True, text=True, timeout=3).stdout.split()
        if not out:
            return None
        pid = int(out[0])
        cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=3).stdout.strip()
        return pid, cmd
    except Exception:
        return None


def _kill(pid):
    """先禮後兵：TERM 等 3 秒，還在就 KILL。回傳是否結束。"""
    import signal
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=8)
        else:
            os.kill(pid, signal.SIGTERM)
            for _ in range(30):
                time.sleep(0.1)
                try:
                    os.kill(pid, 0)
                except OSError:
                    break
            else:
                os.kill(pid, signal.SIGKILL)
                time.sleep(0.3)
        return True
    except Exception as e:
        print(f"[web] 無法結束 PID {pid}：{e}")
        return False


def _short_cmd(cmd):
    """指令太長時只留 monitor.py 之後的部分（python 路徑沒資訊）。"""
    k = cmd.find("monitor.py")
    return cmd[k:][:70] if k >= 0 else cmd[-70:]


def start_web(port, tries=10):
    """
    綁 127.0.0.1:port。被佔時：佔用者若是另一個 monitor.py（上一次沒關的儀表板）就砍掉它、接管同一個 port；
    是別的程式就往後找（最多 tries 個）。回傳 (server, 實際 port)。
    """
    i = 0
    killed = False
    while i < tries:
        p = port + i
        try:
            srv = http.server.ThreadingHTTPServer(("127.0.0.1", p), Handler)
        except OSError as e:
            if e.errno not in (48, 98, 10048):   # EADDRINUSE：macOS / Linux / Windows
                raise
            who = _port_holder(p)
            if who and "monitor.py" in who[1] and who[0] != os.getpid() and not killed:
                print(f"[web] port {p} 被上一個 monitor 佔用（PID {who[0]}：{_short_cmd(who[1])}），結束它並接管")
                killed = _kill(who[0])
                if killed:
                    time.sleep(0.5)
                    continue          # 同一個 port 再試一次
            print(f"[web] port {p} 已被佔用" + (f"（PID {who[0]}：{_short_cmd(who[1])}）" if who else "") + "，改用下一個")
            i += 1; killed = False
            continue
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        print(f"[web] 儀表板：http://127.0.0.1:{p}/   （只綁本機，不對外）")
        return srv, p
    raise SystemExit(f"[web] {port}~{port + tries - 1} 都被佔用，請用 --web <其他 port>")

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
        self.index_stream = False
        self.live_alerts = {c: [] for c in codes}   # code -> [[HH:MM:SS, text], ...]（分流劇變、權證異動，10 秒迴圈產生）
        self.wq = {}                                 # code -> 最近一次權證盤中彙總（快迴圈每 60 秒更新）
        self.mis_bars = {c: {} for c in codes}       # 無 Shioaji 時用 MIS 報價自己湊分 K
        self.idx_bars = {"t00": {}, "o00": {}}       # 無指數串流時用 MIS 湊
        self.fast_thread = None
        self._last_wq_t = 0
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
                    try:
                        idx_ok = src_shioaji.start_index_stream()
                        self.index_stream = bool(idx_ok)
                        print(f"[shioaji] 指數串流 {idx_ok or '失敗，改用 MIS 10 秒取樣'}")
                    except Exception as e:
                        print(f"[shioaji] 指數串流失敗：{str(e)[:80]}")
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
        now = dt.datetime.now()
        try:
            quotes = src_mis.fetch(sorted(self.ex_codes))
        except hc.HttpError:
            if not self.flow_src:
                raise
            quotes = self._quotes_from_shioaji(now)
            if not quotes:
                raise
            print(f"[{now:%H:%M:%S}] MIS 失敗，這輪估算用 Shioaji 串流報價（無五檔/族群）")
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
            self._record_flow_hist(c, flow, q, now)
            wq = self.wq.get(c) if (self.fast_thread and c in self.wq) else self._warrant_snapshot(c, q, now, max(1, round(self.interval / 60)))
            ev = model.evaluate(d, q, idx, peers_q, flow, self.samples[c], now, warrants=wq)
            if wq and wq.get("alerts"):
                ev["warnings"] = wq["alerts"] + ev["warnings"]
            if self.fast_thread:
                ev["live_alerts"] = list(self.live_alerts.get(c, []))[-12:]
            WEB.put(ev, quotes)
            results.append(ev)
        return results

    # ------------------------------------------------------------ 10 秒快迴圈（圖表、報價、分流、即時警示）
    def _record_flow_hist(self, c, flow, q, now, min_gap=50):
        """每 ≥50 秒記一筆主力/散戶淨額，給 model._flow_dynamics 看劇變；兩個迴圈共用同一份。"""
        if not (flow and flow.get("volume")):
            return
        hh = self.flow_hist.setdefault(c, [])
        if not hh or now.timestamp() - hh[-1][0] >= min_gap:
            hh.append([now.timestamp(), flow["big_net"], flow["small_net"], q.get("ref")])
            del hh[:-120]
        flow["hist"] = list(hh)

    def _warrant_snapshot(self, c, q, now, interval_min):
        d = self.daily[c]
        wl = (d.get("warrants") or {}).get("list") or []
        if not wl:
            return None
        try:
            wdict = {"call": [w for w in wl if w["right"] == "call"], "put": [w for w in wl if w["right"] == "put"]}
            snap = W.intraday_detail(wdict)
            if not snap:
                return None   # MIS 偶發回空（限流），保留上一輪的快照，不要用零值蓋掉也不要誤發警示
            wq = self._aggregate(snap)
            stock_chg = ((q["ref"] / q["y"] - 1) * 100) if (q.get("ref") and q.get("y")) else None
            wq["alerts"] = W.alerts(snap, self.w_prev.get(c), stock_chg, (d.get("warrants") or {}).get("call_vol"), interval_min)
            self.w_prev[c] = snap
            return wq
        except Exception as e:
            print(f"  [warn] 權證盤中: {str(e)[:80]}")
            return None

    def _add_alert(self, c, now, text):
        """即時警示：同一句 5 分鐘內不重複，最多留 30 筆。"""
        L = self.live_alerts.setdefault(c, [])
        key = text[:24]
        for t, txt in L[-10:]:
            if txt[:24] == key and (now - dt.datetime.strptime(t, "%H:%M:%S").replace(year=now.year, month=now.month, day=now.day)).total_seconds() < 300:
                return
        L.append([now.strftime("%H:%M:%S"), text])
        del L[:-30]

    @staticmethod
    def _mis_bar(bars, now, price, cumvol):
        """用 MIS 報價湊分 K：同一分鐘內更新 h/l/c，量用累積量差。"""
        key = now.strftime("%H:%M")
        last_v = bars.get("_v", 0)
        dv = max(0, (cumvol or 0) - last_v) if "_v" in bars else 0
        bars["_v"] = cumvol or last_v
        b = bars.get(key)
        if b is None:
            bars[key] = [price, price, price, price, dv, price * dv]
        else:
            b[1] = max(b[1], price); b[2] = min(b[2], price); b[3] = price; b[4] += dv; b[5] += price * dv

    def _quotes_from_shioaji(self, now):
        """MIS 掛掉時用 Shioaji 串流狀態湊報價（個股與指數；沒有五檔、沒有族群）。"""
        out = {}
        for c in self.codes:
            bars = self.flow_src.series(c)
            last = self.flow_src.last_price(c)
            if not bars or not last:
                continue
            y = (self.daily[c].get("tech") or {}).get("close")
            out[c] = {"code": c, "name": self.daily[c]["name"], "ex": self.daily[c]["market"], "t": now.strftime("%H:%M:%S"),
                      "z": last, "ref": last, "o": bars[0][1], "h": max(b[2] for b in bars), "l": min(b[3] for b in bars),
                      "y": y, "v": sum(b[5] for b in bars), "bids": [], "asks": [], "trial": None, "src": "shioaji"}
        if self.index_stream:
            for k in ("t00", "o00"):
                last = self.flow_src.index_last(k)
                y = (WEB.live.get("index") or {}).get(k, {}).get("y") if WEB.live else None
                if last and y:
                    out[k] = {"code": k, "ref": last, "z": last, "y": y, "bids": [], "asks": []}
        return out

    def fast_tick(self):
        """一輪快更新：MIS 報價 → 分時序列、分流、分流劇變警示；每 60 秒順便更新權證快照與警示。
        MIS 失敗時改用 Shioaji 串流湊報價（圖表、分流不中斷，只少五檔與族群）。"""
        now = dt.datetime.now()
        try:
            quotes = src_mis.fetch(sorted(self.ex_codes))
            self._mis_fail = 0
        except hc.HttpError as e:
            self._mis_fail = getattr(self, "_mis_fail", 0) + 1
            if not self.flow_src:
                raise
            if self._mis_fail in (1, 10, 100):
                print(f"[fast] MIS 失敗（第 {self._mis_fail} 次），改用 Shioaji 串流報價：{str(e)[:100]}")
            quotes = self._quotes_from_shioaji(now)
            if not quotes:
                raise
        do_wq = time.time() - self._last_wq_t >= 60
        live = {"time": now.strftime("%H:%M:%S"), "phase": model.phase_of(now), "index": {}, "index_series": {},
                "quotes": {}, "series": {}, "flow": {}, "warrants": {}, "alerts": {},
                "shioaji": bool(self.flow_src), "index_stream": self.index_stream, "interval": self.interval,
                "mis_fail": getattr(self, "_mis_fail", 0)}
        for k in ("t00", "o00"):
            q = quotes.get(k)
            if q and q.get("y"):
                ref = q.get("ref")
                if self.index_stream:
                    ref = self.flow_src.index_last(k) or ref
                if ref:
                    live["index"][k] = {"ref": ref, "y": q["y"], "chg": (ref / q["y"] - 1) * 100}
                    if self.index_stream:
                        live["index_series"][k] = self.flow_src.index_series(k)
                    else:
                        self._mis_bar(self.idx_bars[k], now, ref, 0)
                        live["index_series"][k] = [[kk, v[3]] for kk, v in sorted(self.idx_bars[k].items()) if kk != "_v"]
        for c in self.codes:
            q = quotes.get(c)
            if not q or not q.get("ref"):
                continue
            if q.get("v") is not None:
                self.samples[c].append((now, q["ref"], q["v"]))
                if len(self.samples[c]) > 4000:
                    del self.samples[c][:1000]
            if q.get("z") is None and self.flow_src:
                # 沒成交價就用逐筆最後一筆（比五檔中點可靠）
                try:
                    last = self.flow_src.last_price(c)
                except Exception:
                    last = None
                if last:
                    q["ref"] = last
            if q.get("trial"):
                self._add_alert(c, now, f"⚠ 延緩撮合（瞬間價格穩定措施）：試算價 {q['trial']} 超出今日區間 {q.get('l')}–{q.get('h')}，2 分鐘後恢復撮合，波動即將放大，掛單先撤")
            lq = {k: q.get(k) for k in ("ref", "o", "h", "l", "v", "y", "bids", "asks", "name", "trial")}
            lq["chg_pct"] = (q["ref"] / q["y"] - 1) * 100 if q.get("y") else None
            live["quotes"][c] = lq
            if self.flow_src:
                live["series"][c] = self.flow_src.series(c)
            else:
                self._mis_bar(self.mis_bars[c], now, q["ref"], q.get("v"))
                live["series"][c] = [[kk] + v for kk, v in sorted(self.mis_bars[c].items()) if kk != "_v"]
            flow = self.flow_src.flow(c, price=q["ref"]) if self.flow_src else None
            if flow:
                self._record_flow_hist(c, flow, q, now)
                fF, fW = model._flow_dynamics(flow, now)
                for w in fW:
                    self._add_alert(c, now, w)
                live["flow"][c] = {k: v for k, v in flow.items() if k != "hist"}
            if do_wq:
                wq = self._warrant_snapshot(c, q, now, 1)
                if wq:
                    self.wq[c] = wq
                    for w in wq.get("alerts") or []:
                        self._add_alert(c, now, w)
            if self.wq.get(c):
                live["warrants"][c] = self.wq[c]
            live["alerts"][c] = list(self.live_alerts.get(c, []))[-12:]
        if do_wq:
            self._last_wq_t = time.time()
        WEB.put_live(live)
        return live

    def fast_loop(self, every=10):
        while True:
            t0 = time.time()
            backoff = 0
            try:
                self.fast_tick()
            except hc.HttpError as e:
                backoff = 20   # MIS 偶發回 HTML/限流：這輪跳過，多等 20 秒再試
                print(f"[fast] MIS 抓取失敗，20 秒後再試：{str(e)[:120]}")
            except Exception as e:
                print(f"[fast] {str(e)[:100]}")
            hhmm = dt.datetime.now().strftime("%H:%M")
            wait = 60 if (hhmm >= "13:35" or hhmm < "08:25") else every + backoff
            time.sleep(max(1, wait - (time.time() - t0)))

    def start_fast(self, every=10):
        self.fast_thread = threading.Thread(target=self.fast_loop, args=(every,), daemon=True)
        self.fast_thread.start()
        print(f"[fast] 圖表/報價/分流每 {every} 秒更新；多空估算與進出場每 {self.interval} 秒")

    def log(self, ev):
        path = os.path.join(LOG_DIR, f"monitor_{dt.date.today():%Y%m%d}.jsonl")
        rec = {k: v for k, v in ev.items() if k not in ("quote",)}
        rec["quote"] = {k: ev["quote"].get(k) for k in ("ref", "o", "h", "l", "v", "bids", "asks")}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    def run(self, once=False, as_json=False, verbose=False, brief=False):
        if self.web and not once:
            self.start_fast()
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
            except hc.HttpError as e:
                print(f"[{dt.datetime.now():%H:%M:%S}] 抓取失敗（這一輪跳過，下一輪再試）：{str(e)[:160]}")
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
        _, a.web = start_web(a.web)
        if not a.no_open:
            try:
                webbrowser.open(f"http://127.0.0.1:{a.web}/")  # macOS / Windows / Linux 都用系統預設瀏覽器
            except Exception:
                pass
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
