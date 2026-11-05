"""
多空勝率模型：把盤前籌碼/技術面與盤中即時強弱轉成「偏多/偏空分數」，再映射成勝率與進出場價位。
每個因子都回傳 (名稱, 分數, 說明)，分數正 = 偏多、負 = 偏空，絕對值約 0.2~0.8。
這是經驗規則模型，不是回測最佳化；重點是把判斷流程固定下來、每 3 分鐘重算。
"""
import datetime as dt
import indicators as I


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


# ---------------------------------------------------------------- 盤前因子
def daily_factors(d):
    F, warns = [], []
    t = d.get("tech") or {}
    c = t.get("close")
    vol = t.get("vol") or 1
    vma5 = t.get("vol_ma5") or vol

    if d.get("flags"):
        for fl in d["flags"]:
            if "處置" in fl:
                warns.append(f"{fl}：分盤撮合，不列入當沖")
            else:
                warns.append(fl)

    # 法人 3 日
    s = d.get("insti_sum") or {}
    fg, tr = s.get("foreign", 0), s.get("trust", 0)
    fg_pct = fg / (vma5 * 3) * 100
    tr_pct = tr / (vma5 * 3) * 100
    if tr_pct > 3:
        F.append(("投信連買", 0.8, f"投信3日 +{tr} 張 ({tr_pct:.1f}% 量)，作帳/護盤風險，空單勝率打折"))
    elif tr_pct < -3:
        F.append(("投信連賣", -0.4, f"投信3日 {tr} 張"))
    if fg_pct > 8:
        F.append(("外資連買", 0.5, f"外資3日 +{fg} 張 ({fg_pct:.1f}% 量)"))
    elif fg_pct < -8:
        F.append(("外資連賣", -0.5, f"外資3日 {fg} 張 ({fg_pct:.1f}% 量)"))
    # 前日法人 vs 股價：法人買但跌 = 承接；法人賣但漲 = 出貨
    insti = d.get("insti") or {}
    last_key = max(insti.keys()) if insti else None
    if last_key and t.get("chg_pct") is not None:
        li = insti[last_key]
        if li.get("foreign", 0) + li.get("trust", 0) > vma5 * 0.05 and t["chg_pct"] < -2:
            F.append(("跌勢法人承接", 0.3, f"前日跌 {t['chg_pct']:.1f}% 但外資+投信 +{li['foreign']+li['trust']} 張"))
        if li.get("foreign", 0) + li.get("trust", 0) < -vma5 * 0.05 and t["chg_pct"] > 2:
            F.append(("漲勢法人出貨", -0.4, f"前日漲 {t['chg_pct']:.1f}% 但外資+投信 {li['foreign']+li['trust']} 張"))

    # 隔日沖庫存
    b = (d.get("brokers") or {}).get("day") or {}
    dtp = b.get("daytrade_buy_pct") or 0
    if dtp >= 5:
        F.append(("隔日沖庫存重", -0.7, f"隔日沖分點昨買 {b['daytrade_buy']} 張 = {dtp:.1f}% 量 ({','.join(b.get('daytrade_names', []))})，今日開高必倒"))
    elif dtp >= 2:
        F.append(("隔日沖庫存", -0.3, f"隔日沖分點昨買 {b['daytrade_buy']} 張 ({dtp:.1f}% 量)"))
    # 外資主力分點 4 日
    r4 = (d.get("brokers") or {}).get("range4") or {}
    fnp = r4.get("foreign_net_pct") or 0
    if fnp > 6:
        F.append(("外資分點吸貨", 0.3, f"4日外資券商淨買 {r4['foreign_net']:+} 張 ({fnp:.1f}% 量)，下方有人接"))
    elif fnp < -6:
        F.append(("外資分點出貨", -0.3, f"4日外資券商淨賣 {r4['foreign_net']:+} 張 ({fnp:.1f}% 量)"))
    # 前五買賣集中
    if b.get("top5_sell_pct", 0) > 25:
        F.append(("賣方集中", -0.2, f"前五賣超佔量 {b['top5_sell_pct']:.0f}%，主力單邊出"))
    if b.get("top5_buy_pct", 0) > 25:
        F.append(("買方集中", 0.2, f"前五買超佔量 {b['top5_buy_pct']:.0f}%"))

    # 常勝主力（近 20 日在此股隔日勝率 ≥65% 的分點）昨天的動作與庫存
    w = d.get("winners") or {}
    if w.get("n_scored"):
        wl = w.get("winners_last_net_pct") or 0
        if wl >= 1.5:
            F.append(("常勝主力進場", 0.5, f"常勝分點昨淨買 {w['winners_last_net']:+} 張 ({wl:+.1f}% 量)：" + "、".join(s['broker'] for s in w['winners'][:3])))
        elif wl <= -1.5:
            F.append(("常勝主力出場", -0.5, f"常勝分點昨淨賣 {w['winners_last_net']:+} 張 ({wl:+.1f}% 量)：" + "、".join(s['broker'] for s in w['winners'][:3])))
        inv = w.get("winners_inventory_pct") or 0
        if inv >= 3:
            F.append(("常勝主力持有庫存", 0.2, f"常勝分點 20 日累積 {w['winners_inventory']:+} 張 ({inv:+.1f}% 量)，有貨要拉"))
        elif inv <= -3:
            F.append(("常勝主力已出清", -0.2, f"常勝分點 20 日累積 {w['winners_inventory']:+} 張 ({inv:+.1f}% 量)"))
        ll = w.get("losers_last_net_pct") or 0
        if ll >= 2:
            F.append(("常敗分點追買", -0.2, f"反指標：常敗分點昨淨買 {w['losers_last_net']:+} 張 ({ll:+.1f}% 量)"))
        elif ll <= -2:
            F.append(("常敗分點殺出", 0.2, f"反指標：常敗分點昨淨賣 {w['losers_last_net']:+} 張 ({ll:+.1f}% 量)"))

    # 信用
    m = d.get("margin")
    if m:
        ratio = (m["short_now"] / m["fin_now"] * 100) if m.get("fin_now") else 0
        if ratio > 30:
            F.append(("券資比高", 0.4, f"券資比 {ratio:.0f}%，軋空風險，空單要快進快出"))
        if m["fin_chg"] > vol * 0.1 and (t.get("chg_pct") or 0) < 0:
            F.append(("散戶逆勢加資", -0.3, f"跌勢中融資 +{m['fin_chg']} 張，散戶接刀"))
        elif m["fin_chg"] < -vol * 0.1:
            F.append(("融資退場", 0.2, f"融資 {m['fin_chg']} 張，浮額減少"))
    sbl = d.get("sbl")
    if sbl:
        if sbl["sbl_sell"] > vol * 0.03:
            F.append(("借券放空增", -0.3, f"前日借券賣出 {sbl['sbl_sell']} 張 ({sbl['sbl_sell']/vol*100:.1f}% 量)"))
        elif sbl["sbl_return"] > sbl["sbl_sell"] * 3 and sbl["sbl_return"] > vol * 0.05:
            F.append(("借券回補", 0.2, f"還券 {sbl['sbl_return']} > 借券賣出 {sbl['sbl_sell']}，空方在回補"))

    # 技術面
    if c and t.get("ma5") and t.get("ma10") and t.get("ma20"):
        if c < t["ma5"] < t["ma10"] < t["ma20"]:
            F.append(("空頭排列", -0.5, f"收 {c} < MA5 {t['ma5']:.1f} < MA10 {t['ma10']:.1f} < MA20 {t['ma20']:.1f}，順勢空"))
        elif c > t["ma5"] > t["ma10"] > t["ma20"]:
            F.append(("多頭排列", 0.5, f"收 {c} > MA5 > MA10 > MA20，順勢多"))
        elif c < t["ma5"] and c < t["ma20"]:
            F.append(("均線下", -0.25, f"收 {c} 在 MA5/MA20 之下"))
        elif c > t["ma5"] and c > t["ma20"]:
            F.append(("均線上", 0.25, f"收 {c} 在 MA5/MA20 之上"))
    r = t.get("rsi14")
    if r is not None:
        if r < 25:
            F.append(("RSI超賣", 0.35, f"RSI {r:.0f}，隨時技術反彈，空單勝率打折"))
        elif r > 75:
            F.append(("RSI超買", -0.35, f"RSI {r:.0f}"))
    if c and t.get("bb_dn") and c < t["bb_dn"]:
        F.append(("跌破布林下軌", 0.3, f"收 {c} < 下軌 {t['bb_dn']:.1f}，均值回歸風險"))
    if c and t.get("bb_up") and c > t["bb_up"]:
        F.append(("突破布林上軌", -0.3, f"收 {c} > 上軌 {t['bb_up']:.1f}"))
    st = t.get("streak") or 0
    if st <= -4:
        F.append(("連跌過久", 0.25, f"連跌 {abs(st)} 天，反彈機率升"))
    if st >= 4:
        F.append(("連漲過久", -0.25, f"連漲 {st} 天"))
    if (t.get("ret20") or 0) < -20:
        F.append(("月跌幅深", 0.2, f"20日 {t['ret20']:.0f}%，空方獲利回補壓力"))

    # 大盤 / 夜盤 / 美股
    mc = d.get("market_ctx") or {}
    tx = mc.get("tx_night") or {}
    if tx.get("chg_pct") is not None:
        p = tx["chg_pct"]
        if p < -1.5:
            F.append(("夜盤重挫", -0.4, f"台指夜盤 {p:+.2f}%，開低壓力，但留意開低拉回"))
        elif p > 1:
            F.append(("夜盤大漲", 0.3, f"台指夜盤 {p:+.2f}%"))
    sox = (mc.get("us") or {}).get("SOX", {}).get("pct")
    if sox is not None:
        if sox < -3:
            F.append(("費半重挫", -0.3, f"SOX {sox:+.1f}%"))
        elif sox > 2:
            F.append(("費半大漲", 0.2, f"SOX {sox:+.1f}%"))
    br = mc.get("breadth") or {}
    if br.get("down") and br.get("up") is not None and br["down"] > br["up"] * 6:
        F.append(("前日全面殺盤", 0.15, f"漲 {br['up']} / 跌 {br['down']}，超跌反彈日常見"))
    return F, warns


