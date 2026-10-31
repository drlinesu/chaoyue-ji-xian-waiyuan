# -*- coding: utf-8 -*-
"""内存 TTL 缓存 / 磁盘持久化 / 图片 LRU / 图片失败负缓存"""
import collections
import hashlib
import json
import os
import threading
import time

from .config import ROOT


# 图片代理内存 LRU 缓存 (封面/插画重复加载极多, 命中后零网络延迟)
# ----------------------------------------------------------------------------
_img_cache = collections.OrderedDict()   # url -> (bytes, ctype)
_img_cache_bytes = [0]                   # 当前缓存总字节数
_IMG_CACHE_MAX_ITEMS = 500
_IMG_CACHE_MAX_BYTES = 64 * 1024 * 1024  # 64MB
_img_cache_lock = threading.Lock()


def img_cache_get(key):
    with _img_cache_lock:
        item = _img_cache.get(key)
        if item is None:
            return None
        _img_cache.move_to_end(key)      # LRU: 移到最新
        return item


def img_cache_put(key, data, ctype):
    with _img_cache_lock:
        if key in _img_cache:
            return
        _img_cache[key] = (data, ctype)
        _img_cache_bytes[0] += len(data)
        while (len(_img_cache) > _IMG_CACHE_MAX_ITEMS
               or _img_cache_bytes[0] > _IMG_CACHE_MAX_BYTES):
            _, (old, _) = _img_cache.popitem(last=False)  # 淘汰最旧
            _img_cache_bytes[0] -= len(old)


# ---- 图片失败负缓存 + 占位图: 上游封面挂起时不再拖住浏览器连接 ----
_img_fail = {}
_IMG_FAIL_TTL = 180       # 失败后 3 分钟内直接回占位图
_IMG_TIMEOUT = 5          # 封面单次回源超时 (原 30s 会占满浏览器 6 个连接)
_IMG_PLACEHOLDER = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="400">'
    '<rect width="300" height="400" fill="#f2f0ec"/>'
    '<text x="150" y="200" text-anchor="middle" font-family="sans-serif" '
    'font-size="16" fill="#b9b3a8">封面加载失败</text></svg>'
).encode("utf-8")


def img_fail_get(url):
    now = time.time()
    with _img_cache_lock:
        exp = _img_fail.get(url)
        if exp and exp > now:
            return True
        if exp:
            _img_fail.pop(url, None)
    return False


def img_fail_put(url):
    with _img_cache_lock:
        if len(_img_fail) > 800:
            _img_fail.clear()
        _img_fail[url] = time.time() + _IMG_FAIL_TTL


# 缓存: 内存表 + 长 TTL 条目磁盘持久化 (重启不丢, v2.0.0)
# ----------------------------------------------------------------------------
_cache = {}
_cache_lock = threading.Lock()
_CACHE_MAX_ITEMS = 1500                  # 内存条目上限, 淘汰顺序: 先过期, 再最早写入

_CACHE_DIR = ROOT / "runtime" / "cache"  # 磁盘缓存目录 (安装/打包均排除)
_CACHE_PERSIST_MIN_TTL = 600             # >=10 分钟的条目才落盘
_DISK_MAX_FILES = 400                    # 磁盘缓存文件数上限, 超出按最久未访问淘汰
_disk_writes = [0]


def _disk_path(key):
    return _CACHE_DIR / (hashlib.md5(key.encode("utf-8")).hexdigest()[:20] + ".json")


def _disk_prune():
    """磁盘缓存治理: 清理超期未访问文件; 超上限时按最久未访问淘汰"""
    try:
        if not _CACHE_DIR.exists():
            return
        now = time.time()
        for p in _CACHE_DIR.glob("*.json"):   # 7 天未访问的条目必然已过期 (最长 TTL 24h)
            try:
                if now - p.stat().st_mtime > 7 * 86400:
                    p.unlink()
            except Exception:
                pass
        files = [p for p in _CACHE_DIR.glob("*.json")]
        if len(files) > _DISK_MAX_FILES:
            files.sort(key=lambda p: p.stat().st_mtime)
            for p in files[:len(files) - _DISK_MAX_FILES]:
                try:
                    p.unlink()
                except Exception:
                    pass
    except Exception:
        pass


def _disk_save(key, exp, value):
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        data = json.dumps({"k": key, "exp": exp, "val": value},
                          ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(data) > 8 * 1024 * 1024:       # 单条超 8MB 不落盘
            return
        p = _disk_path(key)
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, p)                    # 原子替换, 读侧不会读到半截文件
        _disk_writes[0] += 1
        if _disk_writes[0] % 25 == 0:
            threading.Thread(target=_disk_prune, daemon=True).start()
    except Exception:
        pass                                  # 序列化失败/磁盘异常时静默, 仅损失持久化


def _disk_load(key):
    """磁盘命中返回 (exp, val); 未命中/已过期/损坏返回 None"""
    try:
        p = _disk_path(key)
        if not p.exists():
            return None
        d = json.loads(p.read_bytes().decode("utf-8"))
        exp, val = d.get("exp", 0), d.get("val")
        if exp <= time.time():
            try:
                p.unlink()
            except Exception:
                pass
            return None
        try:
            os.utime(p, None)                 # 刷新访问时间, 供 LRU 淘汰
        except Exception:
            pass
        return (exp, val)
    except Exception:
        return None


def cache_get(key):
    with _cache_lock:
        item = _cache.get(key)
        if item and item[0] > time.time():
            return item[1]
        if item:
            del _cache[key]
    hit = _disk_load(key)                     # 内存未命中 -> 从磁盘恢复
    if hit:
        with _cache_lock:
            _cache[key] = hit
        return hit[1]
    return None


def cache_drop_prefix(prefix):
    """删除指定前缀的缓存 (内存 + 磁盘), 供切换外部片源渠道后立即失效"""
    with _cache_lock:
        for k in [k for k in _cache if k.startswith(prefix)]:
            _cache.pop(k, None)
    try:
        for p in _CACHE_DIR.glob("*.json"):
            try:
                j = json.loads(p.read_text(encoding="utf-8"))
                if str(j.get("k", "")).startswith(prefix):
                    p.unlink()
            except Exception:
                continue
    except Exception:
        pass


def cache_put(key, value, ttl=300):
    exp = time.time() + ttl
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX_ITEMS:
            now = time.time()
            for k in [k for k, (e, _) in _cache.items() if e <= now]:
                _cache.pop(k, None)
            while len(_cache) >= _CACHE_MAX_ITEMS:
                _cache.pop(next(iter(_cache)))
        _cache[key] = (exp, value)
    if ttl >= _CACHE_PERSIST_MIN_TTL:
        _disk_save(key, exp, value)           # 长 TTL 条目落盘, 重启后仍可命中


# ----------------------------------------------------------------------------
