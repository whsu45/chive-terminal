#!/usr/bin/env python3
"""
竭盡跡象（**觀察用，不計分**）：盤中「五檔、成交明細、分時量」三個畫面各自的「買盤用完了 / 賣盤用完了」型態。
想法來自當沖實務的說法：一路開高走高之後，「買單掛得密密麻麻、成交卻全變小單、開始掉出大綠單」同時出現就是該停利的時候。

  掛單（五檔）  S 掛單虛胖    價格在今高 2 檔內、3 分鐘沒再創高，委買五檔合計卻是委賣的 3 倍以上
  明細（逐筆）  A 大單熄火    一路大外盤單往上推（火箭段）之後，連續 2 分鐘沒有任何大外盤單，只剩小單在跳，價格還停在高點 2 檔內
                B 大內盤單    火箭段之後第一筆「往下扣一檔成交」的大內盤單（前 3 分鐘沒出現過）
  K 棒（分時量）C 量縮價滯    前一根 5 分 K 放量大漲創今高，這一根幾乎沒漲、量縮到四成以下
                D 天量長影    1 分 K 爆出今天最大量又創今高，但留一半以上的上影線或收黑
  賣盤竭盡是鏡像（今低、大內盤單往下殺、第一筆往上扣一檔的大外盤單、長下影）。
  火箭段 = 5 分鐘內 ≥5 筆同向大單、量是反向大單的 2 倍以上、把價格推 ≥3 檔而且創了今高（今低）。大單門檻跟主力分流同一套。

**為什麼不計分**（2026-11-11 回測，9 檔 × 42 天逐筆、378 個股票日；報酬是反轉方向、進場用保守價，都還沒扣 0.235% 成本）：
                    次數   5 分     10 分    30 分
  對照：火箭段進行中、價格在高點 2 檔內的任意時點
    買方           1,591  +0.02%  +0.09%  +0.07%
    賣方           2,178  +0.01%  +0.02%  −0.02%
  A 大單熄火  買     116  −0.03%  +0.01%  +0.09%      賣  134  +0.01%  +0.06%  −0.01%
  B 大內盤單  買      58  +0.05%  +0.08%  −0.14%      賣   65  +0.02%  −0.02%  −0.02%
  C 量縮價滯  買      32  −0.08%  −0.13%  −0.23%      賣   20  +0.01%  −0.04%  +0.08%
  D 天量長影  買      21  −0.02%  +0.14%  +0.26%      賣   11  +0.15%  +0.03%  +0.01%
  A 之後 5 分鐘內又出現 B：買 8 次 −0.13%（10 分）、賣 7 次 +0.17%。
  - 沒有一個比「正在漲、價格在高點附近」的對照組穩定地好，前後半段方向也不一致，幅度都小於成本。
  - C（量縮價滯）在買方前後半段都是負的：放量大漲後縮量停住，比較常是休息之後續漲，不是見頂。門檻放寬（量縮到六成、漲幅四成）結論一樣。
  - D 看起來最好，但只有 21 次；把形狀條件拿掉（只要天量創高）有 189 次，10 分 +0.06%，跟對照組一樣。
  - S（掛單虛胖）沒辦法回測：手上沒有歷史五檔。monitor 每分鐘的紀錄裡「高點附近、委買 ≥3 倍」只有 4 筆。
  - 這 9 檔多半是大型股；你實際在做的中小型股、有隔日沖的股票沒有逐筆歷史，結論不一定能套過去。
  所以先偵測、顯示、發 👁 警示並追蹤結果（復盤畫面的「警示回顧」會算它比基準高多少），等自己的股票累積夠了再決定要不要給分數。
"""
import bisect
import datetime as dt
import indicators as I

RW, R_NBIG, R_RATIO, R_PUSH = 300, 5, 2.0, 3        # 火箭段：5 分鐘內 ≥5 筆大單、量是反向大單的 2 倍、推 ≥3 檔且創今高
A_QUIET, A_SMALL, A_NEAR = 120, 5, 2                # A：2 分鐘沒有同向大單、期間同向小單 ≥5 筆、離今高 ≤2 檔
B_CLEAN, B_NEAR = 180, 3                            # B：前 3 分鐘沒有反向大單、成交前離今高 ≤3 檔
C_BODY_TK, C_BODY_PCT, C_VOL, C_STALL, C_SHRINK = 3, 0.4, 1.5, 0.25, 0.4   # C：前一根實體 ≥3 檔且 ≥0.4%、量 ≥ 之前中位數 1.5 倍；這一根實體 ≤25%、量 ≤40%
D_VOL, D_RANGE_TK, D_SHADOW, D_BLACK_TK = 3, 3, 0.5, 2   # D：量 ≥ 之前 1 分 K 平均的 3 倍且是今天最大、振幅 ≥3 檔；影線 ≥ 一半，或收黑 ≥2 檔
S_RATIO, S_NEAR, S_STALL = 3.0, 2, 180              # S：同向五檔 ≥ 反向 3 倍、離今高 ≤2 檔、3 分鐘沒再創高
WINDOW = 900                                        # 要給最近幾秒的逐筆
TTL = 300                                           # 一個跡象在面板上留幾秒
T_START, T_END = "09:05", "13:15"

