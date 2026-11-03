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

from app.core import TAG_SLOTS, BatchRunner, BatchSpec, unique_batch_dir
from app.runtime import build_storages
from app.wan_api import global_inflight, global_small_inflight

_SAFE = re.compile(r"[^A-Za-z0-9_-]+")
_STYLE_MAX = 40

TAG_LABELS = dict(TAG_SLOTS)   # slot -> 中文名（上衣/内衬/裤子/鞋子/场景/特殊备注）
# 款号类栏位（上衣/内衬/裤子/鞋子）：英文+数字组合；场景/备注允许中文自由文本。
_TAG_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
_NOTE_MAX = 100


def clean_tag_value(slot: str, value) -> str:
    """单个标签值：去控制字符与首尾空白；款号类只认英文数字 -_，场景/备注放行中文。"""
    text = re.sub(r"[\x00-\x1f\x7f]", "", str(value or "")).strip()
    if not text:
        return ""
    if slot == "note":
        return text[:_NOTE_MAX]
    if slot == "scene":
        return text[:_STYLE_MAX]
    if not _TAG_CODE_RE.fullmatch(text):
        label = TAG_LABELS.get(slot, slot)
        raise ValueError(f"「{label}」款号只能用英文、数字、- 和 _，最多 40 个字符")
    return text


def clean_tags(raw) -> dict:
    """整理 6 栏位标签 dict：空值丢弃；top（上衣）缺失直接拒绝（路径段靠它）。"""
    source = raw if isinstance(raw, dict) else {}
    tags: dict[str, str] = {}
    for slot, _label in TAG_SLOTS:
        value = clean_tag_value(slot, source.get(slot))
        if value:
            tags[slot] = value
    if not tags.get("top"):
        raise ValueError("请填写「上衣」款号（云端归档路径按它命名）")
    return tags


def tags_summary(tags: dict | None, style_no: str = "") -> str:
    """给列表/详情用的单行摘要：优先新标签，兜底旧款号字段。"""
    source = tags if isinstance(tags, dict) else {}
    parts = [f"{TAG_LABELS[key]} {value}" for key, value in source.items()
             if value and key in TAG_LABELS]
    if not parts and style_no:
        parts = [f"上衣 {style_no}"]
    return " · ".join(parts)


def clean_style_no(value) -> str:
    """旧款号字段（v0.9.0 及以前）：仅用于读取老数据，新提交一律走 clean_tags。"""
    text = re.sub(r"[\x00-\x1f\x7f]", "", str(value or "")).strip()
    return text[:_STYLE_MAX]


# 重试时认的非成功终态：skipped 是「被取消/批量放弃」的条目，重跑一次才有意义；
# running 不算——它表示还在等云端出片（含等待超时），此时重试会和原任务抢同一份费用。
_RETRYABLE_RECORD_STATES = frozenset({"failed", "invalid", "expired", "canceled", "skipped"})

_SECRET_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9._-]{12,}"), "[已隐藏密钥]"),
    (re.compile(r"(q-signature|q-ak|SecretId|SecretKey)=([^&\\s]+)", re.I), r"\1=[已隐藏]"),
]