# ---------------------------------------------------------------- 盤中因子
PHASES = [
    ("pre",   "00:00", "08:30", "盤前",  "", "看夜盤、美股與盤前籌碼，定今天的主劇本"),
    ("trial", "08:30", "09:00", "試撮",  "08:30–09:00", "掛單可撤，價量可能是假的；只看方向不下結論"),
    ("early", "09:00", "10:30", "早盤",  "09:00–10:30", "隔日沖倒貨與主力表態多在此段；首 10 分鐘不追單，等 VWAP 與分流方向一致再進"),
    ("mid",   "10:30", "12:00", "中盤",  "10:30–12:00", "量能通常縮、假突破多，只做 5 分 K 確認的突破/跌破單，目標縮短"),
    ("late",  "12:00", "13:30", "尾盤",  "12:00–13:30", "法人作價與當沖回補主導，順 VWAP 方向做；13:20 前當沖部位處理完，13:25 後試撮不開新單"),
    ("closed","13:30", "24:00", "已收盤", "", "復盤：review.py / replay.py"),
]


def phase_of(now):
    """回 {key, label, range, hint}。"""
    hhmm = now.strftime("%H:%M")
    for key, a, b, label, rng, hint in PHASES:
        if a <= hhmm < b:
            return {"key": key, "label": label, "range": rng, "hint": hint}
    return {"key": "closed", "label": "已收盤", "range": "", "hint": ""}


