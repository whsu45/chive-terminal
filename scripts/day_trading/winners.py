"""
常勝主力：用該股近 N 日每天的分點買賣超（MoneyDJ zco 單日表，前 15 買/賣）
配合日K，算每個分點「買了隔天漲、賣了隔天跌」的勝率與平均捕捉報酬，
找出在這檔股票上長期站對邊的分點，再看他們昨天的動作與目前庫存。

限制：分點資料只有盤後，盤中拿不到；單日表只列前 15 名，小額進出看不到。
"""
import datetime as dt
import src_moneydj
import tw_calendar as cal


def broker_daily_history(code, days=20, ref_date=None):
    """近 N 個交易日逐日分點表 → {date_iso: {broker: net}}。過去日期快取 30 天。"""
    ref = ref_date or cal.today_tw()
    out = {}
    for d in cal.last_n_trading_days(ref, days):
        try:
            summ = src_moneydj.broker_summary_range(code, d, d, cache_ttl=86400 * 30)
        except Exception:
            continue
        rows = {}
        for r in summ.get("buyers", []) + summ.get("sellers", []):
            rows[r["broker"]] = rows.get(r["broker"], 0) + r["net"]
        if rows:
            out[d.isoformat()] = rows
    return out


def score_branches(hist_rows, broker_days, min_count=4):
    """
    hist_rows: 日K [{date, c, v}] 舊→新；broker_days: {date: {broker: net}}
    每筆 (分點, 日 d, net) 的結果 = sign(net) × (close[d+1]/close[d] − 1)。
    回 [{broker, count, win_rate, avg_capture_pct, cum_net, last_date, last_net, buy_days, sell_days, tag}]
    """
    closes = {r["date"]: r["c"] for r in hist_rows}
    dates = sorted(closes)
    nxt = {d: dates[i + 1] for i, d in enumerate(dates[:-1])}
    vol20 = sum(r["v"] for r in hist_rows[-20:]) or 1
    stats = {}
    for d, rows in broker_days.items():
        for broker, net in rows.items():
            if net == 0:
                continue
            s = stats.setdefault(broker, {"broker": broker, "count": 0, "wins": 0, "capture": 0.0, "cum_net": 0,
                                           "buy_days": 0, "sell_days": 0, "last_date": None, "last_net": 0, "scored": 0})
            s["cum_net"] += net
            s["buy_days" if net > 0 else "sell_days"] += 1
            if s["last_date"] is None or d > s["last_date"]:
                s["last_date"], s["last_net"] = d, net
            if d in nxt and d in closes and nxt[d] in closes and closes[d]:
                r1 = closes[nxt[d]] / closes[d] - 1
                cap = (1 if net > 0 else -1) * r1 * 100
                s["scored"] += 1
                s["capture"] += cap
                s["wins"] += 1 if cap > 0 else 0
    out = []
    for s in stats.values():
        s["count"] = s["buy_days"] + s["sell_days"]
        n = s["scored"]
        s["win_rate"] = s["wins"] / n if n else None
        s["avg_capture_pct"] = s["capture"] / n if n else None
        s["cum_net_pct"] = s["cum_net"] / vol20 * 100
        s["daytrade"] = src_moneydj.is_daytrade_branch(s["broker"])
        s["foreign"] = src_moneydj.is_foreign(s["broker"])
        if n >= min_count and s["win_rate"] >= 0.65 and s["avg_capture_pct"] > 0.3:
            s["tag"] = "常勝"
        elif n >= min_count and s["win_rate"] <= 0.35 and s["avg_capture_pct"] < -0.3:
            s["tag"] = "常敗"
        else:
            s["tag"] = ""
        del s["capture"], s["wins"]
        out.append(s)
    out.sort(key=lambda x: (-(x["win_rate"] or 0), -(x["avg_capture_pct"] or 0), -x["count"]))
    return out


def summarize(scored, prev_date_iso, vol_prev):
    """
    常勝主力昨天做了什麼、手上有沒有貨。
    回 {winners:[...], losers:[...], winners_last_net, winners_last_net_pct, winners_inventory_pct, losers_last_net, text}
    """
    winners = [s for s in scored if s["tag"] == "常勝"]
    losers = [s for s in scored if s["tag"] == "常敗"]
    w_last = sum(s["last_net"] for s in winners if s["last_date"] == prev_date_iso)
    l_last = sum(s["last_net"] for s in losers if s["last_date"] == prev_date_iso)
    w_inv = sum(s["cum_net"] for s in winners)
    vol_prev = vol_prev or 1
    return {
        "winners": winners[:6], "losers": losers[:4],
        "winners_last_net": w_last, "winners_last_net_pct": w_last / vol_prev * 100,
        "winners_inventory": w_inv, "winners_inventory_pct": sum(s["cum_net_pct"] for s in winners),
        "losers_last_net": l_last, "losers_last_net_pct": l_last / vol_prev * 100,
        "n_scored": len([s for s in scored if s["win_rate"] is not None]),
    }


def describe(w):
    L = []
    if not w or not w.get("n_scored"):
        return "  常勝主力：資料不足"
    L.append(f"  常勝主力（近20日在此股隔日勝率 ≥65%）：昨日合計 {w['winners_last_net']:+} 張 ({w['winners_last_net_pct']:+.1f}% 量)，20日累積庫存 {w['winners_inventory']:+} 張 ({w['winners_inventory_pct']:+.1f}% 量)")
    for s in w["winners"]:
        L.append(f"    {s['broker']:<14} 勝率 {s['win_rate']*100:.0f}% ({s['scored']}次) 平均捕捉 {s['avg_capture_pct']:+.2f}%  累積 {s['cum_net']:+}  最近 {s['last_date'][5:] if s['last_date'] else '-'} {s['last_net']:+}"
                 + ("  [隔日沖]" if s["daytrade"] else "") + ("  [外資]" if s["foreign"] else ""))
    if w["losers"]:
        L.append(f"  常敗分點（反指標）：昨日合計 {w['losers_last_net']:+} 張  " + "  ".join(f"{s['broker']} 勝率{s['win_rate']*100:.0f}%" for s in w["losers"]))
    return "\n".join(L)
