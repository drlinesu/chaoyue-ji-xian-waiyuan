# -*- coding: utf-8 -*-
"""AGE 动漫源: 域名探测/首页/搜索/详情/线路解析/更新表"""
import json
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib import parse as up
from urllib.parse import unquote
from html import unescape

from ..textutil import _clean
from ..cache import cache_get, cache_put
from ..net import http_get_text
from .ext import _EXT_LINE_PREFIX, ext_resolve_play


AGE_BASES = [
    "https://www.agefans.com",
    "https://www.agedm.io",
    "https://agemys.org",
    "https://www.agefans.cc",
]
# 快看漫画（官方平台，SSR 数据 + 带签名图片直链）
KK_API = "https://api.kuaikanmanhua.com"
KK_WEB = "https://www.kuaikanmanhua.com"

_age_base = None
_base_lock = threading.Lock()

# ----------------------------------------------------------------------------
# HTTP 工具
# ----------------------------------------------------------------------------


# ----------------------------------------------------------------------------
# AGE 动漫源
# ----------------------------------------------------------------------------
# 探测失败的域名 -> 失败时间戳 (仅连接级失败才拉黑 2 分钟, 内容不符不拉黑)
_dead_bases = {}
_DEAD_TTL = 120



def age_base(force=False):
    global _age_base
    with _base_lock:
        if _age_base and not force:
            return _age_base
        for round_no in (0, 1):
            now = time.time()
            cands = [b for b in AGE_BASES if now - _dead_bases.get(b, 0) >= _DEAD_TTL]
            if not cands:
                if round_no == 0:
                    _dead_bases.clear()   # 全部在黑名单时清档重试一轮, 避免网络抖动后长时间误报
                    continue
                break

            def _probe(b):
                try:
                    t = http_get_text(b + "/", timeout=8, tries=1)
                    return b if "/detail/" in t else None   # 能连上但内容异常不拉黑, 交给下一轮
                except Exception:
                    _dead_bases[b] = time.time()
                    return None

            # 并行探测候选域名, 最先验证成功者胜出 (避免逐个撞死域名串行超时)
            pool = ThreadPoolExecutor(max_workers=len(cands))
            futs = {}
            winner = None
            try:
                futs = {pool.submit(_probe, b): b for b in cands}
                for fut in as_completed(futs):
                    r = fut.result()
                    if r:
                        winner = r
                        break
            finally:
                for f in futs:
                    f.cancel()
                pool.shutdown(wait=False, cancel_futures=True)
            if winner:
                _age_base = winner
                return winner
        raise RuntimeError("AGE动漫各域名均不可访问, 请检查网络或稍后再试")


def _age_fetch(path, referer=None, timeout=12, tries=3):
    """抓取 AGE 页面: 失败自动换域名重试, 全部失败抛 RuntimeError (客户端可见)"""
    last = None
    for attempt in range(tries):
        try:
            base = age_base(force=attempt > 0)
            return base, http_get_text(base + path, referer=referer or base, timeout=timeout)
        except Exception as e:
            last = e
            if attempt < tries - 1:
                time.sleep(0.5)
    raise RuntimeError("AGE动漫暂时无法访问, 请稍后重试 (%s)" % last)


def _abs_url(u, base):
    if not u:
        return ""
    u = u.strip()
    if u.startswith("//"):
        return "https:" + u
    if u.startswith("http"):
        return u
    if u.startswith("/"):
        return base + u
    return u




def unwrap_img(u, base):
    """还原被百度图床代理包裹的真实封面地址, 处理 &amp; 转义"""
    u = unescape(u or "").strip()
    if not u:
        return ""
    if ("gimg" in u or "bdxigua" in u or "baidu" in u) and "src=" in u:
        m = re.search(r"[?&]src=([^&\"]+)", u)
        if m:
            try:
                u = unquote(m.group(1))
            except Exception:
                pass
    return _abs_url(u, base)


