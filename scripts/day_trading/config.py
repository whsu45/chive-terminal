"""
day_trading 共用設定：路徑、常數、分點名單、族群定義。
所有本機執行的腳本都從這裡讀設定；API 金鑰只從 .env 讀，不寫在程式裡。
"""
import os
import sys
import json

# --- 跨平台：Windows 主控台預設 cp950，印「≥ ⚠ ▶」會直接 UnicodeEncodeError。
#     統一把 stdout/stderr 切成 UTF-8；終端不支援的字元用 ? 取代，不中斷程式。
for _stream in (sys.stdout, sys.stderr):
    try:
        if _stream and hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if os.name == "nt":
    # 讓 Windows Terminal / PowerShell 正確顯示 UTF-8（舊 cmd 也不會當掉，只是部分符號顯示為 ?）
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
CACHE_DIR = os.path.join(HERE, ".cache")          # 已被 .gitignore 的 .cache 規則忽略
LOG_DIR = os.path.join(HERE, "logs")              # *.log 已被 .gitignore 忽略
ENV_FILE = os.path.join(HERE, ".env")             # .env 已被 .gitignore 忽略
GROUPS_FILE = os.path.join(HERE, "groups.json")

os.makedirs(CACHE_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

# --- 裝置 ID：同一份 repo 可能在不同電腦開 monitor，每天的紀錄都會 commit 回 GitHub。
#     各台寫各自的檔（monitor_YYYYMMDD_<id>.jsonl、alert_outcomes_<id>.jsonl…），git 就不會撞；讀的時候再合併。
#     ID 第一次執行時隨機產生、存在 .device_id（已 .gitignore，不能進 git，否則兩台會拿到同一個 ID）。
#     不用電腦名稱，避免把主機名推到公開 repo。想取好認的名字可設環境變數 DEVICE_ID=mac（小寫英數與 -，最多 16 字）。
import re as _re
import glob as _glob
import secrets as _secrets

DEVICE_FILE = os.path.join(HERE, ".device_id")
_DEV_RE = r"[a-z0-9][a-z0-9-]{0,15}"


def _device_id():
    v = (os.environ.get("DEVICE_ID") or "").strip().lower()
    if _re.fullmatch(_DEV_RE, v):
        return v
    try:
        with open(DEVICE_FILE, encoding="utf-8") as f:
            v = f.read().strip().lower()
        if _re.fullmatch(_DEV_RE, v):
            return v
    except OSError:
        pass
    v = _secrets.token_hex(3)
    try:
        with open(DEVICE_FILE, "w", encoding="utf-8") as f:
            f.write(v + "\n")
    except OSError:
        pass
    return v


DEVICE_ID = _device_id()


def dev_path(dirname, stem, ext):
    """這台電腦要寫的檔：<stem>_<裝置ID><ext>。"""
    return os.path.join(dirname, f"{stem}_{DEVICE_ID}{ext}")


def dev_files(dirname, stem, ext):
    """所有裝置的同名檔 [(裝置ID, 路徑)]：沒有裝置 ID 的舊檔（加這機制前寫的）裝置記為 ""，排第一個。"""
    out = []
    legacy = os.path.join(dirname, stem + ext)
    if os.path.exists(legacy):
        out.append(("", legacy))
    pat = _re.compile(_re.escape(stem) + "_(" + _DEV_RE + ")" + _re.escape(ext))
    for p in sorted(_glob.glob(os.path.join(dirname, _glob.escape(stem) + "_*" + ext))):
        m = pat.fullmatch(os.path.basename(p))
        if m:
            out.append((m.group(1), p))
    return out


def read_jsonl_devices(dirname, stem, ext=".jsonl"):
    """讀所有裝置的 jsonl，每列加上 _dev（裝置ID）。壞行跳過。"""
    rows = []
    for dev, p in dev_files(dirname, stem, ext):
        with open(p, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(r, dict):
                    r["_dev"] = dev
                    rows.append(r)
    return rows


def one_device(rows, key):
    """兩台同一天都開了同一檔，紀錄會重複（只是時間點略有差）：同一個 key（通常是 (日期, 代碼)）只留一台的。
    優先留永豐串流（src != mis）筆數多的那台，其次總筆數多的；結果去掉 _dev 欄位。"""
    groups = {}
    for r in rows:
        groups.setdefault(key(r), {}).setdefault(r.get("_dev", ""), []).append(r)
    pick = {k: max(by_dev, key=lambda d: (sum(1 for r in by_dev[d] if r.get("src") != "mis"), len(by_dev[d])))
            for k, by_dev in groups.items()}
    out = [r for r in rows if r.get("_dev", "") == pick[key(r)]]   # 保留原本順序
    for r in out:
        r.pop("_dev", None)
    return out

HTTP_TIMEOUT = 12
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36"

# MoneyDJ 官網已導向 SSO，改走券商鏡站；Referer 一定要帶。
MONEYDJ_MIRRORS = [
    "http://5850web.moneydj.com",
    "https://fubon-ebrokerdj.fbs.com.tw",
    "https://jdata.yuanta.com.tw",
]

# 隔日沖內資分點（買一天、隔天倒貨）。名稱要與 MoneyDJ 分點頁顯示一致，供 zco 頁比對。
DAYTRADE_BRANCHES = {
    "凱基-城中": ("9200", "9227"),
    "凱基-台北": ("9200", "9268"),
    "凱基-松山": ("9200", "9217"),
    "凱基-市政": ("9200", "9239"),
    "凱基-虎尾": ("9200", "9236"),
    "富邦-建國": ("9600", "9658"),
    "富邦-虎尾": ("9600", "9697"),
    "元大-土城永寧": ("9800", "9875"),
}
# 外資主力券商（總公司層級）。
FOREIGN_BROKERS = {
    "摩根士丹利": "1470",
    "美林": "1440",
    "摩根大通": "8440",
    "美商高盛": "1480",
    "新加坡商瑞銀": "1650",
    "港商野村": "1560",
    "台灣摩根士丹利": "1470",
    "港商麥格理": "1360",
    "花旗環球": "1590",
    "瑞士信貸": "1650",
}
# zco 頁名稱比對用：只要券商名稱包含這些關鍵字就視為外資。
FOREIGN_KEYWORDS = ["摩根", "美林", "高盛", "瑞銀", "野村", "麥格理", "花旗", "瑞士信貸", "巴克萊", "德意志", "法銀巴黎", "匯豐", "大和", "新加坡商", "港商", "美商", "台灣摩根"]


def load_env(path=ENV_FILE):
    """讀 .env（KEY=VALUE，# 為註解）。只注入尚未存在的環境變數。"""
    if not os.path.exists(path):
        return {}
    out = {}
    with open(path, "r", encoding="utf-8-sig") as f:  # utf-8-sig：吃掉 Windows 記事本加的 BOM
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            out[k] = v
            os.environ.setdefault(k, v)
    return out


def load_groups():
    """族群定義：{"族群名": ["代碼", ...]}。使用者可自行編輯 groups.json。"""
    if not os.path.exists(GROUPS_FILE):
        return {}
    with open(GROUPS_FILE, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def find_groups_for(code, groups=None):
    groups = groups if groups is not None else load_groups()
    return [g for g, members in groups.items() if code in members]
