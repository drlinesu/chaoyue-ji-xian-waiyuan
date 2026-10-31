# -*- coding: utf-8 -*-
"""B站弹幕: 番剧搜索 -> 分集 cid -> 分段 protobuf/XML 双路合并"""
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from struct import unpack
from urllib import parse as up
from urllib.request import Request

from .config import UA, _opener
from .textutil import _clean, _BG_SEASON_RE
from .cache import cache_get, cache_put
from .net import http_get, http_get_text
from .sources.meta import _norm_map_title


# ----------------------------------------------------------------------------
# B站弹幕 (按番剧名搜索 B站番剧 -> 匹配集数 cid -> 拉取弹幕 XML)
# 任一环节失败都返回空列表, 前端静默降级为无弹幕, 不影响播放
# ----------------------------------------------------------------------------
_BILI_HEADERS = {"Referer": "https://www.bilibili.com"}
_BILI_CK = [""]          # buvid 访客 Cookie (懒获取, 无 Cookie 的接口请求会被风控)
_BILI_CK_LOCK = threading.Lock()
_DM_RE = re.compile(r'<d p="([^"]+)"[^>]*>(.*?)</d>', re.S)


def _bili_cookie():
    """获取 B站访客 Cookie: 访问首页由服务端下发 b_nut/buvid3 (搜索等接口风控必需)"""
    with _BILI_CK_LOCK:
        if _BILI_CK[0]:
            return _BILI_CK[0]
        try:
            req = Request("https://www.bilibili.com/",
                          headers={"User-Agent": UA, "Accept": "text/html"})
            pairs = []
            with _opener.open(req, timeout=10) as resp:
                for k, v in resp.headers.items():
                    if k.lower() == "set-cookie":
                        p = v.split(";", 1)[0].strip()
                        if p.startswith(("b_nut=", "buvid3=", "buvid4=")):
                            pairs.append(p)
                resp.read(256)
            if pairs:
                _BILI_CK[0] = "; ".join(pairs)
        except Exception:
            pass
        return _BILI_CK[0]


def _bili_json(url, timeout=10, referer="https://www.bilibili.com"):
    # 注意: extra_headers 会覆盖 http_get 的 referer 参数, Referer 必须以这里为准
    h = {"Referer": referer}
    ck = _bili_cookie()
    if ck:
        h["Cookie"] = ck
    try:
        j = json.loads(http_get_text(url, referer=referer,
                                     timeout=timeout, extra_headers=h))
        return j if isinstance(j, dict) else None
    except Exception:
        return None


