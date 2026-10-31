# -*- coding: utf-8 -*-
"""外源版内容层: 完全由「外部片源渠道」驱动的首页/搜索/详情/分类/更新表。

渠道两种类型 (见 channels.py):
  maccms -> maccms10 JSON 接口, 线路为 x 直链线路 (ep 即视频直链)
  age    -> AGE 动漫站点, 复用 age.py 的抓取实现, 线路为站点原生线路

本模块同名函数在 age.py 末尾替换掉 AGE 主站实现 (config.NO_AGE=True),
对上层 (web.py / meta.py / media.py) 暴露与 AGE 相同的数据结构, 前端无需改动。
"""
import datetime
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

from ..cache import cache_get, cache_put
from ..textutil import _clean
from .channels import enabled_channels
from .ext import _ext_api_get, _ext_match_score, ext_resolve_play

_CAP_HOME = 80
_CAP_SEARCH = 36
_ID_RE = re.compile(r"^c(\d+):([0-9A-Za-z_\-]+)$")
_CLS_CACHE = {}          # api -> (时间戳, [(type_id, type_name)])
_CLS_TTL = 6 * 3600
_AGE_INFO = "AGE动漫"
# maccms 资源站多为影视综合站: 只保留分类名属于动漫的条目 (首页/搜索/分类共用)
_ANIME_KEYS = ("动漫", "動漫", "番剧", "番劇", "动画", "動畫",
               "国漫", "日漫", "新番", "剧场版", "OVA", "泡面番")
# 只要日漫: 分类/地区命中这些标记即剔除 (国产/欧美/韩台等)
_NON_JP_KEYS = ("国产", "国漫", "大陆", "欧美", "美国", "韩国", "韩漫",
                "港台", "香港", "台湾", "海外", "泰国")


def _is_anime_name(name):
    n = str(name or "")
    return any(k.lower() in n.lower() for k in _ANIME_KEYS)


def _is_jp_name(name):
    """分类名是否指向日本动漫"""
    n = str(name or "")
    if not n or any(k in n for k in _NON_JP_KEYS):
        return False
    return "日" in n


def _anime_tids(apis):
    """渠道中属于日漫的分类 id 集合"""
    return {tid for tid, name in _classes(apis)
            if _is_anime_name(name) and _is_jp_name(name)}


def _keep_anime(it, tids):
    """只保留日本动漫: 分类须为动漫, 地区明确为非日本则剔除"""
    tn = _clean(it.get("type_name") or "")
    area = _clean(it.get("vod_area") or "")
    if tn and not _is_anime_name(tn):
        return False
    jp_cat = _is_jp_name(tn)
    if area:
        if "日本" in area or "日韩" in area:
            return True
        if any(k in area for k in _NON_JP_KEYS):
            return False
        return jp_cat
    if tn:
        return jp_cat
    try:
        tid = int(it.get("type_id"))
    except Exception:
        return False
    return tid in tids



def _sources():
    """[(名称, 类型, [主域, *镜像]), ...] —— 顺序与渠道文件排序一致"""
    return [(c["name"], c.get("type") or "maccms", [c["api"]] + list(c["mirrors"]))
            for c in enabled_channels()]


def _sync_age_bases():
    """把 AGE 渠道配置的域名补进 AGE 域名候选池 (只补新域名, 不改变原有顺序)"""
    from . import age as _age
    chans = [c for c in enabled_channels() if (c.get("type") or "maccms") == "age"]
    extra = []
    for c in chans:
        extra.extend([c["api"]] + list(c["mirrors"]))
    for b in reversed(extra):
        b = str(b or "").strip().rstrip("/")
        if b and b not in _age.AGE_BASES:
            _age.AGE_BASES.insert(0, b)


def _get(apis, qs):
    for a in apis:
        j = _ext_api_get(a, qs)
        if j and (j.get("list") or j.get("class")):
            return j
    return None


