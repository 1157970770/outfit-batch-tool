#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""任务批次：一份锁定清单 + 冻结点画质/时长，提示词锁定后仍可单独修改，之后分批生产。

核心约束（与 batch-spec.md 一致）：
  · 一次只做一件事：`draft`（挑清单）→ `locked`（可生产）→ `closed`。
  · **没有 running 态**：「是否在跑」由 `active_job_id` 派生，避免状态与 job 不同步。
  · 去重靠 `items[].state` 状态位，不反查 jobs——反查既慢，又会丢掉「还没生产的候选」。
  · 取数 + 标 producing + 建 job 必须在**同一把锁**内（复用 JobManager.lock 同一个 RLock）。
  · 读时对账：`active_job_id` 对应的 job 已终态 → 收敛条目状态并清空 `active_job_id`。
    服务重启后首次读取即自动恢复，不引事件总线。
  · 存储：web-data/batches/<id>.json，一 batch 一文件（tmp + os.replace 原子写），
    两个批次并发生产互不覆盖。
  · 删除批次只删这一份批次 JSON：**已生成的视频属于 job**，web-data/output/ 与
    web-data/jobs/ 下的文件一律不碰；job 的 batch_ref 保持原值不回写，历史分组按
    「批次文件还在不在」判定归属，删掉后这些 job 自然落回 __ungrouped__。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
import zipfile
from pathlib import Path

from app.web.jobs import _redact, duration_ok
from app.web.templates import TemplateError

BATCH_STATES = ("draft", "locked", "closed")
ITEM_STATES = ("pending", "producing", "done", "failed")
# v2：单次生产只接受这三档；重试失败另行放宽到 1–100。
PRODUCE_CHOICES = (20, 50, 100)
DEFAULT_PRODUCE_COUNT = 50
RETRY_MIN = 1
RETRY_MAX = 100
UNGROUPED_KEY = "__ungrouped__"

# 计费口径：一条任务的计费秒数 ≈ 模板时长 + 输出视频秒数。
# 输出默认固定 7 秒（duration=7，API 接受 2–30）；duration=-1 时才按 7 秒估算。
DEFAULT_DURATION = 7
OUTPUT_SECONDS = 7.0

# 工作簿「时长(秒)」缺失时的兜底（规格卡按 8.45 秒/条口径，即模板约 1.45 秒）。
DEFAULT_TEMPLATE_SECONDS = 1.45

_TERMINAL_JOB = {"succeeded", "partial", "failed", "canceled", "interrupted"}
_RECORD_DONE = {"success"}
# 记录里这些状态算「没跑成但也没花掉」：条目回 pending，可以再排一次。
_RECORD_UNTOUCHED = {"pending", "skipped"}


