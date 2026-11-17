#!/usr/bin/env python3
"""
本機準時補觸發 deploy.yml，讓網站開盤前一定有「三大法人淨額（夜盤）」。

GitHub 的排程常常延後或被丟掉：10/01～11/17 排了約 280 次只跑了 100 次，
早上 6～9 點準時跑的幾乎沒有，網站的夜盤法人常要等到 10 點後才補上。
這支給本機排程（macOS launchd，見 scripts/launchd/install_kick_deploy.sh）早上跑幾次，
三個條件都成立才用 gh 觸發一次 deploy.yml（workflow_dispatch 會馬上跑，不受排程延後影響）：
  1. repo 裡的 data/history.json 還沒有今天的夜盤三大法人
  2. 期交所已經公布了（約 06:00 後）
  3. 沒有 deploy 正在排隊或執行
不成立就什麼都不做，所以一天跑好幾次、或電腦醒來補跑都沒關係。
只用 Python 標準函式庫和已登入的 gh CLI，不讀也不存任何金鑰。

用法：python3 scripts/kick_deploy.py [--dry-run]   （--dry-run 只檢查、不觸發）
"""
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TW = timezone(timedelta(hours=8))
AH_URL = "https://www.taifex.com.tw/cht/3/futContractsDateAh"
ACTIVE = {"queued", "in_progress", "waiting", "requested", "pending"}


def target_date(now):
    """ 跟 fetch_and_build.py 同一個日期：平日是今天；週六、週日是下週一（週五夜盤歸到週一，週六清晨公布） """
    d = now
    if d.weekday() == 5:
        d += timedelta(days=2)
    elif d.weekday() == 6:
        d += timedelta(days=1)
    return d.strftime("%Y/%m/%d")


def gh(*args):
    r = subprocess.run(["gh", *args], cwd=REPO_DIR, capture_output=True, text=True,
                       encoding="utf-8", timeout=90)
    if r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:2])} 失敗：{r.stderr.strip()[:200]}")
    return r.stdout


def site_has_ah(target):
    """ 直接讀 repo 上的 data/history.json（不讀 Pages：Pages 有 10 分鐘快取，剛更新完會誤判） """
    raw = gh("api", "-H", "Accept: application/vnd.github.raw",
             "repos/{owner}/{repo}/contents/data/history.json")
    rec = next((x for x in json.loads(raw) if x.get("date") == target), None)
    return rec is not None and isinstance(rec.get("foreign_net_ah"), int)


def taifex_published(target):
    """ 期交所盤後三大法人頁：已公布時表格上方有「日期2026/11/17」；還沒公布或休市就沒有這格 """
    payload = urllib.parse.urlencode({
        "queryType": "1", "goDay": "", "doQuery": "1", "dateaddcnt": "",
        "queryDate": target, "commodityId": "TXF", "button": "送出查詢",
    }).encode()
    req = urllib.request.Request(AH_URL, data=payload,
                                 headers={"User-Agent": "Mozilla/5.0", "Referer": AH_URL})
    with urllib.request.urlopen(req, timeout=30) as resp:
        html = resp.read().decode("utf-8", "replace")
    m = re.search(r">\s*日期\s*(\d{4})/(\d{1,2})/(\d{1,2})\s*<", html)
    if not m:
        return False
    return f"{m.group(1)}/{int(m.group(2)):02d}/{int(m.group(3)):02d}" == target


def deploy_running():
    runs = json.loads(gh("run", "list", "--workflow", "deploy.yml", "--limit", "5", "--json", "status") or "[]")
    return any(r.get("status") in ACTIVE for r in runs)


def main():
    dry = "--dry-run" in sys.argv[1:]
    now = datetime.now(TW)
    target = target_date(now)
    tag = f"[{now:%Y-%m-%d %H:%M}] {target}"
    try:
        if site_has_ah(target):
            print(f"{tag} 網站已有夜盤三大法人，不用觸發")
            return 0
        if not taifex_published(target):
            print(f"{tag} 期交所還沒公布夜盤三大法人（或休市），等下一次")
            return 0
        if deploy_running():
            print(f"{tag} 已經有 deploy 在排隊或執行，不重複觸發")
            return 0
        if dry:
            print(f"{tag} 網站缺夜盤三大法人、期交所已公布 → 會觸發 deploy.yml（--dry-run，沒有真的觸發）")
            return 0
        gh("workflow", "run", "deploy.yml")
        print(f"{tag} 網站缺夜盤三大法人、期交所已公布 → 已觸發 deploy.yml")
        return 0
    except Exception as e:
        print(f"{tag} 檢查或觸發失敗：{e}")
        return 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
