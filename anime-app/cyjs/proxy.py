# -*- coding: utf-8 -*-
"""HLS/图片代理: m3u8 重写、广告块剔除、分片缓存与预取、图片缓存"""
import collections
import hashlib
import io
import os
import re
import threading
import time
from urllib import parse as up

from .config import ROOT
from .net import _safe_url, http_get


# ----------------------------------------------------------------------------
# HLS / 图片 代理
# ----------------------------------------------------------------------------
def proxied(u, kind):
    return "/api/proxy/%s?u=%s" % (kind, up.quote(u, safe=""))


# ---- ts 分段磁盘 LRU 缓存: 回看/重播/切集免重复下载, 大幅减少卡顿 ----
_SEG_DIR = ROOT / "runtime" / "segcache"
_SEG_MAX_BYTES = 1024 * 1024 * 1024   # 1GB 上限, 超出按最久未访问淘汰
_seg_sizes = {}                        # path -> size (内存账本)
_seg_bytes = [0]
_seg_lock = threading.Lock()
_seg_inited = [False]


def _seg_init():
    if _seg_inited[0]:
        return
    with _seg_lock:
        if _seg_inited[0]:
            return
        try:
            _SEG_DIR.mkdir(parents=True, exist_ok=True)
            total = 0
            for f in _SEG_DIR.glob("*.ts"):
                try:
                    sz = f.stat().st_size
                    _seg_sizes[f] = sz
                    total += sz
                except OSError:
                    pass
            _seg_bytes[0] = total
            # 残留 .tmp (上次进程中断) 一律清理
            for f in _SEG_DIR.glob("*.tmp"):
                try:
                    f.unlink()
                except OSError:
                    pass
            # 启动即强制执行容量上限 (上次退出前可能超限)
            if total > _SEG_MAX_BYTES:
                for f in sorted(list(_seg_sizes), key=lambda x: x.stat().st_mtime):
                    if _seg_bytes[0] <= _SEG_MAX_BYTES:
                        break
                    try:
                        s = _seg_sizes.pop(f, 0)
                        f.unlink()
                        _seg_bytes[0] -= s
                    except OSError:
                        pass
        except OSError:
            pass
        _seg_inited[0] = True


def _seg_key(url):
    return hashlib.sha1(_safe_url(url).encode("utf-8")).hexdigest()


def _seg_get(url):
    """命中返回磁盘路径并刷新访问时间, 未命中返回 None"""
    p = _SEG_DIR / (_seg_key(url) + ".ts")
    if not p.is_file():
        return None
    try:
        os.utime(p, None)
        return p
    except OSError:
        return None


def _seg_put(url, tmp_path):
    """分段下载完成后入缓存, 超限淘汰最旧文件"""
    try:
        sz = tmp_path.stat().st_size
        p = _SEG_DIR / (_seg_key(url) + ".ts")
        with _seg_lock:
            old = _seg_sizes.get(p, 0)
            os.replace(str(tmp_path), str(p))
            _seg_sizes[p] = sz
            _seg_bytes[0] += sz - old
        if _seg_bytes[0] > _SEG_MAX_BYTES:
            files = sorted(_seg_sizes.keys(), key=lambda x: x.stat().st_mtime)
            for f in files:
                if _seg_bytes[0] <= _SEG_MAX_BYTES:
                    break
                with _seg_lock:
                    s = _seg_sizes.pop(f, 0)
                try:
                    f.unlink()
                    _seg_bytes[0] -= s
                except OSError:
                    pass
    except OSError:
        pass


M3U8_RE = re.compile(r"\.m3u8(\?|$)", re.I)

# ---- 前方分片预取 (弱网防断粮): 媒体清单服务时登记分片顺序, ts 命中时后台预取前方 N 片 ----
_PL_SEGS = {}        # playlist_url -> [seg_url, ...] (仅媒体清单)
_PL_INDEX = {}       # seg_url -> (playlist_url, index)
_PL_LOCK = threading.Lock()
_PL_MAX = 6          # 最多记忆最近 6 个播放列表
_SEG_INFLIGHT = set()
_SEG_PF_LOCK = threading.Lock()
_PREFETCH_AHEAD = 3  # 预取前方 3 片 (~12-30s 内容)


def _pl_register(pl_url, text):
    """登记媒体清单的分片顺序 (主清单无 #EXTINF 自动跳过), 供预取定位前方分片"""
    if "#EXTINF" not in text:
        return
    segs = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        segs.append(line if line.lower().startswith("http") else up.urljoin(pl_url, line))
    if len(segs) < 2:
        return
    with _PL_LOCK:
        _PL_SEGS[pl_url] = segs
        if len(_PL_SEGS) > _PL_MAX:
            for k in list(_PL_SEGS)[:-_PL_MAX]:
                for s in _PL_SEGS.pop(k, []):
                    if _PL_INDEX.get(s, (None,))[0] == k:
                        _PL_INDEX.pop(s, None)
        for i, s in enumerate(segs):
            _PL_INDEX[s] = (pl_url, i)


def _seg_has(url):
    return (_SEG_DIR / (_seg_key(url) + ".ts")).is_file()


