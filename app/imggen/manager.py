# -*- coding: utf-8 -*-
"""生图任务管理器：排队 → 调接口 → 成品落盘 → 供前端轮询。

与桌面版的三处差别（都是有意的）：
1. 任务**落盘**到 `web-data/imagegen/tasks/<id>.json`，服务重启后历史还在
   （桌面版只存内存，关窗即丢）。重启时残留的「排队中/生成中」会被标成失败并说明原因。
2. 参考图在**创建任务时复制**进任务目录：uploads 有 GC、用户也可能删图，
   复制后任务随时可复现（重跑、查证据）。
3. 没有登录页：调用方已由网页会话鉴权，任务按 owner 隔离；管理员可看全部。

目录布局（都随 data_dir 迁移，记录里只存相对路径）：
  web-data/imagegen/tasks/<task_id>.json
  web-data/imagegen/files/<task_id>/1.png（或 1.jpg，按实际字节判断）+ 1.meta.json
  web-data/imagegen/files/<task_id>/ref1.png
"""
from __future__ import annotations

import json
import queue
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable

from . import gemini
from .api import edit_image, generate_image
from .errors import explain_error
from .models import GEMINI_SIZE_BY_VALUE, GEMINI_SIZE_SPECS, IMAGE_MODEL_IDS, \
    image_model_label, image_model_provider, image_model_spec, image_quality_options, \
    image_request_parameters, is_gemini_model, supports_image_parameters
from .processing import api_size_for_output, finalize_output_image, output_spec_label, \
    output_specs_for_model

PROMPT_LIMIT = 4000
COUNT_CAP = 8
REFERENCE_CAP = 10

STATUS_QUEUED = "排队中"
STATUS_RUNNING = "生成中"
STATUS_DONE = "已完成"
STATUS_PARTIAL = "部分完成"
STATUS_FAILED = "生成失败"
ACTIVE_STATUSES = (STATUS_QUEUED, STATUS_RUNNING)

DEFAULT_CONFIG = {
    "enabled": True,
    "base_url": "https://api2.laozhang.ai/v1/",
    "api_key": "",
    "gemini_base_url": "",
    "model": "gpt-image-2-vip",
    "default_quality": "high",
    "timeout_seconds": 360,
    "max_count": 4,
    "max_references": REFERENCE_CAP,
    "max_inflight": 3,
    "price_per_image": "0.03",
}


