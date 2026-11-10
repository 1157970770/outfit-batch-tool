#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自由视频任务管理器。

设计取舍（与 jobs.py 的换装链路刻意分开）：

* **不读模板清单、不打标签、不归档 COS**：用户给什么提示词就生成什么，
  成品直接落 `web-data/videogen/files/<task>/shot-<n>.mp4`。
* **独立队列 + 独立线程池**：不占 `max_active_jobs` / `max_active_per_user`，
  也不跟大批量抢「job 槽位」；云端在飞仍走全局信号量（小批量快速通道口径）。
* **单任务多分镜顺序跑**：一次提交 N 个分镜就是 N 条独立云端任务，
  顺序执行可避免一个用户瞬间压满在飞名额；失败不影响后面的分镜。
* **记录里只存文件名**：绝对路径在读取时按当前 `data_dir` 重新拼，
  换机器 / 改数据目录都不会悬空（参考 `app/web/paths.py::relocate()` 的思路）。
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from app import config as appconfig
from app import media as media_mod
from app.core import download_file
from app.runtime import build_storages
from app.wan_api import (TaskCanceled, TaskExpired, WaitStopped, WaitTimeout,
                         WanClient, WanConfig, WanError, estimate_cost,
                         global_small_inflight)
from app.videogen.models import (MAX_IMAGES, VideoGenError, as_bool,
                                 now_text, options, validate_request)

STATUS_QUEUED = "排队中"
STATUS_RUNNING = "生成中"
STATUS_DONE = "已完成"
STATUS_PARTIAL = "部分完成"
STATUS_FAILED = "生成失败"
STATUS_CANCELED = "已取消"
ACTIVE_STATUSES = (STATUS_QUEUED, STATUS_RUNNING)
SHOT_QUEUED = "排队中"
SHOT_RUNNING = "生成中"
SHOT_DONE = "已完成"
SHOT_FAILED = "生成失败"
SHOT_CANCELED = "已取消"

UPLOAD_KINDS = {"video": {"suffixes": {".mp4", ".mov", ".m4v"}, "max_mb": 100},
                "audio": {"suffixes": {".wav", ".mp3"}, "max_mb": 15}}

_TASK_ID = re.compile(r"[a-f0-9]{32}")


def _iso_now() -> str:
    return now_text()


