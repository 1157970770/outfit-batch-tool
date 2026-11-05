#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「标签」页接口：给视频打标 / 弃用 / 标签库维护。

权限口径（用户已拍定）：**所有登录用户都能写**，不要求 admin；每次写都记审计。
同步口径（用户已拍定）：不做轮询/SSE。前端进页面拉一次快照，保存后用响应里的
`updated` 行局部替换；并发靠 revision 乐观锁，旧值写入一律 409 并回带最新 revision。
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import time
import uuid
from pathlib import Path

from tools.template_store import RevisionConflict, StoreError

MAX_IDS = 2000
MAX_UPLOAD_MB = 50
TEMPLATE_PREFIX = "input/tpl"
CATEGORY_SLUGS = {"景别": "jingbie", "适合季节": "jijie", "场景": "changjing", "姿势": "zishi"}


class ApiError(Exception):
    """业务错误 → HTTP 状态码；extra 会合并进响应体，方便前端直接拿 impact/冲突信息。"""

    def __init__(self, message, status: int = 400, **extra):
        super().__init__(str(message))
        self.status = int(status or 400)
        self.extra = extra

    def payload(self) -> dict:
        return dict(self.extra)


def _store(app):
    store = getattr(app, "tag_store", None)
    if store is None:
        raise ApiError(
            "标签真源尚未启用。请在主机执行：python run_web.py --import-tags", 503,
            enabled=False, source=getattr(app.settings, "tag_source", "none"))
    return store


def _wrap(exc: StoreError) -> ApiError:
    if isinstance(exc, RevisionConflict):
        return ApiError(str(exc), 409, conflict=True, revision=exc.revision)
    return ApiError(str(exc), int(getattr(exc, "status", 400) or 400))


def _rows(app, result: dict) -> list[dict]:
    """回传受影响的行；用 catalog.public_tags() 补齐 ready/upload_state，
    前端据此局部替换，不必重拉整表。"""
    updated = list(result.get("updated") or [])
    if not updated:
        return []
    try:
        index = {row["id"]: row for row in app.catalog.public_tags()}
    except Exception:  # noqa: BLE001
        index = {}
    return [index.get(str(row.get("id") or ""), row) for row in updated]


def _after_write(app, store, result: dict) -> dict:
    """写成功后再重建内存候选集：失败只记错误，不回滚已落盘的写入。"""
    error = ""
    try:
        app.rebuild_catalog()
    except Exception as exc:  # noqa: BLE001 - 候选集重建失败不该让保存看起来失败
        error = str(exc)
    payload = {
        "ok": True,
        "revision": int(result.get("revision") or store.revision),
        "changed": int(result.get("changed") or 0),
        "updated": _rows(app, result),
        "stats": _stats(app, store),
    }
    if "library" in result:
        payload["library"] = {**result["library"], "usage": store.usage()}
    if "affected" in result:
        payload["affected"] = int(result.get("affected") or 0)
    if "becomes_untagged" in result:
        payload["becomes_untagged"] = int(result.get("becomes_untagged") or 0)
    if result.get("skipped"):
        payload["skipped"] = list(result.get("skipped") or [])
    if error:
        payload["catalog_error"] = f"候选集重建失败：{error}"
    return payload


def _stats(app, store) -> dict:
    info = getattr(app.catalog, "tag_workbook_info", {}) or {}
    stats = store.stats()
    stats["ready"] = int(info.get("source_ready_templates") or 0)
    stats["candidates"] = len(getattr(app.catalog, "items", []) or [])
    return stats


def _locked_batches(app, ids) -> list[dict]:
    """弃用前的提醒：这些模板还在未完成批次的清单里，生产时会被标成「模板已不可用」。"""
    wanted = {str(value) for value in ids or []}
    if not wanted:
        return []
    found: list[dict] = []
    batches = getattr(getattr(app, "batches", None), "batches", {}) or {}
    lock = getattr(getattr(app, "batches", None), "mutex", None)
    with (lock if lock is not None else contextlib.nullcontext()):
        for batch in list(batches.values()):
            if batch.get("state") == "closed":
                continue
            pending = [item for item in batch.get("items") or []
                       if item.get("state") in ("pending", "producing")
                       and str(item.get("template_id")) in wanted]
            if pending:
                found.append({"batch_no": str(batch.get("batch_no") or batch.get("id") or ""),
                              "items": len(pending),
                              "state": str(batch.get("state") or "")})
    return found


