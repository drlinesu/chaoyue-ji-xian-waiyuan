#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
超越次元 (Dimension Anime) - 本地聚合服务
动漫源: AGE动漫 (agefans.com / agedm.io)  漫画源: 拷贝漫画 (copymanga)
纯 Python 标准库实现, 无需安装任何第三方依赖。
仅供个人学习研究使用, 内容均来自第三方公开站点。
"""
import json
import os
import re
import shutil
import ssl
import socket
import sys
import gzip
import time
import hashlib
import zipfile
import hmac
import threading
import subprocess
import collections
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import escape, unescape
from urllib.parse import unquote
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import parse as up
from urllib.request import Request, build_opener, HTTPSHandler, ProxyHandler
from pathlib import Path

# ---- 模块化桥接 (阶段 1: config + cache) ----
from cyjs import config as _cfg
from cyjs import cache as _cache_mod
from cyjs import net as _net_mod
from cyjs import textutil as _tu
from cyjs.sources import ext as _ext_mod
from cyjs.sources import channels as _chan_mod
from cyjs.sources import age as _age_mod
from cyjs.sources import meta as _meta_mod
from cyjs import proxy as _proxy_mod
from cyjs import danmaku as _dm_mod
from cyjs import media as _media_mod
from cyjs import updater as _upd_mod
from cyjs import illust as _illus_mod
from cyjs import kuaikan as _kk_mod
for _m in (_cfg, _cache_mod, _net_mod, _tu, _ext_mod, _chan_mod, _age_mod, _meta_mod, _proxy_mod, _dm_mod, _media_mod, _upd_mod, _illus_mod, _kk_mod):
    globals().update({k: v for k, v in vars(_m).items() if not k.startswith("__")})

# ----------------------------------------------------------------------------
# 基础配置
# ----------------------------------------------------------------------------

# ----------------------------------------------------------------------------
# AGE 动漫域名候选（按顺序探测）
# ----------------------------------------------------------------------------
# 外部聚合片源 (maccms 资源站 JSON API, 直链 m3u8, 作为 AGE 线路之外的补充)
# ----------------------------------------------------------------------------
# (名称, 主 API, 备用镜像 API 列表); 上游域名失联时自动切换并记忆可用域名


def source_self_check():
    """播放源上游手动自检修复: 清失效域名记忆与线路缓存 (含负缓存), 并行探测 AGE 主源与全部外部片源,
    返回各源可达状态 [{name, ok, ms}]; 由播放失败页面的「自检修复」按钮触发, 不影响既有接口"""
    # 1. 清失效记忆: 外部源可用域名记忆 + AGE 域名选择, 下次请求强制重新探测
    _EXT_OK_API.clear()
    global _age_base
    with _base_lock:
        _age_base = ""
    # 2. 清全部外部线路缓存 (含未命中负缓存), 修复后重试才会真正重新拉源
    with _cache_lock:
        stale = [k for k in _cache if k.startswith("ext:lines:")]
        for k in stale:
            del _cache[k]

    def _probe_ext(name, apis):
        t0 = time.time()
        for a in apis:
            try:
                j = json.loads(http_get_text("%s?ac=list&wd=test" % a, timeout=6, tries=1))
                if isinstance(j, dict):   # 收到合法 JSON 即视为源可达 (搜索词无结果也算源活)
                    return {"name": name, "ok": True, "ms": int((time.time() - t0) * 1000)}
            except Exception:
                pass
        return {"name": name, "ok": False, "ms": int((time.time() - t0) * 1000)}

    def _probe_age():
        t0 = time.time()
        try:
            b = age_base(force=True)
            return {"name": "AGE主站", "ok": True, "ms": int((time.time() - t0) * 1000), "base": b}
        except Exception:
            return {"name": "AGE主站", "ok": False, "ms": int((time.time() - t0) * 1000)}

    def _probe_age_chan(name, bases):
        """AGE 类型渠道: 任一域名能返回带 /detail/ 的首页即视为可用"""
        t0 = time.time()
        for b in bases:
            try:
                if "/detail/" in http_get_text(b + "/", timeout=6, tries=1):
                    return {"name": name, "ok": True, "ms": int((time.time() - t0) * 1000)}
            except Exception:
                pass
        return {"name": name, "ok": False, "ms": int((time.time() - t0) * 1000)}

    builtin = list(EXT_VOD_APIS)
    chans = enabled_apis()          # 外部片源渠道 (动漫目录/外部片源 下的 JSON), 不影响内置源
    agechans = age_channels()       # type=age 的外部渠道
    probe_age = not _cfg.NO_AGE     # 外源版不含 AGE 主站, 不探测
    out = {"age": None, "ext": [], "channels": []}
    workers = len(builtin) + len(chans) + len(agechans) + (1 if probe_age else 0)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        fage = pool.submit(_probe_age) if probe_age else None
        fexts = [pool.submit(_probe_ext, n, [a] + list(m)) for n, a, m in builtin]
        fchans = [pool.submit(_probe_ext, n, [a] + list(m)) for n, a, m in chans]
        fagechans = [pool.submit(_probe_age_chan, c["name"], [c["api"]] + list(c["mirrors"]))
                     for c in agechans]
        if fage:
            try:
                out["age"] = fage.result(timeout=25)
            except Exception:
                out["age"] = {"name": "AGE主站", "ok": False, "ms": 0}
        for n_idx, f in enumerate(fexts):
            try:
                out["ext"].append(f.result(timeout=25))
            except Exception:
                out["ext"].append({"name": builtin[n_idx][0], "ok": False, "ms": 0})
        for n_idx, f in enumerate(fchans):
            try:
                out["channels"].append(f.result(timeout=25))
            except Exception:
                out["channels"].append({"name": chans[n_idx][0], "ok": False, "ms": 0})
        for n_idx, f in enumerate(fagechans):
            try:
                out["channels"].append(f.result(timeout=25))
            except Exception:
                out["channels"].append({"name": agechans[n_idx]["name"], "ok": False, "ms": 0})
    out["ok"] = bool((out["age"] or {}).get("ok")) or any(
        x["ok"] for x in out["ext"] + out["channels"])
    return out


def _seed_cache_load():
    """随包种子缓存: 启动瞬间灌入首页/分类/域名的预生成数据
    首次打开直接命中 (无需等抓源), 之后由后台刷新换成最新内容"""
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runtime", "seed_cache.json")
        if not os.path.exists(p):
            return
        with open(p, "r", encoding="utf-8") as f:
            seed = json.load(f)
        for it in seed or []:
            key, val = it.get("key"), it.get("value")
            if not key or val is None:
                continue
            if cache_get(key) is None:          # 已有更新缓存时不覆盖
                cache_put(key, val, int(it.get("ttl") or 1800))
    except Exception:
        pass


def _first_screen_prewarm():
    """首屏预热: 冷缓存下首页要现场抓 AGE 主页 (约 3~4s)。
    启动时后台先抓一次, 用户等浏览器打开的那几秒里缓存已就绪。"""
    try:
        age_home()
    except Exception:
        pass


def _delayed(sec, fn, *args):
    """延后执行: 给首屏留出带宽/CPU, 避免启动瞬间几十个打分请求把首屏拖慢"""
    time.sleep(sec)
    try:
        fn(*args)
    except Exception:
        pass


def main():
    _seed_cache_load()          # 先灌种子缓存, 再挂端口/预热
    threading.Thread(target=_first_screen_prewarm, daemon=True).start()  # 首屏预热与端口绑定并行
    srv = None
    for _ in range(30):   # 端口被旧实例占用时等待重试 (更新重启场景)
        try:
            srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
            break
        except OSError:
            time.sleep(0.5)
    if srv is None:
        raise SystemExit("端口 %d 被占用" % PORT)
    srv.daemon_threads = True
    threading.Thread(target=_delayed, args=(8, _illus_prewarm), daemon=True).start()   # 插画预热: 让首屏先占带宽
    threading.Thread(target=_delayed, args=(120, age_home, True), daemon=True).start()  # 种子缓存 2 分钟后刷新为最新
    threading.Thread(target=_delayed, args=(15, _reco_prewarm), daemon=True).start()    # 推荐数据预热
    threading.Thread(target=_delayed, args=(25, _reco_auto_refresh), daemon=True).start()  # 年度榜 Bangumi 评分定时自动刷新
    threading.Thread(target=_delayed, args=(10, _bgm_reach_probe), daemon=True).start() # bgm.tv 可达性探测 (评分短路)
    threading.Thread(target=_disk_prune, daemon=True).start()      # 磁盘缓存治理 (清过期/超限)
    threading.Thread(target=_appdata_housekeeping, daemon=True).start()  # Edge 配置文件缓存治理 (贮存上限)
    threading.Thread(target=_seg_init, daemon=True).start()        # segcache 启动治理 (清 .tmp 残留 + 强制容量上限)
    threading.Thread(target=_upd_hub_serve, daemon=True).start()
    threading.Thread(target=auto_check_loop, daemon=True).start()   # 启动静默检查更新   # 本机更新源 (独立端口 8966, 管理接口需密钥)
    print("超越次元服务已启动: http://127.0.0.1:%d" % PORT)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


# ---- 兼容注入: AGE 更新表复用的元数据辅助 (age 模块先于这些函数定义加载) ----
def _inject_age_helpers():
    from cyjs.sources import age as _m
    for _n in ("_norm_map_title", "_anilist_graphql", "_bgdata_reverse_map"):
        _f = globals().get(_n)
        if _f is not None:
            setattr(_m, _n, _f)


_inject_age_helpers()


# ---- 启动装配: 把组合根全局注入 web 层, 取回 Handler ----
import cyjs.web as _web_mod
Handler = _web_mod.bind(globals())


if __name__ == "__main__":
    main()
