"""
因子組合提醒（觀察用、不計分）。

想法：大盤漲、族群強、個股又比同族群強——幾個條件同時成立時，續漲的機率是不是更高？
盤中出現這類組合時發一則 👁 警示並顯示在「即時」卡片，警示之後的走勢照樣被 _track_alerts 追蹤（復盤第 5 塊看得到各組合的順向率）。

為什麼不計分（2026-11-12 驗證）：
- 9 檔 × 41 天逐筆（沒有大盤分鐘歷史，用台積電當大盤、其他幾檔平均當同業），每 30 分鐘取樣 2,023 個：
    三方同向做多（大盤漲、同業強、個股強於同業）150 個 → 之後 30 分鐘 −0.08%（全部樣本 ±0.00%），看到收盤 −0.03%；
    三方同向做空 178 個 → 之後 30 分鐘 +0.04%（沒有續跌）；同向的條件從 0 個加到 3 個，報酬沒有跟著變好。
    這些條件說明的是「現在為什麼漲」，價格已經反映，不是「接下來還會漲」。
- 唯一方向一致的是「同業先動、自己還沒跟」：同業 10 分鐘比自己多走 0.2～1.0% 時，自己之後 10 分鐘平均 +0.04～0.07%
  （反過來自己先跑的 −0.06～0.08%），方向跟文獻的產業內領先落後一致，但幅度比來回成本 0.235% 小很多。
- 文獻：產業動能（Moskowitz & Grinblatt 1999）、多頭市場後動能較強（Cooper 等 2004、Hühn 2015）講的是以「月」為單位；
  盤中的延續主要在大盤層級（開盤半小時 → 最後半小時，Gao 等 2018），個股層級盤中反而偏反轉（Heston 等 2010）；
  同產業大型股領先小型股（Hou 2007）是少數直接講「因子互相影響」的結果。
實際盤中紀錄只有 17 個股票日、有族群資料的 8 個，還不能驗證；所以先提醒 + 追蹤，累積夠了再決定要不要計分。
"""

M_PCT = 0.3        # 大盤今日漲跌幅超過這個才算「漲 / 跌」
G_PCT = 0.5        # 族群平均漲跌幅
S_PCT = 1.0        # 個股比族群平均強 / 弱多少
VOL_PACE = 1.5     # 量比到這個數字，文字多標「帶量」
LEAD_PCT = 0.5     # 族群 10 分鐘走了這麼多
LAG_PCT = 0.5      # 個股 10 分鐘比族群少走這麼多 → 「還沒跟上」
T_START, T_END = "09:10", "13:15"
COOLDOWN = 1800    # 同一檔同一種組合 30 分鐘內只提醒一次

# key -> (名稱, 預期方向, 驗證結果一句話)
KINDS = {
    "align_up": ("三方同向·偏多", 1, "回測這個組合之後 30 分鐘沒有比平常更常續漲"),
    "align_dn": ("三方同向·偏空", -1, "回測這個組合之後 30 分鐘沒有比平常更常續跌"),
    "strong_alone": ("逆勢強", 1, "回測之後 30 分鐘 +0.08%，跟平常分不出來"),
    "weak_alone": ("逆勢弱", -1, "回測之後 30 分鐘 −0.06%，跟平常分不出來"),
    "catch_up": ("族群先漲·還沒跟", 1, "回測之後 10 分鐘平均只多 0.04～0.07%，不夠付成本"),
    "catch_dn": ("族群先跌·還沒跟", -1, "回測之後 10 分鐘平均只少 0.06～0.08%，不夠付成本"),
}


def alert_types():
    """monitor.ALERT_TYPES 用：[(種類, 比對警示文字的 regex)]。"""
    return [(f"組合·{nm}", rf"^👁 組合·{nm}") for nm, _, _ in KINDS.values()]


def directions():
    """eod.ALERT_DIR 用：{種類: 預期方向}。"""
    return {f"組合·{nm}": d for nm, d, _ in KINDS.values()}


def _f(x, n=1):
    return f"{x:+.{n}f}%"


def detect(info, own10=None, hhmm=None):
    """
    info：model.evaluate 回的 info（chg_pct、idx_chg_pct、group_avg、group_up、group_n、group_trend_10m、vol_pace）。
    own10：個股自己最近 10 分鐘的漲跌 %（沒有就不判斷「還沒跟」）。
    回目前成立的組合 [{key, name, dir, text, note}]；沒有族群報價的標的不會有任何組合。
    """
    if hhmm and not (T_START <= hhmm <= T_END):
        return []
    chg, idx, grp = info.get("chg_pct"), info.get("idx_chg_pct"), info.get("group_avg")
    out = []

    def add(key, text):
        nm, d, note = KINDS[key]
        out.append({"key": key, "name": nm, "dir": d, "text": text, "note": note})

    if chg is not None and idx is not None and grp is not None:
        rel = chg - grp
        ups, n = info.get("group_up"), info.get("group_n")
        gtxt = f"族群平均 {_f(grp)}" + (f"（{ups}/{n} 檔漲）" if n else "")
        vp = info.get("vol_pace")
        vtxt = f"，量比 {vp:.1f}（帶量）" if vp is not None and vp >= VOL_PACE else ""
        if idx >= M_PCT and grp >= G_PCT and rel >= S_PCT:
            add("align_up", f"大盤 {_f(idx)}、{gtxt}、個股 {_f(chg)}（比族群強 {rel:.1f}）{vtxt}")
        elif idx <= -M_PCT and grp <= -G_PCT and rel <= -S_PCT:
            add("align_dn", f"大盤 {_f(idx)}、{gtxt}、個股 {_f(chg)}（比族群弱 {-rel:.1f}）{vtxt}")
        elif idx <= -M_PCT and grp <= -G_PCT and rel >= S_PCT and chg > 0:
            add("strong_alone", f"大盤 {_f(idx)}、{gtxt}，個股卻 {_f(chg)}{vtxt}")
        elif idx >= M_PCT and grp >= G_PCT and rel <= -S_PCT and chg < 0:
            add("weak_alone", f"大盤 {_f(idx)}、{gtxt}，個股卻 {_f(chg)}{vtxt}")
    gt = info.get("group_trend_10m")
    if gt is not None and own10 is not None:
        if gt >= LEAD_PCT and gt - own10 >= LAG_PCT:
            add("catch_up", f"族群 10 分鐘 {_f(gt, 2)}，個股同時間只有 {_f(own10, 2)}")
        elif gt <= -LEAD_PCT and own10 - gt >= LAG_PCT:
            add("catch_dn", f"族群 10 分鐘 {_f(gt, 2)}，個股同時間只有 {_f(own10, 2)}")
    return out


def alert_text(x):
    return f"👁 組合·{x['name']}：{x['text']}。觀察用、不計分（{x['note']}）"
