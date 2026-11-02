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
def intraday_factors(d, q, idx, peers_q, flow, samples, now):
    """
    q: 個股 MIS quote；idx: {t00:quote, o00:quote}；peers_q: {code: quote}；
    flow: shioaji 大單分流（可 None）；samples: 本程式歷次取樣 [(time, price, cumvol)]
    """
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

    # 隔日沖庫存 + 盤中急拉（不一定是跳空）：庫存 ≥5% 量且盤中漲幅 ≥5%，就是倒貨點附近
    dtp_inv = ((d.get("brokers") or {}).get("day") or {}).get("daytrade_buy_pct") or 0
    if dtp_inv >= 5 and chg >= 5 and not pre_open:
        F.append(("庫存倒貨點", -0.6, f"隔日沖分點昨買 {dtp_inv:.0f}% 量，盤中已拉 {chg:+.1f}%：他們的獲利了結區，反彈不過高點就是空點（但首 10 分鐘先不追）"))
    elif dtp_inv >= 5 and chg >= 2.5 and not pre_open:
        F.append(("庫存逼近倒貨區", -0.3, f"隔日沖庫存 {dtp_inv:.0f}% 量，盤中 {chg:+.1f}%，接近他們的出貨區"))

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
    pc = []
    for code, pq in (peers_q or {}).items():
        if pq.get("ref") and pq.get("y"):
            pc.append(((pq["ref"] / pq["y"] - 1) * 100, code, pq.get("name")))
    if pc:
        avg = sum(x[0] for x in pc) / len(pc)
        ups = sum(1 for x in pc if x[0] > 0)
        info["group_avg"] = avg; info["group_up"] = ups; info["group_n"] = len(pc)
        info["group_detail"] = sorted(pc, reverse=True)
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

    # 時間規則
    if hhmm >= "09:50":
        warns.append("已過 09:50：依紀律不開新倉，以下僅供觀察與復盤")
    elif hhmm >= "09:35":
        warns.append("09:35 以後只做縮短時間目標的單，09:50 前全數平倉")
    elif "09:00" <= hhmm < "09:10":
        warns.append("開盤首 10 分鐘：位置、VWAP、相對強弱都還是雜訊，盤中因子只計一半，不追單")
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


def evaluate(d, q, idx, peers_q, flow, samples, now=None, warrants=None):
    now = now or dt.datetime.now()
    DF, dwarn = daily_factors(d)
    IF, iwarn, info = intraday_factors(d, q, idx, peers_q, flow, samples, now)
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
    if r1:
        entry = I.round_tick(r1[1] - tick, "down")
        stop = I.round_tick(r1[1] + pad, "up")
        tgt1 = s1[1] if s1 else I.round_tick(p - atr * 0.5, "down")
        tgt2 = below[1][1] if len(below) > 1 else I.round_tick(p - atr * 0.9, "down")
        plan["short"] = {"entry": f"反彈至 {r1[1]} ({r1[0]}) 不過，{entry} 空", "stop": stop,
                         "t1": tgt1, "t2": tgt2, "risk_pct": (stop - entry) / entry * 100,
                         "reward_pct": (entry - tgt1) / entry * 100,
                         "breakdown": f"備援：5分K收破 {s1[1]} ({s1[0]}) 追空，停損 {I.round_tick(s1[1] + pad, 'up')}，目標 {tgt2}" if s1 else None}
    if s1:
        entry = I.round_tick(s1[1] + tick, "up")
        stop = I.round_tick(s1[1] - pad, "down")
        tgt1 = r1[1] if r1 else I.round_tick(p + atr * 0.5, "up")
        tgt2 = above[1][1] if len(above) > 1 else I.round_tick(p + atr * 0.9, "up")
        plan["long"] = {"entry": f"回測 {s1[1]} ({s1[0]}) 守住，{entry} 多", "stop": stop,
                        "t1": tgt1, "t2": tgt2, "risk_pct": (entry - stop) / entry * 100,
                        "reward_pct": (tgt1 - entry) / entry * 100,
                        "breakout": f"備援：5分K收過 {r1[1]} ({r1[0]}) 追多，停損 {I.round_tick(r1[1] - pad, 'down')}，目標 {tgt2}" if r1 else None}
    return plan
