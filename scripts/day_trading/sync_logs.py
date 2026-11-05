#!/usr/bin/env python3
"""
把每天本機拿到的資料 commit 回 GitHub：
  logs/*.jsonl、logs/*.json（monitor 估算紀錄、警示/訊號結果、交戰紀錄）、.cache/daily_*.json（盤前資料）、.cache/calibration_*.json
不碰 .env、.cache/http/（原始 HTTP 快取 450MB）、.cache/ticks/。
    python3 sync_logs.py            # add + commit + push
    python3 sync_logs.py --no-push  # 只 commit
monitor.py 收盤後（13:35）會自動跑一次；環境變數 LOG_SYNC=0 關掉。
"""
import os
import sys
import glob
import subprocess
import datetime as dt

HERE = os.path.dirname(os.path.abspath(__file__))


def git(*args, check=True):
    r = subprocess.run(["git", *args], cwd=HERE, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()[:300]}")
    return r.stdout.strip()


def main(push=True):
    root = git("rev-parse", "--show-toplevel")
    rel = os.path.relpath(HERE, root)
    paths = []
    for pat in ("logs/*.jsonl", "logs/*.json", ".cache/daily_*.json", ".cache/calibration_*.json", "model_params.json"):
        paths += [os.path.relpath(p, root) for p in glob.glob(os.path.join(HERE, pat))]
    if not paths:
        print("沒有資料檔"); return 0
    subprocess.run(["git", "add", "--", *paths], cwd=root, capture_output=True, text=True)
    staged = subprocess.run(["git", "diff", "--cached", "--name-only", "--", rel], cwd=root, capture_output=True, text=True).stdout.split()
    if not staged:
        print("資料沒有變動，不用 commit"); return 0
    today = dt.date.today().isoformat()
    names = sorted({os.path.basename(p) for p in staged})
    msg = f"day_trading 資料 {today}：{', '.join(names[:6])}{'…' if len(names) > 6 else ''}（{len(staged)} 檔）"
    r = subprocess.run(["git", "commit", "-q", "-m", msg], cwd=root, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"commit 失敗：{r.stderr.strip()[:200]}"); return 1
    sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()
    print(f"已 commit {sha}：{msg}")
    if push:
        r = subprocess.run(["git", "push"], cwd=root, capture_output=True, text=True, timeout=120)
        if r.returncode != 0 and ("non-fast-forward" in (r.stderr + r.stdout) or "fetch first" in (r.stderr + r.stdout)):
            # GitHub Actions 每天會自動 commit data/*.json，先把遠端拉下來 rebase 再推
            pr = subprocess.run(["git", "pull", "--rebase", "--autostash", "-q"], cwd=root, capture_output=True, text=True, timeout=180)
            if pr.returncode != 0:
                print(f"遠端有新 commit 且 rebase 失敗，請手動處理：{(pr.stderr or pr.stdout).strip()[:200]}"); return 1
            r = subprocess.run(["git", "push"], cwd=root, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            print(f"push 失敗（commit 已留在本機，下次再推）：{(r.stderr or r.stdout).strip()[:200]}"); return 1
        print("已 push 到 GitHub")
    return 0


if __name__ == "__main__":
    sys.exit(main(push="--no-push" not in sys.argv))
