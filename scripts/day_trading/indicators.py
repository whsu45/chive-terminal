"""技術指標與台股 tick 規則（純函式）。"""
import math


def tick_size(p):
    if p < 10: return 0.01
    if p < 50: return 0.05
    if p < 100: return 0.1
    if p < 500: return 0.5
    if p < 1000: return 1.0
    return 5.0


def round_tick(p, direction="down"):
    t = tick_size(p)
    q = p / t
    q = math.floor(q + 1e-9) if direction == "down" else math.ceil(q - 1e-9)
    return round(q * t, 2)


def limits(prev_close):
    """漲停/跌停（10%，依 tick 取整）。"""
    up = round_tick(prev_close * 1.10, "down")
    down = round_tick(prev_close * 0.90, "up")
    return up, down


def sma(vals, n):
    v = [x for x in vals if x is not None]
    if len(v) < n:
        return None
    return sum(v[-n:]) / n


def stdev(vals, n):
    v = [x for x in vals if x is not None][-n:]
    if len(v) < n:
        return None
    m = sum(v) / n
    return math.sqrt(sum((x - m) ** 2 for x in v) / n)


def rsi(closes, n=14):
    c = [x for x in closes if x is not None]
    if len(c) < n + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(c)):
        d = c[i] - c[i - 1]
        gains.append(max(d, 0)); losses.append(max(-d, 0))
    ag = sum(gains[:n]) / n; al = sum(losses[:n]) / n
    for i in range(n, len(gains)):
        ag = (ag * (n - 1) + gains[i]) / n
        al = (al * (n - 1) + losses[i]) / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def atr(rows, n=14):
    if len(rows) < n + 1:
        return None
    trs = []
    for i in range(1, len(rows)):
        h, l, pc = rows[i]["h"], rows[i]["l"], rows[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-n:]) / n


def daily_snapshot(rows):
    """
    rows: 日K [{date,o,h,l,c,v}] 由舊到新（不含今天）。
    回傳 MA/BB/ATR/RSI/量能/位置等。
    """
    if not rows:
        return {}
    closes = [r["c"] for r in rows]
    vols = [r["v"] for r in rows]
    last = rows[-1]
    ma20 = sma(closes, 20); sd20 = stdev(closes, 20)
    out = {
        "date": last["date"], "close": last["c"], "open": last["o"], "high": last["h"], "low": last["l"], "vol": last["v"],
        "ma5": sma(closes, 5), "ma10": sma(closes, 10), "ma20": ma20, "ma60": sma(closes, 60),
        "bb_up": (ma20 + 2 * sd20) if (ma20 and sd20) else None, "bb_dn": (ma20 - 2 * sd20) if (ma20 and sd20) else None,
        "atr14": atr(rows, 14), "rsi14": rsi(closes, 14),
        "vol_ma5": sma(vols, 5), "vol_ma20": sma(vols, 20),
        "chg_pct": (last["c"] / rows[-2]["c"] - 1) * 100 if len(rows) > 1 and rows[-2]["c"] else None,
        "high20": max(r["h"] for r in rows[-20:]), "low20": min(r["l"] for r in rows[-20:]),
        "high60": max(r["h"] for r in rows[-60:]), "low60": min(r["l"] for r in rows[-60:]),
        "prev_high": rows[-2]["h"] if len(rows) > 1 else None, "prev_low": rows[-2]["l"] if len(rows) > 1 else None,
        "ret5": (last["c"] / rows[-6]["c"] - 1) * 100 if len(rows) > 5 else None,
        "ret20": (last["c"] / rows[-21]["c"] - 1) * 100 if len(rows) > 20 else None,
    }
    # 連續跌/漲天數
    streak = 0
    for i in range(len(rows) - 1, 0, -1):
        d = rows[i]["c"] - rows[i - 1]["c"]
        if streak == 0:
            streak = 1 if d > 0 else (-1 if d < 0 else 0)
            if streak == 0:
                break
        elif (d > 0) == (streak > 0) and d != 0:
            streak += 1 if streak > 0 else -1
        else:
            break
    out["streak"] = streak
    out["atr_pct"] = (out["atr14"] / last["c"] * 100) if out["atr14"] else None
    return out