def _flow_dynamics(flow, now):
    """
    主力分流的「動態」：淨額的水位本身校準過沒預測力（IC≈-0.03），但水位在幾分鐘內的劇變有意義。
    flow["hist"]: 本程式歷次取樣 [[epoch_sec, big_net, small_net, price], ...]（monitor/replay 附上）。
    11/03 建榮復盤：主力淨賣 09:20 -483 → 09:22 -273（2 分鐘回補 210 張）→ 價 89.2→90.85→93.5；
    09:25 -13（幾乎全回補）→ 09:28 -221（再倒 200 張）→ 價 93.5→92.35。前者是空單該停損的硬訊號，
    後者是「拉高、回補鎖單、再倒」的出貨型態。
    """
    F, warns = [], []
    hist = flow.get("hist") or []
    if len(hist) < 2 or not flow.get("volume"):
        return F, warns
    vol = flow["volume"]
    min_abs = max(20, 0.015 * vol)             # 至少 1.5% 量才算「劇變」，小型股下限 20 張
    t_now, big_now = hist[-1][0], hist[-1][1]

    # (1) 3 分鐘內的收斂 / 翻向
    ref = None
    for hrec in reversed(hist[:-1]):
        if t_now - hrec[0] <= 200:
            ref = hrec
        else:
            break
    if ref is not None:
        delta = big_now - ref[1]
        mins = max(1, round((t_now - ref[0]) / 60))
        if ref[1] < 0 and delta >= max(min_abs, 0.4 * abs(ref[1])):
            F.append(("主力急回補", 0.5, f"主力淨賣 {mins} 分鐘內由 {ref[1]:+} 收斂到 {big_now:+}（回補 {delta:+} 張）：倒貨的人自己轉身買，空單前提失效"))
            warns.append(f"⚠ 主力 {mins} 分鐘內回補 {delta} 張（{ref[1]:+} → {big_now:+}），空單停損要執行、不要加碼攤平；多單勿追，先看是回補鎖單還是真轉向")
        elif ref[1] > 0 and -delta >= max(min_abs, 0.4 * abs(ref[1])):
            F.append(("主力急出", -0.5, f"主力淨買 {mins} 分鐘內由 {ref[1]:+} 掉到 {big_now:+}（出掉 {-delta} 張）：拉的人在跑"))
            warns.append(f"⚠ 主力 {mins} 分鐘內出掉 {-delta} 張（{ref[1]:+} → {big_now:+}），多單先減半或停利")
        # 同向加速：已經淨賣還在幾分鐘內再倒一大批（或已淨買再大買）。11/05 晶豪科 09:03 -181 → 09:06 -352（3 分鐘 171 張、當時量的 13%），
        # 價 224 → 219；上面兩條只抓「翻向」，這種加速原本漏掉
        elif ref[1] <= 0 and -delta >= max(2 * min_abs, 0.5 * abs(ref[1])):
            F.append(("主力加速出貨", -0.4, f"主力淨賣 {mins} 分鐘內由 {ref[1]:+} 擴大到 {big_now:+}（再倒 {-delta} 張）：賣壓在加速，不是尾聲"))
            warns.append(f"⚠ 主力 {mins} 分鐘內再倒 {-delta} 張（{ref[1]:+} → {big_now:+}），多單不接、空單順勢，反彈不過前高加碼")
        elif ref[1] >= 0 and delta >= max(2 * min_abs, 0.5 * abs(ref[1])):
            F.append(("主力加速吃貨", 0.4, f"主力淨買 {mins} 分鐘內由 {ref[1]:+} 擴大到 {big_now:+}（再買 {delta} 張）：買盤在加速"))
            warns.append(f"⚠ 主力 {mins} 分鐘內再買 {delta} 張（{ref[1]:+} → {big_now:+}），空單不追、多單順勢")

    # (2) 10 分鐘內「回補 → 再倒」且價格從高點回落 ≥2%：拉高出貨型態
    win = [hrec for hrec in hist if t_now - hrec[0] <= 600]
    if len(win) >= 4:
        i_tr = min(range(len(win)), key=lambda i: win[i][1])              # 淨賣最深點
        if i_tr < len(win) - 2:
            after = win[i_tr:]
            i_pk = max(range(len(after)), key=lambda i: after[i][1])      # 之後的回補高點
            covered = after[i_pk][1] - after[0][1]
            resold = after[i_pk][1] - big_now
            px_hi = max((r[3] or 0) for r in after[: i_pk + 1])
            px_now = hist[-1][3] or 0
            drop = (1 - px_now / px_hi) * 100 if px_hi else 0
            if covered >= min_abs and resold >= max(min_abs, 0.4 * covered) and drop >= 2 and i_pk < len(after) - 1:
                F.append(("主力拉高出貨", -0.5, f"主力先回補 {covered:+.0f} 張把價頂到 {px_hi}，再倒 {resold:.0f} 張，價已回落 {drop:.1f}%：拉高、鎖單、再倒的出貨型態，散戶在高點接貨"))
                warns.append(f"⚠ 主力拉高出貨：回補 {covered:.0f} 張後再倒 {resold:.0f} 張，價自 {px_hi} 回落 {drop:.1f}%。反彈不過高點是空點；多單不接")
    return F, warns


def _bars_from_samples(samples):
    """沒有 Shioaji 分 K 時，用 MIS 10 秒取樣湊 1 分 K：[[HH:MM, o, h, l, c, vol, amt], ...]。"""
    bars = {}
    for t, px, cv in (samples or []):
        if px is None:
            continue
        k = t.strftime("%H:%M")
        b = bars.get(k)
        if b is None:
            bars[k] = [px, px, px, px, 0, 0.0]
        else:
            b[1] = max(b[1], px); b[2] = min(b[2], px); b[3] = px
    return [[k] + v for k, v in sorted(bars.items())]


def _momentum(bars, p, h, l, info):
    """
    反彈動能 / 殺盤動能：連續 ≥4 根 1 分 K 同向收盤、幅度 ≥1.2%，而且是從日低（日高）1.5% 內出發。
    11/05 晶豪科復盤：09:22 213.5 起連 5 根紅 K 彈到 218.5，模型空方仍 77%，使用者在 218 空、三根後被拉到 220.5。
    這種時候「反彈至 X 不過」的計畫要等 1 分 K 翻黑確認；紅 K 一斷訊號自動消失，不需要計時器。
    """
    F, warns = [], []
    c = [b[4] for b in bars if b[4] is not None]
    if len(c) < 5:
        return F, warns
    up = dn = 0
    for i in range(len(c) - 1, 0, -1):
        if c[i] > c[i - 1] and dn == 0:
            up += 1
        elif c[i] < c[i - 1] and up == 0:
            dn += 1
        else:
            break
    if up >= 4:
        start = c[-1 - up]
        rise = (c[-1] / start - 1) * 100
        if rise >= 1.2 and (not l or start <= l * 1.015):
            info["momentum"] = {"dir": "up", "bars": up, "pct": rise}
            F.append(("反彈動能", 0.4, f"從 {start} 連 {up} 根 1 分 K 收紅、彈 {rise:.1f}%，空單別在反彈途中進，等 1 分 K 翻黑確認"))
            warns.append(f"⚠ 反彈動能：連 {up} 根紅 K（{start}→{c[-1]}，+{rise:.1f}%），「反彈不過」的空點要等 1 分 K 翻黑再掛，不要碰到壓力就空")
    elif dn >= 4:
        start = c[-1 - dn]
        fall = (1 - c[-1] / start) * 100
        if fall >= 1.2 and (not h or start >= h * 0.985):
            info["momentum"] = {"dir": "down", "bars": dn, "pct": fall}
            F.append(("殺盤動能", -0.4, f"從 {start} 連 {dn} 根 1 分 K 收黑、跌 {fall:.1f}%，多單別在殺盤途中接，等 1 分 K 翻紅確認"))
            warns.append(f"⚠ 殺盤動能：連 {dn} 根黑 K（{start}→{c[-1]}，-{fall:.1f}%），「回測守住」的多點要等 1 分 K 翻紅再掛")
    return F, warns


