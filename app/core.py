#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""换装批量生产器 · 核心引擎（不依赖界面，可单独测试）。

职责：模板清单 → 任务规划 → 上传服装图 → 并发提交 → 轮询 → 下载 → 校验 → 记账落盘。
界面层只负责调用 run() 并接收事件回调。

API 协议在 app/wan_api.py（官方 DashScope / 旧 RunningHub 双通道），
素材上传在 app/media.py（COS 源目录 / Base64 兜底），本文件只编排。
"""

from __future__ import annotations

import csv
import json
import random
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

try:                      # 作为包导入（测试/界面）
    from .storage import Storage, file_fingerprint, object_key
    from .wan_api import (
        STATUS_CANCELED, STATUS_FAILED, STATUS_SUCCEEDED, STATUS_UNKNOWN,
        TaskCanceled, TaskExpired, WaitTimeout, WanClient, WanConfig, WanError,
        estimate_cost,
    )
    from .media import MediaError, SourceUploader, describe_media_source, resolve_body, resolve_image
except ImportError:       # 直接 python3 app/core.py 时
    from storage import Storage, file_fingerprint, object_key  # type: ignore
    from wan_api import (  # type: ignore
        STATUS_CANCELED, STATUS_FAILED, STATUS_SUCCEEDED, STATUS_UNKNOWN,
        TaskCanceled, TaskExpired, WaitTimeout, WanClient, WanConfig, WanError,
        estimate_cost,
    )
    from media import (  # type: ignore
        MediaError, SourceUploader, describe_media_source, resolve_body, resolve_image,
    )

DEFAULT_PROMPT = (
    "编辑视频1：保持视频1中人物的面部、发型、肤色、动作、背景与镜头不变，"
    "仅将人物身上服装替换为图1中无头模特展示的这套服装（版型、颜色、材质、细节一致），"
    "人物头部完整、面部清晰、颈部衔接自然，画面自然真实，布料质感清晰"
)

GROUP_SIZE = 100
TOTAL_TEMPLATES = 500


class EngineError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# 模板清单
# ---------------------------------------------------------------------------

def load_manifest(source: str, timeout: int = 15) -> list[dict]:
    """source 可以是 https URL、本地文件路径。返回 [{id, url, key, duration}]。"""
    if source.startswith(("http://", "https://")):
        try:
            with urllib.request.urlopen(source, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8-sig")
        except (urllib.error.URLError, OSError) as exc:
            raise EngineError(f"模板清单下载失败：{exc}") from None
    else:
        path = Path(source)
        if not path.exists():
            raise EngineError(f"模板清单不存在：{source}")
        raw = path.read_text(encoding="utf-8-sig")

    data = json.loads(raw)
    items = data.get("templates") if isinstance(data, dict) else data
    if not isinstance(items, list) or not items:
        raise EngineError("模板清单格式不对，应为数组或 {templates:[...]}")

    out = []
    for idx, item in enumerate(items):
        if isinstance(item, str):
            item = {"id": f"{idx + 1:03d}", "url": item}
        tid = str(item.get("id") or f"{idx + 1:03d}").zfill(3)
        url = (item.get("url") or "").strip()
        key = (item.get("key") or "").strip().lstrip("/")
        if not url and not key:
            raise EngineError(f"模板 {tid} 缺少 url（公有桶）或 key（私有桶）")
        out.append({"id": tid, "url": url, "key": key, "duration": item.get("duration")})
    return out


def plan_batch(templates: list[dict], mode: str, group: int | None = None,
               count: int = GROUP_SIZE, rng: random.Random | None = None) -> list[dict]:
    """mode: group（按第 N 组取 100 条）/ random（随机取 count 条）/ first（取前 count 条）。"""
    rng = rng or random.Random()
    if mode == "group":
        if not group:
            raise EngineError("未指定分组")
        start = (group - 1) * GROUP_SIZE
        picked = templates[start:start + GROUP_SIZE]
        if len(picked) < GROUP_SIZE:
            raise EngineError(f"第 {group} 组模板不足 {GROUP_SIZE} 个（清单共 {len(templates)} 个）")
    elif mode == "random":
        picked = rng.sample(templates, min(count, len(templates)))
    elif mode == "first":
        picked = templates[:count]
    else:
        raise EngineError(f"未知的分组方式：{mode}")
    return picked


# ---------------------------------------------------------------------------
# HTTP / 本地工具
# ---------------------------------------------------------------------------

def download_file(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "outfit-batch/1.0", "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=600) as resp, open(tmp, "wb") as fh:
        shutil.copyfileobj(resp, fh)
    tmp.replace(dest)


def probe_duration(path: Path) -> float | None:
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    try:
        return float(out.stdout.strip())
    except (ValueError, AttributeError):
        return None


def _as_seconds(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


# ---------------------------------------------------------------------------
# 批量执行
# ---------------------------------------------------------------------------

@dataclass
class BatchSpec:
    batch_id: str
    image_path: str
    prompt: str
    out_dir: Path
    templates: list[dict]
    api_key: str
    base: str = ""
    provider: str = "dashscope"
    model: str = "wan3.0-video"
    mode: str = "edit"
    resolution: str = "720P"
    duration: str | int | None = None
    aspect_ratio: str | None = None
    concurrency: int = 3
    poll_interval: float = 15.0
    poll_timeout: int = 1800
    expect_seconds: float = 7.0
    # 可选：产物同步到对象存储（None = 只落本地）
    storage: Storage | None = None
    # 可选：模板视频所在桶（清单里写 key 时用它签名）；缺省复用 storage
    template_storage: Storage | None = None
    archive_prefix: str = "output"
    archive_expires: int = 7 * 24 * 3600
    # 服装图源目录：默认 {COS_PREFIX}/output/source
    source_prefix: str = ""
    source_expires: int = 7 * 24 * 3600
    price_per_second: dict = field(default_factory=dict)
    # 网页多批次共享全局提交并发上限；桌面默认 None，不改变原行为。
    submit_semaphore: threading.Semaphore | None = None


@dataclass
class BatchState:
    spec: BatchSpec
    records: dict = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self) -> dict:
        with self.lock:
            billable = 0.0
            estimated = 0.0
            for rec in self.records.values():
                try:
                    billable += float(rec.get("billable_seconds") or 0)
                except (TypeError, ValueError):
                    pass
                try:
                    estimated += float(rec.get("estimated_cost") or 0)
                except (TypeError, ValueError):
                    pass
            return {
                "batch_id": self.spec.batch_id,
                "started": getattr(self, "started", ""),
                "provider": self.spec.provider,
                "model": self.spec.model,
                "mode": self.spec.mode,
                "concurrency": self.spec.concurrency,
                "prompt": self.spec.prompt,
                "billable_seconds": round(billable, 2),
                "estimated_cost": round(estimated, 2) if estimated else None,
                "records": list(self.records.values()),
            }


class BatchRunner:
    """一条批次 = 一个实例。run() 阻塞，直到跑完 / 被停止。"""

    def __init__(self, spec: BatchSpec, on_event=None, log=None):
        self.spec = spec
        self.on_event = on_event or (lambda evt: None)
        self.log = log or (lambda msg: None)
        self.pause_event = threading.Event()
        self.pause_event.set()          # set = 运行中
        self.stop_event = threading.Event()
        self.state = BatchState(spec)
        self.state.started = time.strftime("%Y-%m-%d %H:%M:%S")
        self._image_url: str | None = None
        self._upload_lock = threading.Lock()
        self._save_lock = threading.Lock()
        self._media_cache: dict = {}
        self._client = WanClient(self._wan_config())
        # 本地目录后端只用于测试/离线演练，签名出来是 file://，官方拉不到；
        # 这种场景直接走 Base64，避免把 file:// 塞进请求体。
        remote_storage = (
            spec.storage is not None
            and str(getattr(spec.storage.cfg, "provider", "")).lower() != "local"
        )
        self._uploader = (
            SourceUploader(
                spec.storage, spec.source_prefix,
                expires=spec.source_expires, log=self.log,
            )
            if remote_storage else None
        )

    def _wan_config(self) -> WanConfig:
        spec = self.spec
        return WanConfig(
            provider=spec.provider,
            api_key=spec.api_key,
            base_url=spec.base,
            model=spec.model,
            default_mode=spec.mode,
            default_duration=spec.duration,
            default_ratio=spec.aspect_ratio,
            poll_interval=spec.poll_interval,
            poll_timeout=spec.poll_timeout,
            max_concurrency=spec.concurrency,
            price_per_second=spec.price_per_second,
        )

    # ---- 控制 ----
    def pause(self) -> None:
        self.pause_event.clear()
        self.log("已暂停（正在跑的几条会先跑完）")

    def resume(self) -> None:
        self.pause_event.set()
        self.log("已继续")

    def stop(self) -> None:
        self.stop_event.set()
        self.pause_event.set()
        self.log("正在停止…")

    # ---- 主流程 ----
    def run(self) -> dict:
        spec = self.spec
        spec.out_dir.mkdir(parents=True, exist_ok=True)
        self._prepare_templates()
        self._ensure_image()
        self._save_state()

        total = len(spec.templates)
        done = 0
        self._emit("started", total=total)

        with ThreadPoolExecutor(max_workers=max(1, spec.concurrency)) as pool:
            futures = {pool.submit(self._run_one, tpl): tpl for tpl in spec.templates}
            for fut in as_completed(futures):
                tpl = futures[fut]
                try:
                    rec = fut.result()
                except Exception as exc:  # 单条异常不影响整批
                    rec = {"id": tpl["id"], "status": "failed", "error": f"{type(exc).__name__}: {exc}"}
                with self.state.lock:
                    self.state.records[tpl["id"]] = rec
                done += 1
                self._save_state()
                self._emit("progress", done=done, total=total, record=rec)

        self._export_csv()
        self._save_state()
        summary = {
            "total": total,
            "success": sum(1 for r in self.state.records.values() if r.get("status") == "success"),
            "failed": sum(1 for r in self.state.records.values() if r.get("status") == "failed"),
            "running": sum(1 for r in self.state.records.values() if r.get("status") == "running"),
            "expired": sum(1 for r in self.state.records.values() if r.get("status") == "expired"),
            "canceled": sum(1 for r in self.state.records.values() if r.get("status") == "canceled"),
            "billable_seconds": round(sum(
                float(r.get("billable_seconds") or 0) for r in self.state.records.values()
            ), 2),
            "estimated_cost": self._total_estimated_cost(),
            "cost": self._total_estimated_cost(),  # 兼容旧界面字段
            "out_dir": str(spec.out_dir),
            "provider": spec.provider,
            "model": spec.model,
            "archived": sum(1 for r in self.state.records.values() if r.get("cos_key")),
            "archive_failed": sum(1 for r in self.state.records.values() if r.get("archive_error")),
        }
        self._emit("finished", summary=summary)
        return summary

    def _prepare_templates(self) -> None:
        """清单里写的是对象 key（私有桶）时，开工前统一签成有效 URL。"""
        pending = [t for t in self.spec.templates if not t.get("url") and t.get("key")]
        if not pending:
            return
        signer = self.spec.template_storage or self.spec.storage
        if signer is None:
            raise EngineError(
                "模板清单用的是 COS 对象 key，需要签名才能给模型访问，"
                "但当前没有启用 COS。"
            )
        self.log(f"为 {len(pending)} 个模板生成签名链接（24 小时有效）")
        for tpl in pending:
            try:
                tpl["url"] = signer.sign_url(tpl["key"], 24 * 3600)
            except Exception as exc:  # noqa: BLE001
                raise EngineError(f"模板 {tpl['id']} 签名失败：{exc}") from None

    def _ensure_image(self) -> None:
        with self._upload_lock:
            if self._image_url:
                return
            self.log(f"准备服装图：{Path(self.spec.image_path).name}")
            try:
                self._image_url = resolve_image(self.spec.image_path, self._uploader)
            except MediaError as exc:
                raise EngineError(str(exc)) from None
            self.log(f"服装图已就绪（{describe_media_source(self._image_url)}），{len(self.spec.templates)} 条任务共用")

    def _run_one(self, tpl: dict) -> dict:
        spec = self.spec
        tid = tpl["id"]
        out_file = spec.out_dir / f"{tid}.mp4"
        template_seconds = _as_seconds(tpl.get("duration"))
        rec = {"id": tid, "template": tpl["url"], "status": "pending",
               "provider": spec.provider, "model": spec.model, "mode": spec.mode,
               "resolution": spec.resolution, "file": str(out_file),
               "billable_seconds": None, "estimated_cost": None, "cost": None,
               "seconds": None, "error": None}

        self.pause_event.wait()
        if self.stop_event.is_set():
            rec["status"] = "skipped"
            return rec

        task = {
            "id": tid,
            "mode": spec.mode,
            "prompt": spec.prompt,
            "imageUrls": [self._image_url],
            "videoUrls": [tpl["url"]],
            "resolution": spec.resolution,
            "audio": False,
            "promptExtend": False,
            "watermark": False,
        }
        if spec.duration is not None:
            task["duration"] = spec.duration
        if spec.aspect_ratio:
            task["aspectRatio"] = spec.aspect_ratio

        body, errors, warnings = self._client.build_body(
            task, video_durations=[template_seconds], default_mode=spec.mode,
        )
        for msg in warnings:
            self.log(f"[{tid}] 提示：{msg}")
        if errors:
            rec["status"] = "invalid"
            rec["error"] = "; ".join(errors)
            return rec

        started = time.time()
        try:
            # 桌面链路里 image/video 通常已是 URL；这里兜底处理本地路径
            body = resolve_body(body, self._uploader, cache=self._media_cache)
            wire = self._client.wire_body(body)
            if spec.submit_semaphore is not None:
                with spec.submit_semaphore:
                    if self.stop_event.is_set():
                        rec["status"] = "skipped"
                        return rec
                    resp = self._client.submit(wire)
            else:
                resp = self._client.submit(wire)
            rec["task_id"] = resp.task_id
            rec["request_id"] = resp.request_id
            rec["submitted_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            rec["task_expires_at"] = int(time.time()) + 24 * 3600
            rec["status"] = "running"
            with self.state.lock:
                self.state.records[tid] = dict(rec)
            self._save_state()
            self.log(f"[{tid}] 已提交 taskId={resp.task_id} status={resp.status}")
            self._emit("update", record=dict(rec))

            final = self._client.wait_task(
                resp.task_id, timeout=spec.poll_timeout, interval=spec.poll_interval,
                on_status=lambda status: self.log(f"[{tid}] {status}"),
            )
            if final.status == STATUS_UNKNOWN:
                raise TaskExpired(resp.task_id)
            if final.status == STATUS_CANCELED:
                raise TaskCanceled(resp.task_id)
            if final.status == STATUS_FAILED:
                raise EngineError(
                    f"任务失败 [{final.code or 'UNKNOWN'}] {final.message or '无错误信息'}"
                    + (f" request_id={final.request_id}" if final.request_id else "")
                )
            if final.status != STATUS_SUCCEEDED or not final.video_url:
                raise EngineError(f"任务状态 {final.status} 但没有结果地址")

            download_file(final.video_url, out_file)

            usage = final.usage or {}
            billable, estimated = estimate_cost(usage, spec.resolution, spec.price_per_second)
            rec.update({
                "status": "success",
                "request_id": final.request_id or rec.get("request_id", ""),
                "usage": usage,
                "billable_seconds": billable,
                "estimated_cost": estimated,
                "seconds": round(time.time() - started, 1),
                "verify": self._verify(out_file, template_seconds),
            })
            self._archive(rec, out_file)
        except WaitTimeout as exc:
            rec["status"] = "running"
            rec["error"] = str(exc)
        except TaskExpired as exc:
            rec["status"] = "expired"
            rec["error"] = str(exc)
        except TaskCanceled as exc:
            rec["status"] = "canceled"
            rec["error"] = str(exc)
        except WanError as exc:
            rec["status"] = "failed"
            rec["error"] = str(exc)
            if exc.request_id:
                rec["request_id"] = exc.request_id
        except (EngineError, MediaError) as exc:
            rec["status"] = "failed"
            rec["error"] = str(exc)
        except Exception as exc:  # 单个任务炸掉不影响整批
            rec["status"] = "failed"
            rec["error"] = f"{type(exc).__name__}: {exc}"

        with self.state.lock:
            self.state.records[tid] = rec
        self._save_state()
        return rec

    def _archive(self, rec: dict, path: Path) -> None:
        """把刚下载的成品推进对象存储。

        归档失败**不算任务失败**：视频已经在本机了，记下原因下轮补传即可。
        """
        storage = self.spec.storage
        if storage is None or not path.exists():
            return
        key = object_key(
            self.spec.archive_prefix, path.name, batch=self.spec.batch_id,
            prefix=storage.cfg.clean_prefix(),
            digest=file_fingerprint(path),
        )
        try:
            storage.put_path(path, key)
            rec["cos_key"] = key
            rec["cos_url"] = storage.sign_url(key, self.spec.archive_expires)
        except Exception as exc:  # noqa: BLE001 - 归档是附加动作，不能拖垮整批
            rec["archive_error"] = f"{type(exc).__name__}: {exc}"
            self.log(f"[{rec['id']}] 归档失败：{exc}")

    def _verify(self, path: Path, expected: float | None = None) -> dict:
        info = {"size_kb": round(path.stat().st_size / 1024) if path.exists() else 0}
        if info["size_kb"] < 50:
            info["warn"] = "文件过小，疑似下载不完整"
        secs = probe_duration(path)
        if secs is not None:
            info["duration"] = round(secs, 2)
            target = expected if expected is not None else self.spec.expect_seconds
            if target and abs(secs - target) > 2.0:
                info["warn"] = f"时长 {secs:.1f}s 与预期 {target:g}s 不符"
        return info

    def _total_estimated_cost(self) -> float | None:
        total = 0.0
        found = False
        for rec in self.state.records.values():
            value = rec.get("estimated_cost")
            if value is None:
                continue
            found = True
            try:
                total += float(value)
            except (TypeError, ValueError):
                pass
        return round(total, 2) if found else None

    def _save_state(self) -> None:
        path = self.spec.out_dir / "batch.json"
        with self._save_lock:  # Windows 下并发替换同一个文件会报占用
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state.snapshot(), ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)

    def _export_csv(self) -> None:
        path = self.spec.out_dir / "report.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow([
                "模板编号", "状态", "模式", "耗时(秒)", "计费秒数", "输出时长(秒)",
                "分辨率", "预估费用", "文件大小(KB)", "实测时长(秒)", "错误",
                "对象Key", "request_id",
            ])
            for rid in sorted(self.state.records):
                rec = self.state.records[rid]
                verify = rec.get("verify") or {}
                usage = rec.get("usage") or {}
                writer.writerow([
                    rid, rec.get("status"), rec.get("mode"), rec.get("seconds"),
                    rec.get("billable_seconds"), usage.get("output_video_duration"),
                    rec.get("resolution"), rec.get("estimated_cost"),
                    verify.get("size_kb"), verify.get("duration"), rec.get("error"),
                    rec.get("cos_key") or "", rec.get("request_id") or "",
                ])

    def _emit(self, kind: str, **payload) -> None:
        self.on_event({"type": kind, **payload})


# ---------------------------------------------------------------------------
# 便捷函数
# ---------------------------------------------------------------------------

def unique_batch_dir(root: Path, batch_id: str) -> Path:
    """批次号重复时自动追加 -2 / -3，不覆盖历史。"""
    safe = "".join(c for c in batch_id if c.isalnum() or c in "-_") or "batch"
    candidate = root / safe
    if not candidate.exists():
        return candidate
    index = 2
    while True:
        candidate = root / f"{safe}-{index}"
        if not candidate.exists():
            return candidate
        index += 1
