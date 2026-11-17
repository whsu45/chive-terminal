"""
GitHub Actions 抓資料前先等期交所公布「夜盤三大法人」。

期交所的盤後交易時段三大法人（交易口數與契約金額）約每天 06:00 後才公布，
夜盤行情則是 05:00 收盤後就有。GitHub 的排程常延後好幾個小時、早上那幾次常被丟掉，
如果這次剛好在 06:00 前跑，就只抓到夜盤漲跌、法人是 NA，而下一次排程可能 10 點後才來
（2026-11-17 那次 05:59 抓，期交所還沒公布，網站一直缺到 08:45 手動補跑）。

這支只在台灣時間 04:30～06:45 之間跑時才等：先睡到 05:50，之後每 2 分鐘查一次，
查到當天的夜盤法人、確定當天沒有夜盤（休市）或到 06:45 就結束。其他時段直接結束。
不論結果都以 0 結束，不擋後面的步驟。
"""
import os
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402
from modules.utils import get_past_trading_days  # noqa: E402
from modules.taifex_scraper import fetch_institutional_positions_ah, fetch_night_market_data  # noqa: E402

TW = timezone(timedelta(hours=8))
START = (4, 30)       # 這之前跑的不等：離公布還久，交給之後的排程
FIRST_CHECK = (5, 50)  # 期交所約 06:00 後公布，先睡到這裡再開始查
DEADLINE = (6, 45)    # 最晚等到這個時間
POLL_SEC = 120


def _at(now, hm):
    return now.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)


def main():
    now = datetime.now(TW)
    if not (_at(now, START) <= now < _at(now, DEADLINE)):
        print(f"{now:%H:%M} 不在 04:30～06:45，不用等夜盤三大法人")
        return
    deadline = _at(now, DEADLINE)
    # 跟 fetch_and_build.py 同一個日期：平日是今天，週六、週日是下週一（週五夜盤歸到週一，週六清晨公布）
    target = get_past_trading_days(count=1)[0][0]

    first = _at(now, FIRST_CHECK)
    if now < first:
        print(f"{now:%H:%M} 先睡到 {FIRST_CHECK[0]:02d}:{FIRST_CHECK[1]:02d} 再查 {target} 夜盤三大法人")
        time.sleep((first - now).total_seconds())

    session = requests.Session()
    while True:
        now = datetime.now(TW)
        if fetch_institutional_positions_ah(session, target).get("foreign") is not None:
            print(f"{now:%H:%M} 期交所已公布 {target} 夜盤三大法人")
            return
        if fetch_night_market_data(session, target) is None:
            print(f"{now:%H:%M} 查不到 {target} 的夜盤行情（休市，或夜盤歸到別的交易日），不等了")
            return
        if now + timedelta(seconds=POLL_SEC) >= deadline:
            print(f"{now:%H:%M} 等到 {DEADLINE[0]:02d}:{DEADLINE[1]:02d} 期交所還沒公布，先照常抓，交給之後的更新補")
            return
        print(f"{now:%H:%M} {target} 夜盤三大法人還沒公布，{POLL_SEC // 60} 分鐘後再查")
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # 等待只是加分，任何錯誤都不擋後面的抓資料
        print(f"等待夜盤三大法人時出錯，略過：{e}")
