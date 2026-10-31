# -*- coding: utf-8 -*-
"""媒体: ffmpeg 定位/编码探测/下载任务(HLS->mp4, 兼容转码)"""
import collections
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from urllib import parse as up

from .config import ROOT, UA
from .net import _safe_url, http_get, http_get_text
from .proxy import _ad_drop_urls, _m3u8_filter_text, _strip_ad_blocks
from .sources.age import age_resolve_play


# ----------------------------------------------------------------------------
# 在线下载 (保存到 D:\超越极限下载; ffmpeg 优先转封装 mp4, 否则纯 Python 抓 ts)
# ----------------------------------------------------------------------------
DL_DIR = Path(r"D:\超越极限下载")
_DL_DIR_CACHE = []


def dl_dir():
    """下载输出目录: 优先 D:\超越极限下载, 不可写则退回应用目录下的 downloads"""
    if _DL_DIR_CACHE:
        return _DL_DIR_CACHE[0]
    for cand in (DL_DIR, ROOT / "downloads"):
        try:
            cand.mkdir(parents=True, exist_ok=True)
            probe = cand / ".w_test"
            probe.write_bytes(b"1")
            probe.unlink()
            _DL_DIR_CACHE.append(cand)
            return cand
        except Exception:
            continue
    _DL_DIR_CACHE.append(DL_DIR)
    return DL_DIR


_dl_jobs = {}
_dl_lock = threading.Lock()
_dl_seq = [0]
_dl_sem = threading.Semaphore(2)  # 最多 2 个并发下载


_FFMPEG_CACHE = []   # 进程内缓存探测结果 (ffmpeg 存在性静态, 下载页 2s 轮询不必反复扫 PATH)


def _ffmpeg():
    if _FFMPEG_CACHE:
        return _FFMPEG_CACHE[0]
    exe = shutil.which("ffmpeg")
    if exe:
        _FFMPEG_CACHE.append(exe)
        return exe
    bundled = ROOT / "runtime" / "ffmpeg.exe"
    r = str(bundled) if bundled.exists() else None
    _FFMPEG_CACHE.append(r)
    return r


# 浏览器/Edge 原生可解的组合; 其余在下载时转码成 H.264 + AAC
_BROWSER_VIDEO = ("h264", "vp8", "vp9", "av1")
_BROWSER_AUDIO = ("aac", "mp3", "opus", "vorbis", "flac")


def _probe_codecs(src, headers="", local_pl=False, timeout=30):
    """用 ffmpeg 探测首个视频/音频编码 (HLS 也能探), 失败返回 (\"\", \"\")"""
    exe = _ffmpeg()
    if not exe:
        return "", ""
    cmd = [exe, "-hide_banner", "-loglevel", "info", "-tls_verify", "0"]
    if headers:
        cmd += ["-headers", headers]
    if local_pl:
        cmd += ["-protocol_whitelist", "file,http,https,tcp,tls,crypto"]
    cmd += ["-i", src, "-t", "1", "-f", "null", "-"]
    flags = 0x08000000 if os.name == "nt" else 0
    try:
        p = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           stdin=subprocess.DEVNULL, timeout=timeout, creationflags=flags)
        err = p.stderr.decode("utf-8", "ignore")
    except Exception:
        return "", ""
    mv = re.search(r"Stream #\d+:\d+.*?: Video:\s*([A-Za-z0-9_]+)", err)
    ma = re.search(r"Stream #\d+:\d+.*?: Audio:\s*([A-Za-z0-9_]+)", err)
    return (mv.group(1).lower() if mv else ""), (ma.group(1).lower() if ma else "")


def _referer_of(u):
    try:
        p = up.urlparse(u)
        return p.scheme + "://" + p.netloc + "/"
    except Exception:
        return "https://www.agefans.com/"


def _safe_name(s):
    return re.sub(r'[\\/:*?"<>|\r\n\t]', "_", s or "").strip()[:80] or "video"


def _m3u8_total(text):
    try:
        return sum(float(x) for x in re.findall(r"#EXTINF:([\d.]+)", text))
    except Exception:
        return 0.0


def dl_start(pid, line, ep, title, epname):
    data = age_resolve_play(pid, line, ep)  # 自带多线路回退
    url = (data or {}).get("url") or ""
    if not url:
        raise RuntimeError("未能解析出视频地址")
    with _dl_lock:
        _dl_seq[0] += 1
        jid = "%d_%d" % (int(time.time()), _dl_seq[0])
        ext = ".mp4" if _ffmpeg() else ".ts"
        job = {
            "id": jid, "anime_id": pid,
            "title": title or ("番剧" + pid),
            "ep": epname or ("第%s集" % ep),
            "status": "waiting", "progress": 0.0,
            "done": 0, "total": 0, "size": 0,
            "file": "", "error": "", "cancel": False,
            "url": url,
            "out": str(dl_dir() / (_safe_name(title or pid) + "-" + _safe_name(epname or ep) + ext)),
        }
        _dl_jobs[jid] = job
    threading.Thread(target=_dl_worker, args=(job,), daemon=True).start()
    return {"id": jid, "title": job["title"], "ep": job["ep"], "status": "waiting"}