# 繁 -> 简 常用字映射 (台版/日文汉字标题与 B站简体条目比对时使用)
_ZH_MAP = str.maketrans({
    "畫": "画", "計": "计", "劃": "划", "傳": "传", "戰": "战", "戦": "战", "龍": "龙",
    "竜": "龙", "亂": "乱", "馬": "马", "夢": "梦", "與": "与", "為": "为", "國": "国",
    "學": "学", "會": "会", "來": "来", "時": "时", "間": "间", "門": "门", "問": "问",
    "開": "开", "關": "关", "関": "关", "見": "见", "說": "说", "話": "话", "語": "语",
    "讀": "读", "書": "书", "寫": "写", "筆": "笔", "車": "车", "東": "东", "動": "动",
    "員": "员", "兒": "儿", "嗎": "吗", "媽": "妈", "爺": "爷", "買": "买", "賣": "卖",
    "錢": "钱", "銀": "银", "鐵": "铁", "鋼": "钢", "銅": "铜", "風": "风", "飛": "飞",
    "鳥": "鸟", "魚": "鱼", "獸": "兽", "獣": "兽", "聖": "圣", "劍": "剑", "剣": "剑",
    "騎": "骑", "爭": "争", "軍": "军", "隊": "队", "師": "师", "親": "亲", "愛": "爱",
    "戀": "恋", "惡": "恶", "殺": "杀", "傷": "伤", "醫": "医", "藥": "药", "園": "园",
    "遊": "游", "戲": "戏", "劇": "剧", "場": "场", "館": "馆", "艦": "舰", "機": "机",
    "輪": "轮", "達": "达", "運": "运", "選": "选", "擇": "择", "擊": "击", "闘": "斗",
    "鬥": "斗", "術": "术", "異": "异", "變": "变", "変": "变", "態": "态", "樣": "样",
    "這": "这", "裡": "里", "邊": "边", "還": "还", "將": "将", "從": "从", "對": "对",
    "対": "对", "錯": "错", "過": "过", "進": "进", "實": "实", "実": "实", "現": "现",
    "發": "发", "発": "发", "髮": "发", "覺": "觉", "覚": "觉", "認": "认", "識": "识",
    "記": "记", "憶": "忆", "訊": "讯", "網": "网", "線": "线", "電": "电", "視": "视",
    "聽": "听", "聲": "声", "樂": "乐", "團": "团", "鄉": "乡", "縣": "县", "區": "区",
    "總": "总", "結": "结", "續": "续", "続": "续", "終": "终", "極": "极", "級": "级",
    "別": "别", "輕": "轻", "轉": "转", "転": "转", "辦": "办", "織": "织", "繪": "绘",
    "數": "数", "據": "据", "產": "产", "業": "业", "專": "专", "題": "题", "麼": "么",
    "灣": "湾", "衛": "卫", "護": "护", "覽": "览", "觀": "观", "賞": "赏", "奪": "夺",
    "寶": "宝", "貝": "贝", "貴": "贵", "價": "价", "貨": "货", "費": "费", "資": "资",
    "財": "财", "敗": "败", "勝": "胜", "負": "负", "強": "强", "勁": "劲", "勢": "势",
    "勞": "劳", "務": "务", "職": "职", "標": "标", "準": "准", "確": "确", "練": "练",
    "習": "习", "訓": "训", "導": "导", "領": "领", "頭": "头", "顏": "颜", "願": "愿",
    "類": "类", "體": "体", "鬧": "闹", "鬆": "松", "嚇": "吓", "驚": "惊", "響": "响",
    "靜": "静", "點": "点", "無": "无", "靈": "灵", "滅": "灭", "絕": "绝", "隱": "隐",
    "誰": "谁", "應": "应", "該": "该", "個": "个", "們": "们", "稱": "称", "號": "号",
    "齊": "齐", "斷": "断", "緣": "缘", "綁": "绑", "縛": "缚", "純": "纯", "繞": "绕",
    "繼": "继", "統": "统", "約": "约", "紅": "红", "綠": "绿", "藍": "蓝", "黃": "黄",
    "黒": "黑", "頁": "页", "項": "项", "順": "顺", "須": "须", "顆": "颗", "騰": "腾",
    "驅": "驱", "駕": "驾", "駛": "驶", "驗": "验", "嘆": "叹", "罷": "罢", "氣": "气",
    "気": "气",
})

# 标题尾部常见修饰 (搜索前剥离, 便于回落到主番剧条目)
_TITLE_TAIL_RE = re.compile(
    r"[\s\-·]*(特别篇|特别版|剧场版|總集篇|总集篇|合集|中文配音版?|中配版?|"
    r"國語版?|国语版?|普通话版?|重制版?|重製版?|完全版)$", re.I)


# 词组级繁简差异 (单纯逐字映射会把 "計畫" 变成 "计画", 而大陆惯用 "计划")
_ZH_PHRASES = (
    ("計畫", "计划"), ("计画", "计划"), ("企畫", "企划"), ("企画", "企划"),
    ("策畫", "策划"), ("策画", "策划"), ("規畫", "规划"), ("规画", "规划"),
    ("動畫", "动画"), ("漫畫", "漫画"), ("畫集", "画集"),
)


def _zh_simplify(s):
    s = str(s or "")
    for a, b in _ZH_PHRASES:
        if a in s:
            s = s.replace(a, b)
    return s.translate(_ZH_MAP)