def _classes(apis):
    """渠道分类表 (缓存 6 小时), 返回 [(type_id, type_name)]"""
    key = apis[0]
    hit = _CLS_CACHE.get(key)
    if hit and time.time() - hit[0] < _CLS_TTL:
        return hit[1]
    out = []
    for c in (_get(apis, "ac=list&pg=1") or {}).get("class") or []:
        if isinstance(c, dict):
            tid, name = c.get("type_id"), _clean(c.get("type_name") or "")
            if tid and name:
                out.append((int(tid), name))
    if out:
        _CLS_CACHE[key] = (time.time(), out)
    return out


def _item(ci, it):
    """maccms 列表项 -> 前端卡片 {id,title,cover,ep,info}"""
    vid = it.get("vod_id")
    title = _clean(it.get("vod_name") or "")
    if not vid or not title:
        return None
    cover = str(it.get("vod_pic") or "").strip()
    if cover.startswith("//"):
        cover = "https:" + cover
    info = " · ".join(x for x in (_clean(it.get("type_name") or ""),
                                 _clean(it.get("vod_year") or ""),
                                 _clean(it.get("vod_area") or "")) if x)
    return {"id": "c%d:%s" % (ci, vid), "title": title, "cover": cover,
            "ep": _clean(it.get("vod_remarks") or ""), "info": info}


def _collect(rows, cap):
    """多源混排 + 标题去重"""
    out, seen = [], set()
    for it in rows:
        if not it or it["title"] in seen:
            continue
        seen.add(it["title"])
        out.append(it)
        if len(out) >= cap:
            break
    return out


def _detail_pics(apis, items):
    """列表接口常缺封面: 批量 ac=detail 补齐 vod_pic"""
    ids = ",".join(str(it.get("vod_id")) for it in items if it.get("vod_id"))
    if not ids:
        return {}
    j = _get(apis, "ac=detail&ids=" + ids)
    return {str(x.get("vod_id")): str(x.get("vod_pic") or "")
            for x in (j or {}).get("list") or []}


def _maccms_items(ci, apis, qs, kw=""):
    j = _get(apis, qs)
    tids = _anime_tids(apis)
    its = []
    for it in (j or {}).get("list") or []:
        if kw and _ext_match_score(kw, str(it.get("vod_name") or "")) < 1:
            continue
        if not _keep_anime(it, tids):     # 影视综合源: 只要动漫分类
            continue
        its.append(it)
    if its and any(not str(it.get("vod_pic") or "").strip() for it in its):
        pics = _detail_pics(apis, its)     # 一次请求补齐封面
        for it in its:
            if not str(it.get("vod_pic") or "").strip():
                it["vod_pic"] = pics.get(str(it.get("vod_id")), "")
    return [x for x in (_item(ci, it) for it in its) if x]


def _age_items(ci, kw=""):
    """AGE 渠道列表项: 复用 age.py 抓取, id 统一成 c{渠道序号}:{AGE pid}"""
    from . import age as _age
    _sync_age_bases()
    try:
        rows = _age._AGE_SEARCH(kw) if kw else _age._AGE_HOME()
    except Exception:
        return []
    out = []
    for it in rows or []:
        pid = str(it.get("id") or "").strip()
        title = _clean(it.get("title") or "")
        if not pid or not title:
            continue
        if kw and _ext_match_score(kw, title) < 1:
            continue
        info = _AGE_INFO + (" · " + it["info"] if it.get("info") else "")
        out.append({"id": "c%d:%s" % (ci, pid), "title": title,
                    "cover": it.get("cover") or "", "ep": it.get("ep") or "",
                    "info": info})
    return out


def _source_items(ci, typ, apis, qs, kw=""):
    if typ == "age":
        return _age_items(ci, kw)
    return _maccms_items(ci, apis, qs, kw)


def _all_items(qs, kw=""):
    """并行拉取全部渠道, 按渠道顺序拼接"""
    srcs = _sources()
    if not srcs:
        return []
    with ThreadPoolExecutor(max_workers=max(1, len(srcs))) as pool:
        futs = [pool.submit(_source_items, ci, typ, apis, qs, kw)
                for ci, (_, typ, apis) in enumerate(srcs)]
        rows = []
        for f in futs:
            try:
                rows.extend(f.result())
            except Exception:
                pass
    return rows