def _dl_finish(job, status, err=""):
    with _dl_lock:
        job["status"] = status
        if err:
            job["error"] = err


def _dl_worker(job):
    with _dl_sem:
        if job["cancel"]:
            return
        try:
            dl_dir().mkdir(parents=True, exist_ok=True)
            exe = _ffmpeg()
            if exe:
                _dl_with_ffmpeg(job, exe)
            else:
                _dl_with_py(job)
        except Exception as e:
            try:
                Path(job["out"]).unlink(missing_ok=True)
            except Exception:
                pass
            _dl_finish(job, "error", str(e))


def _dl_with_ffmpeg(job, exe):
    out = job["out"]
    if not out.lower().endswith(".mp4"):
        out += ".mp4"
        job["out"] = out
    # 从 m3u8 统计总时长, 供进度估算; 同时检测广告分片, 命中则改用剔除后的本地清单
    total = 0.0
    src, tmp_pl = _safe_url(job["url"]), None
    try:
        raw_pl = http_get_text(
            job["url"], referer=_referer_of(job["url"]), timeout=15)
        pl_url = job["url"]
        depth = 0
        while "#EXT-X-STREAM-INF" in raw_pl and depth < 3:
            sub = None                       # 主清单: 下钻到首个码率子清单才能拿到分片时长
            for ln in raw_pl.splitlines():
                ln = ln.strip()
                if ln and not ln.startswith("#"):
                    sub = up.urljoin(pl_url, ln)
                    break
            if not sub:
                break
            pl_url = sub
            raw_pl = http_get_text(sub, referer=_referer_of(job["url"]), timeout=15)
            depth += 1
        stripped = _strip_ad_blocks(raw_pl, pl_url)   # 下载同样剔除贴片广告块
        drops = _ad_drop_urls(stripped, pl_url)
        if drops or stripped != raw_pl:
            tmp_pl = str(ROOT / "runtime" / ("dlpl_%s.m3u8" % job["id"]))
            raw_pl = _m3u8_filter_text(stripped, pl_url, drops)
            Path(tmp_pl).write_text(raw_pl, encoding="utf-8")
            src = tmp_pl
        total = _m3u8_total(raw_pl)
        job["total"] = total
    except Exception:
        pass
    job["status"] = "downloading"
    headers = "Referer: %s\r\nUser-Agent: %s\r\n" % (_referer_of(job["url"]), UA)
    vcodec, acodec = _probe_codecs(src, headers, tmp_pl is not None)
    need_tc = ((vcodec and vcodec not in _BROWSER_VIDEO)
               or (acodec and acodec not in _BROWSER_AUDIO))
    if need_tc:
        print("[dl] 源编码 %s/%s 不兼容, 转码 H.264/AAC" % (vcodec or "?", acodec or "?"), flush=True)
    cmd = [exe, "-y", "-hide_banner", "-loglevel", "error", "-tls_verify", "0",
           "-headers", headers]
    if tmp_pl:   # 本地清单内嵌 http 分片地址, 需显式放行协议
        cmd += ["-protocol_whitelist", "file,http,https,tcp,tls,crypto"]
    if need_tc:
        cmd += ["-i", src,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k",
                "-progress", "pipe:1", "-nostats", out]
    else:
        cmd += ["-i", src,
                "-c", "copy", "-bsf:a", "aac_adtstoasc",
                "-progress", "pipe:1", "-nostats", out]
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, creationflags=flags)
    errbuf = collections.deque(maxlen=10)        # 保留 ffmpeg 最后几行报错, 失败时回传给前端

    def _drain_err():
        try:
            for raw in iter(proc.stderr.readline, b""):
                s = raw.decode("utf-8", "ignore").strip()
                if s:
                    errbuf.append(s)
        except Exception:
            pass

    threading.Thread(target=_drain_err, daemon=True).start()
    with _dl_lock:
        job["proc"] = proc
    for raw in iter(proc.stdout.readline, b""):
        if job["cancel"]:
            proc.terminate()
            if tmp_pl:
                Path(tmp_pl).unlink(missing_ok=True)
            return _dl_finish(job, "cancel")
        line = raw.decode("ascii", "ignore").strip()
        if line.startswith("out_time_ms=") and total > 0:
            try:
                secs = float(line.split("=", 1)[1]) / 1e6
                job["progress"] = min(99.0, round(secs / total * 100, 1))
            except Exception:
                pass
    code = proc.wait()
    if code and code > 2 ** 31:
        code -= 2 ** 32                             # Windows 32 位无符号退出码转成有符号 errno
    if tmp_pl:
        Path(tmp_pl).unlink(missing_ok=True)
    with _dl_lock:
        job.pop("proc", None)
    if job["cancel"]:
        return _dl_finish(job, "cancel")
    p = Path(out)
    if code == 0 and p.exists() and p.stat().st_size > 1024:
        job["progress"] = 100.0
        job["size"] = p.stat().st_size
        job["file"] = out
        _dl_finish(job, "done")
    else:
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass
        _dl_finish(job, "error", "ffmpeg 转封装失败(code=%s)%s" % (
            code, (": " + " | ".join(errbuf)[-300:]) if errbuf else ", 请重试或换个线路"))


