"""只用標準庫的 HTTP 小工具：重試、UA、Referer、簡單檔案快取。"""
import json
import os
import ssl
import time
import hashlib
import urllib.request
import urllib.parse
import urllib.error

from config import CACHE_DIR, HTTP_TIMEOUT, USER_AGENT


class HttpError(Exception):
    pass


def _ssl_context():
    """
    完整驗證憑證，只是多加信任來源：系統憑證庫 + certifi（有裝的話）+ DAYTRADE_CA_BUNDLE / SSL_CERT_FILE 指定的 PEM。
    公司網路 / 防毒做 HTTPS 檢查時會換上自己的根憑證，把那張根憑證加進來才連得到證交所。
    """
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(certifi.where())
    except Exception:
        pass
    for var in ("DAYTRADE_CA_BUNDLE", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        p = os.environ.get(var)
        if p and os.path.exists(p):
            try:
                ctx.load_verify_locations(p)
            except Exception as e:
                print(f"[http] 讀不到 {var}={p}：{e}")
    return ctx


_CTX = _ssl_context()


def _err_text(url, e):
    """SSL 失敗要把原因印完整（原本只印到「<urlopen error [SSL: 」就被截斷）。"""
    reason = getattr(e, "reason", e)
    if isinstance(reason, ssl.SSLError) or isinstance(e, ssl.SSLError):
        host = urllib.parse.urlparse(url).netloc
        return (f"{host} SSL 憑證驗證失敗：{getattr(reason, 'verify_message', None) or reason}。"
                f"多半是公司網路或防毒軟體的 HTTPS 檢查：跟 IT 要公司根憑證，設 DAYTRADE_CA_BUNDLE 指向它（見 README「Windows」段）")
    return f"{url}: {e}"


def _cache_path(key):
    return os.path.join(CACHE_DIR, "http", hashlib.md5(key.encode()).hexdigest() + ".bin")


def _cache_fresh(cp, cache_ttl):
    """檔案快取是否仍可用：在 TTL 內，而且是「今天」寫的。
    日資料（月 K、法人、融資）每個交易日都會多一列，昨天 09:18 抓的月 K 到今天 09:00 仍在 24 小時 TTL 內，
    會讓盤前少掉昨天那根 K（6148 2026-11-05 昨收顯示 40.95，實際 44.2），所以跨日一律重抓。"""
    if cache_ttl <= 0 or not os.path.exists(cp):
        return False
    mt = os.path.getmtime(cp)
    if time.time() - mt >= cache_ttl:
        return False
    return time.localtime(mt).tm_yday == time.localtime().tm_yday and time.localtime(mt).tm_year == time.localtime().tm_year


def get(url, params=None, headers=None, referer=None, retries=2, timeout=HTTP_TIMEOUT, cache_ttl=0):
    """GET 回 bytes。cache_ttl>0 時以秒為單位使用檔案快取（日資料用）。"""
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    key = "GET " + url
    cp = _cache_path(key)
    if _cache_fresh(cp, cache_ttl):
        with open(cp, "rb") as f:
            return f.read()
    h = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if referer:
        h["Referer"] = referer
    if headers:
        h.update(headers)
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
                data = r.read()
            if cache_ttl > 0:
                os.makedirs(os.path.dirname(cp), exist_ok=True)
                with open(cp, "wb") as f:
                    f.write(data)
            return data
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            last = e
            time.sleep(0.8 * (i + 1))
    raise HttpError(_err_text(url, last))


def post(url, data, headers=None, referer=None, retries=2, timeout=HTTP_TIMEOUT, cache_ttl=0):
    body = urllib.parse.urlencode(data).encode()
    key = "POST " + url + " " + body.decode()
    cp = _cache_path(key)
    if _cache_fresh(cp, cache_ttl):
        with open(cp, "rb") as f:
            return f.read()
    h = {"User-Agent": USER_AGENT, "Content-Type": "application/x-www-form-urlencoded"}
    if referer:
        h["Referer"] = referer
    if headers:
        h.update(headers)
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=body, headers=h, method="POST")
            with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
                out = r.read()
            if cache_ttl > 0:
                os.makedirs(os.path.dirname(cp), exist_ok=True)
                with open(cp, "wb") as f:
                    f.write(out)
            return out
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            last = e
            time.sleep(0.8 * (i + 1))
    raise HttpError(_err_text(url, last))


def get_json(url, params=None, **kw):
    """GET 並解析 JSON。MIS 偶爾回「只有空白行、沒有內容」的 200，這種非 JSON 回應會等 1 秒再試一次。"""
    last = None
    for attempt in range(2):
        raw = get(url, params=params, **kw)
        txt = raw.decode("utf-8", errors="ignore").strip()
        try:
            return json.loads(txt)
        except json.JSONDecodeError as e:
            last = HttpError(f"{url}: 非 JSON 回應 ({e}; 長度 {len(raw)})")
            if kw.get("cache_ttl"):
                try:
                    os.remove(_cache_path(url + json.dumps(params or {}, sort_keys=True)))
                except Exception:
                    pass
            time.sleep(1)
    raise last


def to_float(x, default=None):
    if x is None:
        return default
    s = str(x).replace(",", "").replace("+", "").strip()
    if s in ("", "-", "--", "X", "None", "null"):
        return default
    try:
        return float(s)
    except ValueError:
        return default


def to_int(x, default=0):
    v = to_float(x, None)
    return int(v) if v is not None else default
