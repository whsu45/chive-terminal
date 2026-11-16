#!/usr/bin/env python3
"""
收盤後手動跑的 A/B 對照：模組 B（ab/ab_models.Model_B，噪音區突破）重跑當天，跟模組 A（monitor 的即時訊號）並排比。

2026-11-16 起 B 不在 monitor 裡即時跑了（它在背景抓歷史分 K，曾讓 monitor 啟動時當機；盤中也不看它），改成每天收盤後自己跑：

    python3 ab_compare.py                     # 今天 monitor 監控過的股票（交戰紀錄裡的監控日）
    python3 ab_compare.py --date 2026-11-13   # 指定日期
    python3 ab_compare.py 2344 6213           # 指定股票
    python3 ab_compare.py --no-shioaji        # 不登入永豐，只用 Yahoo 5 分 K

- 資料：有永豐就用 kbars（1 分 K 合成 5 分 K；收盤後查詢不算盤中額度），沒有就用 Yahoo 5 分 K（量是股，換成張）。
  每檔查一次，含當天和前 30 天。當天 13:35 前不跑（資料不完整，盤中查 kbars 也要算額度）。
- B：前 14 個交易日算噪音帶，當天 09:30 起每 15 分鐘看 5 分 K 收盤，收盤價進、移動停損出、13:20 平；扣現股當沖成本（CostModel 預設）。
  平倉的單 append 到 logs/ab_trades_<裝置>.jsonl（跟以前即時版同格式；各台已經記過的不重複寫）。
- A：讀 logs/signal_outcomes_*.jsonl 當天這檔的訊號結果（monitor 寫的；兩台同一天都開時只留一台）。
- B 只是觀察用的挑戰者：回測（120 檔 × 515 天）扣成本後每筆 −0.52%（勝率 25%、盈虧比 1.3）。
"""
import os
import sys
import json
import glob
import argparse
import datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "ab"))
from ab_models import Model_B, CostModel  # noqa: E402
from config import LOG_DIR, dev_path, one_device  # noqa: E402

HIST_DAYS = 14
VERDICT = {"win": "贏", "loss": "輸", "flat": "平", "nofill": "沒成交"}


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


def _from_shioaji(api, code, day):
    import src_shioaji as S
    contract = S.get_contract(api, code)
    if contract is None:
        return None
    start = (day - dt.timedelta(days=30)).isoformat()
    k = S.fetch_kbars(api, contract, start, "A/B 對照", end=day.isoformat())
    rows = []
    for ts, o, h, l, c, v in zip(k.ts, k.Open, k.High, k.Low, k.Close, k.Volume):
        t = S.ns_to_dt(ts) - dt.timedelta(minutes=1)        # 永豐 ts 是結束時間
        if t.date() <= day:
            rows.append({"ts": t, "open": float(o), "high": float(h), "low": float(l), "close": float(c), "volume": float(v)})
    return _resample5(_session(rows))


def _from_yahoo(code, market, day):
    import src_yahoo
    syms = [f"{code}.TW" if market == "tse" else f"{code}.TWO"] if market in ("tse", "otc") else [f"{code}.TW", f"{code}.TWO"]
    for sym in syms:
        rows = []
        for r in src_yahoo.chart(sym, "5m", "1mo", cache_ttl=3600):
            t = r["ts"]
            if t.date() <= day and r.get("o") and r.get("h") and r.get("l"):
                rows.append({"ts": t, "open": float(r["o"]), "high": float(r["h"]), "low": float(r["l"]), "close": float(r["c"]),
                             "volume": float(r["v"] or 0) / 1000.0})
        if rows:
            return _session(rows)
    return None


def load_bars(code, market, day, api):
    """當天和前 HIST_DAYS 個交易日的 5 分 K + 來源。"""
    rows, src, err = None, None, []
    if api is not None:
        try:
            rows, src = _from_shioaji(api, code, day), "永豐分 K"
        except Exception as e:
            err.append(f"永豐：{str(e)[-70:]}")
    if not rows:
        try:
            rows, src = _from_yahoo(code, market, day), "Yahoo 5 分 K"
        except Exception as e:
            err.append(f"Yahoo：{str(e)[-70:]}")
    if not rows:
        raise RuntimeError("；".join(err) or "沒有分 K")
    days = sorted({r["ts"].date() for r in rows})[-(HIST_DAYS + 1):]
    return [r for r in rows if r["ts"].date() in set(days)], src