class VideoGenManager:
    """任务落盘 + 守护线程；接口风格对齐 ImageGenManager。"""

    def __init__(self, settings, *, root: str | Path | None = None,
                 client_factory=None, downloader=None):
        self.settings = settings
        self.data_dir = Path(settings.data_dir)
        self.root = Path(root) if root else self.data_dir / "videogen"
        self.tasks_dir = self.root / "tasks"
        self.files_dir = self.root / "files"
        self.uploads_dir = self.root / "uploads"
        for folder in (self.tasks_dir, self.files_dir, self.uploads_dir):
            folder.mkdir(parents=True, exist_ok=True)

        self._client_factory = client_factory or self._default_client
        self._download = downloader or download_file
        self._lock = threading.RLock()
        self._queue: queue.Queue[str] = queue.Queue()
        self._tasks: dict[str, dict] = {}
        self._workers: list[threading.Thread] = []
        self._worker_limit = 0
        self._closed = False
        self._uploader_obj = None
        self._uploader_error = ""
        self._uploader_checked = False
        self._load_existing()

    # ------------------------------------------------------------------ 配置
    @property
    def configured(self) -> bool:
        return bool(appconfig.wan_configured(self.settings.config))

    def unconfigured_reason(self) -> str:
        if self.configured:
            return ""
        return "未配置 wan.api_key / wan.model，自由视频不可用。"

    def options(self) -> dict:
        cfg = self.settings.config
        return options(
            configured=self.configured,
            reason=self.unconfigured_reason(),
            model=str(cfg.get("WAN_MODEL") or "wan3.0-video"),
            provider=str(cfg.get("WAN_PROVIDER") or "dashscope"),
            cos_enabled=appconfig.truthy(cfg.get("COS_ENABLED")),
        )

    def _default_client(self) -> WanClient:
        return WanClient(WanConfig.from_mapping(self.settings.config))

    def _uploader(self):
        """懒建 SourceUploader；COS 关闭时返回 None（图片走 Base64）。"""
        if self._uploader_checked:
            return self._uploader_obj
        self._uploader_checked = True
        cfg = self.settings.config
        if not appconfig.truthy(cfg.get("COS_ENABLED")):
            return None
        try:
            _template_store, output_store = build_storages(cfg)
        except Exception as exc:  # noqa: BLE001 - 配置坏了只影响参考视频，不应拖垮页面
            self._uploader_error = str(exc)
            return None
        if output_store is None:
            return None
        base = ""
        try:
            base = str(output_store.cfg.clean_prefix() or "")
        except Exception:  # noqa: BLE001 - LocalStorage 没有 cfg 时按无前缀处理
            base = ""
        # 放在 {prefix}/output/free 下：沿用现有 CAM 的 output/* 授权，不额外开目录。
        prefix = f"{base}/output/free" if base else "output/free"
        self._uploader_obj = media_mod.SourceUploader(output_store, prefix, log=self._log)
        return self._uploader_obj

    # -------------------------------------------------------------- 任务持久化
    def _path(self, task_id: str) -> Path:
        return self.tasks_dir / f"{task_id}.json"

    def _task_dir(self, task_id: str) -> Path:
        return self.files_dir / task_id

    def _persist(self, record: dict) -> None:
        """原子写；临时文件名带随机后缀，避免「取消」与工作线程同时落盘时抢同一个 .tmp。"""
        target = self._path(record["id"])
        payload = json.dumps(record, ensure_ascii=False, indent=2)
        with self._lock:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")
            try:
                tmp.write_text(payload, encoding="utf-8")
                os.replace(tmp, target)
            finally:
                if tmp.exists():
                    try:
                        tmp.unlink()
                    except OSError:
                        pass

    def _load_existing(self) -> None:
        """重启恢复：进行中的任务直接落成失败（云端可能已出片并计费）。"""
        for path in sorted(self.tasks_dir.glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(record, dict) or not record.get("id"):
                continue
            if record.get("status") in ACTIVE_STATUSES:
                record["status"] = STATUS_FAILED
                record["message"] = ("服务重启，进行中的任务已中止；云端可能已出片并计费，"
                                     "请到百炼控制台确认。")
                for shot in record.get("shots") or []:
                    if shot.get("status") in (SHOT_QUEUED, SHOT_RUNNING):
                        shot["status"] = SHOT_FAILED
                        shot["error"] = "服务重启中止"
                record["finished_at"] = record.get("finished_at") or _iso_now()
                try:
                    self._persist(record)
                except OSError:
                    pass
            self._tasks[record["id"]] = record

    # ------------------------------------------------------------------ 上传
    def add_upload(self, user: dict, temp: Path, filename: str, kind: str) -> dict:
        """登记一段参考视频 / 音频；文件已由 HTTP 层写进临时路径。"""
        kind = str(kind or "").strip().lower()
        if kind not in UPLOAD_KINDS:
            raise VideoGenError("素材类型只能是 video 或 audio。")
        spec = UPLOAD_KINDS[kind]
        suffix = Path(filename).suffix.lower()
        if suffix not in spec["suffixes"]:
            allowed = " / ".join(sorted(spec["suffixes"]))
            raise VideoGenError(f"参考{'视频' if kind == 'video' else '音频'}只支持 {allowed}。")
        size_mb = round(temp.stat().st_size / 1024 / 1024, 3)
        if size_mb > spec["max_mb"]:
            raise VideoGenError(
                f"参考素材 {size_mb:.1f}MB，超过 {spec['max_mb']}MB 上限。", 413)
        try:
            if kind == "video":
                info = media_mod.validate_video(temp)
            else:
                info = media_mod.validate_audio(temp)
        except media_mod.MediaError as exc:
            raise VideoGenError(str(exc)) from None

        upload_id = uuid.uuid4().hex
        dest = self.uploads_dir / f"{upload_id}{suffix}"
        shutil.move(str(temp), str(dest))
        meta = {
            "upload_id": upload_id,
            "owner": user["username"],
            "kind": kind,
            "name": Path(filename).name or f"{kind}{suffix}",
            "file": dest.name,
            "size_mb": size_mb,
            "created_at": _iso_now(),
            "duration": info.get("duration"),
            "width": info.get("width"),
            "height": info.get("height"),
        }
        (self.uploads_dir / f"{upload_id}.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        return {key: meta[key] for key in
                ("upload_id", "kind", "name", "size_mb", "duration", "width", "height")}

    def _resolve_upload(self, upload_id: str, user: dict, kind: str) -> dict:
        upload_id = str(upload_id or "").strip()
        if not _TASK_ID.fullmatch(upload_id):
            raise VideoGenError("参考素材已失效，请重新上传。", 404)
        meta_path = self.uploads_dir / f"{upload_id}.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise VideoGenError("参考素材已失效，请重新上传。", 404) from None
        if meta.get("kind") != kind:
            raise VideoGenError("参考素材类型不匹配。")
        if meta.get("owner") != user["username"] and user.get("role") != "admin":
            raise VideoGenError("找不到这个参考素材。", 404)
        path = self.uploads_dir / str(meta.get("file") or "")
        if not path.is_file():
            raise VideoGenError("参考素材文件已丢失，请重新上传。", 404)
        meta["path"] = str(path)
        return meta

    # -------------------------------------------------------------- 创建任务
    def create_task(self, user: dict, payload: dict, images: list[dict]) -> dict:
        if not self.configured:
            raise VideoGenError(self.unconfigured_reason(), 503)
        video_ids = self._id_list(payload.get("video_ids"), "参考视频")
        audio_ids = self._id_list(payload.get("audio_ids"), "参考音频")
        videos = [self._resolve_upload(item, user, "video") for item in video_ids]
        audios = [self._resolve_upload(item, user, "audio") for item in audio_ids]
        images = list(images or [])
        if len(images) > MAX_IMAGES:
            raise VideoGenError(f"参考图最多 {MAX_IMAGES} 张。")

        fields = validate_request(payload, image_count=len(images),
                                  video_metas=videos, audio_count=len(audios))

        task_id = uuid.uuid4().hex
        task_dir = self._task_dir(task_id)
        task_dir.mkdir(parents=True, exist_ok=True)

        refs = {"images": [], "videos": [], "audios": []}
        try:
            for index, slot in enumerate(images, 1):
                src = Path(str(slot.get("path") or ""))
                if not src.is_file():
                    raise VideoGenError(f"第 {index} 张参考图已失效，请重新上传。", 404)
                dest_name = f"image{index}{src.suffix.lower()}"
                info = media_mod.validate_image(src)
                shutil.copy2(src, task_dir / dest_name)
                refs["images"].append({
                    "index": index, "file": dest_name,
                    "name": str(slot.get("name") or src.name),
                    "size_mb": round(src.stat().st_size / 1024 / 1024, 3),
                    "width": info.get("width"), "height": info.get("height"),
                })
            for index, meta in enumerate(videos, 1):
                src = Path(meta["path"])
                dest_name = f"video{index}{src.suffix.lower()}"
                shutil.copy2(src, task_dir / dest_name)
                refs["videos"].append({
                    "index": index, "file": dest_name, "name": meta.get("name") or src.name,
                    "size_mb": meta.get("size_mb"), "duration": meta.get("duration"),
                    "width": meta.get("width"), "height": meta.get("height"),
                })
            for index, meta in enumerate(audios, 1):
                src = Path(meta["path"])
                dest_name = f"audio{index}{src.suffix.lower()}"
                shutil.copy2(src, task_dir / dest_name)
                refs["audios"].append({
                    "index": index, "file": dest_name, "name": meta.get("name") or src.name,
                    "size_mb": meta.get("size_mb"),
                })
        except Exception:
            shutil.rmtree(task_dir, ignore_errors=True)
            raise

        record = {
            "id": task_id,
            "owner": user["username"],
            "created_at": _iso_now(),
            "updated_at": _iso_now(),
            "started_at": "",
            "finished_at": "",
            "status": STATUS_QUEUED,
            "progress": f"0/{fields['count']}",
            "prompt": fields["prompt"],
            "negative_prompt": fields["negative_prompt"],
            "mode_choice": fields["mode_choice"],
            "mode": fields["mode"],
            "resolution": fields["resolution"],
            "ratio": fields["ratio"],
            "duration": fields["duration"],
            "count": fields["count"],
            "seed": fields["seed"],
            "audio": fields["audio"],
            "prompt_extend": fields["prompt_extend"],
            "watermark": fields["watermark"],
            "video_seconds": fields["video_seconds"],
            "refs": refs,
            "shots": [{"index": index, "status": SHOT_QUEUED, "file": "", "error": "",
                       "message": "", "task_id": "", "seconds": 0,
                       "billable_seconds": 0.0, "estimated_cost": None}
                      for index in range(1, fields["count"] + 1)],
            "message": "",
            "cancel_requested": False,
        }
        with self._lock:
            self._tasks[task_id] = record
            self._persist(record)
        self._ensure_workers()
        self._queue.put(task_id)
        return self.public(record, admin=self._is_admin(user), detail=True)

    @staticmethod
    def _id_list(raw, label: str) -> list[str]:
        if raw in (None, ""):
            return []
        if not isinstance(raw, list):
            raise VideoGenError(f"{label}参数无效。")
        items = [str(item or "").strip() for item in raw if str(item or "").strip()]
        if len(set(items)) != len(items):
            raise VideoGenError(f"同一段{label}不能重复提交。")
        return items

    # ------------------------------------------------------------------ 查询
    @staticmethod
    def _is_admin(user: dict) -> bool:
        return str((user or {}).get("role") or "") == "admin"

    def _authorized(self, task_id: str, user: dict) -> dict:
        task_id = str(task_id or "")
        if not _TASK_ID.fullmatch(task_id):
            raise VideoGenError("找不到这个视频任务。", 404)
        record = self._tasks.get(task_id)
        if record is None:
            try:
                record = json.loads(self._path(task_id).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raise VideoGenError("找不到这个视频任务。", 404) from None
            self._tasks[task_id] = record
        if record.get("owner") != (user or {}).get("username") and not self._is_admin(user):
            raise VideoGenError("找不到这个视频任务。", 404)
        return record

    def get_record(self, task_id: str, user: dict) -> dict:
        return self.public(self._authorized(task_id, user),
                           admin=self._is_admin(user), detail=True)

    def list_tasks(self, user: dict, *, limit: int = 40) -> list[dict]:
        admin = self._is_admin(user)
        with self._lock:
            records = list(self._tasks.values())
        if not admin:
            records = [item for item in records if item.get("owner") == user.get("username")]
        records.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        limit = max(1, min(200, int(limit or 40)))
        return [self.public(record, admin=admin, detail=True) for record in records[:limit]]

    def video_file(self, task_id: str, index: int, user: dict) -> tuple[Path, str]:
        record = self._authorized(task_id, user)
        shot = next((item for item in record.get("shots") or []
                     if int(item.get("index") or 0) == int(index)), None)
        if not shot or not shot.get("file"):
            raise VideoGenError("这条视频还没有生成。", 404)
        path = self._task_dir(record["id"]) / str(shot["file"])
        if not path.is_file():
            raise VideoGenError("视频文件已丢失。", 404)
        return path, f"{record['id']}-{int(index)}.mp4"

    def reference_file(self, task_id: str, kind: str, index: int,
                       user: dict) -> tuple[Path, str]:
        record = self._authorized(task_id, user)
        if kind not in ("images", "videos", "audios"):
            raise VideoGenError("找不到这个参考素材。", 404)
        ref = next((item for item in record.get("refs", {}).get(kind) or []
                    if int(item.get("index") or 0) == int(index)), None)
        if not ref or not ref.get("file"):
            raise VideoGenError("找不到这个参考素材。", 404)
        path = self._task_dir(record["id"]) / str(ref["file"])
        if not path.is_file():
            raise VideoGenError("参考素材已丢失。", 404)
        return path, str(ref.get("name") or path.name)

    # ------------------------------------------------------------------ 取消/删除
    def cancel_task(self, task_id: str, user: dict) -> dict:
        record = self._authorized(task_id, user)
        with self._lock:
            if record.get("status") not in ACTIVE_STATUSES:
                raise VideoGenError("这个任务已经结束，不能取消。", 409)
            record["cancel_requested"] = True
            if record.get("status") == STATUS_QUEUED:
                record["status"] = STATUS_CANCELED
                record["finished_at"] = _iso_now()
                for shot in record.get("shots") or []:
                    if shot.get("status") == SHOT_QUEUED:
                        shot["status"] = SHOT_CANCELED
            else:
                record["message"] = "正在取消：当前这一条会尽快停下（云端任务不会被撤回）。"
            record["updated_at"] = _iso_now()
            self._persist(record)
        return self.public(record, admin=self._is_admin(user), detail=True)

    def delete_task(self, task_id: str, user: dict) -> None:
        record = self._authorized(task_id, user)
        if record.get("status") in ACTIVE_STATUSES:
            raise VideoGenError("任务还在跑，先取消再删除。", 409)
        with self._lock:
            self._tasks.pop(record["id"], None)
            try:
                self._path(record["id"]).unlink(missing_ok=True)
            except OSError:
                pass
        shutil.rmtree(self._task_dir(record["id"]), ignore_errors=True)

    # ------------------------------------------------------------------ 输出
    def public(self, record: dict, *, admin: bool = False, detail: bool = True) -> dict:
        task_id = record["id"]
        shots = []
        for shot in record.get("shots") or []:
            done = bool(shot.get("file"))
            item = {
                "index": int(shot.get("index") or 0),
                "status": shot.get("status") or "",
                "error": shot.get("error") or "",
                "message": shot.get("message") or "",
                "task_id": shot.get("task_id") or "",
                "seconds": shot.get("seconds") or 0,
                "url": (f"/api/videogen/tasks/{task_id}/videos/{shot.get('index')}"
                        if done else ""),
                "download_url": (f"/api/videogen/tasks/{task_id}/videos/{shot.get('index')}?download=1"
                                 if done else ""),
            }
            if admin:
                item["billable_seconds"] = shot.get("billable_seconds") or 0
                item["estimated_cost"] = shot.get("estimated_cost")
            shots.append(item)

        out = {
            "id": task_id,
            "owner": record.get("owner") or "",
            "created_at": record.get("created_at") or "",
            "updated_at": record.get("updated_at") or "",
            "started_at": record.get("started_at") or "",
            "finished_at": record.get("finished_at") or "",
            "status": record.get("status") or "",
            "progress": record.get("progress") or "",
            "prompt": record.get("prompt") or "",
            "negative_prompt": record.get("negative_prompt") or "",
            "mode": record.get("mode") or "",
            "mode_choice": record.get("mode_choice") or "",
            "resolution": record.get("resolution") or "",
            "ratio": record.get("ratio") or "",
            "duration": record.get("duration"),
            "count": record.get("count") or 1,
            "seed": record.get("seed"),
            "audio": bool(record.get("audio")),
            "prompt_extend": bool(record.get("prompt_extend")),
            "watermark": bool(record.get("watermark")),
            "video_seconds": record.get("video_seconds"),
            "message": record.get("message") or "",
            "active": record.get("status") in ACTIVE_STATUSES,
            "cancel_requested": bool(record.get("cancel_requested")),
        }
        if detail:
            out["shots"] = shots
            out["refs"] = self._public_refs(record)
        return out

    def _public_refs(self, record: dict) -> dict:
        task_id = record["id"]
        out: dict[str, list[dict]] = {}
        for kind in ("images", "videos", "audios"):
            items = []
            for ref in record.get("refs", {}).get(kind) or []:
                index = int(ref.get("index") or 0)
                items.append({
                    "index": index,
                    "name": ref.get("name") or "",
                    "size_mb": ref.get("size_mb"),
                    "duration": ref.get("duration"),
                    "width": ref.get("width"),
                    "height": ref.get("height"),
                    "url": f"/api/videogen/tasks/{task_id}/refs/{kind}/{index}",
                })
            out[kind] = items
        return out

    # ------------------------------------------------------------------ 队列
    def _ensure_workers(self) -> None:
        if self._workers and len(self._workers) == self._worker_limit:
            return
        try:
            wanted = int(self.settings.web.get("videogen_max_inflight", 2))
        except (TypeError, ValueError):
            wanted = 2
        wanted = max(1, min(8, wanted))
        with self._lock:
            if self._workers and len(self._workers) == wanted:
                return
            self._worker_limit = wanted
            while len(self._workers) < wanted:
                index = len(self._workers) + 1
                thread = threading.Thread(target=self._loop, name=f"videogen-{index}",
                                          daemon=True)
                thread.start()
                self._workers.append(thread)

    def _loop(self) -> None:
        while True:
            try:
                task_id = self._queue.get()
            except Exception:  # noqa: BLE001 - 关闭时 Queue 不抛，兜底防线程裸崩
                return
            try:
                record = self._tasks.get(task_id)
                if record is not None:
                    self._run(record)
            except Exception as exc:  # noqa: BLE001 - 单任务异常不能带走线程
                self._log(f"videogen 任务 {task_id} 异常：{exc}")
                record = self._tasks.get(task_id)
                if record is not None and record.get("status") in ACTIVE_STATUSES:
                    self._finish(record, error=str(exc))
            finally:
                self._queue.task_done()

    # ------------------------------------------------------------------ 执行
    def _task_payload(self, record: dict) -> dict:
        task_dir = self._task_dir(record["id"])
        images = [str(task_dir / ref["file"]) for ref in record["refs"]["images"]]
        videos = [str(task_dir / ref["file"]) for ref in record["refs"]["videos"]]
        audios = [str(task_dir / ref["file"]) for ref in record["refs"]["audios"]]
        payload = {
            "prompt": record["prompt"],
            "mode": record["mode"],
            "resolution": record["resolution"],
            "aspectRatio": record["ratio"],
            "duration": record["duration"],
            "audio": bool(record["audio"]),
            "promptExtend": bool(record["prompt_extend"]),
            "watermark": bool(record["watermark"]),
        }
        if record.get("negative_prompt"):
            payload["negativePrompt"] = record["negative_prompt"]
        if record.get("seed") is not None:
            payload["seed"] = record["seed"]
        if images:
            payload["imageUrls"] = images
        if videos:
            payload["videoUrls"] = videos
        if audios:
            payload["audioUrls"] = audios
        return payload

    def _run(self, record: dict) -> None:
        if record.get("cancel_requested") or record.get("status") != STATUS_QUEUED:
            for shot in record.get("shots") or []:
                if shot.get("status") == SHOT_QUEUED:
                    shot["status"] = SHOT_CANCELED
                    shot["error"] = "已取消"
            record["status"] = STATUS_CANCELED
            self._finish(record)
            return

        record["status"] = STATUS_RUNNING
        record["started_at"] = _iso_now()
        record["updated_at"] = record["started_at"]
        self._persist(record)

        try:
            client = self._client_factory()
        except WanError as exc:
            self._fail_all(record, str(exc))
            return

        try:
            task = self._task_payload(record)
            durations = [ref.get("duration") for ref in record["refs"]["videos"]]
            body, errors, warnings = client.build_body(
                task, default_mode=record["mode"], video_durations=durations)
            if errors:
                self._fail_all(record, "参数不合法：" + "；".join(errors))
                return
            if warnings:
                record["message"] = "；".join(warnings)

            uploader = self._uploader()
            if uploader is None and (record["refs"]["videos"] or record["refs"]["audios"]):
                reason = self._uploader_error or "未启用 COS"
                self._fail_all(record, f"参考视频/音频需要开启 COS 才能上传（官方不支持 Base64）：{reason}")
                return
            budget = media_mod.base64_budget_error(
                [ref for ref in task.get("imageUrls") or []],
                cos_enabled=uploader is not None)
            if budget:
                self._fail_all(record, budget)
                return
            try:
                body = media_mod.resolve_body(body, uploader, cache={})
            except media_mod.MediaError as exc:
                self._fail_all(record, f"参考素材处理失败：{exc}")
                return
            wire = client.wire_body(body)

            for shot in record["shots"]:
                if record.get("cancel_requested"):
                    shot["status"] = SHOT_CANCELED
                    shot["error"] = "已取消"
                    self._persist(record)
                    continue
                shot["status"] = SHOT_RUNNING
                record["updated_at"] = _iso_now()
                self._persist(record)
                self._run_shot(client, wire, record, shot)
                self._persist(record)
        finally:
            self._finish(record)

    def _run_shot(self, client: WanClient, wire: dict, record: dict, shot: dict) -> None:
        started = time.monotonic()
        try:
            # 云端在飞：单条视频走「快速通道」池，和大批量主池隔开。
            with global_small_inflight():
                submit = client.submit(wire)
                shot["task_id"] = submit.task_id
                record["updated_at"] = _iso_now()
                self._persist(record)
                result = client.wait_task(
                    submit.task_id,
                    should_stop=lambda: bool(record.get("cancel_requested")))
            if result.status != "succeeded" or not result.video_url:
                raise VideoGenError(result.message or f"云端返回 {result.status}", 502)
            seconds, cost = estimate_cost(result.usage or {}, record["resolution"],
                                          self.settings.price_map())
            shot["billable_seconds"] = seconds
            shot["estimated_cost"] = cost
            dest = self._task_dir(record["id"]) / f"shot-{shot['index']}.mp4"
            self._download(result.video_url, dest)
            if not dest.is_file() or dest.stat().st_size <= 0:
                raise VideoGenError("视频下载失败：文件为空。", 502)
            shot["file"] = dest.name
            shot["status"] = SHOT_DONE
            shot["error"] = ""
        except WaitStopped:
            shot["status"] = SHOT_CANCELED
            shot["error"] = "已取消"
        except TaskCanceled:
            shot["status"] = SHOT_CANCELED
            shot["error"] = "云端任务已取消"
        except TaskExpired:
            shot["status"] = SHOT_FAILED
            shot["error"] = "云端任务已过期/查不到；可稍后用 task_id 到百炼控制台确认。"
        except WaitTimeout as exc:
            shot["status"] = SHOT_FAILED
            shot["error"] = (f"等待超时（{exc}）；云端可能已出片并计费，"
                             "请到百炼控制台按 task_id 确认。")
        except (WanError, VideoGenError, media_mod.MediaError) as exc:
            shot["status"] = SHOT_FAILED
            shot["error"] = str(exc)
        except Exception as exc:  # noqa: BLE001 - 未预期异常也要落到任务记录里
            shot["status"] = SHOT_FAILED
            shot["error"] = f"生成失败：{exc}"
        finally:
            shot["seconds"] = round(time.monotonic() - started, 1)

    def _finish(self, record: dict, *, error: str = "") -> None:
        shots = record.get("shots") or []
        done = sum(1 for shot in shots if shot.get("status") == SHOT_DONE)
        canceled = sum(1 for shot in shots if shot.get("status") == SHOT_CANCELED)
        record["progress"] = f"{done}/{len(shots)}"
        if error and not done:
            record["status"] = STATUS_FAILED
            record["message"] = error
        elif done == len(shots) and done > 0:
            record["status"] = STATUS_DONE
        elif done > 0:
            record["status"] = STATUS_PARTIAL
        elif canceled == len(shots) and canceled > 0:
            record["status"] = STATUS_CANCELED
        else:
            record["status"] = STATUS_FAILED
            if not record.get("message"):
                first_error = next((shot.get("error") for shot in shots if shot.get("error")), "")
                record["message"] = first_error or "生成失败"
        record["finished_at"] = _iso_now()
        record["updated_at"] = record["finished_at"]
        try:
            self._persist(record)
        except OSError as exc:
            self._log(f"videogen 任务 {record.get('id')} 写盘失败：{exc}")

    def _fail_all(self, record: dict, message: str) -> None:
        for shot in record.get("shots") or []:
            if shot.get("status") in (SHOT_QUEUED, SHOT_RUNNING):
                shot["status"] = SHOT_FAILED
                shot["error"] = message
        record["message"] = message
        self._finish(record)

    def _log(self, message: str) -> None:
        print(f"[videogen] {message}")

    # ------------------------------------------------------------------ 关闭
    def close(self) -> None:
        """测试/退出用：不再接新任务，等队列里的任务自然结束。"""
        self._closed = True
        while not self._queue.empty():
            time.sleep(0.02)