def _library_affected_ids(store, action: str, data: dict) -> list[str]:
    """库级操作影响的模板 ID。

    merge/delete/rename/set_category 的请求体里只有标签名、没有 ids，而「未完成批次
    还在用这条素材」的提醒要按模板 ID 去批次清单里查，所以得先反查行。合并只关心
    被清掉的源标签（目标标签保留，不算影响）。
    """
    if action == "merge":
        names = {str(data.get("source") or "").strip()}
    elif action in ("delete", "rename", "set_category"):
        names = {str(data.get("name") or "").strip()}
    else:
        return []
    names.discard("")
    if not names:
        return []
    ids: list[str] = []
    for row in store.rows(include_deprecated=True):
        if names & {str(tag) for tag in row.get("tags") or []}:
            template_id = str(row.get("id") or "")
            if template_id:
                ids.append(template_id)
    return ids


def snapshot(app, rev=None) -> dict:
    store = getattr(app, "tag_store", None)
    if store is None:
        return {"ok": True, "enabled": False, "source": getattr(app.settings, "tag_source", "none"),
                "revision": 0, "videos": [], "categories": [],
                "library": {"version": 2, "tags": [], "categories": {}, "usage": {}},
                "stats": {}, "audit": [], "manifest": {},
                "error": getattr(app.settings, "tag_store_error", "")}

    # 桌面打标器可能刚写过：先按 mtime 判断，变了就重载 + 重建候选集（不拉 COS）
    if store.refresh_if_changed():
        try:
            app.rebuild_catalog()
        except Exception:  # noqa: BLE001 - 快照仍要能返回
            pass
    if rev is not None and str(rev).strip().isdigit() and int(str(rev).strip()) == store.revision:
        return {"ok": True, "unchanged": True, "revision": store.revision}

    info = getattr(app.catalog, "tag_workbook_info", {}) or {}
    manifest_items = getattr(app, "manifest_items", []) or []
    return {
        "ok": True,
        "enabled": True,
        "source": "store",
        "schema": int(store.data.get("schema") or 0),
        "revision": store.revision,
        "updated_at": store.updated_at,
        "updated_by": store.updated_by,
        "categories": list(getattr(app.catalog, "tag_categories", []) or []),
        "library": {**store.library(), "usage": store.usage()},
        "videos": app.catalog.public_tags(),
        "stats": _stats(app, store),
        "audit": store.audit_tail(30),
        "manifest": {
            "count": len(manifest_items),
            "matched": int(info.get("source_ready_templates") or 0),
            "pulled_at": float(getattr(app, "manifest_pulled_at", 0) or 0),
            "warning": str(getattr(app, "manifest_warning", "") or ""),
        },
        "path": str(getattr(store, "path", "") or ""),
    }


def _revision_of(data: dict, action: str = "写入") -> int:
    """写接口必须带 revision：不带就等于关掉乐观锁，后写者会静默覆盖先写者。"""
    raw = data.get("revision")
    if raw is None or isinstance(raw, bool):
        raise ApiError(f"{action}需要 revision：请刷新页面拿到最新版本后重试", 400)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ApiError("revision 必须是整数：请刷新页面后重试", 400) from None
    if value < 0:
        raise ApiError("revision 不能为负数：请刷新页面后重试", 400)
    return value


def set_tags(app, data: dict, user: dict) -> dict:
    store = _store(app)
    try:
        result = store.set_tags(data.get("ids"), data.get("tags"),
                                mode=str(data.get("mode") or "replace"),
                                user=str(user.get("username") or ""),
                                revision=_revision_of(data, "改标签"))
    except StoreError as exc:
        raise _wrap(exc) from None
    return _after_write(app, store, result)


def deprecate(app, data: dict, user: dict) -> dict:
    store = _store(app)
    try:
        result = store.deprecate(data.get("ids"), bool(data.get("deprecated", True)),
                                 user=str(user.get("username") or ""),
                                 revision=_revision_of(data, "弃用/恢复"),
                                 reason=str(data.get("reason") or ""))
    except StoreError as exc:
        raise _wrap(exc) from None
    return _after_write(app, store, result)


def set_rating(app, data: dict, user: dict) -> dict:
    """给视频打星（1-5，0 清除）；走同一套 revision 乐观锁与审计。"""
    store = _store(app)
    try:
        result = store.set_rating(data.get("ids"), data.get("rating"),
                                  user=str(user.get("username") or ""),
                                  revision=_revision_of(data, "打分"))
    except StoreError as exc:
        raise _wrap(exc) from None
    return _after_write(app, store, result)


_LIBRARY_PARAMS = {
    "create": ("name", "category"),
    "rename": ("name", "new_name"),
    "set_category": ("name", "category"),
    "merge": ("source", "target"),
    "delete": ("name",),
    "category_create": ("name",),
    "category_rename": ("name", "new_name"),
    "category_delete": ("name",),
}


