#!/usr/bin/env python3
"""
盤中通知（2026-11-19 加）：出現建議的進場 / 出場點時，monitor 自己發一聲清脆的「叮」，不用開儀表板網頁、不用一直盯盤。

  進場「叮」（一聲）：▶ 建議做空 / 做多（現價成交或掛單等回）、▶ 掛單成交、你排的單成交
  出場「叮咚」（兩聲，第二聲低一點）：訊號結案（到目標 / 逆向 3 tick / 60 分鐘到時）、撤單警告、
                                    你自己的單到停損 / 到目標 / 13:20 還沒平倉
  被過濾擋掉的、只記錄的觀察（開高放空、⏸ 環境過濾）不出聲。

怎麼發聲（都不用另外安裝東西，也不設排程或開機自動執行）：
  Mac：afplay 播放（系統內建），另外跳系統通知橫幅（osascript display notification；第一次可能要在「系統設定 → 通知」允許）。
  Windows：winsound 播放（Python 內建），終端機印出同一行文字。
  其他系統：有 paplay / aplay 就用，沒有就只印文字。
音效檔第一次用到時用純 Python 產生在 .cache/sounds/（不進 git）。
關掉：環境變數 DAYTRADE_SOUND=0（聲音）、DAYTRADE_BANNER=0（Mac 橫幅），或 monitor.py --no-sound。

    python3 notify.py           # 試聽：先「叮」（進場），再「叮咚」（出場），Mac 會跳一則測試橫幅
"""
import math
import os
import struct
import subprocess
import sys
import threading
import time
import wave

from config import CACHE_DIR

SOUND_DIR = os.path.join(CACHE_DIR, "sounds")
SR = 44100
KINDS = {"entry": "進場「叮」", "exit": "出場「叮咚」", "kd": "KD 共振「叮叮」"}
ENABLED = os.environ.get("DAYTRADE_SOUND", "1") != "0"
BANNER = os.environ.get("DAYTRADE_BANNER", "1") != "0"
SOUND_VER = 2            # 改音色就加 1（快取的音效檔名帶版本）
MIN_GAP = 1.5            # 兩聲之間至少隔幾秒（同時好幾檔出訊號時不要疊在一起變成噪音）
_lock = threading.Lock()
_last_play = 0.0


BRIGHT = [(1.0, 1.0), (2.0, 0.32), (3.01, 0.12), (4.17, 0.05)]     # 清脆：泛音多
DEEP = [(1.0, 1.0), (2.0, 0.18), (3.0, 0.05)]                         # 低沉：泛音少、以基音為主


def _bell(f0, secs, amp=1.0, parts=BRIGHT, tau=0.32):
    """鈴聲：基音 + 幾個泛音，泛音衰減得比較快；2 ms 起音避免爆音。tau 越大餘音越長。"""
    n = int(SR * secs)
    out = []
    for i in range(n):
        t = i / SR
        att = min(1.0, t / 0.002)
        v = 0.0
        for k, (mul, a) in enumerate(parts):
            v += a * math.exp(-t / (tau / (1 + 0.7 * k))) * math.sin(2 * math.pi * f0 * mul * t)
        out.append(amp * att * v)
    return out


def render(kind):
    """產生音效 → [-1, 1] 的樣本。
    進場：A6（1760 Hz）一聲，清脆。出場：G5（784 Hz）接 C5（523 Hz）的叮咚，低沉、泛音少、餘音長一點（2026-11-19 使用者要出場低沉一點）。"""
    if kind == "entry":
        s = _bell(1760.0, 0.9)
    elif kind == "kd":
        # KD 共振（2026-11-26 加）：E6 → G6 兩聲短促的「叮叮」，跟進場的單聲「叮」分得出來；只是加分提醒，不是進場點
        a = _bell(1318.5, 0.35, tau=0.12)
        b = _bell(1568.0, 0.7, tau=0.22)
        off = int(SR * 0.16)
        s = [0.0] * (off + len(b))
        for i, x in enumerate(a):
            s[i] += x
        for i, x in enumerate(b):
            s[off + i] += 0.9 * x
    else:
        a = _bell(784.0, 1.0, parts=DEEP, tau=0.45)
        b = _bell(523.25, 1.3, parts=DEEP, tau=0.55)
        off = int(SR * 0.28)
        s = [0.0] * (off + len(b))
        for i, x in enumerate(a):
            s[i] += x
        for i, x in enumerate(b):
            s[off + i] += 0.9 * x
    peak = max(abs(x) for x in s) or 1.0
    return [x / peak * 0.6 for x in s]                 # 峰值 −4.4 dBFS，清楚但不刺耳


def sound_path(kind):
    path = os.path.join(SOUND_DIR, f"ding_{kind}_v{SOUND_VER}.wav")
    if not os.path.exists(path):
        os.makedirs(SOUND_DIR, exist_ok=True)
        tmp = path + ".tmp"
        with wave.open(tmp, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
            w.writeframes(b"".join(struct.pack("<h", int(x * 32767)) for x in render(kind)))
        os.replace(tmp, path)
    return path


def play(kind):
    """非同步播放，不卡住快迴圈。"""
    path = sound_path(kind)
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["afplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif sys.platform.startswith("win"):
            import winsound
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
        else:
            for player in ("paplay", "aplay"):
                try:
                    subprocess.Popen([player, path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    break
                except FileNotFoundError:
                    continue
    except Exception as e:
        print(f"[通知] 播放失敗：{str(e)[:80]}")


def _as(s):
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def banner(title, msg):
    """Mac 系統通知橫幅（不開網頁也看得到）；其他系統不做。"""
    if not (BANNER and sys.platform == "darwin"):
        return
    try:
        subprocess.Popen(["osascript", "-e", f"display notification {_as(msg[:200])} with title {_as(title[:80])}"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"[通知] 橫幅失敗：{str(e)[:80]}")


def notify(kind, title, msg=""):
    """kind = entry / exit / kd。聲音太密時（MIN_GAP 內）只跳橫幅、不重複發聲；終端機一律印一行。"""
    global _last_play
    print(f"[通知] {({'entry': '🔔', 'kd': '📶'}).get(kind, '🔕')} {title}" + (f"｜{msg}" if msg else ""))
    if ENABLED:
        with _lock:
            now = time.time()
            ok = now - _last_play >= MIN_GAP
            if ok:
                _last_play = now
        if ok:
            play(kind)
    banner(title, msg)


if __name__ == "__main__":
    print("試聽：進場「叮」")
    play("entry")
    time.sleep(1.6)
    print("試聽：出場「叮咚」")
    play("exit")
    time.sleep(2.0)
    print("試聽：KD 共振「叮叮」")
    play("kd")
    time.sleep(0.4)
    banner("▶ 建議做空 1815 富喬 @122.5（測試）", "停損 124.0，目標 119.0｜這是通知測試，不是真的訊號")
    time.sleep(1.5)
    print(f"音效檔：{sound_path('entry')}、{sound_path('exit')}、{sound_path('kd')}")