def _parse_video_cards(html, base, limit=48):
    """解析 AGE 通用视频卡片 (cata_video_item): 标题/封面/更新集数/信息"""
    items = []
    blocks = re.split(r'class="card cata_video_item', html)
    for b in blocks[1:]:
        idm = re.search(r'/detail/(\d+)', b)
        if not idm:
            continue
        tm = re.search(r'title="([^"]{2,120})"', b)
        if not tm:
            tm = re.search(r'<h5 class="card-title"><a[^>]*>([^<]{2,120})</a>', b)
        title = unescape(tm.group(1)).strip() if tm else ""
        if not title:
            continue
        cm = re.search(r'data-original="([^"]+)"', b) or re.search(
            r'(?:data-src|src)="([^"]+?\.(?:jpg|jpeg|png|webp)[^"]*)"', b)
        cover = unwrap_img(cm.group(1), base) if cm else ""
        if not cover:
            cover = _age_cover(idm.group(1))
        em = re.search(r'video_play_status[^>]*>([^<]+)<', b)
        ep = em.group(1).strip() if em else ""
        body = _clean(re.sub(r"<h5.*?</h5>", "", b))[:150]
        items.append({"id": idm.group(1), "title": title, "cover": cover,
                      "ep": ep, "info": body})
        if len(items) >= limit:
            break
    return items


def _extract_episodes(block, base):
    """从 HTML 片段中提取 /play/{id}/{line}/{ep} 链接"""
    eps = []
    for m in re.finditer(
            r'href="[^"]*?/play/(\d+)/(\d+)/(\d+)[^"]*"[^>]*>(.*?)</a>',
            block, re.S):
        line, ep = int(m.group(2)), int(m.group(3))
        name = _clean(m.group(4)) or ("第%02d集" % ep)
        eps.append({"line": line, "ep": ep, "name": name,
                    "pid": m.group(1)})
    return eps


def age_home(refresh=False):
    key = "age:home"
    if not refresh:
        hit = cache_get(key)
        if hit:
            return hit
    base, html = _age_fetch("/", timeout=12)
    items = _parse_video_cards(html, base)
    if len(items) < 8:
        # 备选通用解析
        got = {}
        for m in re.finditer(r'<a[^>]+href="[^"]*?/detail/(\d+)[^"]*"[^>]*>(.*?)</a>',
                             html, re.S):
            pid = m.group(1)
            chunk = html[max(0, m.start() - 600): m.end() + 300]
            title = ""
            tm = re.search(r'title="([^"]{2,80})"', chunk)
            if tm:
                title = unescape(tm.group(1))
            if not title:
                title = _clean(m.group(2))[:40]
            if not title or title.startswith("http"):
                continue
            cover = ""
            for cm in re.finditer(r'(?:data-original|data-src|src)="([^"]+?\.(?:jpg|jpeg|png|webp)[^"]*)"', chunk):
                cu = cm.group(1)
                if "logo" in cu.lower() or "icon" in cu.lower() or len(cu) < 10:
                    continue
                cover = unwrap_img(cu, base)
                break
            epm = re.search(r'(第\s*\d+\s*[集话]|全集|\[TV[^\]<]{0,20}\]|剧场版|OVA)', chunk)
            ep_label = epm.group(1).replace(" ", "") if epm else ""
            it = got.setdefault(pid, {"id": pid, "title": title, "cover": cover,
                                      "ep": "", "info": ""})
            if cover and not it["cover"]:
                it["cover"] = cover
            if not it["cover"]:
                it["cover"] = _age_cover(pid)
            if len(title) > len(it["title"]):
                it["title"] = title
            if ep_label:
                it["ep"] = ep_label
        items = list(got.values())
    if not items:
        raise RuntimeError("首页解析失败")
    items = items[:80]
    cache_put(key, items, 180)
    return items


def age_search(kw):
    key = "age:search:" + kw
    hit = cache_get(key)
    if hit:
        return hit
    base = age_base()
    q = up.quote(kw)
    html = None
    last_err = None
    for path in ("/search?query=" + q, "/search?keyword=" + q):
        try:
            html = http_get_text(base + path, referer=base + "/")
            if "/detail/" in html:
                break
        except Exception as e:
            last_err = e
    if not html or "/detail/" not in html:
        raise RuntimeError("搜索失败: %s" % last_err)
    items = _parse_video_cards(html, base, limit=36)
    if not items:
        raise RuntimeError("未搜索到结果")
    cache_put(key, items, 1800)   # 搜索结果缓存 30 分钟 (目录以天变动, 重复搜索/候选词直接命中)
    return items


