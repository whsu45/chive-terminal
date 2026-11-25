#!/usr/bin/env python3
"""
國安基金護盤期（2026-11-20 加）：護盤期間的開盤策略限制，和盤中「權值股突然被拉」的偵測。

背景（新聞，國安基金不公布進場時間，以下是報導與事後觀察）：
  - 11/06 收盤後國安基金決議啟動第 10 次護盤（可動用上限 5,000 億）；金管會「限空令」11/09 起實施
    （借券賣出上限從日均量 30% 降到 3%、融券保證金 90% → 130%）。
  - 限空令加碼：11/20 起前一天收盤跌 ≥3.5% 的個股隔天平盤下不得融券 / 借券賣出；11/23 起全面平盤下禁空
    （融券、借券賣價不得低於昨收，融券保證金 150%，暫停新的認售權證）。現股當沖先賣不在禁令內，平盤下照樣可以先賣，
    但當天沒買回就不能改成融券 / 借券了結 → 空單一定要當天回補（鎖漲停買不回 = 違約交割風險）。
  - 11/19 官員證實已實質進場。護盤向來以台積電等權值股為主，常見盤中 / 尾盤突然拉抬權值股。
  - 使用者判斷這次護盤約延續四週，11/20 是第二週 → 預設護盤期 11/09～12/11（環境變數 DAYTRADE_SUPPORT_UNTIL=YYYY-MM-DD 可改，
    DAYTRADE_SUPPORT_START 改開始日；設成空字串 = 不在護盤期）。

護盤期的開盤策略（2026/04–11/19 全市場日 K，開盤空收盤補，150 元以內、昨日成交值前 40%）：
  護盤期 11/09–11/19 共 9 天，依當天全市場開盤跳空中位數分：
    大盤開高（≥ +0.3%，4 天）：開高 2～7% 放空每天平均 −0.61%、隔日沖倒貨 −0.67%（11/09 兩組都 −4.7%：宣布護盤後第一天，大盤開高又一路漲 3.3%）
    大盤平開或開低（5 天）：開高 2～7% 放空每天平均 +2.88%、隔日沖倒貨 +4.02%，5 天裡 4 天賺
  護盤前（4 月～11/06）大盤小開高的日子開高放空還是 +0.31%/筆，所以這是護盤期才有的現象。只有 4 個開高日，證據薄，照使用者「小心護盤訊號」先擋。
  → 護盤期間大盤開高的日子，開盤策略不建議放空（照樣記錄）；平開或開低照常。

盤中「權值股突然被拉」（疑似護盤拉抬）：台積電、鴻海、聯發科平均 5 分鐘漲 ≥1%，加權 5 分鐘漲 ≥0.5%，櫃買比權值股少漲 ≥0.5%。
  10/01–11/06（護盤前 24 天）5 次，之後加權 30 分鐘 +0.61%、到收盤 +0.34%（多在 11/05–06 崩跌見底時）。
  11/09–11/20（護盤期 10 天）7 次（每天 0.7 次），之後加權 30 分鐘 −0.17%、到收盤 −0.92%，7 次裡 6 次收盤比拉抬時更低：
  拉抬撐不久，但 5 分鐘內加權會被拉 0.6～1.2%，空單容易在這時被停損掃掉。
  → 偵測到時叮咚提醒；之後 30 分鐘的訊號加註參考（做多：拉完常又跌；做空：可能再被拉一段，停損放寬或等拉完再進）。不擋訊號。
  樣本很少（7 次），是提醒不是驗證過的規則；每次偵測都寫進 logs/support_<裝置ID>.jsonl，之後可以再驗證。
"""
import datetime as dt
import json
import os
from collections import deque

from config import LOG_DIR, dev_path

START = os.environ.get("DAYTRADE_SUPPORT_START", "2026-11-09")
UNTIL = os.environ.get("DAYTRADE_SUPPORT_UNTIL", "2026-12-11")
GAP_UP = 0.3             # 全市場開盤跳空中位數 ≥ 這個 % = 大盤開高（護盤期這天開盤策略不建議）
HW = ("2330", "2317", "2454")
PULL_HW, PULL_TX, PULL_SPREAD, PULL_MIN = 1.0, 0.5, 0.5, 5      # 權值股 ≥1%、加權 ≥0.5%、權值股比櫃買多漲 ≥0.5%，看 5 分鐘
PULL_GAP_MIN = 15        # 兩次偵測至少隔幾分鐘
REF_MIN = 30             # 偵測後幾分鐘內的訊號加註參考


def in_period(d=None):
    d = (d or dt.date.today()).isoformat() if not isinstance(d, str) else d
    return bool(START and UNTIL) and START <= d <= UNTIL


