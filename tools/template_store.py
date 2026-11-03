#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""视频标签唯一真源（JSON）：网页服务与桌面打标器共用本模块。

设计约定：
- 只依赖标准库 + `tools.template_catalog`（xlsx 读写与标签校验），不 import `app.*`、不碰 PySide6。
- 写入一律走 `_mutate()`：进程内 RLock → 跨进程文件锁 → **锁内重读** → 比对 revision → 改内存
  → revision+1 → 审计 → 原子写（tmp + os.replace）。后写者拿旧 revision 会被拒（409）。
- 「标签为空」与「deprecated」是两件事：前者是未打标素材（历史口径：不进候选集），
  后者是网页/桌面显式弃用（可恢复原标签）。两者都不进候选集，但语义与可恢复性不同。
- 不存 url/key：云端地址永远由 TemplateCatalog 按「文件名 / 相对路径 / 指纹 + 大小一致」现场 join，
  保持 templates.py 文件头那条硬约定（绝不按模板 ID 匹配）。
"""

from __future__ import annotations

import copy
import gzip
import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from tools.template_catalog import (
    TAG_CATEGORIES,
    CatalogError,
    load_xlsx,
    normalize_tag_categories,
    save_xlsx,
    validate_tag_name,
)

SCHEMA_VERSION = 3
AUDIT_LIMIT = 500
MAX_BULK_IDS = 2000
MODES = ("replace", "add", "remove")
LIBRARY_ACTIONS = ("create", "rename", "set_category", "merge", "delete")

# 版本快照：整份真源（视频库 + 标签 + 弃用态）压成 gzip，单版约 50–80KB。
VERSION_DIRNAME = "tag_versions"
VERSION_SCHEMA = 1
AUTO_KEEP = 50          # 自动版本只留最近 50 份
PINNED_KEEP = 50        # 手动/回滚前备份最多留 50 份
VERSION_NAME = re.compile(r"^v(\d{6})-(\d+)-(auto|manual)-([0-9a-f]{4})\.json\.gz$")

_DIGITS = re.compile(r"^\d+$")


class StoreError(ValueError):
    """参数/语义错误，映射 HTTP 400。"""

    status = 400


class NotFound(StoreError):
    status = 404


class RevisionConflict(StoreError):
    """乐观锁失败：调用方基于旧 revision 写入，映射 HTTP 409。"""

    status = 409

    def __init__(self, message: str, revision: int):
        super().__init__(message)
        self.revision = revision


class StoreUnavailable(StoreError):
    """文件损坏、写入失败或锁超时，映射 HTTP 503。"""

    status = 503


def default_store_path() -> Path:
    """OUTFIT_TAG_STORE 优先；否则仓库下的 web-data/template_tags.json。"""
    override = (os.getenv("OUTFIT_TAG_STORE") or "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "web-data" / "template_tags.json"


def normalize_id(value) -> str:
    """模板 ID 规范化：纯数字补零到 3 位，其余原样（与 templates._normalize_id 一致）。"""
    text = str(value or "").strip()
    return text.zfill(3) if text.isdigit() else text


def _stat_of(path: Path) -> tuple[int, int] | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return (int(info.st_mtime_ns), int(info.st_size))


class _FileLock:
    """跨进程互斥：网页服务（多线程）与桌面打标器（另一进程）共写一个 JSON。

    Windows 用 msvcrt 锁首字节，POSIX 用 flock；锁文件与真源同目录，随进程退出自动释放。
    """

    def __init__(self, target: Path, timeout: float = 5.0):
        self.path = Path(str(target) + ".lock")
        self.timeout = timeout
        self._fd = -1

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o666)
        deadline = time.time() + self.timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(self._fd, 0, os.SEEK_SET)
                    msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.time() > deadline:
                    os.close(self._fd)
                    self._fd = -1
                    raise StoreUnavailable("标签真源正在被其他程序写入，请稍后重试") from None
                time.sleep(0.05)

    def __exit__(self, *exc):
        if self._fd < 0:
            return False
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            os.close(self._fd)
            self._fd = -1
        return False


def _clean_tags(values) -> list[str]:
    if isinstance(values, str):
        values = values.split(";")
    if not isinstance(values, (list, tuple, set)):
        return []
    out: list[str] = []
    for value in values:
        label = str(value).strip()
        if label and label not in out:
            out.append(label)
    return out


def _clean_ids(values) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple, set)):
        raise StoreError("请提供要操作的视频编号列表")
    out: list[str] = []
    for value in values:
        template_id = normalize_id(value)
        if template_id and template_id not in out:
            out.append(template_id)
    if not out:
        raise StoreError("请提供要操作的视频编号列表")
    if len(out) > MAX_BULK_IDS:
        raise StoreError(f"单次最多操作 {MAX_BULK_IDS} 条视频")
    return out


def _empty_data() -> dict:
    return {
        "schema": SCHEMA_VERSION,
        "revision": 0,
        "updated_at": 0.0,
        "updated_by": "",
        "library": {"version": 2, "tags": [], "categories": {}},
        "project": {"project_id": "", "source_root": "", "roots": [], "next_id": 1},
        "videos": [],
        "audit": [],
        "audit_dropped": 0,
    }


def _normalize_video(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise StoreUnavailable("标签真源里的视频条目格式错误")
    template_id = normalize_id(raw.get("id"))
    if not template_id:
        raise StoreUnavailable("标签真源里存在没有模板ID的视频条目")
    return {
        "id": template_id,
        "relative_path": str(raw.get("relative_path") or "").replace("\\", "/"),
        "name": str(raw.get("name") or ""),
        "duration": raw.get("duration"),
        "size_mb": raw.get("size_mb"),
        "size_bytes": raw.get("size_bytes"),
        "fingerprint": str(raw.get("fingerprint") or "").strip().lower(),
        "tags": _clean_tags(raw.get("tags")),
        "deprecated": bool(raw.get("deprecated")),
        "deprecated_at": float(raw.get("deprecated_at") or 0),
        "deprecated_by": str(raw.get("deprecated_by") or ""),
        "deprecated_reason": str(raw.get("deprecated_reason") or ""),
        "missing": bool(raw.get("missing")),
        "changed": bool(raw.get("changed")),
        "previous_fingerprint": str(raw.get("previous_fingerprint") or ""),
        "uploaded": bool(raw.get("uploaded")),
        "uploaded_at": float(raw.get("uploaded_at") or 0),
        "source_dir": str(raw.get("source_dir") or ""),
        "original_name": str(raw.get("original_name") or ""),
        "touched_at": float(raw.get("touched_at") or 0),
        "touched_by": str(raw.get("touched_by") or ""),
    }


def _normalize_data(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise StoreUnavailable("标签真源格式错误：顶层应为 JSON 对象")
    try:
        schema = int(raw.get("schema") or SCHEMA_VERSION)
    except (TypeError, ValueError):
        raise StoreUnavailable("标签真源格式错误：schema 不是整数") from None
    if schema > SCHEMA_VERSION:
        raise StoreUnavailable(f"标签真源版本过新（schema {schema}），请升级程序后再打开")

    library_raw = raw.get("library") if isinstance(raw.get("library"), dict) else {}
    tags: list[str] = []
    for value in library_raw.get("tags") or []:
        try:
            name = validate_tag_name(value)
        except CatalogError:
            continue
        if name.casefold() not in {tag.casefold() for tag in tags}:
            tags.append(name)
    categories = normalize_tag_categories(tags, library_raw.get("categories") or {})

    videos: list[dict] = []
    seen: set[str] = set()
    for item in raw.get("videos") or []:
        video = _normalize_video(item)
        if video["id"] in seen:
            raise StoreUnavailable(f"标签真源里模板ID重复：{video['id']}")
        seen.add(video["id"])
        # 字典里没有的标签不丢（可能是别人刚加的），但补齐分类，避免前端拿不到分类
        for tag in video["tags"]:
            if tag not in categories:
                tags.append(tag)
                categories[tag] = normalize_tag_categories([tag]).get(tag, "场景")
        videos.append(video)
    videos.sort(key=lambda row: (not row["id"].isdigit(), int(row["id"]) if row["id"].isdigit() else 0, row["id"]))

    project_raw = raw.get("project") if isinstance(raw.get("project"), dict) else {}
    project = {
        "project_id": str(project_raw.get("project_id") or ""),
        "source_root": str(project_raw.get("source_root") or ""),
        "roots": [str(value) for value in (project_raw.get("roots") or []) if str(value)],
        "next_id": int(project_raw.get("next_id") or (max((int(v["id"]) for v in videos if v["id"].isdigit()), default=0) + 1)),
    }
    audit = [entry for entry in (raw.get("audit") or []) if isinstance(entry, dict)][-AUDIT_LIMIT:]
    return {
        "schema": SCHEMA_VERSION,
        "revision": int(raw.get("revision") or 0),
        "updated_at": float(raw.get("updated_at") or 0),
        "updated_by": str(raw.get("updated_by") or ""),
        "library": {"version": 2, "tags": tags, "categories": categories},
        "project": project,
        "videos": videos,
        "audit": audit,
        "audit_dropped": int(raw.get("audit_dropped") or 0),
        "migrated_from": raw.get("migrated_from") if isinstance(raw.get("migrated_from"), dict) else None,
    }


class TemplateStore:
    """标签真源的读写门面。所有写操作都带 revision 乐观锁。"""

    def __init__(self, path: str | Path, data: dict, stat: tuple[int, int] | None = None):
        self.path = Path(path)
        self.data = data
        self._stat = stat
        self._lock = threading.RLock()
        self._index_locked()

    # ------------------------------------------------------------------ 装载
    @classmethod
    def load(cls, path: str | Path | None = None, *, create_empty: bool = False) -> "TemplateStore":
        target = Path(path) if path else default_store_path()
        if not target.exists():
            if not create_empty:
                raise FileNotFoundError(str(target))
            return cls(target, _empty_data(), None)
        try:
            raw = json.loads(target.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StoreUnavailable(
                f"标签真源文件损坏：{target.name}（{exc}）。可把 {target.name}.bak 改回原名后重试") from None
        return cls(target, _normalize_data(raw), _stat_of(target))

    @classmethod
    def empty(cls, path: str | Path | None = None) -> "TemplateStore":
        return cls(Path(path) if path else default_store_path(), _empty_data(), None)

    def _index_locked(self) -> None:
        self._by_id = {row["id"]: row for row in self.data["videos"]}

    def refresh_if_changed(self) -> bool:
        """别的进程写过就重读；返回是否真的重载了。"""
        with self._lock:
            if _stat_of(self.path) == self._stat:
                return False
            if not self.path.exists():
                return False
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
                data = _normalize_data(raw)
            except (OSError, json.JSONDecodeError, StoreUnavailable):
                return False
            self.data = data
            self._stat = _stat_of(self.path)
            self._index_locked()
            return True

    def _reload_locked(self) -> None:
        """锁内重读：保证看到别的进程最后一次写入。文件不存在时保留内存现状。"""
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
            data = _normalize_data(raw)
        except (OSError, json.JSONDecodeError) as exc:
            raise StoreUnavailable(f"标签真源读取失败：{exc}") from None
        self.data = data
        self._stat = _stat_of(self.path)
        self._index_locked()

    # ------------------------------------------------------------------ 属性
    @property
    def revision(self) -> int:
        return int(self.data.get("revision") or 0)

    @property
    def updated_at(self) -> float:
        return float(self.data.get("updated_at") or 0)

    @property
    def updated_by(self) -> str:
        return str(self.data.get("updated_by") or "")

    def library(self) -> dict:
        library = self.data["library"]
        return {"version": 2, "tags": list(library["tags"]), "categories": dict(library["categories"])}

    def categories(self) -> dict[str, str]:
        return dict(self.data["library"]["categories"])

    def rows(self, *, include_deprecated: bool = True) -> list[dict]:
        with self._lock:
            rows = [copy.deepcopy(row) for row in self.data["videos"]
                    if include_deprecated or not row["deprecated"]]
        return rows

    def row(self, template_id: str) -> dict | None:
        with self._lock:
            row = self._by_id.get(normalize_id(template_id))
            return copy.deepcopy(row) if row else None

    def usage(self) -> dict[str, int]:
        counts: dict[str, int] = {tag: 0 for tag in self.data["library"]["tags"]}
        for row in self.data["videos"]:
            for tag in row["tags"]:
                counts[tag] = counts.get(tag, 0) + 1
        return counts

    def stats(self) -> dict:
        videos = self.data["videos"]
        deprecated = sum(1 for row in videos if row["deprecated"])
        untagged = sum(1 for row in videos if not row["tags"] and not row["deprecated"])
        return {
            "total": len(videos),
            "active": len(videos) - deprecated - untagged,
            "deprecated": deprecated,
            "untagged": untagged,
            "missing": sum(1 for row in videos if row["missing"]),
            "changed": sum(1 for row in videos if row["changed"]),
            "uploaded": sum(1 for row in videos if row["uploaded"]),
        }

    # ------------------------------------------------------------------ 写入骨架
    def _atomic_save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.data, ensure_ascii=False, separators=(",", ":"))
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            if self.path.exists() and self.path.stat().st_size:
                shutil.copyfile(self.path, self.path.with_name(self.path.name + ".bak"))
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise StoreUnavailable(f"标签真源写入失败：{exc}") from None
        self._stat = _stat_of(self.path)

    def _audit_append(self, action: str, user: str, detail: dict) -> None:
        entry = {"ts": time.time(), "user": str(user or ""), "revision": self.revision,
                 "action": str(action or ""), "detail": detail or {}}
        audit = self.data.setdefault("audit", [])
        audit.append(entry)
        overflow = len(audit) - AUDIT_LIMIT
        if overflow > 0:
            del audit[:overflow]
            self.data["audit_dropped"] = int(self.data.get("audit_dropped") or 0) + overflow

    def _mutate(self, fn, *, user: str, revision: int | None, action: str) -> dict:
        """统一写路径：锁 → 重读 → 校验 revision → 改内存 → revision+1 → 审计 → 原子写。"""
        with self._lock, _FileLock(self.path):
            self._reload_locked()
            if revision is not None and int(revision) != self.revision:
                raise RevisionConflict("标签数据已被其他人更新，请刷新后重试", self.revision)
            result = fn() or {}
            changed = int(result.get("changed") or 0)
            detail = result.pop("detail", None) or {}
            if not changed:
                return {**result, "changed": 0, "revision": self.revision}
            self.data["revision"] = self.revision + 1
            self.data["updated_at"] = time.time()
            self.data["updated_by"] = str(user or "")
            self._audit_append(action, user, detail)
            self._atomic_save()
            self._index_locked()
            # 每次成功写入都留一份整库快照；快照失败不影响已经落盘的数据
            try:
                self._write_version_locked("auto", action, user, detail)
            except Exception:  # noqa: BLE001 - 版本是旁路能力，不能反过来阻断保存
                pass
            return {**result, "changed": changed, "revision": self.revision}

    # ------------------------------------------------------------------ 视频写
    def set_tags(self, ids, tags, *, mode: str = "replace", user: str = "",
                 revision: int | None = None) -> dict:
        mode = str(mode or "replace").strip().lower()
        if mode not in MODES:
            raise StoreError("打标方式只能是 replace / add / remove")
        wanted = _clean_ids(ids)
        labels = _clean_tags(tags)

        def op() -> dict:
            # 锁内重读后再校验：别人刚删掉的标签不能写进来
            known = set(self.data["library"]["tags"])
            unknown = [label for label in labels if label not in known]
            if unknown:
                raise StoreError(f"标签不存在：{unknown[0]}")
            updated: list[dict] = []
            skipped: list[str] = []
            now = time.time()
            for template_id in wanted:
                row = self._by_id.get(template_id)
                if row is None:
                    skipped.append(template_id)
                    continue
                before = list(row["tags"])
                if mode == "replace":
                    after = list(labels)
                elif mode == "add":
                    after = before + [label for label in labels if label not in before]
                else:
                    after = [label for label in before if label not in labels]
                if after == before:
                    continue
                row["tags"] = after
                row["touched_at"] = now
                row["touched_by"] = str(user or "")
                updated.append(copy.deepcopy(row))
            if not updated and skipped and len(skipped) == len(wanted):
                raise NotFound(f"视频不存在：{skipped[0]}")
            return {"changed": len(updated), "updated": updated, "skipped": skipped,
                    "detail": {"ids": wanted, "mode": mode, "tags": labels, "affected": len(updated)}}

        return self._mutate(op, user=user, revision=revision, action="set_tags")

    def deprecate(self, ids, deprecated: bool, *, user: str = "", revision: int | None = None,
                  reason: str = "") -> dict:
        wanted = _clean_ids(ids)
        flag = bool(deprecated)

        def op() -> dict:
            updated: list[dict] = []
            skipped: list[str] = []
            now = time.time()
            for template_id in wanted:
                row = self._by_id.get(template_id)
                if row is None:
                    skipped.append(template_id)
                    continue
                if bool(row["deprecated"]) == flag:
                    continue
                row["deprecated"] = flag
                row["deprecated_at"] = now if flag else 0.0
                row["deprecated_by"] = str(user or "") if flag else ""
                row["deprecated_reason"] = str(reason or "") if flag else ""
                row["touched_at"] = now
                row["touched_by"] = str(user or "")
                updated.append(copy.deepcopy(row))
            if not updated and skipped and len(skipped) == len(wanted):
                raise NotFound(f"视频不存在：{skipped[0]}")
            return {"changed": len(updated), "updated": updated, "skipped": skipped,
                    "detail": {"ids": wanted, "deprecated": flag, "reason": str(reason or "")}}

        return self._mutate(op, user=user, revision=revision, action="deprecate")

    def _unique_name_locked(self, name: str) -> str:
        """同一份清单里文件名必须唯一：重名自动加 -2、-3（COS 对象名另带随机后缀）。"""
        taken = {str(row.get("name") or "").casefold() for row in self.data["videos"]}
        base = str(name or "").strip() or "video.mp4"
        if base.casefold() not in taken:
            return base
        stem, suffix = Path(base).stem, Path(base).suffix
        index = 2
        while f"{stem}-{index}{suffix}".casefold() in taken:
            index += 1
        return f"{stem}-{index}{suffix}"

    def _next_movie_number_locked(self) -> int:
        """按清单里现有文件名的数字部分顺延，例如最后是 1411.mp4 → 1412。"""
        numbers = []
        for row in self.data["videos"]:
            for raw in (row.get("name"), row.get("relative_path"), row.get("original_name")):
                stem = Path(str(raw or "")).stem
                if stem.isdigit():
                    numbers.append(int(stem))
        return max(numbers, default=0) + 1

    def add_video(self, *, name: str, tags, size_mb=0.0, size_bytes=0, duration=None,
                  fingerprint: str = "", uploaded: bool = True,
                  source_dir: str = "网页上传", auto_number: bool = False,
                  user: str = "", revision: int | None = None) -> dict:
        """网页上传成功后登记一条新视频（编号自动顺延，重名自动改名）。

        `auto_number=True` 时忽略原文件名，按清单里最后一个数字文件顺延成 0001 形式。
        COS 对象已经先传好了再调这里；本方法只写清单，不碰对象存储。
        """
        labels = _clean_tags(tags)
        if not labels:
            raise StoreError("新视频至少要有一个标签")
        raw_name = str(name or "").strip() or "video.mp4"

        def op() -> dict:
            known = set(self.data["library"]["tags"])
            unknown = [label for label in labels if label not in known]
            if unknown:
                raise StoreError(f"标签不存在：{unknown[0]}")
            if auto_number:
                suffix = Path(raw_name).suffix.lower() or ".mp4"
                filename = self._unique_name_locked(f"{self._next_movie_number_locked():04d}{suffix}")
            else:
                filename = self._unique_name_locked(raw_name)
            videos = self.data["videos"]
            maximum = max((int(row["id"]) for row in videos if row["id"].isdigit()), default=0)
            try:
                next_id = int(self.data["project"].get("next_id") or 0)
            except (TypeError, ValueError):
                next_id = 0
            next_id = max(next_id, maximum + 1)
            template_id = f"{next_id:03d}"
            now = time.time()
            row = _normalize_video({
                "id": template_id, "relative_path": filename, "name": filename,
                "duration": duration, "size_mb": size_mb, "size_bytes": size_bytes,
                "fingerprint": fingerprint, "tags": labels, "uploaded": uploaded,
                "deprecated": False, "missing": False, "changed": False,
                "source_dir": source_dir, "uploaded_at": now, "touched_at": now,
                "touched_by": str(user or ""),
            })
            videos.append(row)
            videos.sort(key=lambda item: (not item["id"].isdigit(),
                                          int(item["id"]) if item["id"].isdigit() else 0,
                                          item["id"]))
            self.data["project"]["next_id"] = next_id + 1
            return {
                "changed": 1,
                "updated": [copy.deepcopy(row)],
                "row": copy.deepcopy(row),
                "renamed": filename != raw_name,
                "detail": {"id": template_id, "name": filename, "tags": labels,
                           "renamed_from": raw_name if filename != raw_name else ""},
            }

        return self._mutate(op, user=user, revision=revision, action="upload_video")

    def apply_project_diff(self, base: dict, current: dict, *, user: str = "",
                           revision: int | None = None) -> dict:
        """桌面打标器唯一写入口：只写两边的差异，绝不整份覆盖（避免冲掉网页刚改的标签）。

        - current 有、base 没有 → 追加（主机扫描新增的视频）。
        - 两边都有且字段不同 → 按 current 更新。
        - base 有、current 没有 → 标记弃用（不删行；新版桌面端的「移除」已改为弃用）。
        """
        base_rows = {normalize_id(row.get("id")): row for row in (base or {}).get("videos") or []}
        current_rows = {normalize_id(row.get("id")): row for row in (current or {}).get("videos") or []}
        fields = ("relative_path", "name", "duration", "size_mb", "fingerprint", "tags",
                  "deprecated", "missing", "changed", "uploaded", "source_dir", "original_name")
        library_tags = _clean_tags((current or {}).get("tags"))
        library_categories = (current or {}).get("tag_categories") or {}

        def op() -> dict:
            updated: list[dict] = []
            added = 0
            retired = 0
            now = time.time()
            for template_id, source in current_rows.items():
                target = self._by_id.get(template_id)
                if target is None:
                    row = _normalize_video(source)
                    row["touched_at"] = now
                    row["touched_by"] = str(user or "")
                    self.data["videos"].append(row)
                    added += 1
                    updated.append(copy.deepcopy(row))
                    continue
                before = {key: target.get(key) for key in fields}
                after = {key: source.get(key) for key in fields}
                after["tags"] = _clean_tags(after.get("tags"))
                after["deprecated"] = bool(after.get("deprecated"))
                if before == after:
                    continue
                target.update(after)
                target["touched_at"] = now
                target["touched_by"] = str(user or "")
                updated.append(copy.deepcopy(target))
            for template_id, row in base_rows.items():
                if template_id in current_rows:
                    continue
                target = self._by_id.get(template_id)
                if target is None or target["deprecated"]:
                    continue
                target["deprecated"] = True
                target["deprecated_at"] = now
                target["deprecated_by"] = str(user or "")
                target["deprecated_reason"] = "桌面端移除"
                retired += 1
                updated.append(copy.deepcopy(target))
            library_changed = 0
            known = {tag.casefold() for tag in self.data["library"]["tags"]}
            for tag in library_tags:
                if tag.casefold() in known:
                    continue
                self.data["library"]["tags"].append(tag)
                self.data["library"]["categories"][tag] = (
                    library_categories.get(tag) if library_categories.get(tag) in TAG_CATEGORIES else "场景")
                known.add(tag.casefold())
                library_changed += 1
            if updated or library_changed:
                self.data["videos"].sort(key=lambda item: (not item["id"].isdigit(),
                                                           int(item["id"]) if item["id"].isdigit() else 0,
                                                           item["id"]))
            return {"changed": len(updated) + library_changed, "updated": updated,
                    "added": added, "retired": retired, "library_added": library_changed,
                    "detail": {"updated": len(updated), "added": added, "retired": retired}}

        return self._mutate(op, user=user, revision=revision, action="project_sync")

    # ------------------------------------------------------------------ 标签库写
    def _require_tag(self, name: str) -> str:
        label = str(name or "").strip()
        if not label:
            raise StoreError("请提供标签名称")
        if label not in self.data["library"]["tags"]:
            raise StoreError(f"标签不存在：{label}")
        return label

    def library_create(self, name: str, category: str = "", *, user: str = "",
                       revision: int | None = None) -> dict:
        label = validate_tag_name(name)
        category = str(category or "").strip() or "场景"
        if category not in TAG_CATEGORIES:
            raise StoreError(f"分类只能是：{' / '.join(TAG_CATEGORIES)}")

        def op() -> dict:
            if label.casefold() in {tag.casefold() for tag in self.data["library"]["tags"]}:
                raise StoreError("这个标签已经存在")
            self.data["library"]["tags"].append(label)
            self.data["library"]["categories"][label] = category
            return {"changed": 1, "library": self.library(), "affected": 0,
                    "detail": {"name": label, "category": category}}

        return self._mutate(op, user=user, revision=revision, action="library_create")

    def library_rename(self, name: str, new_name: str, *, user: str = "",
                       revision: int | None = None) -> dict:
        source = self._require_tag(name)
        target = validate_tag_name(new_name)
        if target == source:
            raise StoreError("新名称与原名相同")

        def op() -> dict:
            tags = self.data["library"]["tags"]
            if source not in tags:
                raise StoreError(f"标签不存在：{source}")
            if target.casefold() in {tag.casefold() for tag in tags}:
                raise StoreError("新名称已存在，请改用合并")
            tags[tags.index(source)] = target
            categories = self.data["library"]["categories"]
            categories[target] = categories.pop(source, "场景")
            updated = []
            for row in self.data["videos"]:
                if source not in row["tags"]:
                    continue
                row["tags"] = [target if tag == source else tag for tag in row["tags"]]
                updated.append(copy.deepcopy(row))
            return {"changed": 1, "library": self.library(), "affected": len(updated),
                    "updated": updated,
                    "detail": {"from": source, "to": target, "affected": len(updated)}}

        return self._mutate(op, user=user, revision=revision, action="library_rename")

    def library_set_category(self, name: str, category: str, *, user: str = "",
                             revision: int | None = None) -> dict:
        label = self._require_tag(name)
        category = str(category or "").strip()
        if category not in TAG_CATEGORIES:
            raise StoreError(f"分类只能是：{' / '.join(TAG_CATEGORIES)}")

        def op() -> dict:
            if self.data["library"]["categories"].get(label) == category:
                return {"changed": 0, "library": self.library(), "affected": 0}
            self.data["library"]["categories"][label] = category
            return {"changed": 1, "library": self.library(), "affected": 0,
                    "detail": {"name": label, "category": category}}

        return self._mutate(op, user=user, revision=revision, action="library_set_category")

    def library_merge(self, source: str, target: str, *, user: str = "",
                      revision: int | None = None) -> dict:
        source_label = self._require_tag(source)
        target_label = self._require_tag(target)
        if source_label == target_label:
            raise StoreError("不能把标签合并到自己")

        def op() -> dict:
            tags = self.data["library"]["tags"]
            if source_label not in tags or target_label not in tags:
                raise StoreError("标签已被其他人删除，请刷新后重试")
            updated = []
            for row in self.data["videos"]:
                if source_label not in row["tags"]:
                    continue
                merged: list[str] = []
                for tag in row["tags"]:
                    label = target_label if tag == source_label else tag
                    if label not in merged:
                        merged.append(label)
                row["tags"] = merged
                updated.append(copy.deepcopy(row))
            tags = self.data["library"]["tags"]
            if source_label in tags:
                tags.remove(source_label)
            self.data["library"]["categories"].pop(source_label, None)
            return {"changed": max(1, len(updated)), "library": self.library(),
                    "affected": len(updated), "updated": updated,
                    "becomes_untagged": 0,
                    "detail": {"source": source_label, "target": target_label, "affected": len(updated)}}

        return self._mutate(op, user=user, revision=revision, action="library_merge")

    def library_delete(self, name: str, *, user: str = "", revision: int | None = None) -> dict:
        label = self._require_tag(name)

        def op() -> dict:
            tags = self.data["library"]["tags"]
            if label not in tags:
                raise StoreError(f"标签不存在：{label}")
            updated = []
            becomes_untagged = 0
            for row in self.data["videos"]:
                if label not in row["tags"]:
                    continue
                row["tags"] = [tag for tag in row["tags"] if tag != label]
                if not row["tags"]:
                    becomes_untagged += 1
                updated.append(copy.deepcopy(row))
            tags.remove(label)
            self.data["library"]["categories"].pop(label, None)
            return {"changed": max(1, len(updated)), "library": self.library(),
                    "affected": len(updated), "becomes_untagged": becomes_untagged,
                    "updated": updated,
                    "detail": {"name": label, "affected": len(updated),
                               "becomes_untagged": becomes_untagged}}

        return self._mutate(op, user=user, revision=revision, action="library_delete")

    # ------------------------------------------------------------------ 只读推演
    def impact_preview(self, action: str, params: dict | None = None) -> dict:
        """不写盘、不动 revision。给前端二次确认用。"""
        params = params or {}
        action = str(action or "").strip()
        with self._lock:
            rows = self.data["videos"]
            affected = 0
            becomes_untagged = 0
            samples: list[dict] = []
            if action in ("delete", "merge", "rename", "set_category"):
                label = str(params.get("name") or params.get("source") or "").strip()
                if label and label not in self.data["library"]["tags"]:
                    raise StoreError(f"标签不存在：{label}")
                if action in ("delete", "merge", "rename") and label:
                    for row in rows:
                        if label not in row["tags"]:
                            continue
                        affected += 1
                        if action == "delete" and len(row["tags"]) == 1:
                            becomes_untagged += 1
                        if len(samples) < 5:
                            samples.append({"id": row["id"], "name": row["name"]})
            elif action == "deprecate":
                for template_id in _clean_ids(params.get("ids")):
                    row = self._by_id.get(template_id)
                    if row is None or row["deprecated"]:
                        continue
                    affected += 1
                    if len(samples) < 5:
                        samples.append({"id": row["id"], "name": row["name"]})
            elif action in ("set_tags", "library_create"):
                affected = len(_clean_ids(params.get("ids"))) if params.get("ids") else 0
                samples = []
            else:
                raise StoreError("不支持的影响预览动作")
            return {"action": action, "affected": affected, "becomes_untagged": becomes_untagged,
                    "samples": samples, "revision": self.revision}

    def audit_tail(self, limit: int = 30) -> list[dict]:
        limit = max(1, min(int(limit or 30), AUDIT_LIMIT))
        with self._lock:
            return [dict(entry) for entry in self.data.get("audit", [])[-limit:]]

    # ------------------------------------------------------------------ 版本快照
    def versions_dir(self) -> Path:
        return self.path.parent / VERSION_DIRNAME

    def _snapshot_payload(self, kind: str, action: str, user: str, detail: dict,
                          note: str = "") -> dict:
        """整份真源快照：视频库（含每条的标签与弃用态）+ 标签定义 + 项目信息。"""
        return {
            "schema": VERSION_SCHEMA,
            "kind": kind,
            "action": str(action or ""),
            "note": str(note or "")[:120],
            "revision": self.revision,
            "at": time.time(),
            "user": str(user or ""),
            "detail": detail or {},
            "library": self.library(),
            "project": copy.deepcopy(self.data.get("project") or {}),
            "videos": copy.deepcopy(self.data.get("videos") or []),
        }

    def _write_version_locked(self, kind: str, action: str, user: str, detail: dict,
                              note: str = "") -> dict:
        payload = self._snapshot_payload(kind, action, user, detail, note)
        folder = self.versions_dir()
        folder.mkdir(parents=True, exist_ok=True)
        name = (f"v{payload['revision']:06d}-{int(payload['at'])}-{kind}-"
                f"{uuid.uuid4().hex[:4]}.json.gz")
        target = folder / name
        tmp = folder / (name + ".tmp")
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        with gzip.open(tmp, "wb", compresslevel=6) as handle:
            handle.write(raw)
        os.replace(tmp, target)

        meta = {
            "name": name, "kind": kind, "action": payload["action"], "note": payload["note"],
            "revision": payload["revision"], "at": payload["at"], "user": payload["user"],
            "count": len(payload["videos"]), "bytes": target.stat().st_size,
        }
        meta_path = folder / (name + ".meta.json")
        meta_tmp = folder / (name + ".meta.json.tmp")
        meta_tmp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        os.replace(meta_tmp, meta_path)
        self._prune_versions_locked()
        return meta

    def _prune_versions_locked(self) -> None:
        folder = self.versions_dir()
        if not folder.is_dir():
            return
        entries: list[tuple[float, dict, Path]] = []
        for meta_path in folder.glob("*.meta.json"):
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            entries.append((float(meta.get("at") or 0), meta, meta_path))
        for kind, keep in (("auto", AUTO_KEEP), ("manual", PINNED_KEEP)):
            group = sorted((item for item in entries if item[1].get("kind") == kind),
                           key=lambda item: item[0])
            for _at, meta, meta_path in group[:-keep]:
                name = str(meta.get("name") or "")
                if name:
                    (folder / name).unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)

    def list_versions(self, limit: int = 100) -> list[dict]:
        """只读 meta，不解析快照正文（快照是 gzip 的整库数据）。"""
        limit = max(1, min(int(limit or 100), 500))
        with self._lock:
            folder = self.versions_dir()
            if not folder.is_dir():
                return []
            items: list[dict] = []
            for meta_path in folder.glob("*.meta.json"):
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if isinstance(meta, dict) and meta.get("name"):
                    items.append(meta)
        items.sort(key=lambda item: float(item.get("at") or 0), reverse=True)
        return items[:limit]

    def read_version(self, name: str) -> dict:
        raw_name = str(name or "").strip()
        if not VERSION_NAME.match(raw_name):
            raise StoreError("版本文件名无效")
        path = self.versions_dir() / raw_name
        if not path.is_file():
            raise NotFound("找不到该版本")
        try:
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise StoreUnavailable(f"版本文件损坏：{raw_name}（{exc}）") from None
        if not isinstance(payload, dict):
            raise StoreUnavailable(f"版本文件格式错误：{raw_name}")
        return payload

    def create_version(self, note: str = "", *, user: str = "") -> dict:
        """手动打一个版本（长期保留，不参与自动版本的 50 份淘汰）。"""
        with self._lock, _FileLock(self.path):
            self._reload_locked()
            return self._write_version_locked(
                "manual", "manual", user or self.updated_by,
                {"note": str(note or "")[:120]}, note=str(note or ""))

    def restore_version(self, name: str, *, user: str = "",
                        revision: int | None = None) -> dict:
        """把整份真源回滚到某个版本：视频库条目、每条标签、弃用态、标签定义一起还原。

        回滚前会自动留一份 manual 备份（note=回滚前备份），所以这次回滚本身也能撤回。
        """
        snapshot = self.read_version(name)
        raw_videos = snapshot.get("videos")
        if not isinstance(raw_videos, list) or not raw_videos:
            raise StoreError("该版本里没有视频数据，无法回滚")
        videos: list[dict] = []
        seen: set[str] = set()
        for item in raw_videos:
            row = _normalize_video(item)
            if row["id"] in seen:
                raise StoreError(f"版本数据里模板ID重复：{row['id']}")
            seen.add(row["id"])
            videos.append(row)
        library_raw = snapshot.get("library") if isinstance(snapshot.get("library"), dict) else {}
        target_tags = _clean_tags(library_raw.get("tags"))
        target_categories = normalize_tag_categories(target_tags, library_raw.get("categories") or {})

        def op() -> dict:
            # 先备份当前状态（与回滚同一次持锁，避免中间被别的进程插队）
            backup = self._write_version_locked(
                "manual", "before_restore", user or self.updated_by,
                {"restore_to": name}, note=f"回滚前备份（{name}）")
            current_ids = {row["id"] for row in self.data.get("videos") or []}
            target_ids = {row["id"] for row in videos}
            self.data["library"] = {"version": 2, "tags": target_tags,
                                    "categories": target_categories}
            self.data["videos"] = videos
            if isinstance(snapshot.get("project"), dict) and snapshot["project"]:
                self.data["project"] = copy.deepcopy(snapshot["project"])
            return {
                "changed": max(1, len(videos)),
                "updated": [],
                "restored": len(videos),
                # 当前有、目标版本没有 → 回滚后会从清单消失（数据仍在备份版里）
                "added_since": len(current_ids - target_ids),
                # 目标版本有、当前没有 → 回滚后重新出现
                "removed_since": len(target_ids - current_ids),
                "backup": backup.get("name", ""),
                "detail": {"restore_to": name, "restored": len(videos)},
            }

        result = self._mutate(op, user=user, revision=revision, action="restore_version")
        return {**result, "version": name}

    # ------------------------------------------------------------------ 迁移 / 交接
    def import_from_xlsx(self, source: str | Path, *, dry_run: bool = False, user: str = "import") -> dict:
        """把标签 xlsx 全量导入真源。dry_run 只对账，不写盘。"""
        try:
            project = load_xlsx(source)
        except CatalogError as exc:
            raise StoreError(str(exc)) from None
        videos: list[dict] = []
        seen: set[str] = set()
        deprecated = 0
        untagged = 0
        no_fingerprint = 0
        no_size = 0
        for video in project.get("videos") or []:
            row = _normalize_video(video)
            if row["id"] in seen:
                raise StoreError(f"清单中模板ID重复：{row['id']}")
            seen.add(row["id"])
            if row["deprecated"]:
                deprecated += 1
            if not row["tags"]:
                untagged += 1
            if not row["fingerprint"]:
                no_fingerprint += 1
            if not row["size_mb"]:
                no_size += 1
            videos.append(row)
        videos.sort(key=lambda item: (not item["id"].isdigit(),
                                      int(item["id"]) if item["id"].isdigit() else 0, item["id"]))
        report = {
            "source": str(source), "rows": len(videos), "deprecated": deprecated,
            "untagged": untagged, "no_fingerprint": no_fingerprint, "no_size": no_size,
            "tags": len(project.get("tags") or []),
            "duplicate_ids": 0,
        }
        if dry_run:
            self._check_import(report)
            return {**report, "dry_run": True, "written": False, "revision": self.revision}

        def op() -> dict:
            self.data["library"] = {
                "version": 2,
                "tags": _clean_tags(project.get("tags")),
                "categories": normalize_tag_categories(_clean_tags(project.get("tags")),
                                                       project.get("tag_categories") or {}),
            }
            self.data["project"] = {
                "project_id": str(project.get("project_id") or ""),
                "source_root": str(project.get("source_root") or ""),
                "roots": [str(value) for value in (project.get("roots") or []) if str(value)],
                "next_id": int(project.get("next_id") or 1),
            }
            self.data["videos"] = videos
            self.data.setdefault("migrated_from", None)
            self.data["migrated_from"] = {
                "kind": "xlsx", "path": str(source), "imported_at": time.time(),
                "rows": len(videos), "deprecated": deprecated, "untagged": untagged,
            }
            return {"changed": 1, "detail": {"source": str(source), "rows": len(videos)},
                    "affected": len(videos)}

        result = self._mutate(op, user=user, revision=None, action="import_xlsx")
        return {**report, "dry_run": False, "written": True, "revision": result["revision"]}

    @staticmethod
    def _check_import(report: dict) -> None:
        if report["rows"] <= 0:
            raise StoreError("标签清单里没有任何视频行")
        if report["duplicate_ids"]:
            raise StoreError(f"清单中有 {report['duplicate_ids']} 个重复模板ID")

    def export_to_xlsx(self, destination: str | Path) -> Path:
        return save_xlsx(self.to_project(), destination)

    def to_project(self) -> dict:
        """转成打标器现有的 project 结构（桌面端直接复用，不用改 UI 数据结构）。"""
        with self._lock:
            library = self.library()
            project = copy.deepcopy(self.data["project"])
            videos = copy.deepcopy(self.data["videos"])
        project["tags"] = library["tags"]
        project["tag_categories"] = library["categories"]
        project["videos"] = videos
        project.setdefault("roots", sorted({row["source_dir"] for row in videos if row["source_dir"]}))
        return project
