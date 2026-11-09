#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网页服务配置；密钥只从主机上的 server/config.json 读取。"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from app import config as appconfig

# 单 job 线程上限（与 wan_api.SUBMIT_MAX_CONCURRENCY 一致）：配置写更大也不放大。
# 2026-11-02 从 50 提到 100，与「大批量」单次 100 条档对齐，避免单 job 被线程数卡住。
MAX_CONCURRENCY_CAP = 100
# 同时在跑的任务数上限（跨账户）。每账户自己的额度由 Key 分桶控制，这里只是主机级闸门。
MAX_ACTIVE_JOBS_CAP = 20
# 批次清单上限与单次生产档位上限（v2：只允许 20/50/100 三档）。
MAX_BATCH_ITEMS_CAP = 500
MAX_BATCH_RUN_ITEMS_CAP = 100
# price_per_second 为 null 时的兜底单价（元/计费秒），来自阿里云百炼控制台
# wan3.0-video（万相 3.0 视频）公开定价页：480P 0.3 / 720P 0.6 / 1080P 1.2。
# 这是公开价目表、不是密钥；**仅用于给用户看预估金额，实际以账单为准**。
# 配置里有值时一律以配置为准（unit_price 返回 source="config"）。
DEFAULT_PRICE_PER_SECOND = {"480P": 0.3, "720P": 0.6, "1080P": 1.2}



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
    # ---- 标签真源（web-data/template_tags.json，网页与桌面打标器共用）----
    tag_store_path: Path | None = None
    tag_store: object | None = None          # tools.template_store.TemplateStore
    tag_store_error: str = ""
    tag_source: str = "none"                 # "store" | "xlsx" | "none"
    manifest_items: list[dict] = field(default_factory=list)
    manifest_overlay: list[dict] = field(default_factory=list)
    manifest_overlay_path: Path | None = None
    manifest_warning: str = ""
    manifest_pulled_at: float = 0.0
    # 开发实例标记（web.json 里 dev_mode: true，只有设计区/开发副本会开）：
    # 页面据此显示「开发版」徽标，生产不显示。
    dev_mode: bool = False
    # 生图 tab 配置（server/imagegen.json，可选文件；缺省时该 tab 显示「未配置」）
    imagegen: dict = field(default_factory=dict)
    imagegen_file: Path | None = None
    imagegen_error: str = ""

    @property
    def users(self) -> dict:
        users = self.web.get("users")
        return users if isinstance(users, dict) else {}

    @property
    def max_active_jobs(self) -> int:
        return max(1, min(MAX_ACTIVE_JOBS_CAP, int(self.web.get("max_active_jobs", 2))))

    @property
    def max_active_per_user(self) -> int:
        """每账户同时跑几个「大批量」job；小批量走快速通道，不受这个限制。"""
        try:
            value = int(self.web.get("max_active_per_user", 1))
        except (TypeError, ValueError):
            value = 1
        return max(1, min(MAX_ACTIVE_JOBS_CAP, value))

    @property
    def small_lane_items(self) -> int:
        """条数 ≤ 该值的 job 走快速通道，不跟大批量抢 job 槽位与在飞名额。

        上限钳死 3：配大它就静默突破「快速通道 ≤3 条」的契约
        （12 条的批次被当小任务插队，大批量全饿死）。要调并发改 max_active_small_jobs。
        """
        try:
            value = int(self.web.get("small_lane_items", 3))
        except (TypeError, ValueError):
            value = 3
        return max(1, min(3, value))

    @property
    def max_active_small_jobs(self) -> int:
        """快速通道同时跑几个小 job（全站）。"""
        try:
            value = int(self.web.get("max_active_small_jobs", 4))
        except (TypeError, ValueError):
            value = 4
        return max(1, min(MAX_ACTIVE_JOBS_CAP, value))

    @property
    def small_lane_inflight(self) -> int:
        """预留给快速通道的在飞名额，独立于大批量的在飞池。"""
        try:
            value = int(self.web.get("small_lane_inflight", 12))
        except (TypeError, ValueError):
            value = 12
        return max(1, min(50, value))

    @property
    def max_items(self) -> int:
        return max(1, min(100, int(self.web.get("max_job_items", 20))))

    @property
    def max_concurrency(self) -> int:
        """单 job 工作线程数；读 WAN_MAX_CONCURRENCY，钳在 1–50。"""
        try:
            value = int(self.config.get("WAN_MAX_CONCURRENCY") or 3)
        except (TypeError, ValueError):
            value = 3
        return max(1, min(MAX_CONCURRENCY_CAP, value))

    @property
    def max_batch_items(self) -> int:
        """一个批次锁定清单的条目上限。"""
        try:
            value = int(self.web.get("max_batch_items", MAX_BATCH_ITEMS_CAP))
        except (TypeError, ValueError):
            value = MAX_BATCH_ITEMS_CAP
        return max(1, min(MAX_BATCH_ITEMS_CAP, value))

    @property
    def max_batch_run_items(self) -> int:
        """批次单次生产上限（20/50/100 三档里的最大可用档）。"""
        try:
            value = int(self.web.get("max_batch_run_items", MAX_BATCH_RUN_ITEMS_CAP))
        except (TypeError, ValueError):
            value = MAX_BATCH_RUN_ITEMS_CAP
        return max(1, min(MAX_BATCH_RUN_ITEMS_CAP, value))

    def unit_price(self, resolution: str) -> tuple[float | None, str]:
        """返回 (元/秒, 来源)。config 有值就用 config；null 时用兜底常量并标注 default。"""
        key = str(resolution or "").strip().upper()
        raw = self.config.get("WAN_PRICE_PER_SECOND")
        value = raw.get(key) if isinstance(raw, dict) else None
        if value not in (None, ""):
            try:
                return float(value), "config"
            except (TypeError, ValueError):
                pass
        fallback = DEFAULT_PRICE_PER_SECOND.get(key)
        return (fallback, "default") if fallback else (None, "missing")

    def price_map(self) -> dict[str, float]:
        """传给引擎的单价表。

        引擎的 estimate_cost 只认传入的映射、自己不兜底；以前直接把
        config 里全 null 的 price_per_second 传下去，导致每条记录的
        estimated_cost 都是 None，账户花费统计永远是 0。这里统一兜底。
        """
        prices: dict[str, float] = {}
        for key in DEFAULT_PRICE_PER_SECOND:
            value, _source = self.unit_price(key)
            if value:
                prices[key] = float(value)
        return prices



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


