"""
個股交戰紀錄（logs/stock_history_<裝置ID>.json，讀取時合併各台）：當沖要做熟悉的股票，所以每檔記
  - 監控過哪些天（同一天開幾次 monitor 都算一天；launches 另計）
  - 每一次「建議做空/做多」訊號的結果：贏 / 輸 / 平、5 分鐘內就反向（估錯）、可疑原因
  - 使用者備註（notes），例如「連三次開高走低」
model.evaluate 會拿 summary() 當參考：該股某一邊的訊號勝率夠差就壓那一邊的勝率並警告；儀表板開「交戰紀錄」卡。
"""
import os
import json
import threading
import datetime as dt

from config import LOG_DIR, dev_path, dev_files, one_device

# 每台電腦寫自己的 stock_history_<裝置ID>.json（git 才不會兩台互撞）；讀取（get / summary / 儀表板）合併所有裝置的檔
PATH = dev_path(LOG_DIR, "stock_history", ".json")
_lock = threading.Lock()
_EMPTY = lambda: {"name": None, "market": None, "group": None, "days": [], "launches": 0, "signals": [], "notes": []}


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _load():
    """只讀這台自己的檔（寫入用）。"""
    return _read(PATH)


def _save(db):
    os.makedirs(LOG_DIR, exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=1)
    os.replace(tmp, PATH)


def _merged():
    """所有裝置（含加裝置 ID 前的舊檔）合併：監控日取聯集、啟動次數相加、
    訊號同一天兩台都有時只留一台的（避免重複計勝率）、備註全收。"""
    out = {}
    for dev, path in dev_files(LOG_DIR, "stock_history", ".json"):
        for code, e in _read(path).items():
            if not isinstance(e, dict):
                continue
            m = out.setdefault(code, _EMPTY())
            for k in ("name", "market", "group"):
                if e.get(k):
                    m[k] = e[k]
            m["days"] = sorted(set(m["days"]) | set(e.get("days") or []))
            m["launches"] += e.get("launches", 0) or 0
            m["signals"] += [dict(s, _dev=dev) for s in (e.get("signals") or []) if isinstance(s, dict)]
            m["notes"] += [n for n in (e.get("notes") or []) if isinstance(n, dict)]
    for m in out.values():
        m["signals"] = sorted(one_device(m["signals"], key=lambda s: s.get("date")),
                              key=lambda s: (s.get("date") or "", s.get("time") or ""))[-300:]
        m["notes"].sort(key=lambda n: n.get("date") or "")
    return out


def record_launch(code, name=None, market=None, group=None, today=None):
    """monitor 啟動時呼叫：記監控日（一天算一次）與啟動次數。回該股紀錄。"""
    today = today or dt.date.today().isoformat()
    with _lock:
        db = _load()
        e = db.setdefault(code, dict(_EMPTY(), name=name, market=market, group=group))
        if name: e["name"] = name
        if market: e["market"] = market
        if group: e["group"] = group
        if today not in e["days"]:
            e["days"].append(today)
            e["days"].sort()
        e["launches"] = e.get("launches", 0) + 1
        _save(db)
    return _merged().get(code, e)


def record_signal(code, rec):
    """訊號結案時呼叫：rec = {date,time,side,price,stop,t1,t2,verdict,fast_adverse,ret_5m,ret_10m,ret_30m,suspects,why}。"""
    with _lock:
        db = _load()
        e = db.setdefault(code, _EMPTY())
        key = (rec.get("date"), rec.get("time"), rec.get("side"))
        if any((s.get("date"), s.get("time"), s.get("side")) == key for s in e["signals"]):
            return
        e["signals"].append(rec)
        e["signals"] = e["signals"][-300:]
        _save(db)


def add_note(code, text):
    with _lock:
        db = _load()
        e = db.setdefault(code, _EMPTY())
        e["notes"].append({"date": dt.date.today().isoformat(), "text": text})
        _save(db)


def get(code):
    return _merged().get(code)


def summary(code, e=None):
    """給 model / 儀表板用的摘要。"""
    e = e or get(code)
    if not e:
        return None
    all_sig = e.get("signals") or []
    # 勝率只算永豐串流（或舊紀錄沒標來源）的訊號；MIS 10 秒快照模式的訊號時機不準，另外計數不進勝率
    sig = [s for s in all_sig if s.get("src") != "mis"]
    n_mis = len(all_sig) - len(sig)
    def side_stats(side):
        xs = [s for s in sig if s.get("side") == side]
        w = sum(1 for s in xs if s.get("verdict") == "win")
        l = sum(1 for s in xs if s.get("verdict") == "loss")
        fa = sum(1 for s in xs if s.get("fast_adverse"))
        nf = sum(1 for s in xs if s.get("verdict") == "nofill")
        return {"n": len(xs), "win": w, "loss": l, "flat": len(xs) - w - l - nf, "nofill": nf, "fast_adverse": fa,
                "win_rate": (w / (w + l) * 100) if (w + l) else None}
    # 失敗原因排行（只數輸的）
    reasons = {}
    for s in sig:
        if s.get("verdict") == "loss" or s.get("fast_adverse"):
            for r in s.get("suspects") or []:
                reasons[r] = reasons.get(r, 0) + 1
    top = sorted(reasons.items(), key=lambda x: -x[1])[:5]
    return {"code": code, "name": e.get("name"), "group": e.get("group"), "days": len(e.get("days") or []),
            "first": (e.get("days") or [None])[0], "last": (e.get("days") or [None])[-1], "launches": e.get("launches", 0),
            "short": side_stats("short"), "long": side_stats("long"), "n_signals": len(sig),
            "n_mis": n_mis, "recent": all_sig[-8:], "loss_reasons": top, "notes": (e.get("notes") or [])[-5:]}


def all_summary():
    db = _merged()
    out = [summary(c, e) for c, e in db.items()]
    out.sort(key=lambda s: (-s["days"], -s["n_signals"]))
    return out
