#!/usr/bin/env python3
"""
當沖多空監控：每 N 秒（預設 180）重算多空勝率、支撐壓力與進出場計畫，直到 Ctrl+C；
--web 模式另有 10 秒快迴圈更新江波圖、報價、分流與即時警示。

    python3 monitor.py --web                     # 選股模式：網頁先跑 theme_screener 列多空候選，勾選後才開始監控
    python3 monitor.py 3042                      # 用 groups.json 的族群
    python3 monitor.py 3042 --peers 2484,8081    # 自訂族群
    python3 monitor.py 3042 --once --json        # 跑一次、輸出 JSON（給程式/AI 讀）
    python3 monitor.py 3042 7704 --interval 120  # 同時監控多檔

每次評估都會 append 到 logs/monitor_YYYYMMDD_<裝置ID>.jsonl（裝置 ID 見 config.DEVICE_ID），收盤後可直接拿來復盤。
"""
import sys
import os
import json
import time
import argparse
import datetime as dt
import traceback

from config import LOG_DIR, load_env, DEVICE_ID, dev_path
import prep
import model
import indicators as I
import exhaust
import combo
import env_combos
import notify
import fear
import support
import world_holidays
import orderflow
import history
import src_mis
import src_warrants as W
import http_client as hc   # 別名不能叫 http：下面 import http.server 會把它蓋掉
import tw_calendar as cal
# 復盤（eod）、開盤策略（gapfade）、選股、期貨也在啟動時一起載入：以前要用到才 import，monitor 開著時程式改版，
# 晚載入的新版 eod 會配上早就載入的舊版 gapfade（2026-11-25：10:37 啟動、10:56 改版，收盤復盤 'gapfade' has no attribute 'dt_split'）。
# 一起載入 → 整個程序都是同一版，改版要重開才生效。（src_shioaji 沒裝永豐也要能跑，照樣用到才載入）
import eod
import gapfade
import theme_screener
import src_taifex
import re
import glob
import subprocess
import threading
import webbrowser
import http.server
import urllib.parse
from config import CACHE_DIR, HERE

WEB_DIR = os.path.join(HERE, "web")
WEB_PORT = 8999          # --web 不給 port 時用這個（2026-11-25 從 8770 改成 8999），儀表板 http://127.0.0.1:8999/


_TRADING = {}
BOOT_ID = f"{os.getpid()}-{time.time():.0f}"   # 這次程序的 ID（/api/state 回傳）：自動重開後換新，儀表板看到就整頁重新載入


def _is_trading(d):
    if d not in _TRADING:
        try:
            _TRADING[d] = bool(cal.is_trading_day(d))
        except Exception:
            _TRADING[d] = d.weekday() < 5
    return _TRADING[d]


def in_session(now=None):
    """現在是不是盤中（交易日 08:25–13:35）。儀表板用它決定預設畫面：盤中看即時，其餘時間（收盤後、開盤前、休市日）看最新的復盤。"""
    now = now or dt.datetime.now()
    return _is_trading(now.date()) and "08:25" <= now.strftime("%H:%M") < "13:35"


# 開著過夜：漲跌停、技術面、法人、族群這些盤前資料只在啟動（或選定標的）時算一次，大盤與個股分時、訊號、警示也都是那天的。
# 2026-11-17 monitor 從 11/16 開到隔天：畫面一直顯示前一天的漲停 126.5 / 跌停 103.5（今天應是 113.5 / 93.2），
# 98.9 被當成已經跌停；08:25 串流自動接回後，大盤分時把兩天的點混在一起，訊號也用錯的漲跌停去擋（例如「跌停空不到」）。
# 現在：換到新的交易日後，舊程序不再截取、不發訊號，畫面保留前一天的資料給你參考；到 RESTART_AT（試撮 08:30 前）自動重開，
# 等於幫你重跑同一個指令（選股模式回到選股畫面，給代碼的照樣監控那幾檔），所有資料換成今天的。
RESTART_AT = "08:25"


def stale_day(boot, now=None):
    """boot 那天啟動的程序，現在是不是已經跨到之後的交易日（休市日不算：週五開到週末不用重開，週一再重開）。"""
    now = now or dt.datetime.now()
    return now.date() > boot and _is_trading(now.date())


def restart_due(boot, now=None):
    now = now or dt.datetime.now()
    return stale_day(boot, now) and now.strftime("%H:%M") >= RESTART_AT


# 網站（GitHub Pages）的「三大法人淨額（夜盤）」靠 GitHub 排程更新，但排程常延後或被丟掉（10/01～11/17 排約 280 次只跑 100 次，
# 早上 6～9 點幾乎沒有準時跑過），期交所又約 06:00 後才公布，網站常到 10 點後才有。這台是公用電腦，不裝自動觸發的排程；
# 改成 monitor 開著時順便看：期交所已公布、網站還沒有 → 儀表板頂端提醒，附 GitHub 手動觸發（Run workflow）的連結。
# 只讀公開網頁（GitHub Pages 的 history.json、期交所），不用 gh、不碰任何帳號。
SITE_WINDOW = ("06:00", "13:30")
_SITE_STOP = threading.Event()


def _site_repo():
    """從 git remote 取 (owner, repo)；只取名稱，網址裡就算有帳密也不留下、不印出。"""
    try:
        url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=HERE, capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return None
    m = re.search(r"github\.com[:/]+([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", url)
    return (m.group(1), m.group(2)) if m else None


def site_night_status(now=None, repo=None):
    """
    網站的夜盤三大法人是不是今天的。回 None（非交易日或不在 06:00–13:30）或 {state, date, checked, ...}：
      ok      網站已經有今天的夜盤法人
      waiting 網站還沒有，但期交所也還沒公布（約 06:00 後），或昨晚沒有夜盤（休市後第一天）→ 不提醒
      stale   期交所已公布、網站還沒更新 → 提醒到 GitHub 手動觸發（附 url）
    """
    now = now or dt.datetime.now()
    d = now.date()
    if not _is_trading(d) or not (SITE_WINDOW[0] <= now.strftime("%H:%M") < SITE_WINDOW[1]):
        return None
    repo = repo or _site_repo()
    if not repo:
        return None
    owner, name = repo
    target = d.strftime("%Y/%m/%d")
    hist = hc.get_json(f"https://{owner}.github.io/{name}/data/history.json", params={"_": int(time.time())})
    rows = [r for r in hist if isinstance(r, dict)] if isinstance(hist, list) else []
    rec = next((r for r in rows if r.get("date") == target), None)
    base = {"date": target, "checked": now.strftime("%H:%M")}
    if rec and isinstance(rec.get("foreign_net_ah"), int):
        return dict(base, state="ok")
    import src_taifex
    if not src_taifex.ah_published(d):
        return dict(base, state="waiting")
    return dict(base, state="stale", site_date=max((r.get("date") or "" for r in rows), default=None), site_has_row=rec is not None,
                url=f"https://github.com/{owner}/{name}/actions/workflows/deploy.yml")


def site_watch():
    """背景執行緒：交易日 06:00–13:30 每 10 分鐘看一次網站的夜盤三大法人（提醒中每 5 分鐘），網站有了就停到隔天。結果放 WEB.site。"""
    repo = _site_repo()
    if not repo:
        print("[網站] 找不到 GitHub repo（git remote），不檢查網站的夜盤三大法人")
        return
    ok_day = None
    while not _SITE_STOP.is_set():
        now = dt.datetime.now()
        prev = WEB.site
        try:
            st = None if ok_day == now.date() else site_night_status(now, repo)
            if st and st["state"] == "ok":
                ok_day = now.date()
            with WEB.lock:
                WEB.site = st if (st and st["state"] == "stale") else None
            if WEB.site and not prev:
                print(f"[網站] 期交所已公布 {st['date']} 夜盤三大法人，網站還沒更新（GitHub 排程沒跑）：儀表板已提醒到 GitHub 手動觸發")
            elif prev and not WEB.site and st and st["state"] == "ok":
                print(f"[網站] 網站的夜盤三大法人已更新（{st['date']}）")
        except Exception as e:
            print(f"[網站] 檢查夜盤三大法人失敗，下次再試：{str(e)[:100]}")    # 網路錯誤：提醒維持原狀，不閃來閃去
        sys.stdout.flush()
        _SITE_STOP.wait(300 if WEB.site else 600)


def gapfade_watch(use_shioaji):
    """
    背景執行緒：開高放空觀察（gapfade.py）。交易日 09:05 用永豐 snapshots 掃全市場一次（150 元以內、開高 2～7%、昨日成交值前 40%），
    之後每分鐘追蹤、13:25 前最後一筆結算，寫 logs/gapfade_<裝置ID>.jsonl。只記錄假設放空的結果，不發訊號、不建議下單。結果放 WEB.gap。
    沒有永豐（或 --no-shioaji）就只在卡片上寫原因。
    """
    import gapfade
    lock = None
    if use_shioaji:
        try:
            import src_shioaji as S
            lock = S._api_lock
        except Exception:
            use_shioaji = False

    def api_fn():
        if not use_shioaji:
            return None
        try:
            import src_shioaji as S
            return S.login() if S.available() else None
        except Exception:
            return None

    def say(msg):
        print(msg)
        sys.stdout.flush()

    def put(view):
        with WEB.lock:
            WEB.gap = view

    g = gapfade.GapFade(api_fn, cost=Monitor.COST_PCT, is_trading=_is_trading, lock=lock, log=say,
                        notify=lambda kind, title, msg="": notify.notify(kind, title, msg))      # 開盤策略建議的單：叮（進場）/ 叮咚（接近漲停、13:20 回補）
    g.run(_SITE_STOP, put)


def fear_watch(use_shioaji):
    """
    背景執行緒：恐慌指數（fear.py，臺指選擇權波動率指數）。盤中（08:30–13:31）每 60 秒用永豐 snapshots 抓即時值——
    只用 monitor 串流已經登入的連線、在 _api_lock 裡查，不另外登入；其他時間（或沒有永豐）用期交所公開的日收盤。
    結果放 WEB.fear：儀表板頂端、訊號判斷（VIX ≥48 盤中暫停、恐慌升溫時做多寫參考）、開高放空觀察（極度恐慌時 B、C 不成立）都看它。
    """
    while not _SITE_STOP.is_set():
        now = dt.datetime.now()
        live = None
        pull = None
        if use_shioaji and "08:30" <= now.strftime("%H:%M") < "13:31" and _is_trading(now.date()):
            try:
                import src_shioaji as S
                if S._api is not None:
                    with S._api_lock:
                        live = fear.live_value(S._api)
                        pull = _pull_feed(S, now)
            except Exception:
                live = None
        try:
            st = fear.state(now, live=live)
            with WEB.lock:
                WEB.fear = st
        except Exception as e:
            print(f"[恐慌指數] {str(e)[:100]}")
        try:
            sp = support.status(now.date())
            last = SUPPORT_DET.recent(now)
            sp["pull"] = last and dict(last, text=support.describe_pull(last), note=support.PULL_NOTE)
            sp["pulls_today"] = len(SUPPORT_DET.events)
            with WEB.lock:
                WEB.support = sp
            if pull:
                print(f"[護盤] {support.describe_pull(pull)}")
                notify.notify("exit", "疑似護盤拉抬：" + support.describe_pull(pull).split("：", 1)[1], support.PULL_NOTE)
        except Exception as e:
            print(f"[護盤] {str(e)[:100]}")
        _SITE_STOP.wait(60 if live is not None else 600)


SUPPORT_DET = support.PullDetector()


def _pull_feed(S, now):
    """（在 _api_lock 裡呼叫）snapshots 台積電、鴻海、聯發科、加權、櫃買各一次，餵給護盤偵測。回新事件或 None。"""
    api = S._api
    cs = [S.get_contract(api, c) for c in support.HW]
    cs = [c for c in cs if c is not None]
    tx_c, otc_c = api.Contracts.Indexs.TSE["IX0001"], api.Contracts.Indexs.OTC["IX0043"]
    snaps = {getattr(x, "code", None): float(x.close or 0) for x in api.snapshots(cs + [tx_c, otc_c])}
    return SUPPORT_DET.feed(now, {c: snaps.get(c) for c in support.HW}, snaps.get("IX0001"), snaps.get("IX0043"))


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
        self.picker = {"status": "off"}   # 選股模式：off / running / done / error（theme_screener 結果）
        self.selected = None              # 網頁選定的監控代碼
        self.select_event = threading.Event()
        self.picks = {}                   # code -> 選股理由（選股模式選的才有）
        self.tomorrow = {"status": "off"} # 復盤畫面「明天的準備」：off / running / done / error（背景抓分點表與選股）
        self.adding = {}                  # 盤中加入的標的：code -> {status: running / done / error, ...}（Monitor.add_code）
        self.winding = {}                 # 盤中移除、還在收尾的標的：code -> {name, since, open}（Monitor.remove_code）
        self.site = None                  # 網站的夜盤三大法人沒更新時的提醒（site_watch）：{date, site_date, url, checked}；沒事 = None
        self.gap = None                   # 開高放空觀察（gapfade_watch，全市場、只記錄不建議下單）：GapFade.view()
        self.fear = None                  # 恐慌指數（fear_watch）：fear.state()
        self.support = None               # 國安基金護盤期與盤中拉抬偵測（fear_watch）：support.status() + pull
        self._hol = None                  # 國際休市參考（world_holidays.status，純計算、不連網）：holidays() 一天算一次

    def holidays(self):
        """美、日、韓、新、陸的國定假日 / 股市休市（world_holidays.py）：今天日韓新陸有沒有開、昨晚美股有沒有開、接下來 30 天。只顯示、記錄，不擋。"""
        d = dt.date.today()
        h = self._hol
        if not h or h.get("date") != d.isoformat():
            try:
                h = world_holidays.status(d)
            except Exception as e:
                h = {"date": d.isoformat(), "error": str(e)[:120]}
            self._hol = h
        return h

    def put_live(self, obj):
        with self.lock:
            self.live = obj

    def live_json(self):
        with self.lock:
            return json.dumps(self.live, ensure_ascii=False, default=str)

    def put(self, ev, quotes):
        with self.lock:
            if ev["code"] not in self.codes:
                return          # 盤中剛移除的：主執行緒那一輪估算手上還是舊清單，不要把它放回畫面
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
        sess = in_session()
        with self.lock:
            return json.dumps({"codes": self.codes, "latest": self.latest, "daily": self.daily, "describe": self.describe,
                               "index": self.index, "updated": self.updated, "interval": self.interval, "shioaji": self.shioaji,
                               "picks": self.picks, "in_session": sess, "adding": self.adding, "winding": self.winding, "max_codes": MAX_CODES,
                               "boot": BOOT_ID, "site": self.site, "gap": self.gap, "fear": self.fear, "support": self.support,
                               "holidays": self.holidays()},
                              ensure_ascii=False, default=str)

    def history_json(self, code):
        with self.lock:
            return json.dumps(self.history.get(code, []), ensure_ascii=False, default=str)

    def picker_json(self):
        with self.lock:
            return json.dumps({**self.picker, "selected": self.selected, "max_pick": MAX_PICK}, ensure_ascii=False, default=str)

    def select(self, body):
        """POST /api/select {codes: [...]}：選股模式下選定監控標的（只能選一次；要換標的請重開 monitor）。"""
        codes = body.get("codes") if isinstance(body, dict) else None
        if not isinstance(codes, list):
            return False, {"error": "codes 要是代碼陣列"}
        codes = list(dict.fromkeys(str(c).strip().upper() for c in codes if str(c).strip()))
        bad = [c for c in codes if not (4 <= len(c) <= 6 and c.isalnum())]
        if bad:
            return False, {"error": f"代碼格式不對：{'、'.join(bad)}"}
        if not codes:
            return False, {"error": "請至少選一檔"}
        if len(codes) > MAX_PICK:
            return False, {"error": f"最多選 {MAX_PICK} 檔，現在選了 {len(codes)} 檔（{'、'.join(codes)}），請取消 {len(codes) - MAX_PICK} 檔"}
        with self.lock:
            if self.selected is not None:
                return False, {"error": "已經選過了；要換標的請重開 monitor"}
            self.selected = codes
            res = self.picker.get("result") or {}
            for side in ("short", "long"):
                for r in res.get(side) or []:
                    if r["code"] in codes:
                        self.picks[r["code"]] = {**r, "side": side, "active": res.get(f"{side}_active")}
        self.select_event.set()
        return True, {"selected": codes}


def review_json(date):
    """復盤畫面的資料（eod.build）：當天紀錄整理成成績單、訊號、過濾、方向、警示。date = YYYY-MM-DD 或 None（今天）。"""
    import eod
    try:
        return json.dumps(eod.build(date), ensure_ascii=False, default=str)
    except Exception as e:
        return json.dumps({"error": f"{e.__class__.__name__}: {str(e)[:200]}"}, ensure_ascii=False)


def tomorrow_json(codes):
    """
    「明天的準備」：分點表與選股要連線、要等傍晚資料出來，所以在背景抓、結果快取。
    沒抓過、換日了、或上次還有資料沒出（且已過 10 分鐘）就重新抓一次；回目前的狀態。
    """
    import eod
    want = eod.ref_day().isoformat()

    def work():
        try:
            res = eod.tomorrow(codes)
            with WEB.lock:
                WEB.tomorrow = {"status": "done", "result": res, "ts": time.time()}
        except BaseException as e:
            with WEB.lock:
                WEB.tomorrow = {"status": "error", "error": str(e)[:300] or e.__class__.__name__, "ts": time.time()}

    with WEB.lock:
        t = WEB.tomorrow
        res = t.get("result") or {}
        pending = any(b.get("pending") or b.get("error") for b in (res.get("brokers") or {}).values()) or (res.get("screener") or {}).get("pending") \
            or (res.get("screener") or {}).get("error")
        missing = [c for c in codes if c not in (res.get("brokers") or {})]
        stale = t["status"] == "off" or (t["status"] in ("done", "error") and time.time() - t.get("ts", 0) > 600 and (pending or t["status"] == "error")) \
            or (t["status"] == "done" and (res.get("date") != want or missing))
        if stale and t["status"] != "running":
            WEB.tomorrow = {"status": "running", "started": dt.datetime.now().strftime("%H:%M:%S"), "result": t.get("result")}
            threading.Thread(target=work, daemon=True).start()
        return json.dumps(WEB.tomorrow, ensure_ascii=False, default=str)


MAX_PICK = 6   # 選股模式最多同時監控幾檔（每檔一條永豐串流與一次盤前資料）
MAX_CODES = 12 # 盤中再加入標的時，總共最多幾檔（MIS 一次查詢的檔數、畫面分頁、快迴圈的負擔都有限）
STARTUP_WAIT = 1800  # monitor 啟動中按「加入監控」的，最多排隊等幾秒（啟動 5 檔約 9 分鐘；超過就請使用者稍後再加）
WEB = WebState()


