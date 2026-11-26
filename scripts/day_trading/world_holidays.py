#!/usr/bin/env python3
"""
國際休市參考（2026-11-26 加）：美國、日本、韓國、新加坡、中國大陸的國定假日與股市休市日，給當沖當參考。

只顯示、記在訊號裡（sig["holidays"]），不加分、不擋訊號：還沒驗證過「外國休市的日子」訊號表現有沒有不同。
等 signal_outcomes 累積夠多再用這個欄位比（同 KD 參考的做法）。

時間對照（台股 09:00–13:30）：
  - 日、韓、新、陸股跟台股同一個白天交易 → 看「今天」有沒有休市。
  - 美股在台灣時間晚上交易（22:30–05:00，夏令 21:30–04:00）→ 台股開盤前看的是「昨晚」那一場；
    昨晚美股休市 = 今天早上沒有美股、台積電 ADR 的方向可以參考。「今晚」休市會寫在接下來的清單。

資料來源：
  美股（NYSE）、日股（JPX）：照交易所的休市規則算，任何年份都能算（2026、2027 已對過交易所公布的日期）。
  韓股（KRX）、新加坡（SGX）、陸股（上交所 / 深交所）：農曆假日、補假、選舉日、政府每年公告的調休沒辦法用規則算，
    下面 _TABLE 逐年手填，每年要補下一年（缺年份時儀表板會寫「XX 年休市表還沒建」）。
      陸股：國務院每年 11 月初公布下一年的放假安排，交易所 12 月底公告休市；週末的調休上班日 A 股照樣休市，所以只列平日。
      韓股：KRX 公告的休場日（含 5/1 勞動節、12/31 年終休場、補假、選舉日）。
      新加坡：人力部（MOM）公告的公眾假期 + SGX 的半天交易日。

    python3 world_holidays.py              # 印今天的狀態與接下來 30 天
    python3 world_holidays.py 2026-12-24   # 指定日期
"""
import datetime as dt
import sys

MARKETS = {"US": ("美國", "美股"), "JP": ("日本", "日股"), "KR": ("韓國", "韓股"), "SG": ("新加坡", "新加坡股"), "CN": ("中國大陸", "陸股")}
ASIA = ("JP", "KR", "SG", "CN")
WD = "一二三四五六日"
NOTE = "只顯示、未驗證"