# AGE 站点真实分类标签 (来自 /catalog 筛选面板, 共44个, 按站点顺序)
AGE_TAGS = [
    "搞笑", "运动", "励志", "热血", "战斗", "竞技", "校园", "青春",
    "爱情", "恋爱", "冒险", "后宫", "百合", "治愈", "萝莉", "魔法",
    "悬疑", "推理", "奇幻", "科幻", "游戏", "神魔", "恐怖", "血腥",
    "机战", "战争", "犯罪", "历史", "社会", "职场", "剧情", "伪娘",
    "耽美", "童年", "教育", "亲子", "真人", "歌舞", "肉番", "美少女",
    "轻小说", "吸血鬼", "女性向", "泡面番", "欢乐向",
]
# 前端展示用: (标签, 名称) — 标签即名称
AGE_GENRES = [(t, t) for t in AGE_TAGS]
# 首页快捷分类 (副维度, 仅用于首页快捷入口, 点击跳 /catalog)
AGE_YEARS = [(2025, "2025"), (2024, "2024"), (2023, "2023"),
             (2022, "2022"), (2021, "2021"), (2020, "2020")]
AGE_REGIONS = [("japan", "日本"), ("china", "国产"), ("usa", "欧美"),
               ("korea", "韩国"), ("other", "其他")]
AGE_STATUS = [("finish", "完结"), ("ongoing", "连载中")]
AGE_ORDERS = [("time", "最新"), ("hot", "热门")]
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
# AGE 封面 CDN 模式 (从 anime ID 构造, 用于更新页等无封面的场景)
AGE_CDN_COVER = "https://cdn.aqdstatic.com:966/age/covers/%s.jpg"


def _age_cover(pid):
    """从 anime ID 构造 CDN 封面 URL"""
    return AGE_CDN_COVER % pid


