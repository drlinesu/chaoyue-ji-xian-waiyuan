# -*- coding: utf-8 -*-
"""标题清洗 / 季数后缀归一"""
import re


def _clean(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s or "")).strip()


_BG_SEASON_RE = re.compile(
    r"[\s\-·]*(第\s*[0-9一二三四五六七八九十百]+\s*[季部篇]|season\s*\d+|s\d+|part\s*[.·]?\s*\d+|"
    r"[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]|(?:II|III|IV|VI|VII|VIII|IX|XI|XII|[IVX]+\s*(?:季|部)))\s*$", re.I)


def _bgm_title_variants(title):
    t = _clean(title)
    out = [t]
    prev = t
    for _ in range(3):   # 逐级去掉季数后缀, 如 "无职转生 第二季" -> "无职转生"
        nxt = _BG_SEASON_RE.sub("", prev).strip(" -·")
        if not nxt or nxt == prev:
            break
        out.append(nxt)
        prev = nxt
    return out


# ---------------- 聚合搜索: 标题 / 关键词 / 角色名 ----------------