def _series_change(series, now, back_sec=600, tol_sec=240):
    """[[HH:MM, close] 或 [HH:MM, o,h,l,c,...]] 最後一筆 vs 約 back_sec 秒前那筆的變化 %；資料不夠回 None。"""
    pts = []
    for b in series or []:
        try:
            hh, mm = b[0].split(":")
            ts = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0).timestamp()
            close = b[1] if len(b) == 2 else b[4]
            if close:
                pts.append((ts, float(close)))
        except Exception:
            continue
    if len(pts) < 3:
        return None
    t_last, c_last = pts[-1]
    target = t_last - back_sec
    ref = min(pts[:-1], key=lambda x: abs(x[0] - target))
    if abs(ref[0] - target) > tol_sec:
        return None
    return (c_last / ref[1] - 1) * 100


def _trend_factors(ctx, mkt_code, pc_now, now, info):
    """
    大盤與族群的「趨勢」（10 分鐘變化），補原本只看水位（今日漲跌 %）的盲點：
    大盤 -3% 但最近 10 分鐘 V 轉 +0.6%，個股的「弱於大盤」還成立，可是空單進場時機已經不對。
    """
    F, warns = [], []
    ctx = ctx or {}
    ixs = (ctx.get("idx_series") or {}).get(mkt_code) or (ctx.get("idx_series") or {}).get("t00")
    ch = _series_change(ixs, now)
    if ch is not None:
        info["idx_trend_10m"] = ch
        nm = "櫃買" if mkt_code == "o00" else "加權"
        if ch >= 0.6:
            F.append(("大盤急拉", 0.35, f"{nm} 10 分鐘 {ch:+.2f}%，指數在 V 轉，空單進場時機不對"))
        elif ch >= 0.3:
            F.append(("大盤轉強", 0.2, f"{nm} 10 分鐘 {ch:+.2f}%"))
        elif ch <= -0.6:
            F.append(("大盤急殺", -0.35, f"{nm} 10 分鐘 {ch:+.2f}%，系統性賣壓，多單別接刀"))
        elif ch <= -0.3:
            F.append(("大盤轉弱", -0.2, f"{nm} 10 分鐘 {ch:+.2f}%"))
    ph = ctx.get("peer_hist") or {}
    t_now = now.timestamp()
    deltas, names = [], []
    for chg, code, name in pc_now:
        hist = ph.get(code) or []
        past = [r for r in hist if t_now - r[0] >= 480]
        if not past:
            continue
        prev = past[-1]
        if t_now - prev[0] > 900:
            continue
        deltas.append(chg - prev[1]); names.append(name or code)
    if deltas:
        gt = sum(deltas) / len(deltas)
        info["group_trend_10m"] = gt
        if gt >= 1.0:
            F.append(("族群急拉", 0.45, f"族群 10 分鐘平均 {gt:+.2f}%（{len(deltas)} 檔），資金正在回流這個族群"))
        elif gt >= 0.5:
            F.append(("族群轉強", 0.3, f"族群 10 分鐘平均 {gt:+.2f}%"))
        elif gt <= -1.0:
            F.append(("族群急殺", -0.45, f"族群 10 分鐘平均 {gt:+.2f}%（{len(deltas)} 檔），整個族群在被倒"))
        elif gt <= -0.5:
            F.append(("族群轉弱", -0.3, f"族群 10 分鐘平均 {gt:+.2f}%"))
    return F, warns


