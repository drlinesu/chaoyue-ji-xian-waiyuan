# -*- coding: utf-8 -*-
"""快看漫画: 搜索/详情/章节 + Nuxt SSR 数据解析"""
import json
import re
import time
from urllib import parse as up

from .cache import cache_get, cache_put
from .net import http_get
from .sources.age import KK_API, KK_WEB


# ----------------------------------------------------------------------------
# 快看漫画源（官方平台: 搜索JSON API + SSR页面数据, 图片为带签名直链）
# ----------------------------------------------------------------------------
_UA_M = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"


def _nuxt_seg(html):
    m = re.search(r'window\.__NUXT__\s*=\s*(.*?)</script>', html, re.S)
    return m.group(1) if m else None


def _split_top(s):
    """按顶层逗号切分（忽略字符串与括号内的逗号）"""
    out, depth, cur, i, instr = [], 0, "", 0, None
    while i < len(s):
        ch = s[i]
        if instr:
            cur += ch
            if ch == instr and s[i - 1] != "\\":
                instr = None
        elif ch in "\"'":
            cur += ch
            instr = ch
        elif ch in "([{":
            depth += 1
            cur += ch
        elif ch in ")]}":
            depth -= 1
            cur += ch
        elif ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
        i += 1
    if cur.strip():
        out.append(cur.strip())
    return out


def _nuxt_map(seg):
    """解析 __NUXT__=(function(a,b,...){...}(v1,v2,...)) 的参数名→值 映射"""
    sig_ix = seg.find("function(")
    if sig_ix < 0:
        return {}
    params = seg[sig_ix + 9:seg.find(")", sig_ix)].split(",")
    call_ix = seg.rfind("}(")
    if call_ix < 0:
        return {}
    tail = seg[call_ix + 2:].rstrip().rstrip(";")
    if tail.endswith(")"):
        tail = tail[:-1]
    args = _split_top(tail)
    return dict(zip([p.strip() for p in params], args))


def _nuxt_val(pmap, v, depth=0):
    """把 __NUXT__ 里的值(字面量/变量引用/数组)还原为 Python 值"""
    if v is None or depth > 6:
        return v
    v = v.strip()
    if v.startswith('"') and v.endswith('"') and len(v) >= 2:
        return v[1:-1].replace("\\u002F", "/").replace('\\"', '"').replace("\\/", "/")
    if v.startswith("'") and v.endswith("'") and len(v) >= 2:
        return v[1:-1].replace("\\u002F", "/")
    if v in ("!0", "true"):
        return True
    if v in ("!1", "false"):
        return False
    if v in ("null", "void 0", ""):
        return None
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    if re.fullmatch(r"-?\d+\.\d+", v):
        return float(v)
    m = re.fullmatch(r"Array\((\d+)\)", v)
    if m:
        return [None] * int(m.group(1))
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        if not inner:
            return []
        return [_nuxt_val(pmap, item, depth + 1) for item in _split_top(inner)]
    if v in pmap and pmap[v] != v:
        return _nuxt_val(pmap, pmap[v], depth + 1)
    return v


def _balanced(seg, start):
    """从 seg[start]=='[' 或 '{' 起找匹配的闭括号下标"""
    pairs = {"[": "]", "{": "}"}
    o = seg[start]
    c = pairs[o]
    depth, i, instr = 0, start, None
    while i < len(seg):
        ch = seg[i]
        if instr:
            if ch == instr and seg[i - 1] != "\\":
                instr = None
        elif ch in "\"'":
            instr = ch
        elif ch == o:
            depth += 1
        elif ch == c:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _parse_obj_items(block, pmap):
    """解析形如 [{id:x,title:y,...},{...}] 的对象数组 -> [dict]"""
    items = []
    i = 0
    while True:
        b = block.find("{", i)
        if b < 0:
            break
        e = _balanced(block, b)
        if e < 0:
            break
        body = block[b + 1:e]
        obj = {}
        for pair in _split_top(body):
            k, _, val = pair.partition(":")
            k = k.strip().strip("\"'")
            if k:
                obj[k] = _nuxt_val(pmap, val.strip())
        items.append(obj)
        i = e + 1
    return items


def _enclosing_obj(seg, inner_ix):
    """找包含 seg[inner_ix] 的最近外层对象 {..}，返回 (start, end)"""
    end = seg.rfind("}", 0, inner_ix)
    start = seg.rfind("{", 0, inner_ix)
    if start < 0:
        return None
    e = _balanced(seg, start)
    if e < 0 or e < inner_ix:
        return None
    return start, e


def _kk_html(url, ttl=600):
    key = "kk:" + url
    hit = cache_get(key)
    if hit:
        return hit
    obj, _ = http_get(url, timeout=15)
    html = obj.decode("utf-8", "replace")
    cache_put(key, html, ttl)
    return html


def _norm_kk_topic(c):
    return {
        "id": c.get("id"),
        "title": c.get("title") or "",
        "cover": c.get("vertical_image_url") or c.get("cover_image_url")
                 or c.get("vertical_cover") or c.get("horizontal_cover") or "",
        "author": " / ".join(c.get("author_name") or []) if isinstance(c.get("author_name"), list)
                  else (c.get("author_name") or ""),
        "is_finish": 1 if c.get("is_finish") else 0,
        "styles": c.get("styles") or [],
        "last_ord": c.get("last_ord") or c.get("total") or "",
    }