def age_update():
    """解析 /update 页面, 返回 {today:1-7, groups:{1:[],...7:[]}, all:[]}.
    按分区标题 (周一~周日 / 星期X / 日式「X曜日」) 分组;
    若页面结构变化导致识别不到条目, 用外部放送日程 (B站+AniList) 兜底。"""
    key = "age:update:v3"
    hit = cache_get(key)
    if hit:
        return hit
    try:
        base = age_base()
        html = http_get_text(base + "/update", referer=base + "/", timeout=15)
    except Exception as e:
        print("[update] 更新页抓取失败: %r" % (e,), flush=True)
        base, html = "", ""

    # 周几标记 (用于定位每个分组的起点)
    # AGE 更新页分组标题几种写法:
    #   <h2>周一</h2> / <div>星期一</div> / <button>今天 (水曜日)</button>
    #   或 data-wday / data-day 属性
    markers = list(re.finditer(
        r'(?:>(\s*(?:周[一二三四五六日]|星期[一二三四五六日天])\s*)<)'
        r'|(?:([月火水木金土日])曜日)'
        r'|(?:data-wday="(\d)")'
        r'|(?:class="[^"]*week[^"]*"[^>]*data-day="(\d)")',
        html))
    got = {}
    items_with_day = []  # [(item, day)]
    today = time.localtime().tm_wday + 1  # 1=周一...7=周日
    _WD_MAP = {"周一": 1, "周二": 2, "周三": 3, "周四": 4, "周五": 5,
               "周六": 6, "周日": 7,
               "星期一": 1, "星期二": 2, "星期三": 3, "星期四": 4,
               "星期五": 5, "星期六": 6, "星期日": 7, "星期天": 7,
               # 日式曜日: 月=周一 ... 日=周日
               "月": 1, "火": 2, "水": 3, "木": 4, "金": 5, "土": 6, "日": 7}

    if markers:
        # 为每个标记计算其覆盖范围 (到下一个标记或文档末)
        bounds = []
        for i, m in enumerate(markers):
            start = m.end()
            end = markers[i + 1].start() if i + 1 < len(markers) else len(html)
            # 提取 day 数字
            day = None
            for g in m.groups():
                if g:
                    g = g.strip()
                    if g.isdigit():
                        day = int(g)
                    else:
                        day = _WD_MAP.get(g)
                    if day:
                        break
            if day:
                bounds.append((day, start, end))
        # 在每个区间内解析卡片
        for day, s, e in bounds:
            seg = html[s:e]
            for m in re.finditer(r'href="https?://[^"]*?/detail/(\d+)"[^>]*>(.*?)</a>', seg, re.S):
                pid = m.group(1)
                if pid in got: continue
                title = _clean(m.group(2))
                if not title or len(title) < 2 or title.startswith("http"): continue
                chunk = seg[max(0, m.start() - 400): m.end() + 400]
                cover = ""
                for imgm in re.finditer(r'(?:data-original|data-src|src)="([^"]+?\.(?:jpg|jpeg|png|webp)[^"]*)"', chunk):
                    cu = imgm.group(1)
                    if "logo" in cu.lower() or len(cu) < 10: continue
                    cover = unwrap_img(cu, base); break
                epm = re.search(r'video_item--info[^>]*>([^<]+)<', chunk)
                ep = epm.group(1).strip() if epm else ""
                if not ep:
                    em = re.search(r'(第\s*\d+\s*[集话]|全集|剧场版|OVA)', chunk)
                    ep = em.group(1).replace(" ", "") if em else ""
                it = {"id": pid, "title": title, "cover": cover, "ep": ep}
                got[pid] = (it, day)
                items_with_day.append((it, day))

    if not items_with_day:
        # 兜底: 全量解析, 全部归入 today
        for m in re.finditer(r'href="https?://[^"]*?/detail/(\d+)"[^>]*>(.*?)</a>', html, re.S):
            pid = m.group(1)
            if pid in got: continue
            title = _clean(m.group(2))
            if not title or len(title) < 2 or title.startswith("http"): continue
            chunk = html[max(0, m.start() - 400): m.end() + 400]
            cover = ""
            for imgm in re.finditer(r'(?:data-original|data-src|src)="([^"]+?\.(?:jpg|jpeg|png|webp)[^"]*)"', chunk):
                cu = imgm.group(1)
                if "logo" in cu.lower() or len(cu) < 10: continue
                cover = unwrap_img(cu, base); break
            epm = re.search(r'video_item--info[^>]*>([^<]+)<', chunk)
            ep = epm.group(1).strip() if epm else ""
            if not ep:
                em = re.search(r'(第\s*\d+\s*[集话]|全集|剧场版|OVA)', chunk)
                ep = em.group(1).replace(" ", "") if em else ""
            it = {"id": pid, "title": title, "cover": cover, "ep": ep}
            got[pid] = (it, today)
            items_with_day.append((it, today))

    # 构建分组 + 封面补全 (更新页无封面图, 从 ID 构造 CDN URL)
    groups = {str(i): [] for i in range(1, 8)}
    all_items = []
    for it, day in items_with_day:
        if not it.get("cover") and it.get("id"):
            it["cover"] = _age_cover(it["id"])
        groups.setdefault(str(day), []).append(it)
        all_items.append(it)
    if not all_items:
        # AGE 更新页抓不到时可解析条目时, 用外部放送日程兜底
        # (B站番剧时间表 + AniList 日程合并, 条目无 AGE ID 只能跳站内搜索)
        try:
            alt = _week_update_external()
            if alt.get("all"):
                cache_put(key, alt, 1800)
                return alt
        except Exception as e:
            print("[update] 外部日程兜底失败: %r" % (e,), flush=True)
    result = {"today": today, "groups": groups, "all": all_items,
              "weekdays": WEEKDAYS}
    cache_put(key, result, 300)
    return result