def _title_candidates(title):
    """标题候选: 原名 -> 去空格 -> 去季数 -> 去括号副标题 -> 去尾部修饰 -> 去新/续前缀"""
    t = _clean(title)
    out = []

    def add(s):
        s = str(s or "").strip(" -·:：,，.。~～")
        if s and s not in out:
            out.append(s)

    add(t)
    add(_zh_simplify(t))
    add(re.sub(r"\s+", "", t))
    add(re.sub(r"\s+", "", _zh_simplify(t)))
    season = _BG_SEASON_RE.sub("", t).strip(" -·")
    add(season)
    add(_zh_simplify(season))
    add(re.sub(r"\s+", "", season))
    main = re.sub(r"[（(\[【][^）)\]】]*[）)\]】]", "", season or t).strip(" -·")
    add(main)
    add(_zh_simplify(main))
    tail = _TITLE_TAIL_RE.sub("", main or t).strip(" -·")
    add(tail)
    add(_zh_simplify(tail))
    head = re.sub(r"^[\s]*(新|続|续|真)[\s·\-]*", "", tail or main or t).strip(" -·")
    add(head)
    add(_zh_simplify(head))
    return out


def _core_keywords(title):
    """长标题的核心关键词: 按标点/空格切分, 取 >=4 字的最长前 3 段 (含繁简变体)
    中英混排 (如 "xxxrestart") 时额外取其中的纯汉字片段"""
    parts = re.split(r"[\s:：\-·~～/／|｜、，,。.！!？?]+", _clean(title))
    segs = []
    for p in parts:
        p = re.sub(r"[（(\[【[^）)\]】]*[）)\]】]", "", p).strip(" -·")
        for run in re.findall(r"[\u4e00-\u9fff]{4,}", p):
            segs.append(run)
        if len(p) >= 4:
            segs.append(p)
    segs.sort(key=len, reverse=True)
    out = []
    for p in segs[:3]:
        for v in (p, _zh_simplify(p)):
            if v and v not in out:
                out.append(v)
    return out


def _bili_search_one(kw, target):
    """单次搜索, 返回 (season_id, 匹配分)"""
    j = _bili_json("https://api.bilibili.com/x/web-interface/search/type?"
                   "search_type=media_bangumi&keyword=" + up.quote(kw),
                   referer="https://search.bilibili.com")   # 搜索接口校验此 Referer
    items = (((j or {}).get("data") or {}).get("result") or [])
    if isinstance(items, dict):
        items = items.get("data") or []
    best, best_sc = None, 0
    for it in items:
        name = re.sub(r"<[^>]+>", "", str(it.get("title") or ""))
        nt = _norm_map_title(_zh_simplify(name))
        if not nt:
            continue
        sc = 0
        if nt == target:
            sc = 4
        elif target in nt or nt in target:
            sc = 3
        else:
            t0, n0 = _BG_SEASON_RE.sub("", target), _BG_SEASON_RE.sub("", nt)
            if t0 and n0 and (t0 == n0 or t0 in n0 or n0 in t0):
                sc = 2
        if sc > best_sc:
            best, best_sc = it.get("season_id"), sc
    return (str(best) if best else None), best_sc


def _bili_search_season(title):
    """按标题搜索 B站番剧, 返回最匹配的 season_id
    先按标题变体逐个试; 全部落空才用核心关键词兜底 (繁简已归一)"""
    target = _norm_map_title(_zh_simplify(title))
    if not target:
        return None
    tried = set()
    for kw in _title_candidates(title):
        k = _norm_map_title(_zh_simplify(kw))
        if not k or k in tried:
            continue
        tried.add(k)
        sid, sc = _bili_search_one(kw, target)
        if sid and sc >= 2:
            return sid
    for kw in _core_keywords(title):
        k = _norm_map_title(_zh_simplify(kw))
        if not k or k in tried:
            continue
        tried.add(k)
        sid, sc = _bili_search_one(kw, target)
        if sid and sc >= 2:
            return sid
    return None


