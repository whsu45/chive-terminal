"""
盤前資料（每天一次，結果快取）：
日K技術面、三大法人 3 日、融資融券/借券、分點（隔日沖庫存、外資主力）、處置/注意、
大盤前日與夜盤/美股、族群成員。monitor.py 啟動時自動呼叫；也可單獨執行：
    python3 prep.py 3042 [--peers 2484,8081] [--refresh]
"""
import sys
import json
import os
import argparse
import datetime as dt

from config import CACHE_DIR, load_groups, find_groups_for
import http_client as http
import tw_calendar as cal
import src_mis, src_twse, src_tpex, src_moneydj, src_yahoo, src_taifex
import indicators as I
import winners
import src_warrants as W


def _safe(fn, default=None, label=""):
    try:
        return fn()
    except Exception as e:
        print(f"  [warn] {label or getattr(fn, '__name__', '')}: {str(e)[:120]}")
        return default


def resolve_peers(code, peers_arg=None):
    groups = load_groups()
    if peers_arg:
        peers = [p.strip() for p in peers_arg.split(",") if p.strip()]
        gname = "自訂"
    else:
        gs = find_groups_for(code, groups)
        gname = gs[0] if gs else None
        peers = [c for c in groups.get(gname, []) if c != code] if gname else []
    return gname, peers


def markets_for(codes):
    """一次用 MIS 判定上市/上櫃並拿名稱。"""
    ex = []
    for c in codes:
        ex += [f"tse_{c}.tw", f"otc_{c}.tw"]
    out = {}
    global _MIS_ERR
    _MIS_ERR = None
    for i in range(0, len(ex), 40):
        try:
            q = src_mis.fetch(ex[i:i + 40])
        except Exception as e:
            _MIS_ERR = str(e)
            print(f"  [warn] mis: {_MIS_ERR}")
            q = {}
        for c, v in q.items():
            out[c] = {"ex": v["ex"], "name": v["name"], "y": v["y"]}
    missing = [c for c in codes if c not in out]
    if missing:
        # 備援：MIS 沒給的，用前一交易日的證交所 / 櫃買收盤檔判定市場、名稱、昨收（盤前資料只需要這三個）
        try:
            prev = cal.prev_trading_day(cal.today_tw())
            tw = _safe(lambda: src_twse.all_quotes(prev), {}, "twse 收盤檔")
            tp = _safe(lambda: src_tpex.daily_quotes(prev), {}, "tpex 收盤檔")
            for c in missing:
                if c in tw and tw[c].get("c"):
                    out[c] = {"ex": "tse", "name": tw[c]["name"], "y": tw[c]["c"]}
                elif c in tp and tp[c].get("c"):
                    out[c] = {"ex": "otc", "name": tp[c]["name"], "y": tp[c]["c"]}
            got = [c for c in missing if c in out]
            if got:
                print(f"  [prep] MIS 沒給 {','.join(got)}，改用 {prev} 收盤檔判定上市櫃與昨收")
        except Exception as e:
            print(f"  [warn] 收盤檔備援失敗：{str(e)[:100]}")
    return out


_MIS_ERR = None


PREP_VERSION = 6  # 快取結構版本：欄位有增減就 +1，舊快取會自動重建