def _week_update_external():
    """外部放送日程重建一周更新表 (AGE 更新页已整体下线时的兜底)。

    合并两路可达数据源, 统一按周几分组:
      · B 站番剧时间表   —— 中文标题, 国内直连稳定
      · AniList 放送日程 —— 覆盖广, 用 bangumi-data 反查中文名 (查不到即跳过)
    条目没有 AGE 番剧 ID, id 留空, 用 search 携带标题供前端跳站内搜索;
    跨源按归一化标题去重。
    """
    groups = {str(i): [] for i in range(1, 8)}
    seen = set()
    errors = []

    def _add(wd, title, cover, ep, info):
        nt = _norm_map_title(title)
        if not nt or nt in seen:
            return
        seen.add(nt)
        groups[str(wd)].append({"id": "", "title": title, "cover": cover,
                                "ep": ep, "info": info, "search": title})

    # ---- 1) B 站番剧时间表: 13 天窗口内同一周几会出现两次, 取距今天最近的一次 ----
    try:
        url = "https://api.bilibili.com/pgc/web/timeline?types=1&before=6&after=6"
        data = json.loads(http_get_text(url, referer="https://www.bilibili.com/",
                                        timeout=12))
        if str(data.get("code")) != "0":
            raise RuntimeError("code=%s" % (data.get("code"),))
        now = time.localtime()
        base_ts = time.mktime((now.tm_year, now.tm_mon, now.tm_mday,
                               12, 0, 0, 0, 0, -1))

        def _offset(mmdd):
            m = re.match(r"^(\d{1,2})-(\d{1,2})$", (mmdd or "").strip())
            if not m:
                return None
            mo, dd = int(m.group(1)), int(m.group(2))
            try:
                ts = time.mktime((now.tm_year, mo, dd, 12, 0, 0, 0, 0, -1))
                if ts < base_ts - 180 * 86400:      # 跨年: 12 月看到次年 1 月
                    ts = time.mktime((now.tm_year + 1, mo, dd,
                                      12, 0, 0, 0, 0, -1))
            except (ValueError, OverflowError):
                return None
            return int(round((ts - base_ts) / 86400))

        picked = {}   # 周几 -> (排序键, day)
        for day in data.get("result") or []:
            try:
                wd = int(day.get("day_of_week"))
            except (TypeError, ValueError):
                continue
            if not 1 <= wd <= 7:
                continue
            off = _offset(day.get("date"))
            if off is None:
                key = 999
            elif off >= 0:                          # 今天及未来优先
                key = off
            else:
                key = 100 + (-off)                  # 已经过去的排后面
            prev = picked.get(wd)
            if prev is None or key < prev[0]:
                picked[wd] = (key, day)
        for wd, (_, day) in picked.items():
            for ep in day.get("episodes") or []:
                title = (ep.get("title") or "").strip()
                if not title:
                    continue
                cover = (ep.get("cover") or "").strip()
                if cover.startswith("//"):
                    cover = "https:" + cover
                idx = (ep.get("pub_index") or "").strip()
                pub = (ep.get("pub_time") or "").strip()
                _add(wd, title, cover, idx,
                     " · ".join(x for x in (pub, idx) if x))
    except Exception as e:
        errors.append("B站:%r" % (e,))

    # ---- 2) AniList 放送日程: 未来 7 天, 只收能反查到中文名的条目 ----
    try:
        try:
            rev = _bgdata_reverse_map()
        except Exception:
            rev = {}
        q = ("query($a:Int,$b:Int,$p:Int){Page(page:$p,perPage:50){"
             "airingSchedules(airingAt_greater:$a,airingAt_lesser:$b,sort:TIME){"
             "airingAt episode media{id title{native romaji english}"
             " coverImage{large}}}}}")
        now_ts = int(time.time())
        got = 0
        for page in (1, 2, 3):
            res = _anilist_graphql(q, {"a": now_ts, "b": now_ts + 7 * 86400,
                                       "p": page})
            sch = ((res.get("data") or {}).get("Page") or {}).get(
                "airingSchedules") or []
            if not sch:
                break
            got += len(sch)
            for s in sch:
                ts = int(s.get("airingAt") or 0)
                if not ts:
                    continue
                media = s.get("media") or {}
                mt = media.get("title") or {}
                zh = ""
                for c in (mt.get("native") or "", mt.get("romaji") or "",
                          mt.get("english") or ""):
                    c = _clean(c)
                    v = rev.get(_norm_map_title(c)) if c else ""
                    # 只要真正的中文名: 必须含汉字且不含假名
                    # (bangumi-data 里有日文/英文原样回填的条目)
                    if v and re.search(r"[\u4e00-\u9fff]", v) \
                            and not re.search(r"[\u3040-\u309f\u30a0-\u30ff]", v):
                        zh = v
                        break
                if not zh:
                    continue        # 站内搜索只认中文标题, 无中文名的跳过
                n = s.get("episode")
                ep = ("第%s话" % n) if n else ""
                info = " ".join(x for x in (
                    time.strftime("%H:%M", time.localtime(ts)), ep) if x)
                _add(time.localtime(ts).tm_wday + 1, zh,
                     (media.get("coverImage") or {}).get("large") or "",
                     ep, info)
        if not got:
            raise RuntimeError("日程为空")
    except Exception as e:
        errors.append("AniList:%r" % (e,))

    all_items = [r for i in range(1, 8) for r in groups[str(i)]]
    if not all_items:
        raise RuntimeError("; ".join(errors) or "外部日程均不可用")
    return {"today": time.localtime().tm_wday + 1, "groups": groups,
            "all": all_items, "weekdays": WEEKDAYS}


