import re
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from .utils import clean_int, DATA_SOURCES


def _is_response_date_matched(soup, target_date_str):
    """
    檢查期交所回傳的頁面是否真正屬於 target_date_str。
    若日期尚未結算，期交所會自動退回顯示前一日資料或顯示查無資料。
    """
    page_text = soup.get_text()
    if "查無資料" in page_text or "查無相關資料" in page_text:
        return False

    # target_date_str 格式如 "2026/09/10" 或 "2026/9/10"
    parts = target_date_str.split('/')
    if len(parts) == 3:
        y, m, d = parts[0], str(int(parts[1])), str(int(parts[2]))
        # 匹配 "2026/09/10" 或 "2026/9/10" 或 "2026年9月10日"
        patterns = [
            f"{y}/{parts[1]}/{parts[2]}",
            f"{y}/{m}/{d}",
            f"{y}年{m}月{d}日"
        ]
        # 只要頁面標題/資訊區有出現對應日期即算相符
        for pat in patterns:
            if pat in page_text:
                return True
        return False

    return target_date_str in page_text


def _post_daily_market_report(session, query_date_str, market_code):
    url = DATA_SOURCES["taifex"]["daily_market_report_url"]
    payload = {
        'queryType': '2', 'marketCode': market_code, 'dateaddcnt': '',
        'commodity_id': 'TX', 'commodity_id2': '', 'queryDate': query_date_str,
        'MarketCode': market_code, 'commodity_idt': 'TX', 'commodity_id2t': '', 'commodity_id2t2': ''
    }
    headers = {'User-Agent': 'Mozilla/5.0'}
    resp = session.post(url, data=payload, headers=headers, timeout=5)
    if resp.status_code != 200:
        return None
    return BeautifulSoup(resp.text, 'html.parser')


def _parse_night_session_start(soup):
    """ 夜盤行情表標題「2026/10/22 15:00~次日05:00 盤後交易時段行情表」→ '2026/10/22' """
    m = re.search(r'(\d{4})/(\d{1,2})/(\d{1,2})\s*15:00\s*~\s*次日\s*05:00', soup.get_text(' '))
    if not m:
        return None
    return f"{m.group(1)}/{int(m.group(2)):02d}/{int(m.group(3)):02d}"


def fetch_night_market_data(session, query_date_str):
    """
    以期交所「交易日」查詢夜盤行情。
    回傳 {"price_change", "volume", "session_start"}；查無資料時回傳 None。
    注意：期交所將夜盤歸屬於「下一個營業日」，遇休市時交易日會晚於夜盤實際開盤日的隔天。
    """
    try:
        soup = _post_daily_market_report(session, query_date_str, '1')
        if soup is None or not _is_response_date_matched(soup, query_date_str):
            return None

        tables = soup.find_all('table', {'class': ['table_f', 'table_a']})
        for table in tables:
            for row in table.find_all('tr'):
                cols = [td.text.strip() for td in row.find_all('td')]
                if len(cols) >= 9 and cols[0] == 'TX':
                    return {
                        "price_change": clean_int(cols[6]),
                        "volume": clean_int(cols[8]),
                        "session_start": _parse_night_session_start(soup),
                    }
    except Exception as e:
        print(f"[{query_date_str}] Night market error: {e}")
    return None


def find_night_session(session, target_date_str, prev_date_str, today_str):
    """
    找出「前一個平日 (prev_date_str) 15:00 開盤」的那盤夜盤，作為 target_date_str 的開盤預測依據。
    一般情況期交所交易日 = target_date_str；若 target_date_str 休市，該夜盤會被歸到之後的營業日，
    因此往後逐日查詢（不超過今天），並以頁面上的夜盤開始日期比對。
    回傳 {"trade_date", "price_change", "volume"}；前一個平日休市（沒有夜盤）時回傳 None。
    """
    curr = datetime.strptime(target_date_str, "%Y/%m/%d")
    today = datetime.strptime(today_str, "%Y/%m/%d")
    for _ in range(8):
        if curr > today:
            break
        if curr.weekday() < 5:
            q = curr.strftime("%Y/%m/%d")
            night = fetch_night_market_data(session, q)
            if night is not None:
                start = night["session_start"]
                if start is None or start == prev_date_str:
                    return {"trade_date": q, "price_change": night["price_change"], "volume": night["volume"]}
                # 該交易日的夜盤並非 prev_date_str 開盤 → prev_date_str 當天沒有夜盤
                return None
        curr += timedelta(days=1)
    return None


def is_taifex_trading_day(session, date_str):
    """ 期交所一般交易時段是否有該日行情（休市日回傳 False） """
    try:
        soup = _post_daily_market_report(session, date_str, '0')
        return soup is not None and _is_response_date_matched(soup, date_str)
    except Exception as e:
        print(f"[{date_str}] Trading day check error: {e}")
        return True


