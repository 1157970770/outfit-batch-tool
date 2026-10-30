#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网页服务配置；密钥只从主机上的 server/config.json 读取。"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from app import config as appconfig


@dataclass
class WebSettings:
    base_dir: Path
    web_file: Path
    api_file: Path
    data_dir: Path
    config: dict
    web: dict
    templates: list[dict]
    templates_error: str = ""
    tag_workbook_path: Path | None = None
    tag_workbook_info: dict | None = None

    @property
    def users(self) -> dict:
        users = self.web.get("users")
        return users if isinstance(users, dict) else {}

    @property
    def max_active_jobs(self) -> int:
        return max(1, min(3, int(self.web.get("max_active_jobs", 2))))

    @property
    def max_items(self) -> int:
        return max(1, min(100, int(self.web.get("max_job_items", 20))))


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise RuntimeError(f"找不到配置文件：{path}") from None
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"配置文件读取失败：{path.name}（{exc}）") from None
    if not isinstance(data, dict):
        raise RuntimeError(f"配置文件格式错误：{path.name} 顶层应为 JSON 对象")
    return data


def load_settings(*, base_dir: str | Path | None = None,
                  web_file: str | Path | None = None,
                  api_file: str | Path | None = None,
                  load_templates: bool = True) -> WebSettings:
    use_runtime_defaults = base_dir is None
    base = Path(base_dir) if base_dir else appconfig.app_dir()
    web_path = Path(web_file) if web_file else Path(os.getenv("OUTFIT_WEB_CONFIG") or base / "server" / "web.json")
    api_path = Path(api_file) if api_file else Path(os.getenv("OUTFIT_CONFIG") or base / "server" / "config.json")
    web = _read_json(web_path)
    remote = _read_json(api_path)
    if not web.get("session_secret") or not isinstance(web.get("users"), dict) or not web["users"]:
        raise RuntimeError("网页账号尚未初始化。请在主机运行：python3 run_web.py --init")
    data_dir = Path(web.get("data_root") or "web-data")
    if not data_dir.is_absolute():
        data_dir = base / data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    output_dir = data_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    cfg = appconfig.from_remote(remote, output_dir)
    if not appconfig.wan_configured(cfg):
        raise RuntimeError(f"{api_path} 中没有可用的 wan.api_key/model 配置")

    tag_source = os.getenv("OUTFIT_TAG_WORKBOOK") or web.get("tag_workbook")
    if tag_source:
        tag_workbook_path = Path(str(tag_source)).expanduser()
        if not tag_workbook_path.is_absolute():
            tag_workbook_path = base / tag_workbook_path
    elif use_runtime_defaults:
        tag_workbook_path = Path.home() / "Desktop" / "标签清单1版.xlsx"
    else:
        # 测试/嵌入模式未指定时不隐式读取用户桌面工作簿。
        tag_workbook_path = None

    templates: list[dict] = []
    templates_error = ""
    tag_workbook_info: dict = {}
    manifest = cfg.get("MANIFEST", "")
    manifest_items: list[dict] = []
    manifest_error = ""
    if load_templates and manifest:
        try:
            from app.core import load_manifest
            manifest_items = load_manifest(manifest)
        except Exception as exc:
            manifest_error = str(exc)
    elif load_templates:
        manifest_error = "服务端没有配置模板清单"

    # 生产清单 = 本机标签工作簿；manifest 只是"哪些视频已上传云端"的索引，
    # 它缺失或与工作簿不同源都不应阻断标签筛选。
    has_workbook = load_templates and tag_workbook_path is not None and Path(tag_workbook_path).is_file()
    if has_workbook:
        try:
            from app.web.templates import TemplateCatalog
            catalog = TemplateCatalog(manifest_items)
            tag_workbook_info = catalog.attach_tag_workbook(tag_workbook_path)
            if manifest_error:
                tag_workbook_info["manifest_warning"] = f"云端模板索引不可用（{manifest_error}），本次仅按本地生产清单筛选"
            templates = catalog.items
        except Exception as exc:
            templates = []
            templates_error = f"标签清单加载失败：{exc}"
    elif load_templates:
        templates = manifest_items
        templates_error = "" if manifest_items else (manifest_error or "服务端没有配置模板清单")
    return WebSettings(base, web_path, api_path, data_dir, cfg, web, templates,
                       templates_error, tag_workbook_path, tag_workbook_info)
