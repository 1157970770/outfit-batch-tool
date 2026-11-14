#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批次永久档案：款号、提示词、全部参考图的本机独立副本。

为什么单独做一份，而不是只留 uploads：
  · uploads 目前没有 GC，但将来清理、换机、误删都可能发生；批次 JSON 一删，
    「这批复用了哪张定妆图、当时提示词是什么」就再也对不上；
  · 档案按「款号 / 批次号」组织，人可以直接在资源管理器里翻，不依赖网页；
  · 批次删除后档案继续保留（只写 deleted.json 墓碑），这是本模块最重要的语义。

目录（<data_dir>/archive/）：
    <款号>/<批次号>__<batch_id>/
        meta.json               当前快照 + 全部图片索引 + 校验和
        prompt.txt              当前提示词
        prompt-history.jsonl    历次提示词全文（append-only）
        history.jsonl           建批次 / 改图 / 换主图 / 改词 / 锁定 / 生产 / 关闭 / 删除
        archive说明.txt         人看的说明
        checksums.sha256        images/ 下每个文件的 sha256
        images/<upload_id>.<ext> 每张曾上传的图都留，删图不删文件
        runs/<job_id>.json      每轮提交时的提示词、图片、模板 ID 快照
        deleted.json            仅批次被删除后出现

写入约定：
  · JSON / 文本一律临时文件 + fsync + os.replace，避免半截文件；
  · 图片按 upload_id 命名，天然去重；同尺寸已存在则跳过复制，校验和复用；
  · 失败抛 BatchArchiveError，由调用方决定「记 warning 继续」还是「拦住删除」。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
import zipfile
from pathlib import Path

from app.web.paths import relocate

ARCHIVE_VERSION = 1
STYLE_FALLBACK = "未分类"
MAX_SEGMENT = 60
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
             *(f"lpt{i}" for i in range(1, 10))}


class BatchArchiveError(RuntimeError):
    """档案落盘失败（磁盘满、权限、源文件读不了等）。"""


def _text(value) -> str:
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    return str(value or "").strip()


def style_of(batch: dict) -> str:
    """款号：6 栏位里的上衣优先，老批次回退 style_no，都空则归「未分类」。"""
    tags = batch.get("tags") if isinstance(batch.get("tags"), dict) else {}
    return _text(tags.get("top")) or _text(batch.get("style_no")) or STYLE_FALLBACK


def safe_segment(raw: str, fallback: str = "未命名") -> str:
    """把款号/批次号变成 Windows 合法目录名，并挡住路径穿越与保留名。"""
    text = _text(raw) or fallback
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", text)
    text = re.sub(r"\s+", " ", text).strip().strip(".")
    if not text:
        text = fallback
    if text.lower() in _RESERVED:
        text = f"_{text}"
    # 目录名太长会让 Windows 路径超限（款号 60 + 批次号 60 + 文件名）。
    return text[:MAX_SEGMENT].rstrip(" .") or fallback