def week_no(d=None):
    d = d or dt.date.today()
    if isinstance(d, str):
        d = dt.date.fromisoformat(d)
    if not in_period(d):
        return None
    return (d - dt.date.fromisoformat(START)).days // 7 + 1


def status(d=None):
    d = d or dt.date.today()
    on = in_period(d)
    out = {"on": on, "start": START, "until": UNTIL, "week": week_no(d)}
    out["text"] = (f"國安基金護盤期・第 {out['week']} 週（{START[5:].replace('-', '/')} 起，預設到 {UNTIL[5:].replace('-', '/')}）："
                   "大盤開高的日子開盤策略不放空；權值股突然被拉會叮咚提醒") if on else ""
    return out


def open_block(d, mkt_gap):
    """護盤期、大盤開高（全市場開盤跳空中位數 ≥ GAP_UP）→ 回擋的理由；否則 None。"""
    if mkt_gap is None or not in_period(d) or mkt_gap < GAP_UP:
        return None
    return (f"國安基金護盤期・大盤開高（全市場開盤跳空中位數 {mkt_gap:+.2f}%）：護盤期這種日子開盤放空每天平均 −0.6%"
            f"（4 天；11/09 −4.7%），平開或開低的日子 +2.9～+4.0%，今天不建議放空，只記錄")


class PullDetector:
    """每分鐘餵一次價格（權值股、加權、櫃買），偵測「權值股突然被拉」。"""

    def __init__(self, log_dir=None):
        self.hist = deque(maxlen=30)     # [(epoch, {"hw": [..], "tx": x, "otc": y})]
        self.last = None                 # 最近一次偵測
        self.events = []                 # 今天的偵測
        self.day = None
        self.log_dir = log_dir or LOG_DIR

    def feed(self, now, hw_px, tx, otc):
        """hw_px：{代碼: 價}（至少 2 檔）、tx 加權、otc 櫃買。回新偵測到的事件 dict 或 None。"""
        if self.day != now.date():
            self.day = now.date(); self.hist.clear(); self.events = []; self.last = None
        if not (tx and otc) or sum(1 for v in hw_px.values() if v) < 2:
            return None
        self.hist.append((now.timestamp(), dict(hw_px), tx, otc))
        if not ("09:10" <= now.strftime("%H:%M") <= "13:20"):
            return None
        base = None
        for ts, h, t, o in self.hist:
            if now.timestamp() - ts >= PULL_MIN * 60 - 20:
                base = (ts, h, t, o)
            else:
                break
        if base is None or now.timestamp() - base[0] > (PULL_MIN + 3) * 60:
            return None
        _, h0, t0, o0 = base
        rs = [(hw_px[c] / h0[c] - 1) * 100 for c in hw_px if hw_px.get(c) and h0.get(c)]
        if len(rs) < 2:
            return None
        r_hw = sum(rs) / len(rs); r_tx = (tx / t0 - 1) * 100; r_otc = (otc / o0 - 1) * 100
        if r_hw >= PULL_HW and r_tx >= PULL_TX and r_hw - r_otc >= PULL_SPREAD:
            if self.last and now.timestamp() - self.last["ts"] < PULL_GAP_MIN * 60:
                return None
            ev = {"ts": now.timestamp(), "time": now.strftime("%H:%M:%S"), "date": now.date().isoformat(),
                  "r_hw": round(r_hw, 2), "r_tx": round(r_tx, 2), "r_otc": round(r_otc, 2), "tx": tx, "otc": otc,
                  "period": in_period(now.date())}
            self.last = ev
            self.events.append(ev)
            try:
                os.makedirs(self.log_dir, exist_ok=True)
                with open(dev_path(self.log_dir, "support", ".jsonl"), "a", encoding="utf-8") as f:
                    f.write(json.dumps(ev, ensure_ascii=False) + "\n")
            except Exception:
                pass
            return ev
        return None

    def recent(self, now, minutes=REF_MIN):
        """最近 minutes 分鐘內的偵測（給訊號加註參考）。"""
        if self.last and now.timestamp() - self.last["ts"] <= minutes * 60 and self.last["date"] == now.date().isoformat():
            return self.last
        return None


def describe_pull(ev):
    return (f"權值股突然被拉（疑似護盤）：台積電 / 鴻海 / 聯發科 5 分鐘 {ev['r_hw']:+.1f}%、加權 {ev['r_tx']:+.1f}%、櫃買 {ev['r_otc']:+.1f}%")


PULL_NOTE = "護盤期 7 次這種拉抬，6 次收盤比拉抬時更低（之後加權到收盤平均 −0.9%）：空單別在拉抬時被洗出場，追多要小心"


if __name__ == "__main__":
    print(status()["text"] or "現在不在國安基金護盤期")