def _bili_episode_cid(season_id, ep):
    """取番剧第 ep 集的弹幕 cid (按分集数字标题匹配, 跳过预告/PV)"""
    j = _bili_json("https://api.bilibili.com/pgc/view/web/season?season_id=" + str(season_id))
    eps = (((j or {}).get("result") or {}).get("episodes") or [])
    cand = []
    for e in eps:
        badge = str(e.get("badge") or "")
        if "预告" in badge or "PV" in badge.upper():
            continue
        t, cid = str(e.get("title") or "").strip(), e.get("cid")
        if cid:
            cand.append((t, cid))
    if not cand:
        return None
    want = str(ep).strip()
    for t, cid in cand:
        if t == want:
            return cid
    nums = [(t, cid) for t, cid in cand if re.fullmatch(r"\d+(\.\d+)?", t)]
    try:
        n = int(float(want))
        if 1 <= n <= len(nums):
            return nums[n - 1][1]
        if 1 <= n <= len(cand):
            return cand[n - 1][1]
    except Exception:
        pass
    return None


# ---- 分片弹幕接口 protobuf 解析 (标准库手写, 无第三方依赖) ----
def _pb_varint(buf, i):
    """读一个 varint, 返回 (值, 下一字节位置); 数据截断返回 (None, i)"""
    v, shift, n = 0, 0, len(buf)
    while i < n:
        c = buf[i]
        i += 1
        v |= (c & 0x7F) << shift
        if not (c & 0x80):
            return v, i
        shift += 7
        if shift > 63:
            break
    return None, i


def _pb_fields(buf):
    """遍历 protobuf 消息, 产出 (字段号, wire_type, 值)
    wire_type 0 -> int; 2 -> bytes; 5 -> 4 字节; 1 -> 8 字节"""
    i, n = 0, len(buf)
    while i < n:
        key, i = _pb_varint(buf, i)
        if key is None:
            return
        fn, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _pb_varint(buf, i)
            if v is None:
                return
            yield fn, wt, v
        elif wt == 2:
            ln, i = _pb_varint(buf, i)
            if ln is None or i + ln > n:
                return
            yield fn, wt, buf[i:i + ln]
            i += ln
        elif wt == 5:
            if i + 4 > n:
                return
            yield fn, wt, buf[i:i + 4]
            i += 4
        elif wt == 1:
            if i + 8 > n:
                return
            yield fn, wt, buf[i:i + 8]
            i += 8
        else:
            return


def _pb_dm_elem(buf):
    """解析单条 DanmakuElem -> {t,m,s,c,x}; 字段缺失/异常返回 None"""
    try:
        p = {}
        for fn, wt, val in _pb_fields(buf):
            if wt in (0, 2):
                p[fn] = val
        text = p.get(7)
        if not isinstance(text, bytes):
            return None
        text = text.decode("utf-8", "replace").strip()
        if not text or len(text) > 120:
            return None
        return {"t": round(float(p.get(2, 0)) / 1000.0, 1),
                "m": int(p.get(3, 1)), "s": int(p.get(4, 25)),
                "c": int(p.get(5, 16777215)), "x": text}
    except Exception:
        return None


def _bili_dm_seg(cid, max_seg=30, cap=20000):
    """分片弹幕接口 (seg.so): 单集全量弹幕, 突破旧 XML 的 3600 条上限
    每片 6 分钟, 越界分片返回空 -> 立即停止; 失败时返回已拿到的部分"""
    h = dict(_BILI_HEADERS)
    ck = _bili_cookie()
    if ck:
        h["Cookie"] = ck
    out, seen = [], set()
    for seg in range(1, max_seg + 1):
        url = ("https://api.bilibili.com/x/v2/dm/web/seg.so?type=1&oid=%s&segment_index=%d"
               % (cid, seg))
        try:
            raw, _ct = http_get(url, referer="https://www.bilibili.com", timeout=10,
                                extra_headers=h, max_mb=8, tries=1)
        except Exception:
            break
        if not raw:
            break
        got = 0
        for fn, wt, elem in _pb_fields(raw):
            if fn != 1 or wt != 2:
                continue
            d = _pb_dm_elem(elem)
            if not d:
                continue
            k = (d["t"], d["x"])
            if k in seen:
                continue
            seen.add(k)
            out.append(d)
            got += 1
        if not got:      # 该片为空即已超出视频时长
            break
        if len(out) >= cap:
            break
    out.sort(key=lambda d: d["t"])
    return out


