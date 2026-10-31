# -*- coding: utf-8 -*-
"""版本 / 自更新 / 更新源 Hub / 公网下载隧道"""
import gzip
import hashlib
import hmac
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import parse as up
from urllib.request import Request, build_opener, HTTPSHandler, ProxyHandler

from .config import ROOT, UA
from .net import _safe_url, http_get_text
from .sources.age import age_home, age_update
from .sources.meta import metadata_info, reco_yearly, _anilist_tag_set


# ----------------------------------------------------------------------------
# 版本检查 (更新源固定为官方 GitHub 发布页, 不可配置)
# ----------------------------------------------------------------------------
APP_VERSION = "2.5.2"
# 唯一公开仓库: 其余仓库均已设为私人, 更新检查只认这个地址
GITHUB_REPO = "drlinesu/chaoyue-ji-xian-waiyuan"
GITHUB_BASE = "https://github.com/%s/releases/latest/download" % GITHUB_REPO
# 唯一官方下载渠道: 应用内只提示新版本, 不提供下载/安装, 统一引导到 GitHub 发布页
OFFICIAL_URL = "https://github.com/%s" % GITHUB_REPO
# 源顺序: 国内镜像优先 (GitHub 直连在国内大多超时, 放最后避免每次白等 12s)
DEFAULT_UPDATE_SOURCES = [
    "https://ghproxy.net/" + GITHUB_BASE,
    "https://ghfast.top/" + GITHUB_BASE,
    # 仓库无 Release 时回退读仓库里的 VERSION (jsdelivr 走 GitHub 公共 CDN, 国内可直连)
    "https://cdn.jsdelivr.net/gh/%s@main/anime-app" % GITHUB_REPO,
    GITHUB_BASE,
]
DEFAULT_UPDATE_SOURCE = GITHUB_BASE
_UPD_PROG = {"phase": "idle", "pct": 0, "msg": ""}   # 在线更新进度 (前端轮询展示)


def _read_version(root=ROOT):
    try:
        v = (root / "VERSION").read_text(encoding="utf-8").strip()
        return v.lstrip("\ufeff").strip() or APP_VERSION   # 兼容带 BOM 的 VERSION
    except Exception:
        return APP_VERSION


def _update_sources():
    """版本检查固定使用官方 GitHub 发布页 (忽略本地配置, 防止更新源被恶意篡改)"""
    return list(DEFAULT_UPDATE_SOURCES)


def _update_source():
    """兼容旧调用: 返回首选源"""
    srcs = _update_sources()
    return srcs[0] if srcs else ""


def _upd_set_prog(phase, pct, msg=""):
    _UPD_PROG.update(phase=phase, pct=int(pct), msg=msg)


def _upd_download(url, max_mb=80):
    """分块下载更新包到临时文件, 实时写进度; 返回 (临时路径, 实际字节数)"""
    _upd_set_prog("download", 0, "连接更新源...")
    req = Request(_safe_url(url), headers={
        "User-Agent": UA, "Accept": "*/*", "Connection": "close"})
    tmp = Path(os.environ.get("TEMP", "/tmp")) / "cyjm_update_pkg.zip"
    done, total = 0, 0
    with _opener.open(req, timeout=30) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        if total > max_mb * 1024 * 1024:
            raise RuntimeError("更新包过大 (%.1f MB), 已中止" % (total / 1048576))
        with open(tmp, "wb") as f:
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total:
                    _upd_set_prog("download", min(99, done * 100 // total),
                                  "下载中 %.1f/%.1f MB" % (done / 1048576, total / 1048576))
    if total and done < total:
        raise RuntimeError("下载不完整 (%d/%d 字节), 请重试" % (done, total))
    return tmp, done


def _ver_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3]) or (0,)


def update_check():
    sources = _update_sources()
    cur = _read_version()
    if not sources:
        return {"current": cur, "source": "", "configured": False, "has_update": False,
                "official": OFFICIAL_URL}
    last_err = None
    for src in sources:
        try:
            if src.lower().startswith(("http://", "https://")):
                latest = http_get_text(src.rstrip("/") + "/VERSION", timeout=6).strip()
            else:
                latest = _read_version(Path(src))
        except Exception as e:
            last_err = e
            continue
        return {"current": cur, "latest": latest, "source": src, "configured": True,
                "has_update": _ver_tuple(latest) > _ver_tuple(cur), "official": OFFICIAL_URL}
    return {"current": cur, "source": sources[0], "configured": True,
            "has_update": False, "error": "无法访问更新源: %s" % last_err,
            "official": OFFICIAL_URL}