def run_screener(top=3):
    """背景執行 theme_screener，結果放 WEB.picker 給網頁顯示。"""
    with WEB.lock:
        WEB.picker = {"status": "running", "started": dt.datetime.now().strftime("%H:%M:%S")}
    try:
        import theme_screener as TS
        res = None
        try:
            res = TS.load_precomputed()          # GitHub Actions 收盤後預先算好的（資料日要對得上），有就直接用
        except Exception as e:
            print(f"[選股] 讀預算結果失敗：{str(e)[:100]}")
        if res:
            print(f"[選股] 用預先算好的結果：資料日 {res['date']}（{res.get('generated_by')} 於 {res.get('generated_at')} 產生）")
        else:
            print("[選股] 沒有可用的預算結果，本機現在算（第一次要補抓資料會久一點）")
            res = TS.to_dict(TS.screen(TS.default_ref(), top, warr_max_new=2))
        with WEB.lock:
            WEB.picker = {"status": "done", "result": res, "finished": dt.datetime.now().strftime("%H:%M:%S")}
        print(f"[選股] {res['date']} 完成：做空 {','.join(r['code'] for r in res['short'][:top]) or '無'}"
              f"｜做多 {','.join(r['code'] for r in res['long'][:top]) or '無'}（{'大盤弱，弱勢股做空有效' if res['short_active'] else '大盤不弱，弱勢股做空無效'}；做多只在恐慌開低時做）"
              f"｜隔日沖倒貨候選 {','.join(r['code'] for r in (res.get('nds') or [])[:5]) or '無'}"
              + ("｜今天沒有適合的個股：不做" if res.get("no_trade") else ""))
    except BaseException as e:      # screen() 資料不足時會 SystemExit
        with WEB.lock:
            WEB.picker = {"status": "error", "error": str(e)[:300] or e.__class__.__name__}
        print(f"[選股] 失敗：{str(e)[:200]}（網頁仍可手動輸入代碼）")


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
        if path == "/api/quote":
            mon = getattr(WEB, "monitor", None)
            return self._send(mon.quote_json() if mon else '{"on": false}')
        if path.startswith("/api/history/"):
            return self._send(WEB.history_json(path.rsplit("/", 1)[-1]))
        if path == "/api/watchlist":
            return self._send(json.dumps(history.all_summary(), ensure_ascii=False, default=str))
        if path == "/api/screener":
            return self._send(WEB.picker_json())
        if path in ("/api/review", "/api/review/tomorrow"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if path == "/api/review":
                d = (q.get("date") or [""])[0]
                return self._send(review_json(d if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) else None))
            codes = [c for c in (q.get("codes") or [""])[0].split(",") if 4 <= len(c) <= 6 and c.isalnum()][:12]
            return self._send(tomorrow_json(codes))
        self._send('{"error":"not found"}', code=404)

    def do_POST(self):
        """POST /api/position（持倉面板）、/api/select（選股模式選標的）。只綁 127.0.0.1；要求 JSON（跨站表單送不了）且 Origin 是本機。"""
        path = urllib.parse.urlparse(self.path).path
        mon = getattr(WEB, "monitor", None)
        if not ((path in ("/api/position", "/api/add", "/api/remove") and mon) or path == "/api/select"):
            return self._send('{"error":"not found"}', code=404)
        origin = self.headers.get("Origin") or ""
        host = (urllib.parse.urlparse(origin).hostname or "") if origin else "127.0.0.1"
        if host not in ("127.0.0.1", "localhost") or "application/json" not in (self.headers.get("Content-Type") or ""):
            return self._send('{"error":"forbidden"}', code=403)
        n = int(self.headers.get("Content-Length") or 0)
        if n > 4096:
            return self._send('{"error":"too large"}', code=413)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send('{"error":"bad json"}', code=400)
        code = body.get("code") if isinstance(body, dict) else None
        ok, res = (WEB.select(body) if path == "/api/select" else mon.add_code(code) if path == "/api/add"
                   else mon.remove_code(code) if path == "/api/remove" else mon.position_action(body))
        self._send(json.dumps(res, ensure_ascii=False, default=str), code=200 if ok else 400)


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
            for k in ("short_na", "long_na"):
                if pl.get(k):
                    L.append(f"  {'空' if k == 'short_na' else '多'}：{pl[k]}")
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
        self.codes = list(codes)
        self.interval = interval
        self.web = web
        self._peers_arg, self._refresh = peers_arg, refresh      # 盤中加入標的（add_code）時用同一組設定抓盤前資料
        WEB.codes = list(codes); WEB.interval = interval
        print(f"[裝置] ID {DEVICE_ID}：今天的紀錄寫到 logs/*_{DEVICE_ID}.jsonl（換台電腦開就換 ID，git 不會撞檔）")
        self.daily = {}
        self.samples = {c: [] for c in codes}
        self.w_prev = {}
        self.flow_hist = {}   # code -> [[epoch, big_net, small_net, price], ...]，給 model._flow_dynamics 看主力淨額的變化
        self._flow_now = {}   # code -> 這一輪快迴圈的最新分流（訊號判斷的環境組合要用「現在」的主力淨額）
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
        self._cap_done = set()                       # 已經抓過「只抓一次」那一筆的時段：(日期, pre/trial/after)
        self._cap_tries = {}                         # 同上，失敗重試的次數（最多 3 次）
        self._stream_off = False                     # 收盤後已登出永豐串流
        self._bg_th = None                           # 背景抓 MIS / 權證快照的執行緒（一次只跑一個，不卡快迴圈）
        self._wq_ready = {}                          # 背景抓好的權證快照，下一輪快迴圈套用
        self._mis_fail = 0
        self.peer_hist = {c: {} for c in codes}     # code -> {peer: [[epoch, chg_pct], ...]}，給 model 算族群 10 分鐘趨勢
        self.signals = {}                            # code -> 目前的「建議做空/做多」訊號（快迴圈每 3 秒檢查進場確認條件）
        self.sig_block = {}                          # code -> 最近一次被逆勢過濾擋掉的訊號（照樣追蹤假設進場的結果，驗證過濾有沒有擋對）
        self._idx_day = {"date": None, "hlc": None, "ts": 0}   # 加權指數日 K（前一天為止），給大盤日 KD（_kd_ref）
        self.walls = {}                              # code -> {"bid": 牆, "ask": 牆}：五檔裡特別大的那一檔，追蹤被吃/被撤/補單判真假
        # 撤單觀察與程式單偵測（orderflow.py，2026-11-26 加）：五檔每次變動扣掉成交算撤單、逐筆找定時定量 / 固定張數 / 拆單。只顯示、記錄
        self.of = orderflow.OrderFlow(CACHE_DIR, dev_path(LOG_DIR, "orderflow", ".jsonl"))
        self._fs_cache = {}                          # code -> (epoch, 每分鐘主力/散戶累積淨額序列)，10 秒重算一次給「大戶散戶」圖
        self.pending = []                            # 警示結果追蹤：每則 ⚠/▶ 記下當時狀態，10/30 分鐘後補上漲跌，寫 logs/alert_outcomes.jsonl
        self._tracked = set()
        self._book_hist = {}                         # code -> [(ts, 價差 tick, 買一張數, 賣一張數, 買五檔合計, 賣五檔合計)]，流動性統計用（_liquidity）
        self.exh = {}                                # code -> 最近一次買盤 / 賣盤竭盡事件（_exhaustion）
        self._exh_seen = {}                          # code -> 已處理過的事件 (side, 打斷時間)
        self.signs = {}                              # code -> 最近的竭盡跡象（exhaust.detect：掛單虛胖、大單熄火、量縮價滯…；觀察用、不計分）
        self._sign_seen = {}                         # code -> 已經判斷過的事件
        self.sign_sum = {}                           # code -> exhaust.summary（三個畫面各亮了哪個），給模型列因子與持倉面板
        self._combo_at = {}                          # code -> {組合種類: 上次提醒的時間}（combo.py：因子組合提醒，觀察用、不計分）
        self.runs = {}                               # code -> 目前的連外次 / 連內次（_exhaustion 的回傳；訊號用來看反向是不是還亮著燈）
        self.liq = {}                                # code -> 最近一次流動性統計
        self._liq_alerted = set()                    # 今天已經發過「掛單薄」警示的 code
        self._sig_done = {}                          # code -> [結束的訊號 {side, lo, hi, since, label, res, ext}]：同價位不重發用（_repeat_block）
        self._sig_seen = {}                          # (code, side, px) -> epoch，同一個點 15 分鐘內不重發
        self.positions = {}                          # code -> 使用者自己的單（持倉面板）：queued / holding / closed / cancelled / skipped
        self._pos_stack = {}                         # code -> 之前的狀態（復原用）
        self._pos_last_act = {}
        self.user_trades = {}                        # (code, 訊號時間) -> 使用者這筆的結果，訊號結案時附上
        self.pos_alerts = {}                         # code -> 持倉提醒（只顯示，不寫 log）
        self.pos_lock = threading.Lock()
        self.winding = {}                            # 盤中移除、但還有訊號 / 警示在追蹤結果的標的：code -> {ts, since, open}（remove_code、_wind_down）
        self.code_lock = threading.Lock()            # 改 self.codes / self.winding 時拿（加入、移除、收尾在不同執行緒）；不要跟 WEB.lock 巢狀拿
        self.boot_date = dt.date.today()             # 盤前資料是這天抓的；跨到之後的交易日就自動重開（stale_day / restart_due）
        self._halt = False                           # 自動重開前停掉快迴圈
        # WEB.monitor 一設好，儀表板就能按「加入監控」，但下面抓盤前資料、接串流要好幾分鐘（11/25 啟動 5 檔花了 9 分鐘）。
        # 那段時間加入的會用到還沒建好的 ex_codes / flow_src（11/25 1560、3034 加入失敗：no attribute 'ex_codes'），
        # 所以 ex_codes 先建好，加入的背景執行緒等 _ready（啟動全部做完）才開始（_add_worker）。
        self.ex_codes = set(src_mis.index_codes())
        self._ready = threading.Event()
        WEB.monitor = self
        self._load_positions()
        import src_shioaji
        for c in codes:
            self._setup_code(c)
        for c in codes:
            self._add_ex(c)
        self.calibrate_after_close = False
        self._init_stream(codes, use_shioaji, big_lot)
        self._ready.set()

    def _add_ex(self, c):
        """這檔和它的族群成員加進 MIS 要查的代碼。用重新指定（不是原地改），另一條執行緒正在 sorted(self.ex_codes) 也不會出錯。"""
        d = self.daily[c]
        self.ex_codes = self.ex_codes | {f"{d['market']}_{c}.tw"} | {f"{pi['ex']}_{p}.tw" for p, pi in d["peer_info"].items()}

    def _setup_code(self, c):
        """一檔標的的盤前資料與儀表板用的精簡版（啟動時、盤中加入時都走這裡）。"""
        import src_shioaji
        peers_arg, refresh = self._peers_arg, self._refresh
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
        br = dd.get("brokers") or {}
        if br.get("daytraders"):
            # 昨天買進的隔日沖分點（儀表板「隔日沖分點」卡）；date = 那張分點表的日期
            WEB.daily[c]["daytraders"] = dict(br["daytraders"], date=br.get("day_date"))
        WEB.describe[c] = prep.describe(dd)
        pc = (self.daily[c].get("tech") or {}).get("close")
        if pc:
            th = src_shioaji.lot_thresholds(pc)
            print(f"  大單門檻（以昨收 {pc} 推算）：≥{th['big_lots']} 張 ≈ {th['big_amount']/1e4:.0f} 萬；散戶 <{th['small_lots']} 張 ≈ {th['small_amount']/1e4:.0f} 萬；開盤後再以當日成交分布修正")

    def add_code(self, code):
        """
        盤中再加入一檔監控（儀表板 POST /api/add {code}）。在背景抓盤前資料（10～30 秒）、接上永豐串流並回補今天稍早的逐筆
        （逐筆額度用完就讀本機存檔、再不行用分 K 補線圖），全部準備好才放進 self.codes，其他迴圈不會看到只做一半的標的。
        回 (ok, {status, code} 或 {error})；進度放在 WEB.adding，儀表板從 /api/state 看。
        """
        code = str(code or "").strip().upper()
        if not (4 <= len(code) <= 6 and code.isalnum()):
            return False, {"error": "代碼格式不對（4～6 碼英數字）"}
        with WEB.lock:
            if code in self.codes:
                return False, {"error": f"{code} 已經在監控了"}
            if (WEB.adding.get(code) or {}).get("status") == "running":
                return False, {"error": f"{code} 正在加入中"}
            busy = sum(1 for v in WEB.adding.values() if v.get("status") == "running")
            if len(self.codes) + busy >= MAX_CODES:
                return False, {"error": f"最多同時監控 {MAX_CODES} 檔"}
            WEB.adding[code] = {"status": "running", "started": dt.datetime.now().strftime("%H:%M:%S")}
        with self.code_lock:
            was_winding = self.winding.pop(code, None) is not None     # 剛移除、還在收尾：報價和永豐訂閱都還在，直接接回去
        if was_winding:
            with WEB.lock:
                WEB.winding.pop(code, None)
        threading.Thread(target=self._add_worker, args=(code, was_winding), daemon=True, name=f"add-{code}").start()
        return True, {"status": "running", "code": code}

    def _add_worker(self, code, was_winding=False):
        t0 = time.time()
        try:
            if not self._ready.is_set():
                # monitor 還在啟動（抓監控中標的的盤前資料、接串流）：排隊等啟動完成再加，不然會用到還沒建好的東西
                with WEB.lock:
                    WEB.adding[code] = dict(WEB.adding.get(code) or {}, wait=True)
                print(f"[加入] {code}：monitor 還在啟動，啟動完成就接著加")
                if not self._ready.wait(STARTUP_WAIT):
                    raise RuntimeError(f"monitor 啟動還沒完成（等了 {STARTUP_WAIT // 60} 分鐘），請稍後再加一次")
                with WEB.lock:
                    (WEB.adding.get(code) or {}).pop("wait", None)
                t0 = time.time()
            print(f"[加入] {code}：抓盤前資料中…")
            for store, empty in ((self.samples, list), (self.live_alerts, list), (self.mis_bars, dict), (self.peer_hist, dict)):
                store.setdefault(code, empty())
            self._setup_code(code)
            if not self.daily[code].get("name"):
                raise RuntimeError("查不到這個代碼（不是上市櫃股票，或今天沒有交易）")
            self._add_ex(code)
            note = "；剛移除還在收尾，報價直接接回" if was_winding else ""
            if self.flow_src and not self._stream_off and not was_winding:
                ok = self.flow_src.start_stream([code])          # 訂閱逐筆與五檔，並回補今天稍早的（backfill_today）
                if ok:
                    self._backfill_flow(ok)
                else:
                    note = "；永豐串流訂閱失敗，這檔只有公開報價"
            with self.code_lock:
                self.codes = self.codes + [code]                 # 新的 list：其他執行緒手上的舊 list 不受影響
            with WEB.lock:
                WEB.codes = list(self.codes)
                WEB.adding[code] = {"status": "done", "name": self.daily[code].get("name"), "secs": round(time.time() - t0), "note": note.lstrip("；")}
            print(f"[加入] {code} {self.daily[code].get('name')} 開始監控（{time.time() - t0:.0f} 秒）{note}")
        except BaseException as e:          # prep.build 查不到代碼時丟的是 SystemExit，不接住的話這條執行緒會無聲結束、畫面一直顯示「加入中」
            with WEB.lock:
                WEB.adding[code] = {"status": "error", "error": str(e)[:160] or e.__class__.__name__}
                WEB.daily.pop(code, None); WEB.describe.pop(code, None)      # 做一半的不要留在畫面資料裡
            self.daily.pop(code, None)
            if was_winding:
                self._drop_feed(code)                            # 收尾中的報價已經接手過來了，加入失敗就照原本的移除把它停掉
            print(f"[加入] {code} 失敗：{str(e)[:160]}")

    def remove_code(self, code):
        """
        盤中移除一檔監控（儀表板 POST /api/remove {code}）。回 (ok, {status: removed / winding, ...} 或 {error})。
        馬上從分頁、多空估算、權證快照、掛單牆 / 竭盡 / 流動性判斷、新訊號拿掉（省 MIS 查詢、權證查詢、永豐流量和這台的計算）。
        還有訊號或警示在追蹤結果時先「收尾」（_wind_down）：那些紀錄要用之後的價格才能結案、寫進 signal_outcomes /
        alert_outcomes / 交戰紀錄，直接停掉報價就永遠不會寫（盤後統計會少掉）。收尾只推進既有的追蹤、不發新訊號，
        做完（最晚 13:31 收盤結案）才停它的報價與永豐訂閱；沒有東西要追蹤就立刻停（_drop_feed）。
        持倉面板還有排隊中 / 持有中的單不能移除（停損停利提醒要報價）；至少留一檔。
        """
        code = str(code or "").strip().upper()
        now = dt.datetime.now()
        with self.code_lock:
            if code not in self.codes:
                return False, {"error": f"{code} 不在監控中"}
            if len(self.codes) <= 1:
                return False, {"error": "至少要留一檔；要整批換掉請重開 monitor"}
            pos = self.positions.get(code)
            if pos and pos.get("state") in ("queued", "holding"):
                return False, {"error": f"{code} 持倉面板還有{'排隊中' if pos['state'] == 'queued' else '持有中'}的單，先在持倉面板結束或取消再移除"}
            self.codes = [c for c in self.codes if c != code]        # 新的 list：其他執行緒手上的舊 list 不受影響
            n = self._open_tracking(code)
            if n:
                self.winding[code] = {"ts": now.timestamp(), "since": now.strftime("%H:%M:%S"), "open": n}
        name = (self.daily.get(code) or {}).get("name") or ""
        with WEB.lock:
            WEB.codes = list(self.codes)
            for store in (WEB.latest, WEB.daily, WEB.describe):
                store.pop(code, None)                                # 強度走勢（WEB.history）留著：再加回來時圖接得上
            if n:
                WEB.winding[code] = {"name": name, "since": now.strftime("%H:%M:%S"), "open": n}
        if n:
            print(f"[移除] {code} {name}：不再估算與顯示；還有 {n} 個訊號 / 警示在追蹤結果，追蹤完（最晚 {self.CLOSE_SNAP}）才停它的報價")
        else:
            self._drop_feed(code)
            print(f"[移除] {code} {name}：停止監控" + ("，永豐已取消訂閱" if self.flow_src and not self._stream_off else ""))
        return True, {"status": "winding" if n else "removed", "code": code, "name": name, "open": n}

    def _open_tracking(self, c):
        """這檔還有幾個要用之後價格結案的紀錄：追蹤中的警示 / 訊號（self.pending）＋還沒結果的建議訊號。"""
        n = sum(1 for r in list(self.pending) if r.get("code") == c)
        sg = self.signals.get(c)
        return n + (1 if sg and not sg.get("status") else 0)

    def _rebuild_ex(self):
        """MIS 要查的代碼重新算：指數 + 監控中與收尾中的標的和它們的族群成員（移除的那檔若是別檔的族群成員就留著）。"""
        with self.code_lock:
            keep = list(self.codes) + list(self.winding)
        ex = set(src_mis.index_codes())
        for c in keep:
            d = self.daily.get(c)
            if d:
                ex |= {f"{d['market']}_{c}.tw"} | {f"{pi['ex']}_{p}.tw" for p, pi in d["peer_info"].items()}
        self.ex_codes = ex

    def _drop_feed(self, c):
        """移除的最後一步：MIS 不再查它、永豐取消訂閱（逐筆先存本機，再加回來時回補用）。又被加回來了就不動。"""
        with self.code_lock:
            back = c in self.codes or c in self.winding
        with WEB.lock:
            back = back or (WEB.adding.get(c) or {}).get("status") == "running"
            if not back:
                WEB.winding.pop(c, None)
        if back:
            return
        self._rebuild_ex()
        if self.flow_src and not self._stream_off:
            try:
                self.flow_src.stop_stream([c])
            except Exception as e:
                print(f"[移除] {c} 取消永豐訂閱失敗：{str(e)[:100]}")

    def _wind_down(self, quotes, now):
        """
        收尾中的標的（盤中移除、還有追蹤沒結案）：每輪快迴圈只推進既有的追蹤——建議訊號照原規則結案（_signal 不給 ev 就不會開新的）、
        警示 / 訊號紀錄到時間就結案寫檔（_track_alerts，只追蹤移除前發的警示，收尾時的「✔ / ✘」結果不再追 30 分鐘）。
        不估算、不抓權證、不判斷掛單牆 / 竭盡。都結案了（或 13:31 收盤那一輪之後）就停報價。
        """
        with self.code_lock:
            items = list(self.winding.items())
        closed = now.strftime("%H:%M") >= self.CLOSE_SNAP
        for c, w in items:
            q = dict(quotes.get(c) or {})
            if self.flow_src:
                self._overlay_shioaji(c, q)
            if q.get("ref"):
                try:
                    if self.flow_src:
                        bars = self.flow_src.series(c)
                    else:
                        self._mis_bar(self.mis_bars.setdefault(c, {}), now, q["ref"], q.get("v"))
                        bars = [[kk] + v for kk, v in sorted(self.mis_bars[c].items()) if kk != "_v"]
                    flow = self.flow_src.flow(c, price=q["ref"]) if self.flow_src else None
                    self._signal(c, None, bars, q, now)
                    self._track_alerts(c, q, flow, bars, now, track_until=w["ts"])
                except Exception as e:
                    print(f"[移除] {c} 收尾追蹤失敗：{str(e)[:100]}")
            left = self._open_tracking(c)
            if left and not closed:
                if left != w.get("open"):
                    w["open"] = left
                    with WEB.lock:
                        if c in WEB.winding:
                            WEB.winding[c]["open"] = left
                continue
            with self.code_lock:
                if self.winding.pop(c, None) is None:
                    continue                                     # 收尾時又被加回來了
            self._drop_feed(c)
            print(f"[{now:%H:%M:%S}] [移除] {c} " + ("追蹤的訊號 / 警示都結案了" if not left else f"收盤了，{left} 筆沒有報價可結案")
                  + "，停止它的報價" + ("與永豐訂閱" if self.flow_src and not self._stream_off else ""))

    def _init_stream(self, codes, use_shioaji, big_lot):
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
                          + f"；今日盤中 ticks 查詢已用 {src_shioaji.ticks_usage()}/{src_shioaji.ticks_budget()}（用完就不再查；逐筆每分鐘存本機，重啟先讀本機的）")
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
            ctx = self._ctx(c, now)
            ev = model.evaluate(d, q, idx, peers_q, flow, self.samples[c], now, warrants=wq, ctx=ctx)
            try:
                ev["info"]["kd"] = self._kd_ref(c, q, ctx.get("bars"), (ctx.get("idx_series") or {}).get("t00"))
            except Exception as e:
                print(f"[{now:%H:%M:%S}] {c} KD 計算失敗：{str(e)[:100]}")
            if wq and wq.get("alerts"):
                ev["warnings"] = wq["alerts"] + ev["warnings"]
            try:
                ev["combos"] = self._combos(c, ev, now)
            except Exception as e:
                print(f"[{now:%H:%M:%S}] {c} 組合提醒失敗：{str(e)[:100]}")
            if self.fast_thread:
                ev["live_alerts"] = list(self.live_alerts.get(c, []))[-12:]
            WEB.put(ev, quotes)
            results.append(ev)
        return results

    IDX_MOVE = ((5, 0.4), (10, 0.6))    # 大盤急漲急跌：(幾分鐘, 變化 % 門檻)。11/05–11/11 五個交易日 5 分鐘變化的中位數 0.14%、九成在 0.42% 內
    IDX_COOLDOWN = 600                  # 同一個指數同一個方向 10 分鐘內只通知一次
    IDX_SHOW = 300                      # 通知後畫面上方的指數反白 5 分鐘

    def _index_alerts(self, live, now):
        """
        大盤急漲 / 急跌通知：加權或櫃買指數在 5 分鐘內變化 ≥0.4%、或 10 分鐘內 ≥0.6%（跟模型「大盤急拉 / 急殺」因子同一個門檻）。
        指數急動會帶動個股情緒，所以每一檔監控中的標的都發一則警示（櫃買的只發給上櫃股），之後個股的走勢照樣由 _track_alerts 追蹤。
        用指數分 K 的收盤比：現在的指數 vs N 分鐘前那一分鐘的收盤。
        """
        hhmm = now.strftime("%H:%M")
        if not ("09:05" <= hhmm <= "13:25"):
            return
        st = self.__dict__.setdefault("_idx_alert", {})          # (指數, 方向) -> 上次通知的 epoch；指數 -> 最近一次通知的內容
        for k, nm in (("t00", "加權"), ("o00", "櫃買")):
            ix = live["index"].get(k)
            ser = dict(live["index_series"].get(k) or [])
            if not ix or not ser:
                continue
            ref = ix["ref"]
            hit = None
            for win, th in self.IDX_MOVE:
                past = None
                for back in (win, win + 1):                        # 那一分鐘剛好沒有資料就再往前一分鐘
                    past = ser.get((now - dt.timedelta(minutes=back)).strftime("%H:%M"))
                    if past:
                        break
                if not past:
                    continue
                ch = (ref / past - 1) * 100
                if abs(ch) >= th and (hit is None or abs(ch) / th > abs(hit[1]) / hit[3]):
                    hit = (win, ch, past, th)
            if hit:
                win, ch, past, _ = hit
                d = 1 if ch > 0 else -1
                if now.timestamp() - st.get((k, d), 0) >= self.IDX_COOLDOWN:
                    st[(k, d)] = now.timestamp()
                    word = "急漲" if d > 0 else "急跌"
                    tail = "個股容易被帶上去，空單留意軋空" if d > 0 else "個股容易被帶下去，多單不要急著接"
                    nd = 0 if ref >= 1000 else 2                  # 櫃買指數只有三百多點，要留小數
                    text = f"⚠ 大盤{word}：{nm} {win} 分鐘 {ch:+.2f}%（{ref - past:+,.{nd}f} 點，{past:,.{nd}f} → {ref:,.{nd}f}），{tail}"
                    st[k] = {"dir": d, "pct": round(ch, 2), "win": win, "pts": round(ref - past, nd), "t": now.strftime("%H:%M:%S"), "ts": now.timestamp()}
                    for c in self.codes:
                        if k == "t00" or (self.daily.get(c) or {}).get("market") == "otc":
                            self._add_alert(c, now, text)
                    print(f"[{now:%H:%M:%S}] {text}")
            mv = st.get(k)
            if mv and now.timestamp() - mv["ts"] <= self.IDX_SHOW:
                ix["move"] = {x: mv[x] for x in ("dir", "pct", "win", "pts", "t")}

    def _combos(self, c, ev, now):
        """因子組合提醒（combo.py）：大盤、族群、個股相對強弱同向，或族群先動個股還沒跟。回目前成立的組合（給儀表板），
        新出現的發 👁 警示（同一檔同一種 30 分鐘一次），之後的走勢由 _track_alerts 追蹤。觀察用，不改多空強度。"""
        if not self._trading_today():
            return []
        own10 = None
        ss = self.samples.get(c) or []
        if ss and ss[-1][1]:
            past = [x for x in ss if 540 <= (now - x[0]).total_seconds() <= 720 and x[1]]
            if past:
                own10 = (ss[-1][1] / past[-1][1] - 1) * 100
        act = combo.detect(ev.get("info") or {}, own10, now.strftime("%H:%M"))
        seen = self._combo_at.setdefault(c, {})
        for x in act:
            if now.timestamp() - seen.get(x["key"], 0) >= combo.COOLDOWN:
                seen[x["key"]] = now.timestamp()
                self._add_alert(c, now, combo.alert_text(x))
        return act

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
    def _against_trend(side, info):
        """
        逆勢過濾：大盤 / 族群最近 10 分鐘往訊號的反方向走就不發。
          強烈反向（任一成立就擋）：大盤 10 分鐘 ≥0.6%、族群 10 分鐘平均 ≥1.0%（做空看上漲、做多看下跌）
          溫和反向：大盤 ≥0.3% 或族群 ≥0.5%，而且另一個沒有同向支持（大盤 ≤-0.3% / 族群 ≤-0.5% 算支持）
        門檻同 model._trend_factors 的「急拉/轉強」。11/06 界霖 09:41、09:57 兩筆空單（加權 10 分鐘 +1.01%、+0.42%）都輸，會被擋；
        11:07 彰源空單（加權 −0.64% 同向）贏，照發。回字串＝擋的理由，None＝不擋。
        """
        sgn = 1 if side == "short" else -1
        it, gt = info.get("idx_trend_10m"), info.get("group_trend_10m")
        ai = it * sgn if it is not None else None     # >0：對這一邊不利
        ag = gt * sgn if gt is not None else None
        strong = ([f"大盤 10 分鐘 {it:+.2f}%"] if ai is not None and ai >= 0.6 else []) + \
                 ([f"族群 10 分鐘 {gt:+.2f}%"] if ag is not None and ag >= 1.0 else [])
        if strong:
            return "、".join(strong) + " 強烈反向"
        mild = ([f"大盤 10 分鐘 {it:+.2f}%"] if ai is not None and ai >= 0.3 else []) + \
               ([f"族群 10 分鐘 {gt:+.2f}%"] if ag is not None and ag >= 0.5 else [])
        support = (ai is not None and ai <= -0.3) or (ag is not None and ag <= -0.5)
        if mild and not support:
            return "、".join(mild) + " 反向"
        return None

    def _limit_block(self, c, side, p, t1, q):
        """漲跌停：做多時價在漲停（委賣沒單 = 鎖死，根本買不到；有委賣也沒上漲空間）、或目標 1 超過漲停 → 擋；做空對稱看跌停。
        回字串 = 擋的理由，None = 不擋。"""
        d = self.daily.get(c) or {}
        up, dn = d.get("limit_up"), d.get("limit_down")
        tick = I.tick_size(p)
        if side == "long" and up:
            if p >= up - tick / 2:
                asks = [a for a in (q.get("asks") or []) if a and a[0] and (a[1] or 0) > 0]
                return f"漲停 {up} 鎖死、委賣沒單，買不到" if not asks else f"已在漲停 {up}，上面沒有空間"
            if t1 > up:
                return f"目標 {t1} 超過漲停 {up}，到不了"
        if side == "short" and dn:
            if p <= dn + tick / 2:
                bids = [b for b in (q.get("bids") or []) if b and b[0] and (b[1] or 0) > 0]
                return f"跌停 {dn} 鎖死、委買沒單，空不到" if not bids else f"已在跌停 {dn}，下面沒有空間"
            if t1 < dn:
                return f"目標 {t1} 低於跌停 {dn}，到不了"
        return None

    # 追價過濾（2026-11-11）：訊號確認時，前 5 分鐘價格已經順著訊號方向走了 ≥0.5%（而且 ≥2 檔）就不發，照樣追蹤。
    # 起因：發出去的訊號比「同一檔同一天任意時點進場」還差（兩天 18 筆成交 22% 勝率、每筆 −0.54%，對照 45%、±0）。
    # 用 9 檔 × 329 個股票日的逐筆（通用支撐壓力訊號 7,119 個、現行輸贏規則）拆開看，不是停損太緊（放寬 2、3 倍一樣差，抱著不動也輸），
    # 而是「反彈一段之後才確認」的訊號方向偏錯。每筆比任意進場：
    #   前 5 分鐘已順向走    價格還在進場價→現價    已離開進場價→現價追    已離開→掛回去等（有成交）
    #   不到 0.5%            +0.01%                −0.03%                +0.13%（t +6～7，最好的一格）
    #   0.5～0.8%            −0.2%～−0.3%          −0.16%                −0.02%
    #   0.8% 以上            −0.3%                 −0.2%～−0.3%          −0.1%～−0.2%
    # 前後半段、tick 大小不同的股票結果一致。所以分界不是「等回或現價」，是「確認前已經跑了多少」：
    # 不到 0.5% 的照發（等回掛單在這一區並不差）；超過的不管怎麼進場都不如任意進場，改成只追蹤。
    # 套到這兩天實際發出的 27 個：會擋掉 18 個（成交 11 筆 2 贏 9 輸，合計 −7.0%；其中等回 7 筆全輸），留下 9 個（成交 6 筆 1 贏 5 輸，−3.95%）。
    # 留下的在實際紀錄裡每筆一樣差，樣本太少分不出來——這個過濾是少做最差的那一類，不是讓訊號變成會賺。
    CHASE_PCT, CHASE_TICKS, CHASE_MIN = 0.5, 2, 5

    @classmethod
    def _run_before(cls, bars, p, side, now):
        """訊號當下，價格比 5 分鐘前（那一根 1 分 K 的收盤）順著訊號方向走了多少 → (百分比, 檔數)；沒有 5 分鐘前的 K 棒回 (None, None)。"""
        if not (bars and p):
            return None, None
        cut = (now - dt.timedelta(minutes=cls.CHASE_MIN)).strftime("%H:%M")
        prev = [b for b in bars if b[0] <= cut]
        if not prev or prev[-1][0] < (now - dt.timedelta(minutes=cls.CHASE_MIN + 3)).strftime("%H:%M"):
            return None, None
        ref = prev[-1][4]
        if not ref:
            return None, None
        sg = -1 if side == "short" else 1
        return round(sg * (p / ref - 1) * 100, 2), round(sg * (p - ref) / I.tick_size(p), 1)

    def _chase_block(self, side, run_pct, run_tk):
        if run_pct is None or run_pct < self.CHASE_PCT or run_tk < self.CHASE_TICKS:
            return None
        return (f"前 {self.CHASE_MIN} 分鐘已經{'跌' if side == 'short' else '漲'}了 {run_pct:.1f}%（{run_tk:g} 檔）才確認，太晚了："
                f"回測這種訊號不管追價還是等回，都比任意時點進場差")

    SURGE_PCT, NEAR_LIMIT_PCT = 7.0, 3.0

    def _surge_block(self, c, side, p, q):
        """
        急漲不放空 / 急殺不做多：個股比昨收漲 ≥7%、或離漲停 ≤3% 時不發做空（跌停對稱，不發做多）。
        不是因為期望值差（回測 2026/03–11 全市場 1484 筆開盤跳空 7–9.5%，開盤空、收盤補平均反而 +1.5%），
        而是尾端風險回測算不到：這類股票 67% 盤中碰漲停、45% 收漲停，鎖住時停損買不回來、當沖可能要標借。
        回字串 = 擋的理由（照樣追蹤假設結果，⏸ 急漲過濾），None = 不擋。
        """
        d = self.daily.get(c) or {}
        y = q.get("y") or (d.get("tech") or {}).get("close")
        up, dn = d.get("limit_up"), d.get("limit_down")
        if not (y and p):
            return None
        chg = (p / y - 1) * 100
        if side == "short":
            room = (up / p - 1) * 100 if up else None
            if chg >= self.SURGE_PCT or (room is not None and room <= self.NEAR_LIMIT_PCT):
                return f"已漲 {chg:+.1f}%" + (f"、離漲停 {up} 只剩 {room:.1f}%" if room is not None else "") + "，漲停鎖住時空單停損買不回來（軋空）"
        else:
            room = (1 - dn / p) * 100 if dn else None
            if chg <= -self.SURGE_PCT or (room is not None and room <= self.NEAR_LIMIT_PCT):
                return f"已跌 {chg:+.1f}%" + (f"、離跌停 {dn} 只剩 {room:.1f}%" if room is not None else "") + "，跌停鎖住時多單停損賣不掉"
        return None

    @staticmethod
    def _entry_zone(side, base, stop, t1, rr_min=None):
        """
        進場區（限價掛單範圍）：做多 = [觸發價（base 下 1 tick）, base + 1/3 停損距離]，上緣再壓到報酬風險比 ≥ rr_min 的價位；
        做空對稱。區間不成立（目標太近）回 None。
        例：11/06 廣達 進場 280.5、停損 278、目標 290 → 280.0–281.0（原本 09:34 掛在 283，報酬風險比 1.4）。
        """
        tick = I.tick_size(base)
        rr_min = Monitor.RR_MIN if rr_min is None else rr_min
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

    EXH_BUCKET = 5          # 每幾秒比一次內外盤：這 5 秒外盤量 > 內盤量算「連外 1 次」，反過來算「連內 1 次」（當沖神器的算法）
    EXH_MIN_N = 10          # 連 10 次亮燈；亮過燈之後出現反方向的 5 秒 = 熄燈 = 竭盡點
    EXH_GAP = 6             # 連續 6 個 5 秒（30 秒）沒成交或內外盤平手 → 這一串散掉，不算竭盡
    EXH_MIN_PUSH = 2        # 這一串至少把價格推動幾檔（沒推動的「連 10 次」只是在同一個價位換手，回測之後是往原方向走）
    EXH_SHORT_TICKS = 3     # 離今高 / 今低這麼近卻沒過就熄燈：回測多半會再試一次，只標示、不加分
    EXH_WINDOW = 900        # 回頭看幾秒的逐筆（一串超過 15 分鐘的話，次數只算到這個範圍內）
    EXH_TTL = 300           # 竭盡事件的加分維持幾秒
    EXH_BONUS = 0.3         # 加分的分數（跟其他盤中因子同一個量級；換算強度約 3 點）

    def _exhaustion(self, c, q, now):
        """
        連外次 / 連內次與買盤竭盡 / 賣盤竭盡，照權證小哥「當沖神器」的算法：每 5 秒比一次這 5 秒內外盤成交量，外盤多 = 連外 +1、
        內盤多 = 連內 +1；同一邊連 10 次「亮燈」（急拉 / 急殺），亮燈後出現反方向的 5 秒 =「熄燈」= 竭盡點（空在爆量漲不動、買在爆量跌不動）。
        連外量 / 連內量 = 這一串裡該方向的成交張數。要永豐逐筆（內外盤），MIS 模式沒有。
        回 {out_n, out_v, in_n, in_v（目前還在連的那一串，只算走完的 5 秒）, lit（是否亮燈）, last（最近 5 分鐘的竭盡事件）}。

        **是轉折的確認，不是進場訊號。** 11/10 用 9 檔、338 個股票日逐筆回測（進場價用保守價：做空賣在買價、做多買在賣價）：
        - 連 ≥10 次後熄燈、不加其他條件 8,861 次：之後 5 / 10 / 30 分鐘的反轉報酬 −0.09% / −0.08% / −0.09%，單獨進場是賠的。
        - 其中超過一半（4,824 次）整串推不到 1 檔，只是同一個價位換手 → 要求至少推 2 檔。
        - **推 ≥2 檔、而且把價格推過這一串之前的今高（賣方：殺破今低）之後熄燈**（470 次）：買盤竭盡 +0.03% / +0.04% / +0.05%、
          賣盤竭盡 +0.08% / +0.05% / +0.11%；同一時段「價格在今高 / 今低 2 檔內的任意時點」對照組是 −0.02% ~ −0.03%。前後半段方向一致。
          → kind = "pierce"，空方 / 多方 +0.3 分 5 分鐘，發警示。
        - **差 0–3 檔沒過今高 / 今低就熄燈**（350 次）：−0.14% / −0.18%（買）、−0.09% / −0.13% / −0.19%（賣）——後面多半會再試一次。
          → kind = "short"，只在面板標示，不加分。
        - 昨高 / 昨低、昨收、開盤價當關卡都沒有效果；連量倍數越大並沒有越準（4 倍以上反而差）。所以關卡只認今高 / 今低，量只顯示不當條件。
        - 亮燈當下逆勢進場（連次剛到 10、已推 ≥2 檔，1,197 次）：之後 1 分鐘 −0.07% ~ −0.14%（t −6 ~ −8）→ 訊號碰到反向亮燈會提醒等熄燈。
        效果都遠小於來回成本 0.235%，所以只當「同方向訊號的加分與確認」，不會單獨觸發進場。
        """
        if not self.flow_src:
            return None
        ticks, _ = self.flow_src.recent_ticks(c, seconds=self.EXH_WINDOW)
        nowts = now.timestamp()
        out = {"out_n": 0, "out_v": 0, "in_n": 0, "in_v": 0, "lit": False, "last": None}

        def last_ev():
            cur = self.exh.get(c)
            return cur if cur and nowts - cur["ts"] <= self.EXH_TTL else None

        B = self.EXH_BUCKET
        cur_b = int(nowts // B)                             # 還沒走完的那 5 秒不算
        bk = {}                                             # 桶 -> [外盤量, 內盤量, 開, 高, 低, 收]
        for ts, p, v, tt in ticks:
            b = int(ts // B)
            if b >= cur_b:
                break
            d = bk.get(b)
            if d is None:
                d = bk[b] = [0, 0, p, p, p, p]
            if tt == 1:
                d[0] += v
            elif tt == 2:
                d[1] += v
            if p > d[3]:
                d[3] = p
            if p < d[4]:
                d[4] = p
            d[5] = p
        if not bk:
            out["last"] = last_ev()
            return out
        side, run, gap, lastb, ended = 0, [], 0, None, None
        for b in sorted(bk):
            if lastb is not None:
                gap += b - lastb - 1
            lastb = b
            d = bk[b]
            s = 1 if d[0] > d[1] else 2 if d[1] > d[0] else 0
            if s == 0:
                gap += 1
            if gap >= self.EXH_GAP:
                side, run, gap = 0, [], 0
            if s == 0:
                continue
            if s == side:
                run.append(b)
                gap = 0
            else:
                if side and len(run) >= self.EXH_MIN_N:
                    ended = (side, run, b)                  # 最近一串「亮過燈、被反向的 5 秒打斷」的
                side, run, gap = s, [b], 0
        if side and run and gap + (cur_b - 1 - lastb) < self.EXH_GAP:
            k = 0 if side == 1 else 1
            out["out_n" if side == 1 else "in_n"] = len(run)
            out["out_v" if side == 1 else "in_v"] = sum(bk[b][k] for b in run)
            out["lit"] = len(run) >= self.EXH_MIN_N
        if ended:
            sd, rn, bb = ended
            tb = (bb + 1) * B
            key = (sd, bb)
            if self._exh_seen.get(c) != key and nowts - tb <= 30:      # 太舊的不補發（剛啟動時）
                self._exh_seen[c] = key
                buy = sd == 1
                sg = 1 if buy else -1
                p0 = bk[rn[0]][2]
                tick = I.tick_size(p0)
                ext = max(bk[b][3] for b in rn) if buy else min(bk[b][4] for b in rn)
                vol = sum(bk[b][0 if buy else 1] for b in rn)
                push = round(sg * (ext - p0) / tick)
                # 今高 / 今低要用這一串「之前」的（不然它自己創的高低點永遠算突破）：分 K 取起點那一分鐘以前的，加上逐筆裡起點以前的
                t0 = rn[0] * B
                hm = dt.datetime.fromtimestamp(t0).strftime("%H:%M")
                pre = [x[2] if buy else x[3] for x in (self.flow_src.series(c) or []) if x[0] < hm]
                pre += [p for ts, p, _, _ in ticks if ts < t0]
                pre_ext = (max(pre) if buy else min(pre)) if pre else None
                over = round(sg * (ext - pre_ext) / tick) if pre_ext else None
                kind = None
                if push >= self.EXH_MIN_PUSH and over is not None:
                    kind = "pierce" if over > 0 else "short" if over >= -self.EXH_SHORT_TICKS else None
                mins = max(1.0, (now.hour * 60 + now.minute + now.second / 60) - 540)
                vpm = (q.get("v") or 0) / mins
                d = bk[bb]
                lvl = "今高" if buy else "今低"
                ev = {"side": "buy" if buy else "sell", "ts": tb, "time": dt.datetime.fromtimestamp(tb).strftime("%H:%M:%S"), "n": len(rn), "vol": vol,
                      "vr": round(vol / vpm, 1) if vpm else None, "push_tk": push, "push_pct": round(sg * (ext / p0 - 1) * 100, 2), "ext": ext,
                      "pre": pre_ext, "over": over, "kind": kind, "brk_px": d[5], "brk_vol": d[1] if buy else d[0],
                      "near": [lvl, pre_ext] if kind == "pierce" else None}
                self.exh[c] = ev
                if kind == "pierce":
                    lab, who, io, verb, back = (("買盤竭盡", "空方", "外", "推", "回落") if buy else ("賣盤竭盡", "多方", "內", "殺", "回升"))
                    self._add_alert(c, now, f"➕ {lab}（{who}加分）：連{io} {len(rn)} 次 {vol} 張，{verb} {push} 檔{'過' if buy else '破'}{lvl} {pre_ext} 到 {ext} 後熄燈"
                                            f"（出現{'內' if buy else '外'}盤較多的 5 秒）。回測這個型態之後 5～30 分鐘小幅{back}的機會略高；"
                                            f"是轉折的確認，不是進場訊號")
        out["last"] = last_ev()
        return out

    def _exh_signs(self, c, q, now):
        """竭盡跡象（exhaust.py）：五檔、明細、K 棒三個畫面各自的型態。新出現的發 👁 警示（_track_alerts 會追蹤之後的走勢），
        回最近 5 分鐘三個畫面的狀態給儀表板。回測沒有優勢，所以模型只列出、不計分。要永豐逐筆。"""
        if not self.flow_src:
            return None
        nowts = now.timestamp()
        ticks, big = self.flow_src.recent_ticks(c, seconds=exhaust.WINDOW)
        L = self.signs.setdefault(c, [])
        if ticks:
            new = exhaust.detect(ticks, big, self.flow_src.series(c) or [], {"bids": q.get("bids"), "asks": q.get("asks")},
                                 nowts, self._sign_seen.setdefault(c, set()))
            for sn in new:
                if nowts - sn["ts"] <= 90:                       # 剛啟動時補出來的舊事件不發
                    L.append(sn)
                    self._add_alert(c, now, exhaust.alert_text(sn))
        L[:] = [x for x in L if nowts - x["ts"] <= exhaust.TTL]
        self.sign_sum[c] = exhaust.summary(L, nowts)
        return self.sign_sum[c]

    def _exh_bonus(self, c, side, now):
        """訊號方向跟最近 5 分鐘內「推過今高 / 殺破今低後熄燈」的竭盡事件一致就回那個事件（做空 ← 買盤竭盡；做多 ← 賣盤竭盡），否則 None。"""
        ex = self.exh.get(c)
        if not ex or not ex.get("near") or now.timestamp() - ex["ts"] > self.EXH_TTL:
            return None
        return ex if (ex["side"] == "buy") == (side == "short") else None

    def _exh_lit_against(self, c, side):
        """訊號方向的反向正在亮燈就回 (連次, 連量)：做空時連外還亮著、做多時連內還亮著。
        回測亮燈當下逆勢進場，之後 1 分鐘平均 −0.07% ~ −0.14%（價格多半還會往原方向再走），所以提醒等熄燈。"""
        rn = self.runs.get(c) or {}
        if not rn.get("lit"):
            return None
        if side == "short" and rn.get("out_n", 0) >= self.EXH_MIN_N:
            return rn["out_n"], rn["out_v"]
        if side == "long" and rn.get("in_n", 0) >= self.EXH_MIN_N:
            return rn["in_n"], rn["in_v"]
        return None

    LIQ_WINDOW_S, LIQ_MIN_N = 1200, 30      # 看最近 20 分鐘、每 10 秒取一次五檔，至少 30 筆才判斷
    LIQ_WIDE_THIN, LIQ_WIDE_SOFT = 40, 20   # 價差 ≥2 tick 的時間佔比（%）：≥40 算薄、≥20 算偏薄
    LIQ_L1_VALUE = 500_000                  # 最佳一檔掛單金額中位數低於這個（元）也算薄

    def _liquidity(self, c, q, now):
        """
        流動性（掛單薄不薄）：最近 20 分鐘的五檔，看 (1) 最佳買賣價各掛幾張 (2) 買賣價差 ≥2 tick 的時間佔比。
        11/10 志聖：最佳一檔中位數 4 張（約 125 萬）、44% 的時間價差 ≥2 tick，17 張就把價格推高 3 tick，
        使用者 310.5 的空單回補在反彈最高點 313.5。門檻照 11/05–11/10 十四檔的分布定：緯創 2%、廣達 5%、華通 0%、
        GIS 10%、晶豪科 12%（不薄）；十銓 21%、順德 23%、永擎 29%（偏薄）；志聖 44%、精誠 44%、彰源 46%、光鼎 55%、界霖 85%（薄）。
        回 {level: thin / soft / ok, l1, l1_value, wide_pct, depth5, max_lots, n}；樣本不夠回 None。漲跌停鎖住（單邊沒掛單）的不取樣。
        """
        import statistics
        p = q.get("ref")
        bids, asks = q.get("bids") or [], q.get("asks") or []
        h = self._book_hist.setdefault(c, [])
        ts = now.timestamp()
        if (p and bids and asks and "09:05" <= now.strftime("%H:%M") < "13:25" and bids[0][0] and asks[0][0] and asks[0][0] > bids[0][0]
                and bids[0][1] and asks[0][1] and (not h or ts - h[-1][0] >= 10)):
            h.append((ts, round((asks[0][0] - bids[0][0]) / I.tick_size(p)), bids[0][1], asks[0][1], sum(v for _, v in bids), sum(v for _, v in asks)))
        while h and ts - h[0][0] > self.LIQ_WINDOW_S:
            h.pop(0)
        if len(h) < self.LIQ_MIN_N or not p:
            return None
        l1 = statistics.median((r[2] + r[3]) / 2 for r in h)
        wide = sum(1 for r in h if r[1] >= 2) / len(h) * 100
        value = l1 * p * 1000
        level = "thin" if (wide >= self.LIQ_WIDE_THIN or value < self.LIQ_L1_VALUE) else "soft" if wide >= self.LIQ_WIDE_SOFT else "ok"
        return {"level": level, "l1": round(l1, 1), "l1_value": round(value), "wide_pct": round(wide), "n": len(h),
                "depth5": round(statistics.median((r[4] + r[5]) / 2 for r in h), 1), "max_lots": max(1, int(l1))}

    RR_MIN = 1.5            # 報酬（到前方第一個支撐 / 壓力）÷ 風險（到模型停損）低於這個值 → 「空間不夠」
    # 空間不夠時擋不擋（2026-11-17 改成不擋、只當參考）。11/10 志聖復盤後加的「空間過濾」，驗證下來沒有效：
    #   實盤 11/10–11/16：擋掉的 24 筆成交照做每筆 +0.06%（勝 11 輸 13），發出去的 23 筆每筆 −0.39%（勝 7 輸 16）。
    #   tw_stocker 5 分 K 回測（120 檔、2024/02–2026/04、515 天、69.9 萬個通用支撐壓力訊號、現行評分扣成本）：
    #   會被擋的 21% 每筆 −0.269%、沒被擋的 −0.283%，被擋的反而好 0.014%（以交易日重抽 95% 區間 +0.010～+0.017）；
    #   大盤平靜 / 中等 / 劇烈、2024 / 2025 上半 / 2025 下半～2026 各段都一樣，只有逆勢單擋了好 0.015%，都遠小於成本 0.235%。
    # 所以只在訊號上標「📏 空間參考」（儀表板、警示、signal_outcomes 的 space 欄位），訊號照發；改回 True 就恢復擋。
    SPACE_BLOCK = False
    # 環境組合門檻（2026-11-18，使用者：「沒有適合的進場點就不做」）：訊號要符合 env_combos.COMBOS 的 10 種實盤贏過的環境之一才發，
    # 不符合的記成 ⏸ 環境過濾、照樣追蹤假設結果。這 10 組只在實盤 90 筆裡贏過，大樣本（tw_stocker、歷史逐筆）沒有重現，
    # 樣本外 11/19–11/23 只有 2/11 勝；細節與限制見 env_combos.py；改 False 就回到不看環境。
    # 2026-11-24 起它是額外的門檻：VIX ≥48 恐慌暫停、急漲、逆勢過濾先擋，過了才看環境組合（重複 / 追價只提醒，見 make() 裡的說明）。
    ENV_GATE = True
    # 只寫參考、不擋的過濾（2026-11-24 復盤：全期驗證擋了沒有比較好，見 make() 裡的說明）。拿掉就恢復擋。
    REF_ONLY = ("重複過濾", "追價過濾")

    def _env_ctx(self, c, ev, q, now, inzone, run5):
        """訊號當下的環境（給 env_combos 判斷）：欄位跟 signal_outcomes 的 features 同一套算法（_track_alerts），研究和即時才對得上。"""
        ev = ev or {}
        inf = ev.get("info") or {}
        p = q.get("ref")
        hist = self.flow_hist.get(c) or []
        fl = self._flow_now.get(c)
        big_now = fl["big_net"] if fl else (hist[-1][1] if hist else None)
        big_10 = None
        if big_now is not None and hist:
            past = [r for r in hist if now.timestamp() - r[0] >= 540]
            if past:
                big_10 = big_now - past[-1][1]
        bids, asks = q.get("bids") or [], q.get("asks") or []
        bv = sum((v or 0) for _, v in bids); av = sum((v or 0) for _, v in asks)
        return {"time": now.strftime("%H:%M"), "factors": [f[0] for f in (ev.get("intraday_factors") or [])],
                "idx_trend_10m": inf.get("idx_trend_10m"), "group_trend_10m": inf.get("group_trend_10m"), "rs_vs_index": inf.get("rs_vs_index"),
                "chg_pct": inf.get("chg_pct"),
                "range_pos": ((p - q["l"]) / (q["h"] - q["l"])) if (p and q.get("h") and q.get("l") and q["h"] != q["l"]) else None,
                "big_net_10m": big_10, "small_net_pct": inf.get("small_net_pct"), "book_imb": (bv - av) / (bv + av) if bv + av else None,
                "vol_pace": inf.get("vol_pace"), "inzone": inzone, "run5": run5}
    # ──────────────────────────────────────────────── KD 參考（2026-11-25 加，使用者要求）
    # 大盤（加權）與個股的 KD(9,3,3)：盤中 5 分 K（跟江波圖下面的 KD 圖同一套算法）與日 KD（前一天為止 + 今天盤中這一根）。
    # 只顯示、寫進訊號紀錄（sig["kd"] → signal_outcomes），不加分、不擋訊號：還沒驗證 KD 對這套訊號有沒有用，
    # 照「過濾要先證明有效」的原則先當參考，之後用 signal_outcomes 的 kd 欄位分組比勝率再決定。
    WALL_HIDDEN_MAX = 600         # 掛單牆跑出看得到的五檔超過幾秒就不追了（_track_walls；看不到的時候不判斷撤單）
    IDX_DAY_RETRY = 600     # 加權日 K 抓失敗，隔多久再試

    def _idx_ser(self, k):
        """指數分 K [[HH:MM, 值]]：有永豐指數串流用串流，否則用 MIS 取樣組的。"""
        if self.index_stream:
            return self.flow_src.index_series(k)
        return [[kk, v[3]] for kk, v in sorted((self.idx_bars.get(k) or {}).items()) if kk != "_v"]

    def _idx_day_hlc(self):
        """加權指數前一天為止的日 K [(高, 低, 收)]（證交所 MI_5MINS_HIST，一天抓一次）；抓不到回 None。"""
        today = dt.date.today()
        st = self._idx_day
        if st["date"] == today and st["hlc"] is not None:
            return st["hlc"]
        if st["date"] == today and time.time() - st["ts"] < self.IDX_DAY_RETRY:
            return None
        st.update(date=today, ts=time.time(), hlc=None)
        try:
            import src_twse
            rows = src_twse.taiex_ohlc(today, months=2)
            st["hlc"] = [(h, l, c) for _, _, h, l, c in rows] or None
        except Exception as e:
            print(f"[KD] 加權日 K 抓不到，{self.IDX_DAY_RETRY // 60} 分鐘後再試：{str(e)[:80]}")
        return st["hlc"]

    def _kd_ref(self, c, q, bars, idx_ser):
        """大盤與個股的 KD：{idx_5m, idx_day, stk_5m, stk_day}，每個是 indicators.kd_state 的結果（或 None）。"""
        out = {}
        today = dt.date.today().isoformat()
        out["stk_5m"] = I.kd_state(I.tf_hlc(bars, 5)) if bars else None
        hist = [(r["h"], r["l"], r["c"]) for r in ((self.daily.get(c) or {}).get("hist_tail") or [])
                if r.get("h") and r.get("l") and r.get("c") and str(r.get("date", "")) < today]
        p, h, l = q.get("ref"), q.get("h"), q.get("l")
        out["stk_day"] = I.kd_state(hist + ([(h, l, p)] if (p and h and l) else [])) if hist else None
        out["idx_5m"] = I.kd_state(I.tf_hlc(idx_ser, 5)) if idx_ser else None
        ih = self._idx_day_hlc()
        vals = [v for _, v in (idx_ser or []) if v]
        out["idx_day"] = I.kd_state(ih + ([(max(vals), min(vals), vals[-1])] if vals else [])) if ih else None
        return out

    @staticmethod
    def _kd_text(kdv):
        """KD 參考的一行字：加權 日 K/D、5 分 K/D；個股 同上（高檔 / 低檔 / 剛交叉另外標）。"""
        def one(x):
            if not x:
                return "-"
            tag = "".join(f"・{t}" for t in (x.get("zone"), x.get("cross")) if t)
            return f"K{x['k']:.0f}/D{x['d']:.0f}{tag}"
        kdv = kdv or {}
        return (f"加權 日 {one(kdv.get('idx_day'))}、5分 {one(kdv.get('idx_5m'))}；"
                f"個股 日 {one(kdv.get('stk_day'))}、5分 {one(kdv.get('stk_5m'))}（只顯示、未驗證）")

    REPEAT_TICKS = 3        # 進場區相差這麼多 tick 以內算「同一個價位」

    def _repeat_block(self, c, side, lo, hi, q, tick):
        """
        同一個價位的訊號結束後不重發，除非之後跌破新低（做空）/ 突破新高（做多）。回字串 = 擋的理由（⏸ 重複過濾，照樣追蹤假設結果）。
        11/10 志聖：11:35 空 309.5 四分鐘到 305.5（贏），之後今低 305 守了 30 分鐘、彈到 313.5；12:10 同一句話同一個價位又發一次，
        這次大盤在回升、量只剩 4 成，逆向 3 tick 記輸。模型不記得這個價位已經用過、低點也守住了。
        只看最近一筆同邊、同價位的：它結束時的今低（今高）之後有沒有再被跌破（突破）。重啟 monitor 會忘記今天發過的。
        """
        short = side == "short"
        ext_now = q.get("l") if short else q.get("h")
        pad = self.REPEAT_TICKS * tick
        for d in reversed(self._sig_done.get(c, [])):
            if d["side"] != side or hi < d["lo"] - pad - 1e-9 or lo > d["hi"] + pad + 1e-9:
                continue
            if ext_now and d.get("ext") and ((ext_now < d["ext"] - 1e-9) if short else (ext_now > d["ext"] + 1e-9)):
                return None
            zone = f"{d['lo']}" if d["lo"] == d["hi"] else f"{d['lo']}–{d['hi']}"
            return (f"這個價位（{zone}）{d['since'][:5]} 已經發過{d['label']}（{d['res']}），"
                    f"之後{'今低' if short else '今高'} {d.get('ext')} 沒再{'跌破' if short else '突破'}")
        return None

    BURST_SEC, BURST_TICKS = 15, 3

    def _burst_through(self, c, cur, p):
        """
        掛單被大單砸穿（撤單警告）：等回的掛單還沒成交，價格已到掛單價 1 tick 內，而且最近 15 秒是被**大單**從 ≥3 tick 外一路砸過來的
        （做多看內盤大單賣、做空看外盤大單買）→ 叫使用者撤單，這筆不算進場。
        回測（backtest_entry.py --burst-cancel，9 檔 × 42 天，3156 筆成交）：會被撤的 117 筆若照成交每筆淨 −0.37%（前後半段 −0.50% / −0.26%），
        全部平均 −0.22%。門檻放寬到「30 秒 ≥2 tick」會撤 506 筆、但那些只比平均差 0.02%，等於只是少做，所以只撤真的砸很兇的。
        慢慢回到掛單價、或砸下來但沒有大單的，不撤。
        要逐筆內外盤，只在永豐串流模式運作（MIS 10 秒快照看不出誰砸的）。回 None 或 {status, alert, ...}。
        """
        lo, hi = cur["zone"]
        lim = lo if cur["side"] == "short" else hi
        b = self._burst_check(c, cur["side"], lim, p)
        if not b:
            return None
        verb, side_txt, ext, big_vol, big = b["verb"], b["side_txt"], b["from"], b["big_vol"], b["big_lots"]
        status = f"⚠ 撤單：{self.BURST_SEC} 秒內從 {ext} 被大單{verb}到 {p}（{side_txt} {big_vol} 張，≥{big} 張算大單）"
        alert = (f"⚠ 撤單 {cur['label']} 掛 {lim}：{self.BURST_SEC} 秒內從 {ext} 被大單{verb}到 {p}（{side_txt} {big_vol} 張），"
                 f"還沒成交的單先撤，這筆不進；已經成交的照停損 {cur['stop']} 走。"
                 f"回測這種被大單{verb}穿才成交的單每筆淨 −0.37%，平均 −0.22%")
        return {**b, "status": status, "alert": alert}

    def _burst_check(self, c, side, lim, p):
        """_burst_through 的核心：價格到 lim 1 tick 內、最近 BURST_SEC 秒被大單從 ≥BURST_TICKS tick 外衝過來 → 回細節，否則 None。"""
        if not self.flow_src or not p or not lim:
            return None
        short = side == "short"
        tk = I.tick_size(lim)
        if (p < lim - tk) if short else (p > lim + tk):
            return None                                   # 還沒靠近掛單價
        try:
            ticks, big = self.flow_src.recent_ticks(c, self.BURST_SEC)
        except Exception:
            return None
        if not ticks or not big:
            return None
        ext = min(t[1] for t in ticks) if short else max(t[1] for t in ticks)
        run = ((lim - ext) if short else (ext - lim)) / tk
        agg_tt = 1 if short else 2
        big_vol = sum(t[2] for t in ticks if t[3] == agg_tt and t[2] >= big)
        if run < self.BURST_TICKS or big_vol <= 0:
            return None
        return {"time": dt.datetime.now().strftime("%H:%M:%S"), "px": p, "from": ext, "run_ticks": round(run, 1),
                "big_vol": big_vol, "big_lots": big, "verb": "拉" if short else "砸", "side_txt": "外盤大單買" if short else "內盤大單賣"}

    # ──────────────────────────────────────────────── 持倉面板（使用者自己的單）
    # 使用者在儀表板點選「排在哪個價 → 成交 → 出場」，模型就改盯他真正的排隊價與成交價：
    #   排隊中：撤單警告看他的排隊價（不是模型的掛單價）；價格到了提示按「成交」
    #   持倉中：只看停損/目標（不因中途回吐叫他跑），到停損明確叫出場、13:20 提醒平倉，即時算扣成本後的損益
    # 沒點任何東西：不假設他有單，訊號結果一律標「模型假設」。事件寫 logs/trades.jsonl（重啟時重放當天事件恢復狀態）。
    COST_PCT = 0.235          # 當沖來回成本 %（手續費 0.1425%×2 打 3 折 + 證交稅 0.15%）
    POS_ACTIONS = ("queue", "fill", "cancel", "skip", "exit", "undo")

    @staticmethod
    def _step(px, n, side):
        """從 px 往「更划算」的方向走 n tick：做多往下、做空往上（跨價位級距時 tick 大小跟著變）。"""
        for _ in range(n):
            px = round(px - I.tick_size(px - 1e-9), 2) if side == "long" else round(px + I.tick_size(px), 2)
        return px

    def _pos_choices(self, sg):
        """訊號的三個排隊價：模型價、再划算 1 tick、2 tick（不超過停損那側）。"""
        base = sg["price"] if sg.get("filled_at_alert", True) else (sg["zone"][1] if sg["side"] == "long" else sg["zone"][0])
        out = []
        for n in range(3):
            px = self._step(base, n, sg["side"])
            if (px > sg["stop"]) if sg["side"] == "long" else (px < sg["stop"]):
                out.append(px)
        return out

    def _pos_apply(self, c, ev, replay=False):
        """套用一個持倉事件（即時或重放）。回 (ok, 錯誤訊息)。"""
        act = ev["action"]
        cur = self.positions.get(c)
        stack = self._pos_stack.setdefault(c, [])
        if act == "undo":
            if not stack:
                return False, "沒有可以復原的動作"
            self.positions[c] = stack.pop()
            back = self.positions[c]
            if back and back.get("sig_since") and back["state"] in ("queued", "holding"):
                self.user_trades.pop((c, back["sig_since"]), None)    # 復原了出場/撤單 → 結果還沒定
            return True, None
        if act == "queue":
            if cur and cur["state"] in ("queued", "holding"):
                return False, "已經有一筆排隊或持倉"
            new = {"state": "queued", "side": ev["side"], "label": ev.get("label"), "q_px": ev["price"], "stop": ev["stop"],
                   "t1": ev["t1"], "t2": ev.get("t2"), "sig_since": ev.get("sig_since"), "model_px": ev.get("model_px"),
                   "queued_at": ev["time"], "lots": ev.get("lots") or 1, "flags": {}}
        elif act == "skip":
            new = {"state": "skipped", "sig_since": ev.get("sig_since"), "side": ev.get("side"), "closed_ts": ev["ts"], "flags": {}}
        elif act in ("fill", "cancel"):
            if not cur or cur["state"] != "queued":
                return False, "沒有排隊中的單"
            new = dict(cur, flags={})
            if act == "fill":
                new.update(state="holding", fill_px=ev["price"], filled_at=ev["time"])
            else:
                new.update(state="cancelled", closed_at=ev["time"], closed_ts=ev["ts"])
        elif act == "exit":
            if not cur or cur["state"] != "holding":
                return False, "沒有持倉"
            sgn = 1 if cur["side"] == "long" else -1
            ret = sgn * (ev["price"] / cur["fill_px"] - 1) * 100
            lots = cur.get("lots") or 1
            new = dict(cur, flags={}, state="closed", exit_px=ev["price"], closed_at=ev["time"], closed_ts=ev["ts"],
                       ret_pct=round(ret, 3), net_pct=round(ret - self.COST_PCT, 3),
                       ticks=round(sgn * (ev["price"] - cur["fill_px"]) / I.tick_size(cur["fill_px"]), 1),
                       pnl=round((ret - self.COST_PCT) / 100 * cur["fill_px"] * 1000 * lots))      # 扣成本後賺賠幾元（張數 × 1000 股）
        else:
            return False, "不認得的動作"
        stack.append(cur)
        del stack[:-20]
        self.positions[c] = new
        if new["state"] in ("closed", "cancelled", "skipped") and new.get("sig_since"):
            self.user_trades[(c, new["sig_since"])] = {k: new.get(k) for k in ("state", "side", "q_px", "fill_px", "exit_px", "ret_pct", "net_pct", "ticks", "lots", "pnl")}
        return True, None

    def _load_positions(self):
        """重啟時重放今天的持倉事件。"""
        path = os.path.join(LOG_DIR, "trades.jsonl")
        if not os.path.exists(path):
            return
        today = dt.date.today().isoformat()
        for line in open(path, encoding="utf-8"):
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("date") == today and ev.get("code") in self.codes:
                self._pos_apply(ev["code"], ev, replay=True)
        for c, p in self.positions.items():
            if p and p["state"] in ("queued", "holding"):
                print(f"[持倉] {c} 恢復：{'排隊' if p['state'] == 'queued' else '持倉'} {p['label']} {p.get('fill_px') or p['q_px']}")

    def position_action(self, body):
        """儀表板 POST /api/position：{code, action, price?}。回 (ok, {position} 或 {error})。"""
        c = str(body.get("code") or "")
        act = body.get("action")
        if c not in self.codes or act not in self.POS_ACTIONS:
            return False, {"error": "參數錯誤"}
        now = dt.datetime.now()
        with self.pos_lock:
            cur = self.positions.get(c)
            ev = {"date": now.date().isoformat(), "time": now.strftime("%H:%M:%S"), "ts": now.timestamp(), "code": c,
                  "name": self.daily[c]["name"], "src": self.src, "action": act}
            if act in ("queue", "fill", "exit"):
                try:
                    px = round(float(body.get("price")), 2)
                except (TypeError, ValueError):
                    return False, {"error": "價格錯誤"}
                if px <= 0:
                    return False, {"error": "價格錯誤"}
                ev["price"] = px
            if act == "queue":
                # 張數：復盤用金額算賺賠（空台積電賠 1 張、買富喬賺 1 張，加起來還是賠），沒給就當 1 張
                try:
                    raw = body.get("lots")
                    lots = 1 if raw in (None, "") else int(raw)
                except (TypeError, ValueError):
                    return False, {"error": "張數錯誤"}
                if not 1 <= lots <= 999:
                    return False, {"error": "張數要在 1～999 之間"}
                ev["lots"] = lots
            if act in ("queue", "skip"):
                sg = self.signals.get(c)
                if not sg:
                    return False, {"error": "現在沒有訊號"}
                ev.update(side=sg["side"], label=sg["label"], stop=sg["stop"], t1=sg["t1"], t2=sg.get("t2"), sig_since=sg["since"],
                          model_px=sg["price"], zone=sg.get("zone"))
            elif cur:
                ev.update(side=cur.get("side"), label=cur.get("label"), sig_since=cur.get("sig_since"), lots=cur.get("lots") or 1)
                if act == "exit":
                    ev.update(entry_px=cur.get("fill_px"), stop=cur.get("stop"), t1=cur.get("t1"))
            ok, err = self._pos_apply(c, ev)
            if not ok:
                return False, {"error": err}
            p = self.positions.get(c)
            if act == "exit" and p:
                ev.update(ret_pct=p["ret_pct"], net_pct=p["net_pct"], ticks=p["ticks"], pnl=p.get("pnl"))
            try:
                with open(os.path.join(LOG_DIR, "trades.jsonl"), "a", encoding="utf-8") as f:
                    f.write(json.dumps(ev, ensure_ascii=False) + "\n")
            except Exception as e:
                print(f"[持倉] 寫入失敗：{str(e)[:80]}")
            self._pos_last_act[c] = now.timestamp()
            return True, {"position": self._pos_view(c, (WEB.live.get("quotes") or {}).get(c, {}).get("ref") if WEB.live else None, now)}

    def _pos_view(self, c, p, now):
        """給儀表板的持倉狀態（含即時損益、距停損/目標幾 tick、可選的排隊價）。"""
        pos = self.positions.get(c)
        sg = self.signals.get(c)
        can_undo = now.timestamp() - self._pos_last_act.get(c, 0) < 60 and bool(self._pos_stack.get(c))
        if pos and pos["state"] in ("closed", "cancelled", "skipped") and now.timestamp() - pos.get("closed_ts", 0) > 180:
            pos = None                                         # 結束 3 分鐘後收起來
        if not pos or (pos["state"] in ("closed", "cancelled", "skipped") and sg and sg["since"] != pos.get("sig_since") and not sg.get("status")):
            if sg and not sg.get("status"):
                return {"state": "none", "side": sg["side"], "label": sg["label"], "choices": self._pos_choices(sg),
                        "sig_since": sg["since"], "can_undo": can_undo}
            return {"state": "none", "can_undo": can_undo} if can_undo else None
        v = {k: v for k, v in pos.items() if k != "flags"}
        v["now"] = p; v["can_undo"] = can_undo
        v["burst"] = pos["flags"].get("burst")
        v["hint"] = pos["flags"].get("hint")
        if pos["state"] == "holding" and p:
            sgn = 1 if pos["side"] == "long" else -1
            tk = I.tick_size(pos["fill_px"])
            ret = sgn * (p / pos["fill_px"] - 1) * 100
            v.update(pnl_ticks=round(sgn * (p - pos["fill_px"]) / tk, 1), pnl_pct=round(ret, 2), net_pct=round(ret - self.COST_PCT, 2),
                     pnl=round((ret - self.COST_PCT) / 100 * pos["fill_px"] * 1000 * (pos.get("lots") or 1)),
                     to_stop=round(sgn * (p - pos["stop"]) / tk, 1), to_t1=round(sgn * (pos["t1"] - p) / tk, 1))
            sm = (self.sign_sum.get(c) or {}).get("buy" if pos["side"] == "long" else "sell") or {}
            if sm.get("n", 0) >= 2:
                on = "、".join(f"{exhaust.CAT_LABEL[k]}（{sm[k]['name']}）" for k in ("book", "tick", "bar") if sm.get(k))
                v["exh_note"] = (f"{'買' if pos['side'] == 'long' else '賣'}盤竭盡跡象 {sm['n']}/3：{on}。"
                                 f"回測這些型態出現後並沒有更常反轉，照原本的停損和目標走")
        return v

    def _pos_watch(self, c, q, now):
        """快迴圈每輪：排隊中盯撤單、持倉中盯停損/目標/尾盤。回儀表板要的狀態。"""
        p = q.get("ref")
        with self.pos_lock:
            pos = self.positions.get(c)
            if pos and p:
                fl = pos["flags"]
                short = pos["side"] == "short"
                if pos["state"] == "queued":
                    qp = pos["q_px"]
                    if not fl.get("burst"):
                        b = self._burst_check(c, pos["side"], qp, p)
                        if b:
                            fl["burst"] = b
                            self._pos_alert(c, now, f"⚠ 撤單 你排的{pos['label']} {qp}：{self.BURST_SEC} 秒內從 {b['from']} 被大單{b['verb']}到 {p}"
                                                    f"（{b['side_txt']} {b['big_vol']} 張），還沒成交就先撤；已經成交的照停損 {pos['stop']} 走")
                            self._notify("exit", c, f"⚠ 撤單：你排的{pos['label']} {qp} 被大單{b['verb']}過來", "還沒成交就先撤")
                    if (p >= qp) if short else (p <= qp):
                        fl["hint"] = f"價格到了 {qp}，成交了就按「成交」"
                        if not fl.get("reach"):
                            fl["reach"] = True
                            self._notify("entry", c, f"價格到了你排的{pos['label']} {qp}", "成交了就在持倉面板按「成交」")
                    elif (p <= pos["t1"]) if short else (p >= pos["t1"]):
                        fl["hint"] = f"沒回到 {qp} 就到目標 {pos['t1']}，建議撤單不追"
                    elif now.strftime("%H:%M") >= "13:05":
                        fl["hint"] = "13:05 後不進場，建議撤單"
                    else:
                        fl.pop("hint", None)
                elif pos["state"] == "holding":
                    if not fl.get("stop") and ((p >= pos["stop"]) if short else (p <= pos["stop"])):
                        fl["stop"] = True; fl["hint"] = f"⛔ 停損 {pos['stop']} 到了，照紀律出場"
                        self._pos_alert(c, now, f"⛔ 你的{pos['label']} {pos['fill_px']} 到停損 {pos['stop']}（現價 {p}）：照紀律出場，不要凹單、不要攤平")
                        self._notify("exit", c, f"⛔ 你的{pos['label']} 到停損 {pos['stop']}（現價 {p}）", "照紀律出場，不要凹單、不要攤平")
                    elif not fl.get("t1") and ((p <= pos["t1"]) if short else (p >= pos["t1"])):
                        fl["t1"] = True; fl["hint"] = f"✔ 目標 1 {pos['t1']} 到了，照計畫出場"
                        self._pos_alert(c, now, f"✔ 你的{pos['label']} {pos['fill_px']} 到目標 1 {pos['t1']}（現價 {p}）：照計畫出場")
                        self._notify("exit", c, f"✔ 你的{pos['label']} 到目標 1 {pos['t1']}（現價 {p}）", "照計畫出場")
                    if not fl.get("late") and now.strftime("%H:%M") >= "13:20":
                        fl["late"] = True; fl["hint"] = "⏰ 13:25 前一定要平倉"
                        self._pos_alert(c, now, f"⏰ 你的{pos['label']} {pos['fill_px']} 還沒平倉：13:25 前一定要出")
                        self._notify("exit", c, f"⏰ 你的{pos['label']} 還沒平倉", "13:25 前一定要出")
            return self._pos_view(c, p, now)

    def _signal(self, c, ev, bars, q, now):
        """
        「建議做空 / 做多」：模型那一邊強度 ≥60，而且進場確認條件**已經發生**，才掛出來：
          主計畫：最近 12 根 1 分 K 碰過壓力（支撐），且最後一根完成的 1 分 K 收在進場價之下（之上），現價還在進場價附近、沒到目標也沒過停損；
          備援：最後一根完成的 5 分 K 帶量（≥1.5 倍前 6 根均量）收破支撐 / 收過壓力，現價離觸發價 1% 內。
        反彈動能中不給空訊號、殺盤動能中不給多訊號；09:10 前、13:20 後不給。
        訊號會留著直到：順向到 2 倍風險（✔）、逆向超過 3 tick（✘）、或成交後 60 分鐘 / 13:25（看扣成本損益），跟結案評分一致，結束後再顯示 2 分鐘。
        進場後只看停損/目標，不因中途回吐提早出場（回測 8 組「浮盈回吐一半＋反向主動量就出」都比原本差）；
        防護都放在進場前：追價上限、逆勢過濾、掛單被大單砸穿就撤（_burst_through）。
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
                    burst = self._burst_through(c, cur, p)
                    if burst:
                        cur["status"] = burst["status"]; cur["cancelled"] = burst
                        for rec in self.pending:           # 結案時改判「撤單驗證」：照樣算假設成交的結果，不進勝率
                            if rec["code"] == c and rec.get("sig") and rec.get("time") == cur["since"] and rec["sig"].get("side") == cur["side"]:
                                rec["sig"]["cancelled"] = burst
                    elif (p >= lo) if short else (p <= hi):
                        cur.update(filled=True, fill_px=p, fill_ts=now.timestamp(), price=p)
                        self._add_alert(c, now, f"▶ 掛單成交 {cur['label']} @{p}（進場區 {lo}–{hi}）：停損 {cur['stop']}，目標 {cur['t1']} / {cur['t2']}")
                        self._notify("entry", c, f"▶ 掛單成交 {cur['label']} @{p}", f"停損 {cur['stop']}，目標 {cur['t1']} / {cur['t2']}")
                    elif (p <= cur["t1"]) if short else (p >= cur["t1"]):
                        cur["status"] = f"○ 未成交：沒回到 {lo}–{hi} 就到目標 1，放棄"
                    elif now.timestamp() - cur["since_ts"] > 1200:
                        cur["status"] = f"○ 未成交：20 分鐘沒回到 {lo}–{hi}，取消"
                    elif now.strftime("%H:%M") >= "13:05":
                        cur["status"] = "○ 未成交：13:05 後不進場，取消"
                else:
                    # 跟結案評分一致（_judge_signal）：逆向超過 3 tick 算輸、順向 2 倍風險算贏、60 分鐘（最晚 13:25）看扣成本損益。
                    # 模型的停損 / 目標仍是技術面參考價，持倉面板（你自己的單）照那組走。
                    sc = cur.get("score") or self.score_levels(cur["side"], cur["price"])
                    entry = (cur["zone"][0] if short else cur["zone"][1]) if cur.get("filled_at_alert") is False else cur["price"]
                    if (p >= sc["stop"]) if short else (p <= sc["stop"]):
                        cur["status"] = f"✘ 逆向超過 {sc['heat_ticks']} tick（{sc['stop']}），記輸（模型停損 {cur['stop']} 是技術面參考）"
                    elif (p <= sc["tgt"]) if short else (p >= sc["tgt"]):
                        cur["status"] = f"✔ 順向到 {sc['tgt']}（2 倍風險），記贏（模型目標 {cur['t1']} 可續抱參考）"
                    elif now.timestamp() - (cur.get("fill_ts") or cur["since_ts"]) > self.SCORE_WINDOW_MIN * 60 or now.strftime("%H:%M") >= "13:25":
                        net = ((entry - p) if short else (p - entry)) / entry * 100 - self.COST_PCT
                        cur["status"] = f"⏱ 成交後 60 分鐘（或 13:25）到，現價 {p} 扣成本 {net:+.2f}%，記{'贏' if net > 0 else '輸'}"
                if cur.get("status"):
                    cur["end_ts"] = now.timestamp()
                    sh = cur["side"] == "short"
                    ext = [x for x in ((q.get("l") if sh else q.get("h")), p) if x]
                    res = {"✔": "贏", "✘": "輸", "⏱": "到時結算", "○": "未成交"}.get(cur["status"][:1], "撤單")
                    self._sig_done.setdefault(c, []).append({"side": cur["side"], "lo": cur["zone"][0], "hi": cur["zone"][1], "since": cur["since"],
                                                             "label": cur["label"], "res": res, "ext": (min(ext) if sh else max(ext)) if ext else None})
                    self._add_alert(c, now, f"{cur['status']}：{cur['label']} @{cur['price']}" if not cur.get("cancelled") else cur["cancelled"]["alert"])
                    if res != "未成交":
                        self._notify("exit", c, (cur["cancelled"]["alert"] if cur.get("cancelled") else f"{cur['status'].split('，')[0]}：{cur['label']} @{cur['price']}")[:70],
                                     "模型的訊號結案（你自己的單照持倉面板的停損 / 目標）")
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
        # 竭盡加分立刻生效：模型 60 秒才重算一次，這一輪估算還沒把它列成因子的話，先在這裡補上（±0.3 分 ≈ 強度 3 點）
        counted = {f[0] for f in (ev.get("intraday_factors") or [])}
        pts = round(11 * self.EXH_BONUS, 1)
        if self._exh_bonus(c, "short", now) and "買盤竭盡" not in counted:
            ps, pl = min(85, ps + pts), max(15, pl - pts)
        if self._exh_bonus(c, "long", now) and "賣盤竭盡" not in counted:
            pl, ps = min(85, pl + pts), max(15, ps - pts)

        def vol_ok():
            if len(f5) < 4:
                return False
            prev = [x[5] for x in f5[-7:-1]]
            return bool(prev) and f5[-1][5] >= 1.5 * (sum(prev) / len(prev))

        # 午盤量縮時 1 分 K 的「確認」沒有意義（11/05 晶豪科 12:03 五分鐘只成交 118 張）：最近 3 根合計要有 20 根均量的一半
        v20 = [b[5] or 0 for b in completed[-20:]]
        avg20 = sum(v20) / len(v20) if v20 else 0
        thin = avg20 > 0 and sum((b[5] or 0) for b in completed[-3:]) < 1.5 * avg20

        def make(side, base, label, stop, t1, t2, why, t1_name=None):
            short = side == "short"
            z = self._entry_zone(side, base, stop, t1)
            room = None
            space = None
            if not z:
                # 到前方第一個支撐 / 壓力的空間不夠（進場區內湊不到報酬風險比 1.5）。SPACE_BLOCK 決定擋不擋（見該常數說明），
                # 不擋時只在訊號上註明「空間參考」，照樣記進 signal_outcomes（space 欄位），之後還能分組比
                z = self._entry_zone(side, base, stop, t1, rr_min=0)
                risk0 = abs(stop - base)
                if not z or not risk0:
                    return None
                txt = (f"到前方第一個{'支撐' if short else '壓力'} {t1}{f'（{t1_name}）' if t1_name else ''}只有 {abs(base - t1) / tick:.0f} tick，"
                       f"報酬風險比 {abs(base - t1) / risk0:.1f} 不到 {self.RR_MIN}")
                space = {"text": txt, "t1": t1, "t1_name": t1_name, "ticks": round(abs(base - t1) / tick),
                         "rr": round(abs(base - t1) / risk0, 2), "rr_min": self.RR_MIN}
                if self.SPACE_BLOCK:
                    room = txt
            lo, hi = z
            key = (c, side, lo, hi)
            if now.timestamp() - self._sig_seen.get(key, 0) < 900:
                return None
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
                   "alert_px": p, "chase": round(chase, 3), "rr_now": rr_now and round(rr_now, 2), "rr_zone": rr_zone and round(rr_zone, 2),
                   "space": space}
            lim_why = self._limit_block(c, side, p, t1, q)
            if lim_why:
                # 漲停鎖死買不到、跌停鎖死空不到、目標超過漲跌停：不發，也不追蹤假設結果（根本成交不了）
                lkey = ("limit", c, side)
                if now.timestamp() - self._sig_seen.get(lkey, 0) >= 900:
                    self._sig_seen[lkey] = now.timestamp()
                    self._add_alert(c, now, f"⏸ 漲跌停過濾：原本要建議{label} @{p}，但{lim_why}，不發")
                return None
            sig["run5"], sig["run5_tk"] = self._run_before(bars, p, side, now)
            # 環境組合（2026-11-18）：當下環境要符合實盤贏過的前 10 種組合之一才發，沒有適合的進場點就不做（env_combos.py）
            env_ctx = self._env_ctx(c, ev, q, now, inzone, sig.get("run5"))
            env = env_combos.match(side, env_ctx)
            sig["env"] = [cb["id"] for cb in env]
            sig["env_conds"] = sorted(env_combos.conditions(side, env_ctx))
            sig["env_txt"] = [env_combos.label(cb) for cb in env]
            # 過濾（2026-11-24 收盤後復盤定案）：擋的只有「恐慌暫停、急漲、逆勢、環境組合」；重複 / 追價只寫參考、不擋。
            #   11/19–11/24 發出去的訊號（含被擋的假設結果）各種組合比過：
            #     環境組合 + 只擋逆勢 + VIX ≥48 暫停：10 筆 6 勝、每筆 +0.19%、1 張 +10,916 元（最好）
            #     11/18 版（環境組合、三個只提醒）：20 筆 7 勝、−0.30%、−425 元；今早版（三個都擋 + VIX 暫停）：3 筆 0 勝、−3,994 元
            #     同上但不看環境組合：19 筆 7 勝、−0.26%、−284 元（環境組合有用：被它擋的 17 筆 4 勝、每筆 −0.45%）
            #   11/10–11/24 全期，各過濾成立時照做：重複 19 筆 58% 勝、每筆 +0.24%（比平均好，擋了反而虧）；
            #     追價 32 筆 34% 勝、−0.29%（跟全部訊號平均 −0.3% 一樣，擋了沒差）；11/24 帶追價 / 重複發出去的 7 筆贏 6 筆。
            #     逆勢 10 筆 40% 勝、−0.08%：沒有證明有效，但它擋的是「大盤正在急拉時放空」（11/23 發出去 0/4），
            #     護盤期使用者要小心這種拉抬，所以照擋。
            # 恐慌暫停：臺指 VIX ≥48（fear.INTRADAY_PAUSE，盤中即時值，沒有就用昨收）時盤中訊號全部不發、照樣追蹤假設結果。
            #   實盤 11/10–11/23（signal_outcomes，含被擋的假設結果）：VIX ≥48 時做空 23 筆只贏 4 筆、每筆 −0.54%（發出去的 13 筆贏 1 筆），
            #   做多 21 筆贏 9 筆、每筆 −0.03%；VIX <40 時做空 17 筆贏 10 筆 +0.30%。兩邊都沒有優勢，照「沒有適合的就不做」。樣本小（6 天）。
            # 每一種過濾成不成立都記在 sig["filters"]（含只提醒的重複 / 追價），之後可以各自驗證；只擋第一個會擋的。
            fz = WEB.fear or {}
            vv = fz.get("now") if fz.get("now") is not None else fz.get("prev")
            vix_blk = (f"臺指 VIX {vv:.1f} ≥{fear.INTRADAY_PAUSE:g} 極度恐慌，盤中不做（實盤 VIX ≥48 時做空 23 筆只贏 4 筆、"
                       f"做多 21 筆贏 9 筆每筆 −0.03%）") if (fz.get("pause") and vv is not None) else None
            checks = (("恐慌暫停", vix_blk),
                      ("重複過濾", self._repeat_block(c, side, lo, hi, q, tick)),
                      ("急漲過濾", self._surge_block(c, side, p, q)),
                      ("追價過濾", self._chase_block(side, sig["run5"], sig["run5_tk"])),
                      ("逆勢過濾", self._against_trend(side, info)))
            hits = [(k, why_) for k, why_ in checks if why_]
            if self.ENV_GATE and not env:
                hits.append(("環境過濾", f"當下環境不符合實盤贏過的前 10 種組合（成立的條件：{'、'.join(sig['env_conds']) or '無'}），"
                                       f"沒有適合的進場點就不做"))
            sig["filters"] = [{"kind": k, "text": why_} for k, why_ in hits]
            sig["refs"] = [{"kind": k[:2], "text": why_} for k, why_ in hits if k in self.REF_ONLY]
            hits = [(k, why_) for k, why_ in hits if k not in self.REF_ONLY]
            if room:
                hits.insert(0, ("空間過濾", room))
            kind, blk = hits[0] if hits else (None, None)
            if fz.get("now") is not None or fz.get("prev") is not None:
                sig["fear"] = {k: fz.get(k) for k in ("now", "prev", "chg_pct", "level", "extreme", "surge", "pause")}
            if side == "long" and fz.get("surge") and fz.get("now") is not None:
                # 回測（tw_stocker 2024–2026）：盤中 VIX 比昨收高 ≥10% 時做多訊號抱到收盤每筆 −0.38%（平常 −0.32%）；幅度小，只提醒不擋
                sig.setdefault("refs", []).append({"kind": "恐慌升溫", "text": f"臺指 VIX {fz['now']:.1f} 比昨收高 {fz['chg_pct']:.0f}%："
                                                   "回測這時做多訊號抱到收盤每筆 −0.38%（平常 −0.32%）"})
            pl = SUPPORT_DET.recent(now)
            if pl:
                sig["support_pull"] = {k: pl.get(k) for k in ("time", "r_hw", "r_tx", "r_otc")}
                sig.setdefault("refs", []).append({"kind": "護盤拉抬", "text": (f"{pl['time'][:5]} 權值股突然被拉（加權 5 分鐘 {pl['r_tx']:+.1f}%）："
                    + ("護盤期這種拉抬 7 次裡 6 次收盤更低，追多小心" if side == "long" else "可能再被拉一段，停損放寬或等拉完再進；護盤期 7 次裡 6 次收盤更低"))})
            try:
                sig["kd"] = self._kd_ref(c, q, bars, self._idx_ser("t00"))
                sig.setdefault("refs", []).append({"kind": "KD", "text": self._kd_text(sig["kd"])})
            except Exception as e:
                print(f"[{now:%H:%M:%S}] {c} KD 參考失敗：{str(e)[:100]}")
            try:
                # 國際休市（2026-11-26 加）：昨晚美股、今天日韓新陸有沒有開。每筆都記 flags（之後比休市日的訊號表現），有休市才寫參考
                hs = WEB.holidays()
                sig["holidays"] = hs.get("flags")
                ht = world_holidays.ref_text(hs)
                if ht:
                    sig.setdefault("refs", []).append({"kind": "休市", "text": ht})
            except Exception as e:
                print(f"[{now:%H:%M:%S}] {c} 休市參考失敗：{str(e)[:100]}")
            try:
                sig["orderflow"] = self.of.summary(c, now)      # 撤單觀察 / 程式單：只記錄，之後驗證
            except Exception:
                pass
            if blk:
                bkey = ("block",) + key            # 被擋的另外去重：趨勢轉回來後同一個進場區還能正常發
                if now.timestamp() - self._sig_seen.get(bkey, 0) < 900:
                    return None
                if kind in ("空間過濾", "重複過濾", "環境過濾", "恐慌暫停"):
                    # 這幾種常常連續成立（VWAP 每分鐘在動，進場區跟著變）：同一檔同一邊 10 分鐘只記一次，免得洗版
                    tkey = ("blockkind", c, side, kind)
                    if now.timestamp() - self._sig_seen.get(tkey, 0) < 600:
                        return None
                    self._sig_seen[tkey] = now.timestamp()
                self._sig_seen[bkey] = now.timestamp()
                sig.update(blocked=True, block_reason=blk, block_kind=kind)
                self.sig_block[c] = sig
                self._add_alert(c, now, f"⏸ {kind}：原本要建議{label} @{p}（{why.split('（')[0]}），但{blk}，不發；照樣追蹤假設進場的結果")
                return None
            self._sig_seen[key] = now.timestamp()
            self.signals[c] = sig
            sc = self.score_levels(sig["side"], p if inzone else (lo if short else hi))
            sig["score"] = sc
            score_txt = f"｜評分：逆向超過 {sc['heat_ticks']} tick（{sc['stop']}）算輸、到 {sc['tgt']} 算贏，60 分鐘內都沒到看扣成本損益"
            if space:
                score_txt += f"｜📏 空間參考：{space['text']}（只提醒、不擋訊號）"
            if env:
                score_txt += f"｜✅ 環境符合 {env_combos.label(env[0])}" + (f" 等 {len(env)} 組" if len(env) > 1 else "")
            for rf in sig.get("refs") or []:
                score_txt += f"｜ⓘ {rf['kind']}參考（只提醒）：{rf['text']}"
            ex = self._exh_bonus(c, side, now)
            if ex:
                sig["exh"] = {k: ex.get(k) for k in ("side", "time", "n", "vol", "vr", "push_tk", "push_pct", "ext", "pre", "over", "kind")}
                score_txt += (f"｜➕ 轉折確認：{ex['time']} {'買' if short else '賣'}盤竭盡（連{'外' if short else '內'} {ex['n']} 次 {ex['vol']} 張"
                              f"{'推過今高' if short else '殺破今低'} {ex['pre']} 到 {ex['ext']} 後熄燈）")
            la = self._exh_lit_against(c, side)
            if la:
                sig["exh_lit"] = {"n": la[0], "vol": la[1]}
                score_txt += (f"｜⏳ 連{'外' if short else '內'}還亮著燈（{la[0]} 次 {la[1]} 張）：{'買' if short else '賣'}盤還沒停，"
                              f"等熄燈再進比較穩（回測亮燈中逆勢進場，下一分鐘多半還會往原方向走）")
            lq = self.liq.get(c)
            if lq and lq["level"] == "thin":
                sig["liq"] = lq
                score_txt += f"｜⚠ 掛單薄（最佳一檔約 {lq['l1']:.0f} 張）：單筆 ≤{lq['max_lots']} 張、用限價"
            env_note = f"｜{sig['env_txt'][0]}" if sig.get("env_txt") else ""
            if inzone:
                self._notify("entry", c, f"▶ 建議{label} @{p}", f"停損 {stop}，目標 {t1} / {t2}{env_note}")
            else:
                self._notify("entry", c, f"▶ 建議{label} 掛 {lo}–{hi}（現價 {p}，等回再進）", f"停損 {stop}，目標 {t1} / {t2}{env_note}")
            if inzone:
                self._add_alert(c, now, f"▶ 建議{label} @{p}：{why}；停損 {stop}，目標 {t1}{f'（{t1_name}）' if t1_name else ''} / {t2}（報酬風險比 {rr_now:.1f}）{score_txt}")
            else:
                self._add_alert(c, now, f"▶ 建議{label} 掛 {lo}–{hi}（現價 {p} 已{'殺低' if short else '追高'}，報酬風險比只剩 {rr_now:.1f}，等回再進）："
                                        f"{why}；停損 {stop}，目標 {t1} / {t2}{score_txt}")
            return sig

        s = plan.get("short")
        if s and ps >= 60 and ps > pl and mom != "up" and not (info.get("warrant_surge_5m") and (info.get("book_imb") or 0) > 0.35):
            trig, entry, stop, t1, t2 = s["trigger_px"], s["entry_px"], s["stop"], s["t1"], s["t2"]
            touched = any(b[2] >= trig - tick for b in recent)
            rw = real_wall("bid")
            if rw and t1 < rw["px"] < p:
                touched = False   # 真支撐擋在目標前面，空單不發
            if touched and not thin and last[4] <= entry and t1 < p <= entry + tick and p < stop:
                return make("short", entry, "做空", stop, t1, t2, f"反彈 {trig} 不過，1 分 K 收 {last[4]} 確認（偏空強度 {ps:.0f}）", s.get("t1_name"))
            bd = s.get("bd_trigger")
            if bd and f5 and f5[-1][4] < bd and vol_ok() and s.get("bd_target") and s["bd_target"] < p < bd and (bd - p) / p < 0.01:
                return make("short", bd, "做空（破線追）", s["bd_stop"], s["bd_target"], s["bd_target"], f"5 分 K 帶量收破 {bd}（量 {f5[-1][5]} 張，偏空強度 {ps:.0f}）", s.get("bd_target_name"))
        l = plan.get("long")
        if l and pl >= 60 and pl > ps and mom != "down":
            trig, entry, stop, t1, t2 = l["trigger_px"], l["entry_px"], l["stop"], l["t1"], l["t2"]
            touched = any(b[3] <= trig + tick for b in recent)
            rw = real_wall("ask")
            if rw and p < rw["px"] < t1:
                touched = False
            if touched and not thin and last[4] >= entry and entry - tick <= p < t1 and p > stop:
                return make("long", entry, "做多", stop, t1, t2, f"回測 {trig} 守住，1 分 K 收 {last[4]} 確認（偏多強度 {pl:.0f}）", l.get("t1_name"))
            bo = l.get("bo_trigger")
            if bo and f5 and f5[-1][4] > bo and vol_ok() and l.get("bo_target") and bo < p < l["bo_target"] and (p - bo) / p < 0.01:
                return make("long", bo, "做多（突破追）", l["bo_stop"], l["bo_target"], l["bo_target"], f"5 分 K 帶量收過 {bo}（量 {f5[-1][5]} 張，偏多強度 {pl:.0f}）", l.get("bo_target_name"))
        return None

    BLOCK_TYPES = ("逆勢過濾", "急漲過濾", "空間過濾", "重複過濾", "追價過濾", "環境過濾", "恐慌暫停")     # 被擋掉、但照樣追蹤假設進場結果的訊號
    # 每一種 BLOCK_TYPES 都要在 ALERT_TYPES 有對應的樣式，不然 ⏸ 提醒會被歸成「其他」、不會寫進 signal_outcomes
    # （2026-11-25：恐慌暫停漏了，當天 13 筆被擋的訊號沒有追蹤結果，復盤顯示被擋 0 筆；之後補回）
    ALERT_TYPES = exhaust.alert_types() + combo.alert_types() + [("大盤急漲", r"^⚠ 大盤急漲"), ("大盤急跌", r"^⚠ 大盤急跌"),
                   ("認購權證急漲", r"認購權證.*急漲"), ("認購權證急跌", r"認購權證.*急跌"), ("認售權證急漲", r"認售權證.*急漲"),
                   ("權證爆量", r"權證.*成交.*張"), ("主力急回補", r"主力 \d+ 分鐘內回補"), ("主力急出", r"主力 \d+ 分鐘內出掉"),
                   ("主力加速出貨", r"再倒"), ("主力加速吃貨", r"再買"), ("主力拉高出貨", r"拉高出貨"),
                   ("反彈動能", r"反彈動能"), ("殺盤動能", r"殺盤動能"), ("權證急漲+委買厚", r"權證急漲 \+ 委買"),
                   ("建議做空", r"▶ 建議做空"), ("建議做多", r"▶ 建議做多"), ("掛單成交", r"▶ 掛單成交"), ("逆勢過濾", r"⏸ 逆勢過濾"), ("漲跌停過濾", r"⏸ 漲跌停過濾"), ("急漲過濾", r"⏸ 急漲過濾"), ("空間過濾", r"⏸ 空間過濾"), ("重複過濾", r"⏸ 重複過濾"), ("追價過濾", r"⏸ 追價過濾"), ("環境過濾", r"⏸ 環境過濾"), ("恐慌暫停", r"⏸ 恐慌暫停"), ("撤單", r"⚠ 撤單 "), ("延緩撮合", r"延緩撮合"),
                   ("掛單薄", r"^⚠ 掛單薄"), ("買盤竭盡", r"^➕ 買盤竭盡"), ("賣盤竭盡", r"^➕ 賣盤竭盡"),
                   # 撤單觀察 / 程式單偵測（orderflow.py，2026-11-26 加）：分類後 alert_outcomes 有 10 / 30 分鐘後的價格，之後驗證有沒有用
                   ("撤單觀察", r"^⚠ 撤單觀察"), ("整數撤單", r"^⚠ 整數撤單"), ("固定張數撤掛", r"^⚠ 固定張數(撤單|掛單|掛撤)"),
                   ("掛單牆", r"^⚠ (買單牆|賣單牆)"), ("疑似程式單", r"^⚠ 疑似程式單"), ("固定張數成交", r"^⚠ 固定張數成交"), ("大單拆單", r"^⚠ 大單拆單"), ("固定張數連發", r"^⚠ 固定張數連發")]

    @classmethod
    def _alert_type(cls, text):
        import re
        for name, pat in cls.ALERT_TYPES:
            if re.search(pat, text):
                return name
        return "其他"

    def _track_alerts(self, c, q, flow, bars, now, track_until=None):
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
            if track_until is not None and ts.timestamp() > track_until + 1:
                continue   # 已移除、收尾中：移除後才出現的（例如訊號結案的 ✔ / ✘）不再追蹤
            typ = self._alert_type(txt)
            extra = {}
            if typ in ("建議做空", "建議做多") + self.BLOCK_TYPES:
                sg = (self.sig_block.get(c) if typ in self.BLOCK_TYPES else self.signals.get(c)) or {}
                ev = WEB.latest.get(c) or {}
                inf = ev.get("info") or {}
                walls = self.walls.get(c, {})
                extra = {"sig": {k: sg.get(k) for k in ("side", "price", "stop", "t1", "t2", "why", "label", "zone", "filled_at_alert", "alert_px", "chase", "run5", "run5_tk", "rr_now", "rr_zone", "blocked", "block_reason", "block_kind", "cancelled", "exh", "exh_lit", "space", "env", "env_conds", "refs", "filters", "fear", "support_pull", "kd", "holidays", "orderflow")},
                         "features": {"idx_trend_10m": inf.get("idx_trend_10m"), "group_trend_10m": inf.get("group_trend_10m"),
                                      "momentum": (inf.get("momentum") or {}).get("dir"), "vol_pace": inf.get("vol_pace"),
                                      "rs_vs_index": inf.get("rs_vs_index"), "group_avg": inf.get("group_avg"), "chg_pct": inf.get("chg_pct"),
                                      "warrant_surge": inf.get("warrant_surge_5m"), "small_net_pct": inf.get("small_net_pct"),
                                      "p_long": ev.get("p_long"), "p_short": ev.get("p_short"),
                                      "liq_level": (self.liq.get(c) or {}).get("level"), "liq_l1": (self.liq.get(c) or {}).get("l1"),
                                      "liq_wide_pct": (self.liq.get(c) or {}).get("wide_pct"),
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
            # 建議訊號：成交後評分窗 60 分鐘（等回掛單另加最多 20 分鐘等成交），13:25 強制平倉後就能結案
            if rec.get("sig"):
                need = (self.SCORE_WINDOW_MIN + (20 if not rec["sig"].get("filled_at_alert", True) else 0) + 1) * 60
                done = "30m" in rec["out"] and (age >= need or now.strftime("%H:%M") >= "13:26")
            else:
                done = "30m" in rec["out"]
            if done or (closed and age > 60):
                if rec.get("sig"):
                    self._judge_signal(rec, bmap, m0)
                with open(dev_path(LOG_DIR, "alert_outcomes", ".jsonl"), "a", encoding="utf-8") as f:
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

    # 勝負評分（11/09 改）：原本「30 分鐘內先到目標 1 / 停損」目標太遠，15 筆真實訊號 12 筆記平，等於沒在判。
    # 改成照使用者的判斷：成交後逆向最多容忍 3 tick（被軋 / 突破太遠就算輸），順向走到 2 倍風險就算贏；
    # 先到的算數（同一根 K 都到算輸，保守）；60 分鐘內都沒到（最晚 13:25 平倉）就看那時扣成本後賺賠，不再有「平」。
    # 3 tick 在不同價位差很多（24 元 0.6%、101 元 1.5%），所以停損距離超過 1% 時減少 tick 數，至少 1 tick。
    SCORE_HEAT_TICKS = 3
    SCORE_MAX_STOP_PCT = 1.0
    SCORE_RR = 2
    SCORE_WINDOW_MIN = 60
    SCORE_LAST_MIN = 13 * 60 + 25

    @staticmethod
    def _tick_step(p, n):
        """從 p 往上（n>0）或往下（n<0）走 n 個 tick，跨價位級距時照新的 tick 走。"""
        for _ in range(abs(n)):
            p = p + I.tick_size(p) if n > 0 else p - I.tick_size(p - 1e-9)
        return round(p, 2)

    @classmethod
    def score_levels(cls, side, px):
        """評分用的停損 / 停利價：停損 = 逆向第 (容忍 tick+1) 個 tick，停利 = 順向 2 倍那麼多 tick。"""
        sgn = -1 if side == "short" else 1
        heat = cls.SCORE_HEAT_TICKS
        while heat > 1 and abs(cls._tick_step(px, -sgn * (heat + 1)) - px) / px * 100 > cls.SCORE_MAX_STOP_PCT:
            heat -= 1
        n_stop = heat + 1
        return {"heat_ticks": heat, "stop": cls._tick_step(px, -sgn * n_stop), "tgt": cls._tick_step(px, sgn * n_stop * cls.SCORE_RR),
                "stop_ticks": n_stop, "tgt_ticks": n_stop * cls.SCORE_RR}

    def _judge_signal(self, rec, bmap, m0):
        """建議訊號的輸贏（評分規則見 SCORE_*）：
        成交：等回掛單的訊號 20 分鐘內 1 分 K 碰到進場區上緣（做空：高點碰到下緣）才算成交、成交價用掛單價；
              沒回到進場區就先到模型目標 1 = nofill，不算輸贏。
        成交後 60 分鐘內（最晚 13:25）：先逆向超過 3 tick = loss（exit=stop）；先順向到 2 倍風險 = win（exit=target）；
              都沒到 = 看最後一根 K 收盤扣成本 0.235% 後 >0 記 win、否則 loss（exit=time）。
        另記 mae / mfe（期間最大逆向 / 順向 tick）與 net_pct（扣成本後損益 %），平均每筆賺幾 % 比勝率重要。
        fast_adverse 照舊：成交後 5 分鐘內反向走掉模型停損距離一半以上（估錯，即使之後賺回來也要記）。"""
        sg = rec["sig"]; side = sg.get("side")
        stop, t1 = sg.get("stop"), sg.get("t1")
        if not (side and stop and t1 and rec.get("price")):
            return
        short = side == "short"
        sgn = -1 if short else 1
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
                    break          # 沒回到進場區就到模型目標 1：沒追到（跟即時訊號框一致）
            px = lim
        else:
            start, px = 1, (sg.get("price") or rec["price"])
        sl = self.score_levels(side, px)
        tk = I.tick_size(px)
        first_stop = first_t1 = None
        exit_kind = exit_px = None
        mae = mfe = 0.0
        adverse5 = 0.0
        last_close = None
        if start is not None:
            filled_later = start > 1 or (zone and sg.get("filled_at_alert") is False)
            end = min(start + self.SCORE_WINDOW_MIN, self.SCORE_LAST_MIN - m0 + 1)
            for i in range(start, max(end, start + 1)):
                b = bmap.get(m0 + i)
                if not b:
                    continue
                hi, lo = b[2], b[3]
                adv = (hi - px) if short else (px - lo)
                fav = (px - lo) if short else (hi - px)
                same_bar = filled_later and i == start      # 成交那根 K 先不算順向（不知道先後），逆向照算
                mae = max(mae, adv)
                if not same_bar:
                    mfe = max(mfe, fav)
                if i < start + 5:
                    adverse5 = max(adverse5, adv)
                hit_stop = (hi >= sl["stop"]) if short else (lo <= sl["stop"])
                hit_tgt = (not same_bar) and ((lo <= sl["tgt"]) if short else (hi >= sl["tgt"]))
                if hit_stop:
                    first_stop = i; exit_kind, exit_px = "stop", sl["stop"]; break
                if hit_tgt:
                    first_t1 = i; exit_kind, exit_px = "target", sl["tgt"]; break
                last_close = b[4]
            if exit_kind is None and last_close is not None:
                exit_kind, exit_px = "time", last_close
        net = None
        if start is None:
            verdict = "nofill"
        elif exit_kind is None:
            verdict = "nofill"                               # 成交後沒有任何分 K（資料斷掉），不算
        else:
            net = round(sgn * (exit_px - px) / px * 100 - self.COST_PCT, 3)
            verdict = "win" if (exit_kind == "target" or (exit_kind == "time" and net > 0)) else "loss"
        risk = abs(stop - px)
        fast_adverse = bool(start is not None and risk and adverse5 >= 0.5 * risk)
        score = {"scoring": 2, "score_stop": sl["stop"], "score_tgt": sl["tgt"], "heat_ticks": sl["heat_ticks"], "tick": tk,
                 "exit": exit_kind, "exit_px": exit_px, "net_pct": net,
                 "mae_ticks": round(mae / tk, 1) if start is not None else None, "mfe_ticks": round(mfe / tk, 1) if start is not None else None}
        feat = dict(rec.get("features") or {})
        feat.update({"book_imb": rec.get("book_imb"), "big_net_10m": rec.get("big_net_10m"), "small_net_pct": feat.get("small_net_pct", rec.get("small_net")),
                     "phase": rec.get("phase"), "range_pos": rec.get("range_pos")})
        suspects = self._suspects(side, feat) if (verdict == "loss" or fast_adverse) else []
        rec["verdict"] = verdict; rec["fast_adverse"] = fast_adverse; rec["adverse5"] = adverse5
        rec["first_stop_min"] = first_stop; rec["first_t1_min"] = first_t1; rec["suspects"] = suspects
        out = {"date": rec["date"], "time": rec["time"], "src": rec.get("src"), "side": side, "label": sg.get("label"), "price": px, "stop": stop, "t1": t1, "t2": sg.get("t2"),
               "zone": zone, "alert_px": sg.get("alert_px", rec["price"]), "filled_at_alert": sg.get("filled_at_alert"), "fill_min": start,
               "chase": sg.get("chase"), "run5": sg.get("run5"), "run5_tk": sg.get("run5_tk"), "rr_now": sg.get("rr_now"), "rr_zone": sg.get("rr_zone"),
               "verdict": verdict, "fast_adverse": fast_adverse, "first_stop_min": first_stop, "first_t1_min": first_t1,
               **score,
               "ret_10m": (rec["out"].get("10m") or {}).get("ret_pct"), "ret_30m": (rec["out"].get("30m") or {}).get("ret_pct"),
               "suspects": suspects, "why": sg.get("why"), "features": feat, "factors": rec.get("factors"), "exh": sg.get("exh"), "exh_lit": sg.get("exh_lit"),
               "space": sg.get("space"), "env": sg.get("env"), "env_conds": sg.get("env_conds"), "refs": sg.get("refs"), "filters": sg.get("filters"), "fear": sg.get("fear"), "support_pull": sg.get("support_pull"), "kd": sg.get("kd"), "holidays": sg.get("holidays"), "orderflow": sg.get("orderflow")}
        if sg.get("blocked"):
            out.update(blocked=True, block_reason=sg.get("block_reason"), block_kind=sg.get("block_kind"))
        if sg.get("cancelled"):
            out.update(cancelled=sg["cancelled"])
        ut = self.user_trades.get((rec["code"], rec["time"]))   # 使用者自己的結果只放提醒，不寫進 signal_outcomes（公開 repo）
        try:
            with open(dev_path(LOG_DIR, "signal_outcomes", ".jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps({"code": rec["code"], "name": rec.get("name"), **out}, ensure_ascii=False) + "\n")
            if sg.get("blocked"):
                # 被擋的訊號不進交戰紀錄勝率，只回報「擋對了沒」
                res = {"win": "會 ✔ 贏 → 擋錯了", "loss": "會 ✘ 輸 → 擋對了", "flat": "會 — 平", "nofill": "不會成交"}[verdict]
                self._add_alert(rec["code"], dt.datetime.now(), f"⏸ 過濾驗證：{rec['time']} 被{sg.get('block_kind') or '過濾'}擋的{sg.get('label')} @{rec['price']} 若進場{res}")
                return
            if sg.get("cancelled"):
                # 撤單的不進交戰紀錄勝率，只回報「撤對了沒」（verdict 是假設掛單照樣成交的結果）
                res = {"win": "會 ✔ 贏 → 撤錯了", "loss": "會 ✘ 輸 → 撤對了", "flat": "會 — 平", "nofill": "其實不會成交"}[verdict]
                self._add_alert(rec["code"], dt.datetime.now(), f"⚠ 撤單驗證：{rec['time']} {sg.get('label')} 掛 {px} 若沒撤{res}")
                return
            history.record_signal(rec["code"], out)
            wl = {"win": "✔ 贏", "loss": "✘ 輸", "flat": "— 平", "nofill": "○ 未成交"}[verdict] + ("（5 分鐘內反向，估錯）" if fast_adverse else "")
            # 使用者沒在持倉面板記這筆 → 明講是模型假設，不是他的損益
            mine = ""
            if ut and ut.get("state") == "closed":
                mine = f"｜你的單 {ut.get('fill_px')}→{ut.get('exit_px')} 扣成本 {ut.get('net_pct'):+.2f}%"
            elif ut:
                mine = {"cancelled": "｜你撤單了", "skipped": "｜你沒做"}.get(ut.get("state"), "")
            how = {"stop": f"逆向超過 {sl['heat_ticks']} tick（{sl['stop']}）", "target": f"順向到 {sl['tgt']}（2 倍風險）",
                   "time": f"60 分鐘（或 13:25）到時 {exit_px} 平"}.get(exit_kind, "")
            txt = (f"{wl}：{sg.get('label')} @{px}" + (f" → {how}，扣成本 {net:+.2f}%" if net is not None else "")
                   + (f"，可疑原因：{'、'.join(suspects)}" if suspects else ""))
            if ut:
                self._pos_alert(rec["code"], dt.datetime.now(), txt + mine)
            else:
                self._add_alert(rec["code"], dt.datetime.now(), "（模型假設）" + txt)
        except Exception as e:
            print(f"[fast] 訊號結案寫入失敗：{str(e)[:80]}")

    def _orderflow(self, c, q, now):
        """撤單觀察與程式單偵測（orderflow.py）：把這 3 秒新的五檔變動、逐筆餵進去，新的偵測進警示清單（同一檔 10 分鐘最多 6 則）。
        永豐五檔串流有接上就用每一次變動；沒有就用這一輪的五檔快照（MIS 10 秒一次，比較粗）。沒有逐筆時只算價格沒碰過的價位。"""
        evs, tks, n_t = [], [], None
        if self.flow_src:
            i0 = self.of.tick_index(c)
            tks, n_t = self.flow_src.ticks_from(c, i0)
            if n_t < i0:                       # 重啟回補把逐筆清單重建了：這一檔從頭算
                self.of.reset(c)
                tks, n_t = self.flow_src.ticks_from(c, 0)
            if q.get("book_src") == "shioaji":
                evs = self.flow_src.book_events(c, self.of.book_since(c))
        if not evs and q.get("bids") and q.get("asks"):
            evs = [(now.timestamp(), q["bids"], q["asks"])]
        texts, _, view = self.of.update(c, now, evs, tks, tick_i=n_t, has_ticks=bool(self.flow_src), price=q.get("ref"))
        for t in texts:
            self._add_alert(c, now, t)
        return view

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
            # 牆的價位跑出看得到的五檔（價格往反方向走、牆被擠到第 6 檔以外）時，五檔裡找不到它不代表撤單，只是看不到：
            #   2026-11-26 日月光 11:10:55 賣單牆 508（第 5 檔）在最佳賣價掉到 503 的那一個快照裡被擠出畫面，被當成整面撤掉
            #   報「假壓力」，其實前後每分鐘的五檔 508 都還掛 181～183 張。看不到的時候不更新、不判斷；回到五檔再接著追，
            #   回來時已經不在了就照實寫「離開五檔時不見了」（不算假支撐 / 假壓力）；看不到超過 WALL_HIDDEN_MAX 秒就不追了（不發警示）。
            hidden = False
            if w and not w.get("status") and len(levels) >= 5:
                pxs = [x for x, _ in levels]
                hidden = (w["px"] > max(pxs)) if key == "ask" else (w["px"] < min(pxs))
            if hidden:
                w.setdefault("hidden_ts", now.timestamp())
                w["now"] = p
                w["verdict"] = "離開五檔（看不到，不判斷）"
                if now.timestamp() - w["hidden_ts"] > self.WALL_HIDDEN_MAX:
                    w["status"], w["end_ts"] = "離開五檔太久，不追了", now.timestamp()
            elif w and not w.get("status"):
                cur = dict(levels).get(w["px"])
                traded = self.flow_src.traded_at(c, w["px"], w["since_ts"])[0] if self.flow_src else 0
                if cur is None:
                    cur = 0
                if w.pop("hidden_ts", None) is not None:
                    # 剛回到五檔：看不到的那段時間的增減分不出是撤單還是補單，從現在的量重新接著追（成交量照算，成交不會看不到）
                    if cur <= 0.2 * w["size0"] and traded < 0.5 * w["size0"]:
                        w["last"], w["traded"] = cur, traded
                        w["status"], w["end_ts"] = f"離開五檔期間不見了（這個價位只成交 {traded} 張，是看不到的時候撤的）：價格回來時這道牆已經不在，不算假{'支撐' if key == 'bid' else '壓力'}", now.timestamp()
                        self._add_alert(c, now, f"⚠ {lab} {w['px']}（{w['size0']} 張）{w['status']}")
                        out[key] = w
                        continue
                    w["last"], w["traded"] = cur, traded
                d_tr = traded - w["traded"]
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
                                        + ("破線在即，多單不要新進、空單準備順勢（已進場的多單照原停損走）" if key == "bid" else "突破在即，空單不要新進、多單準備順勢（已進場的空單照原停損走）"))
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
        ex = self.exh.get(c)
        return {"bars": bars, "idx_series": idx_series, "peer_hist": self.peer_hist.get(c, {}), "alerts": alerts, "market": mkt,
                "exhaust": ex if ex and ex.get("near") and now.timestamp() - ex["ts"] <= self.EXH_TTL else None,
                "signs": self.sign_sum.get(c)}

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

    def _notify(self, kind, c, title, msg=""):
        """出現建議進出場點時發聲（進場「叮」、出場「叮咚」）＋ Mac 系統通知橫幅，不用開儀表板網頁（notify.py）。
        被過濾擋掉的、只記錄的觀察不叫。失敗只印一行，不影響盤中流程。"""
        try:
            name = (self.daily.get(c) or {}).get("name", "")
            notify.notify(kind, f"{c} {name}｜{title}", msg)
        except Exception as e:
            print(f"[通知] 失敗：{str(e)[:80]}")

    def _pos_alert(self, c, now, text):
        """持倉面板的提醒（你的單到停損、撤單…）：只顯示在儀表板，不進 live_alerts，
        所以不會寫進 monitor_*.jsonl / alert_outcomes.jsonl 被推到公開 repo。"""
        self._add_alert(c, now, text, store=self.pos_alerts)

    def _add_alert(self, c, now, text, store=None):
        """即時警示：同一句 5 分鐘內不重複，最多留 30 筆。"""
        L = (self.live_alerts if store is None else store).setdefault(c, [])
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

    def _refresh_mis(self, now):
        """抓一次 MIS 報價放到 _last_quotes。失敗時有永豐串流就用串流湊（只少族群與五檔），沒有就把錯誤丟出去。"""
        try:
            self._last_quotes = src_mis.fetch(sorted(self.ex_codes))
            self._last_mis_t = time.time()
            self._mis_fail = 0
        except hc.HttpError as e:
            self._mis_fail += 1
            if not self.flow_src:
                raise
            if self._mis_fail in (1, 10, 100):
                print(f"[fast] MIS 失敗（第 {self._mis_fail} 次），改用 Shioaji 串流報價：{str(e)[:100]}")
            qs = self._quotes_from_shioaji(now)
            self._last_mis_t = time.time() - 5   # 5 秒後再試 MIS
            if not qs:
                raise
            self._last_quotes = qs

    def _bg_fetch(self, now, want_mis, want_wq):
        """在背景抓 MIS 報價和權證快照（一次只跑一個執行緒，上一個還沒回來就不再派）。
        權證快照的結果放 _wq_ready，由下一輪快迴圈套用並發警示，這樣警示清單只有快迴圈在改。"""
        if self._bg_th and self._bg_th.is_alive():
            return

        def work():
            if want_mis:
                try:
                    self._refresh_mis(now)
                except Exception as e:
                    print(f"[fast] MIS 抓取失敗（背景）：{str(e)[:100]}")
            if want_wq:
                res = {}
                for c in self.codes:
                    q = dict((self._last_quotes or {}).get(c) or {})
                    try:
                        q["ref"] = self.flow_src.last_price(c) or q.get("ref")
                    except Exception:
                        pass
                    wq = self._warrant_snapshot(c, q, now, 1)
                    if wq:
                        res[c] = wq
                self._wq_ready = res
                self._last_wq_t = time.time()

        self._bg_th = threading.Thread(target=work, daemon=True)
        self._bg_th.start()

    def quote_json(self):
        """
        /api/quote：成交價與五檔的快速通道。直接讀永豐串流在記憶體裡的最新狀態，不經過快迴圈，頁面每 0.5 秒拉一次。
        原本的路徑是「快迴圈每 3 秒整理一次 → 頁面每 3 秒拉 /api/live」，平均慢 3 秒、最多 6 秒，快迴圈卡在 MIS 時更久。
        每檔多回 age（最後一筆成交是本機幾秒前收到的）與 lag（交易所時間到本機收到差幾秒）。只有盤中、有串流時才開。
        """
        now = time.time()
        if not self.flow_src or self._stream_off or not self._last_quotes or self._capture_mode(dt.datetime.now()) != "live":
            return '{"on": false}'
        out = {"on": True, "ts": now, "quotes": {}, "index": {}}
        for c in self.codes:
            base = self._last_quotes.get(c)
            sn = self.flow_src.quote_snapshot(c)
            if not base or not sn or not base.get("y"):
                continue
            y = base["y"]
            q = {"ref": sn["last"], "y": y, "chg_pct": (sn["last"] / y - 1) * 100, "src": "shioaji",
                 "o": base.get("o") or sn["open"],
                 "h": max(sn["high"], base.get("h") or 0) if sn["high"] else base.get("h"),
                 "l": min(sn["low"], base.get("l") or sn["low"]) if sn["low"] else base.get("l"),
                 "v": max(sn["total_volume"] or 0, base.get("v") or 0),
                 "age": round(now - sn["recv"], 2) if sn["recv"] else None,
                 "lag": round(sn["recv"] - sn["t"], 2) if sn["recv"] and sn["t"] else None}
            if sn["bids"] or sn["asks"]:
                q["bids"], q["asks"], q["book_src"] = sn["bids"], sn["asks"], "shioaji"
                q["book_age"] = round(now - sn["book_recv"], 2) if sn["book_recv"] else None
            out["quotes"][c] = q
        if self.index_stream:
            for k in ("t00", "o00"):
                ref, y = self.flow_src.index_last(k), (self._last_quotes.get(k) or {}).get("y")
                if ref and y:
                    out["index"][k] = {"ref": ref, "y": y, "chg": (ref / y - 1) * 100}
        return json.dumps(out, ensure_ascii=False)

    def fast_tick(self, mode="live"):
        """一輪快更新：報價 → 分時序列、分流、分流劇變警示；每 60 秒順便更新權證快照與警示。
        MIS 每 10 秒抓一次（限流），有永豐串流時每 3 秒用串流覆寫成交價/高低/量/五檔；MIS 失敗時整份改用串流湊（只少族群）。
        mode 是 _capture_mode 的時段，只用來告訴儀表板現在還有沒有在抓。"""
        now = dt.datetime.now()
        mis_due = (time.time() - self._last_mis_t >= 10) or not self._last_quotes
        do_wq = time.time() - self._last_wq_t >= 60
        # 有永豐串流時，成交價和五檔都來自串流，MIS 只補昨收、族群和指數；MIS 一次要 0.7～4 秒、權證快照要連抓好幾次，
        # 原本都在這條迴圈裡等，等的時候串流的最新價也送不到畫面。所以盤中改丟到背景執行緒，這一輪先用上一份。
        bg = mode == "live" and bool(self.flow_src) and bool(self._last_quotes)
        if bg:
            if mis_due or do_wq:
                self._bg_fetch(now, mis_due, do_wq)
            do_wq = False
        elif mis_due:
            self._refresh_mis(now)
        quotes = {k: dict(v) for k, v in self._last_quotes.items()}
        ready, self._wq_ready = self._wq_ready, {}
        live = {"time": now.strftime("%H:%M:%S"), "phase": model.phase_of(now), "index": {}, "index_series": {},
                "quotes": {}, "series": {}, "flow": {}, "warrants": {}, "alerts": {},
                "shioaji": bool(self.flow_src), "index_stream": self.index_stream, "interval": self.interval,
                "fast_every": 3 if self.flow_src else 10, "mis_t": dt.datetime.fromtimestamp(self._last_mis_t).strftime("%H:%M:%S"),
                "signal": {}, "sig_block": {}, "position": {}, "flow_series": {}, "walls": {}, "orderflow": {}, "liquidity": {}, "runs": {}, "signs": {}, "ticks_used": (self.flow_src.ticks_usage() if self.flow_src else None), "ticks_limit": (self.flow_src.ticks_budget() if self.flow_src else None),
                "mis_fail": self._mis_fail,
                "capture": {"mode": mode, "text": self._capture_text(mode)}}
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
        if mode == "live" and self._trading_today():
            try:
                self._index_alerts(live, now)
            except Exception as e:
                print(f"[fast] 大盤急漲急跌判斷失敗：{str(e)[:100]}")
        if mode == "live" and self.flow_src and time.time() - getattr(self, "_ticks_saved_at", 0) >= 15:
            # 逐筆每 15 秒存本機一次（原本 60 秒）：重開時存檔越新，越不用花 ticks 查詢額度補中間那段（backfill_today 20 秒內直接用）
            self._ticks_saved_at = time.time()
            try:
                self.flow_src.save_ticks_cache()
            except Exception as e:
                print(f"[fast] 逐筆存檔失敗：{str(e)[:100]}")
            try:
                self.flow_src.save_book_cache()       # 五檔每次變動 append 到 .cache/book_live（撤單觀察回放、調門檻用）
            except Exception as e:
                print(f"[fast] 五檔存檔失敗：{str(e)[:100]}")
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
            self._flow_now[c] = flow
            if flow:
                self._record_flow_hist(c, flow, q, now)
                fF, fW = model._flow_dynamics(flow, now)
                for w in fW:
                    self._add_alert(c, now, w)
                live["flow"][c] = {k: v for k, v in flow.items() if k != "hist"}
                # 大戶散戶卡的分鐘序列：全日逐筆重算一次 O(n)，10 秒一次就夠。
                # 每列 [HH:MM, 主力累積, 散戶累積, 累積量, 籌碼K線口徑大戶累積, 籌碼K線口徑散戶累積, 籌碼K線口徑大戶量, 籌碼K線口徑散戶量, 價]
                t_fs, fs = self._fs_cache.get(c, (0, None))
                if time.time() - t_fs >= 10 or fs is None:
                    try:
                        fs = self.flow_src.flow_history(c)
                    except Exception:
                        fs = fs or []
                    self._fs_cache[c] = (time.time(), fs)
                live["flow_series"][c] = [[dt.datetime.fromtimestamp(r[0]).strftime("%H:%M"), r[1], r[2], r[4]] + list(r[5:9]) + [r[3]] for r in fs]
                live["chipk_amount"] = getattr(self.flow_src, "CHIPK_AMOUNT", None)
            wq = self._warrant_snapshot(c, q, now, 1) if do_wq else ready.get(c)
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
                ofv = self._orderflow(c, q, now)
                if ofv:
                    live["orderflow"][c] = ofv
            except Exception as e:
                print(f"[fast] 撤單觀察 / 程式單偵測失敗：{str(e)[:100]}")
            if not self._trading_today():
                # 週末/休市日報價是凍結的舊價：不發訊號、不追蹤結果（11/07、11/08 舊版這樣寫進 22 筆假訊號）
                live["alerts"][c] = list(self.live_alerts.get(c, []))[-12:]
                continue
            try:
                rn = self._exhaustion(c, q, now)
                if rn:
                    live["runs"][c] = rn
                    self.runs[c] = rn
            except Exception as e:
                print(f"[fast] 竭盡判斷失敗：{str(e)[:100]}")
            try:
                sm = self._exh_signs(c, q, now)
                if sm:
                    live["signs"][c] = sm
            except Exception as e:
                print(f"[fast] 竭盡跡象失敗：{str(e)[:100]}")
            try:
                lq = self._liquidity(c, q, now)
                if lq:
                    self.liq[c] = lq
                    live["liquidity"][c] = lq
                    if lq["level"] == "thin" and c not in self._liq_alerted:
                        self._liq_alerted.add(c)
                        self._add_alert(c, now, f"⚠ 掛單薄：最近 20 分鐘最佳買賣價各只掛約 {lq['l1']:.0f} 張（約 {lq['l1_value'] / 1e4:.0f} 萬），"
                                                f"{lq['wide_pct']}% 的時間價差 ≥2 tick。單筆不要超過 {lq['max_lots']} 張，進出用限價、不要追市價；"
                                                f"幾十張就能推動好幾 tick，小反彈不代表趨勢反轉")
            except Exception as e:
                print(f"[fast] 流動性統計失敗：{str(e)[:100]}")
            try:
                sig = self._signal(c, WEB.latest.get(c), live["series"].get(c) or [], q, now)
                if sig:
                    live["signal"][c] = sig
                blk = self.sig_block.get(c)
                if blk and now.timestamp() - blk["since_ts"] < 300:
                    live["sig_block"][c] = {k: blk.get(k) for k in ("side", "label", "alert_px", "since", "block_reason", "block_kind", "why", "env_conds")}
            except Exception as e:
                print(f"[fast] 訊號判斷失敗：{str(e)[:100]}")
            try:
                pv = self._pos_watch(c, q, now)
                if pv:
                    live["position"][c] = pv
            except Exception as e:
                print(f"[fast] 持倉面板失敗：{str(e)[:100]}")
            try:
                self._track_alerts(c, q, flow, live["series"].get(c) or [], now)
            except Exception as e:
                print(f"[fast] 警示追蹤失敗：{str(e)[:100]}")
            live["alerts"][c] = sorted(list(self.live_alerts.get(c, []))[-12:] + list(self.pos_alerts.get(c, []))[-6:], key=lambda a: a[0])
        if self.winding and self._trading_today():
            self._wind_down(quotes, now)
        if do_wq:
            self._last_wq_t = time.time()
        WEB.put_live(live)
        return live

    # 盤中資料截取的時段（2026-11-11 改）。原本快迴圈沒有收盤的概念：13:25 只停模型估算，報價、權證快照照樣每 10 秒抓，
    # 13:35 後放慢成每 60 秒、一路抓到隔天；永豐串流也一直掛著（14:30 盤後定價的成交還會進分 K）。
    # 現在只有 08:25–13:25 連續抓。其餘三段各抓一次就停：
    #   pre   開盤前啟動：抓一次讓儀表板有東西看，08:25 才開始連續抓
    #   trial 13:25 收盤試撮開始：抓一次（訊號在這一筆照 13:25 的規則結案），之後不抓
    #   after 13:31 起（與休市日）：抓一次收盤價、把還沒結案的警示結掉，然後登出永豐串流，全部停止
    FAST_START, FAST_END, CLOSE_SNAP = "08:25", "13:25", "13:31"

    def _capture_mode(self, now):
        hhmm = now.strftime("%H:%M")
        if not self._trading_today():
            return "after"
        if hhmm < self.FAST_START:
            return "pre"
        if hhmm < self.FAST_END:
            return "live"
        return "trial" if hhmm < self.CLOSE_SNAP else "after"

    def _capture_text(self, mode):
        if mode == "live":
            return ""
        if mode == "pre":
            return f"還沒開盤，{self.FAST_START} 開始截取"
        if mode == "trial":
            return f"收盤試撮，已停止截取（{self.CLOSE_SNAP} 抓一次收盤價）"
        return "已收盤，資料截取已停止" if self._trading_today() else "今天休市，沒有在截取資料"

    def _stop_stream(self):
        """收盤後登出永豐（逐筆、五檔、指數的訂閱一起斷）。已經收到的資料留在記憶體，圖表照常畫。"""
        if self.flow_src and not self._stream_off:
            self._stream_off = True
            try:
                self.flow_src.save_ticks_cache()        # 今天的逐筆存到本機：之後重啟不用再查，也累積成回測資料
                self.flow_src.save_book_cache()
            except Exception as e:
                print(f"[shioaji] 逐筆存檔失敗：{str(e)[:80]}")
            try:
                self.flow_src.logout()
            except Exception as e:
                print(f"[shioaji] 登出失敗：{str(e)[:80]}")

    def _fast_step(self, now, every):
        """快迴圈的一步，回傳下一步前要等幾秒。live 以外的時段只抓一次（失敗最多試 3 次），之後只看時鐘、不對外取資料。"""
        if stale_day(self.boot_date, now):
            # 開著過夜到新的交易日：不截取（原本 08:25 會把串流接回來，大盤分時混進兩天的點、訊號用前一天的漲跌停），
            # 畫面保留前一天的資料給你參考；RESTART_AT 主迴圈自動重開（Monitor.run 回 "restart"）
            return 15
        mode = self._capture_mode(now)
        key = (now.date(), mode)
        if mode != "live":
            if key in self._cap_done:
                return 15
            if self._last_quotes is not None and (mode == "pre" or not self._trading_today()):
                self._cap_done.add(key)      # 開盤前已經有資料／跨到休市日：畫面上已經有資料，不用再抓
                return 15
        err = http = None
        try:
            self.fast_tick(mode)
        except hc.HttpError as e:
            err, http = e, True
        except Exception as e:
            err = e
        if mode == "live":
            if http:
                print(f"[fast] MIS 抓取失敗，20 秒後再試：{str(err)[:120]}")   # MIS 偶發回 HTML/限流：這輪跳過，多等 20 秒
                return every + 20
            if err:
                print(f"[fast] {str(err)[:100]}")
            return every
        if err:
            n = self._cap_tries[key] = self._cap_tries.get(key, 0) + 1
            if n < 3:
                print(f"[fast] 抓取失敗（第 {n} 次），20 秒後再試：{str(err)[:100]}")
                return 20
            print(f"[fast] 抓取失敗 3 次，不再試：{str(err)[:100]}")
        self._cap_done.add(key)
        if mode == "after":
            self._stop_stream()
            if self._trading_today():
                what = "已抓收盤價" if not err else "收盤價沒抓到"
                print(f"[{now:%H:%M:%S}] {what}，資料截取全部停止（報價、權證快照不再抓" + ("，永豐串流已登出" if self.flow_src else "") + "）；儀表板與復盤畫面照常")
            else:
                print(f"[{now:%H:%M:%S}] 今天休市：只抓一次報價，之後不再截取")
        elif mode == "trial":
            print(f"[{now:%H:%M:%S}] {self.FAST_END} 收盤試撮開始，停止截取報價；{self.CLOSE_SNAP} 抓一次收盤價後全部停止")
        else:
            print(f"[{now:%H:%M:%S}] 還沒開盤：先抓一次報價，{self.FAST_START} 起開始連續截取")
        sys.stdout.flush()
        return 15

    def fast_loop(self, every=10):
        while not self._halt:
            t0 = time.time()
            wait = self._fast_step(dt.datetime.now(), every)
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

    def _trading_today(self):
        d = dt.date.today()
        if getattr(self, "_tday", (None,))[0] != d:
            try:
                self._tday = (d, cal.is_trading_day(d))
            except Exception:
                self._tday = (d, d.weekday() < 5)
        return self._tday[1]

    def log(self, ev):
        if not self._trading_today():
            return          # 非交易日不寫 monitor_*.jsonl
        path = dev_path(LOG_DIR, f"monitor_{dt.date.today():%Y%m%d}", ".jsonl")   # 各台寫各自的檔，git 不會撞
        rec = {k: v for k, v in ev.items() if k not in ("quote",)}
        rec["quote"] = {k: ev["quote"].get(k) for k in ("ref", "o", "h", "l", "v", "bids", "asks")}
        rec["src"] = self.src
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    EVAL_START, EVAL_END = "08:30", "13:25"     # 模型估算時段。13:25 起是收盤試撮，之後不再估算

    def run(self, once=False, as_json=False, verbose=False, brief=False):
        if self.web and not once:
            self.start_fast()
        evaluated = False
        off_tries = 0
        idle_note = None
        while True:
            t0 = time.time()
            if self.web and not once and restart_due(self.boot_date):
                return "restart"          # 開著過夜到新的交易日：main() 重跑同一個指令（_restart_self）
            stale = self.web and not once and stale_day(self.boot_date)
            hhmm = dt.datetime.now().strftime("%H:%M")
            in_hours = self.EVAL_START <= hhmm < self.EVAL_END and not stale
            # 13:25 一到就停止模型估算（原本收盤後每 5 分鐘還在算、印「偏空，勝率 85%」這種沒有意義的 log，也會寫進 monitor_*.jsonl）。
            # 盤後才啟動的話只算一次，讓儀表板有東西看（抓不到最多試 3 次）。校準與資料同步照常；
            # 快迴圈也在 13:25 停止截取，13:31 抓一次收盤價後全部停（見 _capture_mode）。
            if in_hours:
                off_tries = 0
            if once or in_hours or (not evaluated and off_tries < 3):
                off_tries += 0 if in_hours else 1
                try:
                    results = self.poll()
                    evaluated = evaluated or bool(results)
                    for ev in results:
                        if once or in_hours:
                            self.log(ev)
                        if as_json:
                            print(json.dumps({k: v for k, v in ev.items() if k != "quote"}, ensure_ascii=False, default=str))
                        elif self.web:
                            inf = ev["info"]
                            print(f"[{ev['time']}] {ev['code']} {ev['name']} {ev['quote'].get('ref')} {fmt_pct(inf.get('chg_pct'))}  多 {ev['p_long']:.0f} 空 {ev['p_short']:.0f}  {ev['verdict']}"
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
            if not in_hours:
                note = (dt.date.today(), hhmm >= self.EVAL_END, stale)
                if idle_note != note:
                    idle_note = note
                    if stale:
                        msg = (f"今天 {dt.date.today():%m/%d} 是交易日，這個 monitor 是 {self.boot_date:%m/%d} 啟動的：先保留前一天的資料給你參考，"
                               f"{RESTART_AT}（試撮前）自動重開，換成今天的盤前資料、漲跌停、大盤與分時")
                    elif note[1]:
                        msg = f"{self.EVAL_END} 起（收盤試撮）模型停止估算、報價停止截取，儀表板保留最後一次估算；{self.CLOSE_SNAP} 抓一次收盤價結案，資料同步照常"
                    else:
                        msg = f"還沒開盤，{self.EVAL_START} 開始估算"
                    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}")
                    sys.stdout.flush()
            if hhmm >= "13:35" or hhmm < self.EVAL_START:
                if self.calibrate_after_close:
                    run_calibration("收盤後補跑校準")
                    self.calibrate_after_close = False
                self._sync_logs_once()
                if not self.web:
                    print("已收盤，監控結束。")
                    break
                time.sleep(60 if stale else 300)     # 網頁模式：收盤後維持服務（不再估算）；跨日後每分鐘看一次，RESTART_AT 準時重開
                continue
            time.sleep(max(5, self.interval - (time.time() - t0)))


def main():
    ap = argparse.ArgumentParser(description="台股當沖多空勝率監控")
    ap.add_argument("codes", nargs="*", help="股票代碼（可多檔）；--web 不給代碼 = 選股模式，在網頁上選")
    ap.add_argument("--peers", default=None, help="自訂同族群代碼，逗號分隔；不給則查 groups.json")
    ap.add_argument("--interval", type=int, default=60, help="多空估算間隔秒數（預設 60；圖表/報價另有 3–10 秒快迴圈）")
    ap.add_argument("--once", action="store_true", help="只跑一次")
    ap.add_argument("--json", action="store_true", help="輸出 JSON 行（給程式讀）")
    ap.add_argument("--verbose", action="store_true", help="印出每個因子的說明")
    ap.add_argument("--brief", action="store_true", help="精簡輸出")
    ap.add_argument("--no-shioaji", action="store_true", help="不使用永豐 API")
    ap.add_argument("--no-sound", action="store_true", help="出現建議進出場點時不要發聲、不跳系統通知（也可設 DAYTRADE_SOUND=0）")
    ap.add_argument("--big-lot", type=int, default=0, help="固定大單門檻（張）；預設 0 = 依金額與當日分布自動推算")
    ap.add_argument("--big-amount", type=float, default=None, help="大單金額門檻（元），預設 .env 的 BIG_LOT_AMOUNT 或 500 萬")
    ap.add_argument("--refresh", action="store_true", help="重抓盤前資料（忽略今日快取）")
    ap.add_argument("--web", nargs="?", const=WEB_PORT, type=int, default=None, metavar="PORT",
                    help=f"啟動本機網頁儀表板（預設 port {WEB_PORT}，只綁 127.0.0.1）")
    ap.add_argument("--no-open", action="store_true", help="--web 時不要自動開瀏覽器")
    a = ap.parse_args()
    if a.no_sound:
        notify.ENABLED = notify.BANNER = False
    elif a.web or not a.once:
        print("[通知] 出現建議進出場點會發聲：進場「叮」、出場「叮咚」" + ("，並跳系統通知橫幅" if sys.platform == "darwin" else "") + "（--no-sound 關掉）")
    import signal
    if hasattr(signal, "SIGTERM"):
        # 被新開的 monitor 接管 port 時（start_web → _kill 先送 TERM、3 秒後才強制結束），照樣走 finally：存最後一次逐筆、登出永豐
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    if not a.codes and not a.web:
        ap.error("請給股票代碼，或用 --web 進入選股模式（網頁上選標的）")
    if a.big_amount:
        os.environ["BIG_LOT_AMOUNT"] = str(int(a.big_amount))
    boot = dt.date.today()
    srv = None
    if a.web:
        srv, a.web = start_web(a.web)
        if not a.once:
            threading.Thread(target=site_watch, daemon=True, name="site-watch").start()
            threading.Thread(target=gapfade_watch, args=(not a.no_shioaji,), daemon=True, name="gapfade").start()
            threading.Thread(target=fear_watch, args=(not a.no_shioaji,), daemon=True, name="fear").start()
        if not a.no_open:
            try:
                webbrowser.open(f"http://127.0.0.1:{a.web}/")  # macOS / Windows / Linux 都用系統預設瀏覽器
            except Exception:
                pass
    codes = a.codes
    if not codes:
        # 選股模式：網頁先列 theme_screener 的多空候選，選定後才建 Monitor（抓盤前資料、開永豐串流）
        threading.Thread(target=run_screener, daemon=True).start()
        print("[選股] 執行 theme_screener 中；請在網頁勾選今天要監控的標的（Ctrl+C 結束）")
        try:
            while not WEB.select_event.wait(1):
                if restart_due(boot):           # 選股畫面開著過夜：候選股是前一天的，重開重跑選股
                    _restart_self(srv)
        except KeyboardInterrupt:
            print("\n使用者中止。")
            return
        codes = WEB.selected
        print(f"[選股] 選定 {','.join(codes)}，開始準備盤前資料與監控")
    m = Monitor(codes, a.peers, a.interval, not a.no_shioaji, a.big_lot, a.refresh, web=bool(a.web))
    restart = False
    try:
        restart = m.run(a.once, a.json, a.verbose, a.brief) == "restart"
        if restart:
            m._halt = True               # 先停快迴圈
            m._stop_stream()             # 存今天的逐筆、登出永豐（收盤後已經登出就不會重複）
    except KeyboardInterrupt:
        print("\n使用者中止。")
    finally:
        if m.flow_src:
            if not m._stream_off:
                try:
                    m.flow_src.save_ticks_cache()   # 結束前把逐筆存最後一次：下次重開時存檔是新的，少一段空檔、少花查詢額度
                    m.flow_src.save_book_cache()
                except Exception:
                    pass
            m.flow_src.logout()
    if restart:
        _restart_self(srv)


def _restart_self(srv):
    """
    開著過夜到新的交易日，RESTART_AT（試撮 08:30 前）自動重開：等於幫你重跑同一個指令。選股模式回到選股畫面（重跑選股），
    給代碼的照樣監控那幾檔；盤前資料、漲跌停、大盤與分時、訊號與警示全部換成今天的。儀表板分頁會自己重新載入（BOOT_ID 變了）。
    macOS / Linux 用 exec 原地換成新程式（同一個 PID、同一個終端機視窗，Ctrl+C 照常）；
    Windows 沒有真正的 exec，改成開子程序跑新的，這個程序等它結束（Ctrl+C 會同時送到子程序，由它收尾）。
    """
    print(f"[{dt.datetime.now():%H:%M:%S}] [換日] 今天 {dt.date.today():%m/%d} 是交易日，自動重開 monitor（等於重跑同一個指令），換成今天的資料")
    _SITE_STOP.set()                      # Windows 上這個程序還會活著等子程序：網站檢查交給新的
    if srv is not None:
        try:
            srv.shutdown()                # 放掉 port，新的程式才綁得到同一個
            srv.server_close()
        except Exception:
            pass
    args = [x for x in sys.argv[1:] if x != "--no-open"] + ["--no-open"]   # 瀏覽器分頁已經開著，不要再開一個
    cmd = [sys.executable, os.path.abspath(__file__)] + args
    sys.stdout.flush()
    sys.stderr.flush()
    if os.name == "nt":
        p = subprocess.Popen(cmd)
        while True:
            try:
                sys.exit(p.wait())
            except KeyboardInterrupt:
                continue
    os.execv(sys.executable, cmd)


if __name__ == "__main__":
    main()
