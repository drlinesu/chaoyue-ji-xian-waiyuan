# -*- coding: utf-8 -*-
"""外部片源渠道: 放在 <动漫目录>/外部片源 下的声明式源包 (JSON)。

两种类型:
  type=maccms  苹果CMS10 JSON 接口 (api 为 .../api.php/provide/vod/)
  type=age     AGE 动漫站点 (api 为站点域名, 抓取实现沿用 age.py)

与内置片源完全独立: 内置 EXT_VOD_APIS 不变, 这里只做"追加"。
不加载任何可执行代码, 渠道文件仅包含名称/类型/接口地址/镜像, 避免外部文件带来代码执行风险。
"""
import json
import re
import time
from pathlib import Path

from ..cache import cache_drop_prefix
from ..config import ROOT


CHANNEL_DIR = ROOT.parent / "外部片源"     # 放在动漫目录, 不随 app 更新覆盖
_ID_SAFE_RE = re.compile(r"[^0-9A-Za-z_\-\u4e00-\u9fff ]+")


def channel_dir():
    d = CHANNEL_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _channel_files():
    return sorted(p for p in channel_dir().glob("*.json") if p.is_file())


def _safe_id(name):
    base = _ID_SAFE_RE.sub("", str(name or "")).strip().replace(" ", "_")
    return (base or ("channel_%d" % int(time.time()))) + ".json"


def _valid_api(u):
    u = str(u or "").strip()
    return u if u.lower().startswith(("http://", "https://")) else ""


def _entry(pid, j):
    mirrors = j.get("mirrors") or []
    if not isinstance(mirrors, list):
        mirrors = []
    typ = str(j.get("type") or "maccms").strip().lower()
    if typ not in ("maccms", "age"):
        typ = "maccms"
    return {
        "id": pid,
        "name": str(j.get("name") or Path(pid).stem),
        "type": typ,
        "api": _valid_api(j.get("api")),
        "mirrors": [_valid_api(m) for m in mirrors if _valid_api(m)],
        "enabled": bool(j.get("enabled", True)),
        "lines_only": bool(j.get("lines_only", False)),   # 仅作备用播放线路, 不参与首页/搜索/分类
        "file": str(channel_dir() / pid),
    }


def list_channels():
    """列出全部外部渠道 (含异常文件, ok=False)"""
    out = []
    for p in _channel_files():
        try:
            j = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(j, dict):
                raise ValueError("非法结构")
            e = _entry(p.name, j)
            e["ok"] = bool(e["api"])
            if not e["ok"]:
                e["error"] = "接口地址需为 http(s)"
            out.append(e)
        except Exception as e:
            out.append({"id": p.name, "name": p.stem, "ok": False,
                        "enabled": False, "error": "JSON 解析失败: %s" % e})
    return out


def enabled_channels():
    """启用且格式正确的渠道 (按文件名排序, 便于线路编号稳定)"""
    return [c for c in list_channels() if c.get("ok") and c.get("enabled")]


def enabled_apis():
    """追加到内置片源之后的 (名称, 主接口, 镜像列表) —— 仅 maccms 渠道"""
    return [(c["name"], c["api"], c["mirrors"]) for c in enabled_channels()
            if c.get("type") != "age"]


def age_channels():
    """启用中的 AGE 渠道 (type=age, api 为 AGE 站点域名)"""
    return [c for c in enabled_channels() if c.get("type") == "age"]


def save_channel(name, api, mirrors="", enabled=True, old_id=""):
    """新增/覆盖渠道 (old_id 非空表示编辑旧文件), 返回保存后的条目"""
    name = str(name or "").strip()
    api = _valid_api(api)
    if not name:
        raise RuntimeError("渠道名称不能为空")
    if len(name) > 40:
        raise RuntimeError("渠道名称过长 (最多 40 字)")
    if not api:
        raise RuntimeError("接口地址需为 http(s), 参考: https://host/api.php/provide/vod/")
    ms = [m.strip() for m in re.split(r"[,，\s]+", str(mirrors or "")) if m.strip()]
    ms = [m for m in ms if _valid_api(m)]
    pid = old_id if old_id and old_id.endswith(".json") else _safe_id(name)
    if old_id and old_id != pid:
        try:
            (channel_dir() / old_id).unlink()
        except OSError:
            pass
    typ = "maccms"     # 编辑 AGE 渠道时保持原有类型, 不被表单改成 maccms
    old_p = channel_dir() / pid
    if old_p.is_file():
        try:
            if str(json.loads(old_p.read_text(encoding="utf-8")).get("type") or "").lower() == "age":
                typ = "age"
        except Exception:
            pass
    data = {"name": name, "type": typ, "api": api,
            "mirrors": ms, "enabled": bool(enabled)}
    (channel_dir() / pid).write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    cache_drop_prefix("ext:lines:")     # 渠道变更后线路缓存立即失效
    return _entry(pid, data)


def toggle_channel(cid, enabled):
    p = channel_dir() / str(cid or "")
    if not p.is_file() or p.suffix.lower() != ".json":
        raise RuntimeError("渠道不存在")
    try:
        j = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(j, dict):
            raise ValueError
    except Exception:
        raise RuntimeError("渠道文件损坏, 请删除后重加")
    j["enabled"] = bool(enabled)
    p.write_text(json.dumps(j, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    cache_drop_prefix("ext:lines:")
    return _entry(p.name, j)


def delete_channel(cid):
    p = channel_dir() / str(cid or "")
    if p.parent != channel_dir() or p.suffix.lower() != ".json":
        raise RuntimeError("非法渠道标识")
    if not p.is_file():
        raise RuntimeError("渠道不存在")
    p.unlink()
    cache_drop_prefix("ext:lines:")
    return {"message": "已删除"}