# 启动静默检查更新的结果 (供前端角标/接口读取, 不打扰用户)
UPD_SILENT = {"has_update": False, "latest": "", "checked": 0.0, "error": ""}


def update_notice():
    """前端轮询的静默检查结果 (后台循环填充, 本接口不主动访问网络)"""
    d = dict(UPD_SILENT)
    d["official"] = OFFICIAL_URL
    return d


def auto_check_loop():
    """后台静默检查更新: 启动后立即查; 成功则每 6 小时一次, 失败 10 分钟后重试
    (原实现启动后先睡 20s 且失败一次要等 6 小时, 开机网络未就绪时这轮提示就丢了)"""
    while True:
        ok = False
        try:
            r = update_check() or {}
            UPD_SILENT.update({
                "has_update": bool(r.get("has_update")),
                "latest": r.get("latest") or "",
                "error": r.get("error") or "",
                "checked": time.time(),
            })
            ok = not r.get("error")
        except Exception as e:
            UPD_SILENT["error"] = str(e)
        time.sleep(6 * 3600 if ok else 600)


# 顶层覆盖文件 (与安装器安装集对齐; runtime/python 运行时与用户数据不在更新范围)
_UPD_TOP_FILES = ("server.py", "app.py", "VERSION", "start.bat", "launcher.ps1")
_UPD_SKIP_DIRS = {"runtime", ".app-data", "__pycache__", "cache", "segcache"}