def _dl_with_py(job):
    """无 ffmpeg 时的纯标准库分段抓取 (仅支持未加密 HLS, 输出 .ts)"""
    base = job["url"]
    referer = _referer_of(base)
    text = http_get_text(base, referer=referer, timeout=15)
    if "#EXTM3U" not in text:
        raise RuntimeError("不是有效的 m3u8")
    # 逐层下钻主清单 (分段相对路径始终基于当前清单 URL)
    depth = 0
    while "#EXT-X-STREAM-INF" in text and depth < 3:
        sub = None
        for ln in text.splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                sub = up.urljoin(base, ln)
                break
        if not sub:
            break
        text = http_get_text(sub, referer=referer, timeout=15)
        base = sub
        depth += 1
    if re.search(r"#EXT-X-KEY:METHOD=AES", text):
        raise RuntimeError("该视频流已加密, 需安装 ffmpeg 才能下载 "
                           "(将 ffmpeg.exe 加入 PATH 或放入 runtime 目录)")
    segs = [up.urljoin(base, ln.strip()) for ln in text.splitlines()
            if ln.strip() and not ln.startswith("#")]
    drops = _ad_drop_urls(text, base)      # 剔除广告分片
    if drops:
        segs = [s for s in segs if s not in drops]
    if not segs:
        raise RuntimeError("未找到视频分段")
    job["total"] = len(segs)
    job["status"] = "downloading"
    out = Path(job["out"])
    f = open(out, "wb")

    def _seg_fetch(s):
        """单分段下载, 失败自动重试 2 次"""
        last = None
        for _ in range(3):
            try:
                return http_get(s, referer=referer, timeout=30, max_mb=64)[0]
            except Exception as e:
                last = e
                time.sleep(0.5)
        raise last

    try:
        BATCH = 8  # 并行批量下载, 大幅提速
        with ThreadPoolExecutor(max_workers=6) as pool:
            for start in range(0, len(segs), BATCH):
                if job["cancel"]:
                    f.close()
                    out.unlink(missing_ok=True)
                    return _dl_finish(job, "cancel")
                batch = segs[start:start + BATCH]
                for data in pool.map(_seg_fetch, batch):
                    f.write(data)
                job["done"] = min(start + BATCH, len(segs))
                job["progress"] = round(job["done"] / len(segs) * 100, 1)
                job["size"] = f.tell()
        f.close()
        job["progress"] = 100.0
        job["file"] = str(out)
        _dl_finish(job, "done")
    except Exception as e:
        try:
            f.close()
        except Exception:
            pass
        out.unlink(missing_ok=True)
        _dl_finish(job, "error", str(e))


def dl_cancel(jid):
    with _dl_lock:
        job = _dl_jobs.get(jid)
        if not job:
            raise RuntimeError("任务不存在")
        job["cancel"] = True
        proc = job.get("proc")
    if proc:
        try:
            proc.terminate()
        except Exception:
            pass
    return True


def dl_list():
    files = []
    d = dl_dir()
    if d.exists():
        try:
            fl = [f for f in d.glob("*") if f.is_file()]
            fl.sort(key=lambda x: x.stat().st_mtime, reverse=True)
            for f in fl[:50]:
                files.append({"name": f.name, "size": f.stat().st_size,
                              "mtime": int(f.stat().st_mtime)})
        except Exception:
            pass
    with _dl_lock:
        jobs = [dict(j) for j in reversed(list(_dl_jobs.values()))]
    for j in jobs:
        for k in ("proc", "url", "t0", "out"):
            j.pop(k, None)
    return {"jobs": jobs, "dir": str(d), "files": files,
            "ffmpeg": bool(_ffmpeg())}