def intraday_factors(d, q, idx, peers_q, flow, samples, now, ctx=None):
    """
    q: 個股 MIS quote；idx: {t00:quote, o00:quote}；peers_q: {code: quote}；
    flow: shioaji 大單分流（可 None）；samples: 本程式歷次取樣 [(time, price, cumvol)]
    ctx: monitor 另外提供的脈絡（可 None）：bars 個股 1 分 K、idx_series 指數分時、peer_hist 族群歷史、alerts 即時警示 [(epoch, text)]
    """
    ctx = ctx or {}
    F, warns, info = [], [], {}
    t = d.get("tech") or {}
    y = q.get("y") or t.get("close")
    p = q.get("ref")
    o, h, l = q.get("o"), q.get("h"), q.get("l")
    if not (p and y):
        return F, ["尚無成交/參考價"], info
    chg = (p / y - 1) * 100
    info["chg_pct"] = chg
    mkt_code = "t00" if d["market"] == "tse" else "o00"
    iq = idx.get(mkt_code) or idx.get("t00") or {}
    idx_chg = ((iq.get("ref") or 0) / iq["y"] - 1) * 100 if iq.get("y") and iq.get("ref") else None
    info["idx_chg_pct"] = idx_chg

    hhmm = now.strftime("%H:%M")
    pre_open = hhmm < "09:00"
    if pre_open:
        warns.append("08:30~09:00 為試撮，掛單可撤，價量都可能是假的；只看方向不下結論")

    # 跳空
    if o and y:
        gap = (o / y - 1) * 100
        info["gap_pct"] = gap
        dtp = ((d.get("brokers") or {}).get("day") or {}).get("daytrade_buy_pct") or 0
        if gap >= 3 and dtp >= 2:
            F.append(("開高有庫存", -0.6, f"跳空 {gap:+.1f}% 且隔日沖庫存 {dtp:.1f}%，拉高倒貨劇本"))
        elif gap >= 3:
            F.append(("跳空開高", -0.2, f"跳空 {gap:+.1f}%，追多風險高、反彈不過開盤價可空"))
        elif gap <= -3 and p < y:
            F.append(("跳空開低", 0.4, f"跳空 {gap:+.1f}%，開低追空勝率低（開低拉回常見），等反彈再空"))
        elif gap <= -1.5 and p >= y:
            F.append(("開低拉回翻紅", 0.5, f"開低 {gap:+.1f}% 後站回昨收 {y}，空方被軋、多方控盤"))
        elif gap <= -1.5:
            F.append(("開低", 0.15, f"跳空 {gap:+.1f}%"))

    # 族群先掃一遍：有沒有成員接近漲停（≥6%）。11/03 建榮復盤：台玻 13 萬張鎖漲停，族群資金把建榮
    # 隔日沖 30% 的庫存全接走還軋到漲停；族群「均值」-0.3% 完全看不出來，均值被下游 CCL 的跌幅稀釋
    pc = []
    for code, pq in (peers_q or {}).items():
        if pq.get("ref") and pq.get("y"):
            pc.append(((pq["ref"] / pq["y"] - 1) * 100, code, pq.get("name")))
    leader = max(pc) if pc else None
    leader_strong = bool(leader and leader[0] >= 6)

    # 隔日沖庫存 + 盤中急拉（不一定是跳空）：庫存 ≥5% 量且盤中漲幅 ≥5%，就是倒貨點附近
    dtp_inv = ((d.get("brokers") or {}).get("day") or {}).get("daytrade_buy_pct") or 0
    inv_scale = 0.5 if leader_strong else 1.0
    inv_note = f"；但族群 {leader[2] or leader[1]} {leader[0]:+.1f}% 接近漲停，族群資金可能接走這批貨，權重折半" if leader_strong else ""
    if dtp_inv >= 5 and chg >= 5 and not pre_open:
        F.append(("庫存倒貨點", -0.6 * inv_scale, f"隔日沖分點昨買 {dtp_inv:.0f}% 量，盤中已拉 {chg:+.1f}%：他們的獲利了結區，反彈不過高點就是空點（但首 10 分鐘先不追）{inv_note}"))
    elif dtp_inv >= 5 and chg >= 2.5 and not pre_open:
        F.append(("庫存逼近倒貨區", -0.3 * inv_scale, f"隔日沖庫存 {dtp_inv:.0f}% 量，盤中 {chg:+.1f}%，接近他們的出貨區{inv_note}"))

    # 衝高回落（墓碑）：今高離昨收 ≥5%、現價已從今高回落 ≥3%。11/03 建榮 93.5→90.9、驊宏資 44.95→42.3：
    # 高點接貨的散戶全套牢，上方每一檔都是解套賣壓，此時「強於大盤、VWAP 之上」的多方分數是慣性假象
    # MIS 的今高可能延遲，所以用本程式看過的最高價補正（注意 11/03 建榮「96.65」其實是延緩撮合試算價，已在 src_mis 夾回高低）
    h_eff = max([x for x in [h, p] + [s[1] for s in (samples or [])] if x] or [0])
    info["high_seen"] = h_eff
    if h_eff and y and p and not pre_open:
        hi_chg = (h_eff / y - 1) * 100
        pull = (1 - p / h_eff) * 100
        info["pullback_from_high_pct"] = pull
        if hi_chg >= 5 and pull >= 3 and chg > -1:
            F.append(("衝高回落", -0.4, f"今高 {h_eff} ({hi_chg:+.1f}%) 後已回落 {pull:.1f}%：高點套牢賣壓在上，追多等於接最後一棒；反彈不過前高再空"))

    # 相對強弱
    if idx_chg is not None:
        rs = chg - idx_chg
        info["rs_vs_index"] = rs
        if rs > 1.5:
            F.append(("強於大盤", 0.6, f"個股 {chg:+.2f}% vs 大盤 {idx_chg:+.2f}%（+{rs:.1f}）"))
        elif rs < -1.5:
            F.append(("弱於大盤", -0.6, f"個股 {chg:+.2f}% vs 大盤 {idx_chg:+.2f}%（{rs:.1f}）"))
        elif rs > 0.5:
            F.append(("略強於大盤", 0.2, f"相對強弱 {rs:+.1f}"))
        elif rs < -0.5:
            F.append(("略弱於大盤", -0.2, f"相對強弱 {rs:+.1f}"))

    # 族群
    if pc:
        avg = sum(x[0] for x in pc) / len(pc)
        ups = sum(1 for x in pc if x[0] > 0)
        info["group_avg"] = avg; info["group_up"] = ups; info["group_n"] = len(pc)
        info["group_detail"] = sorted(pc, reverse=True)
        spread = leader[0] - min(pc)[0]
        if leader_strong:
            info["group_leader"] = list(leader)
            F.append(("族群有領漲股", 0.4, f"{leader[2] or leader[1]} {leader[0]:+.1f}% 接近漲停，族群資金輪動中，同業低價股有補漲買盤；"
                                          f"族群均 {avg:+.2f}% 被其他成員稀釋，別只看均值。空單要防族群資金接貨軋空"))
        if spread >= 8 and len(pc) >= 3:
            F.append(("族群分歧", 0.0, f"族群最強 {leader[2] or leader[1]} {leader[0]:+.1f}% vs 最弱 {min(pc)[2] or min(pc)[1]} {min(pc)[0]:+.1f}%："
                                      f"上下游走反向（如玻纖紗漲價：上游漲、CCL 跌），均值無意義，看個股站哪一邊"))
        diff = chg - avg
        if diff < -1.5:
            F.append(("族群最弱", -0.5, f"個股 {chg:+.2f}% vs 族群均 {avg:+.2f}%（{ups}/{len(pc)} 檔漲），弱者恆弱"))
        elif diff > 1.5:
            F.append(("族群最強", 0.5, f"個股 {chg:+.2f}% vs 族群均 {avg:+.2f}%（{ups}/{len(pc)} 檔漲）"))
        if avg < -1.5 and ups == 0:
            F.append(("族群全倒", -0.3, f"族群均 {avg:+.2f}%，無一檔漲"))
        elif avg > 1.5 and ups == len(pc):
            F.append(("族群全紅", 0.3, f"族群均 {avg:+.2f}%，全數上漲"))

    # 日內位置 / 開盤價
    rp = I.range_position(p, h, l)
    info["range_pos"] = rp
    if rp is not None and h != l:
        if rp > 0.8:
            F.append(("日內高檔", 0.3, f"價在今日區間 {rp*100:.0f}% 位置（高 {h} 低 {l}）"))
        elif rp < 0.2:
            F.append(("日內低檔", -0.3, f"價在今日區間 {rp*100:.0f}% 位置（高 {h} 低 {l}）"))
    if o:
        if p > o * 1.005:
            F.append(("站上開盤價", 0.2, f"價 {p} > 開 {o}"))
        elif p < o * 0.995:
            F.append(("跌破開盤價", -0.2, f"價 {p} < 開 {o}"))

    # VWAP（有 shioaji 用成交分流算，否則用取樣近似）
    vwap = None
    if flow and flow.get("vwap"):
        vwap = flow["vwap"]
    elif len(samples) >= 2:
        num = den = 0.0
        for i in range(1, len(samples)):
            dv = samples[i][2] - samples[i - 1][2]
            if dv > 0:
                num += samples[i][1] * dv; den += dv
        vwap = num / den if den else None
    info["vwap"] = vwap
    if vwap:
        if p > vwap * 1.003:
            F.append(("VWAP之上", 0.25, f"價 {p} > VWAP {vwap:.2f}，日內多方控盤"))
        elif p < vwap * 0.997:
            F.append(("VWAP之下", -0.25, f"價 {p} < VWAP {vwap:.2f}，日內空方控盤"))

    # 五檔委託不平衡（弱訊號，試撮期間不採）
    bids, asks = q.get("bids") or [], q.get("asks") or []
    bv = sum(v for _, v in bids); av = sum(v for _, v in asks)
    if bv + av > 0 and not pre_open:
        imb = (bv - av) / (bv + av)
        info["book_imb"] = imb; info["bid5"] = bv; info["ask5"] = av
        if imb > 0.35:
            F.append(("委買厚", 0.15, f"五檔買 {bv} / 賣 {av}（可能是假單，只作參考）"))
        elif imb < -0.35:
            F.append(("委賣厚", -0.15, f"五檔買 {bv} / 賣 {av}"))

    # 權證急漲 + 委買厚 同時出現（5 分鐘內）：11/05 晶豪科 09:07、09:22 兩次都在 3 分鐘內拉 2–3%。
    # 單看權證急漲只是提醒（校準：權證只跟當天同向），但加上五檔買盤變厚就是有人準備拉，空單先退一步
    t_now = now.timestamp()
    recent = [txt for ts, txt in (ctx.get("alerts") or []) if t_now - ts <= 300]
    w_surge = any(("認購權證" in txt and "急漲" in txt) for txt in recent)
    info["warrant_surge_5m"] = w_surge
    if w_surge and info.get("book_imb", 0) > 0.35 and not pre_open:
        F.append(("權證急漲+委買厚", 0.3, f"5 分鐘內認購權證急漲，且五檔買 {info.get('bid5')} / 賣 {info.get('ask5')}：散戶追價、避險買盤、掛單護盤同時出現，現股常在幾分鐘內急拉"))
        warns.append("⚠ 權證急漲 + 委買變厚：空單等 5 分鐘再進，已有空單把停損收緊")

    # 1 分 K 動能（反彈/殺盤途中不逆勢進場）
    bars = ctx.get("bars") or _bars_from_samples(samples)
    if not pre_open:
        mF, mW = _momentum(bars, p, h_eff or h, l, info)
        F += mF; warns += mW

    # 大盤 / 族群 10 分鐘趨勢
    if not pre_open:
        tF, tW = _trend_factors(ctx, mkt_code, pc, now, info)
        F += tF; warns += tW

    # 大單 / 散戶 / 藏單（shioaji）。門檻依金額與當日分布自動推算，見 src_shioaji.lot_thresholds
    # 2026-08-27~10-30 九檔 357 個樣本的校準（calibrate.py）：
    #   主力淨買% 對 09:20→09:50 報酬 IC ≈ -0.03（無預測力，各門檻皆然）→ 只給 ±0.15 當資訊
    #   散戶淨買% IC ≈ -0.11，散戶淨買 ≥15% 量時接下來 30 分鐘只有 15% 機率上漲、平均 -0.62% → 反指標 -0.6
    if flow and flow.get("volume"):
        bn, sn = flow["big_net_pct"], flow["small_net_pct"]
        info["big_net_pct"] = bn; info["small_net_pct"] = sn
        info["lot_thresholds"] = flow.get("thresholds")
        if bn > 8:
            F.append(("大單淨買", 0.15, f"主力淨買 {flow['big_net']:+} 張 ({bn:+.1f}% 量)；校準顯示早盤大單方向對 30 分鐘後報酬無預測力，僅供參考"))
        elif bn < -8:
            F.append(("大單淨賣", -0.15, f"主力淨賣 {flow['big_net']:+} 張 ({bn:+.1f}% 量)；同上，僅供參考"))
        if sn >= 15:
            F.append(("散戶瘋搶", -0.6, f"散戶淨買 {flow['small_net']:+} 張 ({sn:+.1f}% 量)：兩個月校準此情境 30 分鐘後上漲機率僅 15%、平均 -0.62%"))
        elif sn >= 5:
            F.append(("散戶追價", -0.3, f"散戶淨買 {sn:+.1f}% 量（校準：上漲機率 43%、平均 -0.27%）" + ("，且主力在賣" if bn < 0 else "")))
        elif sn <= -15:
            F.append(("散戶殺出", 0.15, f"散戶淨賣 {sn:+.1f}% 量（校準：上漲機率 44%，略優於基期）" + ("，主力在接" if bn > 0 else "")))
        # 規律小單（演算法切單）：張數像散戶、行為像主力
        hn = flow.get("hidden_net_pct") or 0
        ice = [i for i in (flow.get("iceberg") or []) if i.get("regular") or i["share_cnt"] >= 0.2]
        if ice:
            top = ice[0]
            desc = "、".join(f"{i['size']}張×{i['count']}筆{i['side']}" + (f"(每{i['avg_gap_s']:.0f}秒)" if i.get('avg_gap_s') else "") for i in ice[:2])
            info["iceberg"] = desc
            if hn > 5:
                F.append(("規律小單吃貨", 0.45, f"藏單淨買 {flow['hidden_net']:+} 張 ({hn:+.1f}% 量)：{desc}，主力分批進貨不想被看見"))
            elif hn < -5:
                F.append(("規律小單出貨", -0.45, f"藏單淨賣 {flow['hidden_net']:+} 張 ({hn:+.1f}% 量)：{desc}，主力分批倒貨"))
            else:
                F.append(("規律小單", 0.0, f"偵測到切單 {desc}，方向尚不明確"))
        fF, fW = _flow_dynamics(flow, now)
        F += fF; warns += fW
        io = flow["buy_vol"] / flow["sell_vol"] if flow.get("sell_vol") else None
        info["outer_inner"] = io
        if io and io > 1.4:
            F.append(("外盤強", 0.2, f"外盤/內盤 {io:.2f}"))
        elif io and io < 0.7:
            F.append(("內盤強", -0.2, f"外盤/內盤 {io:.2f}"))

    # 量能節奏：目前累積量 vs 前 5 日均量依時間比例
    v = q.get("v") or 0
    vma5 = t.get("vol_ma5")
    mins = max(1, (now.hour - 9) * 60 + now.minute) if hhmm >= "09:00" else 1
    frac = min(1.0, 0.25 + 0.75 * mins / 270)  # 開盤前 1/4 量通常偏重
    if vma5 and hhmm >= "09:03":
        pace = v / (vma5 * frac)
        info["vol_pace"] = pace
        if pace > 1.8:
            F.append(("量能爆增", 0.0, f"量比 {pace:.1f}（{v} 張 vs 均量進度 {vma5*frac:.0f}），方向訊號可信度升"))
        elif pace < 0.6:
            F.append(("量縮", 0.0, f"量比 {pace:.1f}，盤整機率高，兩邊勝率都打折"))

    # 時段規則（階段性策略，見 PHASES）
    ph = phase_of(now)
    info["phase"] = ph
    if "09:00" <= hhmm < "09:10":
        warns.append("開盤首 10 分鐘：位置、VWAP、相對強弱都還是雜訊，盤中因子只計一半，不追單")
    elif hhmm >= "13:20" and ph["key"] == "late":
        warns.append("尾盤最後 10 分鐘：13:25 起收盤試撮不能撤單，當沖部位現在處理")
    elif ph["key"] == "closed":
        warns.append("已收盤，以下僅供復盤")
    return F, warns, info