def _seg_prefetch(cur_url, referer):
    """按清单顺序预取当前位置前方的未缓存分片, 失败静默 (后台线程, 不阻塞播放)"""
    try:
        with _PL_LOCK:
            hit = _PL_INDEX.get(cur_url)
            if not hit:
                return
            pl, i = hit
            segs = _PL_SEGS.get(pl) or []
            ahead = segs[i + 1: i + 1 + _PREFETCH_AHEAD]
        todo = [s for s in ahead if not _seg_has(s)]
        with _SEG_PF_LOCK:
            todo = [s for s in todo if s not in _SEG_INFLIGHT and len(_SEG_INFLIGHT) < 4]
            _SEG_INFLIGHT.update(todo)
        for s in todo:
            threading.Thread(target=_seg_fetch, args=(s, referer), daemon=True).start()
    except Exception:
        pass


def _seg_fetch(url, referer):
    """后台下载单个分片入 segcache (走 http_get 复用重试/限流), 不向客户端转发"""
    tmp = _SEG_DIR / (_seg_key(url) + ".pf.tmp")
    try:
        if _seg_has(url):
            return
        data, _ = http_get(url, referer=referer, timeout=30)
        if not data:
            return
        _SEG_DIR.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(data)
        _seg_put(url, tmp)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
    finally:
        with _SEG_PF_LOCK:
            _SEG_INFLIGHT.discard(url)


IMG_HOST_REFERER = {
    "copymanga": "https://www.copymanga.tv/",
    "mangacopy": "https://www.mangacopy.com/",
    "agedm.io": None,  # 按请求 host 动态
}


def _ad_drop_urls(text, base_url):
    """检测 m3u8 中被插入的广告分片, 返回需剔除的分片绝对 URL 集合。
    依据: 正片分片集中在主域名, 广告分片(澳门新葡京等)托管在独立域名;
    仅在样本>=6 且主域名占比>=60% 时启用, 不误伤单域名或均衡 CDN 的流。"""
    b = base_url.rsplit("/", 1)[0] + "/"
    segs = []
    for raw in text.splitlines():
        ln = raw.strip()
        if not ln or ln.startswith("#") or M3U8_RE.search(ln):
            continue                        # 跳过标签与子清单, 只统计媒体分片
        if ln.startswith("http"):
            segs.append(ln)
        elif ln.startswith("/"):
            segs.append(up.urljoin(base_url, ln))
        else:
            segs.append(b + ln)
    if len(segs) < 6:
        return set()
    hosts = {}
    for s in segs:
        h = up.urlparse(s).netloc.lower()
        hosts[h] = hosts.get(h, 0) + 1
    if len(hosts) < 2:
        return set()
    top, cnt = max(hosts.items(), key=lambda kv: kv[1])
    if cnt < len(segs) * 0.6:
        return set()                        # 无绝对主域名, 可能为多 CDN 均衡, 不动
    drop = {s for s in segs if up.urlparse(s).netloc.lower() != top}
    if drop:
        print("[ad-skip] 检测并跳过广告分片 %d/%d 段" % (len(drop), len(segs)), flush=True)
    return drop


def _m3u8_filter_text(text, base_url, drops):
    """生成剔除广告分片后的播放列表文本 (分片/密钥 URI 全部绝对化, 供 ffmpeg 读取本地清单)"""
    b = base_url.rsplit("/", 1)[0] + "/"

    def absu(u):
        if u.startswith("http"):
            return u
        if u.startswith("/"):
            return up.urljoin(base_url, u)
        return b + u

    out = []
    for raw in text.splitlines():
        ln = raw.strip()
        if not ln:
            continue
        if ln.startswith("#"):
            out.append(re.sub(r'(URI=")([^"]+)"',
                              lambda m: m.group(1) + absu(m.group(2)) + '"', ln))
            continue
        if absu(ln) in drops:
            if out and out[-1].startswith("#EXTINF"):   # 连同对应时长标签一起剔除
                out.pop()
            continue
        out.append(absu(ln))
    return "\n".join(out) + "\n"


# 广告块剔除: 贴片广告通常被 #EXT-X-DISCONTINUITY 包成独立块,
# 路径/码率档与正片不同、时长 15~30s; 整块删掉后正片 PTS 天然接续 (实测无跳变)。
_AD_BLOCK_MAX = 120.0     # 广告块时长上限
_AD_BLOCK_SHORT = 60.0    # 同路径短块: 需两侧都是长块才判广告
_AD_MARKERS = ("adjump", "/ad/", "/ads/", "advert", "guanggao")
# 源站插入贴片广告时留下的注释标记 (如 '# ppvod-ad-injected v1'):
# 该行之后(通常紧跟 #EXT-X-DISCONTINUITY)的第一个块必为广告, 优先级高于时长/路径启发式
_AD_COMMENT_MARKERS = ("ad-injected", "ad_injected", "ext-x-ad", "ad-insert", "advert", "ppvod-ad")


def _m3u8_dir(u):
    try:
        return up.urlparse(u).path.rsplit("/", 1)[0]
    except Exception:
        return ""