# ---------------------------------------------------------------- 手填的表（韓、新、陸）
# (日期, 名稱, 種類, 備註)；種類：closed 休市 / half 半天（備註寫收盤時間，當地時間）
_TABLE = {
    "CN": {
        # 上交所〈關於 2026 年部分節假日休市安排的通知〉2025-12-22（同國務院 2025-11-04 公告）
        2026: [("2026-01-01", "元旦"), ("2026-01-02", "元旦"),
               ("2026-02-16", "春節"), ("2026-02-17", "春節"), ("2026-02-18", "春節"), ("2026-02-19", "春節"), ("2026-02-20", "春節"),
               ("2026-02-23", "春節"), ("2026-04-06", "清明節"), ("2026-05-01", "勞動節"), ("2026-05-04", "勞動節"), ("2026-05-05", "勞動節"),
               ("2026-06-19", "端午節"), ("2026-09-25", "中秋節"),
               ("2026-10-01", "國慶節"), ("2026-10-02", "國慶節"), ("2026-10-05", "國慶節"), ("2026-10-06", "國慶節"), ("2026-10-07", "國慶節")],
        # 國務院〈關於 2027 年部分節假日安排的通知〉國辦發明電〔2026〕10 號，2026-11-05；交易所的休市公告還沒出（通常 12 月底），出了再對一次
        2027: [("2027-01-01", "元旦"),
               ("2027-02-05", "春節"), ("2027-02-08", "春節"), ("2027-02-09", "春節"), ("2027-02-10", "春節"), ("2027-02-11", "春節"),
               ("2027-02-12", "春節"), ("2027-04-05", "清明節"), ("2027-05-03", "勞動節"), ("2027-05-04", "勞動節"), ("2027-05-05", "勞動節"),
               ("2027-06-09", "端午節"), ("2027-09-15", "中秋節"),
               ("2027-10-01", "國慶節"), ("2027-10-04", "國慶節"), ("2027-10-05", "國慶節"), ("2027-10-06", "國慶節"), ("2027-10-07", "國慶節")],
    },
    "KR": {
        # KRX 2026 休場日（todaystk.kr、kwatch.kr 兩家整理一致；6/03 地方選舉、7/17 制憲節有 KRX 5/20 公告的新聞）
        2026: [("2026-01-01", "元旦"), ("2026-02-16", "春節"), ("2026-02-17", "春節"), ("2026-02-18", "春節"), ("2026-03-02", "三一節補假"),
               ("2026-05-01", "勞動節"), ("2026-05-05", "兒童節"), ("2026-05-25", "佛誕日補假"), ("2026-06-03", "地方選舉"),
               ("2026-07-17", "制憲節"), ("2026-08-17", "光復節補假"), ("2026-09-24", "中秋節"), ("2026-09-25", "中秋節"),
               ("2026-10-05", "開天節補假"), ("2026-10-09", "韓文日"), ("2026-12-25", "聖誕節"), ("2026-12-31", "年終休市")],
        # KRX 還沒公告 2027 休場日：照政府 2026-06-29 公布的 2027 年曆（含補假）推。2027 春節在韓國是 2/7（週日，跟中國差一天）→ 2/9 補假。
        # 勞動節、制憲節 2027 起是法定公休日，週六 → 下週一補假是推估；12/31 年終休市照 KRX 慣例。KRX 公告後再對一次
        2027: [("2027-01-01", "元旦"), ("2027-02-08", "春節"), ("2027-02-09", "春節補假"), ("2027-03-01", "三一節"),
               ("2027-05-03", "勞動節補假（估計）"), ("2027-05-05", "兒童節"), ("2027-05-13", "佛誕日"), ("2027-07-19", "制憲節補假（估計）"),
               ("2027-08-16", "光復節補假"), ("2027-09-14", "中秋節"), ("2027-09-15", "中秋節"), ("2027-09-16", "中秋節"),
               ("2027-10-04", "開天節補假"), ("2027-10-11", "韓文日補假"), ("2027-12-27", "聖誕節補假"), ("2027-12-31", "年終休市（估計）")],
    },
    "SG": {
        # 人力部（MOM）公眾假期；週日的假期補週一、週六不補。半天（中午收）是 State Street 整理的 SGX 行事曆，2027 的半天還沒公告、照慣例估
        2026: [("2026-01-01", "元旦"), ("2026-02-16", "農曆除夕", "half", "12:00"), ("2026-02-17", "農曆新年"), ("2026-02-18", "農曆新年"),
               ("2026-04-03", "耶穌受難日"), ("2026-05-01", "勞動節"), ("2026-05-27", "哈芝節"), ("2026-06-01", "衛塞節補假"),
               ("2026-08-10", "國慶日補假"), ("2026-11-09", "屠妖節補假"), ("2026-12-24", "平安夜", "half", "12:00"), ("2026-12-25", "聖誕節"),
               ("2026-12-31", "跨年夜", "half", "12:00")],
        2027: [("2027-01-01", "元旦"), ("2027-02-05", "農曆除夕（估計）", "half", "12:00"), ("2027-02-08", "農曆新年補假"),
               ("2027-03-10", "開齋節"), ("2027-03-26", "耶穌受難日"), ("2027-05-17", "哈芝節"), ("2027-05-20", "衛塞節"),
               ("2027-08-09", "國慶日"), ("2027-10-28", "屠妖節"), ("2027-12-24", "平安夜（估計）", "half", "12:00"),
               ("2027-12-31", "跨年夜（估計）", "half", "12:00")],
    },
}


# ---------------------------------------------------------------- 照規則算（美、日）
def _nth_wd(y, m, wd, n):
    """y 年 m 月第 n 個星期 wd（0 = 週一）；n = -1 是最後一個。"""
    if n > 0:
        d = dt.date(y, m, 1)
        d += dt.timedelta(days=(wd - d.weekday()) % 7)
        return d + dt.timedelta(weeks=n - 1)
    d = dt.date(y, m + 1, 1) - dt.timedelta(days=1) if m < 12 else dt.date(y, 12, 31)
    return d - dt.timedelta(days=(d.weekday() - wd) % 7)


def _easter(y):
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    mo = (h + l_ - 7 * m + 114) // 31
    return dt.date(y, mo, (h + l_ - 7 * m + 114) % 31 + 1)


def _us_observed(d):
    return d - dt.timedelta(days=1) if d.weekday() == 5 else d + dt.timedelta(days=1) if d.weekday() == 6 else d