def library(app, data: dict, user: dict) -> dict:
    store = _store(app)
    action = str(data.get("action") or "").strip()
    if action not in _LIBRARY_PARAMS:
        raise ApiError("不支持的标签库操作")
    revision = _revision_of(data, "标签库操作")
    params = {key: data.get(key) for key in _LIBRARY_PARAMS[action] if key in data}
    # 合并/删除/改名会级联改写视频行：必须先让用户看到影响面再确认
    impact = {}
    if action in ("merge", "delete", "rename"):
        try:
            impact = store.impact_preview(action, params)
        except StoreError as exc:
            raise _wrap(exc) from None
        if int(impact.get("affected") or 0) > 0 and data.get("confirm") is not True:
            raise ApiError(
                f"该操作将影响 {impact['affected']} 条视频，请确认后再提交",
                400, impact=impact, confirm_required=True)
    username = str(user.get("username") or "")
    try:
        if action == "create":
            result = store.library_create(params.get("name"), params.get("category") or "",
                                          user=username, revision=revision)
        elif action == "rename":
            result = store.library_rename(params.get("name"), params.get("new_name") or "",
                                          user=username, revision=revision)
        elif action == "set_category":
            result = store.library_set_category(params.get("name"), params.get("category") or "",
                                                user=username, revision=revision)
        elif action == "merge":
            result = store.library_merge(params.get("source"), params.get("target") or "",
                                         user=username, revision=revision)
        elif action == "category_create":
            result = store.category_create(params.get("name") or "", user=username,
                                           revision=revision)
        elif action == "category_rename":
            result = store.category_rename(params.get("name") or "", params.get("new_name") or "",
                                           user=username, revision=revision)
        elif action == "category_delete":
            result = store.category_delete(params.get("name") or "", user=username,
                                           revision=revision)
        else:
            result = store.library_delete(params.get("name"), user=username,
                                          revision=revision)
    except StoreError as exc:
        raise _wrap(exc) from None
    payload = _after_write(app, store, result)
    payload["action"] = action
    return payload


def impact(app, data: dict, user: dict) -> dict:
    store = _store(app)
    action = str(data.get("action") or "").strip()
    try:
        preview = store.impact_preview(action, data)
    except StoreError as exc:
        raise _wrap(exc) from None
    if action == "deprecate":
        preview["locked_batches"] = _locked_batches(app, data.get("ids") or [])
    elif action in ("merge", "delete", "rename", "set_category"):
        # 库级操作没有 ids：按标签反查受影响的行，才能提示「还有批次在等这条素材」。
        preview["locked_batches"] = _locked_batches(
            app, _library_affected_ids(store, action, data))
    preview["ok"] = True
    preview["checked_at"] = time.time()
    return preview


# ---------------------------------------------------------------------- 上传新视频
def _overlay_path(app) -> Path:
    configured = getattr(app.settings, "manifest_overlay_path", None)
    return Path(configured) if configured else Path(app.settings.data_dir) / "manifest_overlay.json"