def build(code, peers_arg=None, refresh=False):
    today = cal.today_tw()
    prev = cal.prev_trading_day(today)
    cache = os.path.join(CACHE_DIR, f"daily_{code}_{today.isoformat()}.json")
    if not refresh and os.path.exists(cache):
        with open(cache, "r", encoding="utf-8") as f:
            d = json.load(f)
        if d.get("version") == PREP_VERSION and ((not peers_arg) or d.get("peers_arg") == peers_arg):
            return d

    print(f"[prep] 建立 {code} 盤前資料（前一交易日 {prev}）...")
    gname, peers = resolve_peers(code, peers_arg)
    mk = markets_for([code] + peers)
    if code not in mk:
        if _MIS_ERR:
            raise SystemExit(f"連不上證交所 MIS，不是代碼錯：{_MIS_ERR}")
        raise SystemExit(f"MIS 查不到 {code}，請確認代碼。")
    market = mk[code]["ex"]
    name = mk[code]["name"]
    is_tse = market == "tse"

    # --- 日K / 技術面 ---
    hist = _safe(lambda: (src_twse.daily_history if is_tse else src_tpex.daily_history)(code, 5), [], "日K")
    hist = [r for r in hist if r["date"] < today.isoformat()]
    if len(hist) < 30:
        yh = _safe(lambda: src_yahoo.tw_daily(code, market), [], "yahoo日K")
        if len(yh) > len(hist):
            hist = yh
    tech = I.daily_snapshot(hist)
    prev_close = tech.get("close") or mk[code]["y"]
    up, dn = I.limits(prev_close) if prev_close else (None, None)

    # --- 法人 3 日 ---
    days3 = cal.last_n_trading_days(today, 3)
    insti = {}
    for d in days3:
        tbl = _safe(lambda d=d: (src_twse.t86 if is_tse else src_tpex.insti)(d), {}, f"法人 {d}")
        insti[d.isoformat()] = tbl.get(code, {"foreign": 0, "trust": 0, "dealer": 0, "total": 0})
    insti_sum = {k: sum(v.get(k, 0) for v in insti.values()) for k in ("foreign", "trust", "dealer", "total")}

    # --- 信用 / 借券 ---
    margin = _safe(lambda: (src_twse.margin if is_tse else src_tpex.margin)(prev).get(code), None, "融資融券")
    sbl = _safe(lambda: src_twse.sbl(prev).get(code), None, "借券") if is_tse else None

    # --- 分點 ---
    vol_prev = tech.get("vol") or 0
    br1 = _safe(lambda: src_moneydj.broker_summary(code), None, "分點(昨)")
    br4 = _safe(lambda: src_moneydj.broker_summary_range(code, cal.prev_trading_day(today, 4), prev), None, "分點(4日)")
    brokers = {
        "day": src_moneydj.classify(br1, vol_prev) if br1 else None,
        "day_date": br1.get("date") if br1 else None,
        "range4": src_moneydj.classify(br4, sum(r["v"] for r in hist[-4:]) or None) if br4 else None,
    }

    # --- 常勝主力（近 20 日分點勝率）---
    win = None
    try:
        bd = winners.broker_daily_history(code, 20, today)
        sc = winners.score_branches(hist, bd)
        win = winners.summarize(sc, prev.isoformat(), vol_prev)
    except Exception as e:
        print(f"  [warn] 常勝主力: {str(e)[:120]}")

    # --- 權證：前日認購/認售成交、發行商避險、權證清單（供盤中彙總）---
    warr = None
    try:
        wd = W.warrants_for(code, name, market, prev)
        wsum = W.summarize(wd)
        wsum["hedge_net"] = W.hedge_net(prev, code, market)
        wsum["hedge_pct"] = wsum["hedge_net"] / vol_prev * 100 if vol_prev else None
        wsum["call_amt_5d"] = None
        amts = []
        for dd in cal.last_n_trading_days(prev, 5):
            try:
                amts.append(W.summarize(W.warrants_for(code, name, market, dd))["call_amt"])
            except Exception:
                pass
        if amts:
            wsum["call_amt_5d"] = sum(amts) / len(amts)
            wsum["call_surge"] = wsum["call_amt"] / wsum["call_amt_5d"] if wsum["call_amt_5d"] else None
        wsum["list"] = [{k: w[k] for k in ("code", "name", "right", "ex")} for w in wd["call"] + wd["put"]]
        warr = wsum
    except Exception as e:
        print(f"  [warn] 權證: {str(e)[:120]}")

    # --- 處置 / 注意 ---
    flags = []
    if is_tse:
        pun = _safe(lambda: src_twse.punish(today - dt.timedelta(days=12), today + dt.timedelta(days=10)), {}, "處置")
        if code in pun:
            flags.append(f"處置股 {pun[code]}")
        if code in _safe(lambda: src_twse.notice(prev), set(), "注意"):
            flags.append("注意股(前日)")
    else:
        disp = _safe(src_tpex.disposal, {}, "處置")
        if code in disp:
            flags.append(f"處置股 {disp[code]}")
        if code in _safe(src_tpex.attention, set(), "注意"):
            flags.append("注意股")

    # --- 大盤 / 夜盤 / 美股 ---
    mkt = {}
    mkt["prev_date"] = prev.isoformat()
    mkt["breadth"] = _safe(lambda: src_twse.market_breadth(prev), {}, "漲跌家數")
    bfi = _safe(lambda: src_twse.bfi82u(prev), {}, "法人金額")
    mkt["foreign_amt_b"] = round(sum(v for k, v in bfi.items() if k.startswith("外資及陸資")) / 1e8, 1) if bfi else None
    mkt["trust_amt_b"] = round(sum(v for k, v in bfi.items() if "投信" in k) / 1e8, 1) if bfi else None
    mkt["tx_night"] = _safe(lambda: src_taifex.tx_session(today, night=True), None, "夜盤")
    mkt["tx_day_prev"] = _safe(lambda: src_taifex.tx_session(prev, night=False), None, "日盤")
    mkt["foreign_oi"] = _safe(lambda: src_taifex.foreign_tx_oi(prev), None, "外資OI")
    mkt["us"] = _safe(src_yahoo.us_overnight, {}, "美股")

    # --- 族群成員前日資料 ---
    peer_info = {}
    all_tse = _safe(lambda: src_twse.all_quotes(prev), {}, "上市收盤")
    all_otc = _safe(lambda: src_tpex.daily_quotes(prev), {}, "上櫃收盤")
    for p in peers:
        if p not in mk:
            continue
        src = all_tse if mk[p]["ex"] == "tse" else all_otc
        peer_info[p] = {"name": mk[p]["name"], "ex": mk[p]["ex"], "y": mk[p]["y"],
                        "prev_chg_pct": (src.get(p) or {}).get("chg_pct"), "prev_vol": (src.get(p) or {}).get("v")}

    out = {
        "version": PREP_VERSION,
        "code": code, "name": name, "market": market, "date": today.isoformat(), "prev_date": prev.isoformat(),
        "group": gname, "peers": peers, "peers_arg": peers_arg, "peer_info": peer_info,
        "tech": tech, "hist_tail": hist[-30:], "limit_up": up, "limit_down": dn,
        "insti": insti, "insti_sum": insti_sum, "margin": margin, "sbl": sbl, "brokers": brokers, "winners": win, "warrants": warr, "flags": flags,
        "market_ctx": mkt,
    }
    with open(cache, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, default=str)
    print(f"[prep] 完成，快取 {cache}")
    return out