def _update_apply_from(src):
    """在线更新: 拉取 update.zip -> 校验 -> 原子覆盖程序文件 -> 新进程接管端口自动重启"""
    _upd_set_prog("prepare", 0, "准备更新...")
    if src.lower().startswith(("http://", "https://")):
        zip_path, _size = _upd_download(src.rstrip("/") + "/update.zip")
        _upd_set_prog("verify", 100, "校验更新包完整性...")
        try:
            with zipfile.ZipFile(zip_path) as zf:
                bad = zf.testzip()
                if bad is not None:
                    raise RuntimeError("更新包损坏: %s" % bad)
                if not any(n.split("/")[-1] == "server.py" for n in zf.namelist()):
                    raise RuntimeError("更新包内容不完整 (缺少 server.py)")
        except zipfile.BadZipFile:
            zip_path.unlink(missing_ok=True)
            raise RuntimeError("更新包不是有效的 zip 文件")
        ext_dir = zip_path.parent / "cyjm_update_ext"
        if ext_dir.exists():
            shutil.rmtree(ext_dir, ignore_errors=True)
        ext_dir.mkdir(parents=True)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(ext_dir)
        zip_path.unlink(missing_ok=True)
        src_root = ext_dir
    else:
        src_root = Path(src)
        _upd_set_prog("verify", 100, "校验本地更新源...")
    if (src_root / "anime-app" / "server.py").exists():
        src_root = src_root / "anime-app"
    if not (src_root / "server.py").exists():
        raise RuntimeError("更新源中未找到 server.py, 请检查更新源内容")
    # 收集覆盖清单: 顶层程序文件 + static 全量 (排除运行时/缓存/用户数据目录)
    jobs = []
    for name in _UPD_TOP_FILES:
        f = src_root / name
        if f.is_file():
            jobs.append((f, ROOT / name))
    src_static = src_root / "static"
    if src_static.is_dir():
        for p in sorted(src_static.rglob("*")):
            if p.is_file() and not (_UPD_SKIP_DIRS & set(p.relative_to(src_static).parts)):
                jobs.append((p, ROOT / "static" / p.relative_to(src_static)))
    src_pkg = src_root / "cyjs"          # 模块化后的后端包
    if src_pkg.is_dir():
        for p in sorted(src_pkg.rglob("*")):
            if (p.is_file() and p.suffix != ".pyc"
                    and "__pycache__" not in p.parts):
                jobs.append((p, ROOT / "cyjs" / p.relative_to(src_pkg)))
    if not jobs:
        raise RuntimeError("更新源中没有可安装的文件")
    _upd_set_prog("install", 0, "安装更新文件...")
    skipped, failed = [], []
    for i, (sf, df) in enumerate(jobs, 1):
        try:
            df.parent.mkdir(parents=True, exist_ok=True)
            tmpf = df.with_name(df.name + ".upd-new")   # 原子替换: 失败保留原文件
            shutil.copy2(sf, tmpf)
            os.replace(str(tmpf), str(df))
        except (OSError, PermissionError) as e:
            skipped.append(df.name)
            failed.append("%s: %s" % (df.name, e))
        _upd_set_prog("install", i * 100 // len(jobs), "安装更新文件 %d/%d" % (i, len(jobs)))
    if src.lower().startswith(("http://", "https://")):
        shutil.rmtree(src_root, ignore_errors=True)   # 清理解压临时目录 (本地目录源保留)
    _upd_set_prog("restart", 100, "重启服务...")
    # 新进程以接管模式启动 (main 的 bind 重试窗口足够旧进程退出), 本进程延迟退出
    env = dict(os.environ, CYJ_TAKEOVER="1")
    subprocess.Popen(
        [sys.executable, str(ROOT / "server.py")], cwd=str(ROOT), env=env,
        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    threading.Timer(1.2, lambda: os._exit(0)).start()
    msg = "更新完成, 服务正在重启..."
    if skipped:
        msg += " (%d 个文件被占用未替换: %s)" % (len(skipped), ", ".join(skipped[:5]))
    return {"message": msg, "files": len(jobs), "skipped": skipped}


def update_apply():
    """应用内更新已关闭: 只做版本检查, 下载/安装统一走官方 GitHub 渠道"""
    raise RuntimeError("应用内更新已关闭, 请前往官方 GitHub 渠道下载新版: %s" % OFFICIAL_URL)

# ---------------- 本机更新源 (update hub) ----------------
# 随主服务自动启动 (独立端口): 客户端公开拉取 VERSION/update.zip;
# 发布/状态等管理动作必须凭 admin.key (仅管理者持有, 其他人无法篡改源)
UPD_HUB_PORT = 8967
UPD_HUB_DIR = ROOT / "runtime" / "update_hub"


def _upd_hub_key():
    """更新源管理密钥: 首次启动自动生成 32 位 hex 存 admin.key (不随安装包分发)"""
    try:
        UPD_HUB_DIR.mkdir(parents=True, exist_ok=True)
        kf = UPD_HUB_DIR / "admin.key"
        try:
            k = kf.read_text(encoding="utf-8").strip()
            if k:
                return k
        except OSError:
            pass
        k = os.urandom(16).hex()
        kf.write_text(k, encoding="utf-8")
        print("[update-hub] 管理密钥已生成: %s\n[update-hub] 密钥文件: runtime/update_hub/admin.key (请妥善保管)" % k, flush=True)
        return k
    except Exception as e:
        print("[update-hub] 密钥生成失败: %r" % (e,), flush=True)
        return ""


def _upd_hub_publish(zbytes):
    """管理者发布新版本包: 校验 zip (须含 server.py + VERSION) 后原子落盘生效, 返回包内版本号"""
    UPD_HUB_DIR.mkdir(parents=True, exist_ok=True)
    tmp = UPD_HUB_DIR / "update.zip.incoming"
    tmp.write_bytes(zbytes)
    try:
        with zipfile.ZipFile(tmp) as zf:
            names = zf.namelist()
            if not any(n.split("/")[-1] == "server.py" for n in names):
                raise RuntimeError("包内缺少 server.py, 拒绝发布")
            ver = ""
            for cand in ("VERSION", "anime-app/VERSION"):
                if cand in names:
                    ver = zf.read(cand).decode("utf-8", "ignore").strip()
                    break
            if not ver:
                raise RuntimeError("包内缺少 VERSION, 拒绝发布")
        os.replace(str(tmp), str(UPD_HUB_DIR / "update.zip"))
        (UPD_HUB_DIR / "VERSION").write_text(ver, encoding="utf-8")
        return ver
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _upd_hub_serve():
    """更新源 HTTP 服务 (daemon 线程, 端口 8966): 拉取接口公开 (只读无破坏性),
    管理接口 (/upload /status) 必须 key 匹配 (constant-time 比较), 挂掉不影响主服务"""
    class HubHandler(BaseHTTPRequestHandler):
        # HTTP/1.1 keep-alive: 消除隧道场景下每个请求的重复握手 (页面加载提速主因)
        protocol_version = "HTTP/1.1"
        disable_nagle_algorithm = True   # 小包即时发出, 降低门禁页/错误页首字节延迟

        def log_message(self, *a):
            pass

        def _key_ok(self):
            q = up.parse_qs(up.urlparse(self.path).query)
            k = (q.get("key", [""])[0] or "").strip()
            good = _upd_hub_key()
            return bool(good) and hmac.compare_digest(k, good)

        def _json(self, obj, code=200):
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except OSError:
                pass

        def _html(self, body, code=200, gz_ok=False):
            """HTML 响应: 客户端支持时 gzip 压缩 (隧道场景省传输), 全部带 Content-Length 以复用 keep-alive"""
            enc = (self.headers.get("Accept-Encoding") or "")
            if gz_ok and "gzip" in enc.lower():
                body = gzip.compress(body if isinstance(body, bytes) else body.encode("utf-8"))
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass
                return
            data = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except OSError:
                pass

        def do_GET(self):
            p = up.urlparse(self.path).path
            if p in ("/dl", "/dl/go"):
                # 分享下载已关闭: 只保留官方 GitHub 发布页作为唯一下载渠道
                self._html("<!DOCTYPE html><html lang=\"zh\"><head><meta charset=\"utf-8\">"
                           "<title>分享下载已关闭</title></head><body style=\"font:15px/1.7 'Microsoft YaHei',sans-serif;"
                           "padding:40px;text-align:center\"><h3>分享下载已关闭</h3>"
                           "<p>新版请仅从官方 GitHub 发布页下载, 请勿使用他人分享的安装包。</p>"
                           "<p><a href=\"%s\">%s</a></p></body></html>" % (OFFICIAL_URL, OFFICIAL_URL), 403)
                return
            if p in ("/VERSION", "/update.zip"):
                # 源协议接口仅限本机 (仅用于版本检查); 外部分享下载已关闭
                if self.client_address[0] not in ("127.0.0.1", "::1"):
                    self._json({"ok": False, "error": "分享下载已关闭, 请从官方 GitHub 渠道下载"}, 403)
                    return
                f = UPD_HUB_DIR / p.lstrip("/")
                if not f.is_file():
                    self._json({"ok": False, "error": "源尚未发布任何版本"}, 404)
                    return
                data = f.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type",
                                 "application/zip" if p.endswith(".zip") else "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except OSError:
                    pass
                return
            if p == "/status":
                if not self._key_ok():
                    self._json({"ok": False, "error": "需要管理者密钥"}, 403)
                    return
                vf, zf = UPD_HUB_DIR / "VERSION", UPD_HUB_DIR / "update.zip"
                self._json({"ok": True, "data": {
                    "version": vf.read_text(encoding="utf-8").strip() if vf.is_file() else "",
                    "zip_size": zf.stat().st_size if zf.is_file() else 0,
                    "published": zf.is_file()}})
                return
            self._json({"ok": False, "error": "Not Found"}, 404)

        def do_POST(self):
            if up.urlparse(self.path).path != "/upload":
                self._json({"ok": False, "error": "Not Found"}, 404)
                return
            if not self._key_ok():
                self._json({"ok": False, "error": "需要管理者密钥 (key 参数)"}, 403)
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            if n <= 0 or n > 80 * 1024 * 1024:
                self._json({"ok": False, "error": "包大小异常 (0 或超过 80MB)"}, 400)
                return
            try:
                ver = _upd_hub_publish(self.rfile.read(n))
            except (RuntimeError, zipfile.BadZipFile) as e:
                self._json({"ok": False, "error": str(e)}, 400)
                return
            except OSError as e:
                self._json({"ok": False, "error": "写入失败: %s" % e}, 500)
                return
            self._json({"ok": True, "data": {"message": "发布成功", "version": ver, "size": n}})

    try:
        srv = ThreadingHTTPServer(("0.0.0.0", UPD_HUB_PORT), HubHandler)
    except OSError:
        print("[update-hub] 端口 %d 不可用, 更新源未启动" % UPD_HUB_PORT, flush=True)
        return
    srv.daemon_threads = True
    _upd_hub_key()   # 启动即确保密钥存在 (管理者随时可发布)
    print("[update-hub] 更新源已启动: http://127.0.0.1:%d (管理接口需 admin.key)" % UPD_HUB_PORT, flush=True)
    try:
        srv.serve_forever()
    except Exception:
        pass


# ---------------- 门禁下载 (是否公开由管理者决定) ----------------
# 下载入口 = 更新源站 (8966) 的 /dl 门禁页: 每次下载必须输入管理者生成的
# 一次性密码 (随机 6 位, 3 分钟有效, 用后即焚, 时间到即作废);
# 公网可达由 cloudflared 临时隧道提供 (免账号, 随开随关), 不开隧道时仅局域网可用
DL_TTL = 180
_DL_STATE = {"proc": None, "url": "", "tokens": {}}


def _dl_cf_path():
    return ROOT / "runtime" / "cloudflared.exe"


def _dl_ensure_cf():
    """确保 cloudflared.exe 就绪 (无则从镜像/GitHub 下载到 runtime/)"""
    p = _dl_cf_path()
    if p.is_file() and p.stat().st_size > 1024 * 1024:
        return p
    p.parent.mkdir(parents=True, exist_ok=True)
    urls = [
        "https://ghfast.top/https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe",
        "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe",
    ]
    last = None
    for u in urls:
        try:
            print("[dl-tunnel] 下载 cloudflared: %s" % u, flush=True)
            op = build_opener(HTTPSHandler(context=ssl._create_unverified_context()), ProxyHandler({}))
            req = Request(u)
            req.add_header("User-Agent", "Mozilla/5.0")
            with op.open(req, timeout=180) as r, open(p, "wb") as f:
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
            if p.stat().st_size > 1024 * 1024:
                return p
        except Exception as e:
            last = e
    try:
        p.unlink()
    except OSError:
        pass
    raise RuntimeError("cloudflared 下载失败 (%s), 可手动下载 cloudflared-windows-amd64.exe 放入 runtime/ 目录" % last)


def _dl_tunnel_start():
    """启动 cloudflared 临时隧道 (指向门禁站 8966), 返回公网 URL"""
    st = _DL_STATE
    if st["proc"] and st["proc"].poll() is None:
        if st["url"]:
            return {"url": st["url"], "running": True}
        try:
            st["proc"].terminate()
        except OSError:
            pass
    cf = _dl_ensure_cf()
    try:
        st["proc"] = subprocess.Popen(
            [str(cf), "tunnel", "--url", "http://127.0.0.1:%d" % UPD_HUB_PORT,
             "--no-autoupdate", "--protocol", "http2"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as e:
        raise RuntimeError("隧道启动失败: %s" % e)
    st["url"] = ""
    logf = open(ROOT / "runtime" / "dl-tunnel.log", "ab")

    def _watch():
        try:
            for raw in iter(st["proc"].stderr.readline, b""):
                try:
                    logf.write(raw)
                    logf.flush()
                except OSError:
                    pass
                m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", raw.decode("utf-8", "ignore"))
                if m:
                    st["url"] = m.group(0).rstrip("/")
                    print("[dl-tunnel] 公网入口: %s" % st["url"], flush=True)
        except Exception:
            pass
    threading.Thread(target=_watch, daemon=True).start()
    for _ in range(30):   # 隧道建立通常 3-8s, 最多等 15s
        if st["url"]:
            break
        if st["proc"].poll() is not None:
            logf.close()
            raise RuntimeError("隧道进程退出 (code %s), 当前网络可能无法建立隧道" % st["proc"].returncode)
        time.sleep(0.5)
    if not st["url"]:
        logf.close()
        raise RuntimeError("隧道建立超时, 请稍后重试")
    # 公网自检: 临时隧道偶发只注册部分 DNS/边缘不通, 不可达则报错让管理者重开
    ok = False
    for _ in range(4):
        time.sleep(2)
        try:
            op = build_opener(HTTPSHandler(context=ssl._create_unverified_context()), ProxyHandler({}))
            req = Request(st["url"] + "/dl")
            req.add_header("User-Agent", "Mozilla/5.0")
            with op.open(req, timeout=8) as r:
                if r.status == 200:
                    ok = True
                    break
        except Exception:
            pass
    if not ok:
        logf.close()
        try:
            st["proc"].terminate()
        except OSError:
            pass
        st["proc"], st["url"] = None, ""
        raise RuntimeError("隧道建立但公网不可达 (临时隧道偶发问题), 请重试一次开启")
    logf.close()
    return {"url": st["url"], "running": True}


def _dl_tunnel_stop():
    st = _DL_STATE
    if st["proc"]:
        try:
            st["proc"].terminate()
        except OSError:
            pass
        st["proc"] = None
    st["url"] = ""
    return {"url": "", "running": False}


def _dl_token_new():
    """生成一次性下载密码: 随机 6 位, 3 分钟有效, 用后即焚; 顺带清理过期令牌"""
    st = _DL_STATE
    now = time.time()
    for k in [k for k, v in st["tokens"].items() if v["used"] or v["exp"] < now]:
        st["tokens"].pop(k, None)
    import random as _random
    while True:
        pw = "%06d" % _random.randint(0, 999999)
        if pw not in st["tokens"]:
            break
    st["tokens"][pw] = {"exp": now + DL_TTL, "used": False}
    return pw


def _dl_lan_url():
    """本机局域网地址 (无公网隧道时的下载入口)"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return "http://%s:%d" % (s.getsockname()[0], UPD_HUB_PORT)
        finally:
            s.close()
    except OSError:
        return "http://127.0.0.1:%d" % UPD_HUB_PORT


def _dl_page():
    """门禁下载页 (输密码领取文件, 零 JS 零外链; 版本/大小未变时复用内存缓存)"""
    vf, zf = UPD_HUB_DIR / "VERSION", UPD_HUB_DIR / "update.zip"
    ver = vf.read_text(encoding="utf-8").strip() if vf.is_file() else ""
    zsize = zf.stat().st_size if zf.is_file() else 0
    sig = (ver, zsize)
    if _DL_STATE.get("page_sig") == sig:
        return _DL_STATE["page_body"]
    size = "%.1f MB" % (zsize / 1048576.0)
    body = (
        '<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<link rel="icon" href="data:,">'
        '<title>超越次元 v%s - 下载</title><style>'
        '*{box-sizing:border-box}'
        'html,body{margin:0}'
        'body{font:15px/1.6 "Segoe UI","Microsoft YaHei",sans-serif;background:#faf8f4;color:#2b2b2b;'
        'display:flex;justify-content:center;align-items:center;min-height:100dvh;padding:16px}'
        '.card{max-width:420px;width:100%%;background:#fff;border:2px solid #1c1c1c;border-radius:14px;'
        'box-shadow:6px 6px 0 #1c1c1c;padding:30px;text-align:center}'
        'h1{margin:0 0 4px;font-size:24px}.sub{color:#666;margin:0 0 20px}'
        'input{width:200px;font-size:22px;letter-spacing:6px;text-align:center;border:2px solid #1c1c1c;'
        'border-radius:10px;padding:10px;outline:none}'
        'button{display:block;width:100%%;margin:16px 0 0;font-size:16px;font-weight:700;cursor:pointer;'
        'background:#ff5252;color:#fff;border:2px solid #1c1c1c;border-radius:10px;padding:11px;'
        'box-shadow:3px 3px 0 #1c1c1c;transition:transform .12s,box-shadow .12s}'
        'button:hover{transform:translate(-2px,-2px);box-shadow:5px 5px 0 #1c1c1c}'
        '.tip{color:#999;font-size:12px;margin-top:16px}'
        '</style></head><body><div class="card">'
        '<h1>超越次元 <span style="background:#ff5252;color:#fff;border-radius:8px;padding:1px 10px;font-size:15px">v%s</span></h1>'
        '<p class="sub">追番神器 · 独立桌面应用 · 安装包 %s</p>'
        '<form action="/dl/go" method="get">'
        '<input name="pw" inputmode="numeric" maxlength="6" placeholder="&#9679;&#9679;&#9679;&#9679;&#9679;&#9679;" autofocus>'
        '<button type="submit">输入密码下载</button></form>'
        '<p class="tip">密码由分享者提供, 每个密码仅可用一次, 3 分钟后自动作废</p>'
        '</div></body></html>'
    ) % (ver, ver, size)
    _DL_STATE["page_sig"] = sig
    _DL_STATE["page_body"] = body
    return body


def _reco_prewarm():
    """启动预热推荐数据: AniList 标签枚举 (特征找番首次查询免等) + 当年年度榜
    (24h 缓存, 进推荐页默认展示当年 -> 秒开); 均为后台线程, 失败静默不影响服务"""
    try:
        _anilist_tag_set()
    except Exception:
        pass
    try:
        reco_yearly(time.localtime().tm_year)
    except Exception as e:
        print("[reco] 年度榜预热失败: %r" % (e,), flush=True)
    try:
        age_home()   # 首页板块冷缓存转嫁到启动期, 开窗即热
    except Exception as e:
        print("[prewarm] 首页预热失败: %r" % (e,), flush=True)
    try:
        age_update()   # 一周更新表同理
    except Exception as e:
        print("[prewarm] 更新表预热失败: %r" % (e,), flush=True)


def _reco_auto_refresh():
    """年度评分榜自动刷新: 每 12h 强制重抓当年+上一年榜单 (含 Bangumi 评分充实),
    保证评分随 Bangumi 社区变化自动更新; 失败本轮静默, 下轮重试"""
    while True:
        time.sleep(12 * 3600)
        y = time.localtime().tm_year
        for yy in (y, y - 1):
            try:
                reco_yearly(yy, force=True)
                print("[reco] 年度榜 %d 自动刷新完成" % yy, flush=True)
            except Exception as e:
                print("[reco] 年度榜 %d 自动刷新失败: %r" % (yy, e), flush=True)


_APPDATA_DIR = ROOT / ".app-data"
# Edge 配置文件内允许安全删除的浏览器缓存子目录 (删除不影响功能, 浏览器会自动重建)
_APPDATA_CACHE_SUBS = ["Default/Cache", "Default/Code Cache", "Default/GPUCache",
                       "Default/DawnGraphiteCache", "Default/DawnWebGPUCache",
                       "GrShaderCache", "ShaderCache"]
_APPDATA_CACHE_MAX = 300 * 1024 * 1024   # 单个缓存子目录超过 300MB 触发清理


def _appdata_housekeeping():
    """Edge 配置文件 (.app-data) 里浏览器缓存会无上限增长, 启动时超限即清。
    Edge 运行中被锁定的文件清不掉会静默跳过, 不影响功能 (浏览器自动重建缓存目录)"""
    try:
        if not _APPDATA_DIR.is_dir():
            return
        for sub in _APPDATA_CACHE_SUBS:
            d = _APPDATA_DIR.joinpath(*sub.split("/"))
            if not d.is_dir():
                continue
            files = []
            total = 0
            for f in d.rglob("*"):
                try:
                    if f.is_file():
                        files.append(f)
                        total += f.stat().st_size
                except OSError:
                    pass
            if total <= _APPDATA_CACHE_MAX:
                continue
            files.sort(key=lambda f: f.stat().st_mtime)   # 最旧优先
            for f in files:
                try:
                    f.unlink()
                except OSError:
                    pass   # 被浏览器占用, 下次再清
    except Exception:
        pass


def _bgm_reach_probe():
    """启动即探测 bgm.tv 可达性 (独立线程, 与其他预热并行): 被墙时提前建立 10 分钟短路,
    用户首次评分请求不再等满超时 (实测单次变体链 34s)"""
    try:
        metadata_info("海贼王")
    except Exception:
        pass
