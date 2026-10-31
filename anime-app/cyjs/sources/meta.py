# -*- coding: utf-8 -*-
"""元数据: Bangumi / AniList / bangumi-data 标题映射 / 评分简介"""
import json
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.request import Request
from urllib import parse as up

from ..config import ROOT, _opener
from ..textutil import _clean, _bgm_title_variants
from ..cache import cache_get, cache_put
from ..net import http_get
from .age import age_search, age_catalog, AGE_TAGS


# ----------------------------------------------------------------------------
# Bangumi 评分与简介 (bgm.tv 官方 API, 按标题搜索动画条目)
# ----------------------------------------------------------------------------
BGM_API = "https://api.bgm.tv"
_BGM_UA = "cy-anime-app/2.0 (local personal anime app)"
# 直连不可达时依次回退的 GET 中转镜像 (api.bgm.tv 在部分网络被墙)
_BGM_MIRRORS = [
    ("direct", None),
    ("allorigins", "https://api.allorigins.win/raw?url="),
]


def _bgm_http(url, timeout=4):
    headers = {"User-Agent": _BGM_UA, "Accept": "application/json",
               "Accept-Language": "zh-CN,zh;q=0.9"}
    req = Request(url, headers=headers)
    with _opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read(2 * 1024 * 1024).decode("utf-8", "replace"))


# bgm.tv 整体不可达时的熔断: 连续失败后 10 分钟内直接短路, 避免搜索被 6 次串行超时拖死
_bgm_down_until = [0.0]
_bgm_down_lock = threading.Lock()


def _bgm_request(path):
    """GET 方式请求 Bangumi API, 直连失败自动走公共中转镜像; 不可达时熔断 10 分钟"""
    with _bgm_down_lock:
        if time.time() < _bgm_down_until[0]:
            raise RuntimeError("bangumi 熔断中 (10分钟)")
    url = BGM_API + path
    last = None
    for name, prefix in _BGM_MIRRORS:
        try:
            res = _bgm_http(url if prefix is None else prefix + up.quote(url, safe=""))
            with _bgm_down_lock:          # 成功即解除熔断
                _bgm_down_until[0] = 0.0
            return res
        except Exception as e:
            last = e
            print("[bangumi] %s 请求失败 %s: %r" % (name, path, e), flush=True)
    with _bgm_down_lock:
        _bgm_down_until[0] = time.time() + 600
    raise last or RuntimeError("bangumi 不可达")


def _bgm_search_ids(kw):
    """多端点搜索 Bangumi 动画条目 (全部 GET, 便于走中转镜像), 返回 [{id,name,name_cn}]"""
    # 1) v0 GET 搜索
    try:
        res = _bgm_request("/v0/search/subjects?keywords=%s&limit=8" % up.quote(kw))
        items = [{"id": it.get("id"), "name": it.get("name") or "",
                  "name_cn": it.get("name_cn") or ""}
                 for it in (res.get("data") or []) if it.get("id")]
        if items:
            return items
    except Exception as e:
        print("[bangumi] v0 GET search 失败: %r" % (e,), flush=True)
    # 2) 旧版 /search/subject 接口 (type=2 动画)
    try:
        res = _bgm_request("/search/subject/%s?type=2&max_results=8" % up.quote(kw))
        lst = res.get("list") or []
        return [{"id": it.get("id"), "name": it.get("name") or "",
                 "name_cn": it.get("name_cn") or it.get("name", "")} for it in lst if it.get("id")]
    except Exception as e:
        print("[bangumi] legacy search 失败: %r" % (e,), flush=True)
    return []


def _bgm_pick(items, kw):
    """优先命中: 名字完全一致 > 包含关系 > 第一个"""
    def _rel(it):
        names = {_clean(it.get("name_cn") or ""), _clean(it.get("name") or "")}
        kn = _clean(kw)
        if kn in names:
            return 2
        if any(kn and (kn in n or n in kn) for n in names if n):
            return 1
        return 0
    return sorted(items, key=_rel, reverse=True)


# ---- AniList 回退源 (bgm.tv 在部分网络不可达时仍能提供评分与简介) ----
ANILIST_API = "https://graphql.anilist.co"

# ---- bangumi-data 标题映射 (jsDelivr CDN 静态数据集: 中文标题 -> 日文原名/MAL id) ----
_BGDATA_URL = "https://cdn.jsdelivr.net/npm/bangumi-data@0.3.229/dist/data.json"
_BGDATA_FILE = ROOT / "runtime" / "bangumi-data.json"
_bgdata_index = None
_bgdata_lock = threading.Lock()


def _norm_map_title(s):
    s = _clean(s).lower()
    # 全角英数转半角: 'Season２' 与 'Season 2' 必须归一命中
    s = "".join(chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else c for c in s)
    return re.sub(r"[\s:：·.,，。!！?？~～'’\"“”()\[\]【】\-—_（）]", "", s)


def _bgdata_index_build():
    """加载 (必要时从 jsDelivr 下载) bangumi-data 数据集并构建标题索引"""
    global _bgdata_index
    with _bgdata_lock:
        if _bgdata_index is not None:
            return _bgdata_index
        idx = {}
        try:
            stale = (not _BGDATA_FILE.exists()
                     or time.time() - _BGDATA_FILE.stat().st_mtime > 7 * 86400)
            if stale:
                data, _ = http_get(_BGDATA_URL, timeout=30, max_mb=40)
                _BGDATA_FILE.parent.mkdir(parents=True, exist_ok=True)
                _BGDATA_FILE.write_bytes(data)
            items = json.loads(_BGDATA_FILE.read_bytes().decode("utf-8", "replace")).get("items") or []
            for it in items:
                ja = it.get("title") or ""
                mal = next((str(s.get("id") or "") for s in it.get("sites", [])
                            if s.get("site") == "mal"), "")
                tt = it.get("titleTranslate") or {}
                # 保序: zh-Hans 首个为最常用官方译名 (如 '进击的巨人' > '自由之翼'),
                # 用 set 会随机丢序导致反转映射拿到冷门别名
                names = list(tt.get("zh-Hans") or [])
                if ja:
                    names.append(ja)
                for n in names:
                    k = _norm_map_title(n)
                    if k and k not in idx:
                        # names 全量保存: 同一部番的多个中文译名 (药屋少女的呢喃/药师少女的独语)
                        # 都是合法的 AGE 检索候选
                        idx[k] = {"ja": ja, "mal": mal, "zh": n, "names": names}
        except Exception as e:
            print("[bgdata] 加载失败: %r" % (e,), flush=True)
        _bgdata_index = idx
        return idx


def _bgdata_lookup(title):
    """中文标题 -> {ja, mal}; 依次尝试原题与去季数变体"""
    idx = _bgdata_index_build()
    if not idx:
        return None
    for kw in _bgm_title_variants(title):
        ref = idx.get(_norm_map_title(kw))
        if ref:
            return ref
    return None


def _anilist_graphql(query, variables):
    req = Request(ANILIST_API, data=json.dumps({"query": query, "variables": variables}).encode("utf-8"),
                  headers={"Content-Type": "application/json",
                           "Accept": "application/json", "User-Agent": _BGM_UA})
    with _opener.open(req, timeout=10) as resp:
        return json.loads(resp.read(2 * 1024 * 1024).decode("utf-8", "replace"))


_ANILIST_FIELDS = ("id averageScore popularity episodes seasonYear "
                   "description(asHtml:false) title{romaji english native}")


def anilist_info(title):
    """AniList 查询动画条目: 中文标题先经 bangumi-data 映射为 MAL id/日文原名, 返回 bangumi_info 同构 dict"""
    m = None
    ref = None
    try:
        ref = _bgdata_lookup(title)
    except Exception as e:
        print("[bgdata] 查询失败: %r" % (e,), flush=True)
    if ref and ref.get("mal"):
        try:
            m = (_anilist_graphql(
                "query($id:Int){Media(idMal:$id,type:ANIME){%s}}" % _ANILIST_FIELDS,
                {"id": int(ref["mal"])}).get("data") or {}).get("Media")
        except Exception as e:
            print("[anilist] idMal 查询失败: %r" % (e,), flush=True)
    media = [m] if m else []
    if not media:
        searches = []
        if ref and ref.get("ja"):
            searches.append(re.sub(r"\([^)]*\)\s*$", "", ref["ja"]).strip())
        searches.append(title)
        searches.extend(_bgm_title_variants(title))
        for kw in searches:
            if not kw:
                continue
            try:
                res = _anilist_graphql(
                    "query($s:String){Page(page:1,perPage:5){media(search:$s,type:ANIME,sort:SEARCH_MATCH){%s}}}" % _ANILIST_FIELDS,
                    {"s": kw})
                media = ((res.get("data") or {}).get("Page") or {}).get("media") or []
            except Exception as e:
                print("[anilist] 搜索失败 (%s): %r" % (kw, e), flush=True)
                media = []
            if media:
                break
    if not media:
        return {}
    kn = _clean(title)

    def _rel(x):
        t = x.get("title") or {}
        names = {_clean(t.get(k) or "") for k in ("native", "english", "romaji")}
        if kn in names:
            return 3
        if any(kn and (kn in n or n in kn) for n in names if n):
            return 2
        return 0
    media.sort(key=_rel, reverse=True)
    m = media[0]
    if not m.get("averageScore"):
        return {}
    desc = re.sub(r"<[^>]+>", "", m.get("description") or "")
    desc = re.sub(r"\n{3,}", "\n\n", desc).strip()
    t = m.get("title") or {}
    return {
        "id": m.get("id"), "source": "AniList",
        "name": t.get("english") or t.get("romaji") or "",
        "name_cn": t.get("native") or "",
        "score": round(m["averageScore"] / 10.0, 1),
        "total": m.get("popularity") or 0,
        "rank": 0,
        "summary": desc,
        "date": str(m.get("seasonYear") or ""),
        "eps": m.get("episodes") or 0,
        "image": "",
    }


_BGM_DOWN_UNTIL = [0.0]   # bgm.tv 最近一次不可达后的短路截止时间 (被墙时避免每个条目都等满超时)


