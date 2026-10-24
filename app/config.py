#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""后台配置文件（界面上不出现任何输入框）。

文件：与 exe 同目录的 `配置.ini`，纯文本 KEY=VALUE，# 开头是注释。
首次启动自动生成模板，用户只需要填 `RH_API_KEY` 一行。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

CONFIG_NAME = "配置.ini"

# 键 -> (默认值, 说明)
DEFAULTS: dict[str, str] = {
    "RH_API_KEY": "",
    "MANIFEST": "",
    "OUTPUT_ROOT": "",
    "COS_ENABLED": "false",
    "COS_BUCKET": "",
    "COS_REGION": "ap-guangzhou",
    "COS_PREFIX": "",
    "COS_DOMAIN": "",
    "LICENSE_SERVER": "",
}

TEMPLATE = """# ============================================================
#  换装批量生产器 · 配置文件
#  用记事本改下面几行，保存后回软件点「重载配置」即可，不用重启。
#  等号右边不要留空格，也不要加引号。
# ============================================================

# 【必填】RunningHub 的 32 位 Key（企业级-共享 API Key）
# 拿 Key：https://www.runninghub.cn/enterprise-api/sharedApi
RH_API_KEY=

# 【必填】500 个模板视频的清单地址（manifest.json），可以是 https 链接或本地文件路径
# 生成清单：python tools/build_manifest.py --src 视频目录 --base-url https://你的CDN/tpl
MANIFEST=

# 【可选】视频存到哪；留空 = exe 同目录下的「输出」
OUTPUT_ROOT=

# 【可选】成品自动上传腾讯云 COS；不填密钥时会自动用本机已有凭证
#        （环境变量 COS_SECRET_ID/COS_SECRET_KEY、~/.cos/credentials.json、tccli 登录态）
COS_ENABLED=false
COS_BUCKET=
COS_REGION=ap-guangzhou
COS_PREFIX=
COS_DOMAIN=

# 【必填】授权服务器：启动时必须输入分配的密码才能进主界面
#        留空会使用程序内置地址；内置地址也没有时，程序会拒绝启动（不会放行）
#        例：https://license.你的域名.com  或  http://127.0.0.1:8099
LICENSE_SERVER=
"""


def app_dir() -> Path:
    """exe 所在目录（打包后）或项目根目录（源码运行）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def config_path() -> Path:
    return app_dir() / CONFIG_NAME


def _parse(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";", "[", "//")):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip().upper()] = value.strip()
    return values


def load() -> dict:
    """读配置；缺字段用默认值补齐。文件不存在时自动生成模板。"""
    path = config_path()
    if not path.exists():
        try:
            path.write_text(TEMPLATE, encoding="utf-8")
        except OSError:
            pass
        values: dict[str, str] = {}
    else:
        raw = path.read_bytes()
        for encoding in ("utf-8-sig", "gbk"):
            try:
                values = _parse(raw.decode(encoding))
                break
            except UnicodeDecodeError:
                continue
        else:
            values = {}

    cfg = dict(DEFAULTS)
    cfg.update({k: v for k, v in values.items() if k in DEFAULTS})

    # 旧版本把配置放在 %APPDATA%\RHOutfitTool\config.json，做一次性迁移
    legacy = Path(os.getenv("APPDATA") or Path.home()) / "RHOutfitTool" / "config.json"
    if not cfg["RH_API_KEY"] and legacy.exists():
        try:
            old = json.loads(legacy.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            old = {}
        cfg["RH_API_KEY"] = old.get("api_key", "") or ""
        cfg["MANIFEST"] = cfg["MANIFEST"] or old.get("manifest", "") or ""
        cfg["OUTPUT_ROOT"] = cfg["OUTPUT_ROOT"] or old.get("output_root", "") or ""
        if old.get("cos_enabled"):
            cfg["COS_ENABLED"] = "true"
            cfg["COS_BUCKET"] = cfg["COS_BUCKET"] or old.get("cos_bucket", "") or ""
            cfg["COS_REGION"] = cfg["COS_REGION"] or old.get("cos_region", "") or ""
            cfg["COS_PREFIX"] = cfg["COS_PREFIX"] or old.get("cos_prefix", "") or ""
            cfg["COS_DOMAIN"] = cfg["COS_DOMAIN"] or old.get("cos_domain", "") or ""

    if not cfg["OUTPUT_ROOT"]:
        cfg["OUTPUT_ROOT"] = str(app_dir() / "输出")
    return cfg


def truthy(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on", "是"}


def mask(secret: str) -> str:
    """日志/界面里显示用：只露头尾。"""
    secret = (secret or "").strip()
    if len(secret) <= 8:
        return "***" if secret else "(空)"
    return f"{secret[:4]}…{secret[-4:]}"