def _read_optional_json(path: Path) -> tuple[dict, str]:
    """可选配置文件：不存在不算错（生图 tab 就是「未配置」），坏文件只记录错误。"""
    if not path.is_file():
        return {}, ""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, f"{path.name} 读取失败：{exc}"
    if not isinstance(data, dict):
        return {}, f"{path.name} 顶层应为 JSON 对象"
    return data, ""


def resolve_tag_store_path(web: dict, base: Path, data_dir: Path, *,
                           use_default: bool = True) -> Path | None:
    """标签真源路径：OUTFIT_TAG_STORE → web.json → data_root/template_tags.json。

    网页服务与 `--import-tags` 必须用同一套规则，否则迁移到 A 文件、服务读 B 文件。
    """
    raw = os.getenv("OUTFIT_TAG_STORE") or web.get("tag_store")
    if raw:
        path = Path(str(raw)).expanduser()
        return path if path.is_absolute() else Path(base) / path
    return (Path(data_dir) / "template_tags.json") if use_default else None


def _read_overlay(path: Path) -> list[dict]:
    """网页上传的视频不在云端 manifest 里，用本地 overlay 补 url/key（重启不丢）。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return []
    entries = raw.get("entries") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        return []
    return [dict(item) for item in entries if isinstance(item, dict) and (item.get("key") or item.get("url"))]


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
    # 生图 tab 单独一份配置：不把老张的 Key 混进含 DashScope/COS 密钥的 config.json
    imagegen_path = Path(os.getenv("OUTFIT_IMAGEGEN_CONFIG") or base / "server" / "imagegen.json")
    imagegen_cfg, imagegen_error = _read_optional_json(imagegen_path)
    if not web.get("session_secret") or not isinstance(web.get("users"), dict) or not web["users"]:
        raise RuntimeError("网页账号尚未初始化。请在主机运行：python3 run_web.py --init")
    data_dir = Path(web.get("data_root") or "web-data")
    if not data_dir.is_absolute():
        data_dir = base / data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    output_dir = data_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    cfg = appconfig.from_remote(remote, output_dir)
    # 全站只有一把 Key：缺了就直接启动失败，不要等到用户点生产才报错。
    if not appconfig.wan_configured(cfg):
        raise RuntimeError(f"{api_path} 中没有可用的 wan.api_key/model 配置")

    tag_source = os.getenv("OUTFIT_TAG_WORKBOOK") or web.get("tag_workbook")
    if tag_source:
        tag_workbook_path = Path(str(tag_source)).expanduser()
        if not tag_workbook_path.is_absolute():
            tag_workbook_path = base / tag_workbook_path
    elif use_runtime_defaults:
        tag_workbook_path = Path.home() / "Desktop" / "新标签清单.xlsx"
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

    # 网页上传的模板不在云端 manifest 里：用本地 overlay 合并，重启后仍能 join 出地址
    overlay_path = data_dir / "manifest_overlay.json"
    overlay_items = _read_overlay(overlay_path) if load_templates else []
    if overlay_items:
        seen = {str(item.get("key") or "") or str(item.get("url") or "") for item in manifest_items}
        for item in overlay_items:
            marker = str(item.get("key") or "") or str(item.get("url") or "")
            if marker and marker in seen:
                continue
            manifest_items.append(item)
            if marker:
                seen.add(marker)

    # 模板集来源优先级：服务端标签真源（store）> 本机标签工作簿（xlsx，回滚通道）> 纯 manifest。
    # manifest 始终只是"哪些视频已上传云端"的索引，用来补 url/key，不决定标签。
    tag_store_path = (resolve_tag_store_path(web, base, data_dir, use_default=use_runtime_defaults)
                      if load_templates else None)
    tag_store = None
    tag_source = "none"
    tag_store_error = ""
    if load_templates and tag_store_path is not None and Path(tag_store_path).is_file():
        try:
            from tools.template_store import TemplateStore
            from app.web.templates import TemplateCatalog
            tag_store = TemplateStore.load(tag_store_path)
            catalog = TemplateCatalog(manifest_items)
            tag_workbook_info = catalog.attach_tag_store(tag_store)
            if manifest_error:
                tag_workbook_info["manifest_warning"] = f"云端模板索引不可用（{manifest_error}），本次仅按本地生产清单筛选"
            templates = catalog.items
            tag_source = "store"
        except Exception as exc:
            tag_store = None
            tag_store_error = f"标签真源加载失败：{exc}"

    has_workbook = load_templates and tag_workbook_path is not None and Path(tag_workbook_path).is_file()
    if tag_store is None and has_workbook:
        try:
            from app.web.templates import TemplateCatalog
            catalog = TemplateCatalog(manifest_items)
            tag_workbook_info = catalog.attach_tag_workbook(tag_workbook_path)
            if manifest_error:
                tag_workbook_info["manifest_warning"] = f"云端模板索引不可用（{manifest_error}），本次仅按本地生产清单筛选"
            templates = catalog.items
            tag_source = "xlsx"
        except Exception as exc:
            templates = []
            templates_error = f"标签清单加载失败：{exc}"
    elif tag_store is None and load_templates:
        templates = manifest_items
        templates_error = "" if manifest_items else (manifest_error or "服务端没有配置模板清单")
    if tag_source == "store" and tag_store_error:
        templates_error = ""  # 已回退到有效来源，不把旧错误顶到前台
    return WebSettings(base, web_path, api_path, data_dir, cfg, web, templates,
                       templates_error, tag_workbook_path, tag_workbook_info,
                       tag_store_path=tag_store_path, tag_store=tag_store,
                       tag_store_error=tag_store_error, tag_source=tag_source,
                       manifest_items=manifest_items,
                       manifest_overlay=overlay_items,
                       manifest_overlay_path=overlay_path,
                       manifest_warning=(f"云端模板索引不可用（{manifest_error}）" if manifest_error else ""),
                       manifest_pulled_at=0.0,
                       dev_mode=appconfig.truthy(web.get("dev_mode")),
                       imagegen=imagegen_cfg,
                       imagegen_file=imagegen_path,
                       imagegen_error=imagegen_error)