def kd(hlc, n=9):
    """
    KD(9,3,3)：RSV =（收 − 最近 n 根最低）÷（最近 n 根最高 − 最低）× 100（不足 n 根用現有的），
    K = 前一個 K × 2/3 + RSV × 1/3，D = 前一個 D × 2/3 + K × 1/3，從 50 起算。跟儀表板的 KD 圖（index.html kdCalc）同一套算法。
    hlc：[(高, 低, 收)]，舊到新。回 [(K, D)]。
    """
    k = d = 50.0
    out = []
    for i, (h, l, c) in enumerate(hlc):
        win = hlc[max(0, i - n + 1): i + 1]
        hh, ll = max(x[0] for x in win), min(x[1] for x in win)
        rsv = (c - ll) / (hh - ll) * 100 if hh > ll else 50.0
        k = k * 2 / 3 + rsv / 3
        d = d * 2 / 3 + k / 3
        out.append((k, d))
    return out


def kd_state(hlc, n=9):
    """最後一根的 KD：{k, d, cross（最後一根剛發生的黃金交叉 / 死亡交叉）, zone（K≥80 高檔 / K≤20 低檔）, bars}；沒資料回 None。"""
    s = kd(hlc, n)
    if not s:
        return None
    k, d = s[-1]
    cross = None
    if len(s) >= 2:
        pk, pd = s[-2]
        if pk <= pd and k > d:
            cross = "黃金交叉"
        elif pk >= pd and k < d:
            cross = "死亡交叉"
    return {"k": round(k, 1), "d": round(d, 1), "cross": cross, "zone": "高檔" if k >= 80 else ("低檔" if k <= 20 else None), "bars": len(s)}


def kd_dual(stk_series, idx_series, tf=3, lo=20, hi=80, min_bars=3):
    """
    KD 共振（2026-11-26 加，使用者要求）：大盤與個股同一個週期（預設 3 分 K）的 KD(9,3,3) 同時在低檔或高檔。
    - 兩邊 K 都 ≤lo → state = "low"（雙低檔，買進訊號）；兩邊 K 都 ≥hi → "high"（雙高檔，賣出訊號）；其餘 None。
    - 只看 K（跟儀表板「K≤20 低檔」同一個定義）；最後一根是還沒走完的那根（跟儀表板 KD 圖一樣，盤中每分鐘會變）。
    - 每天 09:00 從 50 起算，開盤前 min_bars 根（3 分 K 就是 9 分鐘）還在暖身，不判斷。
    stk_series：個股 1 分 K [[HH:MM, 開, 高, 低, 收, ...]]；idx_series：指數 [[HH:MM, 值]]。
    回 {tf, stk, idx（各是 kd_state 的結果或 None）, state}。
    """
    s = kd_state(tf_hlc(stk_series, tf)) if stk_series else None
    i = kd_state(tf_hlc(idx_series, tf)) if idx_series else None
    st = None
    if s and i and s["bars"] >= min_bars and i["bars"] >= min_bars:
        st = "low" if (s["k"] <= lo and i["k"] <= lo) else ("high" if (s["k"] >= hi and i["k"] >= hi) else None)
    return {"tf": tf, "stk": s, "idx": i, "state": st}


def tf_hlc(series, tf=5):
    """
    盤中 1 分 K → tf 分 K 的 [(高, 低, 收)]（09:00 起每 tf 分鐘一根，最後一根是還沒走完的，跟儀表板 KD 圖一樣）。
    series：個股 [[HH:MM, 開, 高, 低, 收, ...]]，或指數 [[HH:MM, 值]]（指數每分鐘只有一個取樣值，高低用它代替）。
    """
    out, key = [], None
    for row in series or []:
        try:
            hh, mm = str(row[0]).split(":")[:2]
            m = (int(hh) - 9) * 60 + int(mm)
        except (ValueError, IndexError):
            continue
        if not 0 <= m <= 270:
            continue
        h, l, c = (row[2], row[3], row[4]) if len(row) >= 5 else (row[1], row[1], row[1])
        if h is None or l is None or c is None:
            continue
        b = m // tf
        if out and key == b:
            oh, ol, _ = out[-1]
            out[-1] = (max(oh, h), min(ol, l), c)
        else:
            out.append((h, l, c))
            key = b
    return out


def range_position(p, h, l):
    if p is None or h is None or l is None or h == l:
        return None
    return (p - l) / (h - l)


def nearest_levels(price, levels, above=True, k=3):
    """從 levels [(name, value)] 取價格上方/下方最近 k 個。"""
    cand = [(n, v) for n, v in levels if v is not None and ((v > price) if above else (v < price))]
    cand.sort(key=lambda x: (x[1] - price) if above else (price - x[1]))
    return cand[:k]
