#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""运行配置：全部来自服务端下发，本地只保存"输出目录"这一个用户选择。

没有任何 ini / 用户可编辑的配置文件。
  · 服务地址：内置常量 SERVICE_URL（改不了）
  · RH Key / 模板清单 / COS：登录时由服务端随票下发，加密缓存在 license.json
  · 输出目录：用户用"浏览"选一次，记在 ui.json（加密）
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# 唯一的内置配置：配置/授权服务地址
SERVICE_URL = "https://1463907213-3ifuebn6ih.ap-guangzhou.tencentscf.com"

APP_NAME = "RHOutfitTool"


def app_dir() -> Path:
    """exe 所在目录（打包后）或项目根目录（源码运行）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _state_dir() -> Path:
    for env in ("APPDATA", "LOCALAPPDATA"):
        base = os.getenv(env)
        if base:
            candidate = Path(base) / APP_NAME
            try:
                candidate.mkdir(parents=True, exist_ok=True)
                return candidate
            except OSError:
                continue
    fallback = app_dir() / "_state"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def _ui_path() -> Path:
    return _state_dir() / "ui.json"


def load_ui() -> dict:
    from app.secure_store import unseal

    path = _ui_path()
    if not path.exists():
        return {}
    try:
        data = unseal(path.read_text(encoding="utf-8"))
    except OSError:
        return {}
    return data if isinstance(data, dict) else {}


def save_ui(data: dict) -> None:
    from app.secure_store import seal

    try:
        _ui_path().write_text(seal(data), encoding="utf-8")
    except OSError:
        pass


def output_dir() -> Path:
    """用户选过的目录优先；否则 exe 同目录下的「输出」。"""
    saved = (load_ui().get("output_dir") or "").strip()
    if saved:
        return Path(saved)
    return app_dir() / "输出"


def save_output_dir(path: str) -> None:
    ui = load_ui()
    ui["output_dir"] = str(path)
    save_ui(ui)


def truthy(value) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on", "是"}


def load() -> dict:
    """组装成界面/引擎使用的扁平字典（键名保持历史一致，减少改动面）。"""
    from app import license as licensemod

    remote = licensemod.get_config() or {}
    cos = remote.get("cos") or {}
    return {
        "RH_API_KEY": str(remote.get("rh_api_key") or "").strip(),
        "MANIFEST": str(remote.get("manifest_url") or "").strip(),
        "OUTPUT_ROOT": str(output_dir()),
        "COS_ENABLED": "true" if truthy(cos.get("enabled")) else "false",
        "COS_BUCKET": str(cos.get("bucket") or "").strip(),
        "COS_REGION": str(cos.get("region") or "ap-guangzhou").strip(),
        "COS_PREFIX": str(cos.get("prefix") or "").strip(),
        "COS_DOMAIN": str(cos.get("domain") or "").strip(),
        "NOTICE": str(remote.get("notice") or "").strip(),
        "SERVICE_URL": SERVICE_URL,
    }