def _us(y):
    """NYSE：休市日、半天（美東 13:00 收），加上股市照常開的聯邦假日（哥倫布日、退伍軍人節：債市休市）。"""
    out = []
    nyd = dt.date(y, 1, 1)
    if nyd.weekday() != 5:                       # 元旦碰到週六不補（NYSE 規則：前一年 12/31 照常開）
        out.append((_us_observed(nyd), "元旦", "closed", ""))
    out += [(_nth_wd(y, 1, 0, 3), "馬丁路德金恩紀念日", "closed", ""),
            (_nth_wd(y, 2, 0, 3), "總統日", "closed", ""),
            (_easter(y) - dt.timedelta(days=2), "耶穌受難日", "closed", ""),
            (_nth_wd(y, 5, 0, -1), "陣亡將士紀念日", "closed", ""),
            (_us_observed(dt.date(y, 6, 19)), "六月節", "closed", ""),
            (_us_observed(dt.date(y, 7, 4)), "獨立紀念日", "closed", ""),
            (_nth_wd(y, 9, 0, 1), "勞動節", "closed", ""),
            (_nth_wd(y, 11, 3, 4), "感恩節", "closed", ""),
            (_us_observed(dt.date(y, 12, 25)), "聖誕節", "closed", ""),
            (_nth_wd(y, 10, 0, 2), "哥倫布日", "open", "聯邦假日、股市照常（債市休市）"),
            (_us_observed(dt.date(y, 11, 11)), "退伍軍人節", "open", "聯邦假日、股市照常（債市休市）")]
    j3 = dt.date(y, 7, 3)
    if j3.weekday() < 4:                         # 7/4 落在週二～週五：7/3 半天
        out.append((j3, "獨立紀念日前夕", "half", "13:00"))
    out.append((_nth_wd(y, 11, 3, 4) + dt.timedelta(days=1), "感恩節翌日", "half", "13:00"))
    ce = dt.date(y, 12, 24)
    if ce.weekday() < 4:                         # 12/25 落在週二～週五：12/24 半天
        out.append((ce, "平安夜", "half", "13:00"))
    return out


