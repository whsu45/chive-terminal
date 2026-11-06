#!/usr/bin/env python3
"""
進場規則回測：「舊：確認後用現價進場」vs「新：追價上限 + 進場區等回」，按行情分四類統計。
資料：.cache/ticks/CODE_YYYY-MM-DD.json（calibrate.py 抓的歷史逐筆，[秒, 價, 量, 內外盤]）。

訊號機制跟 monitor._signal 主計畫同構，但價位用通用的盤中支撐壓力（模型的盤前資料只能建「今天」，歷史日無法還原）：
  價位 = 昨收、昨高、昨低、今開、開盤 15 分鐘高低、VWAP、盤中高低（到前一根）
  做多：最近 12 根 1 分 K 碰過最近支撐 S（±1 tick），上一根 1 分 K 收 ≥ S+1 tick，現價還沒到目標 → 訊號
        停損 S − max(2 tick, 0.15×昨日振幅)，目標 = 上方第一個離進場 ≥ max(3 tick, 0.5%, 0.2×昨日振幅) 的價位
  做空對稱。09:10–13:05 發訊號，同方向 30 分鐘內不重發；13:25 強制平倉。新舊兩版用**同一批訊號**比。
舊：訊號那根收盤價進場。新：monitor._entry_zone 算進場區，收盤價在區內照進；不在就 20 分鐘內等回到區（掛單價成交），
    沒回來或先到目標 = 未成交（報酬 0、無成本）。進場後 30 分鐘內先到目標 1 = 贏、先到停損 = 輸（同一根都碰到算輸）、都沒到 = 第 30 根收盤出場。
行情分類（事後看當天）：大行情 = 當天 開→收 |漲跌| ≥ 3%，其餘一般；順勢 = 上漲日做多 / 下跌日做空，逆勢相反。
成本：每筆成交扣 0.235%（手續費 0.1425%×2 打 3 折 + 當沖證交稅 0.15%）。
    python3 backtest_entry.py
    python3 backtest_entry.py --big 4 --cost 0.2 --detail
"""
import os
import glob
import json
import argparse
from collections import defaultdict

from config import CACHE_DIR
import indicators as I
from monitor import Monitor

TICK_DIR = os.path.join(CACHE_DIR, "ticks")


def load_days():
    by = defaultdict(dict)
    for p in glob.glob(os.path.join(TICK_DIR, "*.json")):
        code, date = os.path.basename(p)[:-5].split("_")
        by[code][date] = p
    return by