def _norm_genre(genre):
    """归一化分类参数: 中文标签名原样返回, 其余(空/数字)视为全部。"""
    g = str(genre or "").strip()
    return g if g in AGE_TAGS else ""


def age_catalog(page=1, genre=0, order="time"):
    """解析 /catalog 分类目录列表。
    AGE 真实 URL 格式:
      /catalog/{版本}-{年份}-{首字母}-{类型名}-{资源}-{排序}-{页码}
    类型名为中文标签 (如 热血), 排序: time=最新 / 点击量=热门。
    返回 items + genres + 快捷维度 (years/regions/status/orders)。
    """
    page = max(1, int(page))
    tag = _norm_genre(genre)
    order = str(order or "time")
    # 前端 order 值映射到站点真实排序值
    order_map = {"time": "time", "hot": "点击量", "click": "点击量", "new": "time"}
    real_order = order_map.get(order, "time")
    key = f"age:catalog:{tag}:{real_order}:{page}"
    hit = cache_get(key)
    if hit:
        return hit
    # 负缓存: 上游刚失败过则短时间内直接报错, 避免每次点击都重复撞上游
    err_hit = cache_get(key + ":err")
    if err_hit:
        raise RuntimeError(err_hit)
    path = "/catalog/all-all-all-%s-all-%s-%d" % (
        up.quote(tag or "all"), up.quote(real_order), page)
    try:
        base, html = _age_fetch(path, timeout=12)
    except Exception as e:
        cache_put(key + ":err", str(e), 15)
        raise
    items = _parse_video_cards(html, base, limit=60)
    if not items:
        items = []
    result = {"items": items, "page": page, "has_more": len(items) >= 24,
              "genre": tag, "genres": AGE_GENRES,
              "years": AGE_YEARS, "regions": AGE_REGIONS,
              "status": AGE_STATUS, "orders": AGE_ORDERS}
    cache_put(key, result, 300)
    return result

