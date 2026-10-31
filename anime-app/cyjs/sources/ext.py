# -*- coding: utf-8 -*-
"""maccms 聚合片源: 搜索/展开剧集/域名失效记忆/广告标记"""
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib import parse as up

from ..textutil import _clean, _BG_SEASON_RE, _bgm_title_variants
from ..cache import cache_get, cache_put
from ..net import http_get_text
from ..quality import probe_many
from .channels import enabled_apis


EXT_VOD_APIS = []   # 外源版: 内置片源已全部移除, 仅使用「动漫文件夹/外部片源」中的渠道
_EXT_LINE_PREFIX = "x"          # 外部线路 id 形如 x0/x1/x2/x3
_EXT_SEASON_RE = _BG_SEASON_RE

# 广告/水印源标记: 默认排序与播放均优先无广告线路
_AD_HOSTS = ("bfvvs.com", "ymuuy.com")      # 片头/画面烤入 "71305" 等水印的 CDN
_AD_PROVIDERS = ("量子片源",)                # 正片上方滚动贴片广告


def _is_ad_url(url):
    try:
        host = (up.urlparse(str(url or "")).netloc or "").lower().split(":")[0]
    except Exception:
        return False
    return any(host == h or host.endswith("." + h) for h in _AD_HOSTS)


def _line_is_ad(name, episodes):
    if name in _AD_PROVIDERS:
        return True
    eps = episodes or []
    return bool(eps and _is_ad_url(eps[0].get("ep") or ""))


def _ext_norm_title(s):
    s = _clean(s).lower()
    return re.sub(r"[\s:：·.,，。!！?？~～'’\"“”()\[\]【】\-—_]", "", s)


def _ext_match_score(kw, vod_name):
    """标题匹配度: 4 全等 / 3 包含 / 2 去季数后相等 / 1 去季数后包含 / 0 不匹配"""
    t, v = _ext_norm_title(kw), _ext_norm_title(vod_name)
    if not t or not v:
        return 0
    if t == v:
        return 4
    if t in v or v in t:
        return 3
    t0 = _EXT_SEASON_RE.sub("", t).strip(" -·")
    v0 = _EXT_SEASON_RE.sub("", v).strip(" -·")
    if t0 and v0:
        if t0 == v0:
            return 2
        if t0 in v0 or v0 in t0:
            return 1
    return 0


_EXT_OK_API = {}          # 源名 -> (可用api, 时间戳), 30 分钟内优先复用已验证域名
_EXT_API_TTL = 1800


def _ext_api_get(api, qs, timeout=10):
    """请求 maccms 接口并解析 JSON, 失败返回 None"""
    try:
        j = json.loads(http_get_text("%s?%s" % (api, qs), timeout=timeout))
        return j if isinstance(j, dict) else None
    except Exception:
        return None


def _ext_fetch_source(name, apis, title):
    """搜索单个资源站 (apis: [主域, *镜像], 失联自动切换), 返回 {line,name,episodes} 或 None"""
    qs = "ac=list&wd=%s" % up.quote(title)
    # 优先复用上次验证可用的域名, 其余按 主域->镜像 顺序尝试
    ok = _EXT_OK_API.get(name)
    cand, seen = [], set()
    if ok and time.time() - ok[1] < _EXT_API_TTL and ok[0] in apis:
        cand.append(ok[0])
        seen.add(ok[0])
    for a in apis:
        if a not in seen:
            cand.append(a)
            seen.add(a)
    j, winner = None, None
    for api in cand:
        j = _ext_api_get(api, qs)
        if j and j.get("list"):
            winner = api
            break
        j = None
    if not winner:
        return None
    _EXT_OK_API[name] = (winner, time.time())
    items = j.get("list") or []
    best, best_score = None, 0
    for it in items:
        sc = _ext_match_score(title, str(it.get("vod_name", "")))
        if sc > best_score:
            best, best_score = it, sc
    if not best or best_score < 2:   # 至少去季数后相等, 避免乱匹配
        return None
    d2 = _ext_api_get(winner, "ac=detail&ids=%s" % best.get("vod_id"))
    lst = (d2 or {}).get("list") or []
    if not lst:
        return None
    play_url = str(lst[0].get("vod_play_url", ""))
    # vod_play_url: "线路1#第01集$http://...m3u8#第02集$http://...$$$线路2#..."
    groups = play_url.split("$$$")
    best_eps = []
    for grp in groups:
        seg = grp.split("#", 1)[1] if "#" in grp else grp
        eps, seen = [], set()
        for pair in seg.split("#"):
            if "$" not in pair:
                continue
            en, eu = pair.split("$", 1)
            eu = eu.strip()
            if not eu.startswith("http") or not (".m3u8" in eu or ".mp4" in eu):
                continue
            en = _clean(en) or "第?集"
            if en in seen:
                continue
            seen.add(en)
            eps.append({"ep": eu, "name": en})
        if len(eps) > len(best_eps):
            best_eps = eps
    if not best_eps:
        return None
    return {"line": "", "name": name, "episodes": best_eps}


