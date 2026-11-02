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


def range_position(p, h, l):
    if p is None or h is None or l is None or h == l:
        return None
    return (p - l) / (h - l)


def nearest_levels(price, levels, above=True, k=3):
    """從 levels [(name, value)] 取價格上方/下方最近 k 個。"""
    cand = [(n, v) for n, v in levels if v is not None and ((v > price) if above else (v < price))]
    cand.sort(key=lambda x: (x[1] - price) if above else (price - x[1]))
    return cand[:k]