def metadata_info(title):
    """Bangumi 优先, 不可达时回退 AniList; bgm.tv 失败后短路 10 分钟直接走 AniList"""
    if time.time() >= _BGM_DOWN_UNTIL[0]:
        try:
            info = bangumi_info(title)
        except Exception as e:
            print("[bangumi] metadata 异常 (%s): %r" % (title, e), flush=True)
            info = None
        if info and info.get("id"):
            _BGM_DOWN_UNTIL[0] = 0.0   # bgm.tv 恢复可达, 关闭短路
            return info
        _BGM_DOWN_UNTIL[0] = time.time() + 600   # 本次失败, 10 分钟内不再等 bgm.tv 超时
    key = "meta:" + _clean(title)
    hit = cache_get(key)
    if hit is not None:
        return hit
    try:
        info = anilist_info(title)
    except Exception as e:
        print("[anilist] 获取失败 (%s): %r" % (title, e), flush=True)
        info = {}
    cache_put(key, info, 86400 if info else 600)
    return info


# 标题变体: 去掉季数/剧场版等后缀后重试, 提高命中率
def _bgdata_reverse_map():
    """日文原名 -> 中文名 (供 AniList 候选标题反查 AGE 可搜的中文标题)"""
    rev = {}
    for v in _bgdata_index_build().values():
        ja, zh = v.get("ja") or "", v.get("zh") or ""
        if ja and zh:
            rev.setdefault(_norm_map_title(ja), zh)
    return rev


_age_search_fail = {}          # 搜索关键词 -> 失败时间戳 (60 秒负缓存, AGE 故障时不再反复撞上游)
_age_search_fail_lock = threading.Lock()


def _age_search_safe(kw):
    with _age_search_fail_lock:
        t = _age_search_fail.get(kw)
        if t and time.time() - t < 60:
            return []
    try:
        return age_search(kw)
    except Exception:
        with _age_search_fail_lock:
            if len(_age_search_fail) > 64:   # 防字典无限增长
                _age_search_fail.clear()
            _age_search_fail[kw] = time.time()
        return []


def _rel_items(items, kw):
    """相关性过滤: AGE 无匹配时会返回站点默认列表 (垃圾结果), 要求标题与关键词互相包含"""
    nk = _norm_map_title(kw)
    if not nk:
        return list(items or [])
    out = []
    for it in items or []:
        nt = _norm_map_title(it.get("title") or "")
        if nt and (nk in nt or (len(nt) >= 2 and nt in nk)):
            out.append(it)
    return out


def _bgdata_candidates(kw, add):
    """在 bangumi-data 中文标题索引中做包含匹配, 命中的中文名作为 AGE 搜索候选"""
    idx = _bgdata_index_build()
    nk = _norm_map_title(kw)
    if not nk or not idx or len(nk) < 2:
        return
    hits = []
    for k, v in idx.items():
        if (len(nk) >= 2 and nk in k) or (len(k) >= 2 and k in nk):
            hits.append((len(k), v))
    hits.sort(key=lambda x: -x[0])   # 标题越长越具体, 优先
    # 同一番的全部译名都作为候选: 用户搜 '药师少女的独语' (台译),
    # AGE 条目可能是 '药屋少女的呢喃' (陆译) — 只回自身译名会搜空
    for _, v in hits[:6]:
        for n in (v.get("names") or [v.get("zh") or v.get("ja") or ""]):
            if n:
                add(n)


_ANILIST_MEDIA_Q = ("query($s:String){Page(page:1,perPage:6){"
                    "media(search:$s,type:ANIME){title{native romaji english} synonyms}}}")
_ANILIST_CHAR_Q = ("query($s:String){Page(page:1,perPage:2){"
                   "characters(search:$s,sort:FAVOURITES_DESC)"
                   "{name{full} media(perPage:2){nodes{title{native romaji english}}}}}}")


def _anilist_candidates(kw, add):
    """AniList 媒体/角色搜索: 角色名 -> 其所属作品标题; 并反查中文标题
    (AniList 的 Page 查询只允许 1 个子字段, media 与 characters 必须拆成两次查询)"""
    rev = None
    raw = []
    try:
        page = ((res := _anilist_graphql(_ANILIST_MEDIA_Q, {"s": kw})).get("data") or {}).get("Page") or {}
        for m in page.get("media") or []:
            t = m.get("title") or {}
            raw.extend([t.get(k) or "" for k in ("native", "romaji", "english")])
            raw.extend(m.get("synonyms") or [])
    except Exception as e:
        print("[anilist] 媒体搜索候选失败: %r" % (e,), flush=True)
    try:
        page = ((res := _anilist_graphql(_ANILIST_CHAR_Q, {"s": kw})).get("data") or {}).get("Page") or {}
        for c in page.get("characters") or []:
            # Character.media 是 MediaConnection, 标题须经 nodes 子字段
            for m in ((c.get("media") or {}).get("nodes") or []):
                t = m.get("title") or {}
                raw.extend([t.get(k) or "" for k in ("native", "romaji", "english")])
    except Exception as e:
        print("[anilist] 角色搜索候选失败: %r" % (e,), flush=True)
    # 中文映射候选必须排在最前: AniList 会返回几十条多语言同义词,
    # 候选上限 cands[:10] 截断时中文标题 (常在第 10 位之后) 会被全部切掉 -> "搜不全"
    zh_list, raw_list = [], []
    for t in raw:
        t = t.strip()
        if not t:
            continue
        raw_list.append(t)
        if rev is None:
            rev = _bgdata_reverse_map()
        zh = rev.get(_norm_map_title(t))
        if zh:
            zh_list.append(zh)
    for t in zh_list + raw_list:
        add(t)


# ---------------- 本季度新番识别 (一周更新表标记用) ----------------
def season_names():
    """当前季度 AniList 动画标题集合 (含中文反向映射), 缓存 6 小时"""
    key = "season:names"
    hit = cache_get(key)
    if hit:
        return hit
    lt = time.localtime()
    y, m = lt.tm_year, lt.tm_mon
    season = ("WINTER" if m in (12, 1, 2) else "SPRING" if m in (3, 4, 5) else
              "SUMMER" if m in (6, 7, 8) else "FALL")
    year = y + 1 if m == 12 else y
    q = ("query($s:MediaSeason,$y:Int,$p:Int){Page(page:$p,perPage:50){"
         "media(season:$s,seasonYear:$y,type:ANIME,sort:POPULARITY_DESC)"
         "{title{native romaji english} synonyms}}}")
    names = set()
    rev = None
    for page in (1, 2):
        try:
            res = _anilist_graphql(q, {"s": season, "y": year, "p": page})
            medias = ((res.get("data") or {}).get("Page") or {}).get("media") or []
        except Exception as e:
            print("[anilist] 季度名单失败 (p%d): %r" % (page, e), flush=True)
            medias = []
        if not medias:
            break
        if rev is None:
            rev = _bgdata_reverse_map()
        for it in medias:
            t = it.get("title") or {}
            for v in ([t.get("native") or "", t.get("romaji") or "", t.get("english") or ""]
                      + list(it.get("synonyms") or [])):
                nv = _norm_map_title(_clean(v))
                if nv:
                    names.add(nv)
            zh = rev.get(_norm_map_title(_clean(t.get("native") or "")))
            if zh:
                nz = _norm_map_title(_clean(zh))
                if nz:
                    names.add(nz)
    names.discard("")
    out = sorted(names)
    cache_put(key, out, 6 * 3600)
    return out


def _in_season(title, names):
    """标题是否属于当前季度 (去季数后缀 + 双向包含)"""
    nt = _norm_map_title(title)
    if not nt or not names:
        return False
    for base in _bgm_title_variants(title):
        nb = _norm_map_title(base)
        if nb and nb in names:
            return True
    for n in names:
        if len(n) >= 2 and (n in nt or (len(nt) >= 2 and nt in n)):
            return True
    return False


# 常用中文昵称/角色名 -> AGE 可搜标题 (聚合搜索的映射候选)
CN_ALIAS = {
    "骨傲天": ["Overlord"], "骨王": ["Overlord"], "不死者之王": ["Overlord"],
    "利姆露": ["关于我转生变成史莱姆这档事"], "萌王": ["关于我转生变成史莱姆这档事"],
    "转生史莱姆": ["关于我转生变成史莱姆这档事"], "史莱姆": ["关于我转生变成史莱姆这档事"],
    "五条悟": ["咒术回战"], "五条老师": ["咒术回战"], "虎杖悠仁": ["咒术回战"], "虎杖": ["咒术回战"],
    "炭治郎": ["鬼灭之刃"], "祢豆子": ["鬼灭之刃"], "善逸": ["鬼灭之刃"], "伊之助": ["鬼灭之刃"],
    "无惨": ["鬼灭之刃"], "鬼舞辻": ["鬼灭之刃"],
    "路飞": ["航海王", "海贼王"], "索隆": ["航海王", "海贼王"], "娜美": ["航海王", "海贼王"],
    "山治": ["航海王", "海贼王"], "罗宾": ["航海王", "海贼王"], "海贼": ["航海王", "海贼王"],
    "鸣人": ["火影忍者"], "佐助": ["火影忍者"], "卡卡西": ["火影忍者"], "鼬": ["火影忍者"],
    "宇智波": ["火影忍者"], "博人": ["博人传"],
    "一护": ["死神", "BLEACH"], "露琪亚": ["死神", "BLEACH"],
    "悟空": ["龙珠", "七龙珠"], "贝吉塔": ["龙珠", "七龙珠"], "布罗利": ["龙珠", "七龙珠"],
    "柯南": ["名侦探柯南"], "毛利兰": ["名侦探柯南"], "灰原哀": ["名侦探柯南"], "基德": ["名侦探柯南"],
    "炮姐": ["某科学的超电磁炮"], "御坂美琴": ["某科学的超电磁炮"], "超炮": ["某科学的超电磁炮"],
    "当麻": ["魔法禁书目录", "某科学的超电磁炮"], "一方通行": ["魔法禁书目录", "某科学的超电磁炮"],
    "蕾姆": ["Re:从零开始的异世界生活"], "雷姆": ["Re:从零开始的异世界生活"],
    "486": ["Re:从零开始的异世界生活"], "菜月昴": ["Re:从零开始的异世界生活"],
    "艾米莉亚": ["Re:从零开始的异世界生活"], "re0": ["Re:从零开始的异世界生活"],
    "桐人": ["刀剑神域"], "亚丝娜": ["刀剑神域"], "刀剑": ["刀剑神域"],
    "金木研": ["东京喰种"], "喰种": ["东京喰种"], "金木": ["东京喰种"],
    "夜神月": ["死亡笔记"], "基拉": ["死亡笔记"], "L": ["死亡笔记"],
    "埼玉": ["一拳超人"], "杰诺斯": ["一拳超人"], "一拳": ["一拳超人"],
    "艾伦": ["进击的巨人"], "三笠": ["进击的巨人"], "利威尔": ["进击的巨人"], "兵长": ["进击的巨人"], "巨人": ["进击的巨人"],
    "阿尼亚": ["间谍过家家"], "黄昏": ["间谍过家家"], "约尔": ["间谍过家家"], "间谍": ["间谍过家家"],
    "辉夜": ["辉夜大小姐想让我告白"], "白银": ["辉夜大小姐想让我告白"],
    "千反田": ["冰菓"], "折木": ["冰菓"],
    "小埋": ["干物妹小埋"], "银时": ["银魂"], "神乐": ["银魂"],
    "面码": ["未闻花名"], "未闻花名": ["我们仍未知道那天所看见的花的名字"],
    "芙莉莲": ["葬送的芙莉莲"], "费伦": ["葬送的芙莉莲"], "修塔尔克": ["葬送的芙莉莲"],
    "辛美尔": ["葬送的芙莉莲"],
}