def fetch_day_market_volume(session, prev_date_str):
    url = DATA_SOURCES["taifex"]["daily_market_report_url"]
    payload = {
        'queryType': '2', 'marketCode': '0', 'dateaddcnt': '',
        'commodity_id': 'TX', 'commodity_id2': '', 'queryDate': prev_date_str,
        'MarketCode': '0', 'commodity_idt': 'TX', 'commodity_id2t': '', 'commodity_id2t2': ''
    }
    headers = {'User-Agent': 'Mozilla/5.0'}
    try:
        resp = session.post(url, data=payload, headers=headers, timeout=5)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, 'html.parser')
            if not _is_response_date_matched(soup, prev_date_str):
                return None

            tables = soup.find_all('table', {'class': ['table_f', 'table_a']})
            for table in tables:
                day_vol_idx = 9
                headers_text = [th.text.strip() for th in table.find_all('th')]
                for idx, h_text in enumerate(headers_text):
                    if '一般交易時段' in h_text and '成交量' in h_text:
                        day_vol_idx = idx
                        break

                for row in table.find_all('tr'):
                    cols = [td.text.strip() for td in row.find_all('td')]
                    if len(cols) > day_vol_idx and cols[0] == 'TX':
                        volume = clean_int(cols[day_vol_idx])
                        if volume is not None:
                            return volume
    except Exception as e:
        print(f"[{prev_date_str}] Day volume error: {e}")
    return None


def fetch_institutional_positions_ah(session, target_date_str):
    url = DATA_SOURCES["taifex"]["institutional_positions_ah_url"]
    payload = {
        'queryType': '1', 'goDay': '', 'doQuery': '1', 'dateaddcnt': '',
        'queryDate': target_date_str, 'commodityId': 'TXF', 'button': '送出查詢'
    }
    headers = {
        'User-Agent': 'Mozilla/5.0',
        'Content-Type': 'application/x-www-form-urlencoded',
        'Referer': url
    }
    positions = {"foreign": None, "trust": None, "dealer": None}

    try:
        resp = session.post(url, data=payload, headers=headers, timeout=5)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, 'html.parser')
            # 檢查回傳網頁的日期是否相符，若不符代表期交所尚未公布當日資料
            if not _is_response_date_matched(soup, target_date_str):
                return positions

            tables = soup.find_all('table', {'class': ['table_f', 'table_a']})
            for table in tables:
                for row in table.find_all('tr'):
                    cols = [td.text.strip() for td in row.find_all('td')]
                    for idx, col in enumerate(cols):
                        if '自營商' in col and positions["dealer"] is None:
                            if len(cols) > idx + 5:
                                positions["dealer"] = clean_int(cols[idx + 5])
                        elif '投信' in col and positions["trust"] is None:
                            if len(cols) > idx + 5:
                                positions["trust"] = clean_int(cols[idx + 5])
                        elif '外資' in col and positions["foreign"] is None:
                            if len(cols) > idx + 5:
                                positions["foreign"] = clean_int(cols[idx + 5])
    except Exception as e:
        print(f"[{target_date_str}] Institutional AH position error: {e}")

    return positions


def fetch_institutional_positions_full(session, target_date_str):
    url = DATA_SOURCES["taifex"]["institutional_positions_full_url"]
    payload = {
        'queryType': '1', 'goDay': '', 'doQuery': '1', 'dateaddcnt': '',
        'queryDate': target_date_str, 'commodityId': 'TXF', 'button': '送出查詢'
    }
    headers = {
        'User-Agent': 'Mozilla/5.0',
        'Content-Type': 'application/x-www-form-urlencoded',
        'Referer': url
    }
    positions = {"foreign": None, "trust": None, "dealer": None}

    try:
        resp = session.post(url, data=payload, headers=headers, timeout=5)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, 'html.parser')
            # 核心防呆：若期交所回傳的日期非 target_date_str（表示當日尚未收盤產出），直接回傳 None
            if not _is_response_date_matched(soup, target_date_str):
                return positions

            tables = soup.find_all('table', {'class': ['table_f', 'table_a']})
            for table in tables:
                for row in table.find_all('tr'):
                    cols = [td.text.strip() for td in row.find_all('td')]
                    for idx, col in enumerate(cols):
                        if '自營商' in col and positions["dealer"] is None:
                            if len(cols) > idx + 5:
                                positions["dealer"] = clean_int(cols[idx + 5])
                        elif '投信' in col and positions["trust"] is None:
                            if len(cols) > idx + 5:
                                positions["trust"] = clean_int(cols[idx + 5])
                        elif '外資' in col and positions["foreign"] is None:
                            if len(cols) > idx + 5:
                                positions["foreign"] = clean_int(cols[idx + 5])
    except Exception as e:
        print(f"[{target_date_str}] Institutional Full position error: {e}")

    return positions