def ch_home(refresh=False):
    key = "vodch:home"
    if not refresh:
        hit = cache_get(key)
        if hit:
            return hit
    items = _collect(_all_items("ac=list&pg=1"), _CAP_HOME)
    cache_put(key, items, 1200 if items else 60)
    return items


def ch_search(kw):
    kw = _clean(kw)
    if not kw:
        return []
    key = "vodch:search:" + kw
    hit = cache_get(key)
    if hit:
        return hit
    items = _collect(_all_items("ac=list&wd=" + quote(kw), kw), _CAP_SEARCH)
    cache_put(key, items, 1800 if items else 300)
    return items


def ch_update():
    """无 AGE 更新表: 用各渠道最新列表作为「全部」兜底"""
    return {"groups": {}, "all": ch_home()[:60],
            "today": datetime.date.today().isoweekday(),
            "weekdays": ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]}


def _age_catalog(ci, page, name, order):
    """只有 AGE 渠道时: 直接用 AGE 的分类目录"""
    from . import age as _age
    _sync_age_bases()
    try:
        d = _age._AGE_CATALOG(page, name, order)
    except Exception:
        d = {}
    items = []
    for x in d.get("items") or []:
        pid = str(x.get("id") or "").strip()
        if not pid:
            continue
        items.append({"id": "c%d:%s" % (ci, pid), "title": _clean(x.get("title") or ""),
                      "cover": x.get("cover") or "", "ep": x.get("ep") or "",
                      "info": _AGE_INFO})
    return {"items": _collect(items, 60), "page": page,
            "has_more": bool(d.get("has_more")), "genre": name,
            "genres": [list(g) for g in (d.get("genres") or _age.AGE_GENRES)],
            "years": [list(y) for y in (d.get("years") or [])],
            "regions": [list(r) for r in (d.get("regions") or [])],
            "status": [list(s) for s in (d.get("status") or [])],
            "orders": [list(o) for o in (d.get("orders") or [["time", "最新"]])]}


def ch_catalog(page=1, genre=0, order="time"):
    page = max(1, int(page or 1))
    name = _clean(genre) if genre and str(genre) != "0" else ""
    key = "vodch:catalog:%s:%s:%s" % (name, page, order)
    hit = cache_get(key)
    if hit:
        return hit
    srcs = _sources()
    macc = [(ci, apis) for ci, (_, typ, apis) in enumerate(srcs) if typ != "age"]
    if not macc:
        age_ci = next((ci for ci, (_, typ, _a) in enumerate(srcs) if typ == "age"), None)
        if age_ci is not None:
            data = _age_catalog(age_ci, page, name, order)
        else:
            data = {"items": [], "page": page, "has_more": False, "genre": name,
                    "genres": [], "years": [], "regions": [], "status": [],
                    "orders": [["time", "最新"], ["hot", "热门"]]}
        cache_put(key, data, 300)
        return data
    rows, has_more, genres = [], False, []
    genres = [(n, n) for _, n in _classes(macc[0][1]) if _is_anime_name(n)]   # 只展示动漫分类
    with ThreadPoolExecutor(max_workers=max(1, len(macc))) as pool:
        futs, idx, tids = [], [], []
        for ci, apis in macc:
            tid = next((i for i, n in _classes(apis) if n == name), None) if name else None
            q = ("ac=list&t=%d&pg=%d" % (tid, page)) if tid else ("ac=list&pg=%d" % page)
            futs.append(pool.submit(_get, apis, q))
            idx.append(ci)
            tids.append(_anime_tids(apis))
        for f, ci, atids in zip(futs, idx, tids):
            j = f.result() or {}
            try:
                if int(j.get("pagecount") or 1) > page:
                    has_more = True
            except Exception:
                pass
            for it in j.get("list") or []:
                if not _keep_anime(it, atids):    # 影视综合源: 只要动漫分类
                    continue
                rows.append(_item(ci, it))
    items = _collect(rows, 60)
    data = {"items": items, "page": page, "has_more": has_more,
            "genre": name, "genres": genres,
            "years": [], "regions": [], "status": [],
            "orders": [["time", "最新"], ["hot", "热门"]]}
    cache_put(key, data, 300)
    return data