def _append_overlay(app, entry: dict) -> None:
    """把网页上传的对象登记进本地 overlay：重启后仍能 join 出 key/url。"""
    path = _overlay_path(app)
    lock = getattr(app, "overlay_lock", None)
    with (lock if lock is not None else contextlib.nullcontext()):
        entries = [dict(item) for item in (getattr(app, "manifest_overlay", []) or [])]
        marker = entry.get("key") or entry.get("url")
        entries = [item for item in entries if (item.get("key") or item.get("url")) != marker]
        entries.append(entry)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps({"version": 1, "entries": entries}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        app.manifest_overlay = entries


def _tagset(labels: list[str], categories: dict, template_id: str) -> list[dict]:
    tagset = [{"Key": "kind", "Value": "tpl"}, {"Key": "tpl_id", "Value": str(template_id)}]
    grouped: dict[str, list[str]] = {}
    order: list[str] = []
    for label in labels:
        category = categories.get(label) or "场景"
        if category not in grouped:
            grouped[category] = []
            order.append(category)
        grouped[category].append(label)
    for index, category in enumerate(order):
        key = CATEGORY_SLUGS.get(category) or f"cat{index + 1}"
        tagset.append({"Key": key[:16], "Value": "/".join(grouped[category])[:120]})
    return tagset[:10]  # COS 单对象标签上限 10 个


def upload_video(app, temp_path: str | Path, filename: str, tags, user: dict,
                 auto_number: bool = False) -> dict:
    """一次一条：校验 → 传 COS → 登记真源 → 写 overlay → 重建候选集。

    文件先传成功才写清单；清单写失败会把刚传的对象删掉，避免桶里留孤儿。
    """
    store = _store(app)
    labels = [str(value).strip() for value in (tags or []) if str(value).strip()]
    if not labels:
        raise ApiError("请至少选择一个标签", 400)
    known = set(store.library()["tags"])
    unknown = [label for label in labels if label not in known]
    if unknown:
        raise ApiError(f"标签不存在：{unknown[0]}", 400)

    try:
        storage = app.template_store()
    except Exception as exc:  # noqa: BLE001 - 凭证缺失/桶不可用统一按 503 回
        raise ApiError(f"云端存储不可用：{exc}", 503) from None
    if storage is None:
        raise ApiError("云端模板库未启用（COS 未配置），无法上传视频", 503)

    from app.media import validate_video
    src = Path(temp_path)
    if src.stat().st_size > MAX_UPLOAD_MB * 1024 * 1024:
        raise ApiError(f"单个视频不能超过 {MAX_UPLOAD_MB}MB", 413)
    try:
        info = validate_video(src)
    except Exception as exc:  # noqa: BLE001 - MediaError 与 ffprobe 异常都按 400 回
        raise ApiError(str(exc), 400) from None

    from app.storage import file_fingerprint
    size_mb = round(src.stat().st_size / 1024 / 1024, 3)
    duration = info.get("duration")
    fingerprint = file_fingerprint(src)[:16]
    suffix = Path(str(filename)).suffix.lower() or ".mp4"
    stem = re.sub(r"[^\w\u4e00-\u9fa5-]+", "-", Path(str(filename)).stem)[:40] or "video"
    key = f"{TEMPLATE_PREFIX}/{stem}-{uuid.uuid4().hex[:8]}{suffix}"

    try:
        storage.put_path(src, key, content_type="video/mp4")
    except Exception as exc:  # noqa: BLE001
        raise ApiError(f"上传到云端失败：{exc}", 502) from None

    try:
        result = store.add_video(name=str(filename), tags=labels, size_mb=size_mb,
                                 size_bytes=src.stat().st_size, duration=duration,
                                 fingerprint=fingerprint, uploaded=True,
                                 source_dir="网页上传", auto_number=bool(auto_number),
                                 user=str(user.get("username") or ""))
    except StoreError as exc:
        try:
            storage.delete(key)
        except Exception:  # noqa: BLE001
            pass
        raise _wrap(exc) from None

    row = result.get("row") or {}
    try:
        storage.tag_object(key, {item["Key"]: item["Value"] for item in
                                 _tagset(labels, store.categories(), str(row.get("id") or ""))})
    except Exception:  # noqa: BLE001 - 标签权限缺失不影响上传本身
        pass
    try:
        _append_overlay(app, {
            "id": str(row.get("id") or ""), "name": str(row.get("name") or filename),
            "filename": str(row.get("name") or filename),
            "relative_path": str(row.get("name") or filename),
            "size_mb": size_mb, "fingerprint": fingerprint, "duration": duration,
            "key": key, "url": "", "source": "web-upload",
        })
    except Exception as exc:  # noqa: BLE001 - 清单已写入，索引失败只提示
        payload = _after_write(app, store, result)
        payload["warning"] = f"视频已上传，但索引写入失败：{exc}。请在管理页「重新加载视频场景」后重试。"
        payload["row"] = row
        return payload

    payload = _after_write(app, store, result)
    payload["row"] = row
    payload["renamed"] = bool(result.get("renamed"))
    payload["auto_number"] = bool(auto_number)
    payload["key"] = key
    return payload


def versions(app, limit: int = 100) -> dict:
    """版本列表：整份真源快照（视频库 + 标签），自动 50 份 + 手动 50 份。"""
    store = _store(app)
    return {"ok": True, "revision": store.revision,
            "versions": store.list_versions(limit),
            "auto_keep": 50, "pinned_keep": 50,
            "path": str(store.versions_dir())}


def create_version(app, data: dict, user: dict) -> dict:
    store = _store(app)
    note = str(data.get("note") or "").strip()
    try:
        meta = store.create_version(note, user=str(user.get("username") or ""))
    except StoreError as exc:
        raise _wrap(exc) from None
    return {"ok": True, "version": meta, "revision": store.revision}


def restore_version(app, data: dict, user: dict) -> dict:
    """回滚整份真源到指定版本；回滚前服务端会自动留一份「回滚前备份」。"""
    store = _store(app)
    name = str(data.get("name") or "").strip()
    if not name:
        raise ApiError("请指定要回滚的版本")
    revision = _revision_of(data, "回滚版本")
    try:
        result = store.restore_version(name, user=str(user.get("username") or ""),
                                       revision=revision)
    except StoreError as exc:
        raise _wrap(exc) from None
    payload = _after_write(app, store, result)
    payload["restored"] = int(result.get("restored") or 0)
    payload["added_since"] = int(result.get("added_since") or 0)
    payload["removed_since"] = int(result.get("removed_since") or 0)
    payload["backup"] = str(result.get("backup") or "")
    payload["version"] = name
    return payload
