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
