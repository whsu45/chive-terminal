"""只用標準庫的 HTTP 小工具：重試、UA、Referer、簡單檔案快取。"""
import json
import os
import time
import hashlib
import urllib.request
import urllib.parse
import urllib.error

from config import CACHE_DIR, HTTP_TIMEOUT, USER_AGENT


class HttpError(Exception):
    pass


def _cache_path(key):
    return os.path.join(CACHE_DIR, "http", hashlib.md5(key.encode()).hexdigest() + ".bin")


def get(url, params=None, headers=None, referer=None, retries=2, timeout=HTTP_TIMEOUT, cache_ttl=0):
    """GET 回 bytes。cache_ttl>0 時以秒為單位使用檔案快取（日資料用）。"""
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    key = "GET " + url
    cp = _cache_path(key)
    if cache_ttl > 0 and os.path.exists(cp) and time.time() - os.path.getmtime(cp) < cache_ttl:
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
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
            if cache_ttl > 0:
                os.makedirs(os.path.dirname(cp), exist_ok=True)
                with open(cp, "wb") as f:
                    f.write(data)
            return data
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            last = e
            time.sleep(0.8 * (i + 1))
    raise HttpError(f"{url}: {last}")


def post(url, data, headers=None, referer=None, retries=2, timeout=HTTP_TIMEOUT, cache_ttl=0):
    body = urllib.parse.urlencode(data).encode()
    key = "POST " + url + " " + body.decode()
    cp = _cache_path(key)
    if cache_ttl > 0 and os.path.exists(cp) and time.time() - os.path.getmtime(cp) < cache_ttl:
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
            with urllib.request.urlopen(req, timeout=timeout) as r:
                out = r.read()
            if cache_ttl > 0:
                os.makedirs(os.path.dirname(cp), exist_ok=True)
                with open(cp, "wb") as f:
                    f.write(out)
            return out
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            last = e
            time.sleep(0.8 * (i + 1))
    raise HttpError(f"{url}: {last}")


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