class ImageGenError(ValueError):
    """带 HTTP 语义的业务错误：status 由路由层直接写进响应。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = int(status)


def _as_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


class ImageGenManager:
    def __init__(self, settings, *, root: Path | None = None):
        self.settings = settings
        self.config = {**DEFAULT_CONFIG, **(getattr(settings, "imagegen", None) or {})}
        self.root = Path(root) if root is not None else Path(settings.data_dir) / "imagegen"
        self.tasks_dir = self.root / "tasks"
        self.files_dir = self.root / "files"
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.files_dir.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self._tasks: dict[str, dict] = {}
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._workers: list[threading.Thread] = []
        self._started = False
        # 测试可替换：签名与 api.generate_image / api.edit_image 一致
        self._generate: Callable[..., bytes] = generate_image
        self._edit: Callable[..., bytes] = edit_image
        # Gemini 原生协议（Nano Banana）单独一套，测试同样可替换
        self._gemini_generate: Callable[..., bytes] = gemini.generate_image
        self._gemini_edit: Callable[..., bytes] = gemini.edit_image
        self._load_existing()

    # ------------------------------------------------------------ 配置
    @property
    def base_url(self) -> str:
        return str(self.config.get("base_url") or "").strip()

    @property
    def gemini_base_url(self) -> str:
        """Gemini 原生入口的主机；默认复用 base_url，由 gemini.native_endpoint 去掉 /v1。"""
        return str(self.config.get("gemini_base_url") or "").strip() or self.base_url

    @property
    def api_key(self) -> str:
        return str(self.config.get("api_key") or "").strip()

    @property
    def default_model(self) -> str:
        model = str(self.config.get("model") or "").strip()
        return model if model in IMAGE_MODEL_IDS else IMAGE_MODEL_IDS[0]

    @property
    def configured(self) -> bool:
        enabled = self.config.get("enabled", True)
        if isinstance(enabled, str):
            enabled = enabled.strip().lower() not in ("0", "false", "no", "off")
        return bool(enabled and self.api_key and self.base_url)

    @property
    def max_count(self) -> int:
        return max(1, min(COUNT_CAP, _as_int(self.config.get("max_count"), 4)))

    @property
    def max_references(self) -> int:
        return max(0, min(REFERENCE_CAP, _as_int(self.config.get("max_references"), REFERENCE_CAP)))

    @property
    def max_inflight(self) -> int:
        return max(1, min(8, _as_int(self.config.get("max_inflight"), 3)))

    @property
    def timeout(self) -> int:
        return max(30, min(900, _as_int(self.config.get("timeout_seconds"), 300)))

    def unconfigured_reason(self) -> str:
        if not self.config.get("enabled", True):
            return "生图功能已关闭（server/imagegen.json → enabled=false）。"
        if not self.base_url:
            return "生图接口地址未配置：server/imagegen.json → base_url。"
        if not self.api_key:
            return "生图密钥未配置：在 server/imagegen.json 里填 api_key（老张 API Key）。"
        return ""

    def options(self) -> dict[str, Any]:
        """一次性把「模型 → 可用尺寸/质量」交给前端，避免两边各写一份规则。"""
        models = []
        for model_id in IMAGE_MODEL_IDS:
            spec = image_model_spec(model_id)
            models.append({
                "id": model_id,
                "label": image_model_label(model_id),
                "provider": image_model_provider(model_id),
                "price": spec.price_usd if spec is not None else "",
                "sizes": [{"value": value, "label": label}
                          for value, label in _size_options(model_id)],
                "qualities": [{"value": value, "label": label}
                              for value, label in _quality_pairs(model_id)],
            })
        default_model = self.default_model
        default_quality = str(self.config.get("default_quality") or "high")
        if default_quality not in dict(_quality_pairs(default_model)):
            default_quality = "auto"
        return {
            "configured": self.configured,
            "reason": self.unconfigured_reason(),
            "models": models,
            "default_model": default_model,
            "default_quality": default_quality,
            "max_count": self.max_count,
            "max_references": self.max_references,
            "price": str(self.config.get("price_per_image") or ""),
            "prompt_limit": PROMPT_LIMIT,
        }

    # ------------------------------------------------------------ 任务创建
    def create_task(self, user: dict, payload: dict,
                    references: Iterable[dict] = ()) -> dict:
        if not self.configured:
            raise ImageGenError(self.unconfigured_reason() or "生图功能未配置。", 503)

        username = str((user or {}).get("username") or "").strip()
        if not username:
            raise ImageGenError("请先登录。", 401)

        prompt = str(payload.get("prompt") or "").strip()
        if not prompt:
            raise ImageGenError("请先填写提示词。")
        if len(prompt) > PROMPT_LIMIT:
            raise ImageGenError(f"提示词过长（{len(prompt)} 字），上限 {PROMPT_LIMIT} 字。")

        model = str(payload.get("model") or "").strip() or self.default_model
        if model not in IMAGE_MODEL_IDS:
            raise ImageGenError("不支持的图片模型。")

        allowed_sizes = {value for value, _ in _size_options(model)}
        size = str(payload.get("size") or "auto").strip() or "auto"
        if size not in allowed_sizes:
            raise ImageGenError("所选模型不支持这个输出尺寸。")

        # 质量：没传就用配置默认；默认值对该型号不适用时退到它的第一档
        # （Nano Banana 没有 quality 概念，只有 auto）。显式传了非法值仍然报错。
        allowed_qualities = image_quality_options(model)
        requested_quality = str(payload.get("quality") or "").strip()
        quality = requested_quality or str(self.config.get("default_quality") or "").strip()
        if quality not in allowed_qualities:
            if requested_quality:
                raise ImageGenError("所选模型不支持此图片质量。")
            quality = allowed_qualities[0]
        try:
            image_request_parameters(model, size, quality)
        except ValueError as exc:
            raise ImageGenError(str(exc)) from exc

        count = _as_int(payload.get("count"), 1)
        count = max(1, min(self.max_count, count))

        refs = [dict(item) for item in references]
        if len(refs) > self.max_references:
            raise ImageGenError(f"参考图最多 {self.max_references} 张。")

        task_id = uuid.uuid4().hex
        now = time.time()
        record: dict[str, Any] = {
            "id": task_id,
            "owner": username,
            "created_at": now,
            "updated_at": now,
            "started_at": 0.0,
            "finished_at": 0.0,
            "elapsed_ms": 0,
            "prompt": prompt,
            "model": model,
            "model_label": image_model_label(model),
            "size": size,
            "size_label": _size_label(model, size),
            "quality": quality,
            "count": count,
            "api_size": _api_size(model, size),
            "status": STATUS_QUEUED,
            "progress": 0,
            "error": "",
            "references": [],
            "images": [],
        }

        task_dir = self.files_dir / task_id
        try:
            task_dir.mkdir(parents=True, exist_ok=False)
            for index, ref in enumerate(refs, start=1):
                source = Path(str(ref.get("path") or ""))
                if not source.is_file():
                    raise ImageGenError("参考图已失效，请重新上传。", 404)
                suffix = source.suffix.lower() or ".png"
                target = task_dir / f"ref{index}{suffix}"
                shutil.copyfile(source, target)
                record["references"].append({
                    "index": index,
                    "name": str(ref.get("name") or source.name),
                    "upload_id": str(ref.get("upload_id") or ""),
                    "file": target.name,
                })
        except Exception:
            shutil.rmtree(task_dir, ignore_errors=True)
            raise

        with self._lock:
            self._tasks[task_id] = record
            self._persist(record)
        self._queue.put(task_id)
        self._ensure_workers()
        return self.public(record)

    # ------------------------------------------------------------ 查询
    def list_tasks(self, user: dict, *, limit: int = 40) -> list[dict]:
        username = str((user or {}).get("username") or "")
        is_admin = (user or {}).get("role") == "admin"
        with self._lock:
            records = [record for record in self._tasks.values()
                       if is_admin or record.get("owner") == username]
            records.sort(key=lambda item: item.get("created_at") or 0, reverse=True)
            selected = records[:max(1, min(200, limit))]
        return [self.public(record) for record in selected]

    def get_record(self, task_id: str, user: dict) -> dict:
        record = self._authorized(task_id, user)
        return self.public(record)

    def public(self, record: dict) -> dict:
        task_id = record["id"]
        images = []
        for item in record.get("images") or []:
            index = int(item.get("index") or 0)
            images.append({
                "index": index,
                "name": item.get("name") or "",
                "width": item.get("width"),
                "height": item.get("height"),
                "size_mb": item.get("size_mb"),
                "created_at": item.get("created_at"),
                "url": f"/api/imagegen/tasks/{task_id}/images/{index}",
                "download_url": f"/api/imagegen/tasks/{task_id}/images/{index}?download=1",
            })
        references = [
            {"index": int(item.get("index") or 0), "name": item.get("name") or "",
             "url": f"/api/imagegen/tasks/{task_id}/references/{int(item.get('index') or 0)}"}
            for item in record.get("references") or []
        ]
        return {
            "id": task_id,
            "owner": record.get("owner"),
            "created_at": record.get("created_at"),
            "updated_at": record.get("updated_at"),
            "prompt": record.get("prompt"),
            "model": record.get("model"),
            "model_label": record.get("model_label"),
            "size": record.get("size"),
            "size_label": record.get("size_label"),
            "quality": record.get("quality"),
            "count": record.get("count"),
            "api_size": record.get("api_size"),
            "status": record.get("status"),
            "progress": record.get("progress") or 0,
            "error": record.get("error") or "",
            "elapsed_ms": record.get("elapsed_ms") or 0,
            "images": images,
            "references": references,
            "active": record.get("status") in ACTIVE_STATUSES,
        }

    def image_file(self, task_id: str, index: int, user: dict,
                   *, reference: bool = False) -> tuple[Path, str]:
        record = self._authorized(task_id, user)
        task_dir = self.files_dir / record["id"]
        if reference:
            for item in record.get("references") or []:
                if int(item.get("index") or 0) == index:
                    name = str(item.get("file") or "")
                    path = (task_dir / name).resolve()
                    if task_dir.resolve() not in path.parents or not path.is_file():
                        raise ImageGenError("找不到这张参考图。", 404)
                    return path, str(item.get("name") or path.name)
            raise ImageGenError("找不到这张参考图。", 404)
        for item in record.get("images") or []:
            if int(item.get("index") or 0) == index:
                path = (task_dir / str(item.get("file") or "")).resolve()
                if task_dir.resolve() not in path.parents or not path.is_file():
                    raise ImageGenError("图片文件已丢失。", 404)
                return path, str(item.get("name") or path.name)
        raise ImageGenError("找不到这张图片。", 404)

    def delete_task(self, task_id: str, user: dict) -> None:
        with self._lock:
            record = self._authorized(task_id, user)
            if record.get("status") in ACTIVE_STATUSES:
                raise ImageGenError("任务还在生成中，请等它跑完再删。", 409)
            self._tasks.pop(task_id, None)
            (self.tasks_dir / f"{task_id}.json").unlink(missing_ok=True)
        shutil.rmtree(self.files_dir / task_id, ignore_errors=True)

    def _authorized(self, task_id: str, user: dict) -> dict:
        task_id = str(task_id or "").strip()
        with self._lock:
            record = self._tasks.get(task_id)
        if record is None:
            raise ImageGenError("找不到这个生图任务。", 404)
        if (user or {}).get("role") != "admin" and record.get("owner") != (user or {}).get("username"):
            # 与仓库其它接口一致：别人的任务一律当不存在，不泄露 id 是否有效
            raise ImageGenError("找不到这个生图任务。", 404)
        return record

    # ------------------------------------------------------------ 执行
    def _ensure_workers(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            for number in range(self.max_inflight):
                worker = threading.Thread(target=self._loop, name=f"imagegen-{number + 1}",
                                          daemon=True)
                worker.start()
                self._workers.append(worker)

    def _loop(self) -> None:
        while True:
            task_id = self._queue.get()
            try:
                with self._lock:
                    record = self._tasks.get(task_id)
                if record is None:
                    continue
                self._run(record)
            except Exception:  # 兜底：worker 不能因为单条任务崩掉
                with self._lock:
                    record = self._tasks.get(task_id)
                    if record is not None and record.get("status") in ACTIVE_STATUSES:
                        record["status"] = STATUS_FAILED
                        record["error"] = "生图线程发生未预期异常，请重试。"
                        self._persist(record)
            finally:
                self._queue.task_done()

    def _run(self, record: dict) -> None:
        started = time.time()
        with self._lock:
            record["status"] = STATUS_RUNNING
            record["started_at"] = started
            record["error"] = ""
            record["progress"] = 0
            self._persist(record)

        prompt = record["prompt"]
        task_dir = self.files_dir / record["id"]
        reference_paths = [task_dir / str(item.get("file")) for item in record.get("references") or []]

        for index in range(1, int(record.get("count") or 1) + 1):
            with self._lock:
                record["progress"] = index - 1
                self._persist(record)
            try:
                if is_gemini_model(record["model"]):
                    data = self._call_gemini(record, prompt, reference_paths)
                elif reference_paths:
                    data = self._edit(
                        prompt, record["api_size"], reference_paths, self.api_key,
                        base_url=self.base_url, model=record["model"],
                        quality=record["quality"], timeout=self.timeout,
                    )
                else:
                    data = self._generate(
                        prompt, record["api_size"], self.api_key,
                        base_url=self.base_url, model=record["model"],
                        quality=record["quality"], timeout=self.timeout,
                    )
            except Exception as exc:
                self._fail(record, index, exc)
                return

            try:
                final_bytes, (width, height) = finalize_output_image(data, record["size"])
            except Exception as exc:
                self._fail(record, index, exc)
                return

            extension = _sniff_extension(final_bytes)
            filename = f"{index}{extension}"
            target = task_dir / filename
            try:
                target.write_bytes(final_bytes)
                meta = {
                    "task_id": record["id"],
                    "index": index,
                    "prompt": prompt,
                    "model": record["model"],
                    "size": record["size"],
                    "api_size": record["api_size"],
                    "quality": record["quality"],
                    "output_size": [width, height],
                    "reference_images": [str(item.get("file")) for item in record.get("references") or []],
                    "created_at": time.time(),
                    "operator": record.get("owner"),
                }
                (task_dir / f"{index}.meta.json").write_text(
                    json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
            except OSError as exc:
                self._fail(record, index, exc)
                return

            with self._lock:
                record["images"].append({
                    "index": index,
                    "file": filename,
                    "name": f"{record['id'][:8]}-{index}{extension}",
                    "width": width,
                    "height": height,
                    "size_mb": round(len(final_bytes) / (1024 * 1024), 2),
                    "created_at": time.time(),
                })
                record["progress"] = index
                self._persist(record)

        with self._lock:
            record["status"] = STATUS_DONE
            record["finished_at"] = time.time()
            record["elapsed_ms"] = int((record["finished_at"] - started) * 1000)
            self._persist(record)

    def _call_gemini(self, record: dict, prompt: str, reference_paths: list[Path]) -> bytes:
        """Nano Banana：尺寸传「比例@档位」原值，不走 api_size 那套像素换算。"""
        common = {
            "base_url": self.gemini_base_url,
            "model": record["model"],
            "quality": record.get("quality") or "auto",
            "timeout": self.timeout,
        }
        if reference_paths:
            return self._gemini_edit(prompt, record["size"], reference_paths,
                                     self.api_key, **common)
        return self._gemini_generate(prompt, record["size"], self.api_key, **common)

    def _fail(self, record: dict, index: int, exc: BaseException) -> None:
        message = explain_error(exc)
        with self._lock:
            record["error"] = f"第 {index} 张：{message}" if record.get("images") else message
            record["status"] = STATUS_PARTIAL if record.get("images") else STATUS_FAILED
            record["finished_at"] = time.time()
            if record.get("started_at"):
                record["elapsed_ms"] = int((record["finished_at"] - record["started_at"]) * 1000)
            self._persist(record)

    # ------------------------------------------------------------ 持久化
    def _persist(self, record: dict) -> None:
        record["updated_at"] = time.time()
        path = self.tasks_dir / f"{record['id']}.json"
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            # 落盘失败不该让已经生成的图白跑：内存记录仍然可查
            pass

    def _load_existing(self) -> None:
        for path in sorted(self.tasks_dir.glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(record, dict) or not record.get("id"):
                continue
            if record.get("status") in ACTIVE_STATUSES:
                # 上一个进程死掉时线程已消失：别让它永远显示「生成中」
                record["status"] = STATUS_PARTIAL if record.get("images") else STATUS_FAILED
                record["error"] = record.get("error") or "服务重启，任务中断，请重新提交。"
                record["finished_at"] = record.get("finished_at") or time.time()
            self._tasks[record["id"]] = record


def _sniff_extension(data: bytes) -> str:
    """按文件魔数决定扩展名。

    Gemini（Nano Banana）原生接口回来的常是 `image/jpeg`，GPT Image 是 PNG；
    之前一律写成 `.png` 会让静态服务按 image/png 发 JPEG，下载后的后缀也是错的。
    """
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:3] == b"GIF":
        return ".gif"
    return ".png"


def _quality_pairs(model: str) -> list[tuple[str, str]]:
    from .models import QUALITY_LABELS
    return [(value, QUALITY_LABELS.get(value, value)) for value in image_quality_options(model)]


def _size_options(model: str) -> list[tuple[str, str]]:
    """尺寸下拉：Images 协议给像素规格，Gemini 给「比例@档位」。"""
    if is_gemini_model(model):
        return [(spec.value, spec.label) for spec in GEMINI_SIZE_SPECS]
    return [(spec.value, spec.label) for spec in output_specs_for_model(model)]


def _size_label(model: str, value: str) -> str:
    if is_gemini_model(model):
        spec = GEMINI_SIZE_BY_VALUE.get(value)
        return spec.label if spec is not None else value
    return output_spec_label(value)


def _api_size(model: str, value: str) -> str:
    """记进任务里的「实际请求尺寸」：Gemini 记 `2K（4:5）`，Images 记像素。"""
    if is_gemini_model(model):
        spec = GEMINI_SIZE_BY_VALUE.get(value)
        if spec is None or not spec.image_size:
            return "auto"
        return f"{spec.image_size}（{spec.aspect_ratio}）"
    if not supports_image_parameters(model):
        return "auto"
    return api_size_for_output(value, model)