def describe(d):
    """盤前資料的文字摘要（monitor 啟動時印一次）。"""
    t = d["tech"]; L = []
    L.append(f"=== {d['code']} {d['name']} ({'上市' if d['market']=='tse' else '上櫃'})  族群：{d['group'] or '未定義'}  同族群：{','.join(d['peers']) or '-'}")
    if d["flags"]:
        L.append("  !! " + "；".join(d["flags"]))
    if t:
        L.append(f"  前日 {t['date']} 收 {t['close']} ({t.get('chg_pct') or 0:+.2f}%)  量 {t['vol']} 張 (5日均 {t['vol_ma5'] and round(t['vol_ma5'])})  連{'漲' if t['streak']>0 else '跌'} {abs(t['streak'])} 天")
        L.append(f"  MA5 {t['ma5'] and round(t['ma5'],2)}  MA10 {t['ma10'] and round(t['ma10'],2)}  MA20 {t['ma20'] and round(t['ma20'],2)}  MA60 {t['ma60'] and round(t['ma60'],2)}  BB {t['bb_dn'] and round(t['bb_dn'],1)}~{t['bb_up'] and round(t['bb_up'],1)}  ATR {t['atr14'] and round(t['atr14'],2)} ({t['atr_pct'] and round(t['atr_pct'],1)}%)  RSI {t['rsi14'] and round(t['rsi14'],1)}")
        L.append(f"  今日漲停 {d['limit_up']} / 跌停 {d['limit_down']}  20日高低 {t['high20']}/{t['low20']}  前日高低 {t['prev_high']}/{t['prev_low']}")
    s = d["insti_sum"]
    L.append(f"  法人3日：外資 {s['foreign']:+} 投信 {s['trust']:+} 自營 {s['dealer']:+} 張  | 逐日 " +
             " ".join(f"{k[5:]}:外{v['foreign']:+}/投{v['trust']:+}" for k, v in d["insti"].items()))
    m = d["margin"]
    if m:
        ratio = (m["short_now"] / m["fin_now"] * 100) if m["fin_now"] else 0
        L.append(f"  融資 {m['fin_now']} ({m['fin_chg']:+})  融券 {m['short_now']} ({m['short_chg']:+})  券資比 {ratio:.1f}%")
    if d["sbl"]:
        L.append(f"  借券賣出 {d['sbl']['sbl_sell']} 還券 {d['sbl']['sbl_return']} 餘額 {d['sbl']['sbl_bal']} 張")
    b = d["brokers"]
    if b.get("day"):
        bd = b["day"]
        L.append(f"  分點({b['day_date']})：隔日沖買 {bd['daytrade_buy']} 張 ({bd.get('daytrade_buy_pct', 0):.1f}% 量) {','.join(bd['daytrade_names']) or ''}  外資淨 {bd['foreign_net']:+}  前五買 {bd['top5_buy']} / 前五賣 {bd['top5_sell']}")
        L.append("    買：" + "  ".join(f"{r['broker']} {r['net']:+}" for r in bd["top_buyers"]))
        L.append("    賣：" + "  ".join(f"{r['broker']} {r['net']:+}" for r in bd["top_sellers"]))
    if b.get("range4"):
        r4 = b["range4"]
        L.append(f"  4日分點：外資淨 {r4['foreign_net']:+}  隔日沖淨 {r4['daytrade_net']:+}  主買 " + "  ".join(f"{r['broker']} {r['net']:+}" for r in r4["top_buyers"][:3]) + "  主賣 " + "  ".join(f"{r['broker']} {r['net']:+}" for r in r4["top_sellers"][:3]))
    if d.get("winners"):
        L.append(winners.describe(d["winners"]))
    w = d.get("warrants")
    if w and (w["n_call"] or w["n_put"]):
        L.append(f"  權證(前日)：認購 {w['call_vol']} 張 / 認售 {w['put_vol']} 張（{w['n_call_active']}/{w['n_call']} 檔認購有量）  認購金額 {w['call_amt']/1e4:.0f} 萬"
                 + (f"（= 5日均 ×{w['call_surge']:.1f}）" if w.get('call_surge') else "")
                 + f"  發行商避險 {w['hedge_net']:+} 張 ({(w['hedge_pct'] or 0):+.1f}% 量)")
    elif w is not None:
        hn = w.get("hedge_net") or 0
        L.append("  權證：此標的無權證" + (f"；自營商避險欄 {hn:+} 張 ({(w.get('hedge_pct') or 0):+.1f}% 量) 非權證避險，多為可轉債套利或其他避險" if abs(w.get("hedge_pct") or 0) >= 2 else ""))
    mc = d["market_ctx"]
    tx = mc.get("tx_night")
    us = mc.get("us", {})
    L.append(f"  大盤前日：漲 {mc.get('breadth',{}).get('up')} / 跌 {mc.get('breadth',{}).get('down')}  外資 {mc.get('foreign_amt_b')} 億  投信 {mc.get('trust_amt_b')} 億  外資期貨淨OI {(mc.get('foreign_oi') or {}).get('net_oi')}")
    if tx:
        L.append(f"  台指夜盤({tx.get('title')})：{tx['c']} ({tx['chg']:+} / {tx['chg_pct']:+.2f}%)  美股：" + "  ".join(f"{k} {v['pct']:+.2f}%" for k, v in us.items()))
    return "\n".join(L)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("code")
    ap.add_argument("--peers", default=None)
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args()
    d = build(a.code, a.peers, a.refresh)
    print(describe(d))
