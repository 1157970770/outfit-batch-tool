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

import hashlib
import json
import os
import re
import threading
import time
import uuid
import zipfile
from pathlib import Path

from app.web.jobs import (
    MAX_IMAGE_SLOTS, UPLOAD_PATH_RE, _FAILED_RECORD_STATES, _redact,
    clean_style_no, clean_tags, duration_ok, image_slots,
)
from app.web.style_usage import StyleUsageLedger
from app.web.templates import TemplateError

BATCH_STATES = ("draft", "locked", "closed")
ITEM_STATES = ("pending", "producing", "done", "failed")
# v3：单次生产的快捷档位 5 / 10 / 20 / 50 / 100；接口同时接受 PRODUCE_MIN–上限的
# **任意整数**（7、13、33 这类非 5 倍数同样放行），前端"自定条数"就靠这条。
# 唯一例外：清单剩余不足 PRODUCE_MIN 时，允许一次提交「全部剩余」（否则零头永远跑不掉）。
PRODUCE_CHOICES = (5, 10, 20, 50, 100)
PRODUCE_MIN = 5
DEFAULT_PRODUCE_COUNT = 50
RETRY_MIN = 1
RETRY_MAX = 100
UNGROUPED_KEY = "__ungrouped__"
# job.image_path 里的上传图：web-data/uploads/<xx>/<32位id>.<ext>（Windows 反斜杠同样匹配）。
# 命中即说明这张图能通过 /api/uploads/<id>（带 owner 校验）取回来。
# 命名保持对外不变，正则统一收在 jobs.py（读老 job 时两边用同一套）。
UPLOAD_IMAGE_RE = UPLOAD_PATH_RE

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
        # 款号-模板记账本：批次生产提交时记账，供前端「同款号不重复用模板」标记。
        self.usage = StyleUsageLedger(settings.data_dir)
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
            # 上一次生产建了 job 但批次没来得及标 producing（写盘失败 / 进程被杀）：
            # 先认领，别让用户再点一次生产导致同批模板被重复提交。
            job_id = self._adopt_pending_run(batch)
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

    def _save_quiet(self, batch: dict) -> None:
        """尽力而为地落盘：用于已经无法回头（job 已建）的收尾路径。"""
        try:
            self._save(batch)
        except OSError:
            pass

    def _adopt_pending_run(self, batch: dict) -> str:
        """认领 `pending_run` 标记，返回可对账的 job id（没有则空串）。

        `_start_run` 在 `jobs.create` **之前**就把标记落盘，建完立刻把 job_id 写回，
        所以进程在中途被杀 / 写盘失败时，磁盘上一定留有线索：
          · 没有 job_id → 任务根本没建，条目保持 pending，清掉标记即可；
          · 有 job_id 但 job 查不到 → 云端状态无法确认，标失败交给人工核对，不自动重发；
          · 有 job_id 且 job 在 → 接回 producing，让正常对账继续收敛。
        """
        pending = batch.get("pending_run")
        if not isinstance(pending, dict):
            return ""
        job_id = str(pending.get("job_id") or "")
        template_ids = {str(tid) for tid in (pending.get("template_ids") or [])}
        job = self.jobs.jobs.get(job_id) if (self.jobs is not None and job_id) else None
        if not job_id:
            batch.pop("pending_run", None)
        elif not isinstance(job, dict):
            for item in batch.get("items") or []:
                if (str(item.get("template_id")) in template_ids
                        and item.get("state") == "pending"):
                    item["state"] = "failed"
                    item["error"] = "上次生产中断：对应任务已找不到，请核对后重试。"
            batch.pop("pending_run", None)
        else:
            for item in batch.get("items") or []:
                if (str(item.get("template_id")) in template_ids
                        and item.get("state") == "pending"):
                    item["state"] = "producing"
                    item["job_id"] = job_id
                    item["error"] = ""
            batch["active_job_id"] = job_id
            runs = batch.setdefault("runs", [])
            if not any(str(run.get("job_id")) == job_id for run in runs):
                runs.append({
                    "job_id": job_id, "count": len(template_ids),
                    "started_at": float(pending.get("started_at") or time.time()),
                    "finished_at": None, "success": 0, "failed": 0, "blocked": 0,
                })
            batch.pop("pending_run", None)
        batch["updated_at"] = time.time()
        self._refresh(batch)
        self._save(batch)
        return job_id if isinstance(job, dict) else ""

    # ------------------------------------------------------------------ 估算金额
    @staticmethod
    def _money_view(user: dict, estimate: dict) -> dict:
        """P1-5：金额与单价只对管理员下发，普通用户剥离（可反推 wan.price_per_second）。

        options 的 count 档位保留：前端生产按钮的档位靠它（batch.js:84），只剥金额。
        """
        if str((user or {}).get("role") or "") == "admin":
            return estimate
        view = {k: v for k, v in estimate.items()
                if k not in ("unit_price", "price_source", "amount")}
        if isinstance(view.get("options"), list):
            view["options"] = [{k: v for k, v in opt.items() if k != "amount"}
                               for opt in view["options"] if isinstance(opt, dict)]
        return view

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
    # ------------------------------------------------------------------ 多图槽位
    def _image_slots(self, batch: dict) -> list[dict]:
        """统一读图：images[] 为真源；老批次回退成单元素列表。顺序 = 槽位 = 模型「图N」。"""
        return image_slots(batch)

    @staticmethod
    def _sync_image_aliases(batch: dict) -> None:
        """images[0] 回写单值别名，让老代码/老前端/历史对比层继续只认主图。"""
        slots = image_slots(batch)
        first = slots[0] if slots else {}
        batch["image_upload_id"] = str(first.get("upload_id") or "")
        batch["image_path"] = str(first.get("path") or "")
        batch["image_name"] = str(first.get("name") or "")

    def _parse_uploads(self, user: dict, data: dict, *, allow_empty: bool) -> list[dict]:
        """把 upload_ids（回退 upload_id）解析成有序槽位；逐张校验归属与文件存在。"""
        raw = data.get("upload_ids")
        if raw is None:
            single = str(data.get("upload_id") or "").strip()
            raw = [single] if single else []
        if not isinstance(raw, list):
            raise BatchError("图片参数无效，请重新上传。")
        ids = [str(item or "").strip() for item in raw]
        ids = [item for item in ids if item]
        if not ids:
            if allow_empty:
                return []
            raise BatchError("请先上传定妆图。")
        if len(ids) > MAX_IMAGE_SLOTS:
            raise BatchError(
                f"一次最多 {MAX_IMAGE_SLOTS} 张图片（1 张定妆图 + 9 张细节图），当前 {len(ids)} 张。")
        if len(set(ids)) != len(ids):
            raise BatchError("同一张图片不能重复提交。")
        slots = []
        for index, upload_id in enumerate(ids, 1):
            if not re.fullmatch(r"[a-f0-9]{32}", upload_id):
                raise BatchError(f"第 {index} 张图片已失效，请重新上传。", 404)
            meta = self.resolve_upload(upload_id, user["username"])
            if not meta:
                raise BatchError(f"第 {index} 张图片已失效，请重新上传。", 404)
            slots.append({"upload_id": upload_id,
                          "name": str(meta.get("name") or ""),
                          "path": str(meta.get("path") or "")})
        return slots

    def public(self, batch: dict) -> dict:
        with self.mutex:
            data = {key: batch.get(key) for key in (
                "id", "batch_no", "title", "owner", "state", "image_upload_id", "image_name",
                "prompt_id", "prompt", "resolution", "duration", "tag_filters", "style_no", "tags",
                "total_items", "done", "failed", "pending", "producing", "active_job_id",
                "created_at", "updated_at", "locked_at", "closed_at")}
            # image_url 语义固定为「主图（定妆图）」，老前端/历史对比层靠它，不能改。
            data["image_url"] = (f"/api/uploads/{data['image_upload_id']}"
                                 if data.get("image_upload_id") else "")
            slots = self._image_slots(batch)
            data["image_urls"] = [f"/api/uploads/{s['upload_id']}" for s in slots if s.get("upload_id")]
            data["image_names"] = [s["name"] for s in slots]
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
        # 多图：upload_ids 有序（槽位 1 = 定妆图）；缺省时回退老的单个 upload_id。
        # 草稿态允许 0 张（后补），所以 allow_empty=True。
        slots = self._parse_uploads(user, data, allow_empty=True)

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
        # 6 栏位标签：上衣必填（进 COS 归档路径），其余选填。旧批次只有 style_no，不强制补填。
        try:
            batch_tags = clean_tags(data.get("tags"))
        except ValueError as exc:
            raise BatchError(str(exc)) from None
        with self.mutex:
            if _norm_no(batch_no) in self.by_no:
                raise BatchError("批次号已存在，请换一个。", 409)
            batch_id = uuid.uuid4().hex[:12]
            while batch_id in self.batches:
                batch_id = uuid.uuid4().hex[:12]
            now = time.time()
            batch = {
                "id": batch_id, "batch_no": batch_no, "title": title, "owner": user["username"],
                # 标签：6 栏位（上衣必填）；style_no 只保留 v0.9.0 老批次的值。
                "style_no": re.sub(r"[\x00-\x1f\x7f]", "", str(data.get("style_no") or "")).strip()[:40],
                "tags": batch_tags,
                "state": "draft",
                # images[] 是唯一真源；三个单值字段是主图别名，供老前端/老代码读取。
                "images": slots,
                "image_upload_id": (slots[0]["upload_id"] if slots else ""),
                "image_path": (slots[0]["path"] if slots else ""),
                "image_name": (slots[0]["name"] if slots else ""),
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
        """草稿态整组重设（换定妆图/重新选 1+9 张）；锁定后只能走 add/remove_images。"""
        slots = self._parse_uploads(user, data, allow_empty=False)
        with self.mutex:
            batch = self._authorized(batch_id, user)
            if batch.get("state") != "draft":
                raise BatchError("批次已锁定，换图请新开批次。", 409)
            if batch.get("items") or batch.get("runs"):
                raise BatchError("本批次已生产过，换图请新开批次。", 409)
            batch["images"] = slots
            self._sync_image_aliases(batch)
            batch["updated_at"] = time.time()
            self._save(batch)
            return {"image_name": batch["image_name"], "batch": self.public(batch)}

    def add_images(self, batch_id: str, user: dict, data: dict) -> dict:
        """锁定后仍可追加细节图：只落到批次，对**之后**的生产生效（已建 job 持自己的副本）。

        不加「有任务在跑就 409」：_start_run 在同一把锁内一次性读完图并建 job，
        追加只可能落在生产前或生产后，不存在读一半被改。
        """
        incoming = self._parse_uploads(user, data, allow_empty=False)
        with self.mutex:
            batch = self._authorized(batch_id, user)
            if batch.get("state") == "closed":
                raise BatchError("批次已关闭，不能再改图。", 409)
            current = self._image_slots(batch)
            if len(current) + len(incoming) > MAX_IMAGE_SLOTS:
                raise BatchError(
                    f"已绑定 {len(current)} 张，最多 {MAX_IMAGE_SLOTS} 张；本次只能再加 "
                    f"{max(0, MAX_IMAGE_SLOTS - len(current))} 张。")
            batch["images"] = current + incoming
            self._sync_image_aliases(batch)
            batch["updated_at"] = time.time()
            self._save(batch)
            return {"batch": self.public(batch), "slot_count": len(batch["images"])}

    def remove_images(self, batch_id: str, user: dict, data: dict) -> dict:
        """删除细节图（槽位 1 定妆图不可删）；删除后自动左移，保证「图N」不错位。"""
        raw_index = data.get("index")
        upload_id = str(data.get("upload_id") or "").strip()
        with self.mutex:
            batch = self._authorized(batch_id, user)
            if batch.get("state") == "closed":
                raise BatchError("批次已关闭，不能再改图。", 409)
            slots = self._image_slots(batch)
            if raw_index is not None:
                try:
                    index = int(raw_index)
                except (TypeError, ValueError):
                    raise BatchError("图片序号无效。") from None
            else:
                index = next((i + 1 for i, slot in enumerate(slots)
                              if slot.get("upload_id") == upload_id), 0)
            if index <= 1:
                raise BatchError("定妆图不能删除；要换请整批重开。")
            if index > len(slots) or not slots:
                raise BatchError("要删除的细节图不存在。", 404)
            slots.pop(index - 1)
            batch["images"] = slots
            self._sync_image_aliases(batch)
            batch["updated_at"] = time.time()
            self._save(batch)
            return {"batch": self.public(batch), "slot_count": len(slots)}

    # ------------------------------------------------------------------ #3 候选清单
    def candidates(self, batch_id: str, user: dict, data: dict) -> dict:
        tag_filters = data.get("tag_filters") or {}
        if not isinstance(tag_filters, dict):
            raise BatchError("标签筛选条件格式无效。")
        limit = _as_int(data.get("limit"), 500) or 500
        if not 1 <= limit <= 1000:
            raise BatchError("limit 需要 1–1000。")
        # 评分筛选：rating_min=3 表示只要打了 3 星及以上的；0 / 缺省 = 不过滤
        rating_min = _as_int(data.get("rating_min"), 0) or 0
        if not 0 <= rating_min <= 5:
            raise BatchError("rating_min 需要 0–5。")
        # 排序：rating = 评分从高到低（同分按编号升序稳定排列）；缺省保持标签库原顺序
        sort = str(data.get("sort") or "").strip().lower()
        if sort not in ("", "rating"):
            raise BatchError("sort 仅支持空（默认）或 rating（评分高到低）。")
        with self.mutex:
            batch = self._authorized(batch_id, user)
            resolution = str(batch.get("resolution") or "720P")
            out_seconds = self.output_seconds_of(batch)
        catalog = self._catalog()
        try:
            matched = catalog.filter_items(tag_filters)
        except TemplateError as exc:
            raise BatchError(str(exc)) from None
        if rating_min > 0:
            matched = [item for item in matched
                       if int(item.get("rating") or 0) >= rating_min]
        if sort == "rating":
            matched = sorted(matched,
                             key=lambda item: (-int(item.get("rating") or 0), str(item.get("id"))))
        rows = []
        for item in matched[:limit]:
            rows.append({
                "id": str(item.get("id")),
                "name": str(item.get("name") or ""),
                "tags": list(item.get("tags") or []),
                "ready": bool(item.get("url") or item.get("key")),
                "rating": int(item.get("rating") or 0),
            })
        ready_count = sum(1 for item in matched if item.get("url") or item.get("key"))
        return {
            "items": rows,
            "total": len(rows),
            "filtered_total": len(matched),
            "ready_count": ready_count,
            "cost_estimate": self._money_view(user, self.estimate(matched[:limit], resolution, out_seconds)),
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
            if not self._image_slots(batch):
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
    def _quota_check(self, user: dict, count: int) -> None:
        """账户生成数量上限：0/缺省 = 不限。超了直接拒绝，不部分提交。
        settings.users 是 web.json 里的账户表；videos_used 与小批量同口径。"""
        # 用 getattr 兜底：单测/诊断脚本里的 settings 是 SimpleNamespace，没有 users 字段。
        info = (getattr(self.settings, "users", None) or {}).get(
            str((user or {}).get("username") or "")) or {}
        try:
            quota = max(0, int(info.get("quota") or 0))
        except (TypeError, ValueError):
            quota = 0
        if quota <= 0:
            return
        owner = str((user or {}).get("username") or "")
        used = self.jobs.videos_used(owner)
        if used + count > quota:
            raise BatchError(f"生成数量已达上限：该账户已用 {used} 条 / 上限 {quota} 条，"
                             f"本次 {count} 条会超出额度；请联系管理员调整。", 403)

    def _start_run(self, batch: dict, user: dict, wanted: tuple[str, ...], count: int,
                   empty_message: str) -> dict:
        """在锁内取数 → 校验模板 → 建 job → 标 producing。调用方必须已持锁。"""
        if batch.get("state") != "locked":
            raise BatchError("请先锁定清单再生产。", 409)
        if batch.get("active_job_id"):
            raise BatchError("本批次有任务在跑，请等它结束。", 409)
        slots = self._image_slots(batch)
        if not slots:
            raise BatchError("定妆图已丢失，请重新上传或新开批次。", 404)
        for index, slot in enumerate(slots, 1):
            if not Path(str(slot.get("path") or "")).is_file():
                raise BatchError(f"第 {index} 张批次图片已丢失，请重新上传或新开批次。", 404)
        image_path = Path(slots[0]["path"])
        queue = [item for item in (batch.get("items") or []) if item.get("state") in wanted]
        if not queue:
            raise BatchError(empty_message, 409)
        # 额度校验放在改任何状态之前：超限就是纯拒绝，条目一个都不动。
        self._quota_check(user, min(len(queue), max(1, int(count or 0))))
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
        # 先落「本次生产已开始」的标记，再建 job。jobs.create 之后任何一步失败（写盘、
        # 记账、标记条目）都不会让批次看起来还能再点一次生产——否则同一批条目会再建
        # 一个 job，云端会真的再出一遍片、再收一次钱。
        marker = {
            "template_ids": [str(item.get("template_id")) for item, _live_item in taken],
            "job_id": "", "started_at": time.time(),
        }
        batch["pending_run"] = marker
        batch["updated_at"] = marker["started_at"]
        try:
            self._save(batch)
        except OSError as exc:
            batch.pop("pending_run", None)
            raise BatchError(
                f"批次状态写入失败，本次没有创建任务，请稍后重试（{exc}）", 503) from None
        try:
            job = self.jobs.create(
                user["username"], image_path, str(batch.get("image_name") or "定妆图"),
                [self._template_payload(live) for _, live in taken],
                str(batch.get("prompt_id")), resolution, duration,
                str(batch.get("prompt") or ""),
                batch_ref=str(batch["id"]), limit_items=len(taken),
                tags=dict(batch.get("tags") or {}),
                style_no=str(batch.get("style_no") or ""),
                images=slots,
            )
        except ValueError as exc:
            batch.pop("pending_run", None)
            self._save_quiet(batch)
            raise BatchError(str(exc), 409) from None
        job_id = str(job["id"])
        marker["job_id"] = job_id
        try:
            # 任务已建：立刻把 job id 落盘，之后哪怕进程被杀也能在 _reconcile 里认领。
            self._save(batch)
        except OSError as exc:
            raise BatchError(
                f"任务已创建（{job_id}），但批次状态写入失败：{exc}；"
                "请勿重复生产，刷新后查看，或联系管理员核对。", 503) from None
        # 款号-模板记账（提交时记，失败重试也覆盖为最近一次）：纯审计，失败不能影响已建好的任务。
        try:
            top = str((batch.get("tags") or {}).get("top") or "").strip() or clean_style_no(batch.get("style_no"))
            self.usage.record(top, [str(item.get("template_id")) for item, _ in taken],
                              batch_no=str(batch.get("batch_no") or ""), batch_id=str(batch["id"]))
        except Exception:  # noqa: BLE001 - 记账本是附加信息，不能拖垮生产
            self.warnings.append(f"批次 {batch['id']} 的款号记账失败（不影响出片）")
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
        batch.pop("pending_run", None)
        try:
            self._refresh(batch)
            self._save(batch)
        except OSError as exc:
            batch["pending_run"] = marker   # 保住内存里的认领凭据，刷新即对账
            raise BatchError(
                f"任务已创建（{job_id}），但批次状态写入失败：{exc}；"
                "请刷新后查看，不要重复生产。", 503) from None
        return {
            "job": job,
            "batch": self.public(batch),
            "taken": len(taken),
            "blocked": blocked,
            "cost_estimate": self._money_view(user, self.estimate([live for _, live in taken], resolution)),
        }

    def produce(self, batch_id: str, user: dict, data: dict) -> dict:
        raw = data.get("count", DEFAULT_PRODUCE_COUNT)
        count = _as_int(raw, None)
        max_run = self.settings.max_batch_run_items
        if count is None or count <= 0:
            raise BatchError("每次生产条数需要是正整数。")
        if count > max_run:
            raise BatchError(f"单次最多生产 {max_run} 条。")
        with self.mutex:
            batch = self._authorized(batch_id, user)
            self._reconcile(batch)
            if count < PRODUCE_MIN:
                # 低于最小生成单位只有一种合法情形：清单本身只剩这么多（跑掉零头）。
                # 否则 3 条也能随时插队，最小单位就形同虚设。
                pending = sum(1 for item in (batch.get("items") or [])
                              if str(item.get("state") or "") == "pending")
                if pending and count != pending:
                    raise BatchError(
                        f"每次最少生产 {PRODUCE_MIN} 条；清单只剩 {pending} 条时可以一次提交全部剩余。")
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
            data["cost_estimate"] = self._money_view(user, self.estimate_for_pending(batch))
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
        # 每次打包一个唯一文件名：固定名会让同一批次的两个并发下载互相 truncate，
        # 客户端拿到半截 zip（调用方负责发完就删）。
        zip_path = (self.settings.data_dir / "tmp"
                    / f"batch-{batch['id']}-{uuid.uuid4().hex[:10]}.zip")
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for arcname, path in files:
                    zf.write(path, arcname)
        except OSError as exc:
            try:
                zip_path.unlink()
            except OSError:
                pass
            raise BatchError(f"打包失败：{exc}", 500) from None
        return zip_path

    # ------------------------------------------------------------------ #11 历史分组
    def history_groups(self, user: dict, all_users: bool = False, owner: str = "",
                       group_by: str = "batch") -> list[dict]:
        """按 batch_ref 聚合 job；分不到现存批次的（试跑 + 老任务 + 批次已删除）进未分组。

        归属只认「批次文件还在不在」：job.batch_ref 不回写（历史数据只读），批次一旦被删除，
        它名下的 job 会自动落回未分组，不会留下一个点不开的空分组。
        owner 仅对管理员生效：只看某个账户的历史。
        group_by="owner"（仅管理员看全部用户时生效）：把未分组再按账户拆成
        `__ungrouped__:<owner>` 虚拟组，避免多个账户的试跑挤成一张大列表。
        虚拟 key 只存在于接口层，job / batch 的落盘结构一律不动。
        """
        filter_owner = str(owner or "") if all_users else ""
        split_owner = bool(all_users and group_by == "owner")
        with self.mutex:
            groups: dict[str, dict] = {}
            for job in self.jobs.jobs.values() if self.jobs else ():
                if not all_users and job.get("owner") != user.get("username"):
                    continue
                if filter_owner and job.get("owner") != filter_owner:
                    continue
                ref = str(job.get("batch_ref") or "")
                if ref and ref in self.batches:
                    key = ref
                elif split_owner:
                    key = f"{UNGROUPED_KEY}:{str(job.get('owner') or 'unknown')}"
                else:
                    key = UNGROUPED_KEY
                slot = groups.setdefault(key, {
                    "key": key, "job_count": 0, "total": 0,
                    "success": 0, "failed": 0, "unfinished": 0,
                    "first_at": None, "last_at": None, "owners": set(), "sig": [],
                    # 按通道计数：历史页要能只显示大批量，也要能把「未分组」里的试跑挑出来。
                    "small_jobs": 0, "big_jobs": 0,
                })
                slot["job_count"] += 1
                if str(job.get("lane") or "") == "small":
                    slot["small_jobs"] += 1
                else:
                    slot["big_jobs"] += 1
                slot["owners"].add(str(job.get("owner") or ""))
                # 明细状态指纹：前端用它判断已展开的分组要不要重载。
                # 只看成功/失败计数会漏掉「A 条 running→success、B 条 success→running」
                # 这种互相抵消的变化，明细就会停在旧状态。
                slot["sig"].append(
                    f"{job.get('id')}:{job.get('status')}:"
                    + ",".join(f"{str(rec.get('id'))}={str(rec.get('status') or '')}"
                               for rec in job.get("records") or []))
                created = job.get("created_at") or 0
                stamp = job.get("finished_at") or created
                slot["first_at"] = created if slot["first_at"] is None else min(slot["first_at"], created)
                slot["last_at"] = stamp if slot["last_at"] is None else max(slot["last_at"], stamp)
                for rec in job.get("records") or []:
                    status = str(rec.get("status") or "")
                    slot["total"] += 1
                    # 与 jobs.work_for / 账户用量完全同口径：非成功终态全算失败侧，
                    # 其余（running/queued/pending/空）算进行中，保证 total 三者之和。
                    # 以前这里写死 4 种失败状态，unknown/timeout/skipped 和进行中
                    # 都会凭空消失，卡片数字与账户用量对不上。
                    if status == "success":
                        slot["success"] += 1
                    elif status in _FAILED_RECORD_STATES:
                        slot["failed"] += 1
                    else:
                        slot["unfinished"] += 1
            rows = []
            for key, slot in groups.items():
                is_owner_group = key.startswith(UNGROUPED_KEY + ":")
                is_ungrouped = key == UNGROUPED_KEY or is_owner_group
                batch = None if is_ungrouped else self.batches.get(key)
                batch_no = str((batch or {}).get("batch_no") or "")
                # 管理员看全部时，分组卡片要能看出这批是谁跑的；多人混在一起就标"多人"。
                owners = {name for name in slot.get("owners", set()) if name}
                owner_label = next(iter(owners)) if len(owners) == 1 else ("多人" if owners else "")
                rows.append({
                    "key": key,
                    "batch_no": "未分组" if is_ungrouped else batch_no,
                    "group_kind": "owner" if is_owner_group else "batch",
                    "is_ungrouped": is_ungrouped,
                    "small_jobs": slot["small_jobs"], "big_jobs": slot["big_jobs"],
                    "owner": owner_label,
                    "job_count": slot["job_count"], "total": slot["total"],
                    "success": slot["success"], "failed": slot["failed"],
                    "unfinished": slot["unfinished"],
                    "state_sig": hashlib.md5("|".join(sorted(slot["sig"])).encode("utf-8")).hexdigest()[:12],
                    "first_at": slot["first_at"], "last_at": slot["last_at"] or 0,
                })
            # 统一按最近更新倒序：以前把「未分组」钉在最后，最新的试跑反而被
            # 压在一堆老批次下面，管理员一进历史页看到的全是旧数据。
            rows.sort(key=lambda row: -(row["last_at"] or 0))
            return rows

    def history_items(self, key: str, user: dict, offset: int = 0, limit: int = 200,
                      owner: str = "") -> dict:
        offset = max(0, int(offset or 0))
        limit = max(1, min(500, int(limit or 200)))
        owner = str(owner or "") if user.get("role") == "admin" else ""
        with self.mutex:
            if key.startswith(UNGROUPED_KEY + ":"):
                # 管理员按账户拆出来的虚拟组：只取该账户、且不属于任何现存批次的任务。
                if user.get("role") != "admin":
                    raise BatchError("无权查看该分组。", 403)
                target_owner = key[len(UNGROUPED_KEY) + 1:]
                batch_no = "未分组"
                if owner and owner != target_owner:
                    jobs = []          # 筛选与虚拟组冲突时返回空，不能放宽到别的账户
                else:
                    jobs = [job for job in (self.jobs.jobs.values() if self.jobs else ())
                            if (not job.get("batch_ref")
                                or str(job.get("batch_ref")) not in self.batches)
                            and str(job.get("owner") or "") == target_owner]
            elif key != UNGROUPED_KEY:
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
                # 这条记录对应的定妆图 = job 自己的素材图。批次有批次级定妆图，但试跑/未分组
                # 的历史任务没有批次，只有 job 自己这张才对得上；只有落在 uploads/<xx>/<id>.<ext>
                # 的才走带权限校验的 /api/uploads 接口，其它来源一律不给 URL。
                image_url = ""
                # 优先读多图真源的首张，老 job 由 image_slots() 从 image_path 反解兜底；
                # 历史对比层本次仍只展示主图一张。
                job_slots = image_slots(job)
                first_id = str(job_slots[0].get("upload_id") or "") if job_slots else ""
                if not first_id:
                    upload_hit = UPLOAD_IMAGE_RE.search(str(job.get("image_path") or ""))
                    first_id = upload_hit.group("id") if upload_hit else ""
                if first_id:
                    image_url = f"/api/uploads/{first_id}"
                for rec in job.get("records") or []:
                    template_id = str(rec.get("id"))
                    status = str(rec.get("status") or "")
                    raw_name = rec.get("template")
                    # 老任务/模板下架后 job.templates 里没有名字，退回记录自带的
                    # template 字段；它可能是完整云端 URL，只取文件名展示。
                    raw_name = (raw_name.rsplit("/", 1)[-1] or "") if isinstance(raw_name, str) else ""
                    template_name = names.get(template_id, "") or raw_name
                    rows.append({
                        "job_id": str(job.get("id")),
                        "template_id": template_id,
                        "image_url": image_url,
                        "template_name": template_name,
                        # 管理员看全部用户时，明细行要能分辨是谁、什么时候、属于哪批。
                        "owner": str(job_owner or ""),
                        "created_at": job.get("created_at") or 0,
                        "batch_no": batch_no,
                        "job_status": str(job.get("status") or ""),
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
