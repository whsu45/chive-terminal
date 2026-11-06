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
import indicators as I
import history
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
        self.interval = 60
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
        if path == "/api/watchlist":
            return self._send(json.dumps(history.all_summary(), ensure_ascii=False, default=str))
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
        self._last_mis_t = 0
        self._last_quotes = None
        self.peer_hist = {c: {} for c in codes}     # code -> {peer: [[epoch, chg_pct], ...]}，給 model 算族群 10 分鐘趨勢
        self.signals = {}                            # code -> 目前的「建議做空/做多」訊號（快迴圈每 3 秒檢查進場確認條件）
        self.walls = {}                              # code -> {"bid": 牆, "ask": 牆}：五檔裡特別大的那一檔，追蹤被吃/被撤/補單判真假
        self._fs_cache = {}                          # code -> (epoch, 每分鐘主力/散戶累積淨額序列)，10 秒重算一次給「大戶散戶」圖
        self.pending = []                            # 警示結果追蹤：每則 ⚠/▶ 記下當時狀態，10/30 分鐘後補上漲跌，寫 logs/alert_outcomes.jsonl
        self._tracked = set()
        self._sig_seen = {}                          # (code, side, px) -> epoch，同一個點 15 分鐘內不重發
        import src_shioaji
        for c in codes:
            self.daily[c] = prep.build(c, peers_arg, refresh)
            print(prep.describe(self.daily[c]))
            dd = self.daily[c]
            WEB.daily[c] = {k: dd.get(k) for k in ("code", "name", "market", "group", "peers", "limit_up", "limit_down", "tech", "flags", "insti_sum", "margin", "sbl")}
            try:
                he = history.record_launch(c, dd.get("name"), dd.get("market"), dd.get("group"))
                dd["history"] = history.summary(c, he)
                WEB.daily[c]["history"] = dd["history"]
                hs = dd["history"]
                print(f"  交戰紀錄：監控 {hs['days']} 天（首次 {hs['first']}）、建議做空 {hs['short']['win']}勝{hs['short']['loss']}敗、做多 {hs['long']['win']}勝{hs['long']['loss']}敗"
                      + (f"；常見失誤：{'、'.join(r for r, _ in hs['loss_reasons'])}" if hs['loss_reasons'] else ""))
            except Exception as e:
                print(f"  [warn] 交戰紀錄: {str(e)[:80]}")
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
                    self._backfill_flow(ok)
                    try:
                        idx_ok = src_shioaji.start_index_stream()
                        self.index_stream = bool(idx_ok)
                        print(f"[shioaji] 指數串流 {idx_ok or '失敗，改用 MIS 10 秒取樣'}")
                    except Exception as e:
                        print(f"[shioaji] 指數串流失敗：{str(e)[:80]}")
                    u = src_shioaji.usage()
                    print(f"[shioaji] 串流已啟動 {ok}" + (f"，今日流量 {u['used_mb']:.0f}/{u['limit_mb']:.0f} MB" if u else "")
                          + f"；今日 ticks 已用 {src_shioaji.ticks_usage()}/{src_shioaji.TICKS_DAILY_LIMIT}（每次啟動每檔用 1 次）")
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
            self._record_peers(c, peers_q, now)
            if self.flow_src:
                self._overlay_shioaji(c, q)
            flow = self.flow_src.flow(c, price=q.get("ref")) if self.flow_src else None
            self._record_flow_hist(c, flow, q, now)
            wq = self.wq.get(c) if (self.fast_thread and c in self.wq) else self._warrant_snapshot(c, q, now, max(1, round(self.interval / 60)))
            ev = model.evaluate(d, q, idx, peers_q, flow, self.samples[c], now, warrants=wq, ctx=self._ctx(c, now))
            if wq and wq.get("alerts"):
                ev["warnings"] = wq["alerts"] + ev["warnings"]
            if self.fast_thread:
                ev["live_alerts"] = list(self.live_alerts.get(c, []))[-12:]
            WEB.put(ev, quotes)
            results.append(ev)
        return results

    def _backfill_flow(self, codes):
        """盤中才啟動：用回補的逐筆重建 09:00 起每分鐘的主力/散戶淨額，並把那段時間該出的分流警示補發（帶原本的時間戳）。"""
        for c in codes:
            try:
                hist = self.flow_src.flow_history(c)
            except Exception as e:
                print(f"[shioaji] {c} 重建流量歷史失敗：{str(e)[:80]}"); continue
            if len(hist) < 3:
                continue
            self.flow_hist[c] = hist[-120:]
            n_alert = 0
            for i in range(2, len(hist)):
                ts = dt.datetime.fromtimestamp(hist[i][0])
                _, fW = model._flow_dynamics({"hist": hist[max(0, i - 15):i + 1], "volume": hist[i][4]}, ts)
                for w in fW:
                    before = len(self.live_alerts.get(c, []))
                    self._add_alert(c, ts, w)
                    n_alert += len(self.live_alerts.get(c, [])) - before
            t0 = dt.datetime.fromtimestamp(hist[0][0]).strftime("%H:%M"); t1 = dt.datetime.fromtimestamp(hist[-1][0]).strftime("%H:%M")
            print(f"[shioaji] {c} 回補 {t0}–{t1} 主力/散戶流量 {len(hist)} 分鐘，補發歷史警示 {n_alert} 則")

    @staticmethod
    def _five_min(completed):
        """1 分 K → 已完成的 5 分 K [[HH:MM, o,h,l,c,vol], ...]（最後一個 5 分鐘桶若還沒結束就不算）。"""
        buckets = {}
        for b in completed:
            hh, mm = b[0].split(":")
            k = f"{hh}:{int(mm) // 5 * 5:02d}"
            x = buckets.get(k)
            if x is None:
                buckets[k] = [k, b[1], b[2], b[3], b[4], b[5] or 0]
            else:
                x[2] = max(x[2], b[2]); x[3] = min(x[3], b[3]); x[4] = b[4]; x[5] += b[5] or 0
        out = [buckets[k] for k in sorted(buckets)]
        if out and completed:
            hh, mm = completed[-1][0].split(":")
            if int(mm) % 5 != 4:      # 這個 5 分鐘桶還沒收完（最後一根不是 x4 分）
                out = out[:-1]
        return out

    @staticmethod
    def _entry_zone(side, base, stop, t1, rr_min=1.5):
        """
        進場區（限價掛單範圍）：做多 = [觸發價（base 下 1 tick）, base + 1/3 停損距離]，上緣再壓到報酬風險比 ≥ rr_min 的價位；
        做空對稱。區間不成立（目標太近）回 None。
        例：11/06 廣達 進場 280.5、停損 278、目標 290 → 280.0–281.0（原本 09:34 掛在 283，報酬風險比 1.4）。
        """
        tick = I.tick_size(base)
        if side == "long":
            risk = base - stop
            if risk <= 0 or t1 <= base:
                return None
            hi = min(I.round_tick(base + max(tick, risk / 3), "down"), I.round_tick((t1 + rr_min * stop) / (1 + rr_min), "down"))
            lo = I.round_tick(base - tick, "down")
            return (lo, hi) if hi >= lo else None
        risk = stop - base
        if risk <= 0 or t1 >= base:
            return None
        lo = max(I.round_tick(base - max(tick, risk / 3), "up"), I.round_tick((t1 + rr_min * stop) / (1 + rr_min), "up"))
        hi = I.round_tick(base + tick, "up")
        return (lo, hi) if lo <= hi else None

    def _signal(self, c, ev, bars, q, now):
        """
        「建議做空 / 做多」：模型那一邊勝率 ≥60%，而且進場確認條件**已經發生**，才掛出來：
          主計畫：最近 12 根 1 分 K 碰過壓力（支撐），且最後一根完成的 1 分 K 收在進場價之下（之上），現價還在進場價附近、沒到目標也沒過停損；
          備援：最後一根完成的 5 分 K 帶量（≥1.5 倍前 6 根均量）收破支撐 / 收過壓力，現價離觸發價 1% 內。
        反彈動能中不給空訊號、殺盤動能中不給多訊號；09:10 前、13:20 後不給。
        訊號會留著直到：到目標 1（✔）、過停損（✘）、或 20 分鐘逾時，結束後再顯示 2 分鐘。
        追價上限（11/06：6 個訊號掛出時已從停損往目標走了 25–75%，兩筆輸的報酬風險比都 <1）：
          進場區 = 觸發價 ～ 進場價 + 1/3 停損距離，且在區內任何價位報酬風險比 ≥1.5（_entry_zone）；
          現價在區內 → 直接「建議做多 @現價」；已追出區外 → 「建議做多 掛 X–Y（等回）」，價格回到區內才算成交，
          先到目標 1 / 20 分鐘沒回 / 13:05 後 → 未成交，不算輸贏。整個區都湊不到報酬風險比 1.5（目標太近）就不發。
        """
        p = q.get("ref")
        cur = self.signals.get(c)
        if cur and p:
            if not cur.get("status"):
                short = cur["side"] == "short"
                if not cur.get("filled", True):
                    # 等回掛單：價格回到進場區才算進場；先到目標 1、20 分鐘沒回來、13:05 後都算未成交（不算輸贏）
                    lo, hi = cur["zone"]
                    if (p >= lo) if short else (p <= hi):
                        cur.update(filled=True, fill_px=p, fill_ts=now.timestamp(), price=p)
                        self._add_alert(c, now, f"▶ 掛單成交 {cur['label']} @{p}（進場區 {lo}–{hi}）：停損 {cur['stop']}，目標 {cur['t1']} / {cur['t2']}")
                    elif (p <= cur["t1"]) if short else (p >= cur["t1"]):
                        cur["status"] = f"○ 未成交：沒回到 {lo}–{hi} 就到目標 1，放棄"
                    elif now.timestamp() - cur["since_ts"] > 1200:
                        cur["status"] = f"○ 未成交：20 分鐘沒回到 {lo}–{hi}，取消"
                    elif now.strftime("%H:%M") >= "13:05":
                        cur["status"] = "○ 未成交：13:05 後不進場，取消"
                elif (p <= cur["t1"]) if short else (p >= cur["t1"]):
                    cur["status"] = f"✔ 到目標 1 {cur['t1']}"
                elif (p >= cur["stop"]) if short else (p <= cur["stop"]):
                    cur["status"] = f"✘ 過停損 {cur['stop']}，失效"
                elif now.timestamp() - (cur.get("fill_ts") or cur["since_ts"]) > 1200:
                    cur["status"] = "逾時 20 分鐘，訊號取消"
                if cur.get("status"):
                    cur["end_ts"] = now.timestamp()
                    self._add_alert(c, now, f"{cur['status']}：{cur['label']} @{cur['price']}")
            if cur.get("status") and now.timestamp() - cur["end_ts"] > 120:
                del self.signals[c]
                cur = None
            if cur:
                cur["now"] = p
                return cur
        if not (ev and p and bars and len(bars) >= 4):
            return None
        hhmm = now.strftime("%H:%M")
        if hhmm < "09:10" or hhmm >= "13:05":
            return None   # 13:05 起當沖平倉主導，不照一般劇本開新倉（只處理出場）
        completed = [b for b in bars if b[0] != now.strftime("%H:%M")]
        walls = self.walls.get(c, {})
        def real_wall(key):
            w = walls.get(key)
            return w if (w and not w.get("status") and str(w.get("verdict", "")).startswith("真")) else None
        if len(completed) < 3:
            return None
        last = completed[-1]
        recent = completed[-12:]
        f5 = self._five_min(completed)
        plan = ev.get("plan") or {}
        info = ev.get("info") or {}
        mom = (info.get("momentum") or {}).get("dir")
        tick = I.tick_size(p)
        ps, pl = ev.get("p_short", 0), ev.get("p_long", 0)

        def vol_ok():
            if len(f5) < 4:
                return False
            prev = [x[5] for x in f5[-7:-1]]
            return bool(prev) and f5[-1][5] >= 1.5 * (sum(prev) / len(prev))

        # 午盤量縮時 1 分 K 的「確認」沒有意義（11/05 晶豪科 12:03 五分鐘只成交 118 張）：最近 3 根合計要有 20 根均量的一半
        v20 = [b[5] or 0 for b in completed[-20:]]
        avg20 = sum(v20) / len(v20) if v20 else 0
        thin = avg20 > 0 and sum((b[5] or 0) for b in completed[-3:]) < 1.5 * avg20

        def make(side, base, label, stop, t1, t2, why):
            z = self._entry_zone(side, base, stop, t1)
            if not z:
                return None       # 進場區內湊不到報酬風險比 1.5：目標太近，不發
            lo, hi = z
            key = (c, side, lo, hi)
            if now.timestamp() - self._sig_seen.get(key, 0) < 900:
                return None
            self._sig_seen[key] = now.timestamp()
            short = side == "short"
            inzone = (p >= lo) if short else (p <= hi)
            lim = lo if short else hi
            risk = abs(p - stop)
            rr_now = abs(t1 - p) / risk if risk else None
            rr_zone = abs(t1 - lim) / abs(lim - stop) if lim != stop else None
            chase = ((stop - p) / (stop - t1)) if short else ((p - stop) / (t1 - stop))
            sig = {"side": side, "price": p if inzone else lim, "label": label, "stop": stop, "t1": t1, "t2": t2, "why": why,
                   "since": now.strftime("%H:%M:%S"), "since_ts": now.timestamp(), "now": p,
                   "p": ps if short else pl, "zone": [lo, hi], "filled": inzone, "filled_at_alert": inzone,
                   "fill_px": p if inzone else None, "fill_ts": now.timestamp() if inzone else None,
                   "alert_px": p, "chase": round(chase, 3), "rr_now": rr_now and round(rr_now, 2), "rr_zone": rr_zone and round(rr_zone, 2)}
            self.signals[c] = sig
            if inzone:
                self._add_alert(c, now, f"▶ 建議{label} @{p}：{why}；停損 {stop}，目標 {t1} / {t2}（報酬風險比 {rr_now:.1f}）")
            else:
                self._add_alert(c, now, f"▶ 建議{label} 掛 {lo}–{hi}（現價 {p} 已{'殺低' if short else '追高'}，報酬風險比只剩 {rr_now:.1f}，等回再進）："
                                        f"{why}；停損 {stop}，目標 {t1} / {t2}")
            return sig

        s = plan.get("short")
        if s and ps >= 60 and ps > pl and mom != "up" and not (info.get("warrant_surge_5m") and (info.get("book_imb") or 0) > 0.35):
            trig, entry, stop, t1, t2 = s["trigger_px"], s["entry_px"], s["stop"], s["t1"], s["t2"]
            touched = any(b[2] >= trig - tick for b in recent)
            rw = real_wall("bid")
            if rw and t1 < rw["px"] < p:
                touched = False   # 真支撐擋在目標前面，空單不發
            if touched and not thin and last[4] <= entry and t1 < p <= entry + tick and p < stop:
                return make("short", entry, "做空", stop, t1, t2, f"反彈 {trig} 不過，1 分 K 收 {last[4]} 確認（空方 {ps:.0f}%）")
            bd = s.get("bd_trigger")
            if bd and f5 and f5[-1][4] < bd and vol_ok() and s.get("bd_target") and s["bd_target"] < p < bd and (bd - p) / p < 0.01:
                return make("short", bd, "做空（破線追）", s["bd_stop"], s["bd_target"], s["bd_target"], f"5 分 K 帶量收破 {bd}（量 {f5[-1][5]} 張，空方 {ps:.0f}%）")
        l = plan.get("long")
        if l and pl >= 60 and pl > ps and mom != "down":
            trig, entry, stop, t1, t2 = l["trigger_px"], l["entry_px"], l["stop"], l["t1"], l["t2"]
            touched = any(b[3] <= trig + tick for b in recent)
            rw = real_wall("ask")
            if rw and p < rw["px"] < t1:
                touched = False
            if touched and not thin and last[4] >= entry and entry - tick <= p < t1 and p > stop:
                return make("long", entry, "做多", stop, t1, t2, f"回測 {trig} 守住，1 分 K 收 {last[4]} 確認（多方 {pl:.0f}%）")
            bo = l.get("bo_trigger")
            if bo and f5 and f5[-1][4] > bo and vol_ok() and l.get("bo_target") and bo < p < l["bo_target"] and (p - bo) / p < 0.01:
                return make("long", bo, "做多（突破追）", l["bo_stop"], l["bo_target"], l["bo_target"], f"5 分 K 帶量收過 {bo}（量 {f5[-1][5]} 張，多方 {pl:.0f}%）")
        return None

    ALERT_TYPES = [("認購權證急漲", r"認購權證.*急漲"), ("認購權證急跌", r"認購權證.*急跌"), ("認售權證急漲", r"認售權證.*急漲"),
                   ("權證爆量", r"權證.*成交.*張"), ("主力急回補", r"主力 \d+ 分鐘內回補"), ("主力急出", r"主力 \d+ 分鐘內出掉"),
                   ("主力加速出貨", r"再倒"), ("主力加速吃貨", r"再買"), ("主力拉高出貨", r"拉高出貨"),
                   ("反彈動能", r"反彈動能"), ("殺盤動能", r"殺盤動能"), ("權證急漲+委買厚", r"權證急漲 \+ 委買"),
                   ("建議做空", r"▶ 建議做空"), ("建議做多", r"▶ 建議做多"), ("掛單成交", r"▶ 掛單成交"), ("延緩撮合", r"延緩撮合")]

    @classmethod
    def _alert_type(cls, text):
        import re
        for name, pat in cls.ALERT_TYPES:
            if re.search(pat, text):
                return name
        return "其他"

    def _track_alerts(self, c, q, flow, bars, now):
        """
        警示結果追蹤（校準用）：每則新警示記下當時價格、日內位置、主力淨額與 10 分鐘變化、五檔不平衡；
        之後用分 K 補上 10 / 30 分鐘後的價格與期間最高最低，寫到 logs/alert_outcomes.jsonl。
        收盤（13:31）後還沒滿 30 分鐘的用現有資料結案並標 partial。alert_review.py 讀它統計各類警示的勝率。
        """
        p = q.get("ref")
        if not p:
            return
        hist = self.flow_hist.get(c) or []
        big_now = flow["big_net"] if flow else None
        big_10 = None
        if big_now is not None and hist:
            past = [r for r in hist if now.timestamp() - r[0] >= 540]
            if past:
                big_10 = big_now - past[-1][1]
        bids, asks = q.get("bids") or [], q.get("asks") or []
        bv = sum(v for _, v in bids); av = sum(v for _, v in asks)
        for t, txt in self.live_alerts.get(c, []):
            key = (c, t, txt[:24])
            if key in self._tracked:
                continue
            self._tracked.add(key)
            try:
                hh, mm, ss = t.split(":")
                ts = now.replace(hour=int(hh), minute=int(mm), second=int(ss), microsecond=0)
            except Exception:
                continue
            if (now - ts).total_seconds() > 120:
                continue   # 啟動時補發的歷史警示沒有當時狀態，不追蹤
            typ = self._alert_type(txt)
            extra = {}
            if typ in ("建議做空", "建議做多"):
                sg = self.signals.get(c) or {}
                ev = WEB.latest.get(c) or {}
                inf = ev.get("info") or {}
                walls = self.walls.get(c, {})
                extra = {"sig": {k: sg.get(k) for k in ("side", "price", "stop", "t1", "t2", "why", "label", "zone", "filled_at_alert", "alert_px", "chase", "rr_now", "rr_zone")},
                         "features": {"idx_trend_10m": inf.get("idx_trend_10m"), "group_trend_10m": inf.get("group_trend_10m"),
                                      "momentum": (inf.get("momentum") or {}).get("dir"), "vol_pace": inf.get("vol_pace"),
                                      "rs_vs_index": inf.get("rs_vs_index"), "group_avg": inf.get("group_avg"), "chg_pct": inf.get("chg_pct"),
                                      "warrant_surge": inf.get("warrant_surge_5m"), "small_net_pct": inf.get("small_net_pct"),
                                      "p_long": ev.get("p_long"), "p_short": ev.get("p_short"),
                                      "wall_bid": {k: walls["bid"].get(k) for k in ("px", "verdict", "last")} if walls.get("bid") and not walls["bid"].get("status") else None,
                                      "wall_ask": {k: walls["ask"].get(k) for k in ("px", "verdict", "last")} if walls.get("ask") and not walls["ask"].get("status") else None},
                         "factors": [f[0] for f in (ev.get("intraday_factors") or [])]}
            self.pending.append({"date": now.strftime("%Y-%m-%d"), "code": c, "name": self.daily[c]["name"], "time": t, "ts": ts.timestamp(),
                                 "type": typ, "text": txt[:120], "price": p, "src": self.src, **extra,
                                 "chg_pct": (p / q["y"] - 1) * 100 if q.get("y") else None,
                                 "range_pos": ((p - q["l"]) / (q["h"] - q["l"])) if (q.get("h") and q.get("l") and q["h"] != q["l"]) else None,
                                 "big_net": big_now, "big_net_10m": big_10, "small_net": flow["small_net"] if flow else None,
                                 "book_imb": (bv - av) / (bv + av) if bv + av else None,
                                 "phase": model.phase_of(now)["key"], "out": {}})
        if not self.pending:
            return
        bmap = {}
        for b in bars or []:
            try:
                hh, mm = b[0].split(":")
                bmap[int(hh) * 60 + int(mm)] = b
            except Exception:
                pass
        closed = now.strftime("%H:%M") >= "13:31"
        keep = []
        for rec in self.pending:
            if rec["code"] != c:
                keep.append(rec); continue
            t0 = dt.datetime.fromtimestamp(rec["ts"])
            m0 = t0.hour * 60 + t0.minute
            age = (now - t0).total_seconds()
            for horizon in (10, 30):
                k = f"{horizon}m"
                if k in rec["out"] or (age < horizon * 60 and not closed):
                    continue
                win = [bmap[m] for m in range(m0 + 1, m0 + horizon + 1) if m in bmap]
                if not win:
                    continue
                px = win[-1][4]
                rec["out"][k] = {"px": px, "ret_pct": (px / rec["price"] - 1) * 100,
                                 "max_pct": (max(b[2] for b in win) / rec["price"] - 1) * 100,
                                 "min_pct": (min(b[3] for b in win) / rec["price"] - 1) * 100,
                                 "partial": age < horizon * 60}
            # 等回掛單的訊號：最多 20 分鐘等成交 + 成交後 30 分鐘，所以等滿 50 分鐘才結案
            need = 3000 if (rec.get("sig") and not rec["sig"].get("filled_at_alert", True)) else 0
            if ("30m" in rec["out"] and age >= need) or (closed and age > 60):
                if rec.get("sig"):
                    self._judge_signal(rec, bmap, m0)
                with open(os.path.join(LOG_DIR, "alert_outcomes.jsonl"), "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            else:
                keep.append(rec)
        self.pending = keep

    @staticmethod
    def _suspects(side, feat):
        """訊號失敗時的可疑原因（當時就看得到的條件）。"""
        S = []
        f = feat or {}
        short = side == "short"
        it, gt = f.get("idx_trend_10m"), f.get("group_trend_10m")
        if it is not None and ((it > 0.2) if short else (it < -0.2)):
            S.append("大盤 10 分鐘反向")
        if gt is not None and ((gt > 0.3) if short else (gt < -0.3)):
            S.append("族群 10 分鐘反向")
        if f.get("momentum") == ("up" if short else "down"):
            S.append("進場時有反向動能")
        bi = f.get("book_imb")
        if bi is not None and ((bi > 0.3) if short else (bi < -0.3)):
            S.append("五檔反向厚")
        if f.get("warrant_surge") and short:
            S.append("權證急漲中")
        b10 = f.get("big_net_10m")
        if b10 is not None and ((b10 > 0) if short else (b10 < 0)):
            S.append("主力 10 分鐘反向")
        sn = f.get("small_net_pct")
        if sn is not None and ((sn < -3) if short else (sn > 3)):
            S.append("散戶已同向擁擠")
        if f.get("phase") == "late":
            S.append("尾盤")
        vp = f.get("vol_pace")
        if vp is not None and vp < 0.6:
            S.append("量縮")
        rp = f.get("range_pos")
        if rp is not None and ((rp < 0.15) if short else (rp > 0.85)):
            S.append("已在日內極端位置追單")
        w = f.get("wall_bid" if short else "wall_ask")
        if w and str(w.get("verdict", "")).startswith("真"):
            S.append("真牆擋在前面")
        return S

    def _judge_signal(self, rec, bmap, m0):
        """建議訊號的輸贏：成交後 30 分鐘內先到目標 1 = win、先到停損 = loss、都沒到 = flat；
        等回掛單的訊號 20 分鐘內 1 分 K 低點碰到進場區上緣（做空：高點碰到下緣）才算成交、成交價用掛單價；
        沒成交（含先到目標 1）= nofill，不算輸贏。
        成交後 5 分鐘內反向走掉停損距離一半以上 = fast_adverse（估錯，即使之後賺回來也要記）。"""
        sg = rec["sig"]; side = sg.get("side")
        stop, t1 = sg.get("stop"), sg.get("t1")
        if not (side and stop and t1 and rec.get("price")):
            return
        short = side == "short"
        zone = sg.get("zone")
        if zone and sg.get("filled_at_alert") is False:
            lim = zone[0] if short else zone[1]
            start = None
            for i in range(1, 21):
                b = bmap.get(m0 + i)
                if not b:
                    continue
                if (b[2] >= lim) if short else (b[3] <= lim):
                    start = i; break
                if (b[3] <= t1) if short else (b[2] >= t1):
                    break          # 沒回到進場區就到目標：沒追到
            px = lim
        else:
            start, px = 1, (sg.get("price") or rec["price"])
        first_stop = first_t1 = None
        adverse5 = 0.0
        if start is not None:
            filled_later = start > 1 or (zone and sg.get("filled_at_alert") is False)
            for i in range(start, start + 30):
                b = bmap.get(m0 + i)
                if not b:
                    continue
                hi, lo = b[2], b[3]
                same_bar = filled_later and i == start      # 成交那根 K 先不算到目標（不知道先後），停損照算
                if short:
                    if first_stop is None and hi >= stop: first_stop = i
                    if first_t1 is None and lo <= t1 and not same_bar: first_t1 = i
                    if i < start + 5: adverse5 = max(adverse5, hi - px)
                else:
                    if first_stop is None and lo <= stop: first_stop = i
                    if first_t1 is None and hi >= t1 and not same_bar: first_t1 = i
                    if i < start + 5: adverse5 = max(adverse5, px - lo)
        if start is None:
            verdict = "nofill"
        elif first_t1 is not None and (first_stop is None or first_t1 < first_stop):
            verdict = "win"
        elif first_stop is not None:
            verdict = "loss"
        else:
            verdict = "flat"
        risk = abs(stop - px)
        fast_adverse = bool(start is not None and risk and adverse5 >= 0.5 * risk)
        feat = dict(rec.get("features") or {})
        feat.update({"book_imb": rec.get("book_imb"), "big_net_10m": rec.get("big_net_10m"), "small_net_pct": feat.get("small_net_pct", rec.get("small_net")),
                     "phase": rec.get("phase"), "range_pos": rec.get("range_pos")})
        suspects = self._suspects(side, feat) if (verdict == "loss" or fast_adverse) else []
        rec["verdict"] = verdict; rec["fast_adverse"] = fast_adverse; rec["adverse5"] = adverse5
        rec["first_stop_min"] = first_stop; rec["first_t1_min"] = first_t1; rec["suspects"] = suspects
        out = {"date": rec["date"], "time": rec["time"], "src": rec.get("src"), "side": side, "label": sg.get("label"), "price": px, "stop": stop, "t1": t1, "t2": sg.get("t2"),
               "zone": zone, "alert_px": sg.get("alert_px", rec["price"]), "filled_at_alert": sg.get("filled_at_alert"), "fill_min": start,
               "chase": sg.get("chase"), "rr_now": sg.get("rr_now"), "rr_zone": sg.get("rr_zone"),
               "verdict": verdict, "fast_adverse": fast_adverse, "first_stop_min": first_stop, "first_t1_min": first_t1,
               "ret_10m": (rec["out"].get("10m") or {}).get("ret_pct"), "ret_30m": (rec["out"].get("30m") or {}).get("ret_pct"),
               "suspects": suspects, "why": sg.get("why"), "features": feat, "factors": rec.get("factors")}
        try:
            with open(os.path.join(LOG_DIR, "signal_outcomes.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps({"code": rec["code"], "name": rec.get("name"), **out}, ensure_ascii=False) + "\n")
            history.record_signal(rec["code"], out)
            wl = {"win": "✔ 贏", "loss": "✘ 輸", "flat": "— 平", "nofill": "○ 未成交"}[verdict] + ("（5 分鐘內反向，估錯）" if fast_adverse else "")
            self._add_alert(rec["code"], dt.datetime.now(), f"{wl}：{sg.get('label')} @{px}" + (f"，可疑原因：{'、'.join(suspects)}" if suspects else ""))
        except Exception as e:
            print(f"[fast] 訊號結案寫入失敗：{str(e)[:80]}")

    def _track_walls(self, c, q, flow, now):
        """
        掛單牆追蹤（11/05 晶豪科 13:00 215 掛 103 張）：五檔中最大的一檔若 ≥2 倍其他四檔平均且 ≥max(30, 0.5% 今量) 就開始追，
        每 3 秒記：現在大小、出現後在該價成交多少（被吃）、掛單減少卻沒成交的量（撤單）、被吃後補回的量（補單）。
        真支撐 = 被吃 ≥30% 仍在且撤單少；假支撐 = 價格靠近時撤掉、幾乎沒成交；吃穿 = 價格穿過牆。狀態變化時發 ⚠。
        """
        p = q.get("ref")
        if not p:
            return
        vol = (flow or {}).get("volume") or q.get("v") or 0
        min_sz = max(30, 0.005 * vol)
        W = self.walls.setdefault(c, {})
        out = {}
        for side, key, lab in (("bids", "bid", "買單牆"), ("asks", "ask", "賣單牆")):
            levels = q.get(side) or []
            w = W.get(key)
            if w and w.get("status") and now.timestamp() - w["end_ts"] > 180:
                W.pop(key, None); w = None
            if w and not w.get("status"):
                cur = dict(levels).get(w["px"])
                traded = self.flow_src.traded_at(c, w["px"], w["since_ts"])[0] if self.flow_src else 0
                d_tr = traded - w["traded"]
                if cur is None:
                    cur = 0
                drop = w["last"] - cur
                if drop > 0:
                    w["pulled"] += max(0, drop - d_tr)
                elif drop < 0:
                    w["refilled"] += -drop
                w["traded"], w["last"], w["now"] = traded, cur, p
                through = (p < w["px"]) if key == "bid" else (p > w["px"])
                if through and traded >= 0.5 * w["size0"]:
                    w["status"] = f"✘ 被吃穿（成交 {traded} 張）"
                elif cur <= 0.2 * w["size0"] and traded >= 0.5 * w["size0"] and not through:
                    w["status"] = f"被吃光但價守住（成交 {traded} 張，補回 {w['refilled']}）：牆沒了，下一檔才是支撐" if key == "bid" else f"被吃光但價沒過（成交 {traded} 張）：牆沒了，上一檔才是壓力"
                elif cur <= 0.2 * w["size0"] and traded < 0.3 * w["size0"] and not through:
                    w["status"] = f"✘ 撤單（成交只 {traded} 張，少掉 {w['pulled']} 張沒成交）→ 假{ '支撐' if key=='bid' else '壓力'}"
                elif through:
                    w["status"] = "離開五檔"
                if w.get("status"):
                    w["end_ts"] = now.timestamp()
                    self._add_alert(c, now, f"⚠ {lab} {w['px']}（{w['size0']} 張）{w['status']}")
                else:
                    # 破線預告：被吃超過六成、剩不到四成、補單補不回來 → 牆快沒了
                    if w["traded"] >= 0.6 * w["size0"] and cur <= 0.4 * w["size0"] and w["refilled"] < 0.3 * w["size0"] and not w.get("warned_thin"):
                        w["warned_thin"] = True
                        self._add_alert(c, now, f"⚠ {lab} {w['px']} 快被吃完（{w['size0']} → 剩 {cur} 張，成交 {w['traded']}、只補 {w['refilled']}）："
                                        + ("破線在即，多單先減、空單準備順勢" if key == "bid" else "突破在即，空單先減、多單準備順勢"))
                    if w.get("warned_thin"):
                        w["verdict"] = f"被吃到剩 {cur} 張，快{'破' if key == 'bid' else '過'}"
                    elif w["traded"] >= 0.3 * w["size0"] and w["pulled"] < 0.3 * w["size0"] and (cur >= 0.5 * w["size0"] or w["refilled"] >= 0.3 * w["size0"]):
                        v = "真" + ("支撐" if key == "bid" else "壓力") + "傾向"
                        if w.get("verdict") != v:
                            self._add_alert(c, now, f"⚠ {lab} {w['px']} 被打到成交 {w['traded']} 張仍掛 {cur} 張（補回 {w['refilled']}）：{v}，" + ("空單目標別設在它下面" if key == "bid" else "多單目標別設在它上面"))
                        w["verdict"] = v
                    elif w["pulled"] >= 0.5 * w["size0"] and w["traded"] < 0.2 * w["size0"]:
                        w["verdict"] = "假" + ("支撐" if key == "bid" else "壓力") + "傾向（撤單中）"
                    elif w["traded"] < 0.1 * w["size0"]:
                        w["verdict"] = "未測試"
                    else:
                        w["verdict"] = "測試中"
            elif not w and len(levels) >= 3:
                sizes = [v for _, v in levels]
                mx = max(sizes); i = sizes.index(mx)
                others = [v for j, v in enumerate(sizes) if j != i]
                avg = sum(others) / len(others) if others else 0
                if mx >= min_sz and mx >= 2 * avg:
                    w = {"px": levels[i][0], "size0": mx, "last": mx, "since": now.strftime("%H:%M:%S"), "since_ts": now.timestamp(),
                         "traded": 0, "pulled": 0, "refilled": 0, "verdict": "未測試", "status": None, "now": p, "level": i + 1}
                    W[key] = w
            if w:
                out[key] = w
        return out

    def _record_peers(self, c, peers_q, now):
        """每次拿到族群報價就記一筆漲跌 %（≥20 秒一筆，留 90 分鐘），給 model 算族群 10 分鐘趨勢。"""
        ph = self.peer_hist.setdefault(c, {})
        ts = now.timestamp()
        for code, pq in (peers_q or {}).items():
            if not (pq.get("ref") and pq.get("y")):
                continue
            L = ph.setdefault(code, [])
            if L and ts - L[-1][0] < 20:
                continue
            L.append([ts, (pq["ref"] / pq["y"] - 1) * 100])
            del L[:-300]

    def _ctx(self, c, now):
        """給 model.evaluate 的脈絡：個股 1 分 K、指數分時、族群歷史、即時警示（epoch）。"""
        mkt = "o00" if self.daily[c]["market"] == "otc" else "t00"
        if self.flow_src:
            bars = self.flow_src.series(c)
        else:
            bars = [[kk] + v for kk, v in sorted(self.mis_bars[c].items()) if kk != "_v"]
        if self.index_stream:
            idx_series = {k: self.flow_src.index_series(k) for k in ("t00", "o00")}
        else:
            idx_series = {k: [[kk, v[3]] for kk, v in sorted(self.idx_bars[k].items()) if kk != "_v"] for k in ("t00", "o00")}
        alerts = []
        for t, txt in self.live_alerts.get(c, []):
            try:
                hh, mm, ss = t.split(":")
                alerts.append((now.replace(hour=int(hh), minute=int(mm), second=int(ss), microsecond=0).timestamp(), txt))
            except Exception:
                continue
        return {"bars": bars, "idx_series": idx_series, "peer_hist": self.peer_hist.get(c, {}), "alerts": alerts, "market": mkt}

    # ------------------------------------------------------------ 快迴圈（圖表、報價、分流、即時警示；有永豐 3 秒，否則 10 秒）
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

    def _overlay_shioaji(self, c, q):
        """MIS 報價延遲 5–15 秒、10 秒才取一次；有永豐串流時成交價/高低/量/五檔都改用串流（逐筆即時），MIS 只剩昨收與族群。"""
        try:
            st = self.flow_src.stats(c)
            bk = self.flow_src.book(c)
        except Exception:
            return
        if st and st.get("last"):
            q["ref"] = st["last"]; q["z"] = st["last"]
            if st.get("high"): q["h"] = max(st["high"], q.get("h") or 0)
            if st.get("low"): q["l"] = min(st["low"], q.get("l") or st["low"])
            if st.get("open") and not q.get("o"): q["o"] = st["open"]
            if st.get("total_volume"): q["v"] = max(st["total_volume"], q.get("v") or 0)
            q["src"] = "shioaji"
        if bk and bk.get("bids"):
            q["bids"], q["asks"] = bk["bids"], bk["asks"]
            q["book_src"] = "shioaji"

    def fast_tick(self):
        """一輪快更新：報價 → 分時序列、分流、分流劇變警示；每 60 秒順便更新權證快照與警示。
        MIS 每 10 秒抓一次（限流），有永豐串流時每 3 秒用串流覆寫成交價/高低/量/五檔；MIS 失敗時整份改用串流湊（只少族群）。"""
        now = dt.datetime.now()
        mis_due = (time.time() - self._last_mis_t >= 10) or not self._last_quotes
        if mis_due:
            try:
                self._last_quotes = src_mis.fetch(sorted(self.ex_codes))
                self._last_mis_t = time.time()
                self._mis_fail = 0
            except hc.HttpError as e:
                self._mis_fail = getattr(self, "_mis_fail", 0) + 1
                if not self.flow_src:
                    raise
                if self._mis_fail in (1, 10, 100):
                    print(f"[fast] MIS 失敗（第 {self._mis_fail} 次），改用 Shioaji 串流報價：{str(e)[:100]}")
                self._last_quotes = self._quotes_from_shioaji(now)
                self._last_mis_t = time.time() - 5   # 5 秒後再試 MIS
                if not self._last_quotes:
                    raise
        quotes = {k: dict(v) for k, v in self._last_quotes.items()}
        do_wq = time.time() - self._last_wq_t >= 60
        live = {"time": now.strftime("%H:%M:%S"), "phase": model.phase_of(now), "index": {}, "index_series": {},
                "quotes": {}, "series": {}, "flow": {}, "warrants": {}, "alerts": {},
                "shioaji": bool(self.flow_src), "index_stream": self.index_stream, "interval": self.interval,
                "fast_every": 3 if self.flow_src else 10, "mis_t": dt.datetime.fromtimestamp(self._last_mis_t).strftime("%H:%M:%S"),
                "signal": {}, "flow_series": {}, "walls": {}, "ticks_used": (self.flow_src.ticks_usage() if self.flow_src else None), "ticks_limit": 10,
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
            if self.flow_src:
                self._overlay_shioaji(c, q)     # 成交價/高低/量/五檔改用逐筆串流（含 z='-' 延緩撮合時）
            self._record_peers(c, {p_: quotes[p_] for p_ in self.daily[c]["peers"] if p_ in quotes}, now)
            if q.get("trial"):
                self._add_alert(c, now, f"⚠ 延緩撮合（瞬間價格穩定措施）：試算價 {q['trial']} 超出今日區間 {q.get('l')}–{q.get('h')}，2 分鐘後恢復撮合，波動即將放大，掛單先撤")
            lq = {k: q.get(k) for k in ("ref", "o", "h", "l", "v", "y", "bids", "asks", "name", "trial", "src", "book_src")}
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
                # 大戶/散戶累積買賣超分鐘序列（籌碼K線「大戶散戶」那張圖）：全日逐筆重算一次 O(n)，10 秒一次就夠
                t_fs, fs = self._fs_cache.get(c, (0, None))
                if time.time() - t_fs >= 10 or fs is None:
                    try:
                        fs = self.flow_src.flow_history(c)
                    except Exception:
                        fs = fs or []
                    self._fs_cache[c] = (time.time(), fs)
                live["flow_series"][c] = [[dt.datetime.fromtimestamp(r[0]).strftime("%H:%M"), r[1], r[2], r[4]] for r in fs]
            if do_wq:
                wq = self._warrant_snapshot(c, q, now, 1)
                if wq:
                    self.wq[c] = wq
                    for w in wq.get("alerts") or []:
                        self._add_alert(c, now, w)
            if self.wq.get(c):
                live["warrants"][c] = self.wq[c]
            try:
                ws = self._track_walls(c, q, flow, now)
                if ws:
                    live["walls"][c] = ws
            except Exception as e:
                print(f"[fast] 掛單牆追蹤失敗：{str(e)[:100]}")
            try:
                sig = self._signal(c, WEB.latest.get(c), live["series"].get(c) or [], q, now)
                if sig:
                    live["signal"][c] = sig
            except Exception as e:
                print(f"[fast] 訊號判斷失敗：{str(e)[:100]}")
            try:
                self._track_alerts(c, q, flow, live["series"].get(c) or [], now)
            except Exception as e:
                print(f"[fast] 警示追蹤失敗：{str(e)[:100]}")
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

    def start_fast(self, every=None):
        if every is None:
            every = 3 if self.flow_src else 10
        self.fast_thread = threading.Thread(target=self.fast_loop, args=(every,), daemon=True)
        self.fast_thread.start()
        print(f"[fast] 圖表/報價/分流每 {every} 秒更新（MIS 每 10 秒{'，永豐串流每 3 秒覆寫' if self.flow_src else ''}）；多空估算與進出場每 {self.interval} 秒")

    @property
    def src(self):
        """這台的盤中資料來源：shioaji = 永豐逐筆串流（K 線/分流/五檔都即時）；mis = 只有證交所公開報價 10 秒快照（K 線粗、沒有分流）。
        寫進每筆估算、警示、訊號紀錄，統計與演化時分開（MIS 模式的訊號時機和勝率都不準，不該混進模型評分）。"""
        return "shioaji" if self.flow_src else "mis"

    def _sync_logs_once(self):
        """收盤後把今天的資料 commit + push 回 GitHub（一天一次；LOG_SYNC=0 關掉）。"""
        today = dt.date.today().isoformat()
        if os.environ.get("LOG_SYNC", "1") == "0" or getattr(self, "_synced", None) == today:
            return
        self._synced = today
        try:
            r = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "sync_logs.py")],
                               capture_output=True, text=True, timeout=180)
            print(f"[sync] {r.stdout.strip()[-300:]}" + (f" {r.stderr.strip()[-200:]}" if r.returncode else ""))
        except Exception as e:
            print(f"[sync] 失敗：{str(e)[:120]}")

    def log(self, ev):
        path = os.path.join(LOG_DIR, f"monitor_{dt.date.today():%Y%m%d}.jsonl")
        rec = {k: v for k, v in ev.items() if k not in ("quote",)}
        rec["quote"] = {k: ev["quote"].get(k) for k in ("ref", "o", "h", "l", "v", "bids", "asks")}
        rec["src"] = self.src
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
                self._sync_logs_once()
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
    ap.add_argument("--interval", type=int, default=60, help="多空估算間隔秒數（預設 60；圖表/報價另有 3–10 秒快迴圈）")
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
