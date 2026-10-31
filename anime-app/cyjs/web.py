# -*- coding: utf-8 -*-
"""HTTP 路由层: Handler 及其全部 /api 端点 (依赖由 server 启动时注入)"""
import sys
from http.server import BaseHTTPRequestHandler


# ----------------------------------------------------------------------------
# HTTP 服务
# ----------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "DimensionAnime/1.0"
    protocol_version = "HTTP/1.1"

    # ---- 基础输出 ----
    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        # 文本响应 gzip 压缩 (JSON API 体积减 70%+, 明显加快接口响应)
        enc = self.headers.get("Accept-Encoding", "")
        if (len(body) > 1024 and "gzip" in enc
                and any(t in ctype for t in ("json", "html", "css", "javascript", "mpegurl"))):
            body = gzip.compress(body, 5)
            extra = dict(extra or {})
            extra["Content-Encoding"] = "gzip"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _ok(self, data):
        self._send(200, {"ok": True, "data": data})

    def _err(self, msg, code=502):
        self._send(code, {"ok": False, "error": str(msg)})

    def _redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, fmt, *args):
        pass

    # ---- 路由 ----
    def do_GET(self):
        try:
            parsed = up.urlparse(self.path)
            path = parsed.path
            qs = up.parse_qs(parsed.query)

            if path.startswith("/api/"):
                self._api(path, qs)
            else:
                self._static(path)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            try:
                self._err(e)
            except Exception:
                pass

    def _api(self, path, qs):
        g = lambda k: (qs.get(k, [""])[0] or "").strip()

        if path == "/api/health":
            return self._ok({"port": self.server.server_port})

        # ---------------- 动漫 ----------------
        if path == "/api/anime/home":
            return self._ok(age_home(refresh=g("refresh") == "1"))
        if path == "/api/anime/search":
            kw = g("kw")
            if not kw:
                return self._err("关键词不能为空", 400)
            return self._ok(anime_search(kw))
        if path == "/api/anime/detail":
            return self._ok(age_detail(g("id")))
        if path == "/api/anime/play":
            data = age_resolve_play(g("id"), g("line") or "1", g("ep") or "1")
            data["ad"] = _is_ad_url(data.get("url"))
            return self._ok(data)
        if path == "/api/anime/update":
            data = age_update()
            try:
                snames = season_names()
                groups = data.get("groups")
                if isinstance(groups, dict):
                    for items in groups.values():
                        for it in items if isinstance(items, list) else []:
                            if isinstance(it, dict):
                                it["season"] = _in_season(it.get("title") or "", snames)
                al = data.get("all")
                if isinstance(al, list):
                    for it in al:
                        if isinstance(it, dict):
                            it["season"] = _in_season(it.get("title") or "", snames)
            except Exception as e:
                print("[season] 标记失败: %r" % (e,), flush=True)
            return self._ok(data)
        if path == "/api/anime/catalog":
            return self._ok(age_catalog(page=g("page") or 1, genre=g("genre") or 0, order=g("order") or "time"))
        if path == "/api/anime/illus":
            return self._ok(fetch_illus(n=g("n") or 12, refresh=g("refresh") == "1"))
        if path == "/api/anime/extlines":
            return self._ok(ext_lines(g("title")))
        if path == "/api/anime/source_check":
            return self._ok(source_self_check())
        # ---------------- 外部片源渠道 (动漫目录/外部片源 下的声明式 JSON) ----------------
        if path == "/api/src/channels":
            return self._ok(list_channels())
        if path in ("/api/src/channel/save", "/api/src/channel/toggle",
                    "/api/src/channel/delete", "/api/src/channel/open"):
            if self.client_address[0] not in ("127.0.0.1", "::1"):
                return self._err("仅本机可管理外部片源渠道", 403)
            if path == "/api/src/channel/open":
                d = channel_dir()
                os.startfile(str(d))
                return self._ok({"message": "已打开外部片源文件夹", "dir": str(d)})
            if path == "/api/src/channel/delete":
                return self._ok(delete_channel(g("id")))
            if path == "/api/src/channel/toggle":
                return self._ok(toggle_channel(g("id"), g("enabled") in ("1", "true", "on")))
            return self._ok(save_channel(g("name"), g("api"), g("mirrors"),
                                         g("enabled") in ("", "1", "true", "on"), g("id")))
        if path == "/api/danmaku":
            return self._ok(bili_danmaku(g("title"), g("ep") or "1"))
        # ---------------- 推荐板块 (独立增量接口, 不影响既有接口) ----------------
        if path == "/api/anime/reco":
            return self._ok(reco_recommend(g("kw")))
        if path == "/api/anime/reco/feature":
            return self._ok(reco_feature(g("kw")))
        if path == "/api/anime/reco/year":
            return self._ok(reco_yearly(g("year")))
        if path == "/api/anime/reco/go":
            return self._ok(reco_resolve(g("kw")))
        if path == "/api/bangumi":
            return self._ok(metadata_info(g("kw")))

        # ---------------- 下载 ----------------
        if path == "/api/download/start":
            job = dl_start(g("id"), g("line") or "1", g("ep") or "1",
                           g("title"), g("name"))
            return self._ok({"message": "已加入下载队列", "job": job})
        if path == "/api/download/list":
            return self._ok(dl_list())
        if path == "/api/download/cancel":
            dl_cancel(g("job"))
            return self._ok({"message": "已取消"})
        if path == "/api/download/open":
            d = dl_dir()
            d.mkdir(parents=True, exist_ok=True)
            os.startfile(str(d))
            return self._ok({"message": "已打开文件夹"})

        # ---------------- 更新 ----------------
        if path == "/api/update/check":
            return self._ok(update_check())
        if path == "/api/update/notice":
            return self._ok(update_notice())
        if path == "/api/update/progress":
            return self._ok(dict(_UPD_PROG))
        if path in ("/api/update/apply", "/api/update/source",
                    "/api/update/dl_token", "/api/update/dl_tunnel"):
            # 应用内更新与分享下载已关闭: 应用只提示新版本, 下载统一走官方 GitHub 渠道
            return self._err("应用内更新与分享下载已关闭, 请前往官方 GitHub 渠道下载", 403)

        # ---------------- 漫画（快看） ----------------
        if path == "/api/manga/home":
            return self._ok(manga_home(refresh=g("refresh") == "1"))
        if path == "/api/manga/search":
            kw = g("kw")
            if not kw:
                return self._err("关键词不能为空", 400)
            return self._ok(manga_search(kw))
        if path == "/api/manga/detail":
            return self._ok(manga_detail(g("id")))
        if path == "/api/manga/chapter":
            return self._ok(manga_chapter(g("id")))

        # ---------------- 代理 ----------------
        if path in ("/api/proxy/hls", "/api/proxy/ts", "/api/proxy/img"):
            return self._proxy(path, qs)

        return self._err("not found", 404)

    def _proxy(self, path, qs):
        u = (qs.get("u", [""])[0] or "").strip()
        if not u.startswith("http"):
            return self._err("invalid url", 400)
        host = up.urlparse(u).netloc
        referer = "https://%s/" % host
        if "mangafuna" in host or "copymanga" in host or "mangacopy" in host or "hi77" in host:
            referer = "https://www.copymanga.tv/"
        try:
            if path.endswith("hls"):
                # 短 TTL 缓存 m3u8 文本
                text = http_get_text(u, referer=referer, timeout=15)
                if "#EXTM3U" not in text:
                    # 可能是解析页而非清单
                    m = re.search(r'(https?://[^"\'\s<>]+?\.m3u8[^"\'\s<>]*)', text)
                    if m:
                        return self._redirect(proxied(m.group(1), "hls"))
                    return self._err("invalid m3u8", 502)
                out = rewrite_m3u8(text, u)
                _pl_register(u, text)   # 登记分片顺序供前方预取 (主清单自动跳过)
                return self._send(200, out, "application/vnd.apple.mpegurl")
            # ts / img: 流式转发
            self._stream(u, referer, is_img=path.endswith("img"))
        except Exception as e:
            try:
                self._err(e)
            except Exception:
                pass

    def _serve_file(self, p, ctype, rng=None):
        """带 Range 支持的本地文件回源 (命中分段缓存时零网络延迟)"""
        try:
            size = p.stat().st_size
        except OSError:
            return
        start, end, code = 0, size - 1, 200
        m = re.match(r"bytes=(\d*)-(\d*)", rng or "")
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
            else:
                start = max(0, size - int(m.group(2)))
            if start >= size or start > end:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            code = 206
        n = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(n))
        self.send_header("Accept-Ranges", "bytes")
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        try:
            with open(p, "rb") as f:
                f.seek(start)
                remain = n
                while remain > 0:
                    chunk = f.read(min(262144, remain))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remain -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except OSError:
            pass

    def _stream(self, url, referer, is_img=False):
        url = _safe_url(url)
        rng = self.headers.get("Range")
        # ts 分段: 先查磁盘 LRU 缓存 (回看/重播/多人同看场景免重复下载)
        if not is_img:
            _seg_init()
            cached = _seg_get(url)
            if cached:
                _seg_prefetch(url, referer)   # 命中也预取前方, 保持缓冲连续
                self._serve_file(cached, "video/mp2t", rng)
                return
            threading.Thread(target=_seg_prefetch, args=(url, referer), daemon=True).start()
        # 图片: 先查内存 LRU 缓存, 命中直接回 (零网络延迟, 大幅加快封面加载)
        if is_img:
            hit = img_cache_get(url)
            if hit:
                data, ctype = hit
                self.send_response(200)
                self.send_header("Content-Type", ctype or "image/jpeg")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "max-age=86400")
                self.send_header("X-Cache", "hit")
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            if img_fail_get(url):
                return self._img_placeholder()   # 3 分钟内失败过的图直接占位, 不再占用连接
        headers = {
            "User-Agent": UA,
            "Referer": referer,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "Connection": "close",
        }
        if rng:
            headers["Range"] = rng
        req = Request(url, headers=headers)
        # ts 直连上游偶发连接重置: 建连阶段快速重试一次 (响应头未发出前重试安全)
        try:
            resp = _opener.open(req, timeout=(_IMG_TIMEOUT if is_img else 30))
        except OSError:
            if is_img:
                img_fail_put(url)
                return self._img_placeholder()
            time.sleep(0.3)
            try:
                resp = _opener.open(req, timeout=30)
            except OSError as e:
                raise RuntimeError("上游连接失败: %s: %s" % (type(e).__name__, e)) from e
        with resp:
            code = resp.getcode()
            ctype = resp.headers.get("Content-Type", "")
            if is_img:
                # 先完整读入再回包: 失败/空内容直接给占位图, 不把坏响应写进缓存
                if code != 200:
                    img_fail_put(url)
                    return self._img_placeholder()
                try:
                    data = resp.read(10 * 1024 * 1024)
                except Exception:
                    data = b""
                if not data:
                    img_fail_put(url)
                    return self._img_placeholder()
                img_cache_put(url, data, ctype)
                self.send_response(200)
                self.send_header("Content-Type", ctype or "image/jpeg")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "max-age=86400")
                self.send_header("X-Cache", "miss")
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            self.send_response(code)
            if ctype:
                self.send_header("Content-Type", ctype)
            for h in ("Content-Length", "Content-Range", "Accept-Ranges"):
                if resp.headers.get(h):
                    self.send_header(h, resp.headers[h])
            if not resp.headers.get("Accept-Ranges"):
                self.send_header("Accept-Ranges", "bytes")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            # ts 分段: 边转发边落盘缓存 (整段完整才入缓存; Range/非200 不缓存)
            tmp = None
            cacheable = (not is_img and code == 200 and not rng
                         and "text" not in (ctype or "").lower())
            if cacheable:
                try:
                    _SEG_DIR.mkdir(parents=True, exist_ok=True)
                    tmp = _SEG_DIR / (_seg_key(url) + ".%d.tmp" % threading.get_ident())
                except OSError:
                    tmp = None
            try:
                tf = open(tmp, "wb") if tmp else None
            except OSError:
                tf = None
            try:
                while True:
                    chunk = resp.read(262144)
                    if not chunk:
                        break
                    if tf:
                        try:
                            tf.write(chunk)
                        except OSError:
                            tf = None
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        return
                if tf:
                    tf.close()
                    tf = None
                    _seg_put(url, tmp)
            except (BrokenPipeError, ConnectionResetError):
                return
            finally:
                if tf:
                    try:
                        tf.close()
                    except OSError:
                        pass
                # 未成功入缓存的临时文件 (下载中断/替换失败) 一律清理
                if tmp:
                    try:
                        tmp.unlink()
                    except OSError:
                        pass

    # ---- 静态文件 ----
    def _img_placeholder(self):
        """封面失败占位图: 快速返回, 不阻塞页面其余请求"""
        data = _IMG_PLACEHOLDER
        try:
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "max-age=60")
            self.send_header("X-Cache", "fail")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _static(self, path):
        if path in ("/", ""):
            path = "/index.html"
        fp = (STATIC / path.lstrip("/")).resolve()
        if not str(fp).startswith(str(STATIC.resolve())) or not fp.is_file():
            return self._send(404, "Not Found", "text/plain; charset=utf-8")
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".png": "image/png", ".jpg": "image/jpeg", ".svg": "image/svg+xml",
            ".ico": "image/x-icon", ".woff2": "font/woff2",
        }.get(fp.suffix.lower(), "application/octet-stream")
        data = fp.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # 静态资源缓存策略: 入口页与业务 js/css 不缓存 (避免改动后拿旧文件);
        # 第三方库/图标长缓存
        name = fp.name.lower()
        if path.endswith(".html") or fp.suffix.lower() in (".js", ".css"):
            cc = "no-cache"
        elif name.startswith("hls") or fp.suffix.lower() in (".ico", ".woff2"):
            cc = "max-age=86400"
        else:
            cc = "max-age=300"
        self.send_header("Cache-Control", cc)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass


def bind(namespace):
    """启动时把组合根的全局注入本模块 (过渡期依赖注入, 后续可换 AppContext)"""
    here = sys.modules[__name__]
    for _k, _v in list(namespace.items()):
        if not _k.startswith("__"):
            setattr(here, _k, _v)
    return Handler