def _alias_candidates(kw, add):
    """常用中文昵称/角色名 -> 已知标题, 作为 AGE 检索候选"""
    nk = _norm_map_title(kw)
    if not nk:
        return
    for a, titles in CN_ALIAS.items():
        na = _norm_map_title(a)
        if na == nk or (len(na) >= 2 and na in nk) or (len(nk) >= 2 and nk in na):
            for t in titles:
                add(t)


# ---- B站角色名/别名关键词库 (runtime/alias_cn.json, 构建脚本离线生成, 上千关键词) ----
_ALIAS_EXTRA = None


def _alias_extra():
    """懒加载 B站关键词库并做归一化 (角色名/官方别名/标题变体 -> 中文标题)"""
    global _ALIAS_EXTRA
    if _ALIAS_EXTRA is None:
        p = ROOT / "runtime" / "alias_cn.json"
        raw = {}
        try:
            if p.exists():
                raw = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            print("[alias-extra] 加载失败: %r" % (e,), flush=True)
        norm = {}
        for a, t in raw.items():
            na = _norm_map_title(a)
            if na and na not in norm:
                norm[na] = t
        _ALIAS_EXTRA = norm
        print("[alias-extra] 关键词 %d 条" % len(norm), flush=True)
    return _ALIAS_EXTRA


def _search_kw_variants(kw):
    """AGE 是字面匹配, 季度后缀/空格/括号差异会直接搜空 — 生成最多 3 个检索变体
    如 '咒术回战 第二季' -> ['咒术回战 第二季', '咒术回战第二季', '咒术回战']"""
    out = [kw]
    no_space = re.sub(r"\s+", "", kw)
    if no_space != kw:
        out.append(no_space)
    base = re.sub(
        r"[\s　]*(第\s*[0-9一二三四五六七八九十百]+\s*[季期部]|"
        r"[0-9]+(?:st|nd|rd|th)\s*Season|Season\s*[0-9]+|"
        r"S[0-9]{1,2}|[ⅡⅢⅣ]|II|III|IV|Final\s*Season|最终季|最终章|完结篇)\s*$",
        "", no_space, flags=re.IGNORECASE)
    if base and base not in out:
        out.append(base)
    return out[:3]


def anime_search(kw):
    """聚合搜索: AGE 标题搜索 + bangumi-data 中文标题 + AniList 媒体/角色映射,
    直接命中不足 8 条时用候选标题并行检索 AGE 并合并去重
    提速编排: AGE 直接搜索与候选收集并行启动; 候选检索合并到 12 条即取消排队任务提前收工"""
    kw = _clean(kw)
    if not kw:
        raise RuntimeError("关键词不能为空")
    key = "asrch:" + kw
    hit = cache_get(key)
    if hit:
        return hit
    results, order = {}, []
    nk = _norm_map_title(kw)

    def _add(items):
        for it in items or []:
            pid = str(it.get("id") or "")
            if pid and pid not in results:
                results[pid] = it
                order.append(pid)

    def _gather():
        """候选标题收集: 别名/关键词库/bgdata(本地) + AniList(网络), 精确文本去重
        注意: 不可按归一化合并变体 — AGE 搜索是字面匹配, 【】等括号差异会改变结果"""
        cands, seen = [], set()

        def _cand(t):
            t = _clean(t)
            # 候选自身归一化为空 (纯符号/假名, 如 "-ナルト-") 会令相关性过滤失效并泄漏站点默认列表, 必须跳过
            if not t or not _norm_map_title(t) or t in seen or _norm_map_title(t) == nk:
                return
            seen.add(t)
            cands.append(t)

        exact_hit = False                     # 本地别名精确命中 -> 跳过 AniList/Bangumi
        try:
            for a in CN_ALIAS:                # 1) 常用中文昵称/角色名映射
                if _norm_map_title(a) == nk:
                    exact_hit = True
                    break
            _alias_candidates(kw, _cand)
        except Exception as e:
            print("[alias] 搜索候选失败: %r" % (e,), flush=True)
        try:
            ex = _alias_extra()               # 2) B站角色名/别名关键词库
            t = ex.get(nk)
            if t:
                exact_hit = True
                _cand(t)
            else:
                for na, t2 in ex.items():
                    if (len(na) >= 2 and na in nk) or (len(nk) >= 2 and nk in na):
                        _cand(t2)
        except Exception as e:
            print("[alias-extra] 搜索候选失败: %r" % (e,), flush=True)
        try:
            _bgdata_candidates(kw, _cand)     # 3) 中文标题库包含匹配
        except Exception as e:
            print("[bgdata] 搜索候选失败: %r" % (e,), flush=True)
        # AniList/Bangumi 对无法识别的中文词会返回热门动画充当"结果",
        # 其标题作为候选回查 AGE 能通过自身相关性过滤 -> 垃圾混入 (如 '炮姐' 搜出咒术回战)
        if not exact_hit:
            try:
                _anilist_candidates(kw, _cand)    # 4) AniList 媒体/角色映射 (网络)
            except Exception as e:
                print("[anilist] 搜索候选失败: %r" % (e,), flush=True)
            try:
                for it in _bgm_search_ids(kw)[:6]:   # 5) Bangumi 条目搜索 (中日双语标题)
                    if it.get("name_cn"):
                        _cand(it["name_cn"])
                    if it.get("name"):
                        _cand(it["name"])
            except Exception as e:
                print("[bangumi] 搜索候选失败: %r" % (e,), flush=True)
        return cands

    # 0) AGE 直接搜索 (原词 + 变体) 与 候选收集 并行 (候选收集里的网络耗时被直接搜索覆盖)
    variants = _search_kw_variants(kw)
    with ThreadPoolExecutor(max_workers=8) as pool:
        direct_futs = [pool.submit(lambda t=v: _rel_items(_age_search_safe(t), t))
                       for v in variants]
        cand_fut = pool.submit(_gather)
        for f in direct_futs:
            _add(f.result())
        cands = cand_fut.result() or []
        if len(order) < 8 and cands:          # 5) 候选并行搜索 AGE (同样过滤垃圾结果)
            # 注意: 必须用默认参 t=c 绑定候选词, 闭包直接引用外层 c 会后期绑定到最后一个候选
            # 上限 10: 突发请求过多会触发 AGE 限流拒绝, 反而全盘失败
            futs = [pool.submit(lambda t=c: _rel_items(_age_search_safe(t), t))
                    for c in cands[:10]]
            for f in as_completed(futs):
                _add(f.result())
                if len(order) >= 16:          # 结果已够丰富: 取消仍在排队的候选, 提前收工
                    for g in futs:
                        g.cancel()
                    break
    if not order:
        raise RuntimeError("未搜索到结果, 可尝试其他关键词")
    out = [results[p] for p in order]
    cache_put(key, out, 600)
    return out


# ---------------- 推荐板块: 关键词推荐 + 年度评分榜 (独立增量模块, 不影响既有功能) ----------------

# 中文题材关键词 -> AniList genre/tag 映射 (仅推荐模块使用; bgm.tv 无年度榜 API 且被墙, 按既有约定回退 AniList)
_RECO_GENRE_MAP = {
    "热血": {"g": ["Action"]}, "战斗": {"g": ["Action"]}, "冒险": {"g": ["Adventure"]},
    "搞笑": {"g": ["Comedy"]}, "恋爱": {"g": ["Romance"]}, "爱情": {"g": ["Romance"]},
    "催泪": {"g": ["Drama"]}, "感动": {"g": ["Drama"]},
    "治愈": {"g": ["Slice of Life"], "t": ["Iyashikei"]}, "日常": {"g": ["Slice of Life"]},
    "悬疑": {"g": ["Mystery", "Thriller"]}, "推理": {"g": ["Mystery"]},
    "科幻": {"g": ["Sci-Fi"]}, "奇幻": {"g": ["Fantasy"]}, "机战": {"g": ["Mecha"]},
    "校园": {"t": ["School"]}, "运动": {"g": ["Sports"]}, "竞技": {"g": ["Sports"]},
    "恐怖": {"g": ["Horror"]}, "心理": {"g": ["Psychological"]}, "音乐": {"g": ["Music"]},
    "后宫": {"g": ["Harem"]}, "异世界": {"t": ["Isekai"]}, "时间循环": {"t": ["Time Loop"]},
    "超能力": {"t": ["Superpower"]}, "魔法": {"t": ["Magic"]},
}


def _anilist_media_list(genres=None, tags=None, year=None, sort="POPULARITY_DESC", per=50):
    """AniList 批量拉取条目 (题材/标签/年份过滤), 仅推荐模块使用"""
    cond = ["type:ANIME", "isAdult:false"]
    decl, qvars = ["$p:Int"], {"p": 1}
    if genres:
        decl.append("$g:[String]"); qvars["g"] = list(genres); cond.append("genre_in:$g")
    if tags:
        decl.append("$t:[String]"); qvars["t"] = list(tags); cond.append("tag_in:$t")
    if year:
        decl.append("$y:Int"); qvars["y"] = int(year); cond.append("seasonYear:$y")
    fields = ("id averageScore episodes seasonYear coverImage{large} "
              "title{native romaji english} synonyms")
    q = ("query(%s){Page(page:$p,perPage:%d){media(%s,sort:%s){%s}}}"
         % (",".join(decl), max(1, min(int(per or 50), 50)), ",".join(cond), sort, fields))
    res = _anilist_graphql(q, qvars)
    return ((res.get("data") or {}).get("Page") or {}).get("media") or []


