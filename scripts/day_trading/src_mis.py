"""證交所 MIS 即時報價（上市/上櫃/指數）。盤中約 5 秒更新，試撮 08:30–09:00 可見。"""
import http_client as http

MIS_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"


def _split(s):
    return [x for x in (s or "").split("_") if x != ""]


def fetch(ex_codes):
    """
    ex_codes: ["tse_2330.tw", "otc_3707.tw", "tse_t00.tw", "otc_o00.tw"]
    回傳 {code: quote}。quote 欄位：
      t 時間, z 成交價(可能 '-'), o 開, h 高, l 低, y 昨收, v 累積量(張),
      bids/asks: [(price, vol)...] 五檔, u 漲停, w 跌停, n 名稱, ex 市場
    """
    j = http.get_json(MIS_URL, params={"ex_ch": "|".join(ex_codes), "json": "1", "delay": "0"},
                      referer="https://mis.twse.com.tw/stock/index.jsp", retries=1, timeout=8)
    out = {}
    for s in j.get("msgArray", []):
        code = s.get("c")
        z = http.to_float(s.get("z"))
        bids = list(zip([http.to_float(x) for x in _split(s.get("b"))], [http.to_int(x) for x in _split(s.get("g"))]))
        asks = list(zip([http.to_float(x) for x in _split(s.get("a"))], [http.to_int(x) for x in _split(s.get("f"))]))
        # 無最新成交價（z='-'）時以最佳買賣中點當參考價，但夾在今日高低之間：
        # 「瞬間價格穩定措施」延緩撮合的 2 分鐘內 MIS 不給成交價、五檔卻是試算價
        # （11/03 建榮 09:24：成交最高 93.5，五檔中點 96.65），不夾會把試算價當成真的成交。
        # 超出高低的中點另存 trial，給前端/警示用。
        ref = z
        trial = None
        hi, lo = http.to_float(s.get("h")), http.to_float(s.get("l"))
        if ref is None:
            mid = None
            if bids and asks and bids[0][0] and asks[0][0]:
                mid = (bids[0][0] + asks[0][0]) / 2
            elif bids and bids[0][0]:
                mid = bids[0][0]
            elif asks and asks[0][0]:
                mid = asks[0][0]
            if mid is not None:
                ref = mid
                if hi and lo and not (lo <= mid <= hi):
                    trial = mid
                    ref = min(max(mid, lo), hi)
        out[code] = {
            "code": code, "name": s.get("n"), "ex": s.get("ex"), "t": s.get("t"),
            "z": z, "ref": ref, "o": http.to_float(s.get("o")), "h": http.to_float(s.get("h")),
            "l": http.to_float(s.get("l")), "y": http.to_float(s.get("y")), "v": http.to_int(s.get("v")),
            "u": http.to_float(s.get("u")), "w": http.to_float(s.get("w")),
            "bids": bids, "asks": asks,
            "tv": http.to_int(s.get("tv")),  # 當盤成交量
            "trial": trial,  # 延緩撮合期間的試算價（超出今日高低時才有）
        }
    return out


def detect_market(code):
    """同時查 tse/otc，回傳 'tse' 或 'otc'（查不到回 None）。"""
    q = fetch([f"tse_{code}.tw", f"otc_{code}.tw"])
    if code in q:
        return q[code]["ex"]
    return None


def index_codes():
    return ["tse_t00.tw", "otc_o00.tw"]
