#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本机任务队列：持久化任务、复用 BatchRunner、隔离用户数据。"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path

from app.core import BatchRunner, BatchSpec, unique_batch_dir
from app.runtime import build_storages

_SAFE = re.compile(r"[^A-Za-z0-9_-]+")
_SECRET_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9._-]{12,}"), "[已隐藏密钥]"),
    (re.compile(r"(q-signature|q-ak|SecretId|SecretKey)=([^&\\s]+)", re.I), r"\1=[已隐藏]"),
]


def _redact(text: str) -> str:
    result = str(text)
    for pattern, replacement in _SECRET_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


class JobManager:
    def __init__(self, settings, prompts: dict[str, str]):
        self.settings = settings
        self.prompts = prompts
        self.root = settings.data_dir
        self.jobs_dir = self.root / "jobs"
        self.logs_dir = self.root / "logs"
        self.output_root = self.root / "output"
        for folder in (self.jobs_dir, self.logs_dir, self.output_root):
            folder.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.jobs: dict[str, dict] = {}
        self.active: dict[str, dict] = {}
        self.stop_event = threading.Event()
        self.submit_slots = threading.Semaphore(max(1, min(3, int(settings.config.get("WAN_MAX_CONCURRENCY") or 3))))
        self._load_jobs()
        self.dispatcher = threading.Thread(target=self._dispatch, name="web-job-dispatch", daemon=True)
        self.dispatcher.start()

    def _path(self, job_id: str) -> Path:
        return self.jobs_dir / f"{job_id}.json"

    def _save(self, job: dict) -> None:
        path = self._path(job["id"])
        tmp = path.with_suffix(".tmp")
        serial = {k: v for k, v in job.items() if not k.startswith("_")}
        tmp.write_text(json.dumps(serial, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def _load_jobs(self) -> None:
        for path in sorted(self.jobs_dir.glob("*.json")):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if job.get("status") == "running":
                job["status"] = "interrupted"
                job["message"] = "服务重启时任务正在运行；请核对阿里云任务后再重试。"
                job["finished_at"] = time.time()
            self.jobs[job.get("id", path.stem)] = job
            if job.get("status") == "interrupted":
                self._save(job)

    def create(self, owner: str, image_path: Path, image_name: str,
               templates: list[dict], prompt_id: str, resolution: str = "480P",
               duration: int = 5, prompt_body: str | None = None) -> dict:
        if prompt_id not in self.prompts:
            raise ValueError("请选择有效的提示词模板")
        prompt = str(prompt_body if prompt_body is not None else self.prompts[prompt_id]).strip()
        if not prompt:
            raise ValueError("提示词不能为空")
        if len(prompt) > 4000:
            raise ValueError("提示词最多 4000 个字符")
        if not templates:
            raise ValueError("至少选择一个视频场景")
        if len(templates) > self.settings.max_items:
            raise ValueError(f"一次最多生成 {self.settings.max_items} 条")
        if resolution not in ("480P", "720P") or duration not in (5, -1):
            raise ValueError("生成参数无效，请刷新页面后重试")
        job_id = uuid.uuid4().hex[:12]
        now = time.time()
        safe_owner = _SAFE.sub("-", owner)[:20] or "user"
        batch_id = f"WEB-{safe_owner}-{time.strftime('%Y%m%d-%H%M%S')}-{job_id[:4]}"
        job = {
            "id": job_id, "owner": owner, "status": "queued", "created_at": now,
            "started_at": None, "finished_at": None, "batch_id": batch_id,
            "image_path": str(image_path), "image_name": image_name,
            "prompt_id": prompt_id, "prompt": prompt,
            "templates": templates, "template_ids": [str(t["id"]) for t in templates],
            "resolution": resolution, "duration": duration, "total": len(templates),
            "done": 0, "records": [], "summary": None, "message": "已排队，开始前可取消。",
            "out_dir": "", "log_tail": [], "cancel_requested": False,
        }
        with self.changed:
            if sum(1 for item in self.jobs.values() if item.get("status") == "queued") >= 50:
                raise ValueError("当前排队任务较多，请稍后再提交")
            self.jobs[job_id] = job
            self._save(job)
            self.changed.notify_all()
        return self.public(job)

    def _dispatch(self) -> None:
        max_active = self.settings.max_active_jobs
        while not self.stop_event.is_set():
            with self.changed:
                self.changed.wait_for(lambda: self.stop_event.is_set() or self._has_dispatchable(max_active), timeout=1.0)
                if self.stop_event.is_set():
                    return
                active_owners = {item["owner"] for item in self.active.values()}
                queued = sorted((j for j in self.jobs.values() if j.get("status") == "queued"),
                                key=lambda j: j.get("created_at", 0))
                for job in queued:
                    if len(self.active) >= max_active:
                        break
                    if job["owner"] in active_owners:
                        continue
                    slot = {"owner": job["owner"], "runner": None}
                    self.active[job["id"]] = slot
                    active_owners.add(job["owner"])
                    threading.Thread(target=self._run_job, args=(job["id"],),
                                     name=f"web-job-{job['id']}", daemon=True).start()

    def _has_dispatchable(self, max_active: int) -> bool:
        if len(self.active) >= max_active:
            return False
        owners = {item["owner"] for item in self.active.values()}
        return any(j.get("status") == "queued" and j.get("owner") not in owners for j in self.jobs.values())

    def _append_log(self, job_id: str, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        line = f"[{stamp}] {_redact(str(message))[:1000]}"
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return
            job.setdefault("log_tail", []).append(line)
            job["log_tail"] = job["log_tail"][-200:]
            path = self.logs_dir / f"{job_id}.log"
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            self._save(job)

    def _on_event(self, job_id: str, event: dict) -> None:
        with self.changed:
            job = self.jobs[job_id]
            kind = event.get("type")
            if kind == "started":
                job["total"] = int(event.get("total") or job["total"])
            if kind in ("update", "progress") and isinstance(event.get("record"), dict):
                record = event["record"]
                records = {str(r.get("id")): r for r in job.get("records", [])}
                records[str(record.get("id"))] = record
                job["records"] = list(records.values())
            if kind == "progress":
                job["done"] = int(event.get("done") or 0)
            if kind == "finished":
                job["summary"] = event.get("summary")
            self._save(job)
            self.changed.notify_all()

    def _run_job(self, job_id: str) -> None:
        with self.lock:
            job = self.jobs[job_id]
            job["status"] = "running"
            job["started_at"] = time.time()
            job["message"] = "正在生成，请稍候。"
            out_dir = unique_batch_dir(self.output_root, job["batch_id"])
            job["out_dir"] = str(out_dir)
            self._save(job)
        runner = None
        try:
            template_store, output_store = build_storages(self.settings.config)
            cfg = self.settings.config
            concurrency = max(1, min(3, int(cfg.get("WAN_MAX_CONCURRENCY") or 3)))
            spec = BatchSpec(
                batch_id=Path(job["out_dir"]).name,
                image_path=job["image_path"], prompt=job["prompt"], out_dir=Path(job["out_dir"]),
                templates=job["templates"], api_key=app_api_key(cfg), base=str(cfg.get("WAN_BASE_URL") or ""),
                provider=str(cfg.get("WAN_PROVIDER") or "dashscope"),
                model=str(cfg.get("WAN_MODEL") or "wan3.0-video"), mode="edit",
                resolution=job["resolution"], duration=job["duration"],
                aspect_ratio=cfg.get("WAN_DEFAULT_RATIO"), concurrency=concurrency,
                poll_interval=float(cfg.get("WAN_POLL_INTERVAL") or 15),
                poll_timeout=int(cfg.get("WAN_POLL_TIMEOUT") or 1800),
                expect_seconds=float(job["duration"]) if job["duration"] > 0 else 0,
                storage=output_store, template_storage=template_store,
                source_prefix=str(cfg.get("COS_SOURCE_PREFIX") or ""),
                price_per_second=cfg.get("WAN_PRICE_PER_SECOND") or {},
                submit_semaphore=self.submit_slots,
            )
            runner = BatchRunner(spec, on_event=lambda e: self._on_event(job_id, e),
                                 log=lambda m: self._append_log(job_id, m))
            with self.lock:
                if job_id in self.active:
                    self.active[job_id]["runner"] = runner
                if self.jobs[job_id].get("cancel_requested") or self.stop_event.is_set():
                    runner.stop()
            summary = runner.run()
            with self.changed:
                job = self.jobs[job_id]
                job["summary"] = summary
                if job.get("cancel_requested"):
                    job["status"] = "canceled"
                    job["message"] = "已停止继续提交；已提交的任务已处理完成。"
                elif summary.get("success", 0) and summary.get("failed", 0):
                    job["status"] = "partial"
                    job["message"] = "部分完成，请检查失败项。"
                elif summary.get("success", 0):
                    job["status"] = "succeeded"
                    job["message"] = "全部完成。"
                else:
                    job["status"] = "failed"
                    job["message"] = "任务未能完成，请查看失败原因。"
                job["done"] = int(summary.get("total") or job["done"])
                job["finished_at"] = time.time()
                self._save(job)
        except Exception as exc:
            with self.changed:
                job = self.jobs[job_id]
                job["status"] = "failed"
                job["message"] = str(exc)[:500]
                job["finished_at"] = time.time()
                self._save(job)
            self._append_log(job_id, f"任务启动失败：{exc}")
        finally:
            with self.changed:
                self.active.pop(job_id, None)
                self.changed.notify_all()

    def retry_failed(self, job_id: str, owner: str) -> dict:
        with self.lock:
            source = self.jobs.get(job_id)
            if not source:
                raise ValueError("找不到该任务")
            if source.get("status") not in {"partial", "failed", "canceled"}:
                raise ValueError("任务尚未结束，暂时不能重试")
            failed_ids = {str(r.get("id")) for r in source.get("records", [])
                          if r.get("status") in {"failed", "invalid", "expired", "canceled"}}
            selected = [dict(t) for t in source.get("templates", []) if str(t.get("id")) in failed_ids]
            if not selected:
                raise ValueError("该任务没有可重试的失败项")
            image_path = Path(source["image_path"])
            if not image_path.is_file():
                raise ValueError("原服装图片已不存在，请重新上传后新建任务")
            return self.create(owner, image_path, source.get("image_name", "服装图"), selected,
                               source["prompt_id"], source["resolution"], source["duration"], source.get("prompt"))

    def cancel(self, job_id: str) -> bool:
        with self.changed:
            job = self.jobs.get(job_id)
            if not job:
                return False
            if job.get("status") == "queued":
                job["status"] = "canceled"
                job["message"] = "已取消排队任务。"
                job["finished_at"] = time.time()
                self._save(job)
            elif job.get("status") == "running":
                job["cancel_requested"] = True
                job["message"] = "正在停止新提交；已提交的任务会继续处理。"
                runner = self.active.get(job_id, {}).get("runner")
                if runner:
                    runner.stop()
                self._save(job)
            else:
                return False
            self.changed.notify_all()
            return True

    def get(self, job_id: str) -> dict | None:
        with self.lock:
            job = self.jobs.get(job_id)
            return self.public(job) if job else None

    def list_for(self, username: str, admin: bool = False) -> list[dict]:
        with self.lock:
            items = [j for j in self.jobs.values() if admin or j.get("owner") == username]
            items.sort(key=lambda j: j.get("created_at", 0), reverse=True)
            return [self.public(j) for j in items]

    def public(self, job: dict) -> dict:
        with self.lock:
            safe = {k: job.get(k) for k in (
                "id", "owner", "status", "created_at", "started_at", "finished_at",
                "batch_id", "image_name", "prompt_id", "template_ids", "resolution",
                "duration", "total", "done", "message", "log_tail", "cancel_requested")}
            if safe.get("message"):
                safe["message"] = _redact(safe["message"])
            safe["log_tail"] = [_redact(line) for line in (safe.get("log_tail") or [])]
            summary = job.get("summary")
            if isinstance(summary, dict):
                safe["summary"] = {k: v for k, v in summary.items() if k != "out_dir"}
            else:
                safe["summary"] = None
            safe["records"] = []
            for rec in job.get("records", []):
                public_record = {k: rec.get(k) for k in (
                    "id", "status", "seconds", "error", "task_id", "request_id",
                    "billable_seconds", "estimated_cost", "verify", "cos_key") if k in rec}
                if public_record.get("error"):
                    public_record["error"] = _redact(public_record["error"])
                safe["records"].append(public_record)
                if rec.get("archive_error"):
                    safe["records"][-1]["archive_warning"] = "视频已生成，但 COS 归档失败，请联系管理员。"
            if job.get("out_dir"):
                safe["download_all"] = f"/api/jobs/{job['id']}/files.zip"
                safe["report_url"] = f"/api/jobs/{job['id']}/report.csv"
                for rec in safe["records"]:
                    if rec.get("status") == "success":
                        rec["download_url"] = f"/api/jobs/{job['id']}/download/{rec['id']}"
            queue = sorted((j for j in self.jobs.values() if j.get("status") == "queued"), key=lambda j: j.get("created_at", 0))
            safe["queue_position"] = next((i + 1 for i, queued in enumerate(queue) if queued.get("id") == job.get("id")), 0)
            return safe

    def log_path(self, job_id: str) -> Path:
        return self.logs_dir / f"{job_id}.log"

    def output_path(self, job_id: str) -> Path | None:
        with self.lock:
            job = self.jobs.get(job_id)
            return Path(job["out_dir"]) if job and job.get("out_dir") else None

    def shutdown(self) -> None:
        self.stop_event.set()
        with self.changed:
            for slot in self.active.values():
                if slot.get("runner"):
                    slot["runner"].stop()
            self.changed.notify_all()


def app_api_key(cfg: dict) -> str:
    from app.config import active_api_key
    return active_api_key(cfg)