def _reco_score_batch(items):
    """给 AGE 条目批量补评分: bangumi-data 本地查 mal id -> AniList 别名批量查询 (每 12 个一请求)"""
    pairs = []
    for it in items:
        if it.get("ext"):
            continue
        try:
            ref = _bgdata_lookup(it["title"])
        except Exception:
            ref = None
        if ref and ref.get("mal"):
            pairs.append((_norm_map_title(it["title"]), int(ref["mal"])))
    for i in range(0, len(pairs), 12):
        chunk = pairs[i:i + 12]
        try:
            body = " ".join("m%d:Media(idMal:%d,type:ANIME){averageScore}" % (j, mal)
                            for j, (_, mal) in enumerate(chunk))
            data = (_anilist_graphql("query{%s}" % body, {}).get("data") or {})
            for j, (nt, _) in enumerate(chunk):
                m = data.get("m%d" % j) or {}
                if m.get("averageScore"):
                    for it in items:
                        if not it.get("ext") and _norm_map_title(it["title"]) == nt:
                            it["score"] = round(m["averageScore"] / 10.0, 1)
        except Exception as e:
            print("[reco] 评分补全失败 (chunk %d): %r" % (i // 12, e), flush=True)


def _anilist_title_cn(m, rev):
    """AniList media 条目 -> (中文标题, 封面, 评分, 年份), bangumi-data 反查中文名"""
    t = m.get("title") or {}
    cands = [_clean(t.get("native") or ""), _clean(t.get("romaji") or ""),
             _clean(t.get("english") or "")]
    title = ""
    for c in cands:
        if c and rev.get(_norm_map_title(c)):
            title = rev[_norm_map_title(c)]
            break
    if not title:
        title = next((c for c in cands if c), "")
    return title, ((m.get("coverImage") or {}).get("large")) or "", \
        round((m.get("averageScore") or 0) / 10.0, 1), str(m.get("seasonYear") or "")


# ---------------- 特征找番: 模糊特征描述 -> AniList 标签/题材筛选 (独立增量, 不影响普通搜索) ----------------
# 词典为「组」结构: (信号, 核心词, [同义/近义表达...]); 加载时程序化展开成扁平 _FEAT_MAP (核心+同义词全部指向同一信号)
# 整句输入无需分词: 子串扫描天然从句中挖出特征词, 长词优先避免「校园恋爱」被「恋爱」截断歧义
_FEAT_GROUPS = [
    # ===== 题材: 幻想 / 科幻 =====
    ({"t": ["Isekai"]}, "异世界", ["穿越", "平行世界", "异次元", "穿越到异世界", "异世界生活", "另一个世界"]),
    ({"t": ["Time Loop"]}, "时间循环", ["无限流", "轮回", "死亡回归", "不断重来", "重复一天", "循环往复"]),
    ({"t": ["Time Travel"]}, "时间旅行", ["时空穿越", "穿越时空", "时光机", "回到过去", "穿梭时空", "时间倒流"]),
    ({"t": ["Reincarnation"]}, "转生", ["重生", "轮回转世", "转世", "投胎", "带着记忆重生", "重活一世"]),
    ({"g": ["Sci-Fi"]}, "科幻", ["SF", "未来世界", "未来科技", "高科技", "科幻题材", "近未来"]),
    ({"g": ["Sci-Fi"], "t": ["Space"]}, "太空", ["星际", "宇宙", "太空歌剧", "星际旅行", "宇宙航行", "外太空"]),
    ({"g": ["Mecha"]}, "机甲", ["机战", "机器人", "高达", "驾驶机体", "机体对战", "巨型机器人"]),
    ({"t": ["Cyberpunk"]}, "赛博朋克", ["赛博", "黑客", "义体", "虚拟网络", "赛博世界"]),
    ({"t": ["Post-Apocalyptic Apocalypse"]}, "末世", ["末日", "废土", "末日生存", "世界毁灭之后", "末日题材", "文明崩坏"]),
    ({"t": ["Zombies"]}, "丧尸", ["僵尸", "活死人", "丧尸围城", "尸潮"]),
    ({"t": ["Zombies", "Post-Apocalyptic Apocalypse"]}, "丧尸末世", ["丧尸末日", "末日丧尸"]),
    ({"g": ["Fantasy"]}, "奇幻", ["魔幻", "剑与魔法", "幻想", "奇幻世界", "西幻", "魔法大陆"]),
    ({"t": ["Magic"]}, "魔法", ["法师", "魔女", "魔导", "魔法学院", "魔法世界", "施法"]),
    ({"g": ["Mahou Shoujo"]}, "魔法少女", ["马猴烧酒", "魔法少女番", "变身少女"]),
    ({"t": ["Cultivation"]}, "修仙", ["仙侠", "修真", "修仙者", "求道", "飞升"]),
    ({"t": ["Isekai", "Fantasy"]}, "异世界奇幻", ["穿越奇幻"]),
    # ===== 题材: 现实 / 生活 =====
    ({"t": ["School"]}, "校园", ["学院", "高中", "初中", "学校生活", "校园生活", "学生会", "社团活动", "青春校园", "学园"]),
    ({"g": ["Slice of Life"]}, "日常", ["日常生活", "日常系", "悠闲", "生活流", "柴米油盐", "平淡生活", "日常番"]),
    ({"g": ["Slice of Life"], "t": ["Iyashikei"]}, "治愈", ["治愈系", "疗愈", "暖心", "减压", "舒缓", "治愈番", "治愈心灵"]),
    ({"g": ["Slice of Life"]}, "温馨", ["温情", "温暖人心", "温馨治愈", "温情脉脉"]),
    ({"g": ["Comedy"]}, "搞笑", ["欢乐", "沙雕", "爆笑", "吐槽", "喜剧", "搞怪", "无厘头", "轻松", "欢脱", "逗趣"]),
    ({"t": ["Workplace"]}, "职场", ["社畜", "上班族", "打工人", "办公室", "公司生活", "打工日常"]),
    ({"t": ["Workplace", "Slice of Life"]}, "都市生活", ["都市", "城市生活", "现代都市"]),
    ({"t": ["Cooking"]}, "美食", ["料理", "烹饪", "厨艺", "做饭", "吃货", "大胃王", "美食番", "舌尖"]),
    ({"t": ["Medical"]}, "医疗", ["医生", "医院", "医学", "救护", "诊疗"]),
    ({"t": ["Historical"]}, "历史", ["古代", "史实", "战国", "幕末", "历史题材", "古代生活"]),
    ({"t": ["Samurai"]}, "武士", ["浪人", "剑客", "侍魂", "武士道"]),
    ({"t": ["Ninja"]}, "忍者", ["忍术", "忍者世界", "忍法"]),
    ({"t": ["Pirates"]}, "海盗", ["海贼", "航海", "大航海", "扬帆"]),
    ({"t": ["Politics"]}, "政治", ["权谋", "阴谋", "政治斗争", "宫廷斗争", "权力游戏"]),
    ({"t": ["Military"]}, "军事", ["战争", "军武", "战场", "部队生活", "战争题材", "枪炮"]),
    ({"t": ["Racing"]}, "赛车", ["竞速", "飙车", "车速"]),
    ({"t": ["Video Games"]}, "游戏", ["网游", "电竞", "游戏世界", "VR", "RPG", "游戏题材", "电子竞技"]),
    ({"t": ["Card Battle"]}, "卡牌", ["卡牌对战", "卡牌游戏", "集换卡牌"]),
    ({"t": ["Board Game"]}, "桌游", ["棋类", "博弈游戏"]),
    # ===== 题材: 悬疑 / 惊悚 / 超自然 =====
    ({"g": ["Mystery", "Thriller"]}, "悬疑", ["谜团", "悬案", "未解之谜", "悬念", "疑云"]),
    ({"g": ["Mystery"]}, "推理", ["本格推理", "推理解谜", "破案"]),
    ({"g": ["Mystery"], "t": ["Detective"]}, "侦探", ["名侦探", "侦探推理", "侦探题材", "探案"]),
    ({"g": ["Psychological"]}, "心理", ["致郁", "智斗", "烧脑", "心理战", "博弈", "黑深残", "人性刻画", "深度"]),
    ({"g": ["Horror"]}, "恐怖", ["惊悚怪谈", "恐怖片", "阴森", "诡异"]),
    ({"g": ["Thriller"]}, "惊悚", ["紧张刺激", "心跳", "悬疑惊悚", "压迫感"]),
    ({"t": ["Superpower"]}, "超能力", ["异能", "特殊能力", "念力", "超能力战斗", "超常能力"]),
    ({"g": ["Supernatural"]}, "超自然", ["灵异", "驱魔", "退魔", "神秘现象", "灵异事件"]),
    ({"t": ["Vampire"]}, "吸血鬼", ["血族", "吸血鬼题材", "鲜血"]),
    ({"t": ["Ghost"]}, "幽灵", ["恶灵", "亡灵", "鬼怪", "鬼魂", "幽魂"]),
    ({"t": ["Youkai"]}, "妖怪", ["妖魔", "百鬼", "妖异", "妖怪题材", "化外之民"]),
    ({"t": ["Henshin", "Tokusatsu"]}, "变身", ["特摄", "变身英雄", "变身战斗"]),
    ({"t": ["Kaiju"]}, "怪兽", ["巨兽", "巨大生物", "怪兽大战"]),
    ({"t": ["Survival"]}, "生存", ["大逃杀", "荒野求生", "孤岛", "绝境求生", "生存游戏", "荒岛"]),
    ({"t": ["Dystopian"]}, "反乌托邦", ["极权社会", "乌托邦崩坏", "高压统治"]),
    ({"t": ["Espionage"]}, "特工", ["间谍", "谍战", "潜入", "谍报", "卧底"]),
    ({"t": ["Organized Crime"]}, "黑帮", ["黑道", "极道", "黑手党", "帮派", "不法之地"]),
    ({"t": ["Revenge"]}, "复仇", ["报仇", "复仇之路", "雪恨"]),
    ({"t": ["Gore"]}, "血腥", ["残暴", "猎奇", "残酷"]),
    ({"t": ["Guns"]}, "枪战", ["枪械", "射击", "火拼", "持枪"]),
    ({"t": ["Swordplay"]}, "剑术", ["刀剑", "拔刀", "剑技", "剑戟"]),
    ({"t": ["Archery"]}, "弓箭", ["射箭", "箭术", "弓道"]),
    # ===== 题材: 情感 / 受众 =====
    ({"g": ["Romance"]}, "恋爱", ["爱情", "甜蜜", "纯爱", "甜宠", "心动", "恋爱喜剧", "告白", "相恋", "恋爱日常", "恋爱番", "甜度爆表"]),
    ({"g": ["Harem"]}, "后宫", ["多女主", "后宫番", "众星捧月"]),
    ({"t": ["Reverse Harem"]}, "逆后宫", ["女尊", "一女多男"]),
    ({"t": ["Yuri"]}, "百合", ["GL", "少女之爱", "百合番", "姬情"]),
    ({"t": ["Boys' Love"]}, "耽美", ["BL", "男男", "腐向"]),
    ({"g": ["Drama"]}, "催泪", ["感人", "泪目", "感动", "看哭", "泪腺崩坏", "感人至深"]),
    ({"g": ["Drama", "Psychological"]}, "虐心", ["意难平", "BE", "心碎", "刀子", "致郁系"]),
    ({"g": ["Drama"]}, "亲情", ["家庭", "家人", "家族", "亲子", "手足"]),
    ({"g": ["Drama"]}, "友情", ["友谊", "伙伴", "同伴", "羁绊", "队友", "挚友"]),
    ({"g": ["Drama"], "t": ["Coming of Age"]}, "青春", ["成长", "青春期", "长大成人", "年少"]),
    ({"g": ["Ecchi"]}, "福利", ["卖肉", "色气", "擦边", "肉番", "杀必死"]),
    ({"g": ["Shounen"]}, "少年向", ["少年漫", "JUMP系", "少年热血"]),
    ({"g": ["Seinen"]}, "青年向", ["青年漫", "成人向", "写实向", "深刻向"]),
    ({"g": ["Kids"]}, "儿童向", ["儿童", "幼儿", "低龄向", "亲子向"]),
    ({"t": ["Iyashikei", "Slice of Life"]}, "空气系", ["悠闲日常", "轻松治愈"]),
    # ===== 角色特征 / 性格 =====
    ({"t": ["Tsundere"]}, "傲娇", ["娇蛮", "嘴硬心软", "口嫌体正直", "别扭", "娇嗔", "外冷内热"]),
    ({"t": ["Yandere"]}, "病娇", ["偏执", "疯批", "占有欲", "黑化"]),
    ({"t": ["Kuudere"]}, "高冷", ["三无", "冰山", "面瘫", "冷淡", "无口", "冷漠", "寡言"]),
    ({"t": ["Airhead"]}, "天然呆", ["呆萌", "迟钝", "少根筋", "迷糊", "懵懂"]),
    ({"t": ["Chuunibyou"]}, "中二", ["中二病", "邪气眼", "黑历史"]),
    ({"t": ["Ojou-Sama"]}, "大小姐", ["千金", "名门", "富家女", "娇生惯养"]),
    ({"t": ["Deredere"]}, "温柔", ["温柔体贴", "暖男", "治愈系角色", "温柔善良"]),
    ({"t": ["Shy"]}, "内向", ["怕生", "社恐", "害羞", "腼腆", "怯生"]),
    ({"t": ["Genki"]}, "元气", ["活泼", "元气满满", "开朗", "精力旺盛"]),
    ({"t": ["Loli"]}, "萝莉", ["幼女", "小萝莉", "年幼"]),
    ({"t": ["Shota"]}, "正太", ["小男孩", "年幼男孩"]),
    ({"t": ["Maid"]}, "女仆", ["女仆装", "侍女", "女仆咖啡厅"]),
    ({"t": ["Kemonomimi"]}, "兽耳", ["猫娘", "猫耳", "兽娘", "狐娘", "犬娘", "兔耳"]),
    ({"t": ["Elf"]}, "精灵", ["妖精", "精灵族", "精灵少女", "森林精灵"]),
    ({"t": ["Dragon"]}, "巨龙", ["恶龙", "龙族", "屠龙", "神龙", "龙王", "巨龙大战"]),
    ({"t": ["Villainess"]}, "恶役", ["反派千金", "恶役千金", "反派大小姐", "破产千金"]),
    ({"t": ["Siblings"]}, "兄妹", ["姐弟", "姐妹", "双胞胎", "手足情深", "兄妹情"]),
    ({"t": ["Idol Side of Life"]}, "偶像", ["偶像番", "偶像团体", "打歌舞台", "星光舞台"]),
    ({"t": ["Overpowered Main Characters"]}, "无敌", ["碾压", "最强", "龙傲天", "无敌流", "开挂", "实力碾压"]),
    ({"t": ["Genius"]}, "天才", ["高智商", "学霸", "天才少年", "天才少女"]),
    ({"t": ["Hard Work"]}, "努力", ["奋斗", "拼搏", "不服输", "刻苦"]),
    ({"t": ["Gag"]}, "整活", ["玩梗", "谐音梗", "冷笑话"]),
    ({"g": ["Fantasy"]}, "勇者", ["魔王", "讨伐魔王", "勇者斗魔王"]),
    ({"t": ["Ensemble Cast"]}, "群像", ["多主角", "群像剧", "众生相"]),
    ({"t": ["Art"]}, "美术", ["画画", "绘画", "艺术", "画师"]),
    ({"t": ["Music", "Idol Side of Life"]}, "音乐偶像", ["歌姬", "唱跳"]),
    # ===== 角色外貌 (发色 / 发型 / 瞳色配件): AniList 无发色类标签, 发色/双马尾用 seed 作品清单查询,
    # 其余外貌 tag 保留 (kemonomimi/elf/maids 等真实存在); tag 被丢弃时 reco_feature 有兜底保护 =====
    ({"seed": ["Frieren", "Re:Zero", "Angel Beats", "Guilty Crown", "Made in Abyss",
               "Fate/kaleid liner Prisma Illya", "Date A Live", "Wandering Witch"]},
     "白发", ["银发", "白毛", "白色头发", "白头发", "一头白发"]),
    ({"seed": ["Violet Evergarden", "Fate/stay night", "Youjo Senki",
               "Kono Subarashii Sekai ni Shukufuku wo", "Monogatari Series", "Eromanga Sensei"]},
     "金发", ["黄发", "金毛", "金色头发", "金头发", "一头金发"]),
    ({"seed": ["Shingeki no Kyojin", "Kaguya-sama wa Kokurasetai", "Spy x Family",
               "Bleach", "Jujutsu Kaisen"]},
     "黑发", ["黑毛", "黑色头发", "黑头发", "一头黑发"]),
    ({"seed": ["Ore, Twintail ni Narimasu", "Lucky Star", "Fate/stay night",
               "The Quintessential Quintuplets", "Ore no Imouto ga Konna ni Kawaii Wake ga Nai"]},
     "双马尾", ["双马尾巴", "双子马尾", "两束马尾"]),
    ({"seed": ["Bocchi the Rock", "Neon Genesis Evangelion", "Date A Live", "Higurashi no Naku Koro ni"]},
     "蓝发", ["蓝毛", "蓝色头发", "蓝头发"]),
    ({"seed": ["Miss Kobayashi's Dragon Maid", "Working!!", "The Quintessential Quintuplets"]},
     "粉发", ["粉毛", "粉色头发", "粉头发", "樱色头发"]),
    ({"seed": ["Oshi no Ko", "Date A Live", "Fate/Grand Order"]},
     "紫发", ["紫毛", "紫色头发", "紫头发"]),
    ({"seed": ["One Piece", "Fairy Tail", "Yona of the Dawn"]},
     "红发", ["红毛", "赤发", "红色头发", "红头发"]),
    ({"seed": ["Code Geass: Hangyaku no Lelouch", "One Piece", "Hatsune Miku", "One Punch Man"]},
     "绿发", ["绿毛", "绿色头发", "绿头发"]),
    ({"seed": ["Gintama", "Frieren", "Re:Zero"]},
     "灰发", ["灰色头发", "灰白头发", "银灰色头发"]),
    ({"seed": ["Sword Art Online", "Naruto", "Love Live! School Idol Project"]},
     "马尾", ["马尾辫", "单马尾", "扎马尾"]),
    ({"seed": ["K-On!", "Skip and Loafer", "A Place Further Than the Universe"]},
     "波波头", ["齐耳短发", "蘑菇头", "鲍伯头"]),
    ({"seed": ["Sailor Moon", "Naruto"]},
     "丸子头", ["团子头", "双丸子头", "发包"]),
    ({"seed": ["Fullmetal Alchemist", "Ranma 1/2"]},
     "辫子", ["麻花辫", "编发", "辫子头"]),
    ({"seed": ["Girls und Panzer", "Love Live! School Idol Project"]},
     "呆毛", ["头顶呆毛", "翘起的头发"]),
    ({"seed": ["Sailor Moon", "Inuyasha", "Kimetsu no Yaiba", "Violet Evergarden"]},
     "长发", ["一头长发", "长发飘飘", "过腰长发"]),
    ({"seed": ["Shingeki no Kyojin", "Jujutsu Kaisen"]},
     "清爽短发", ["利落短发", "一头短发", "短发"]),
    ({"seed": ["One Punch Man"]},
     "光头", ["秃头", "秃顶", "没有头发"]),
    ({"seed": ["Date A Live", "Hidan no Aria"]},
     "异色瞳", ["双色瞳", "异色双瞳", "两眼颜色不同"]),
    ({"seed": ["Detective Conan", "Hyouka", "Miss Kobayashi's Dragon Maid", "Wotakoi: Love is Hard for Otaku"]},
     "眼镜", ["戴眼镜", "眼镜娘", "四眼"]),
    # ===== 细分枚举 (同一信号多字面) =====
    ({"g": ["Sports"]}, "运动", ["体育", "竞技", "运动番", "篮球", "足球", "棒球", "排球", "网球", "游泳", "田径", "羽毛球", "乒乓球", "高尔夫", "滑冰", "拳击", "摔跤", "柔道", "剑道", "自行车", "体操", "冲浪", "攀岩", "围棋", "将棋", "象棋", "麻将", "下棋", "体育竞技", "台球", "保龄球", "击剑", "跳水", "花样滑冰", "冰球", "马拉松", "铁人三项", "啦啦队"]),
    ({"g": ["Music"]}, "音乐", ["乐队", "唱歌", "歌声", "钢琴", "吉他", "贝斯", "鼓手", "合唱", "小提琴", "爵士", "摇滚", "民谣", "作词", "作曲", "音乐题材", "主唱", "笛子", "二胡", "古筝", "口琴", "萨克斯"]),
    ({"t": ["Martial Arts"]}, "武侠", ["功夫", "武术", "格斗", "武道", "体术", "拳法", "踢馆", "切磋", "比武", "武侠世界", "江湖", "侠客", "门派", "武林"]),
    ({"t": ["School", "Mystery"]}, "学园推理", ["校园推理", "学园悬疑"]),
    ({"t": ["School"], "g": ["Romance"]}, "校园恋爱", ["校园爱情", "青春恋爱", "校园甜宠"]),
    ({"g": ["Mecha", "Action"]}, "机战热血", ["机体战斗"]),
    # ===== 扩充: 性格 / 角色标签 =====
    ({"t": ["Delinquents"]}, "不良少年", ["不良", "暴走族", "古惑仔", "问题少年", "太妹", "热血高校"]),
    ({"t": ["Gyaru"]}, "辣妹", ["时尚少女", "潮流少女", "金发少女", "辣妹子"]),
    ({"t": ["Otaku Culture"]}, "宅文化", ["御宅", "宅男", "宅女", "阿宅", "二次元", "家里蹲"]),
    ({"t": ["Teacher"]}, "教师", ["老师", "班主任", "教书育人", "导师", "家教"]),
    ({"t": ["Childcare"]}, "育儿", ["带娃", "婴儿", "幼儿园", "托儿所", "萌娃", "奶爸", "奶妈"]),
    ({"t": ["Showbiz"]}, "演艺圈", ["娱乐圈", "明星", "经纪人", "剧组", "艺人", "出道"]),
    ({"t": ["Anti-Hero"]}, "反英雄", ["非典型主角", "灰色主角", "亦正亦邪", "以恶制恶"]),
    ({"t": ["Amnesia"]}, "失忆", ["记忆丧失", "忘记过去", "记忆碎片"]),
    ({"t": ["Tragedy"]}, "悲剧", ["悲惨", "壮烈牺牲", "悲剧收场", "生离死别", "哀伤", "战死"]),
    ({"g": ["Comedy"], "t": ["Gag Humor"]}, "吐槽向", ["毒舌", "阴阳怪气", "嘴炮", "抬杠"]),
    ({"g": ["Comedy"]}, "谐星", ["搞笑艺人", "漫才", "短剧"]),
    ({"t": ["School"], "g": ["Romance"]}, "同桌", ["前后桌", "班级同学", "同桌的你"]),
    ({"t": ["School"], "g": ["Slice of Life"]}, "放学后", ["社团日常", "课间"]),
    ({"t": ["Crossdressing"]}, "女装", ["男扮女装", "女扮男装", "性转换", "男娘", "伪娘"]),
    ({"t": ["Love Triangle"]}, "三角恋", ["多角恋", "修罗场", "情敌", "争风吃醋", "两男一女", "两女一男"]),
    ({"t": ["Delinquents"], "g": ["Drama"]}, "校园霸凌", ["欺凌", "被欺负", "排挤"]),
    ({"t": ["Superpower"], "g": ["Action"]}, "异能战斗", ["能力对决", "念动力", "超能力者", "觉醒能力"]),
    # ===== 扩充: 情感 / 人际 =====
    ({"g": ["Romance"]}, "初恋", ["暗恋", "单恋", "青涩恋爱", "情书", "心动瞬间", "双向暗恋"]),
    ({"g": ["Romance"]}, "青梅竹马", ["两小无猜", "发小", "竹马", "邻家玩伴"]),
    ({"t": ["Age Gap"], "g": ["Romance"]}, "年龄差", ["姐弟恋", "忘年恋", "大叔与少女"]),
    ({"g": ["Romance"]}, "婚后恋爱", ["夫妻", "新婚生活", "先婚后爱", "婚后日常"]),
    ({"t": ["Coming of Age"]}, "毕业", ["毕业季", "离别", "青春散场", "最后一夏", "各奔东西"]),
    ({"g": ["Drama"], "t": ["Coming of Age"]}, "叛逆", ["离家出走", "对抗父母", "青春迷茫"]),
    ({"g": ["Drama"]}, "疾病", ["绝症", "患病", "轮椅", "康复", "住院"]),
    ({"g": ["Drama"]}, "贫穷", ["穷困", "拮据", "底层生活"]),
    ({"g": ["Romance"], "t": ["Comedy"]}, "欢喜冤家", ["斗嘴", "冤家路窄", "相爱相杀"]),
    ({"g": ["Romance"], "t": ["Drama"]}, "虐恋", ["纠缠", "爱恨情仇", "误会重重"]),
    # ===== 扩充: 剧情类型 / 元素 =====
    ({"g": ["Adventure"]}, "冒险", ["探险", "秘境", "寻宝", "远征", "开拓", "未知世界"]),
    ({"g": ["Action"]}, "热血", ["沸腾", "斗志", "热血番", "激情", "超燃", "燃向"]),
    ({"g": ["Action"]}, "战斗", ["打斗", "肉搏", "厮杀", "动作大片", "激战"]),
    ({"g": ["Psychological"]}, "反套路", ["反转", "神展开", "意料之外"]),
    ({"g": ["Mystery"]}, "密室", ["暴风雪山庄", "诡计", "谜题", "不在场证明"]),
    ({"g": ["Mystery"]}, "怪盗", ["神偷", "盗窃预告函", "盗贼"]),
    ({"t": ["Dungeon"], "g": ["Fantasy"]}, "地下城", ["迷宫", "攻略迷宫", "爬塔"]),
    ({"t": ["Alchemy"], "g": ["Fantasy"]}, "炼金", ["炼金术师", "调合", "炼丹"]),
    ({"t": ["Guilds"], "g": ["Fantasy"]}, "公会", ["冒险者公会", "佣兵团", "接委托"]),
    ({"t": ["Bounty Hunter"], "g": ["Action"]}, "赏金猎人", ["悬赏", "通缉犯猎人"]),
    ({"t": ["Slow Life"]}, "慢生活", ["种田", "田园生活", "悠闲度日", "佛系", "咸鱼", "躺平", "开店日常"]),
    ({"t": ["Time Skip"]}, "时间跳跃", ["多年以后", "长大之后", "跨越时间"]),
    ({"t": ["Body Swap"]}, "互换身体", ["交换身体", "灵魂互换", "男女互换"]),
    ({"g": ["Supernatural"]}, "诅咒", ["咒术", "怨灵", "封印"]),
    ({"g": ["Supernatural"]}, "附身", ["灵魂出窍", "夺舍", "鬼上身"]),
    ({"g": ["Horror"]}, "都市怪谈", ["细思极恐", "规则怪谈", "网络怪谈"]),
    ({"t": ["Psychological"], "g": ["Thriller"]}, "猫鼠游戏", ["斗智", "智商对决", "高智商对决"]),
    ({"g": ["Adventure"], "t": ["Survival"]}, "荒野", ["无人区", "原始丛林", "探险求生"]),
    ({"g": ["Drama"], "t": ["School"]}, "青春疼痛", ["迷茫", "彷徨"]),
    ({"t": ["Martial Arts"], "g": ["Action"]}, "拳脚格斗", ["搏击赛", "空手道", "泰拳"]),
    ({"t": ["Isekai"], "g": ["Comedy"]}, "穿越喜剧", ["搞笑穿越", "穿越吐槽"]),
    ({"t": ["Reincarnation", "Villainess"]}, "恶役转生", ["穿成反派", "反派重生", "重生恶役"]),
    ({"g": ["Thriller"], "t": ["Survival"]}, "绝命游戏", ["死亡游戏", "赌命", "死亡竞赛"]),
    ({"g": ["Comedy"], "t": ["Workplace"]}, "社畜喜剧", ["摸鱼", "职场吐槽", "开会地狱"]),
    ({"g": ["Fantasy"], "t": ["Adventure"]}, "勇者小队", ["勇者一行", "讨伐之旅", "旅途伙伴"]),
    ({"g": ["Fantasy"], "t": ["Pirates"]}, "宝藏", ["藏宝图", "黄金乡", "传说宝藏"]),
    # ===== 扩充: 种族 / 存在 =====
    ({"t": ["Mythology"]}, "神话", ["众神", "北欧神话", "希腊神话", "神话传说"]),
    ({"t": ["Aliens"]}, "外星人", ["外星生物", "UFO", "天外来客", "外星入侵"]),
    ({"t": ["Angels"]}, "天使", ["炽天使", "守护天使", "天界"]),
    ({"t": ["Demons"]}, "恶魔", ["魔族", "魔界", "恶魔领主", "魅魔"]),
    ({"t": ["Assassins"]}, "刺客", ["暗杀", "杀手", "刺杀"]),
    ({"t": ["Afterlife"]}, "冥界", ["地府", "阴间", "死后世界", "来世", "彼岸"]),
    ({"t": ["Anthropomorphism"]}, "拟人", ["娘化", "拟人化", "舰娘", "刀剑拟人", "动物拟人"]),
    ({"t": ["Monster Girl"]}, "魔物娘", ["蛇娘", "蜘蛛娘", "魔物少女"]),
    ({"t": ["Slime"]}, "史莱姆", ["软泥怪", "黏液怪"]),
    ({"t": ["Knights"], "g": ["Fantasy"]}, "骑士", ["圣骑士", "骑士团", "圆桌骑士"]),
    ({"t": ["Royalty"], "g": ["Fantasy"]}, "王室", ["公主", "王子", "国王", "王女", "宫廷"]),
    ({"t": ["Mermaid"], "g": ["Fantasy"]}, "人鱼", ["美人鱼", "人鱼公主"]),
    ({"g": ["Fantasy"]}, "兽人", ["狼人", "半兽人", "兽族"]),
    ({"g": ["Fantasy"]}, "魔物讨伐", ["讨伐魔物", "狩猎魔物"]),
    ({"g": ["Supernatural"], "t": ["Youkai"]}, "妖狐", ["狐妖", "猫妖", "九尾狐", "百鬼夜行"]),
    ({"t": ["Cultivation"], "g": ["Fantasy"]}, "宗门", ["宗门弟子", "灵根", "丹药"]),
    ({"t": ["Magic"], "g": ["Fantasy"]}, "魔杖", ["施法者", "咒文"]),
    ({"g": ["Sci-Fi"], "t": ["Space"]}, "星际大战", ["星海战争", "银河战争", "外星战争"]),
    # ===== 扩充: 职业 / 场景 / 生活 =====
    ({"t": ["Police"], "g": ["Drama"]}, "警察", ["刑警", "警官", "缉毒", "治安"]),
    ({"t": ["Manga"], "g": ["Drama"]}, "漫画家", ["画漫画", "连载", "责编", "漫画投稿"]),
    ({"t": ["Photography"]}, "摄影", ["拍照", "相机", "摄影师", "写真"]),
    ({"g": ["Slice of Life"]}, "乡下", ["田园", "农村", "乡村生活", "归隐"]),
    ({"g": ["Slice of Life"]}, "温泉", ["泡汤", "汤泉", "旅馆", "泡澡"]),
    ({"g": ["Slice of Life"]}, "咖啡厅", ["咖啡馆", "咖啡店", "喫茶店", "咖啡师"]),
    ({"g": ["Slice of Life"]}, "露营", ["野营", "篝火", "帐篷", "野外生活"]),
    ({"g": ["Slice of Life"]}, "钓鱼", ["垂钓", "海钓", "渔村"]),
    ({"g": ["Slice of Life"]}, "登山", ["爬山", "徒步", "攀登"]),
    ({"g": ["Slice of Life"]}, "滑雪", ["单板滑雪", "雪场"]),
    ({"g": ["Slice of Life"]}, "海边", ["海滨", "沙滩", "度假"]),
    ({"t": ["Cooking"]}, "甜品", ["蛋糕", "烘焙", "点心", "下午茶", "甜点师"]),
    ({"t": ["School"]}, "考试", ["备考", "入学考试", "补习", "高考", "成绩"]),
    ({"t": ["School"]}, "学霸与学渣", ["差生", "垫底", "补考"]),
    ({"t": ["School"], "g": ["Slice of Life"]}, "女子高中", ["女校", "女生宿舍", "男生宿舍"]),
    ({"t": ["Economics"]}, "经商", ["生意", "经营", "财阀", "商业战"]),
    ({"t": ["Workplace"]}, "打工", ["兼职", "打零工", "打工仔"]),
    ({"g": ["Slice of Life"], "t": ["Iyashikei"]}, "慢节奏", ["节奏舒缓", "慵懒", "闲适"]),
    ({"g": ["Sci-Fi"]}, "人工智能", ["AI觉醒", "AI伴侣", "仿生人", "智能机械"]),
    ({"g": ["Sci-Fi"]}, "克隆", ["复制人", "基因改造", "克隆人", "人体实验"]),
    ({"g": ["Sci-Fi"]}, "多元宇宙", ["平行宇宙", "位面", "多重世界"]),
    ({"t": ["Steampunk"]}, "蒸汽朋克", ["蒸汽机械", "齿轮", "维多利亚风"]),
    ({"t": ["Urban Fantasy"]}, "都市异能", ["现代魔法", "都市传说", "隐世超凡"]),
    ({"t": ["Naval"], "g": ["Action"]}, "海战", ["舰队", "军舰", "大海战"]),
    ({"g": ["Music"]}, "虚拟歌手", ["初音", "V家", "电子歌姬", "虚拟偶像"]),
    ({"g": ["Music"], "t": ["School"]}, "吹奏乐", ["管乐队", "吹奏部", "军乐队"]),
    ({"g": ["Music"], "t": ["Showbiz"]}, "选秀", ["练习生", "海选", "出道战"]),
    ({"g": ["Music"], "t": ["Drama"]}, "音乐之路", ["追梦", "音乐梦想", "演奏家"]),
    ({"t": ["Dance"]}, "舞蹈", ["跳舞", "街舞", "芭蕾", "舞娘", "舞团"]),
    ({"t": ["Travel"]}, "旅行", ["环游世界", "背包客", "远行", "公路旅行"]),
    ({"g": ["Shoujo"]}, "少女向", ["少女漫", "少女番", "乙女向", "女性向"]),
    ({"g": ["Sports"]}, "球王", ["绿茵场", "球场", "联赛"]),
    ({"g": ["Sports"], "t": ["Comedy"]}, "运动喜剧", ["爆笑运动", "搞笑竞技"]),
    ({"g": ["Mystery"], "t": ["Psychological"]}, "高智商犯罪", ["犯罪侧写", "罪犯", "犯罪心理"]),
    ({"t": ["Mythology"], "g": ["Fantasy"]}, "神社", ["祭典", "夏日祭", "庙会"]),
]

# 展开: 核心词 + 同义词 -> 同一信号 (setdefault 防跨组撞名时覆盖先到信号)
_FEAT_MAP = {}
for _sig, _core, _syns in _FEAT_GROUPS:
    _FEAT_MAP[_core] = _sig
    for _s in _syns:
        _FEAT_MAP.setdefault(_s, _sig)
_FEAT_KEYS = sorted(_FEAT_MAP, key=len, reverse=True)   # 预排序, 扫描长词优先

_anilist_tags_cache = [None, 0.0]   # [tag 名集合, 取回时间]


def _anilist_tag_set():
    """AniList 全量标签名集合 (24h 缓存); 拉取失败返回 None, 调用侧跳过校验"""
    if _anilist_tags_cache[0] is not None and time.time() - _anilist_tags_cache[1] < 24 * 3600:
        return _anilist_tags_cache[0]
    try:
        res = _anilist_graphql("query{MediaTagCollection{name}}", {})
        names = frozenset(str(t.get("name") or "").strip().lower()
                          for t in (res.get("data") or {}).get("MediaTagCollection") or [])
    except Exception as e:
        print("[reco] tag 枚举拉取失败: %r" % (e,), flush=True)
        return None
    _anilist_tags_cache[0], _anilist_tags_cache[1] = names, time.time()
    return names


def _feat_resolve_tags(tags, tset):
    """词典 tag 名 -> 实际存在的 AniList tag: 精确小写命中; 否则归一化互相包含模糊匹配; 均不中丢弃"""
    if not tset:
        return list(tags)
    out = []
    for t in tags:
        lt = t.strip().lower()
        if lt in tset:
            out.append(t)
            continue
        fuzzy = next((e for e in tset if lt in e or e in lt), None)
        if fuzzy:
            out.append(fuzzy)
    return out


def _anilist_seed_medias(seeds):
    """外貌特征 seed 代表作清单 -> 批量别名搜索 (24h 缓存)。
    AniList 无发色/发型类标签, 白发/金发等外貌特征以「经典代表作」为种子检索;
    注意 Media(search:) 无命中时 AniList 返回 HTTP 404 且整批 data 置 null,
    故批量失败时降级逐个查询, 单 seed 失配只跳过自身不影响其余"""
    seeds = sorted(set(s for s in (seeds or []) if s))
    if not seeds:
        return []
    key = "reco:seed:" + "|".join(seeds)
    hit = cache_get(key)
    if hit is not None:
        return hit
    fields = ("id averageScore episodes seasonYear coverImage{large} "
              "title{native romaji english} synonyms")
    medias = []
    try:
        body = " ".join("m%d:Media(search:%s,type:ANIME){%s}"
                        % (i, json.dumps(s, ensure_ascii=False), fields)
                        for i, s in enumerate(seeds))
        data = (_anilist_graphql("query{%s}" % body, {}).get("data") or {})
        medias = [data.get("m%d" % i) for i in range(len(seeds))]
        medias = [m for m in medias if m and m.get("id")]
    except Exception as e:
        print("[reco] seed 批量搜索失败, 降级逐个查询 (%d 个): %r" % (len(seeds), e), flush=True)
        medias = []
    if not medias:
        for s in seeds:   # 降级: 逐个查询, 单 seed 无命中只影响自己
            try:
                data = (_anilist_graphql(
                    "query{Media(search:%s,type:ANIME){%s}}" % (json.dumps(s, ensure_ascii=False), fields),
                    {}).get("data") or {})
                m = data.get("Media")
                if m and m.get("id"):
                    medias.append(m)
            except Exception:
                continue
    cache_put(key, medias, 24 * 3600)
    return medias


def reco_feature(kw):
    """特征找番: 输入含角色特征/性格/剧情类型等模糊描述, 扫描特征词典命中后按 AniList
    标签+题材组合筛选人气条目 (发色等无标签的外貌特征改用 seed 代表作检索), 反查中文名按评分
    排序; 未命中任何特征词时回退普通关键词推荐 (缓存 6h)"""
    kw = _clean(kw)
    if not kw:
        raise RuntimeError("描述不能为空")
    text = kw.lower()
    hits_g, hits_t, hits_seed, hits = set(), set(), set(), []
    for name in _FEAT_KEYS:
        if name in kw or name in text:
            sig = _FEAT_MAP[name]
            hits_g.update(sig.get("g") or [])
            hits_t.update(sig.get("t") or [])
            hits_seed.update(sig.get("seed") or [])
            if name not in hits:
                hits.append(name)
                if len(hits) >= 6:   # 长句命中过多时截断, 防组合过严查空
                    break
    if not hits:
        out = reco_recommend(kw)   # 无特征词: 与普通搜索一致的兜底
        out["mode"] = "kw"
        return out
    key = "reco:feat:" + "|".join(sorted(hits))
    hit = cache_get(key)
    if hit:
        return hit
    tags = _feat_resolve_tags(sorted(hits_t), _anilist_tag_set())
    medias = []
    if hits_g or tags:
        medias = _anilist_media_list(genres=sorted(hits_g) or None, tags=tags or None, per=40)
        # 组合过严 (标签+题材 AND) 结果不足时, 降级只用题材维度补足 (tag 全被丢弃时首查已是纯题材, 无需重查)
        if len(medias) < 8 and hits_g and tags:
            medias = medias + _anilist_media_list(genres=sorted(hits_g), per=40)
    if hits_seed:
        medias = _anilist_seed_medias(sorted(hits_seed)) + medias
    if not medias and hits_t and not tags and not hits_g and not hits_seed:
        # tag 全被丢弃且无题材/代表作可用: 不做热门兜底 (那是错误结果), 直接返回空并短缓存
        out = {"kw": kw, "mode": "feature", "hits": hits, "items": []}
        cache_put(key, out, 600)
        return out
    seen, uniq = set(), []
    for m in medias:
        if m.get("id") in seen:
            continue
        seen.add(m.get("id"))
        uniq.append(m)
    try:
        rev = _bgdata_reverse_map()
    except Exception:
        rev = {}
    items = []
    for m in uniq:
        sc = m.get("averageScore") or 0
        if not sc:
            continue
        title, cover, score, year = _anilist_title_cn(m, rev)
        if not title:
            continue
        items.append({"title": title, "cover": cover, "id": "", "ep": "",
                      "year": year, "score": score, "ext": True})
    items.sort(key=lambda x: -x["score"])
    items = items[:24]
    for i, it in enumerate(items):
        it["rank"] = i + 1
    out = {"kw": kw, "mode": "feature", "hits": hits, "items": items}
    # 空结果短缓存 10 分钟 (seed/上游偶发失败时避免长时间负缓存), 有结果正常 6h
    cache_put(key, out, 6 * 3600 if items else 600)
    return out


def reco_recommend(kw):
    """关键词推荐: AGE 标签热门 / 聚合搜索 / AniList 题材映射三路候选, 评分降序排名 (缓存 6h)"""
    kw = _clean(kw)
    if not kw:
        raise RuntimeError("关键词不能为空")
    key = "reco:kw:" + _norm_map_title(kw)
    hit = cache_get(key)
    if hit:
        return hit
    merged = {}   # 归一化标题 -> 条目 (去重)

    def _add_age(lst):
        for it in lst or []:
            t = _clean(it.get("title") or "")
            nt = _norm_map_title(t)
            if not t or not nt or nt in merged:
                continue
            merged[nt] = {"title": t, "cover": it.get("cover") or "",
                          "id": str(it.get("id") or ""), "ep": it.get("ep") or "",
                          "year": "", "score": 0.0, "ext": False}

    # 1) 命中 AGE 标签 -> 该分类热门列表 (片源直连)
    if kw in AGE_TAGS:
        try:
            _add_age(age_catalog(page=1, genre=kw, order="hot").get("items"))
        except Exception as e:
            print("[reco] 标签候选失败 (%s): %r" % (kw, e), flush=True)
    # 2) 聚合搜索 (标题/昵称/角色名, 内部自带缓存与限流)
    if len(merged) < 12:
        try:
            _add_age(anime_search(kw))
        except Exception as e:
            print("[reco] 聚合搜索候选失败: %r" % (e,), flush=True)
    # 3) AniList 题材映射 -> 人气条目反查中文名 (无片源 id, 点击时经 /reco/go 解析)
    gm = _RECO_GENRE_MAP.get(kw) or _RECO_GENRE_MAP.get(_norm_map_title(kw))
    if gm:
        try:
            rev = _bgdata_reverse_map()
            for m in _anilist_media_list(genres=gm.get("g"), tags=gm.get("t"), per=40)[:14]:
                t = m.get("title") or {}
                cands = [_clean(t.get("native") or ""), _clean(t.get("romaji") or ""),
                         _clean(t.get("english") or "")]
                title = ""
                for c in cands:
                    if c and rev.get(_norm_map_title(c)):
                        title = rev[_norm_map_title(c)]
                        break
                if not title:
                    title = next((c for c in cands if c), "")
                nt = _norm_map_title(title)
                if not nt or nt in merged:
                    continue
                merged[nt] = {"title": title, "cover": ((m.get("coverImage") or {}).get("large")) or "",
                              "id": "", "ep": "", "year": str(m.get("seasonYear") or ""),
                              "score": round((m.get("averageScore") or 0) / 10.0, 1), "ext": True}
        except Exception as e:
            print("[reco] AniList 题材候选失败: %r" % (e,), flush=True)
    items = list(merged.values())[:30]
    _reco_score_batch(items)   # AGE 条目批量补评分 (AniList 条目自带评分)
    items.sort(key=lambda x: -x["score"])
    for i, it in enumerate(items):
        it["rank"] = i + 1
    out = {"kw": kw, "items": items}
    cache_put(key, out, 6 * 3600)
    return out


def _yearly_bgm_enrich(items):
    """年度榜 Bangumi 评分充实: 并行抓取每部番的 Bangumi 评分, 命中则覆盖 AniList 分
    (Bangumi 评分对国内用户更有参考性); bgm.tv 不可达时 metadata_info 自动短路回退, 不影响原分"""
    if not items:
        return
    def _one(it):
        try:
            info = metadata_info(it["title"])
        except Exception:
            return
        if info and (info.get("score") or 0) > 0:
            it["score"] = round(float(info["score"]), 1)
            it["bgm"] = 1
    with ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(_one, items))


def reco_yearly(year, force=False):
    """年度评分榜: AniList 该年份人气 TOP50 -> Bangumi 评分充实(并行抓取, Bangumi 分优先)
    -> 按评分排序 TOP24 (缓存 24h; force=True 绕过缓存用于定时自动刷新)"""
    try:
        year = int(year or 0)
    except ValueError:
        year = 0
    if year <= 1960 or year > time.localtime().tm_year + 1:
        year = time.localtime().tm_year
    key = "reco:year:%d" % year
    hit = cache_get(key)
    if hit and not force:
        return hit
    medias = _anilist_media_list(year=year, sort="POPULARITY_DESC", per=50)
    try:
        rev = _bgdata_reverse_map()
    except Exception:
        rev = {}
    items = []
    for m in medias:
        sc = m.get("averageScore") or 0
        if not sc:
            continue
        t = m.get("title") or {}
        cands = [_clean(t.get("native") or ""), _clean(t.get("romaji") or ""),
                 _clean(t.get("english") or "")]
        title = ""
        for c in cands:
            if c and rev.get(_norm_map_title(c)):
                title = rev[_norm_map_title(c)]
                break
        if not title:
            title = next((c for c in cands if c), "")
        if not title:
            continue
        items.append({"title": title, "cover": ((m.get("coverImage") or {}).get("large")) or "",
                      "id": "", "score": round(sc / 10.0, 1), "year": year,
                      "eps": m.get("episodes") or 0})
    _yearly_bgm_enrich(items)
    items.sort(key=lambda x: -x["score"])
    items = items[:24]
    for i, it in enumerate(items):
        it["rank"] = i + 1
    out = {"year": year, "items": items}
    cache_put(key, out, 24 * 3600)
    return out


def reco_resolve(kw):
    """AniList 条目 -> AGE 可播放条目 (点击推荐卡/年度榜时按标题解析片源)。
    轻量预算: AGE 直搜 + bangumi-data/别名词典候选 + AniList 候选, AGE 检索上限 6 次;
    整体 22s 硬超时; 结果含负缓存, 避免反复撞不可解析的标题"""
    kw = _clean(kw)
    if not kw:
        raise RuntimeError("标题不能为空")
    key = "reco:go:" + _norm_map_title(kw)
    hit = cache_get(key)
    if hit is not None:
        if hit:
            return hit
        raise RuntimeError("未找到可播放片源")

    def _work():
        def _first(items):
            items = _rel_items(items, kw)
            return items[0] if items else None

        out = _first(_age_search_safe(kw))   # 1) AGE 直接标题搜索
        if not out:
            cands, seen = [], set()

            def _cand(t):
                t = _clean(t)
                if t and t not in seen and _norm_map_title(t) != _norm_map_title(kw):
                    seen.add(t)
                    cands.append(t)

            try:
                _bgdata_candidates(kw, _cand)   # 2) bangumi-data 中文标题候选
            except Exception:
                pass
            try:
                _alias_candidates(kw, _cand)    # 3) 中文昵称/角色名候选
            except Exception:
                pass
            for t in cands[:4]:
                out = _first(_age_search_safe(t))
                if out:
                    break
        if not out:   # 4) AniList 媒体/角色映射候选 (日文原名兜底)
            acands = []
            try:
                _anilist_candidates(kw, lambda t: acands.append(t) if len(acands) < 4 else None)
            except Exception:
                pass
            for t in acands[:2]:
                out = _first(_age_search_safe(t))
                if out:
                    break
        if out:
            return {"id": str(out.get("id") or ""), "title": out.get("title") or "",
                    "cover": out.get("cover") or ""}
        return {}

    result, timed_out = {}, False
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        result = pool.submit(_work).result(timeout=22)
    except TimeoutError:
        timed_out = True
        print("[reco] 解析超时: %s" % kw, flush=True)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    cache_put(key, result, 120 if timed_out else 600)
    if not result.get("id"):
        raise RuntimeError("未找到可播放片源")
    return result


def bangumi_info(title):
    """按标题搜索 Bangumi 动画条目, 返回 {id,name,name_cn,score,total,rank,summary,date,image}"""
    title = _clean(title)
    if not title:
        return {}
    key = "bgm:" + title
    hit = cache_get(key)
    if hit is not None:
        return hit
    info = {}
    for kw in _bgm_title_variants(title):
        try:
            items = _bgm_search_ids(kw)
            if not items:
                continue
            sid = _bgm_pick(items, kw)[0].get("id")
            if not sid:
                continue
            d = _bgm_request("/v0/subjects/" + str(sid))
            rating = d.get("rating") or {}
            info = {
                "id": sid,
                "name": d.get("name") or "",
                "name_cn": d.get("name_cn") or "",
                "score": rating.get("score") or 0,
                "total": rating.get("total") or 0,
                "rank": rating.get("rank") or 0,
                "summary": (d.get("summary") or "").strip(),
                "date": d.get("date") or "",
                "eps": d.get("eps") or 0,
                "image": (d.get("images") or {}).get("common") or "",
            }
            break
        except Exception as e:
            print("[bangumi] 条目获取失败 (%s): %r" % (kw, e), flush=True)
            continue
    cache_put(key, info, 86400 if info else 600)   # 命中缓存1天, 未命中缓存10分钟
    return info
