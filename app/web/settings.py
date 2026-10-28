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
    templates: list[dict] = []
    templates_error = ""
    manifest = cfg.get("MANIFEST", "")
    if load_templates and manifest:
        try:
            from app.core import load_manifest
            templates = load_manifest(manifest)
        except Exception as exc:
            templates_error = str(exc)
    elif load_templates:
        templates_error = "服务端没有配置模板清单"
    return WebSettings(base, web_path, api_path, data_dir, cfg, web, templates, templates_error)