CATS = {"S": "book", "A": "tick", "B": "tick", "C": "bar", "D": "bar"}
CAT_LABEL = {"book": "掛單", "tick": "明細", "bar": "K 棒"}
NAMES = {"S": "掛單虛胖", "A": "大單熄火", "B": "大內盤單", "C": "量縮價滯", "D": "天量長影"}
NAMES_SELL = {**NAMES, "B": "大外盤單"}
UNTESTED = {"S"}


def name_of(side, key):
    return (NAMES if side == "buy" else NAMES_SELL)[key]


def alert_types():
    """monitor.ALERT_TYPES 用：[(種類, 比對警示文字的 regex)]。種類名稱帶買賣方，復盤時才分得出預期方向。"""
    return [(f"{lab}跡象·{name_of(sd, k)}", rf"^👁 {lab}竭盡跡象·{name_of(sd, k)}")
            for sd, lab in (("buy", "買盤"), ("sell", "賣盤")) for k in "SABCD"]


def _hm(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%H:%M")


def _fp(p):
    return f"{p:g}"


def detect(ticks, big_lots, bars, book, now_ts, seen):
    """
    ticks    最近 WINDOW 秒的逐筆 [(epoch, 價, 張, 1 外盤 / 2 內盤)]，舊到新
    big_lots 大單門檻（張）
    bars     今天的 1 分 K [[HH:MM, 開, 高, 低, 收, 量, ...], ...]
    book     {"bids": [[價, 張], ...], "asks": [...]}，沒有就 None
    seen     已經判斷過的事件（呼叫端保存的 set；每個事件只判斷一次，這裡會把新的加進去）
    回這一次新出現的跡象 [{side, key, cat, name, ts, time, text}]。
    """
    out = []
    if not ticks or not (T_START <= _hm(now_ts) <= T_END):
        return out
    ts = [t[0] for t in ticks]
    px = [t[1] for t in ticks]
    n = len(ticks)
    big = big_lots or 0

    def idx(x):                                     # 最後一筆時間 <= x 的位置
        return bisect.bisect_right(ts, x) - 1

    def add(sd, key, t_ev, text):
        out.append({"side": sd, "key": key, "cat": CATS[key], "name": name_of(sd, key), "ts": t_ev,
                    "time": dt.datetime.fromtimestamp(t_ev).strftime("%H:%M:%S"), "text": text})

    for sg in (1, -1):
        buy = sg > 0
        sd = "buy" if buy else "sell"
        dom, opp = (1, 2) if buy else (2, 1)
        io, oi = ("外", "內") if buy else ("內", "外")
        lvl = "今高" if buy else "今低"
        pick = max if buy else min

        def ext_before(cut):
            """cut（含）以前的今高 / 今低：cut 那一分鐘以前的 1 分 K，加上逐筆裡 cut 以前的。"""
            m = _hm(cut)
            vals = [b[2] if buy else b[3] for b in bars if b[0] < m]
            j = idx(cut)
            if j >= 0:
                vals.append(pick(px[:j + 1]))
            return pick(vals) if vals else None

        def ext_in(a, b):
            seg = px[idx(a) + 1: idx(b) + 1]
            return pick(seg) if seg else None

        if big:
            bd = [i for i in range(n) if ticks[i][2] >= big and ticks[i][3] == dom]
            bo = [i for i in range(n) if ticks[i][2] >= big and ticks[i][3] == opp]
        else:
            bd, bo = [], []
        bd_t = [ts[i] for i in bd]
        bo_t = [ts[i] for i in bo]

        def rocket(x):
            """(x−RW, x] 是不是火箭段；是就回 (大單筆數, 大單張數, 推幾檔, 這段的極值)。"""
            a, b = bisect.bisect_right(bd_t, x - RW), bisect.bisect_right(bd_t, x)
            if b - a < R_NBIG:
                return None
            vd = sum(ticks[i][2] for i in bd[a:b])
            a2, b2 = bisect.bisect_right(bo_t, x - RW), bisect.bisect_right(bo_t, x)
            if vd < R_RATIO * sum(ticks[i][2] for i in bo[a2:b2]):
                return None
            pre, cur = ext_before(x - RW), ext_in(x - RW, x)
            if cur is None or (pre is not None and sg * (cur - pre) <= 0):
                return None                                     # 這 5 分鐘沒有創今高（今低）
            j = idx(x - RW)
            if j >= 0:
                p_start = px[j]
            else:
                m = _hm(x - RW)
                prev = [b_ for b_ in bars if b_[0] < m]
                if not prev:
                    return None
                p_start = prev[-1][4]
            push = sg * (cur - p_start) / I.tick_size(cur)
            if push < R_PUSH - 1e-9:
                return None
            return b - a, vd, round(push), cur

        # ---- 明細 A：大單熄火
        if bd:
            tb = bd_t[-1]
            key = (sd, "A", int(tb * 1000))
            if A_QUIET <= now_ts - tb <= A_QUIET + 60 and key not in seen:
                seen.add(key)
                r = rocket(tb)
                if r:
                    small = sum(1 for i in range(bd[-1] + 1, n) if ticks[i][3] == dom)
                    ext = ext_before(now_ts)
                    tick = I.tick_size(ext)
                    if small >= A_SMALL and sg * (ext - px[-1]) <= A_NEAR * tick + 1e-9:
                        add(sd, "A", now_ts, f"前 5 分鐘 {r[0]} 筆大{io}盤單（{r[1]} 張）把價格推了 {r[2]} 檔到{lvl} {_fp(ext)}，"
                                             f"最後一筆在 {dt.datetime.fromtimestamp(tb).strftime('%H:%M:%S')}；之後 {int(now_ts - tb)} 秒沒有大單，"
                                             f"只剩 {small} 筆小單在跳，價格還在 {_fp(px[-1])}")
        # ---- 明細 B：第一筆往反方向扣一檔成交的大單
        for k in range(len(bo) - 1, -1, -1):
            i, t = bo[k], bo_t[k]
            if now_ts - t > 30:
                break
            key = (sd, "B", int(t * 1000))
            if key in seen:
                continue
            seen.add(key)
            if i == 0 or (k > 0 and bo_t[k - 1] > t - B_CLEAN) or sg * (px[i] - px[i - 1]) >= 0:
                continue
            r = rocket(t)
            if not r:
                continue
            ext = ext_before(ts[i - 1])
            tick = I.tick_size(ext)
            if sg * (ext - px[i - 1]) > B_NEAR * tick + 1e-9:
                continue
            add(sd, "B", t, f"前 5 分鐘 {r[0]} 筆大{io}盤單推了 {r[2]} 檔到{lvl} {_fp(ext)} 之後，出現第一筆大{oi}盤單 {ticks[i][2]} 張，"
                            f"從 {_fp(px[i - 1])} {'往下' if buy else '往上'}成交在 {_fp(px[i])}（前 3 分鐘沒有過）")

        # ---- 掛單 S：同向掛單很厚，價格卻不動
        bids, asks = (book or {}).get("bids") or [], (book or {}).get("asks") or []
        bv, av = sum(v for _, v in bids), sum(v for _, v in asks)
        if bv and av:
            ext = ext_before(now_ts)
            recent = ext_in(now_ts - S_STALL, now_ts)
            pre = ext_before(now_ts - S_STALL)
            first_m = _hm(ts[0])
            old = [b[2] if buy else b[3] for b in bars if b[0] < first_m]
            fresh = not old or sg * (pick(px) - pick(old)) >= 0          # 今高（今低）是最近 15 分鐘內的事
            thick, thin = (bv, av) if buy else (av, bv)
            key = (sd, "S", ext)
            if (key not in seen and ext is not None and pre is not None and fresh and thick >= S_RATIO * thin
                    and sg * (ext - px[-1]) <= S_NEAR * I.tick_size(ext) + 1e-9
                    and (recent is None or sg * (recent - pre) <= 0)):
                seen.add(key)
                add(sd, "S", now_ts, f"{'委買' if buy else '委賣'}五檔 {thick} 張是{'委賣' if buy else '委買'} {thin} 張的 {thick / thin:.1f} 倍，"
                                     f"價格 {_fp(px[-1])} 卻停在{lvl} {_fp(ext)} 附近、{S_STALL // 60} 分鐘沒再{'創高' if buy else '破低'}")

        # ---- K 棒：只看剛走完的那一根
        sec = dt.datetime.fromtimestamp(now_ts)
        now_m = sec.hour * 60 + sec.minute - 540
        b1 = {}
        for b in bars:
            m = int(b[0][:2]) * 60 + int(b[0][3:5]) - 540
            if 0 <= m < now_m:
                b1[m] = b
        midnight = now_ts - (sec.hour * 3600 + sec.minute * 60 + sec.second + sec.microsecond / 1e6)
        # D 天量長影（1 分 K）
        m = now_m - 1
        key = (sd, "D", m)
        if m >= 5 and m in b1 and key not in seen:
            seen.add(key)
            d = b1[m]
            earlier = [b1[x] for x in b1 if 1 <= x < m]         # 09:00 那一分鐘是集合競價的量，不算
            every = [b1[x] for x in b1 if x < m]
            if len(earlier) >= 5:
                avg = sum(x[5] for x in earlier) / len(earlier)
                tick = I.tick_size(d[4])
                rng = d[2] - d[3]
                pre_ext = pick(x[2] if buy else x[3] for x in every)
                newext = sg * ((d[2] if buy else d[3]) - pre_ext) > 0
                if d[5] > max(x[5] for x in earlier) and d[5] >= D_VOL * avg and newext and rng >= D_RANGE_TK * tick - 1e-9:
                    shadow = (d[2] - max(d[1], d[4])) if buy else (min(d[1], d[4]) - d[3])
                    black = sg * (d[1] - d[4]) >= D_BLACK_TK * tick - 1e-9
                    if shadow >= D_SHADOW * rng - 1e-9 or black:
                        shape = f"留了 {shadow / rng * 100:.0f}% 的{'上' if buy else '下'}影線" if shadow >= D_SHADOW * rng - 1e-9 else f"收{'黑' if buy else '紅'}"
                        add(sd, "D", midnight + (m + 541) * 60, f"{d[0]} 這根 1 分 K 成交 {d[5]} 張，是今天最大量（之前平均的 {d[5] / avg:.1f} 倍），"
                                                               f"{'創今高' if buy else '破今低'} {_fp(d[2] if buy else d[3])} 但{shape}，收 {_fp(d[4])}")
        # C 量縮價滯（5 分 K）
        k = now_m // 5 - 1
        key = (sd, "C", k)
        if k >= 3 and key not in seen:
            seen.add(key)
            b5 = {}
            for m_, b in sorted(b1.items()):
                kk = m_ // 5
                if kk > k:
                    break
                d = b5.get(kk)
                if d is None:
                    b5[kk] = [b[1], b[2], b[3], b[4], b[5]]
                else:
                    d[1] = max(d[1], b[2]); d[2] = min(d[2], b[3]); d[3] = b[4]; d[4] += b[5]
            earlier = [b5[x] for x in b5 if x < k - 1]
            if k in b5 and k - 1 in b5 and len(earlier) >= 2:
                pv, cu = b5[k - 1], b5[k]
                tick = I.tick_size(pv[3])
                body = sg * (pv[3] - pv[0])
                pre_ext = pick(x[1] if buy else x[2] for x in earlier)
                med = sorted(x[4] for x in earlier)[len(earlier) // 2]
                cb = sg * (cu[3] - cu[0])
                if (body >= max(C_BODY_TK * tick, pv[0] * C_BODY_PCT / 100) - 1e-9 and sg * ((pv[1] if buy else pv[2]) - pre_ext) > 0
                        and pv[4] >= C_VOL * med and -tick - 1e-9 <= cb <= C_STALL * body + 1e-9 and cu[4] <= C_SHRINK * pv[4]):
                    add(sd, "C", midnight + (540 + (k + 1) * 5) * 60,
                        f"前一根 5 分 K {'漲' if buy else '跌'} {abs(pv[3] - pv[0]):g} 元、{pv[4]} 張並{'創今高' if buy else '破今低'}；"
                        f"這一根只{'漲' if cb >= 0 else '回'} {abs(cu[3] - cu[0]):g} 元、量縮到 {cu[4]} 張（{cu[4] / pv[4] * 100:.0f}%）")
    return out


def alert_text(s):
    lab = "買盤" if s["side"] == "buy" else "賣盤"
    tail = "沒有歷史五檔，還沒回測" if s["key"] in UNTESTED else "回測沒有比對照組更常反轉"
    return f"👁 {lab}竭盡跡象·{s['name']}（{CAT_LABEL[s['cat']]}）：{s['text']}。觀察用、不計分（{tail}）"


def summary(signs, now_ts):
    """最近 TTL 秒的跡象 → {"buy": {"n": 三個畫面亮了幾個, "book": 跡象 or None, "tick": ..., "bar": ...}, "sell": {...}}。"""
    out = {}
    for sd in ("buy", "sell"):
        d = {"book": None, "tick": None, "bar": None}
        for s in signs:
            if s["side"] == sd and now_ts - s["ts"] <= TTL:
                d[s["cat"]] = {k: s[k] for k in ("key", "name", "time", "text")}     # 同一個畫面留最新的
        d["n"] = sum(1 for k in ("book", "tick", "bar") if d[k])
        out[sd] = d
    return out