def age_detail(pid):
    key = "age:detail:" + pid
    hit = cache_get(key)
    if hit:
        return hit
    base = age_base()
    html = http_get_text("%s/detail/%s" % (base, pid), referer=base + "/")
    text = re.sub(r"<script.*?</script>", "", html, flags=re.S)

    title = ""
    tm = re.search(r"<title>\s*(.*?)\s*(?:-\s*AGE动漫)?\s*</title>", text, re.S)
    if tm:
        title = _clean(tm.group(1))
    if not title:
        tm = re.search(r"<h1[^>]*>(.*?)</h1>", text, re.S)
        if tm:
            title = _clean(tm.group(1))

    cover = ""
    # 优先取封面容器内的图, 排除头像/装饰图
    vm = re.search(r'class="video_cover.*?(?=<div class="video_detail|card-body)', text, re.S)
    scan = vm.group(0) if vm else text
    for cm in re.finditer(r'(?:data-original|data-src|src)="([^"]+?\.(?:jpg|jpeg|png|webp)[^"]*)"', scan):
        cu = cm.group(1)
        if ("logo" in cu.lower() or "icon" in cu.lower() or "avatar" in cu.lower()
                or "statics/images" in cu):
            continue
        cover = unwrap_img(cu, base)
        break
    if not cover:
        for cm in re.finditer(r'(?:data-original|data-src|src)="([^"]+?\.(?:jpg|jpeg|png|webp)[^"]*)"', text):
            cu = cm.group(1)
            if ("logo" in cu.lower() or "icon" in cu.lower() or "avatar" in cu.lower()
                    or "statics/images" in cu):
                continue
            cover = unwrap_img(cu, base)
            break
    if not cover:
        cover = _age_cover(pid)

    # 解析 video_detail_info 字段 (detail_imform_tag/value)
    info = {}
    for m in re.finditer(r'<span class="detail_imform_tag">\s*([^<]+?)\s*</span>\s*'
                         r'<span class="detail_imform_value">(.*?)</span>', text, re.S):
        k = _clean(m.group(1)).rstrip("：:").strip()
        v = _clean(m.group(2))
        if k:
            info[k] = v
    if not info:
        for m in re.finditer(r'<div class="video_detail_info[^"]*">\s*<span>([^<]+)</span>(.*?)</div>',
                             text, re.S):
            k = _clean(m.group(1)).rstrip("：:").strip()
            v = _clean(m.group(2))
            if k:
                info[k] = v
    region = info.get("地区", "")
    year = (re.search(r"(\d{4})", info.get("首播时间", "")) or [None, ""])[1] if info.get("首播时间") else ""
    status = info.get("播放状态", "")
    types = info.get("剧情类型", "") or info.get("标签", "")
    intro = info.get("简介", "")
    if not intro:
        im = re.search(r"(?:剧情简介|简介)[：:]?\s*(.{30,500}?)(?:收起|展开|在线播放|$)",
                       _clean(text))
        intro = im.group(1).strip() if im else ""

    # 解析线路与剧集: 按钮 data-bs-target="#playlist-source-{key}" + tab-pane 块
    seg = html
    sm = re.search(r"在线播放(.*?)(?:相关推荐|评论|<footer|</html)", html, re.S)
    if sm:
        seg = sm.group(1)
    line_names = {}
    for m in re.finditer(r'data-bs-target="#playlist-source-([\w-]+)"[^>]*>(.*?)</button>',
                         seg, re.S):
        nm = _clean(m.group(2))
        nm = re.sub(r"^VIP\s*", "", nm).strip() or "线路"
        line_names[m.group(1)] = nm
    parts = re.split(r'<div class="tab-pane[^"]*"\s*id="playlist-source-([\w-]+)"', seg)
    lines = {}
    for i in range(1, len(parts) - 1, 2):
        key_name, block = parts[i], parts[i + 1]
        eps = _extract_episodes(block, base)
        if not eps:
            continue
        num = eps[0]["line"]
        seen_eps, uniq = set(), []
        for e in eps:
            if e["name"] in seen_eps:
                continue
            seen_eps.add(e["name"])
            uniq.append({"ep": e["ep"], "name": e["name"], "pid": e["pid"]})
        lines[num] = {"line": num, "name": line_names.get(key_name, "线路%d" % num),
                      "episodes": uniq}
    line_list = [lines[k] for k in sorted(lines)]

    if not title:
        raise RuntimeError("详情解析失败")
    data = {"id": pid, "title": title, "cover": cover, "region": region,
            "year": year, "status": status, "types": types, "intro": intro,
            "lines": line_list}
    cache_put(key, data, 600)
    return data