def _rank_ext(lines):
    """按实测画质排序 (高->低), 同画质再按无广告/集数; 缺 quality 的先探测"""
    todo = {}
    for l in lines:
        eps = l.get("episodes") or []
        if eps and (not l.get("quality") or l["quality"].get("v") != 2):
            u = eps[0].get("ep")
            if u:
                todo[u] = l
    if todo:
        qs = probe_many(list(todo))
        for u, l in todo.items():
            q = qs.get(u)
            if q:
                l["quality"] = q
    lines.sort(key=lambda x: (-(x.get("quality") or {}).get("score", 0),
                              1 if x.get("ad") else 0,
                              -len(x.get("episodes") or [])))
    return lines


def _all_vod_apis():
    """内置片源 + 外部渠道 (渠道追加在末尾, 内置线路编号 x0..x3 保持不变)"""
    return list(EXT_VOD_APIS) + enabled_apis()


def _ext_fetch_all(title):
    """并行聚合所有外部片源拉取一次, 返回 [{line,name,episodes:[{ep(直链url),name}]}]"""
    apis = _all_vod_apis()
    out = []
    with ThreadPoolExecutor(max_workers=max(1, len(apis))) as pool:
        futs = [pool.submit(_ext_fetch_source, n, [a] + list(m), title)
                for n, a, m in apis]
        for i, f in enumerate(futs):
            try:
                r = f.result()
            except Exception:
                r = None
            if r and r.get("episodes"):
                r["line"] = _EXT_LINE_PREFIX + str(i)
                r["ad"] = _line_is_ad(r.get("name") or "", r.get("episodes"))
                out.append(r)
    # 画质优先 (高->低), 同画质再考虑广告与集数
    return _rank_ext(out)


def ext_lines(title):
    """并行聚合所有外部片源, 返回 [{line:'x0',name,episodes:[{ep(直链url),name}]}]"""
    title = _clean(title)
    if not title:
        return []
    key = "ext:lines:" + title
    hit = cache_get(key)
    if hit is not None:
        for l in hit:                     # 旧缓存补齐 ad 标记并重排
            if "ad" not in l:
                l["ad"] = _line_is_ad(l.get("name") or "", l.get("episodes"))
        if any((not l.get("quality")) or l["quality"].get("v") != 2 for l in hit):
            _rank_ext(hit)
            cache_put(key, hit, 21600)
        return hit
    out = _ext_fetch_all(title)
    if not out:
        # 带季数后缀的完整标题 (如 "XX 第三季 Part.2") 外部源站常搜不到,
        # 逐级剥离后缀用主标题重搜, 提高源+ 线路命中率
        for v in _bgm_title_variants(title)[1:]:
            out = _ext_fetch_all(v)
            if out:
                break
    cache_put(key, out, 21600 if out else 3600)   # 命中6小时, 未命中1小时
    return out


def ext_resolve_play(line, ep):
    """外部线路: ep 参数即完整视频直链"""
    ep_url = _clean(ep)
    if not ep_url.startswith("http") or ".m3u8" not in ep_url and ".mp4" not in ep_url:
        raise RuntimeError("无效的片源地址")
    return {"url": ep_url, "via": "ext", "line": str(line)}