# ---------------------------------------------------------------- 彙整
def warrant_factors(d, q, wq, info, now):
    """
    權證因子。校準（calibrate_warrants.py，2026/08/27–10/30 九檔）：
      認購金額爆量、發行商避險淨買 → 與「同日」標的漲跌高度同向（IC +0.29 / +0.38；認購金額 ≥5日均 3 倍的當天 93% 收紅）
      但對「隔日」沒有預測力（IC +0.04 / 0.00）；前日避險淨買 ≥2% 量 → 隔日只有 33% 上漲、均 -0.54%（n=27）
      前日避險淨賣 ≤-2% → 隔日 43% 上漲、均 -1.07%（n=23，續跌）
    結論：權證是同日的順勢確認（散戶買認購 → 發行商買現股避險），不是隔日的領先指標；重的避險部位隔天是賣壓。
    """
    F = []
    w = d.get("warrants") or {}
    if not w:
        return F
    hp = w.get("hedge_pct")
    chg_prev = (d.get("tech") or {}).get("chg_pct") or 0
    has_warrants = bool(w.get("n_call") or w.get("n_put"))
    if hp is not None and has_warrants:
        if hp >= 2:
            F.append(("前日避險買盤重", -0.25, f"發行商避險淨買 {w['hedge_net']:+} 張 ({hp:+.1f}% 量)：校準此情境隔日僅 33% 上漲、均 -0.54%；權證散戶獲利了結時發行商反手賣現股"))
        elif hp <= -2:
            F.append(("前日避險賣壓重", -0.2, f"發行商避險淨賣 {w['hedge_net']:+} 張 ({hp:+.1f}% 量)：校準此情境隔日 43% 上漲、均 -1.07%，弱勢續跌多於反彈"))
    elif hp is not None and not has_warrants:
        # 沒有權證，T86「自營商(避險)」欄不是權證避險：多半是可轉債套利放空（持續性、對隔日方向資訊少）或 ETF 相關避險
        if hp <= -2:
            F.append(("可轉債或其他避險賣壓", -0.1, f"此標的無權證，自營商避險欄淨賣 {w['hedge_net']:+} 張 ({hp:+.1f}% 量)：多為可轉債套利放空，屬持續性賣壓、非權證訊號，權重小"))
        elif hp >= 2:
            F.append(("可轉債或其他避險買盤", 0.1, f"此標的無權證，自營商避險欄淨買 {w['hedge_net']:+} 張 ({hp:+.1f}% 量)：可能是套利部位回補，權重小"))
    surge = w.get("call_surge")
    if surge and surge >= 3:
        F.append(("前日認購爆量", 0.15, f"前日認購金額為 5 日均 {surge:.1f} 倍（標的 {chg_prev:+.1f}%）：校準隔日 69% 上漲、均 +0.85%，但樣本只有 13 個"))
    if wq and wq.get("n"):
        info["w_call_vol"] = wq["call_vol"]; info["w_put_vol"] = wq["put_vol"]
        base = w.get("call_vol") or 0
        hhmm = now.strftime("%H:%M")
        mins = max(1, (now.hour - 9) * 60 + now.minute) if hhmm >= "09:00" else 1
        frac = min(1.0, 0.25 + 0.75 * mins / 270)
        pace = (wq["call_vol"] / (base * frac)) if base else None
        info["w_call_pace"] = pace
        chg = info.get("chg_pct") or 0
        if pace and pace >= 2.5:
            if chg > 0.5:
                F.append(("認購爆量順勢", 0.25, f"認購成交 {wq['call_vol']} 張 = 昨日同進度 {pace:.1f} 倍，{wq['call_up']} 檔漲/{wq['call_down']} 檔跌，標的 {chg:+.1f}%：發行商避險買盤同向，同日順勢確認（不留倉，隔日無效）"))
            elif chg < -0.5:
                F.append(("認購爆量逆勢", 0.1, f"認購成交 {wq['call_vol']} 張 = 昨日同進度 {pace:.1f} 倍但標的 {chg:+.1f}%：散戶低接認購，避險買盤撐價但不保證反轉"))
        elif pace is not None and pace < 0.4 and chg > 2:
            F.append(("漲勢無權證跟單", -0.15, f"標的 {chg:+.1f}% 但認購量只有昨日同進度 {pace:.1f} 倍：散戶沒追、避險買盤也沒進，漲勢缺乏跟隨"))
        if wq["put_vol"] and wq["put_vol"] / max(1, wq["call_vol"]) > 0.5:
            F.append(("認售放大", -0.15, f"認售 {wq['put_vol']} 張 vs 認購 {wq['call_vol']} 張，發行商需放空現股避險"))
    return F