def _age_detail(ci, cid, pid):
    """AGE 渠道详情: 复用 age.py 解析, 线路为站点原生线路 (line 为数字)"""
    key = "vodch:detail:" + cid
    hit = cache_get(key)
    if hit:
        return hit
    from . import age as _age
    _sync_age_bases()
    d = _age._AGE_DETAIL(pid)
    lines = []
    for ln in d.get("lines") or []:
        eps = []
        for e in ln.get("episodes") or []:
            name = _clean(e.get("name") or "")
            if not name:
                continue
            item = {"ep": str(e.get("ep") or ""), "name": name}
            if e.get("pid"):
                item["pid"] = str(e["pid"])
            eps.append(item)
        if eps:
            no = len(lines) + 1
            lines.append({"line": str(ln.get("line") or no),
                          "name": _clean(ln.get("name") or "") or ("线路%d" % no),
                          "episodes": eps})
    data = {"id": cid, "title": _clean(d.get("title") or ""),
            "cover": d.get("cover") or "", "region": _clean(d.get("region") or ""),
            "year": _clean(d.get("year") or ""), "status": _clean(d.get("status") or ""),
            "types": _clean(d.get("types") or ""),
            "intro": _clean(d.get("intro") or "")[:600], "lines": lines}
    if not data["title"]:
        raise RuntimeError("AGE 渠道详情解析失败")
    cache_put(key, data, 600)
    return data


def ch_detail(cid):
    """id 形如 c0:123 (渠道序号:番剧 id; AGE 渠道的 id 即站点 pid)"""
    cid = str(cid or "")
    m = _ID_RE.match(cid)
    if not m:
        raise RuntimeError("外源版只支持渠道番剧, 请从首页/搜索进入")
    ci, vid = int(m.group(1)), m.group(2)
    srcs = _sources()
    if ci >= len(srcs):
        raise RuntimeError("片源渠道已变更, 请返回首页重新选择")
    if srcs[ci][1] == "age":
        return _age_detail(ci, cid, vid)
    key = "vodch:detail:" + cid
    hit = cache_get(key)
    if hit:
        return hit
    j = _get(srcs[ci][2], "ac=detail&ids=%s" % vid)
    lst = (j or {}).get("list") or []
    if not lst:
        raise RuntimeError("该片源暂无此条目")
    it = lst[0]
    intro = _clean(re.sub(r"<[^>]+>", " ", str(it.get("vod_content") or "")))[:600]
    data = {"id": cid, "title": _clean(it.get("vod_name") or ""),
            "cover": str(it.get("vod_pic") or "").strip(),
            "region": _clean(it.get("vod_area") or ""),
            "year": _clean(it.get("vod_year") or ""),
            "status": _clean(it.get("vod_remarks") or ""),
            "types": _clean(it.get("type_name") or ""),
            "intro": intro, "lines": []}
    cache_put(key, data, 600)
    return data


def ch_resolve_play(cid, line, ep):
    """maccms 渠道为 x 线路 (ep 即直链); AGE 渠道走站点原生线路"""
    srcs = _sources()
    m = _ID_RE.match(str(cid or ""))
    if m and int(m.group(1)) < len(srcs) and srcs[int(m.group(1))][1] == "age":
        from . import age as _age
        _sync_age_bases()
        return _age._AGE_PLAY(m.group(2), line, ep)
    if not str(line).startswith("x"):
        raise RuntimeError("该片源不含此线路, 请切换片源线路")
    return ext_resolve_play(line, ep)


def class_names():
    """AGE_TAGS 替代: 外源版不预置标签 (分类由渠道 class 动态返回)"""
    return []