def _bili_dm_all(cid):
    """分片接口 + 旧 XML 双路并行取弹幕: 两库内容互补, 合并去重后按时间排序
    (热门集分片接口可达 8000+ 条, 旧 XML 另有独立存量, 单用任一路都会漏)"""
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        f_seg = pool.submit(_bili_dm_seg, cid)
        f_xml = pool.submit(_bili_danmaku, cid)
        seg, xml = f_seg.result(), f_xml.result()
    finally:
        pool.shutdown(wait=False)
    out, seen = [], set()
    for d in (seg or []) + (xml or []):
        k = (d["t"], d["x"])
        if k in seen:
            continue
        seen.add(k)
        out.append(d)
    out.sort(key=lambda d: d["t"])
    return out[:20000]


def _bili_danmaku(cid):
    """拉取单集弹幕 XML -> [{t,m,s,c,x}], 按时间升序"""
    try:
        raw, _ct = http_get("https://comment.bilibili.com/%s.xml" % cid,
                            referer="https://www.bilibili.com", timeout=12,
                            extra_headers=_BILI_HEADERS, max_mb=8, tries=2)
    except Exception:
        return []
    # B站弹幕接口常以 raw deflate 返回, http_get 只解 gzip, 这里兜底解压
    if not raw.startswith(b"<"):
        import zlib
        for wb in (zlib.MAX_WBITS, -zlib.MAX_WBITS):
            try:
                raw = zlib.decompress(raw, wb)
                break
            except Exception:
                continue
    xml = raw.decode("utf-8", "replace")
    out = []
    for mt in _DM_RE.finditer(xml):
        p = mt.group(1).split(",")
        if len(p) < 4:
            continue
        try:
            t, mode, size, color = float(p[0]), int(p[1]), int(p[2]), int(p[3])
        except ValueError:
            continue
        text = (mt.group(2).replace("&lt;", "<").replace("&gt;", ">")
                .replace("&quot;", '"').replace("&apos;", "'")
                .replace("&amp;", "&")).strip()
        if not text or len(text) > 120:
            continue
        out.append({"t": round(t, 1), "m": mode, "s": size, "c": color, "x": text})
    out.sort(key=lambda d: d["t"])
    return out


def bili_danmaku(title, ep):
    """弹幕总入口: 结果缓存 2 小时"""
    title = _clean(title)
    if not title:
        return []
    key = "dm2:%s:%s" % (_norm_map_title(title), ep)   # dm2: 双路合并后升版, 旧缓存自然失效
    hit = cache_get(key)
    if hit is not None:
        return hit
    out = []
    sid = _bili_search_season(title)
    cid = None
    used_sid = None
    if sid:
        cid = _bili_episode_cid(sid, ep)
        used_sid = sid
    if not cid:
        # 目标 season 无正片(未开播/预告季) 时, 回退到主番剧(去季数后缀)取弹幕
        stripped = _BG_SEASON_RE.sub("", title).strip(" -·")
        if stripped and stripped != title:
            sid2 = _bili_search_season(stripped)
            if sid2 and sid2 != sid:
                cid = _bili_episode_cid(sid2, ep)
                if cid:
                    used_sid = sid2
    if cid:
        out = _bili_dm_all(cid)          # 分片接口 + 旧 XML 双路合并
        print("[danmaku] %s 第%s集: sid=%s cid=%s 弹幕 %d 条"
              % (title, ep, used_sid, cid, len(out)), flush=True)
    elif sid:
        print("[danmaku] %s 第%s集: sid=%s 未匹配到分集" % (title, ep, sid), flush=True)
    else:
        print("[danmaku] %s: 未匹配到B站番剧" % title, flush=True)
    cache_put(key, out, 7200 if out else 900)   # 空结果只缓存 15 分钟, 便于上游恢复后重试
    return out