def _jp_equinox(y, spring):
    base = 20.8431 if spring else 23.2488        # 1980–2099 年的近似式（國立天文台公布的日期都對得上）
    return dt.date(y, 3 if spring else 9, int(base + 0.242194 * (y - 1980) - (y - 1980) // 4))


def _jp(y):
    """JPX：國民の祝日 + 振替休日 + 國民の休日 + 交易所年末年初休市（1/2、1/3、12/31）。"""
    hol = {dt.date(y, 1, 1): "元旦", _nth_wd(y, 1, 0, 2): "成人之日", dt.date(y, 2, 11): "建國紀念日", dt.date(y, 2, 23): "天皇誕辰",
           _jp_equinox(y, True): "春分之日", dt.date(y, 4, 29): "昭和之日", dt.date(y, 5, 3): "憲法紀念日", dt.date(y, 5, 4): "綠之日",
           dt.date(y, 5, 5): "兒童節", _nth_wd(y, 7, 0, 3): "海之日", dt.date(y, 8, 11): "山之日", _nth_wd(y, 9, 0, 3): "敬老之日",
           _jp_equinox(y, False): "秋分之日", _nth_wd(y, 10, 0, 2): "體育之日", dt.date(y, 11, 3): "文化之日", dt.date(y, 11, 23): "勤勞感謝日"}
    for d in sorted(hol):                        # 振替休日：祝日碰到週日 → 之後第一個不是祝日的日子放假
        if d.weekday() == 6:
            s = d + dt.timedelta(days=1)
            while s in hol:
                s += dt.timedelta(days=1)
            hol[s] = f"補假（{hol[d]}）"
    for d in sorted(hol):                        # 國民の休日：夾在兩個祝日中間的平日
        m = d + dt.timedelta(days=1)
        if m not in hol and (m + dt.timedelta(days=1)) in hol and m.weekday() < 6:
            hol[m] = "國民休息日"
    for d, n in ((dt.date(y, 1, 2), "年初休市"), (dt.date(y, 1, 3), "年初休市"), (dt.date(y, 12, 31), "年終休市")):
        hol.setdefault(d, n)
    return [(d, n, "closed", "") for d, n in hol.items()]


# ---------------------------------------------------------------- 查詢
_CACHE = {}


def _year(cc, y):
    k = (cc, y)
    if k not in _CACHE:
        if cc == "US":
            rows = _us(y)
        elif cc == "JP":
            rows = _jp(y)
        else:
            t = _TABLE.get(cc, {}).get(y)
            rows = None if t is None else [(dt.date.fromisoformat(r[0]), r[1], r[2] if len(r) > 2 else "closed", r[3] if len(r) > 3 else "")
                                           for r in t]
        _CACHE[k] = None if rows is None else {d: (n, kind, note) for d, n, kind, note in rows if d.weekday() < 5}
    return _CACHE[k]


def on(cc, d):
    """cc 市場在 d（當地日期）的假日：(名稱, 種類, 備註) 或 None；表裡沒有這一年回 "missing"。"""
    t = _year(cc, d.year)
    if t is None:
        return "missing"
    return t.get(d)


def _entry(cc, d, h):
    n, kind, note = h
    return {"date": d.isoformat(), "wd": WD[d.weekday()], "cc": cc, "country": MARKETS[cc][0], "market": MARKETS[cc][1],
            "name": n, "kind": kind, "note": note}


def _us_tw_close(d, hhmm):
    """美東收盤時間 → 台灣時間（夏令 +12、冬令 +13 小時；夏令＝3 月第二個週日到 11 月第一個週日）。"""
    h, m = map(int, hhmm.split(":"))
    dst = _nth_wd(d.year, 3, 6, 2) <= d < _nth_wd(d.year, 11, 6, 1)
    return f"{(h + (12 if dst else 13)) % 24:02d}:{m:02d}"


def _short(e):
    """頂端用的短句：「日股休市」「美股提早收盤」「新加坡股半天」。"""
    return f"{e['market']}{'休市' if e['kind'] == 'closed' else '提早收盤' if e['cc'] == 'US' else '半天'}"


def _label(e, *, when=""):
    """一筆假日的短句：「日股休市」「美股提早收盤（美東 13:00 = 台灣 02:00）」。"""
    if e["kind"] == "closed":
        return f"{when}{e['market']}休市"
    if e["kind"] == "half":
        if e["cc"] == "US":
            return f"{when}美股提早收盤（美東 {e['note']} = 台灣 {_us_tw_close(dt.date.fromisoformat(e['date']), e['note'])}）"
        return f"{when}{e['market']}只開半天（{e['note']} 收）"
    return f"{when}{e['country']}國定假日（{e['note'] or '股市照常'}）"


def status(d=None, days=30):
    """台股 d 這天的國際休市參考。
    回 {date, today（日韓新陸今天的假日）, us_prev（上一場美股是休市 / 半天）, us_tonight, upcoming（之後 days 天）,
        flags（記到訊號）, chip（頂端短句）, text（完整說明）, missing（缺表的市場與年份）}。"""
    d = d or dt.date.today()
    if isinstance(d, str):
        d = dt.date.fromisoformat(d)
    missing = sorted({f"{MARKETS[cc][1]} {y}" for cc in MARKETS for y in {d.year, (d + dt.timedelta(days=days)).year}
                      if _year(cc, y) is None})
    today = []
    for cc in ASIA:
        h = on(cc, d)
        if h and h != "missing":
            today.append(_entry(cc, d, h))
    # 上一場美股：d 前一個美國平日（週一 → 上週五）。那一天休市 = 台灣時間那天晚上沒開
    p = d - dt.timedelta(days=1)
    while p.weekday() >= 5:
        p -= dt.timedelta(days=1)
    hp = on("US", p)
    us_prev = _entry("US", p, hp) if hp and hp != "missing" and hp[1] != "open" else None
    if us_prev:
        us_prev["when"] = "昨晚" if (d - p).days == 1 else f"{p.month}/{p.day}（{WD[p.weekday()]}）晚上"
    ht = on("US", d)
    us_tonight = _entry("US", d, ht) if ht and ht != "missing" and ht[1] != "open" else None
    upcoming = []
    for i in range(1, days + 1):
        x = d + dt.timedelta(days=i)
        if x.weekday() >= 5:
            continue
        for cc in MARKETS:
            h = on(cc, x)
            if h and h != "missing":
                upcoming.append(_entry(cc, x, h))
    flags = {"closed": [e["cc"] for e in today if e["kind"] == "closed"], "half": [e["cc"] for e in today if e["kind"] == "half"],
             "us_prev": us_prev and us_prev["kind"], "us_tonight": us_tonight and us_tonight["kind"]}
    # 頂端短句：今天有事就寫今天的；沒有就寫 7 天內最近的一筆（股市照常的國定假日不上頂端）
    parts = []
    if us_prev:
        parts.append(f"{us_prev['when']}{_short(us_prev)}（{us_prev['name']}）")
    for e in today:
        if e["kind"] != "open":
            parts.append(f"今天{_short(e)}（{e['name']}）")
    if us_tonight:
        parts.append(f"今晚{_short(us_tonight)}（{us_tonight['name']}）")
    nxt = next((e for e in upcoming if e["kind"] != "open"), None)
    soon = nxt and (dt.date.fromisoformat(nxt["date"]) - d).days <= 7
    chip = "・".join(parts) if parts else (f"{nxt['date'][5:].replace('-', '/')}（{nxt['wd']}）{_short(nxt)}（{nxt['name']}）" if soon else "")
    lines = [f"台股 {d.month}/{d.day}（{WD[d.weekday()]}）國際休市參考（{NOTE}）"]
    lines.append("・上一場美股：" + ((f"{us_prev['when']}休市（{us_prev['name']}），今天早上沒有新的美股、台積電 ADR 方向可參考"
                                    if us_prev["kind"] == "closed" else
                                    f"{us_prev['when']}只開半天（{us_prev['name']}），量小、方向參考性比較低") if us_prev else "正常"))
    asia = [f"{e['market']}{'休市' if e['kind'] == 'closed' else '半天'}（{e['name']}）" for e in today if e["kind"] != "open"]
    lines.append("・今天日韓新陸：" + ("、".join(asia) + "：區域連動與外資進出可能比較少" if asia else "都有開"))
    if us_tonight:
        lines.append(f"・今晚：{_label(us_tonight)}（{us_tonight['name']}）")
    if upcoming:
        lines.append(f"接下來 {days} 天：")
        for e in upcoming:
            lines.append(f"  {e['date'][5:].replace('-', '/')}（{e['wd']}）{e['country']} {e['name']}：{_label(e)}")
    if missing:
        lines.append("⚠ 還沒建休市表：" + "、".join(missing) + "（world_holidays.py 的 _TABLE，每年要補下一年）")
    return {"date": d.isoformat(), "today": today, "us_prev": us_prev, "us_tonight": us_tonight, "upcoming": upcoming,
            "flags": flags, "chip": chip, "active": bool(us_prev or any(e["kind"] != "open" for e in today)), "text": "\n".join(lines), "missing": missing}


def calendar(start, end):
    """start～end（含）所有市場的平日假日，依日期排序；給 GitHub Pages 首頁嵌進頁面、由瀏覽器依當天日期判斷。
    回 (entries, missing)；entries 每筆同 status() 的 upcoming，半天的美股另附台灣收盤時間 tw_close。"""
    out, missing = [], set()
    x = start
    while x <= end:
        if x.weekday() < 5:
            for cc in MARKETS:
                h = on(cc, x)
                if h == "missing":
                    missing.add(f"{MARKETS[cc][1]} {x.year}")
                elif h:
                    e = _entry(cc, x, h)
                    if cc == "US" and e["kind"] == "half":
                        e["tw_close"] = _us_tw_close(x, e["note"])
                    out.append(e)
        x += dt.timedelta(days=1)
    return out, sorted(missing)


def ref_text(st):
    """訊號上的參考句（只看會影響今天盤中的：上一場美股、今天日韓新陸）；沒事回 None。"""
    if not st or not st.get("active"):
        return None
    bits = []
    if st.get("us_prev"):
        u = st["us_prev"]
        bits.append(f"{u['when']}{_short(u)}（{u['name']}）")
    asia = [e for e in st.get("today") or [] if e["kind"] != "open"]
    if asia:
        bits.append("今天" + "、".join(_short(e) for e in asia)
                    + f"（{'、'.join(sorted({e['name'] for e in asia}))}）")
    return "、".join(bits) + f"：外資與區域連動可能比較少（{NOTE}）"


if __name__ == "__main__":
    st = status(dt.date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else None)
    print("頂端：", st["chip"] or "（無）")
    print("訊號參考：", ref_text(st) or "（無）")
    print("記錄：", st["flags"])
    print(st["text"])
