# -*- coding: utf-8 -*-
"""随机插画: 多图源聚合 + 缓存预热"""
import json
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib import parse as up
from urllib.request import Request

from .config import UA, _opener
from .net import _safe_url
from .cache import cache_get, cache_put


# ----------------------------------------------------------------------------
# 随机动漫插图 (多公开图源, 免登录; 首页画廊 + 随机背景共用)
# ----------------------------------------------------------------------------
ILLUS_APIS = [
    ("dmoe", "https://www.dmoe.cc/random.php"),
    ("paugram", "https://api.paugram.com/wallpaper/"),
    ("alcy", "https://t.alcy.cc/ycy"),
    ("ixiaowai", "https://api.ixiaowai.cn/api/api.php"),
    ("paulzzh", "https://img.paulzzh.com/touhou/random"),
    ("picre", "https://pic.re/image"),
    ("shoujo", "https://api.shoujo.icu/random"),
]


def fetch_illus(n=12, refresh=False):
    """并行抓取 n 张随机动漫插图的最终直链 (302 跟随后的稳定 URL), 缓存 5 分钟"""
    key = "illus:%d" % int(n or 12)
    if not refresh:
        hit = cache_get(key)
        if hit:
            return hit
    n = max(4, min(int(n or 12), 24))
    deadline = time.time() + 5.0
    got, seen = [], set()

    def _one(i):
        name, api_url = ILLUS_APIS[i % len(ILLUS_APIS)]
        try:
            req = Request(_safe_url(api_url),
                          headers={"User-Agent": UA, "Accept": "image/*,*/*"})
            with _opener.open(req, timeout=6) as resp:
                ctype = resp.headers.get("Content-Type", "")
                final = resp.geturl()
                if "image" not in ctype and not re.search(
                        r"\.(jpe?g|png|webp|gif)", final, re.I):
                    return None
                resp.read(256)  # 只读少量字节确认可用, 不下载整图
            if final == api_url:
                final += ("&" if "?" in final else "?") + "_r=%d_%d" % (int(time.time() * 1000), i)
            return {"url": final, "src": name}
        except Exception:
            return None

    # 并行抓取 2n 个候选, 去重后取前 n 个 (串行逐个抓会非常慢)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_one, range(n * 2)))
    got, seen = [], set()
    for r in results:
        if r and r["url"] not in seen:
            seen.add(r["url"])
            got.append(r)
        if len(got) >= n:
            break
    if not got:
        raise RuntimeError("插图源暂时都不可用, 请稍后再试")
    cache_put(key, got, 300)
    return got


def _illus_prewarm():
    """服务启动后后台预抓多批插画, 用户进入画廊页直接命中缓存 (零等待)"""
    for n in (24, 12, 16):
        try:
            fetch_illus(n=n, refresh=False)
        except Exception:
            pass
        time.sleep(2)
    # 每 4 分钟轮换一批新图 (缓存 TTL 5 分钟, 保证用户总能拿到新鲜的)
    while True:
        time.sleep(240)
        try:
            fetch_illus(n=24, refresh=True)
        except Exception:
            pass