def _drop_ad_comments(text):
    """清除源站广告注入注释行 (如 '# ppvod-ad-injected v1'), 避免残留标记"""
    out = []
    for ln in text.splitlines():
        s = ln.strip()
        if (s.startswith("#") and not s.startswith("#EXT")
                and any(m in s.lower() for m in _AD_COMMENT_MARKERS)):
            continue
        out.append(ln)
    return "\n".join(out) + "\n"


def _strip_ad_blocks(text, base_url):
    """整块剔除 HLS 贴片广告, 返回过滤后的播放列表文本 (无法判定时仅清注释)"""
    cleaned = _drop_ad_comments(text)     # 兜底: 任何分支都不残留广告注入注释
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return cleaned
    preamble, blocks = [], []
    cur = {"lines": [], "segs": [], "dur": 0.0, "disc": False}
    started = False
    pending_ad = False          # 遇到广告注入注释后, 下一个块标记为广告
    for ln in lines:
        if ln.startswith("#") and not ln.startswith("#EXT"):
            # 广告注入注释 (如 '# ppvod-ad-injected v1'): 记录并丢弃该行
            if any(m in ln.lower() for m in _AD_COMMENT_MARKERS):
                pending_ad = True
                continue
        if ln.startswith("#EXT-X-DISCONTINUITY"):
            if started and cur["lines"]:
                blocks.append(cur)
                cur = {"lines": [], "segs": [], "dur": 0.0, "disc": True, "ad": pending_ad}
                pending_ad = False
            else:
                cur["disc"] = True
            continue
        if not started and not ln.startswith("#"):
            started = True
        if not started:
            preamble.append(ln)
            continue
        cur["lines"].append(ln)
        if ln.startswith("#EXTINF:"):
            try:
                cur["dur"] += float(ln.split(":")[1].split(",")[0])
            except Exception:
                pass
        elif not ln.startswith("#"):
            cur["segs"].append(up.urljoin(base_url, ln))
    if started and cur["lines"]:
        blocks.append(cur)
    if len(blocks) < 2:
        return cleaned
    durs = [b["dur"] for b in blocks]
    if max(durs) < 120:
        return cleaned                  # 整条列表都很短, 不做广告判定
    dom = collections.Counter(_m3u8_dir(u) for b in blocks for u in b["segs"]).most_common(1)[0][0]
    ad = [False] * len(blocks)
    for i, b in enumerate(blocks):
        d = b["dur"]
        # 源站广告注入注释明确标记的块: 直接判广告 (即便时长/路径启发式未命中)
        if b.get("ad"):
            ad[i] = True
        if d <= 0 or not b["segs"] or d > _AD_BLOCK_MAX:
            continue
        path = _m3u8_dir(b["segs"][0])
        marked = any(m in path.lower() for m in _AD_MARKERS)
        diff = path != dom
        prev_d = durs[i - 1] if i > 0 else 0.0
        nxt_d = durs[i + 1] if i + 1 < len(durs) else 0.0
        surrounded = d <= _AD_BLOCK_SHORT and prev_d >= d * 3 and nxt_d >= d * 3
        if marked or diff or surrounded:
            ad[i] = True
    if not any(ad) or all(ad):
        return cleaned
    out, removed, removed_dur = list(preamble), 0, 0.0
    prev_kept, prev_ad = False, False
    for i, b in enumerate(blocks):
        if ad[i]:
            removed += 1
            removed_dur += b["dur"]
            prev_ad = True
            continue
        if prev_kept and b["disc"] and not prev_ad:
            out.append("#EXT-X-DISCONTINUITY")
        out.extend(b["lines"])
        prev_kept, prev_ad = True, False
    if removed:
        print("[ad-skip] 剔除广告块 %d 个, 约 %.1f 秒" % (removed, removed_dur), flush=True)
    return "\n".join(out) + "\n"


def rewrite_m3u8(text, base_url):
    text = _strip_ad_blocks(text, base_url)   # 先整块剔除贴片广告
    lines_out = []
    base = base_url.rsplit("/", 1)[0] + "/"
    drops = _ad_drop_urls(text, base_url)   # 主动检测广告分片
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            # 处理 #EXT-X-KEY:URI="..." 与 #EXT-X-MAP:URI="..."
            def repl(m):
                uri = m.group(2)
                if uri.startswith("http") or uri.startswith("/api/"):
                    absu = uri
                elif uri.startswith("/"):
                    absu = up.urljoin(base_url, uri)
                else:
                    absu = base + uri
                return m.group(1) + proxied(absu, "ts") + '"'
            line = re.sub(r'(URI=")([^"]+)"', repl, line)
            lines_out.append(line)
        else:
            if line.startswith("http"):
                absu = line
            elif line.startswith("/"):
                absu = up.urljoin(base_url, line)
            else:
                absu = base + line
            if absu in drops:                       # 广告分片: 连同其 EXTINF 一并剔除
                if lines_out and lines_out[-1].startswith("#EXTINF"):
                    lines_out.pop()
                continue
            lines_out.append(proxied(absu, "hls" if M3U8_RE.search(line) else "ts"))
    return "\n".join(lines_out) + "\n"