def evaluate(d, q, idx, peers_q, flow, samples, now=None, warrants=None, ctx=None):
    now = now or dt.datetime.now()
    DF, dwarn = daily_factors(d)
    IF, iwarn, info = intraday_factors(d, q, idx, peers_q, flow, samples, now, ctx)
    IF += warrant_factors(d, q, warrants, info, now)
    daily_bias = sum(f[1] for f in DF)
    intra_bias = sum(f[1] for f in IF)
    # 盤中訊號在開盤後權重較高；試撮期間盤中訊號打對折
    hhmm_ = now.strftime("%H:%M")
    w_intra = 0.5 if hhmm_ < "09:00" else (0.5 if hhmm_ < "09:10" else 1.0)  # 試撮與首 10 分鐘的盤中訊號只算一半
    bias = _clamp(daily_bias * 0.8 + intra_bias * w_intra, -3.5, 3.5)
    p_long = 50 + 11 * bias
    p_short = 50 - 11 * bias
    pace = info.get("vol_pace")
    chop = 0
    if pace is not None and pace < 0.6:
        chop = 6
    t = d.get("tech") or {}
    if t.get("atr_pct") and t["atr_pct"] < 2:
        chop += 4
    p_long = _clamp(p_long - chop, 15, 85)
    p_short = _clamp(p_short - chop, 15, 85)
    # 不可空的硬條件
    hard_no_short = any("處置" in w for w in dwarn)
    if any(f[0] == "投信連買" for f in DF):
        p_short = min(p_short, 42)
    if hard_no_short:
        p_short = 0

    levels = build_levels(d, q, info)
    plan = build_plan(d, q, info, levels, p_long, p_short, now)
    side = "空" if p_short > p_long else "多"
    edge = abs(p_short - p_long)
    if hard_no_short:
        side, verdict = "不做", "處置股，不列入當沖"
    elif edge < 8:
        verdict = f"兩邊勝率接近（多 {p_long:.0f}% / 空 {p_short:.0f}%），觀望或縮小部位"
    elif max(p_long, p_short) < 55:
        verdict = f"偏{side}但勝率不足 55%，等觸發再進"
    else:
        verdict = f"偏{side}，勝率 {max(p_long,p_short):.0f}%"
    return {
        "time": now.strftime("%H:%M:%S"), "code": d["code"], "name": d["name"], "price": q.get("ref"), "quote": q,
        "daily_factors": DF, "intraday_factors": IF, "warnings": dwarn + iwarn, "info": info,
        "bias": bias, "daily_bias": daily_bias, "intra_bias": intra_bias,
        "p_long": p_long, "p_short": p_short, "side": side, "verdict": verdict,
        "levels": levels, "plan": plan, "flow": flow, "warrants": warrants,
    }


