# -*- coding: utf-8 -*-
"""片源画质探测: master 多档取最高档; 单档 media 用首段大小/时长估算码率"""
import hashlib
import re
from urllib.request import Request

from .config import UA, _opener
from .net import http_get_text
from .cache import cache_get, cache_put


def _head_size(url, referer=None, timeout=4):
    headers = {"User-Agent": UA, "Accept": "*/*", "Connection": "close"}
    if referer:
        headers["Referer"] = referer
    try:
        req = Request(url, headers=headers, method="HEAD")
        with _opener.open(req, timeout=timeout) as r:
            n = int(r.headers.get("Content-Length") or 0)
            if n > 0:
                return n
    except Exception:
        pass
    try:
        h = dict(headers)
        h["Range"] = "bytes=0-262143"
        req = Request(url, headers=h)
        with _opener.open(req, timeout=timeout) as r:
            data = r.read(262144)
            cr = r.headers.get("Content-Range") or ""
            m = re.search(r"/(\d+)", cr)
            return int(m.group(1)) if m else len(data)
    except Exception:
        return 0


def probe(url, referer=None, timeout=5):
    """返回 {height,bitrate,score,label,kind} 或 None; 结果缓存 6 小时"""
    if not url or not str(url).startswith("http"):
        return None
    if "m3u8" not in str(url).lower():
        return None
    key = "quality:v2:" + hashlib.md5(str(url).encode("utf-8", "ignore")).hexdigest()
    hit = cache_get(key)
    if hit is not None:
        return hit
    try:
        text = http_get_text(url, referer=referer, timeout=timeout, max_mb=2)
    except Exception:
        return None

    best = None
    if "#EXT-X-STREAM-INF" in text:
        for m in re.finditer(r"#EXT-X-STREAM-INF:([^\r\n]+)[\r\n]+([^\r\n#]+)", text):
            attrs = m.group(1)
            bm = re.search(r"BANDWIDTH=(\d+)", attrs)
            bw = int(bm.group(1)) if bm else 0
            rm = re.search(r"RESOLUTION=(\d+)x(\d+)", attrs)
            h = int(rm.group(2)) if rm else 0
            w = int(rm.group(1)) if rm else 0
            score = bw + h * 1000
            if best is None or score > best["score"]:
                best = {"height": h, "width": w, "bitrate": bw, "score": score, "kind": "master"}
        if best:
            label = (("%dP · " % best["height"]) if best["height"] else "") + \
                    ("%.1f Mbps" % (best["bitrate"] / 1000000))
            best["label"] = label
    else:
        durs = [float(x) for x in re.findall(r"#EXTINF:([\d.]+)", text)[:3]]
        segs = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
        dur = sum(durs)
        size = _head_size(segs[0], referer) if segs else 0
        if dur > 0 and size > 0:
            bw = int(size * 8 / dur)
            best = {"height": 0, "width": 0, "bitrate": bw, "score": bw, "kind": "media",
                    "label": "≈%.1f Mbps" % (bw / 1000000)}
    if best:
        best["v"] = 2
        cache_put(key, best, 21600)
    return best


def probe_many(urls, referer=None, workers=4):
    from concurrent.futures import ThreadPoolExecutor
    if not urls:
        return {}
    out = {}
    with ThreadPoolExecutor(max_workers=min(workers, len(urls))) as pool:
        futs = {pool.submit(probe, u, referer): u for u in urls}
        for f, u in futs.items():
            try:
                out[u] = f.result()
            except Exception:
                out[u] = None
    return out
