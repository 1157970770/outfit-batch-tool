#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「标签」页接口：给视频打标 / 弃用 / 标签库维护。

权限口径（用户已拍定）：**所有登录用户都能写**，不要求 admin；每次写都记审计。
同步口径（用户已拍定）：不做轮询/SSE。前端进页面拉一次快照，保存后用响应里的
`updated` 行局部替换；并发靠 revision 乐观锁，旧值写入一律 409 并回带最新 revision。
"""

from __future__ import annotations

import contextlib
import time

from tools.template_store import RevisionConflict, StoreError

MAX_IDS = 2000


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


def set_tags(app, data: dict, user: dict) -> dict:
    store = _store(app)
    try:
        result = store.set_tags(data.get("ids"), data.get("tags"),
                                mode=str(data.get("mode") or "replace"),
                                user=str(user.get("username") or ""),
                                revision=data.get("revision"))
    except StoreError as exc:
        raise _wrap(exc) from None
    return _after_write(app, store, result)


def deprecate(app, data: dict, user: dict) -> dict:
    store = _store(app)
    try:
        result = store.deprecate(data.get("ids"), bool(data.get("deprecated", True)),
                                 user=str(user.get("username") or ""),
                                 revision=data.get("revision"),
                                 reason=str(data.get("reason") or ""))
    except StoreError as exc:
        raise _wrap(exc) from None
    return _after_write(app, store, result)


_LIBRARY_PARAMS = {
    "create": ("name", "category"),
    "rename": ("name", "new_name"),
    "set_category": ("name", "category"),
    "merge": ("source", "target"),
    "delete": ("name",),
}


def library(app, data: dict, user: dict) -> dict:
    store = _store(app)
    action = str(data.get("action") or "").strip()
    if action not in _LIBRARY_PARAMS:
        raise ApiError("不支持的标签库操作")
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
                                          user=username, revision=data.get("revision"))
        elif action == "rename":
            result = store.library_rename(params.get("name"), params.get("new_name") or "",
                                          user=username, revision=data.get("revision"))
        elif action == "set_category":
            result = store.library_set_category(params.get("name"), params.get("category") or "",
                                                user=username, revision=data.get("revision"))
        elif action == "merge":
            result = store.library_merge(params.get("source"), params.get("target") or "",
                                         user=username, revision=data.get("revision"))
        else:
            result = store.library_delete(params.get("name"), user=username,
                                          revision=data.get("revision"))
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
    preview["ok"] = True
    preview["checked_at"] = time.time()
    return preview