def run_b(rows, day, model, cost):
    """回 (當天平倉的單, 沒交易時的原因)。"""
    if not any(r["ts"].date() == day for r in rows):
        return [], "當天沒有分 K（沒交易或資料還沒更新）"
    res = model.run(rows)
    trades = [t for t in res.trades if t.date == day and t.exit is not None and t.reason != "資料結束平倉"]
    info = getattr(model, "last_info", None) or {}
    if trades:
        return trades, None
    if info.get("skip"):
        return [], f"今天不交易：{info['skip']}"
    sigs = [s for s in res.signals if s["ts"] and s["ts"].date() == day and s.get("reason")]
    blocked = [s for s in sigs if s["reason"].startswith("突破")]
    if blocked:
        return [], f"有突破但被濾網擋下（最後一次 {blocked[-1]['ts']:%H:%M}：{blocked[-1]['reason']}）"
    return [], "一整天都在噪音帶內，沒有進場"


def a_signals(day):
    """monitor 寫的模組 A 訊號結果：{code: [rec, ...]}，同一天兩台都開時只留一台。"""
    rows = []
    for path in glob.glob(os.path.join(LOG_DIR, "signal_outcomes*.jsonl")):
        dev = os.path.basename(path)[len("signal_outcomes"):-len(".jsonl")].lstrip("_")
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        j = json.loads(line)
                    except ValueError:
                        continue
                    if j.get("date") == day.isoformat():
                        rows.append(dict(j, _dev=dev))
        except OSError:
            pass
    out = {}
    for r in sorted(one_device(rows, key=lambda r: (r.get("date"), r.get("code"))), key=lambda r: r.get("time") or ""):
        out.setdefault(r.get("code"), []).append(r)
    return out


def monitored_codes(day):
    """交戰紀錄裡 day 這天監控過的股票 {code: (名稱, 市場)}。"""
    import history
    return {c: (e.get("name"), e.get("market")) for c, e in history._merged().items() if day.isoformat() in (e.get("days") or [])}


def _logged():
    keys = set()
    for path in glob.glob(os.path.join(LOG_DIR, "ab_trades*.jsonl")):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        j = json.loads(line)
                        keys.add((j["code"], j["entry_ts"]))
                    except (ValueError, KeyError):
                        pass
        except OSError:
            pass
    return keys


def log_b(code, name, trades, model, cost, logged):
    new = [t for t in trades if (code, t.entry_ts.isoformat()) not in logged]
    if not new:
        return 0
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(dev_path(LOG_DIR, "ab_trades", ".jsonl"), "a", encoding="utf-8") as f:
        for t in new:
            f.write(json.dumps({"model": "B", "code": code, "name": name, "date": t.date.isoformat(), "side": t.side,
                                "entry_ts": t.entry_ts.isoformat(), "entry": t.entry, "stop0": t.stop0, "target": t.target,
                                "exit_ts": t.exit_ts.isoformat(), "exit": t.exit, "reason": t.reason,
                                "gross_pct": round(t.gross_pct(), 3), "net_pct": round(t.net_pct(cost), 3),
                                "params": {k: model.p[k] for k in ("k", "rvol_min", "trail", "max_tick_pct", "every_min")}},
                               ensure_ascii=False) + "\n")
            logged.add((code, t.entry_ts.isoformat()))
    return len(new)


def _side(s):
    return "多" if s == "long" else "空"


def _fmt_a(r):
    net = r.get("net_pct")
    tail = f"淨 {net:+.2f}%" if net is not None else (f"30 分後 {r['ret_30m']:+.2f}%" if r.get("ret_30m") is not None else "")
    return f"{r.get('time', '')[:5]} {r.get('label') or _side(r.get('side'))} {r.get('price')} → {VERDICT.get(r.get('verdict'), r.get('verdict') or '?')}  {tail}".rstrip()