class BatchError(Exception):
    """带 HTTP 状态码的业务错误；server 直接按 status 返回 {ok:false,error}。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = int(status)


def _as_float(value) -> float | None:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _as_int(value, default: int | None = None) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _norm_no(value) -> str:
    """批次号唯一性比较忽略大小写与首尾空白。"""
    return str(value or "").strip().lower()


class BatchManager:
    def __init__(self, settings, jobs, catalog, resolve_upload):
        self.settings = settings
        self.jobs = jobs
        # 复用 JobManager 的 RLock 实例：批次与任务共用临界区，天然排除并发抢同一批条目。
        # 注意字段名不能用 self.lock——本类有个 lock() 方法（锁定清单）。
        self.mutex = jobs.lock if jobs is not None else threading.RLock()
        self.catalog_of = catalog              # () -> TemplateCatalog（热重载后可换）
        self.resolve_upload = resolve_upload   # (upload_id, owner) -> dict | None
        self.root = settings.data_dir / "batches"
        self.root.mkdir(parents=True, exist_ok=True)
        self.batches: dict[str, dict] = {}
        self.by_no: dict[str, str] = {}
        self.warnings: list[str] = []
        self._load()

    # ------------------------------------------------------------------ 存储
    def _path(self, batch_id: str) -> Path:
        return self.root / f"{batch_id}.json"

    def _save(self, batch: dict) -> None:
        path = self._path(batch["id"])
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(batch, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def _load(self) -> None:
        """启动时建索引；重复批次号保留首个并告警，坏文件跳过不阻断启动。"""
        for path in sorted(self.root.glob("*.json")):
            try:
                batch = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(batch, dict):
                continue
            batch_id = str(batch.get("id") or path.stem).strip()
            if not batch_id:
                continue
            batch["id"] = batch_id
            if batch.get("state") not in BATCH_STATES:
                batch["state"] = "draft"
            if not isinstance(batch.get("items"), list):
                batch["items"] = []
            if not isinstance(batch.get("runs"), list):
                batch["runs"] = []
            for item in batch["items"]:
                if not isinstance(item, dict):
                    continue
                if item.get("state") not in ITEM_STATES:
                    item["state"] = "pending"
                item.setdefault("job_id", "")
                item.setdefault("error", "")
            key = _norm_no(batch.get("batch_no"))
            if key and key in self.by_no:
                self.warnings.append(f"批次号重复，保留首个：{batch.get('batch_no')}")
            elif key:
                self.by_no[key] = batch_id
            self.batches[batch_id] = batch
            self._refresh(batch)
            # 重启后首次读取就收敛：producing 条目按 records 落 done/failed/回 pending。
            self._reconcile(batch)

    # ------------------------------------------------------------------ 内部工具
    def _catalog(self):
        catalog = self.catalog_of() if callable(self.catalog_of) else self.catalog_of
        if catalog is None or not getattr(catalog, "items", None):
            raise BatchError("视频场景暂不可用，请联系管理员。", 503)
        return catalog

    @staticmethod
    def _live(catalog, template_id) -> dict | None:
        """取模板当前状态（可能已下架）；不存在返回 None。"""
        try:
            return catalog.preview(template_id)
        except TemplateError:
            return None

    @staticmethod
    def _template_payload(item: dict) -> dict:
        return {"id": str(item.get("id")), "name": str(item.get("name") or ""),
                "url": str(item.get("url") or ""), "key": str(item.get("key") or ""),
                "duration": item.get("duration")}

    def _refresh(self, batch: dict) -> None:
        items = batch.get("items") or []
        batch["total_items"] = len(items)
        batch["done"] = sum(1 for item in items if item.get("state") == "done")
        batch["failed"] = sum(1 for item in items if item.get("state") == "failed")
        batch["pending"] = sum(1 for item in items if item.get("state") == "pending")
        batch["producing"] = sum(1 for item in items if item.get("state") == "producing")

    def _authorized(self, batch_id: str, user: dict) -> dict:
        batch = self.batches.get(str(batch_id or ""))
        if not batch:
            raise BatchError("找不到该批次。", 404)
        if user.get("role") != "admin" and batch.get("owner") != user.get("username"):
            raise BatchError("无权操作该批次。", 403)
        return batch

    def get(self, batch_id: str) -> dict | None:
        with self.mutex:
            return self.batches.get(str(batch_id or ""))

    # ------------------------------------------------------------------ 对账
    def _reconcile(self, batch: dict) -> bool:
        """把 active_job_id 对应 job 的结果收敛回条目状态；有改动返回 True。

        必须在持锁状态下调用（内部只读 self.jobs.jobs，不再取 jobs.changed，
        避免与 dispatcher 互相等待；RLock 允许重入，server 侧统一先持锁）。
        """
        job_id = str(batch.get("active_job_id") or "")
        if not job_id:
            return False
        job = self.jobs.jobs.get(job_id) if self.jobs is not None else None
        if isinstance(job, dict) and job.get("status") not in _TERMINAL_JOB:
            return False
        records: dict[str, dict] = {}
        if isinstance(job, dict):
            records = {str(rec.get("id")): rec for rec in job.get("records", [])
                       if isinstance(rec, dict)}
        success = failed = 0
        for item in batch.get("items") or []:
            if item.get("state") != "producing":
                continue
            if str(item.get("job_id") or "") != job_id:
                continue
            rec = records.get(str(item.get("template_id")))
            if rec is None:
                # job 里查不到这条：多半是建 job 后立刻崩了，回待生产最安全。
                item["state"] = "pending"
                item["job_id"] = ""
                item["error"] = ""
                continue
            status = str(rec.get("status") or "")
            if status in _RECORD_DONE:
                item["state"] = "done"
                item["error"] = ""
                success += 1
            elif status in _RECORD_UNTOUCHED:
                item["state"] = "pending"
                item["job_id"] = ""
                item["error"] = ""
            else:
                item["state"] = "failed"
                item["error"] = _redact(str(rec.get("error") or ""))[:300] or f"任务状态 {status or '未知'}"
                failed += 1
        batch["active_job_id"] = ""
        finished = time.time()
        for run in reversed(batch.get("runs") or []):
            if str(run.get("job_id")) == job_id and not run.get("finished_at"):
                run["finished_at"] = finished
                run["success"] = success
                run["failed"] = failed + int(run.pop("blocked", 0) or 0)
                break
        batch["updated_at"] = finished
        self._refresh(batch)
        self._save(batch)
        return True

    # ------------------------------------------------------------------ 估算金额
    def estimate(self, live_items: list[dict], resolution: str,
                 output_seconds: float | None = None) -> dict:
        """按（模板时长 + 输出秒数）× 单价预估；单价缺失时 amount 为 null。

        output_seconds 缺省 7 秒，与批次冻结的 duration 对齐（duration=-1 也按 7 估算）。
        """
        out_seconds = float(output_seconds if output_seconds and output_seconds > 0 else OUTPUT_SECONDS)
        seconds = [(_as_float(item.get("duration")) or DEFAULT_TEMPLATE_SECONDS) + out_seconds
                   for item in live_items]
        total = round(sum(seconds), 2)
        unit, source = self.settings.unit_price(resolution)
        count = len(seconds)
        per_item = round(total / count, 3) if count else DEFAULT_TEMPLATE_SECONDS + out_seconds
        options = []
        for choice in PRODUCE_CHOICES:
            if choice > self.settings.max_batch_run_items:
                continue
            options.append({"count": choice,
                            "amount": round(choice * per_item * unit, 2) if unit else None})
        return {
            "count": count,
            "seconds": total,
            "unit_price": unit,
            "price_source": source,
            "amount": round(total * unit, 2) if unit else None,
            "resolution": resolution,
            "options": options,
            "currency": "CNY",
            "note": f"按（模板时长 + 输出 {out_seconds:g} 秒）× 单价预估，实际以账单为准",
        }

    @staticmethod
    def output_seconds_of(batch: dict) -> float:
        """批次冻结的输出秒数：duration>0 用它，-1（跟随素材）按 7 秒估算。"""
        duration = _as_int(batch.get("duration"))
        return float(duration) if duration and duration > 0 else OUTPUT_SECONDS

    # ------------------------------------------------------------------ 公开字段
    def public(self, batch: dict) -> dict:
        with self.mutex:
            data = {key: batch.get(key) for key in (
                "id", "batch_no", "title", "owner", "state", "image_upload_id", "image_name",
                "prompt_id", "prompt", "resolution", "duration", "tag_filters", "style_no",
                "total_items", "done", "failed", "pending", "producing", "active_job_id",
                "created_at", "updated_at", "locked_at", "closed_at")}
            data["image_url"] = (f"/api/uploads/{batch['image_upload_id']}"
                                 if batch.get("image_upload_id") else "")
            data["runs"] = [dict(run) for run in (batch.get("runs") or [])]
            return data

    def _record_index(self, cache: dict[str, dict], job_id: str) -> dict[str, dict]:
        """按 job 缓存 records 索引，避免 500 条条目反复遍历同一个 job。"""
        if not job_id:
            return {}
        if job_id not in cache:
            job = self.jobs.jobs.get(job_id) if self.jobs is not None else None
            records = (job or {}).get("records") or []
            cache[job_id] = {str(rec.get("id")): rec for rec in records if isinstance(rec, dict)}
        return cache[job_id]

    def public_items(self, batch: dict, item_state: str = "") -> list[dict]:
        """条目公开字段 + download_url + verify_warn。

        - download_url：done 且有 job 时给，路径与历史分组同源；
        - verify_warn：模型实际输出偏离预期（时长不合/文件过小）时的质量提示，
          状态仍是 done，前端当提示用、不要当失败。
        """
        items = []
        cache: dict[str, dict] = {}
        for item in batch.get("items") or []:
            row = dict(item)
            job_id = str(row.get("job_id") or "")
            template_id = str(row.get("template_id") or "")
            done = row.get("state") == "done"
            row["download_url"] = (f"/api/jobs/{job_id}/download/{template_id}"
                                   if done and job_id and template_id else "")
            warn = ""
            if done and job_id:
                rec = self._record_index(cache, job_id).get(template_id) or {}
                warn = str((rec.get("verify") or {}).get("warn") or "")
            row["verify_warn"] = _redact(warn)[:200]
            items.append(row)
        if item_state:
            items = [item for item in items if item.get("state") == item_state]
        return items

    def estimate_for_pending(self, batch: dict) -> dict:
        """按「待生产条目」算预估金额，供批次详情复用（重开已锁定批次时前端不必再拉候选）。

        模板时长优先取当前清单里的实时值；模板已下架则退回默认时长，绝不因此报错。
        """
        pending = [item for item in (batch.get("items") or []) if item.get("state") == "pending"]
        live_items: list[dict] = []
        catalog = None
        if pending:
            try:
                catalog = self._catalog()
            except BatchError:
                catalog = None
        for item in pending:
            live = self._live(catalog, item.get("template_id")) if catalog is not None else None
            live_items.append(live or {})
        return self.estimate(live_items, str(batch.get("resolution") or "720P"),
                             self.output_seconds_of(batch))

    # ------------------------------------------------------------------ #1 创建
    def create(self, user: dict, data: dict) -> dict:
        batch_no = str(data.get("batch_no") or "").strip()
        title = str(data.get("title") or "").strip()
        prompt_id = str(data.get("prompt_id") or "").strip()
        prompt = str(data.get("prompt") or "").strip()
        resolution = str(data.get("resolution") or "").strip().upper()
        # 网页标配「输出固定 7 秒」；-1（跟随素材）与 2–30 的其他秒数仍接受，向后兼容老批次。
        duration = _as_int(data.get("duration"), DEFAULT_DURATION)
        upload_id = str(data.get("upload_id") or "").strip()

        if not batch_no or len(batch_no) > 40:
            raise BatchError("批次号需要 1–40 个字符。")
        if any(ch in batch_no for ch in "\r\n\t"):
            raise BatchError("批次号不能包含换行或制表符。")
        if len(title) > 60:
            raise BatchError("备注最多 60 个字符。")
        if prompt_id not in (self.jobs.prompts if self.jobs is not None else {}):
            raise BatchError("请选择有效的提示词模板。")
        if not prompt:
            raise BatchError("提示词不能为空。")
        if len(prompt) > 4000:
            raise BatchError("提示词最多 4000 个字符。")
        if resolution not in ("480P", "720P"):
            raise BatchError("画质只能是 480P 或 720P。")
        if not duration_ok(duration):
            raise BatchError("时长只能跟随素材（-1）或 2–30 秒。")
        upload = None
        if upload_id:
            upload = self.resolve_upload(upload_id, user["username"])
            if not upload:
                raise BatchError("图片已失效，请重新上传。", 404)

        with self.mutex:
            if _norm_no(batch_no) in self.by_no:
                raise BatchError("批次号已存在，请换一个。", 409)
            batch_id = uuid.uuid4().hex[:12]
            while batch_id in self.batches:
                batch_id = uuid.uuid4().hex[:12]
            now = time.time()
            batch = {
                "id": batch_id, "batch_no": batch_no, "title": title, "owner": user["username"],
                # 款号：整批一个，冻结后不改；归档时进 COS 路径与对象标签。
                "style_no": re.sub(r"[\x00-\x1f\x7f]", "", str(data.get("style_no") or "")).strip()[:40],
                "state": "draft",
                "image_upload_id": upload_id,
                "image_path": str((upload or {}).get("path") or ""),
                "image_name": str((upload or {}).get("name") or ""),
                "prompt_id": prompt_id, "prompt": prompt,
                "resolution": resolution, "duration": duration,
                "tag_filters": {},
                "items": [], "runs": [],
                "active_job_id": "",
                "total_items": 0, "done": 0, "failed": 0, "pending": 0, "producing": 0,
                "created_at": now, "updated_at": now, "locked_at": None, "closed_at": None,
            }
            self.batches[batch_id] = batch
            self.by_no[_norm_no(batch_no)] = batch_id
            self._save(batch)
            return self.public(batch)

    # ------------------------------------------------------------------ #2 绑定定妆图
    def set_image(self, batch_id: str, user: dict, data: dict) -> dict:
        upload_id = str(data.get("upload_id") or "").strip()
        if not re.fullmatch(r"[a-f0-9]{32}", upload_id):
            raise BatchError("请先上传定妆图。")
        with self.mutex:
            batch = self._authorized(batch_id, user)
            if batch.get("state") != "draft":
                raise BatchError("批次已锁定，换图请新开批次。", 409)
            if batch.get("items") or batch.get("runs"):
                raise BatchError("本批次已生产过，换图请新开批次。", 409)
            upload = self.resolve_upload(upload_id, user["username"])
            if not upload:
                raise BatchError("图片已失效，请重新上传。", 404)
            batch["image_upload_id"] = upload_id
            batch["image_path"] = str(upload.get("path") or "")
            batch["image_name"] = str(upload.get("name") or "")
            batch["updated_at"] = time.time()
            self._save(batch)
            return {"image_name": batch["image_name"], "batch": self.public(batch)}

    # ------------------------------------------------------------------ #3 候选清单
    def candidates(self, batch_id: str, user: dict, data: dict) -> dict:
        tag_filters = data.get("tag_filters") or {}
        if not isinstance(tag_filters, dict):
            raise BatchError("标签筛选条件格式无效。")
        limit = _as_int(data.get("limit"), 500) or 500
        if not 1 <= limit <= 1000:
            raise BatchError("limit 需要 1–1000。")
        with self.mutex:
            batch = self._authorized(batch_id, user)
            resolution = str(batch.get("resolution") or "720P")
            out_seconds = self.output_seconds_of(batch)
        catalog = self._catalog()
        try:
            matched = catalog.filter_items(tag_filters)
        except TemplateError as exc:
            raise BatchError(str(exc)) from None
        rows = []
        for item in matched[:limit]:
            rows.append({
                "id": str(item.get("id")),
                "name": str(item.get("name") or ""),
                "tags": list(item.get("tags") or []),
                "ready": bool(item.get("url") or item.get("key")),
            })
        ready_count = sum(1 for item in matched if item.get("url") or item.get("key"))
        return {
            "items": rows,
            "total": len(rows),
            "filtered_total": len(matched),
            "ready_count": ready_count,
            "cost_estimate": self.estimate(matched[:limit], resolution, out_seconds),
        }

    # ------------------------------------------------------------------ #4 锁定清单
    def lock(self, batch_id: str, user: dict, data: dict) -> dict:
        template_ids = data.get("template_ids")
        if not isinstance(template_ids, (list, tuple)) or not template_ids:
            raise BatchError("锁定清单不能为空。")
        if data.get("confirmed") is not True:
            raise BatchError("请二次确认后再锁定清单。")
        catalog = self._catalog()
        with self.mutex:
            batch = self._authorized(batch_id, user)
            if batch.get("state") != "draft":
                raise BatchError("清单已锁定，不能再改。", 409)
            if not batch.get("image_upload_id"):
                raise BatchError("请先绑定定妆图再锁定清单。", 409)
            if len(template_ids) > self.settings.max_batch_items:
                raise BatchError(f"清单最多 {self.settings.max_batch_items} 条。", 409)
            try:
                templates = catalog.pick_by_ids([str(value) for value in template_ids],
                                                max_items=self.settings.max_batch_items)
            except TemplateError as exc:
                raise BatchError(str(exc)) from None
            batch["items"] = [{
                "template_id": str(item.get("id")),
                "name": str(item.get("name") or ""),
                "tags": list(item.get("tags") or []),
                "state": "pending", "job_id": "", "error": "",
            } for item in templates]
            batch["tag_filters"] = dict(data.get("tag_filters") or {}) if isinstance(
                data.get("tag_filters"), dict) else {}
            batch["state"] = "locked"
            batch["locked_at"] = time.time()
            batch["updated_at"] = batch["locked_at"]
            self._refresh(batch)
            self._save(batch)
            return self.public(batch)

    # ------------------------------------------------------------------ 生产（#5 / #9 共用）
    def _start_run(self, batch: dict, user: dict, wanted: tuple[str, ...], count: int,
                   empty_message: str) -> dict:
        """在锁内取数 → 校验模板 → 建 job → 标 producing。调用方必须已持锁。"""
        if batch.get("state") != "locked":
            raise BatchError("请先锁定清单再生产。", 409)
        if batch.get("active_job_id"):
            raise BatchError("本批次有任务在跑，请等它结束。", 409)
        image_path = Path(str(batch.get("image_path") or ""))
        if not batch.get("image_path") or not image_path.is_file():
            raise BatchError("定妆图已丢失，请重新上传或新开批次。", 404)
        queue = [item for item in (batch.get("items") or []) if item.get("state") in wanted]
        if not queue:
            raise BatchError(empty_message, 409)
        catalog = self._catalog()
        taken: list[tuple[dict, dict]] = []
        blocked: list[str] = []
        for item in queue[:count]:
            live = self._live(catalog, item.get("template_id"))
            if live is None or not (live.get("url") or live.get("key")):
                # 锁定的模板已下架：标失败、本次少发一条，绝不静默替换成别的视频。
                item["state"] = "failed"
                item["job_id"] = ""
                item["error"] = "模板已不可用"
                blocked.append(str(item.get("template_id")))
            else:
                taken.append((item, live))
        if not taken:
            self._refresh(batch)
            batch["updated_at"] = time.time()
            self._save(batch)
            raise BatchError("待生产条目的模板都已不可用，请检查模板清单。", 503)
        resolution = str(batch.get("resolution") or "720P")
        # 批次冻结的输出秒数原样下发（7 / -1 / 2–30）；老批次存了非法值也兜回默认，不让 produce 卡死。
        duration = _as_int(batch.get("duration"), DEFAULT_DURATION)
        if not duration_ok(duration):
            duration = DEFAULT_DURATION
        try:
            job = self.jobs.create(
                user["username"], image_path, str(batch.get("image_name") or "定妆图"),
                [self._template_payload(live) for _, live in taken],
                str(batch.get("prompt_id")), resolution, duration,
                str(batch.get("prompt") or ""),
                batch_ref=str(batch["id"]), limit_items=len(taken),
                style_no=str(batch.get("style_no") or ""),
            )
        except ValueError as exc:
            raise BatchError(str(exc), 409) from None
        job_id = str(job["id"])
        for item, _live_item in taken:
            item["state"] = "producing"
            item["job_id"] = job_id
            item["error"] = ""
        batch["active_job_id"] = job_id
        batch.setdefault("runs", []).append({
            "job_id": job_id, "count": len(taken), "started_at": time.time(),
            "finished_at": None, "success": 0, "failed": 0, "blocked": len(blocked),
        })
        batch["updated_at"] = time.time()
        self._refresh(batch)
        self._save(batch)
        return {
            "job": job,
            "batch": self.public(batch),
            "taken": len(taken),
            "blocked": blocked,
            "cost_estimate": self.estimate([live for _, live in taken], resolution),
        }

    def produce(self, batch_id: str, user: dict, data: dict) -> dict:
        raw = data.get("count", DEFAULT_PRODUCE_COUNT)
        count = _as_int(raw, None)
        if count is None or count not in PRODUCE_CHOICES:
            raise BatchError("每次生产条数只能是 20、50 或 100。")
        if count > self.settings.max_batch_run_items:
            raise BatchError(f"单次最多生产 {self.settings.max_batch_run_items} 条。")
        with self.mutex:
            batch = self._authorized(batch_id, user)
            self._reconcile(batch)
            return self._start_run(batch, user, ("pending",), count,
                                   "没有待生产的条目，可重试失败项或关闭批次。")

    def retry_failed(self, batch_id: str, user: dict, data: dict) -> dict:
        raw = data.get("count", RETRY_MAX)
        count = _as_int(raw, None)
        if count is None or not RETRY_MIN <= count <= RETRY_MAX:
            raise BatchError(f"重试条数需要 {RETRY_MIN}–{RETRY_MAX}。")
        with self.mutex:
            batch = self._authorized(batch_id, user)
            self._reconcile(batch)
            return self._start_run(batch, user, ("failed",), count, "没有失败的条目需要重试。")

    # ------------------------------------------------------------------ #8 关闭
    def close(self, batch_id: str, user: dict) -> dict:
        with self.mutex:
            batch = self._authorized(batch_id, user)
            self._reconcile(batch)
            if batch.get("active_job_id"):
                raise BatchError("还有任务在跑，请等结束后再关闭批次。", 409)
            if batch.get("state") == "closed":
                return self.public(batch)
            batch["state"] = "closed"
            batch["closed_at"] = time.time()
            batch["updated_at"] = batch["closed_at"]
            self._save(batch)
            return self.public(batch)

    # ------------------------------------------------------------------ #14 修改提示词（锁定后仍可改）
    def update_prompt(self, batch_id: str, user: dict, data: dict) -> dict:
        """只改批次的提示词正文，其余（清单/定妆图/画质/时长）仍冻结。

        为什么锁定后允许改：`_start_run` 是在**每次生产时**才读 `batch["prompt"]`
        建任务，所以改提示词不影响已生成的视频，只影响之后的分批生产 —— 这正是
        「跑了一批发现提示词要调，不想整批重开」的用法。
        限制：有任务在跑时拒绝（避免同一次 _start_run 前后读到两份提示词的歧义）；
        批次已关闭则整个批次不再变化，一并拒绝。
        """
        prompt = str(data.get("prompt") or "").strip()
        if not prompt:
            raise BatchError("提示词不能为空。")
        if len(prompt) > 4000:
            raise BatchError("提示词最多 4000 个字符。")
        with self.mutex:
            batch = self._authorized(batch_id, user)
            self._reconcile(batch)
            if batch.get("active_job_id"):
                raise BatchError("本批次有任务在跑，等它结束后再改提示词。", 409)
            if batch.get("state") == "closed":
                raise BatchError("批次已关闭，提示词不可再修改。", 409)
            batch["prompt"] = prompt
            batch["updated_at"] = time.time()
            self._save(batch)
            return self.public(batch)

    # ------------------------------------------------------------------ #12 删除
    def delete(self, batch_id: str, user: dict) -> dict:
        """删除批次：只删 web-data/batches/<id>.json 这一个文件。

        安全口径：
          · 权限：owner 或 admin；不存在 404、非 owner 403（与详情同一套 _authorized）。
          · 运行中禁删：`active_job_id` 非空（job 未终态）→ 409，避免删掉正在被生产读写的批次。
          · **不碰任何产物**：web-data/output/、web-data/jobs/ 下的文件不删不移动——
            已生成的视频属于 job，删批次不等于删视频；job 的 batch_ref 也保持原值（历史数据不回写），
            历史分组按「批次文件是否还在」判断归属，删掉后这些 job 自然落回 __ungrouped__。
          · draft / locked / closed 三态都可删，只要不在跑。
          · 全程持 JobManager.lock（self.mutex 是同一个 RLock），与其他批次/生产操作互斥。
        """
        with self.mutex:
            batch = self._authorized(batch_id, user)
            self._reconcile(batch)         # 先对账：job 已终态则清空 active_job_id，才允许删
            if batch.get("active_job_id"):
                raise BatchError("批次正在生产中，请等待生产结束或取消任务后再删除", 409)
            target = batch["id"]
            try:
                # 先删文件再摘索引：落盘失败就整体失败（500），不留「内存没了、磁盘还在」的幽灵。
                self._path(target).unlink(missing_ok=True)
            except OSError as exc:
                raise BatchError(f"批次文件删除失败：{exc}", 500) from None
            self.batches.pop(target, None)
            key = _norm_no(batch.get("batch_no"))
            # 只摘掉指向本批次的号码索引；批次号重复时旧批次仍占着索引，不能连带删掉。
            if key and self.by_no.get(key) == target:
                self.by_no.pop(key, None)
            return {"id": target}

    # ------------------------------------------------------------------ #6 列表
    def list_for(self, user: dict, all_users: bool = False, owner: str = "") -> list[dict]:
        filter_owner = str(owner or "") if all_users else ""
        with self.mutex:
            rows = []
            for batch in self.batches.values():
                if not all_users and batch.get("owner") != user.get("username"):
                    continue
                if filter_owner and batch.get("owner") != filter_owner:
                    continue
                self._reconcile(batch)
                rows.append(self.public(batch))
            rows.sort(key=lambda row: row.get("created_at") or 0, reverse=True)
            return rows

    # ------------------------------------------------------------------ #7 详情
    def detail(self, batch_id: str, user: dict, item_state: str = "") -> tuple[dict, list[dict]]:
        if item_state and item_state not in ITEM_STATES:
            raise BatchError("条目状态只能是 pending/producing/done/failed。")
        with self.mutex:
            batch = self._authorized(batch_id, user)
            self._reconcile(batch)
            data = self.public(batch)
            # 详情页要能直接显示「再生产这批大概多少钱」，不必先拉候选。
            data["cost_estimate"] = self.estimate_for_pending(batch)
            return data, self.public_items(batch, item_state)

    # ------------------------------------------------------------------ #10 打包
    def zip_path(self, batch_id: str, user: dict) -> Path:
        files: list[tuple[str, Path]] = []
        with self.mutex:
            batch = self._authorized(batch_id, user)
            self._reconcile(batch)
            for item in batch.get("items") or []:
                if item.get("state") != "done":
                    continue
                job = self.jobs.jobs.get(str(item.get("job_id") or "")) if self.jobs else None
                out_dir = Path(str(job.get("out_dir"))) if job and job.get("out_dir") else None
                if not out_dir:
                    continue
                video = out_dir / f"{item.get('template_id')}.mp4"
                if video.is_file():
                    files.append((f"{item.get('template_id')}.mp4", video))
        if not files:
            raise BatchError("暂时没有可下载的视频。", 404)
        zip_path = self.settings.data_dir / "tmp" / f"batch-{batch['id']}.zip"
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for arcname, path in files:
                    zf.write(path, arcname)
        except OSError as exc:
            raise BatchError(f"打包失败：{exc}", 500) from None
        return zip_path

    # ------------------------------------------------------------------ #11 历史分组
    def history_groups(self, user: dict, all_users: bool = False, owner: str = "") -> list[dict]:
        """按 batch_ref 聚合 job；分不到现存批次的（试跑 + 老任务 + 批次已删除）统一进 __ungrouped__。

        归属只认「批次文件还在不在」：job.batch_ref 不回写（历史数据只读），批次一旦被删除，
        它名下的 job 会自动落回 __ungrouped__，不会留下一个点不开的空分组。
        owner 仅对管理员生效：只看某个账户的历史。
        """
        filter_owner = str(owner or "") if all_users else ""
        with self.mutex:
            groups: dict[str, dict] = {}
            for job in self.jobs.jobs.values() if self.jobs else ():
                if not all_users and job.get("owner") != user.get("username"):
                    continue
                if filter_owner and job.get("owner") != filter_owner:
                    continue
                ref = str(job.get("batch_ref") or "")
                key = ref if ref and ref in self.batches else UNGROUPED_KEY
                slot = groups.setdefault(key, {
                    "key": key, "job_count": 0, "total": 0, "success": 0, "failed": 0,
                    "first_at": None, "last_at": None, "owners": set(),
                    # 按通道计数：历史页要能只显示大批量，也要能把「未分组」里的试跑挑出来。
                    "small_jobs": 0, "big_jobs": 0,
                })
                slot["job_count"] += 1
                if str(job.get("lane") or "") == "small":
                    slot["small_jobs"] += 1
                else:
                    slot["big_jobs"] += 1
                slot["owners"].add(str(job.get("owner") or ""))
                created = job.get("created_at") or 0
                stamp = job.get("finished_at") or created
                slot["first_at"] = created if slot["first_at"] is None else min(slot["first_at"], created)
                slot["last_at"] = stamp if slot["last_at"] is None else max(slot["last_at"], stamp)
                for rec in job.get("records") or []:
                    status = str(rec.get("status") or "")
                    slot["total"] += 1
                    if status == "success":
                        slot["success"] += 1
                    elif status in {"failed", "invalid", "expired", "canceled"}:
                        slot["failed"] += 1
            rows = []
            for key, slot in groups.items():
                batch = self.batches.get(key) if key != UNGROUPED_KEY else None
                batch_no = str((batch or {}).get("batch_no") or "")
                # 管理员看全部时，分组卡片要能看出这批是谁跑的；多人混在一起就标"多人"。
                owners = {name for name in slot.get("owners", set()) if name}
                owner_label = next(iter(owners)) if len(owners) == 1 else ("多人" if owners else "")
                rows.append({
                    "key": key,
                    "batch_no": batch_no if key != UNGROUPED_KEY else "未分组",
                    "is_ungrouped": key == UNGROUPED_KEY,
                    "small_jobs": slot["small_jobs"], "big_jobs": slot["big_jobs"],
                    "owner": owner_label,
                    "job_count": slot["job_count"], "total": slot["total"],
                    "success": slot["success"], "failed": slot["failed"],
                    "first_at": slot["first_at"], "last_at": slot["last_at"] or 0,
                })
            rows.sort(key=lambda row: (row["is_ungrouped"], -(row["last_at"] or 0)))
            return rows

    def history_items(self, key: str, user: dict, offset: int = 0, limit: int = 200,
                      owner: str = "") -> dict:
        offset = max(0, int(offset or 0))
        limit = max(1, min(500, int(limit or 200)))
        owner = str(owner or "") if user.get("role") == "admin" else ""
        with self.mutex:
            if key != UNGROUPED_KEY:
                batch = self.batches.get(str(key))
                if not batch:
                    raise BatchError("找不到该批次。", 404)
                if user.get("role") != "admin" and batch.get("owner") != user.get("username"):
                    raise BatchError("无权查看该批次。", 403)
                batch_no = str(batch.get("batch_no") or "")
                jobs = [job for job in (self.jobs.jobs.values() if self.jobs else ())
                        if str(job.get("batch_ref") or "") == key]
            else:
                batch_no = "未分组"
                # 与 history_groups 同口径：没有 batch_ref，或 batch_ref 指向的批次已不存在。
                mine = [job for job in (self.jobs.jobs.values() if self.jobs else ())
                        if not job.get("batch_ref") or str(job.get("batch_ref")) not in self.batches]
                if user.get("role") != "admin":
                    mine = [job for job in mine if job.get("owner") == user.get("username")]
                jobs = mine
            rows = []
            for job in sorted(jobs, key=lambda item: item.get("created_at") or 0):
                job_owner = job.get("owner")
                if user.get("role") != "admin" and job_owner != user.get("username"):
                    continue
                if owner and job_owner != owner:
                    continue
                names = {str(tpl.get("id")): str(tpl.get("name") or "")
                         for tpl in job.get("templates") or [] if isinstance(tpl, dict)}
                for rec in job.get("records") or []:
                    template_id = str(rec.get("id"))
                    status = str(rec.get("status") or "")
                    rows.append({
                        "job_id": str(job.get("id")),
                        "template_id": template_id,
                        "template_name": names.get(template_id, ""),
                        "status": status,
                        "seconds": rec.get("seconds"),
                        "download_url": (f"/api/jobs/{job.get('id')}/download/{template_id}"
                                         if status == "success" else ""),
                        "archive_warning": ("视频已生成，但 COS 归档失败，请联系管理员。"
                                            if rec.get("archive_error") else ""),
                        # 与批次条目同口径：done 的质量提示，状态仍是 success。
                        "verify_warn": _redact(str((rec.get("verify") or {}).get("warn") or ""))[:200],
                    })
            total = len(rows)
            return {"key": key, "batch_no": batch_no,
                    "items": rows[offset:offset + limit],
                    "total": total, "offset": offset, "limit": limit}