def _resolve_age_line(pid, line, ep):
    """解析指定线路某一集的真实视频地址, 失败抛异常"""
    base = age_base()
    play_url = "%s/play/%s/%s/%s" % (base, pid, line, ep)
    html = http_get_text(play_url, referer=base + "/")
    url = ""
    m = re.search(r'player_aaaa\s*=\s*(\{.*?\})\s*[;<]', html, re.S)
    if m:
        try:
            obj = json.loads(m.group(1))
            u = obj.get("url", "")
            if "$" in u:
                u = u.split("$")[0]
            if u and u.startswith("http") and ".m3u8" in u:
                url = u
        except Exception:
            pass
    if not url:
        m = re.search(r'(https?://[^"\'\s<>]+?\.m3u8[^"\'\s<>]*)', html)
        if m:
            url = m.group(1)
    # 播放页经解析器 iframe 中转, 进入解析页提取真实 m3u8
    if not url:
        for im in re.finditer(r'<iframe[^>]+src="([^"]+)"', html):
            ifu = im.group(1)
            if "jx." not in ifu and "vip" not in ifu and "m3u8" not in ifu:
                continue
            try:
                jx = http_get_text(ifu, referer=play_url, timeout=15)
                jm = re.search(r'(https?://[^"\'\s<>]+?\.m3u8[^"\'\s<>]*)', jx)
                if jm:
                    url = jm.group(1)
                    break
            except Exception:
                continue
    if not url:
        raise RuntimeError("线路%s解析失败" % line)
    # page: 播放页地址, 客户端拿去做无头 WebView 截流兜底
    return {"url": url, "via": "proxy", "line": int(line), "page": play_url}


def age_resolve_play(pid, line, ep):
    # 外部聚合片源 (x 线路): ep 即视频直链, 直接返回
    if str(line).startswith(_EXT_LINE_PREFIX):
        return ext_resolve_play(line, ep)
    key = "age:play:%s:%s:%s" % (pid, line, ep)
    hit = cache_get(key)
    if hit:
        return hit
    try:
        data = _resolve_age_line(pid, line, ep)
    except Exception:
        # 部分线路走加密解析器(/vip/ 的 Data 为 AES 密文), 无法服务端还原,
        # 自动回退到其它线路解析同一集
        try:
            # 外源版里 age_detail 已被渠道实现替换, 回退时必须用原始 AGE 详情
            detail = (_AGE_DETAIL if NO_AGE else age_detail)(pid)
            others = [str(l["line"]) for l in detail.get("lines", [])]
        except Exception:
            others = []
        order = [o for o in others if str(o) != str(line)] + [str(line)]
        data = None
        last_err = None
        for alt in order:
            altkey = "age:play:%s:%s:%s" % (pid, alt, ep)
            hit = cache_get(altkey)
            if hit:
                data = hit
                break
            try:
                data = _resolve_age_line(pid, alt, ep)
                cache_put(altkey, data, 1800)
                break
            except Exception as e:
                last_err = e
        if not data:
            raise last_err or RuntimeError("视频地址解析失败, 可尝试切换线路")
    cache_put(key, data, 1800)
    return data


# ----------------------------------------------------------------------------
# 外源版: AGE 主站关闭, 用外部片源渠道实现同名接口 (上游导入方无感知)
# ----------------------------------------------------------------------------
from ..config import NO_AGE

if NO_AGE:
    from . import vodch as _vodch
    # 保留 AGE 原始实现: 外源版把 AGE 作为「外部片源」渠道 (type=age) 时仍会调用它们
    _AGE_HOME = age_home
    _AGE_SEARCH = age_search
    _AGE_CATALOG = age_catalog
    _AGE_UPDATE = age_update
    _AGE_DETAIL = age_detail
    _AGE_PLAY = age_resolve_play
    age_home = _vodch.ch_home
    age_search = _vodch.ch_search
    age_catalog = _vodch.ch_catalog
    age_update = _vodch.ch_update
    age_detail = _vodch.ch_detail
    age_resolve_play = _vodch.ch_resolve_play
    AGE_GENRES = [(t, t) for t in AGE_TAGS]   # AGE 渠道的分类标签保留可用