def _iso(ts=None) -> str:
    if ts is None:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _sha256_file(path: Path, *, limit: int | None = None) -> str:
    digest = hashlib.sha256()
    read = 0
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            read += len(chunk)
            if limit is not None and read > limit:
                break
    return digest.hexdigest()


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _append_jsonl(path: Path, record: dict) -> None:
    """一次 os.write 追加一行，尽量降低与另一个进程交叉写的概率。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def normalize_images(batch: dict, data_dir: Path | str) -> list[dict]:
    """把批次的 images[] 整理成 [{slot, upload_id, name, path}]，老数据回退 image_path。"""
    raw = batch.get("images")
    items: list[dict] = []
    if isinstance(raw, list) and raw:
        for index, slot in enumerate(raw, 1):
            if not isinstance(slot, dict):
                continue
            path = relocate(slot.get("path"), data_dir)
            items.append({"slot": index,
                          "upload_id": _text(slot.get("upload_id")),
                          "name": _text(slot.get("name")),
                          "path": path})
    else:
        path = relocate(batch.get("image_path"), data_dir)
        if str(batch.get("image_upload_id") or "") or str(path):
            items.append({"slot": 1,
                          "upload_id": _text(batch.get("image_upload_id")),
                          "name": _text(batch.get("image_name")),
                          "path": path})
    return items


class BatchArchive:
    """单个数据根下的档案读写。进程内共享 RLock，跨进程靠原子替换。"""

    def __init__(self, data_dir: Path | str, *, create: bool = True):
        self.data_dir = Path(data_dir)
        self.root = self.data_dir / "archive"
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ 路径
    def path_for(self, batch: dict) -> Path:
        style = safe_segment(style_of(batch), STYLE_FALLBACK)
        no = safe_segment(str(batch.get("batch_no") or ""), "无批次号")
        batch_id = safe_segment(str(batch.get("id") or ""), "unknown")
        return self.root / style / f"{no}__{batch_id}"

    # ------------------------------------------------------------------ 查询
    def info(self, batch: dict) -> dict:
        """只读状态，不复制图片；老批次第一次点「下载档案」时才真正补档。"""
        folder = self.path_for(batch)
        meta = _read_json(folder / "meta.json")
        images = normalize_images(batch, self.data_dir)
        ready = (folder / "meta.json").is_file()
        size_bytes = 0
        if folder.is_dir():
            for path in folder.rglob("*"):
                try:
                    if path.is_file():
                        size_bytes += path.stat().st_size
                except OSError:
                    continue
        return {
            "ready": ready,
            "id": str(batch.get("id") or ""),
            "path": str(folder),
            "style": style_of(batch),
            "batch_id": str(batch.get("id") or ""),
            "batch_no": str(batch.get("batch_no") or ""),
            "image_count": len(meta.get("current_images") or images),
            "size_bytes": size_bytes,
            "updated_at": float(meta.get("updated_at") or 0),
            "deleted": bool(meta.get("deleted")),
        }

    # ------------------------------------------------------------------ 同步
    def ensure(self, batch: dict, *, event: str = "sync", user: str = "",
               copy_images: bool = True) -> dict:
        """把当前批次同步进档案；幂等，可重复调用。"""
        with self._lock:
            folder = self.path_for(batch)
            images_dir = folder / "images"
            runs_dir = folder / "runs"
            images_dir.mkdir(parents=True, exist_ok=True)
            runs_dir.mkdir(parents=True, exist_ok=True)

            old_meta = _read_json(folder / "meta.json")
            old_all = {str(item.get("upload_id") or ""): dict(item)
                       for item in (old_meta.get("all_images") or [])
                       if isinstance(item, dict) and item.get("upload_id")}

            now = time.time()
            warnings: list[str] = []
            current: list[dict] = []
            all_images: dict[str, dict] = dict(old_all)

            for slot in normalize_images(batch, self.data_dir):
                upload_id = slot["upload_id"]
                source = Path(slot["path"]) if slot["path"] else Path()
                name = slot["name"] or (source.name if source.name else upload_id or "未命名")
                if not upload_id:
                    warnings.append(f"第 {slot['slot']} 张图片没有 upload_id，无法归档")
                    continue
                ext = source.suffix.lower()
                if ext not in IMAGE_EXTS:
                    ext = ".png"
                filename = f"{upload_id}{ext}"
                dest = images_dir / filename
                existing = all_images.get(upload_id) or {}
                entry = {
                    "upload_id": upload_id,
                    "name": name,
                    "file": f"images/{filename}",
                    "slot": slot["slot"],
                    "size": 0,
                    "sha256": "",
                    "missing": False,
                    "first_seen_at": existing.get("first_seen_at") or now,
                    "last_seen_at": now,
                    "removed_at": None,
                }
                if not source or not source.is_file():
                    # 源图被手工删了：保留索引，但不把整个档案搞挂。
                    entry["missing"] = True
                    warnings.append(f"源图不存在：{name}")
                    if dest.is_file():
                        entry["size"] = dest.stat().st_size
                        entry["sha256"] = existing.get("sha256") or ""
                    all_images[upload_id] = entry
                    current.append(entry)
                    continue
                try:
                    size = source.stat().st_size
                    if dest.is_file() and dest.stat().st_size == size:
                        # 同尺寸视为同一张：复用旧校验和，避免每次重读几百 MB。
                        sha = str(existing.get("sha256") or "")
                        if not sha:
                            sha = _sha256_file(dest)
                    else:
                        if copy_images:
                            self._copy_atomic(source, dest)
                        else:
                            warnings.append(f"未复制图片（本次只写元数据）：{name}")
                        sha = _sha256_file(dest) if dest.is_file() else ""
                    entry.update({"size": size, "sha256": sha})
                except OSError as exc:
                    # 复制/读取失败必须让调用方知道：删除批次时要据此拒绝。
                    raise BatchArchiveError(f"参考图归档失败（{name}）：{exc}") from None
                all_images[upload_id] = entry
                current.append(entry)

            # 当前没出现的图：保留文件，只标 removed_at。
            current_ids = {item["upload_id"] for item in current}
            for upload_id, entry in all_images.items():
                if upload_id not in current_ids and not entry.get("removed_at"):
                    entry["removed_at"] = now

            tags = batch.get("tags") if isinstance(batch.get("tags"), dict) else {}
            items = [item for item in (batch.get("items") or []) if isinstance(item, dict)]
            runs = [dict(run) for run in (batch.get("runs") or []) if isinstance(run, dict)]
            prompt = str(batch.get("prompt") or "")
            meta = {
                "archive_version": ARCHIVE_VERSION,
                "batch_id": str(batch.get("id") or ""),
                "batch_no": str(batch.get("batch_no") or ""),
                "title": str(batch.get("title") or ""),
                "owner": str(batch.get("owner") or ""),
                "state": str(batch.get("state") or ""),
                "category": str(batch.get("category") or ""),
                "style": style_of(batch),
                "style_no": str(batch.get("style_no") or ""),
                "tags": tags,
                "prompt_id": str(batch.get("prompt_id") or ""),
                "prompt": prompt,
                "resolution": str(batch.get("resolution") or ""),
                "duration": batch.get("duration"),
                "duration_mode": str(batch.get("duration_mode") or ""),
                "image_changes": [dict(change) for change in (batch.get("image_changes") or [])
                                  if isinstance(change, dict)][-50:],
                "current_images": current,
                "all_images": sorted(all_images.values(),
                                     key=lambda item: (item.get("slot") or 999,
                                                       item.get("first_seen_at") or 0)),
                "runs": runs,
                "items_count": len(items),
                "done": sum(1 for item in items if item.get("state") == "done"),
                "failed": sum(1 for item in items if item.get("state") == "failed"),
                "pending": sum(1 for item in items if item.get("state") == "pending"),
                "producing": sum(1 for item in items if item.get("state") == "producing"),
                "created_at": batch.get("created_at"),
                "updated_at": batch.get("updated_at"),
                "locked_at": batch.get("locked_at"),
                "closed_at": batch.get("closed_at"),
                "deleted": bool(old_meta.get("deleted")),
                "deleted_at": old_meta.get("deleted_at"),
                "deleted_by": str(old_meta.get("deleted_by") or ""),
                "archive": {"updated_at": now, "warnings": warnings},
            }

            self._write_prompt(folder, prompt, meta["prompt_id"], event, user, now)
            self._write_checksums(folder, current)
            _atomic_write_text(folder / "meta.json",
                               json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
            self._write_readme(folder, meta)
            self._append_history(folder, meta, event, user, prompt, now)
            return meta

    # ------------------------------------------------------------------ 单轮
    def write_run(self, batch: dict, *, job_id: str, prompt: str, images: list[dict],
                  template_ids: list[str], at: float | None = None) -> Path:
        """写一轮生产快照：固定当轮实际提交的 prompt / 图片 / 模板 ID。"""
        with self._lock:
            folder = self.path_for(batch)
            run_path = folder / "runs" / f"{safe_segment(job_id, 'job')}.json"
            payload = {
                "job_id": str(job_id),
                "batch_id": str(batch.get("id") or ""),
                "batch_no": str(batch.get("batch_no") or ""),
                "style": style_of(batch),
                "prompt_id": str(batch.get("prompt_id") or ""),
                "prompt": str(prompt or ""),
                "images": [{"slot": item.get("slot"),
                            "upload_id": item.get("upload_id"),
                            "name": item.get("name"),
                            "file": f"images/{item.get('upload_id')}{Path(str(item.get('path') or '')).suffix.lower()}"
                            if item.get("upload_id") else ""}
                           for item in (images or [])],
                "template_ids": [str(tid) for tid in (template_ids or [])],
                "at": float(at or time.time()),
                "at_text": _iso(at),
            }
            _atomic_write_text(run_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            return run_path

    # ------------------------------------------------------------------ 删除墓碑
    def mark_deleted(self, batch: dict, *, user: str = "") -> Path:
        with self._lock:
            folder = self.path_for(batch)
            now = time.time()
            payload = {"batch_id": str(batch.get("id") or ""),
                       "batch_no": str(batch.get("batch_no") or ""),
                       "style": style_of(batch),
                       "deleted_at": now,
                       "deleted_at_text": _iso(now),
                       "deleted_by": str(user or "")}
            path = folder / "deleted.json"
            _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            meta = _read_json(folder / "meta.json")
            if meta:
                meta["deleted"] = True
                meta["deleted_at"] = now
                meta["deleted_by"] = str(user or "")
                _atomic_write_text(folder / "meta.json",
                                   json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
            _append_jsonl(folder / "history.jsonl",
                          {"at": now, "at_text": _iso(now), "event": "batch_deleted",
                           "by": str(user or ""), "key": f"deleted:{batch.get('id')}:{int(now)}"})
            return path

    # ------------------------------------------------------------------ ZIP
    def build_zip(self, batch: dict, dest: Path, *, ensure: bool = True) -> tuple[Path, int]:
        if ensure:
            self.ensure(batch, event="zip_export")
        folder = self.path_for(batch)
        if not folder.is_dir():
            raise BatchArchiveError("档案目录不存在。")
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        prefix = f"档案_{safe_segment(str(batch.get('batch_no') or ''), '批次')}_{safe_segment(str(batch.get('id') or ''), 'id')}"
        try:
            with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
                for path in sorted(folder.rglob("*")):
                    if not path.is_file():
                        continue
                    if path.name.startswith(".") and path.suffix == ".tmp":
                        continue
                    if path.name.endswith(".tmp"):
                        continue
                    arcname = f"{prefix}/{path.relative_to(folder).as_posix()}"
                    comp = (zipfile.ZIP_STORED
                            if path.suffix.lower() in IMAGE_EXTS else zipfile.ZIP_DEFLATED)
                    zf.write(path, arcname, compress_type=comp)
                    count += 1
        except Exception as exc:  # zipfile.BadZipFile / 权限 / 磁盘满统一包成可读错误
            try:
                dest.unlink(missing_ok=True)
            except OSError:
                pass
            raise BatchArchiveError(f"档案打包失败：{exc}") from None
        return dest, count

    # ------------------------------------------------------------------ 内部
    @staticmethod
    def _copy_atomic(source: Path, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            shutil.copy2(source, tmp)
            # 用追加模式打开只为拿一个可 fsync 的写句柄，不会改动文件内容。
            with tmp.open("ab") as fh:
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, dest)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _write_prompt(self, folder: Path, prompt: str, prompt_id: str,
                      event: str, user: str, now: float) -> None:
        _atomic_write_text(folder / "prompt.txt", prompt + ("\n" if prompt else ""))
        sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        history = folder / "prompt-history.jsonl"
        last_sha = ""
        try:
            lines = history.read_text(encoding="utf-8").splitlines()
            if lines:
                last = json.loads(lines[-1])
                last_sha = str(last.get("sha") or "")
        except (OSError, json.JSONDecodeError):
            last_sha = ""
        initial = not history.exists() and event not in ("batch_created", "backfill")
        if sha != last_sha or initial:
            _append_jsonl(history, {
                "at": now, "at_text": _iso(now), "event": event, "by": str(user or ""),
                "prompt_id": str(prompt_id or ""), "sha": sha, "prompt": prompt,
            })

    def _write_checksums(self, folder: Path, current: list[dict]) -> None:
        lines = []
        for item in current:
            sha = str(item.get("sha256") or "")
            rel = str(item.get("file") or "")
            if sha and rel:
                lines.append(f"{sha}  {rel}")
        _atomic_write_text(folder / "checksums.sha256",
                           "\n".join(sorted(lines)) + ("\n" if lines else ""))

    def _write_readme(self, folder: Path, meta: dict) -> None:
        tags = meta.get("tags") if isinstance(meta.get("tags"), dict) else {}
        lines = [
            "批次永久档案（本机自动留档，不随批次删除）",
            "=" * 40,
            f"款号：{meta.get('style') or ''}",
            f"批次号：{meta.get('batch_no') or ''}（内部 id：{meta.get('batch_id') or ''}）",
            f"备注：{meta.get('title') or ''}",
            f"归属：{meta.get('owner') or ''}",
            f"状态：{meta.get('state') or ''}",
            f"创建：{_iso(meta.get('created_at'))}",
            f"更新：{_iso(meta.get('updated_at'))}",
            "",
            "六栏位标签：",
        ]
        for key, value in tags.items():
            lines.append(f"  {key}: {value}")
        lines += [
            "",
            f"参考图（{len(meta.get('current_images') or [])} 张，历史共 {len(meta.get('all_images') or [])} 张）：",
        ]
        for item in meta.get("current_images") or []:
            flag = "（源图已丢失）" if item.get("missing") else ""
            lines.append(f"  槽位 {item.get('slot')}：{item.get('name')} → {item.get('file')}{flag}")
        lines += [
            "",
            f"生产轮次：{len(meta.get('runs') or [])}",
            "",
            "当前提示词：",
            str(meta.get("prompt") or ""),
            "",
            "文件说明：meta.json 快照 / prompt.txt 当前提示词 / prompt-history.jsonl 历次提示词 /",
            "history.jsonl 操作记录 / images/ 参考图 / runs/ 每轮快照 / checksums.sha256 校验和。",
        ]
        if meta.get("deleted"):
            lines += ["", f"批次已删除（{_iso(meta.get('deleted_at'))}，操作人 {meta.get('deleted_by') or ''}），本档案继续保留。"]
        _atomic_write_text(folder / "archive说明.txt", "\n".join(lines).rstrip() + "\n")

    def _append_history(self, folder: Path, meta: dict, event: str, user: str,
                        prompt: str, now: float) -> None:
        ids = [item.get("upload_id") for item in (meta.get("current_images") or [])]
        key = json.dumps({"event": event, "prompt": hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16],
                          "images": ids, "state": meta.get("state"),
                          "runs": len(meta.get("runs") or [])}, ensure_ascii=False, sort_keys=True)
        history = folder / "history.jsonl"
        try:
            lines = history.read_text(encoding="utf-8").splitlines()
            if lines:
                last = json.loads(lines[-1])
                if str(last.get("key") or "") == key:
                    return
        except (OSError, json.JSONDecodeError):
            pass
        _append_jsonl(history, {"at": now, "at_text": _iso(now), "event": event,
                                "by": str(user or ""), "key": key,
                                "state": meta.get("state"),
                                "images": ids, "runs": len(meta.get("runs") or [])})