def build_levels(d, q, info):
    t = d.get("tech") or {}
    L = [("漲停", d.get("limit_up")), ("跌停", d.get("limit_down")), ("昨收", q.get("y") or t.get("close")),
         ("今開", q.get("o")), ("今高", q.get("h")), ("今低", q.get("l")),
         ("MA5", t.get("ma5")), ("MA10", t.get("ma10")), ("MA20", t.get("ma20")), ("MA60", t.get("ma60")),
         ("布林下", t.get("bb_dn")), ("布林上", t.get("bb_up")), ("昨高", t.get("prev_high")), ("昨低", t.get("prev_low")),
         ("20日低", t.get("low20")), ("20日高", t.get("high20")), ("VWAP", info.get("vwap"))]
    p = q.get("ref")
    if p:
        step = 10 if p >= 100 else (5 if p >= 50 else 1)
        base = (int(p // step)) * step
        L += [("整數", float(base)), ("整數", float(base + step))]
    return [(n, round(v, 2)) for n, v in L if v]


def build_plan(d, q, info, levels, p_long, p_short, now):
    t = d.get("tech") or {}
    p = q.get("ref")
    if not p:
        return {}
    atr = t.get("atr14") or p * 0.03
    tick = I.tick_size(p)
    above = I.nearest_levels(p, levels, True, 4)
    below = I.nearest_levels(p, levels, False, 4)
    # 去掉幾乎重疊的價位
    def dedup(xs):
        out = []
        for n, v in xs:
            if all(abs(v - u) > tick * 2 for _, u in out):
                out.append((n, v))
        return out
    above, below = dedup(above), dedup(below)
    r1 = above[0] if above else None
    s1 = below[0] if below else None
    plan = {"resistance": above, "support": below}
    pad = max(tick * 2, round(atr * 0.15, 2))
    # 目標至少要離進場價 max(3 tick, 0.5%, 0.2 ATR)：11/05 晶豪科 12:03 壓力 VWAP 219.93、支撐布林下 218.99，
    # 空單進場 219.0、目標 1 變 218.99（差 1 tick），「建議做空」一分鐘就 ✔ 到目標、什麼都沒賺。太近的價位跳過當目標
    min_gain = max(tick * 3, p * 0.005, atr * 0.2)
    def targets(cands, entry, down):
        ok = [v for _, v in cands if (entry - v if down else v - entry) >= min_gain]
        return ok[0] if ok else None, (ok[1] if len(ok) > 1 else None)
    if r1:
        entry = I.round_tick(r1[1] - tick, "down")
        stop = I.round_tick(r1[1] + pad, "up")
        t1c, t2c = targets(below, entry, True)
        tgt1 = t1c or I.round_tick(entry - max(min_gain, atr * 0.5), "down")
        tgt2 = t2c or I.round_tick(entry - atr * 0.9, "down")
        mom = info.get("momentum") or {}
        note_s = f"反彈動能中（連 {mom.get('bars')} 根紅 K）：先不進，等 1 分 K 翻黑" if mom.get("dir") == "up" else None
        plan["short"] = {"entry": f"碰 {r1[1]} ({r1[0]}) 不過：1 分 K 收 {entry} 以下才進，{entry} 空；1 分 K 收過 {stop} 計畫作廢",
                         "confirm": f"等價格碰 {r1[1]}，1 分 K 收在 {entry} 以下", "note": note_s,
                         "entry_px": entry, "trigger_px": r1[1], "stop": stop,
                         "t1": tgt1, "t2": tgt2, "risk_pct": (stop - entry) / entry * 100,
                         "reward_pct": (entry - tgt1) / entry * 100,
                         "breakdown": f"備援：5分K帶量收破 {s1[1]} ({s1[0]}) 追空，停損 {I.round_tick(s1[1] + pad, 'up')}，目標 {tgt2}" if s1 else None,
                         "bd_trigger": s1[1] if s1 else None, "bd_stop": I.round_tick(s1[1] + pad, "up") if s1 else None, "bd_target": tgt2 if s1 else None}
    if s1:
        entry = I.round_tick(s1[1] + tick, "up")
        stop = I.round_tick(s1[1] - pad, "down")
        t1c, t2c = targets(above, entry, False)
        tgt1 = t1c or I.round_tick(entry + max(min_gain, atr * 0.5), "up")
        tgt2 = t2c or I.round_tick(entry + atr * 0.9, "up")
        mom = info.get("momentum") or {}
        note_l = f"殺盤動能中（連 {mom.get('bars')} 根黑 K）：先不接，等 1 分 K 翻紅" if mom.get("dir") == "down" else None
        plan["long"] = {"entry": f"回測 {s1[1]} ({s1[0]}) 守住：1 分 K 收 {entry} 以上才進，{entry} 多；1 分 K 收破 {stop} 計畫作廢",
                        "confirm": f"等價格回測 {s1[1]}，1 分 K 收在 {entry} 以上", "note": note_l,
                        "entry_px": entry, "trigger_px": s1[1], "stop": stop,
                        "t1": tgt1, "t2": tgt2, "risk_pct": (entry - stop) / entry * 100,
                        "reward_pct": (tgt1 - entry) / entry * 100,
                        "breakout": f"備援：5分K帶量收過 {r1[1]} ({r1[0]}) 追多，停損 {I.round_tick(r1[1] - pad, 'down')}，目標 {tgt2}" if r1 else None,
                        "bo_trigger": r1[1] if r1 else None, "bo_stop": I.round_tick(r1[1] - pad, "down") if r1 else None, "bo_target": tgt2 if r1 else None}
    return plan
