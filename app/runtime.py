#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""无界面运行时构造：配置映射与对象存储初始化。"""
from __future__ import annotations

from app import config as appconfig
from app.storage import StorageConfig, StorageError, open_storage


def _make_storage(cfg: dict, bucket: str, prefix: str):
    manual = {
        "bucket": bucket,
        "region": (cfg.get("COS_REGION") or "").strip(),
        "prefix": prefix,
        "domain": (cfg.get("COS_DOMAIN") or "").strip(),
    }
    sid = (cfg.get("COS_SECRET_ID") or "").strip()
    skey = (cfg.get("COS_SECRET_KEY") or "").strip()
    if sid and skey:
        manual["secret_id"] = sid
        manual["secret_key"] = skey
    resolved = StorageConfig.from_local_profile(**manual)
    if not (resolved.secret_id and resolved.secret_key):
        raise StorageError(
            "启用了 COS，但本机找不到可用凭证。"
            "请设置环境变量 COS_SECRET_ID/COS_SECRET_KEY，或放进 ~/.cos/credentials.json，"
            "或先跑 tccli auth login --profile agent1"
        )
    return open_storage(resolved)


def build_storages(cfg: dict) -> tuple[object | None, object | None]:
    """返回 (模板桶, 成品桶)；关闭 COS 时两者均为 None。"""
    if not appconfig.truthy(cfg.get("COS_ENABLED", "")):
        return None, None
    base_bucket = (cfg.get("COS_BUCKET") or "").strip()
    template_bucket = (cfg.get("COS_TEMPLATE_BUCKET") or "").strip() or base_bucket
    output_bucket = (cfg.get("COS_OUTPUT_BUCKET") or "").strip() or base_bucket
    prefix = (cfg.get("COS_PREFIX") or "").strip()
    template_store = _make_storage(cfg, template_bucket, "") if template_bucket else None
    output_store = _make_storage(cfg, output_bucket, prefix) if output_bucket else None
    return template_store, output_store


def build_storage(cfg: dict):
    """兼容旧调用：只返回成品桶。"""
    return build_storages(cfg)[1]