def _fmt_b(t, cost):
    return (f"{t.entry_ts:%H:%M} {_side(t.side)} {t.entry} → {t.exit_ts:%H:%M} {t.exit}（{t.reason}）"
            f"  淨 {t.net_pct(cost):+.2f}%（毛 {t.gross_pct():+.2f}%）")


def main():
    ap = argparse.ArgumentParser(description="收盤後的 A/B 對照：模組 B 重跑當天，跟 monitor 的模組 A 訊號並排比")
    ap.add_argument("codes", nargs="*", help="股票代碼；不給就用當天 monitor 監控過的")
    ap.add_argument("--date", default=None, help="哪一天（預設今天）")
    ap.add_argument("--no-shioaji", action="store_true", help="不登入永豐，只用 Yahoo 5 分 K")
    ap.add_argument("--no-log", action="store_true", help="不寫 logs/ab_trades_*.jsonl（只看）")
    a = ap.parse_args()

    day = dt.date.fromisoformat(a.date) if a.date else dt.date.today()
    now = dt.datetime.now()
    if day > now.date() or (day == now.date() and now.strftime("%H:%M") < "13:35"):
        sys.exit(f"{day} 還沒收盤（13:35 後再跑）：當天分 K 不完整，盤中查永豐 kbars 也要算額度")

    known = monitored_codes(day)
    codes = [c.strip().upper() for c in a.codes] or sorted(known)
    if not codes:
        sys.exit(f"交戰紀錄裡 {day} 沒有監控過的股票；用 python3 ab_compare.py 2344 6213 指定代碼")
    a_by = a_signals(day)

    api = None
    if not a.no_shioaji:
        import src_shioaji as S
        if S.available():
            api = S.login()
    model, cost = Model_B(), CostModel()
    logged = _logged()
    tot_a, tot_b, n_logged = [], [], 0
    print(f"=== A/B 對照 {day}（{len(codes)} 檔；B 扣現股當沖成本，A 用 monitor 記的訊號結果）")
    try:
        for code in codes:
            name, market = known.get(code, (None, None))
            print(f"\n{code} {name or ''}".rstrip())
            arecs = a_by.get(code) or []
            tot_a += arecs
            if arecs:
                for r in arecs:
                    print(f"  A  {_fmt_a(r)}")
            else:
                print("  A  沒有訊號")
            try:
                rows, src = load_bars(code, market, day, api)
                trades, why = run_b(rows, day, model, cost)
            except Exception as e:
                print(f"  B  抓不到分 K：{str(e)[:160]}")
                continue
            tot_b += trades
            for t in trades:
                print(f"  B  {_fmt_b(t, cost)}")
            if why:
                print(f"  B  {why}")
            if any(r["ts"].date() == day for r in rows):
                print(f"     （B 資料：{src}，前 {len({r['ts'].date() for r in rows if r['ts'].date() < day})} 天算噪音帶）")
            if trades and not a.no_log:
                n_logged += log_b(code, name, trades, model, cost, logged)
    finally:
        if api is not None:
            import src_shioaji as S
            S.logout()

    print("\n=== 合計")
    nets_a = [r["net_pct"] for r in tot_a if r.get("net_pct") is not None]
    w = sum(1 for r in tot_a if r.get("verdict") == "win"); l = sum(1 for r in tot_a if r.get("verdict") == "loss")
    print(f"  A  {len(tot_a)} 次訊號，贏 {w} 輸 {l}" + (f"，平均淨 {sum(nets_a) / len(nets_a):+.2f}%（{len(nets_a)} 筆有淨損益）" if nets_a else ""))
    nets_b = [t.net_pct(cost) for t in tot_b]
    print(f"  B  {len(tot_b)} 筆" + (f"，賺 {sum(1 for x in nets_b if x > 0)} 賠 {sum(1 for x in nets_b if x <= 0)}，平均淨 {sum(nets_b) / len(nets_b):+.2f}%" if nets_b else ""))
    if n_logged:
        print(f"  B 新增 {n_logged} 筆寫進 {os.path.relpath(dev_path(LOG_DIR, 'ab_trades', '.jsonl'), HERE)}")


if __name__ == "__main__":
    main()
