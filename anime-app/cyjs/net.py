# -*- coding: utf-8 -*-
"""HTTP 客户端 / gzip / 重试 / AGE 全局限速"""
import gzip
import re
import threading
import time
from urllib import parse as up
from urllib.request import Request

from .config import UA, _opener


# AGE 上游全局限速: 所有页面请求最小间隔, 排队执行, 防止密集请求触发站点限流
_AGE_HOSTS = ("agefans", "agedm.io", "agemys")
_age_throttle_lock = threading.Lock()
_age_last_req = [0.0]
_AGE_MIN_INTERVAL = 0.7


def _age_throttle_wait():
    with _age_throttle_lock:
        delta = time.time() - _age_last_req[0]
        if delta < _AGE_MIN_INTERVAL:
            time.sleep(_AGE_MIN_INTERVAL - delta)
        _age_last_req[0] = time.time()


def _safe_url(u):
    """URL 中含非 ASCII 字符时进行百分号编码 (如 .../第01集/index.m3u8)"""
    try:
        u.encode("ascii")
        return u
    except UnicodeEncodeError:
        return up.quote(u, safe="!#$&'()*+,/:;=?@[]%~")


def http_get(url, referer=None, timeout=15, max_mb=20, extra_headers=None, tries=1):
    """带 UA / gzip 的 GET, 返回 (bytes, content_type); 连接级失败自动重试"""
    url = _safe_url(url)
    if any(h in url for h in _AGE_HOSTS):
        _age_throttle_wait()   # AGE 站点限流防护: 全局排队最小间隔
    headers = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "close",
    }
    if referer:
        headers["Referer"] = referer
    if extra_headers:
        headers.update(extra_headers)
    err = None
    for attempt in range(max(1, int(tries or 1))):
        req = Request(url, headers=headers)
        try:
            with _opener.open(req, timeout=timeout) as resp:
                data = resp.read(1024 * 1024 * max_mb)
                ctype = resp.headers.get("Content-Type", "")
            err = None
            break
        except OSError as e:
            # 上游网络错误 (连接重置/超时/SSL) 统一包装为 RuntimeError,
            # 避免与"客户端断开"的 ConnectionResetError 混淆而被静默吞掉
            err = RuntimeError("上游连接失败: %s: %s" % (type(e).__name__, e))
            err.__cause__ = e
            if attempt + 1 < max(1, int(tries or 1)):
                time.sleep(0.4)
    if err is not None:
        raise err
    if resp.headers.get("Content-Encoding") == "gzip" or data[:2] == b"\x1f\x8b":
        try:
            data = gzip.decompress(data)
        except Exception:
            pass
    return data, ctype


def http_get_text(url, **kw):
    kw.setdefault("tries", 2)   # 文本/接口/清单类请求默认自动重试一次
    data, ctype = http_get(url, **kw)
    charset = "utf-8"
    m = re.search(r"charset=([\w-]+)", ctype, re.I)
    if m:
        charset = m.group(1)
    else:
        m2 = re.search(rb'charset=["\']?([\w-]+)', data[:2048], re.I)
        if m2:
            charset = m2.group(1).decode("ascii", "ignore")
    try:
        return data.decode(charset, "replace")
    except LookupError:
        return data.decode("utf-8", "replace")
