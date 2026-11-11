"""
常勝分點：用該股近 20 日每天的分點買賣超（MoneyDJ 單日表，買超、賣超各前 15 名）配合日K，
找出在這檔股票上「買了隔天盤中漲、賣了隔天盤中跌」的分點，再看他們昨天的動作。

打分（2026-11-10 改）：
  每筆 (分點, 日 d, 買賣超) 的結果 = 方向 × 隔天「開盤到收盤」的漲跌 %。這是當沖賺得到的那一段；
  原本用收盤到隔天收盤，跳空那一段當沖根本吃不到。
  常勝 = 有結果的次數 ≥6，而且平均結果明顯大於 0（t ≥ 2：平均 ÷ 標準誤，次數少、忽大忽小的過不了）。常敗 = t ≤ −2。

為什麼改：原本「≥4 次、勝率 ≥65%」太鬆，丟銅板 4 次對 3 次以上就有 31%，每檔每天平均標出 7 個常勝、7 個常敗。
逐日前推回測（27 檔、864 個股票日，第 t 天只用 t−20…t−1 的資料）：
  原規則  常勝昨天淨買賣 ≥1.5% 量 → 隔天順向超額 開→收 +0.24%（t 1.2）；常敗當反指標 −0.13%（沒用）
  新規則  每檔每天約 1 個常勝；常勝昨天淨買 → 隔天開→收超額 +1.43%（73 次，t 3.3）；淨賣 → −0.22%（70 次，沒用）
新規則是 7 種寫法裡最好的一種，數字有挑過的成分，實際效果應該更小；所以只留「常勝進場」加分，出場、庫存、常敗都不計分。

限制：分點資料只有盤後，盤中拿不到；單日表只列前 15 名，小額進出看不到。
"""
import datetime as dt
import src_moneydj
import tw_calendar as cal


def broker_daily_history(code, days=20, ref_date=None):
    """近 N 個交易日逐日分點表 → {date_iso: {broker: net}}。過去的日子存在 .cache/zco/（src_moneydj.zco_day），每天只需補抓一頁。"""
    ref = ref_date or cal.today_tw()
    out = {}
    for d in cal.last_n_trading_days(ref, days):
        try:
            summ = src_moneydj.zco_day(code, d)
        except Exception:
            continue
        rows = {}
        for r in summ.get("buyers", []) + summ.get("sellers", []):
            rows[r["broker"]] = rows.get(r["broker"], 0) + r["net"]
        if rows:
            out[d.isoformat()] = rows
    return out


MIN_SCORED = 6       # 至少幾次有結果才評
T_MIN = 2.0          # 平均結果 ÷ 標準誤 ≥ 這個才算常勝（≤ 負的這個 = 常敗）


def _tstat(xs):
    n = len(xs)
    if n < 3:
        return None
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    if var <= 0:
        return 99.0 if m > 0 else (-99.0 if m < 0 else None)      # 每次結果都一樣：不會有除以零，也不寫 Infinity 進 JSON
    return max(-99.0, min(99.0, m / (var / n) ** 0.5))


def score_branches(hist_rows, broker_days, min_count=MIN_SCORED, t_min=T_MIN):
    """
    hist_rows: 日K [{date, o, c, v}] 舊→新；broker_days: {date: {broker: net}}
    每筆 (分點, 日 d, net) 的結果 = sign(net) × 隔天開盤到收盤的漲跌 %（隔天沒有開盤價才退回收盤到收盤）。
    回 [{broker, count, scored, win_rate, avg_capture_pct, t, cum_net, cum_net_pct, last_date, last_net, buy_days, sell_days, tag}]
    """
    rows = {r["date"]: r for r in hist_rows}
    dates = sorted(rows)
    nxt = {d: dates[i + 1] for i, d in enumerate(dates[:-1])}
    vol20 = sum(r["v"] for r in hist_rows[-20:]) or 1
    stats = {}
    for d, brs in broker_days.items():
        for broker, net in brs.items():
            if net == 0:
                continue
            s = stats.setdefault(broker, {"broker": broker, "count": 0, "caps": [], "cum_net": 0,
                                           "buy_days": 0, "sell_days": 0, "last_date": None, "last_net": 0})
            s["cum_net"] += net
            s["buy_days" if net > 0 else "sell_days"] += 1
            if s["last_date"] is None or d > s["last_date"]:
                s["last_date"], s["last_net"] = d, net
            n1 = rows.get(nxt.get(d))
            if n1 and n1.get("c"):
                base = n1.get("o") or (rows[d].get("c") if d in rows else None)
                if base:
                    s["caps"].append((1 if net > 0 else -1) * (n1["c"] / base - 1) * 100)
    out = []
    for s in stats.values():
        caps = s.pop("caps")
        n = len(caps)
        s["count"] = s["buy_days"] + s["sell_days"]
        s["scored"] = n
        s["win_rate"] = sum(1 for c in caps if c > 0) / n if n else None
        s["avg_capture_pct"] = sum(caps) / n if n else None
        s["t"] = _tstat(caps)
        s["cum_net_pct"] = s["cum_net"] / vol20 * 100
        s["daytrade"] = src_moneydj.is_daytrade_branch(s["broker"])
        s["foreign"] = src_moneydj.is_foreign(s["broker"])
        s["tag"] = ""
        if n >= min_count and s["t"] is not None:
            s["tag"] = "常勝" if s["t"] >= t_min else ("常敗" if s["t"] <= -t_min else "")
        out.append(s)
    out.sort(key=lambda x: (-(x["t"] if x["t"] is not None else -99), -(x["avg_capture_pct"] or 0), -x["count"]))
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
        return "  常勝分點：資料不足"
    if not w["winners"]:
        L.append(f"  常勝分點（近 20 日至少 {MIN_SCORED} 次、買賣後隔天開盤到收盤明顯站對邊）：這檔目前沒有")
    else:
        L.append(f"  常勝分點（近 20 日至少 {MIN_SCORED} 次、買賣後隔天開盤到收盤明顯站對邊）：昨日合計 {w['winners_last_net']:+} 張 ({w['winners_last_net_pct']:+.1f}% 量)")
    for s in w["winners"]:
        L.append(f"    {s['broker']:<14} {s['scored']} 次對 {round((s['win_rate'] or 0) * s['scored'])} 次，平均每次 {s['avg_capture_pct']:+.2f}%  累積 {s['cum_net']:+}  最近 {s['last_date'][5:] if s['last_date'] else '-'} {s['last_net']:+}"
                 + ("  [隔日沖]" if s["daytrade"] else "") + ("  [外資]" if s["foreign"] else ""))
    if w["losers"]:
        L.append("  常敗分點（回測當反指標沒有用，只列出不計分）：" + "  ".join(f"{s['broker']} {s['scored']}次對{round((s['win_rate'] or 0) * s['scored'])}次" for s in w["losers"]))
    return "\n".join(L)