def bars_1m(rows):
    """[秒, 價, 量, _] → [(分鐘序, o, h, l, c, v, 金額)]，只取 09:00–13:30。"""
    out = {}
    for sec, px, v, *_ in rows:
        if not (32400 <= sec <= 48600):
            continue
        m = min(sec // 60, 809)          # 13:30 那筆併到 13:29
        b = out.get(m)
        if b is None:
            out[m] = [m, px, px, px, px, v, px * v]
        else:
            b[2] = max(b[2], px); b[3] = min(b[3], px); b[4] = px; b[5] += v; b[6] += px * v
    return [out[k] for k in sorted(out)]


def hhmm(m):
    return f"{m // 60:02d}:{m % 60:02d}"


def levels_at(i, bars, prev):
    """第 i 根收盤時看得到的價位。"""
    done = bars[:i + 1]
    lv = [prev["close"], prev["high"], prev["low"], bars[0][1]]
    if done[-1][0] >= 555:                                     # 09:15 後才有開盤區間
        orb = [b for b in bars if b[0] < 555]
        lv += [max(b[2] for b in orb), min(b[3] for b in orb)]
    vol = sum(b[5] for b in done)
    if vol:
        lv.append(sum(b[6] for b in done) / vol)
    if i >= 1:
        lv += [max(b[2] for b in done[:-1]), min(b[3] for b in done[:-1])]
    return sorted(set(round(x, 2) for x in lv if x))


def gen_signals(bars, prev):
    """回 [(i, side, trig, entry, stop, t1)]。"""
    sigs = []
    last_side_m = {"long": -999, "short": -999}
    rng = prev["high"] - prev["low"]
    for i in range(4, len(bars)):
        m = bars[i][0]
        if not (550 <= m < 785):                               # 09:10–13:05
            continue
        c = bars[i][4]
        tick = I.tick_size(c)
        pad = max(2 * tick, round(0.15 * rng, 2))
        min_gain = max(3 * tick, c * 0.005, 0.2 * rng)
        lv = levels_at(i, bars, prev)
        recent = bars[max(0, i - 11):i + 1]
        below = [x for x in lv if x < c - tick / 2]
        above = [x for x in lv if x > c + tick / 2]
        # 做多
        if below and m - last_side_m["long"] >= 30:
            s = below[-1]
            entry = I.round_tick(s + tick, "up"); stop = I.round_tick(s - pad, "down")
            t1 = next((x for x in above if x - entry >= min_gain), None) or I.round_tick(entry + max(min_gain, 0.5 * rng), "up")
            if any(b[3] <= s + tick for b in recent) and c >= entry and c < t1 and c > stop:
                sigs.append((i, "long", s, entry, stop, t1)); last_side_m["long"] = m
        # 做空
        if above and m - last_side_m["short"] >= 30:
            r = above[0]
            entry = I.round_tick(r - tick, "down"); stop = I.round_tick(r + pad, "up")
            t1 = next((x for x in reversed(below) if entry - x >= min_gain), None) or I.round_tick(entry - max(min_gain, 0.5 * rng), "down")
            if any(b[2] >= r - tick for b in recent) and c <= entry and c > t1 and c < stop:
                sigs.append((i, "short", r, entry, stop, t1)); last_side_m["short"] = m
    return sigs


def run_trade(bars, i_fill, px, side, stop, t1, skip_t1_first_bar):
    """從 i_fill 那根起最多 30 根；回 (報酬%, 結果)。"""
    short = side == "short"
    last = min(i_fill + 30, len(bars) - 1)
    for j in range(i_fill, last + 1):
        b = bars[j]
        if b[0] >= 805:                                        # 13:25 強制平倉
            ex = b[1]; return ((px - ex) if short else (ex - px)) / px * 100, "flat"
        if j == i_fill and skip_t1_first_bar is None:
            continue                                           # 現價進場：訊號那根收盤才進，從下一根算
        hit_stop = (b[2] >= stop) if short else (b[3] <= stop)
        hit_t1 = ((b[3] <= t1) if short else (b[2] >= t1)) and not (j == i_fill and skip_t1_first_bar)
        if hit_stop:
            return ((px - stop) if short else (stop - px)) / px * 100, "loss"
        if hit_t1:
            return ((px - t1) if short else (t1 - px)) / px * 100, "win"
    ex = bars[last][4]
    return ((px - ex) if short else (ex - px)) / px * 100, "flat"


STRICT_FILL = False   # True：要價格穿過掛單價 1 tick 才算成交（排隊排不到的保守假設）


def simulate(bars, sig):
    i, side, trig, entry, stop, t1 = sig
    short = side == "short"
    c = bars[i][4]
    old = run_trade(bars, i, c, side, stop, t1, None)
    old_rr = abs(t1 - c) / abs(c - stop) if c != stop else None
    z = Monitor._entry_zone(side, entry, stop, t1)
    if not z:
        new = (0.0, "skip")
    else:
        lo, hi = z
        inzone = (c >= lo) if short else (c <= hi)
        if inzone:
            new = old
        else:
            lim = lo if short else hi
            new = (0.0, "nofill")
            for j in range(i + 1, min(i + 21, len(bars))):
                b = bars[j]
                if b[0] >= 785:
                    break                                      # 13:05 後不進場
                gap = I.tick_size(lim) if STRICT_FILL else 0
                if (b[2] >= lim + gap) if short else (b[3] <= lim - gap):
                    new = run_trade(bars, j, lim, side, stop, t1, True); break
                if (b[3] <= t1) if short else (b[2] >= t1):
                    break
    return old, new, old_rr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--big", type=float, default=3.0, help="大行情門檻：當天開→收 |漲跌%%| ≥ 這個值（預設 3）")
    ap.add_argument("--cost", type=float, default=0.235, help="每筆成交的來回成本 %%（預設 0.235）")
    ap.add_argument("--live", type=float, default=None, help="改用「訊號當下」分類（不偷看收盤）：開盤到訊號時 |漲跌%%| ≥ 這個值算大行情，方向用開盤到訊號時")
    ap.add_argument("--strict-fill", action="store_true", help="掛單要被穿過 1 tick 才算成交（保守，排隊可能排不到）")
    ap.add_argument("--detail", action="store_true")
    a = ap.parse_args()
    global STRICT_FILL
    STRICT_FILL = a.strict_fill
    days = load_days()
    stat = defaultdict(lambda: {"old": [], "new": []})
    n_days = defaultdict(int)
    details = []
    for code, dmap in sorted(days.items()):
        dates = sorted(dmap)
        prev = None
        for d in dates:
            try:
                rows = json.load(open(dmap[d], encoding="utf-8"))
            except Exception:
                prev = None; continue
            bars = bars_1m(rows)
            if len(bars) < 60:
                prev = None; continue
            if prev:
                day_ret = (bars[-1][4] / bars[0][1] - 1) * 100
                big = abs(day_ret) >= a.big
                n_days["大行情" if big else "一般"] += 1
                for sig in gen_signals(bars, prev):
                    side = sig[1]
                    ref_ret = day_ret
                    if a.live is not None:
                        ref_ret = (bars[sig[0]][4] / bars[0][1] - 1) * 100
                        big = abs(ref_ret) >= a.live
                    trend = "順勢" if (ref_ret > 0) == (side == "long") else "逆勢"
                    key = ("大行情" if big else "一般", trend)
                    old, new, rr = simulate(bars, sig)
                    stat[key]["old"].append(old); stat[key]["new"].append(new)
                    details.append((code, d, hhmm(bars[sig[0]][0]), side, key, round(day_ret, 1), bars[sig[0]][4], sig, old, new, rr))
            prev = {"close": bars[-1][4], "high": max(b[2] for b in bars), "low": min(b[3] for b in bars)}

    def summ(rs, cost):
        filled = [r for r in rs if r[1] in ("win", "loss", "flat")]
        n = len(rs); nf = len(filled)
        w = sum(1 for r in filled if r[1] == "win"); l = sum(1 for r in filled if r[1] == "loss")
        net = [r[0] - cost for r in filled]
        losses = [x for x in net if x < 0]
        return {"n": n, "fill": nf, "win": w, "loss": l, "wr": w / (w + l) * 100 if (w + l) else float("nan"),
                "avg_trade": sum(net) / nf if nf else float("nan"), "per_sig": sum(net) / n if n else float("nan"),
                "total": sum(net), "avg_loss": sum(losses) / len(losses) if losses else 0.0,
                "skip": sum(1 for r in rs if r[1] == "skip"), "nofill": sum(1 for r in rs if r[1] == "nofill")}

    print(f"樣本：{len(days)} 檔，{sum(n_days.values())} 個股日（大行情 {n_days['大行情']}、一般 {n_days['一般']}，大行情 = 開→收 ≥{a.big:g}%），成本每筆 {a.cost}%")
    if a.live is not None:
        print(f"分類改用訊號當下：開盤到訊號時 |漲跌| ≥{a.live:g}% 算大行情，順逆勢也用開盤到訊號時的方向（不偷看收盤）")
    print()
    hdr = f"{'':<12}{'版本':<4}{'訊號':>5}{'成交':>5}{'勝':>4}{'敗':>4}{'勝率':>7}{'每筆成交均':>10}{'每訊號均':>9}{'總報酬':>9}{'平均虧損':>9}  沒做"
    print(hdr)
    for key in [("大行情", "順勢"), ("大行情", "逆勢"), ("一般", "順勢"), ("一般", "逆勢")]:
        if key not in stat:
            continue
        for ver in ("old", "new"):
            s = summ(stat[key][ver], a.cost)
            lab = f"{key[0]}{key[1]}" if ver == "old" else ""
            extra = "" if ver == "old" else f"  不發 {s['skip']}、未成交 {s['nofill']}"
            print(f"{lab:<12}{'舊' if ver == 'old' else '新':<4}{s['n']:>5}{s['fill']:>5}{s['win']:>4}{s['loss']:>4}{s['wr']:>6.0f}%{s['avg_trade']:>+9.2f}%{s['per_sig']:>+8.2f}%{s['total']:>+8.1f}%{s['avg_loss']:>+8.2f}%{extra}")
        print()
    allo = [r for k in stat for r in stat[k]["old"]]; alln = [r for k in stat for r in stat[k]["new"]]
    so, sn = summ(allo, a.cost), summ(alln, a.cost)
    print(f"{'全部':<12}{'舊':<4}{so['n']:>5}{so['fill']:>5}{so['win']:>4}{so['loss']:>4}{so['wr']:>6.0f}%{so['avg_trade']:>+9.2f}%{so['per_sig']:>+8.2f}%{so['total']:>+8.1f}%{so['avg_loss']:>+8.2f}%")
    print(f"{'':<12}{'新':<4}{sn['n']:>5}{sn['fill']:>5}{sn['win']:>4}{sn['loss']:>4}{sn['wr']:>6.0f}%{sn['avg_trade']:>+9.2f}%{sn['per_sig']:>+8.2f}%{sn['total']:>+8.1f}%{sn['avg_loss']:>+8.2f}%  不發 {sn['skip']}、未成交 {sn['nofill']}")
    print("\n每筆成交均 = 有成交的單平均淨報酬；每訊號均 = 總淨報酬 ÷ 訊號數（沒做的算 0）；平均虧損 = 虧錢那幾筆的平均淨報酬")
    if a.detail:
        print()
        for code, d, t, side, key, dr, c, sig, old, new, rr in details:
            print(f"{d} {t} {code} {side:<5} {key[0]}{key[1]}(日 {dr:+.1f}%) 價 {c} 觸發 {sig[2]} 停 {sig[4]} 目 {sig[5]} RR {rr and round(rr, 1)} | 舊 {old[1]} {old[0]:+.2f}% | 新 {new[1]} {new[0]:+.2f}%")


if __name__ == "__main__":
    main()