def _redact(text: str) -> str:
    result = str(text)
    for pattern, replacement in _SECRET_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def duration_ok(value) -> bool:
    """时长校验，与 app/wan_api.py 的 validate_task 保持同一口径。

    -1 = 跟随素材/智能时长；2–30 = 固定输出秒数（网页标配 7 秒）。
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        return False
    return number == -1 or 2 <= number <= 30



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
        # 全局在飞额度（单例）：全部账户共用一把 Key，额度也只有一份，跨 job 共享。
        self.inflight = global_inflight()
        self.submit_slots = self.inflight      # 兼容旧字段名
        # 快速通道：小批量（≤small_lane_items）专用在飞池，不跟大批量排队。
        self.small_inflight = global_small_inflight()
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
            if not isinstance(job, dict):
                continue
            dirty = False
            # 老版本/手改的文件可能没有 id：以文件名补回并落盘。
            # 少了这一步，派发时 job["id"] 会 KeyError，整条调度线就断了。
            job_id = str(job.get("id") or path.stem)
            if job.get("id") != job_id:
                job["id"] = job_id
                dirty = True
            if job.get("status") == "running":
                job["status"] = "interrupted"
                job["message"] = "服务重启时任务正在运行；请核对阿里云任务后再重试。"
                job["finished_at"] = time.time()
                dirty = True
            self.jobs[job_id] = job
            if dirty:
                self._save(job)

    def create(self, owner: str, image_path: Path, image_name: str,
               templates: list[dict], prompt_id: str, resolution: str = "480P",
               duration: int = 5, prompt_body: str | None = None, *,
               batch_ref: str = "", limit_items: int | None = None,
               tags: dict | None = None, style_no: str = "") -> dict:
        """建任务并入队。batch_ref 指向批次（老路径为空）；limit_items 覆盖单次条数上限。

        tags：6 栏位标签（上衣必填）；style_no：v0.9.0 旧字段，读老批次时兜底，
        两者都缺时报错——云端归档路径必须有上衣款号。
        """
        if prompt_id not in self.prompts:
            raise ValueError("请选择有效的提示词模板")
        prompt = str(prompt_body if prompt_body is not None else self.prompts[prompt_id]).strip()
        if not prompt:
            raise ValueError("提示词不能为空")
        if len(prompt) > 4000:
            raise ValueError("提示词最多 4000 个字符")
        if not templates:
            raise ValueError("至少选择一个视频场景")
        max_items = int(limit_items if limit_items is not None else self.settings.max_items)
        max_items = max(1, min(500, max_items))
        if len(templates) > max_items:
            raise ValueError(f"一次最多生成 {max_items} 条")
        if resolution not in ("480P", "720P") or not duration_ok(duration):
            raise ValueError("生成参数无效，请刷新页面后重试")
        # 上衣款号必填：COS 归档路径按它分段。tags 优先，老调用传 style_no 时兜底映射。
        job_tags = clean_tags(tags) if tags else ({"top": clean_style_no(style_no)} if clean_style_no(style_no) else {})
        if not job_tags.get("top"):
            raise ValueError("请填写「上衣」款号（云端归档路径按它命名）")
        # 快速通道：3 条及以下的小批量独立排队，可与本人的大批量并行。
        lane = "small" if len(templates) <= self.settings.small_lane_items else "big"
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
            # 批次归属：批次生产时指向 BatchManager 的 id，试跑/老 job 为空。
            "batch_ref": str(batch_ref or ""),
            # 标签：6 栏位（上衣必填），归档时进 COS 路径与对象标签。
            "tags": job_tags,
            # 旧款号字段：只服务 v0.9.0 老数据展示，新任务不再写入。
            "style_no": clean_style_no(style_no),
            # small = 快速通道（≤small_lane_items 条）；big = 走大批量队列。
            "lane": lane,
        }
        with self.changed:
            if sum(1 for item in self.jobs.values() if item.get("status") == "queued") >= 50:
                raise ValueError("当前排队任务较多，请稍后再提交")
            self.jobs[job_id] = job
            self._save(job)
            self.changed.notify_all()
        return self.public(job)

    @staticmethod
    def _lane(job: dict) -> str:
        """small = 快速通道（小批量）；big = 大批量。老 job 没这个字段，按 big 处理。"""
        return "small" if str(job.get("lane") or "") == "small" else "big"

    def _active_counts(self) -> tuple[dict[str, int], dict[str, int]]:
        """分别统计大批量/小批量在各账户下的在跑 job 数，互不占用额度。"""
        big: dict[str, int] = {}
        small: dict[str, int] = {}
        for item in self.active.values():
            owner = str(item.get("owner") or "")
            if self._lane(item) == "small":
                small[owner] = small.get(owner, 0) + 1
            else:
                big[owner] = big.get(owner, 0) + 1
        return big, small

    @staticmethod
    def _lane_allowed(lane: str, owner: str,
                      big_owner: dict[str, int], small_owner: dict[str, int],
                      big_n: int, small_n: int,
                      max_big: int, max_small: int, per_user_big: int,
                      per_user_small: int = 1) -> bool:
        """两条通道各自独立计数：小批量不占大批量的 job 槽位与每用户额度。"""
        if lane == "small":
            return small_n < max_small and small_owner.get(owner, 0) < per_user_small
        return big_n < max_big and big_owner.get(owner, 0) < per_user_big

    def _spawn(self, job_id: str) -> None:
        """起一条工作线程。单独一个方法，测试里可以替换成会抛错的实现。"""
        threading.Thread(target=self._run_job, args=(job_id,),
                         name=f"web-job-{job_id}", daemon=True).start()

    def _dispatch_failed(self, job: dict, exc: BaseException) -> None:
        """派发阶段失败：把该 job 置为失败，异常绝不抛回调度线程。

        调度线程是全站唯一的一条，抛出去就等于全站任务永久排队：新 job 能提交、
        前端显示"已排队"，但再没有人派发。所以这里 swallow 掉，只牺牲这一个 job。
        """
        message = _redact(f"任务无法启动：{type(exc).__name__}: {exc}")[:500]
        job_id = str(job.get("id") or "")
        with self.changed:
            # 没有 id 的任务落不了盘，但至少要在内存里置成失败，否则它会一直占着队列。
            live = self.jobs.get(job_id) if job_id else job
            if live is not None and live.get("status") == "queued":
                live["status"] = "failed"
                live["message"] = message
                live["finished_at"] = time.time()
                if job_id:
                    self._save(live)
            self.changed.notify_all()
        if job_id:
            self._append_log(job_id, message)

    def _dispatch(self) -> None:
        max_big = self.settings.max_active_jobs
        max_small = self.settings.max_active_small_jobs
        per_user_big = self.settings.max_active_per_user
        while not self.stop_event.is_set():
            with self.changed:
                self.changed.wait_for(
                    lambda: self.stop_event.is_set()
                    or self._has_dispatchable(max_big, max_small, per_user_big), timeout=1.0)
                if self.stop_event.is_set():
                    return
                self._dispatch_queued(max_big, max_small, per_user_big)

    def _dispatch_queued(self, max_big: int, max_small: int, per_user_big: int) -> None:
        """派发一轮（调用方已持锁）。抽成独立方法是为了能在测试里单步驱动。"""
        big_owner, small_owner = self._active_counts()
        big_n, small_n = sum(big_owner.values()), sum(small_owner.values())
        # 小批量优先：即使它比大批量晚提交，也先放行。
        queued = sorted(
            (j for j in self.jobs.values() if j.get("status") == "queued"),
            key=lambda j: (self._lane(j) != "small", j.get("created_at", 0)))
        for job in queued:
            lane = self._lane(job)
            owner = str(job.get("owner") or "")
            if not self._lane_allowed(lane, owner, big_owner, small_owner,
                                      big_n, small_n, max_big, max_small, per_user_big):
                continue
            job_id = str(job.get("id") or "")
            if not job_id:
                # 没有 id 就没法建文件名/回写状态，只能就地判失败（不会阻塞后续 job）。
                self._dispatch_failed(job, ValueError("任务缺少 id，无法派发"))
                continue
            self.active[job_id] = {"owner": owner, "lane": lane, "runner": None}
            if lane == "small":
                small_owner[owner] = small_owner.get(owner, 0) + 1
                small_n += 1
            else:
                big_owner[owner] = big_owner.get(owner, 0) + 1
                big_n += 1
            try:
                self._spawn(job_id)
            except Exception as exc:  # 线程资源耗尽等：只影响这一个 job
                # 先把占位和计数还回去，再判失败，否则后面的 job 会被这格空占而饿死。
                self.active.pop(job_id, None)
                if lane == "small":
                    small_owner[owner] = max(0, small_owner.get(owner, 0) - 1)
                    small_n = max(0, small_n - 1)
                else:
                    big_owner[owner] = max(0, big_owner.get(owner, 0) - 1)
                    big_n = max(0, big_n - 1)
                self._dispatch_failed(job, exc)

    def _has_dispatchable(self, max_big: int, max_small: int, per_user_big: int) -> bool:
        big_owner, small_owner = self._active_counts()
        big_n, small_n = sum(big_owner.values()), sum(small_owner.values())
        for job in self.jobs.values():
            if job.get("status") != "queued":
                continue
            lane = self._lane(job)
            owner = str(job.get("owner") or "")
            if self._lane_allowed(lane, owner, big_owner, small_owner,
                                  big_n, small_n, max_big, max_small, per_user_big):
                return True
        return False

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
            job = self.jobs.get(job_id)
            if job is None:
                # 事件回调可能晚于 job 被清理（或来自上一个进程），不该把整批线程带崩。
                return
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
        runner = None
        try:
            # 置 running / 建目录 / 落盘也放在 try 里：中间的 OSError 若逃出去，
            # finally 就护不到 active 槽位，job 会永卡 running、且因状态不是终态而不能重试。
            with self.lock:
                job = self.jobs.get(job_id)
                if job is None:
                    return
                if job.get("cancel_requested") or job.get("status") != "queued":
                    # 排队期间被取消：不要再把它改回 running 跑一遍（取消竞态）。
                    return
                job["status"] = "running"
                job["started_at"] = time.time()
                job["message"] = "正在生成，请稍候。"
                out_dir = unique_batch_dir(self.output_root, job["batch_id"])
                job["out_dir"] = str(out_dir)
                self._save(job)
            template_store, output_store = build_storages(self.settings.config)
            cfg = self.settings.config
            # 线程数只是工作线程，不再与 API 并发/速率绑在一起；上限 50 防止误配。
            concurrency = self.settings.max_concurrency
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
                # 单价必须带兜底：引擎不兜底，传全 null 会让每条记录的费用变成 None。
                price_per_second=self.settings.price_map(),
                style_no=str(job.get("style_no") or ""),
                tags=dict(job.get("tags") or {}),
                # 快速通道用自己的在飞池，3 条小任务不会被大批量的几十条挡住。
                inflight_semaphore=(self.small_inflight if job.get("lane") == "small"
                                    else self.inflight),
            )
            runner = BatchRunner(spec, on_event=lambda e: self._on_event(job_id, e),
                                 log=lambda m: self._append_log(job_id, m))
            with self.lock:
                if job_id in self.active:
                    self.active[job_id]["runner"] = runner
                if self.jobs[job_id].get("cancel_requested") or self.stop_event.is_set():
                    runner.stop()
            summary = runner.run()
            self._finish_job(job_id, summary)
        except Exception as exc:
            with self.changed:
                job = self.jobs.get(job_id)
                if job is not None:
                    job["status"] = "failed"
                    job["message"] = _redact(str(exc))[:500]
                    job["finished_at"] = time.time()
                    self._save(job)
                self.changed.notify_all()
            self._append_log(job_id, f"任务启动失败：{exc}")
        finally:
            with self.changed:
                self.active.pop(job_id, None)
                self.changed.notify_all()

    @staticmethod
    def _job_outcome(summary: dict) -> tuple[str, str, int]:
        """把引擎汇总换算成 job 状态：只有每一条都成功，才算「全部完成」。

        引擎侧的非成功终态有 failed / invalid / expired / canceled / skipped 五种，
        以前只数 failed，于是「5 成功 + 5 无效」被显示成 succeeded + 100%。
        running 只在等待超时时出现（P1-19），这里按「未完成」处理，不拉满进度条。
        """
        total = max(0, int(summary.get("total") or 0))
        success = max(0, int(summary.get("success") or 0))
        running = max(0, int(summary.get("running") or 0))
        done = max(0, min(total, total - running))
        if total and success >= total:
            return "succeeded", "全部完成。", done
        if success:
            return ("partial",
                    f"部分完成：成功 {success}/{total}，另有 {total - success} 条未成功"
                    "（失败/无效/超时/取消/跳过），请检查失败项。",
                    done)
        return "failed", "任务未能完成，请查看失败原因。", done

    def _finish_job(self, job_id: str, summary: dict) -> None:
        status, message, done = self._job_outcome(summary)
        with self.changed:
            job = self.jobs.get(job_id)
            if job is None:
                return
            job["summary"] = summary
            if job.get("cancel_requested"):
                job["status"] = "canceled"
                job["message"] = "已停止继续提交；已提交的任务已处理完成。"
            else:
                job["status"] = status
                job["message"] = message
            job["done"] = done
            job["finished_at"] = time.time()
            self._save(job)
            self.changed.notify_all()

    def retry_failed(self, job_id: str, owner: str) -> dict:
        with self.lock:
            source = self.jobs.get(job_id)
            if not source:
                raise ValueError("找不到该任务")
            if source.get("status") not in {"partial", "failed", "canceled"}:
                raise ValueError("任务尚未结束，暂时不能重试")
            failed_ids = {str(r.get("id")) for r in source.get("records", [])
                          if r.get("status") in _RETRYABLE_RECORD_STATES}
            selected = [dict(t) for t in source.get("templates", []) if str(t.get("id")) in failed_ids]
            if not selected:
                if any(str(r.get("status")) == "running" for r in source.get("records", [])):
                    raise ValueError("还有条目在等云端出片（或等待超时），请稍后再重试")
                raise ValueError("该任务没有可重试的失败项")
            image_path = Path(source["image_path"])
            if not image_path.is_file():
                raise ValueError("原服装图片已不存在，请重新上传后新建任务")
            return self.create(owner, image_path, source.get("image_name", "服装图"), selected,
                               source["prompt_id"], source["resolution"], source["duration"], source.get("prompt"),
                               tags=dict(source.get("tags") or {}), style_no=str(source.get("style_no") or ""))

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

    def list_for(self, username: str, admin: bool = False, owner: str = "") -> list[dict]:
        """owner 只对管理员生效：按账户筛选，不传则沿用原口径（mine / all）。"""
        filter_owner = str(owner or "") if admin else ""
        with self.lock:
            items = [j for j in self.jobs.values()
                     if (admin or j.get("owner") == username)
                     and (not filter_owner or j.get("owner") == filter_owner)]
            items.sort(key=lambda j: j.get("created_at", 0), reverse=True)
            return [self.public(j) for j in items]

    def _record_cost(self, rec: dict, job: dict) -> tuple[float, str]:
        """单条记录的费用：优先用生成时落下的金额，否则按当前单价回算。

        历史任务（单价还是 null 时生成的）记录里 estimated_cost 是 None；不回算的话，
        管理端的"花了多少钱"对老数据全是空白。回算只发生在读取时，不改任务文件，
        并标记 cost_source=derived，便于区分"实测落账"与"事后估算"。
        """
        raw = rec.get("estimated_cost")
        if raw not in (None, ""):
            try:
                return float(raw), "record"
            except (TypeError, ValueError):
                pass
        try:
            seconds = float(rec.get("billable_seconds") or 0)
        except (TypeError, ValueError):
            seconds = 0.0
        price, _source = self.settings.unit_price(str(job.get("resolution") or ""))
        if seconds > 0 and price:
            return seconds * float(price), "derived"
        return 0.0, "missing"

    def usage_for(self, start: float | None = None, end: float | None = None,
                  owner: str = "") -> dict:
        """按账户汇总工作量：任务数、条数、成功/失败、计费秒、预估金额、最近活跃。

        口径说明：
          · 条数以 job.records 为准（进行中的 job 也有记录）；
          · 金额是「计费秒 × 单价」的估算，真实账单以阿里云为准；
          · 含零使用量的账户，方便管理员看出"谁还没开始用"。
        """
        failed_states = {"failed", "invalid", "expired", "canceled"}

        def blank(name: str, role: str = "user", enabled: bool = True) -> dict:
            return {"owner": name, "role": role, "enabled": enabled,
                    "jobs": 0, "items": 0, "success": 0, "failed": 0, "unfinished": 0,
                    "billable_seconds": 0.0, "estimated_cost": 0.0, "last_at": 0.0}

        rows: dict[str, dict] = {}
        for name, record in (self.settings.users or {}).items():
            rows[str(name)] = blank(str(name), str(record.get("role", "user")),
                                    bool(record.get("enabled", True)))
        with self.lock:
            for job in self.jobs.values():
                name = str(job.get("owner") or "未知")
                if owner and name != owner:
                    continue
                created = float(job.get("created_at") or 0)
                if start and created < start:
                    continue
                if end and created >= end:
                    continue
                row = rows.setdefault(name, blank(name))
                row["jobs"] += 1
                if job.get("status") in {"queued", "running"}:
                    row["unfinished"] += 1
                for rec in job.get("records") or []:
                    status = str(rec.get("status") or "")
                    row["items"] += 1
                    if status == "success":
                        row["success"] += 1
                    elif status in failed_states:
                        row["failed"] += 1
                    elif status == "running":
                        row["unfinished"] += 1
                    try:
                        row["billable_seconds"] += float(rec.get("billable_seconds") or 0)
                    except (TypeError, ValueError):
                        pass
                    cost_value, _cost_source = self._record_cost(rec, job)
                    row["estimated_cost"] += cost_value
                row["last_at"] = max(row["last_at"], created,
                                     float(job.get("finished_at") or 0))

        for row in rows.values():
            row["success_rate"] = round(row["success"] / row["items"], 4) if row["items"] else 0.0
            row["billable_seconds"] = round(row["billable_seconds"], 2)
            row["estimated_cost"] = round(row["estimated_cost"], 2)

        accounts = sorted(rows.values(),
                          key=lambda r: (-r["estimated_cost"], -r["items"], r["owner"]))
        totals = {key: 0 for key in ("jobs", "items", "success", "failed", "unfinished")}
        for row in accounts:
            for key in totals:
                totals[key] += int(row[key])
        totals["billable_seconds"] = round(sum(row["billable_seconds"] for row in accounts), 2)
        totals["estimated_cost"] = round(sum(row["estimated_cost"] for row in accounts), 2)
        totals["accounts"] = len(accounts)
        return {"accounts": accounts, "totals": totals}

    def work_for(self, owner: str, start: float | None = None, end: float | None = None,
                 status: str = "", offset: int = 0, limit: int = 100,
                 page: bool = True) -> dict:
        """某账户跑了哪些视频、各花了多少：按「记录」逐条展开，供管理端对账。

        · 一条记录 = 一个模板（视频）的一次生成，带模板 ID 与名称；
        · 金额与计费秒来自该记录的 usage，不是事后估算；
        · total_cost / total_billable_seconds 是**筛选结果全量**的合计，不是当前页。
        """
        offset = max(0, int(offset or 0))
        limit = max(1, min(500, int(limit or 100)))
        target = str(owner or "")
        rows: list[dict] = []
        with self.lock:
            for job in self.jobs.values():
                if str(job.get("owner") or "") != target:
                    continue
                created = float(job.get("created_at") or 0)
                if start and created < start:
                    continue
                if end and created >= end:
                    continue
                names = {str(tpl.get("id")): str(tpl.get("name") or "")
                         for tpl in job.get("templates") or [] if isinstance(tpl, dict)}
                for rec in job.get("records") or []:
                    rec_status = str(rec.get("status") or "")
                    if status and rec_status != status:
                        continue
                    template_id = str(rec.get("id"))
                    cost_value, cost_source = self._record_cost(rec, job)
                    rows.append({
                        "created_at": created,
                        "finished_at": job.get("finished_at"),
                        "job_id": str(job.get("id")),
                        "batch_id": str(job.get("batch_id") or ""),
                        "batch_ref": str(job.get("batch_ref") or ""),
                        "template_id": template_id,
                        "template_name": names.get(template_id, ""),
                        "status": rec_status,
                        "seconds": rec.get("seconds"),
                        "billable_seconds": rec.get("billable_seconds"),
                        "estimated_cost": round(cost_value, 4) if cost_source != "missing" else None,
                        "cost_source": cost_source,
                        "resolution": job.get("resolution"),
                        "duration": job.get("duration"),
                    })
        rows.sort(key=lambda row: (row["created_at"], row["template_id"]), reverse=True)
        billable = 0.0
        cost = 0.0
        success = failed = unfinished = 0
        for row in rows:
            status = row.get("status")
            if status == "success":
                success += 1
            elif status in {"failed", "invalid", "expired", "canceled"}:
                failed += 1
            elif status == "running":
                unfinished += 1
            try:
                billable += float(row.get("billable_seconds") or 0)
            except (TypeError, ValueError):
                pass
            try:
                cost += float(row.get("estimated_cost") or 0)
            except (TypeError, ValueError):
                pass
        return {
            "owner": target,
            "total": len(rows),
            "success": success,
            "failed": failed,
            "unfinished": unfinished,
            "offset": offset,
            "limit": limit,
            # page=False 供 CSV 导出：一次给全量，不受分页上限影响。
            "items": rows[offset:offset + limit] if page else rows,
            "total_billable_seconds": round(billable, 2),
            "total_cost": round(cost, 2),
        }

    def public(self, job: dict) -> dict:
        with self.lock:
            safe = {k: job.get(k) for k in (
                "id", "owner", "status", "created_at", "started_at", "finished_at",
                "batch_id",                 "batch_ref", "image_name", "prompt_id", "template_ids", "resolution",
                "duration", "total", "done", "message", "log_tail", "cancel_requested",
                "lane", "style_no", "tags")}
            safe["batch_ref"] = str(safe.get("batch_ref") or "")
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
    """全站唯一的 API Key：从 config.json 读取，所有账户共用同一把。"""
    from app.config import active_api_key
    return active_api_key(cfg)