def manga_home(refresh=False):
    if not refresh:
        hit = cache_get("kk:home")
        if hit:
            return hit
    html = _kk_html(KK_WEB + "/", ttl=600)
    seg = _nuxt_seg(html)
    pmap = _nuxt_map(seg) if seg else {}
    out, seen = [], set()

    def add(tid, title, cover):
        if not tid or tid in seen or not title:
            return
        seen.add(tid)
        out.append({"id": tid, "title": title, "cover": (cover or "").replace("\\u002F", "/"),
                    "author": "", "is_finish": 0, "styles": [], "last_ord": ""})

    # 1) __NUXT__ 字面量卡片: {id:21881,title:"错撩",cover_image_url:"https:..."}
    if seg:
        for m in re.finditer(
                r'\{id:(\d+),title:("[^"]+"|[A-Za-z_$][\w$]*),cover_image_url:("[^"]+"|[A-Za-z_$][\w$]*)', seg):
            add(int(m.group(1)), _nuxt_val(pmap, m.group(2)), _nuxt_val(pmap, m.group(3)))

    # 2) SSR 锚点兜底（标题取最后一行）
    for m in re.finditer(r'<a[^>]+href="(?:https?://[^"]*kuaikanmanhua\.com)?/web/topic/(\d+)"[^>]*>(.*?)</a>', html, re.S):
        lines = [x.strip() for x in re.sub(r"<[^>]+>", "\n", m.group(2)).split("\n") if x.strip()]
        if lines:
            add(int(m.group(1)), lines[-1], "")
    # 无封面的排后
    out.sort(key=lambda c: 0 if c["cover"] else 1)
    out = out[:30]
    if not out:
        raise RuntimeError("漫画首页解析失败")
    cache_put("kk:home", out, 600)
    return out


def manga_search(kw):
    key = "kk:search:" + kw
    hit = cache_get(key)
    if hit:
        return hit
    obj, _ = http_get(KK_API + "/v1/search/topic?q=" + up.quote(kw) + "&size=20",
                      timeout=15, extra_headers={"Referer": KK_WEB + "/"})
    j = json.loads(obj.decode("utf-8", "replace"))
    hits = (j.get("data") or {}).get("hit") or []
    out = []
    for h in hits:
        t = h.get("topic") or h
        out.append(_norm_kk_topic(t))
    cache_put(key, out, 600)
    return out


def manga_detail(tid):
    html = _kk_html("%s/web/topic/%s" % (KK_WEB, tid), ttl=600)
    seg = _nuxt_seg(html)
    if not seg:
        raise RuntimeError("详情数据解析失败")
    pmap = _nuxt_map(seg)
    ix = seg.find("comics:[")
    if ix < 0:
        raise RuntimeError("章节列表缺失")
    en = _balanced(seg, ix + len("comics:"))
    block = seg[ix + len("comics:"):en + 1]
    chapters = _parse_obj_items(block, pmap)
    # 外层对象 = 作品元信息
    meta = {}
    wrap = _enclosing_obj(seg, ix)
    if wrap:
        body = seg[wrap[0] + 1:wrap[1]]
        for pair in _split_top(body):
            k, _, val = pair.partition(":")
            k = k.strip().strip("\"'")
            if k and k != "comics":
                meta[k] = _nuxt_val(pmap, val.strip())
    chapters_out = []
    for i, c in enumerate(chapters):
        if not c.get("id"):
            continue
        chapters_out.append({
            "id": c["id"],
            "title": str(c.get("title") or ""),
            "idx": i + 1,
            "is_free": bool(c.get("is_free")) and not bool(c.get("locked")),
            "locked": bool(c.get("locked")),
            "created_at": c.get("created_at") or "",
        })
    info = {
        "id": tid,
        "title": meta.get("title") or "",
        "cover": meta.get("vertical_cover") or meta.get("cover_image_url") or meta.get("horizontal_cover") or "",
        "author": " / ".join(meta.get("author_name") or []) if isinstance(meta.get("author_name"), list)
                  else (meta.get("author_name") or ""),
        "description": meta.get("description") or meta.get("evaluate") or "",
        "styles": meta.get("styles") or meta.get("tags") or [],
        "is_finish": 1 if meta.get("is_finish") else 0,
        "total": meta.get("comics_count") or len(chapters_out),
        "chapters": chapters_out,
    }
    return info


def manga_chapter(ch_id):
    html = _kk_html("%s/web/comic/%s" % (KK_WEB, ch_id), ttl=1800)
    seg = _nuxt_seg(html)
    if not seg:
        raise RuntimeError("章节图片数据解析失败")
    pmap = _nuxt_map(seg)
    ix = seg.find("comic_images:[")
    if ix < 0:
        raise RuntimeError("本话无图片数据（可能为付费章节）")
    en = _balanced(seg, ix + len("comic_images:"))
    block = seg[ix + len("comic_images:"):en + 1]
    imgs = _parse_obj_items(block, pmap)
    urls = []
    for im in imgs:
        u = im.get("url1280") or im.get("url") or ""
        if u:
            urls.append(u)
    # 上一话/下一话
    prev_nxt = {}
    for k in ("prev_comic_id", "next_comic_id", "prev_id", "next_id"):
        m = re.search(k + r":([A-Za-z_$][\w$]*|\d+)", seg)
        if m:
            v = _nuxt_val(pmap, m.group(1))
            if v:
                prev_nxt[k] = v
    title = ""
    mt = re.search(r'comic_info:\{id:[\w$]+,title:([A-Za-z_$][\w$]*)', seg)
    if mt:
        title = _nuxt_val(pmap, mt.group(1)) or ""
    return {"id": ch_id, "title": str(title), "images": urls, "nav": prev_nxt}
