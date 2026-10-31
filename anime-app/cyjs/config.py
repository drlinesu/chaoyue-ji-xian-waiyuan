# -*- coding: utf-8 -*-
"""全局配置 / 路径 / UA / 全局 opener"""
import ssl
from pathlib import Path
from urllib.request import build_opener, HTTPSHandler, ProxyHandler

PORT = 8965
# 外源版: 关闭 AGE 主站, 所有番剧内容改由「外部片源渠道」提供 (见 sources/vodch.py)
NO_AGE = True
ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE

_opener = build_opener(ProxyHandler({}), HTTPSHandler(context=_ssl_ctx))
