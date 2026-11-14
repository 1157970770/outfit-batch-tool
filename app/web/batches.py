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

import copy
import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
import zipfile
from pathlib import Path

from app.core import (
    DURATION_MODE_ADAPTIVE, DURATION_MODE_FIXED, DURATION_MODES, resolve_output_duration,
)
from app.web.batch_archive import BatchArchive, BatchArchiveError
from app.web.jobs import (
    MAX_IMAGE_SLOTS, UPLOAD_PATH_RE, _FAILED_RECORD_STATES, _redact,
    clean_style_no, clean_tags, duration_ok, image_slots, needs_recheck, record_uid,
)
from app.web.paths import relocate
from app.web.style_usage import StyleUsageLedger
from app.web.templates import TemplateError

BATCH_STATES = ("draft", "locked", "closed")
ITEM_STATES = ("pending", "producing", "done", "failed")
logger = logging.getLogger(__name__)
# v3：单次生产的快捷档位 5 / 10 / 20 / 50 / 100；接口同时接受 PRODUCE_MIN–上限的
# **任意整数**（7、13、33 这类非 5 倍数同样放行），前端"自定条数"就靠这条。
# 唯一例外：清单剩余不足 PRODUCE_MIN 时，允许一次提交「全部剩余」（否则零头永远跑不掉）。
PRODUCE_CHOICES = (5, 10, 20, 50, 100)
PRODUCE_MIN = 5
DEFAULT_PRODUCE_COUNT = 50
RETRY_MIN = 1
RETRY_MAX = 100
UNGROUPED_KEY = "__ungrouped__"
# 按账户分组的虚拟 key 前缀（管理员看全部用户时，一个账户的任务要收进同一张卡）。
OWNER_KEY = "__owner__"
# 按日期分组的虚拟 key 前缀（试跑没有 batch_ref，光按批次/账户拆不出可用的分类）。
DAY_KEY = "__day__"
# job.image_path 里的上传图：web-data/uploads/<xx>/<32位id>.<ext>（Windows 反斜杠同样匹配）。
# 命中即说明这张图能通过 /api/uploads/<id>（带 owner 校验）取回来。
# 命名保持对外不变，正则统一收在 jobs.py（读老 job 时两边用同一套）。
UPLOAD_IMAGE_RE = UPLOAD_PATH_RE

# 任务分类：大批量页的「这条业务线是什么」，建批次时冻结，随 job 落盘，
# 管理端按它汇总消耗（app/web/jobs.py: usage_for 的 categories）。
# 新增业务线只改这一处；前端下拉与校验都读它（GET /api/bootstrap → service.task_categories）。
TASK_CATEGORIES = ("服装换装", "项链手串")
DEFAULT_TASK_CATEGORY = TASK_CATEGORIES[0]

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


def _lane_of(job: dict) -> str:
    """job 的通道。老数据没有 lane 字段，按大批量处理（与 jobs.lane_of 同口径）。"""
    return "small" if str(job.get("lane") or "") == "small" else "big"


def _history_lane(job: dict) -> str:
    """历史页 / 整批下载的通道口径：按来源，不按调度通道。

    批次生产的零头、失败重试常常只有 1–3 条，jobs.create() 会把它们判成 lane=small，
    但它们属于某个真实批次：历史里必须跟批次一起出现在「全部 / 大批量」，整批下载也要
    收得到。只有没有 batch_ref 的才是真正的小批量试跑。
    """
    if str(job.get("batch_ref") or ""):
        return "big"
    return _lane_of(job)


def _waiting_templates(job: dict) -> list[dict]:
    """排队中、还没开跑的任务：records 还是空的，但模板清单提交时就定下来了。

    历史页只看 records，这类任务原来在明细里一条都看不见（卡片上只有 job_count）。
    这里把提交时的模板清单还原成「等待中」条目行，让排队任务在历史里有名字、有状态。
    只认 queued：running 之后 records 一定已建，空 records 的 running 是异常态，
    交给明细按原样显示，不臆造条目。
    """
    if job.get("records") or str(job.get("status") or "") != "queued":
        return []
    out: list[dict] = []
    for tpl in job.get("templates") or []:
        if isinstance(tpl, dict) and str(tpl.get("id") or ""):
            out.append({"id": str(tpl.get("id")), "name": str(tpl.get("name") or "")})
    if out:
        return out
    # 老任务/被裁过的 job 可能只剩 template_ids（没有名字），同样要给出行。
    return [{"id": str(tid), "name": ""} for tid in (job.get("template_ids") or []) if str(tid)]


def _day_key(ts) -> str:
    """按服务器本地时区把时间戳归到 YYYY-MM-DD；缺时间戳归 unknown，绝不丢记录。"""
    try:
        value = float(ts or 0)
    except (TypeError, ValueError):
        value = 0.0
    return time.strftime("%Y-%m-%d", time.localtime(value)) if value > 0 else "unknown"


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
        # 批次永久档案：款号 / 提示词 / 全部参考图的本机独立副本，删批次也保留。
        self.archive = BatchArchive(settings.data_dir)
        self._load()

    # ------------------------------------------------------------------ 存储
    def _path(self, batch_id: str) -> Path:
        return self.root / f"{batch_id}.json"

    def _save(self, batch: dict) -> None:
        path = self._path(batch["id"])
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(batch, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    # ------------------------------------------------------------------ 永久档案
    @staticmethod
    def _user_name(user) -> str:
        if isinstance(user, dict):
            return str(user.get("username") or "")
        return str(user or "")

    def _archive_warn(self, batch: dict, event: str, exc: Exception) -> None:
        message = f"批次 {batch.get('id')} 档案写入失败（{event}）：{exc}"
        logger.warning(message)
        self.warnings.append(message)
        if len(self.warnings) > 50:
            self.warnings[:] = self.warnings[-50:]

    def _archive_quiet(self, batch: dict, event: str, user="", *,
                       copy_images: bool = True) -> bool:
        """同步一次批次档案；失败只告警，绝不影响建批次 / 生产主流程。"""
        try:
            self.archive.ensure(batch, event=event, user=self._user_name(user),
                                copy_images=copy_images)
            return True
        except Exception as exc:  # 磁盘满、权限、单文件被占用都不该拖垮主流程
            self._archive_warn(batch, event, exc)
            return False

    def _archive_run_quiet(self, batch: dict, user, job_id: str,
                           images: list, template_ids: list) -> None:
        try:
            self.archive.write_run(
                batch, job_id=job_id, prompt=str(batch.get("prompt") or ""),
                images=images, template_ids=template_ids,
            )
        except Exception as exc:
            self._archive_warn(batch, "run_snapshot", exc)

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
            # 任务分类：老批次没有这个字段，留空（=未分类），不臆造历史归属。
            if batch.get("category") not in TASK_CATEGORIES:
                batch["category"] = ""
            # 草稿生产清单：老批次没有这个字段，按空清单处理；锁定/关闭后不再保留。
            batch["draft_selection"] = self._clean_selection(batch.get("draft_selection"))
            if batch.get("state") != "draft":
                batch["draft_selection"] = []
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
    def _usage_style(self, batch: dict) -> str:
        """款号-模板账本的键：优先 6 栏位里的「上衣」，老批次回退 style_no。

        与 `_start_run` 记账时同一口径 —— 记账和查询必须用同一把键，否则「已用」永远查不到。
        """
        tags = batch.get("tags") if isinstance(batch.get("tags"), dict) else {}
        return str(tags.get("top") or "").strip() or clean_style_no(batch.get("style_no"))

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

    @staticmethod
    def _clean_selection(raw) -> list[dict]:
        """规范化草稿生产清单：去重保序，只留 {template_id,name}。

        兼容老数据里可能出现的字符串或 {"id": ...} 形状；脏数据直接跳过，
        不让一条坏记录阻断整个批次加载。
        """
        if not isinstance(raw, list):
            return []
        seen: set[str] = set()
        cleaned: list[dict] = []
        for entry in raw:
            if isinstance(entry, dict):
                template_id = str(entry.get("template_id") or entry.get("id") or "").strip()
                name = str(entry.get("name") or "").strip()
            elif isinstance(entry, str):
                template_id = entry.strip()
                name = ""
            else:
                continue
            if not template_id or template_id in seen:
                continue
            seen.add(template_id)
            cleaned.append({"template_id": template_id, "name": name})
        return cleaned

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
    def _heal_items(self, batch: dict) -> bool:
        """把「已判失败、但对应 job 记录后来变 success」的条目改回 done。

        典型场景：等待超时被收成 failed，之后点「复查超时任务」按原 taskId 续查到
        云端已出片，job 记录翻成 success——批次页必须跟着变成已完成，否则用户会
        对同一条模板再点一次「重试失败」，那才是真正的二次计费。

        只做 failed → done 的单向升级，绝不把 done 降回去（续查只会让结果变好）。
        """
        changed = False
        for item in batch.get("items") or []:
            if item.get("state") != "failed":
                continue
            job_id = str(item.get("job_id") or "")
            if not job_id or self.jobs is None:
                continue
            job = self.jobs.jobs.get(job_id)
            if not isinstance(job, dict):
                continue
            for rec in job.get("records") or []:
                if (str(rec.get("id")) == str(item.get("template_id"))
                        and str(rec.get("status")) == "success"):
                    item["state"] = "done"
                    item["error"] = ""
                    item.pop("download_failed", None)
                    changed = True
                    break
        if changed:
            self._refresh(batch)
        return changed

    def _reconcile(self, batch: dict) -> bool:
        """把 active_job_id 对应 job 的结果收敛回条目状态；有改动返回 True。

        必须在持锁状态下调用（内部只读 self.jobs.jobs，不再取 jobs.changed，
        避免与 dispatcher 互相等待；RLock 允许重入，server 侧统一先持锁）。
        """
        healed = self._heal_items(batch)
        job_id = str(batch.get("active_job_id") or "")
        if not job_id:
            # 上一次生产建了 job 但批次没来得及标 producing（写盘失败 / 进程被杀）：
            # 先认领，别让用户再点一次生产导致同批模板被重复提交。
            job_id = self._adopt_pending_run(batch)
            if not job_id:
                if healed:
                    batch["updated_at"] = time.time()
                    self._save(batch)
                return healed
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
                item.pop("download_failed", None)
                success += 1
            elif status in _RECORD_UNTOUCHED:
                item["state"] = "pending"
                item["job_id"] = ""
                item["error"] = ""
                item.pop("download_failed", None)
            else:
                item["state"] = "failed"
                item["error"] = _redact(str(rec.get("error") or ""))[:300] or f"任务状态 {status or '未知'}"
                # 云端已出片、只是本机下载失败：标出来，重试时必须跳过（重试=二次提交=二次计费），
                # 只能走「复查补下载」。
                if rec.get("download_failed"):
                    item["download_failed"] = True
                else:
                    item.pop("download_failed", None)
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
                 output_seconds: float | None = None,
                 duration_mode: str = DURATION_MODE_FIXED) -> dict:
        """按（模板时长 + 输出秒数）× 单价预估；单价缺失时 amount 为 null。

        output_seconds 缺省 7 秒，与批次冻结的 duration 对齐（duration=-1 也按 7 估算）。
        duration_mode=adaptive 时逐条解析输出（≥7→7、4–7→跟随模板、<4→4），
        解析用的是引擎同一个 resolve_output_duration —— 预估不会和实际下发漂移。
        """
        out_seconds = float(output_seconds if output_seconds and output_seconds > 0 else OUTPUT_SECONDS)
        # 别用 _as_int：它对 "5.0" 会抛 ValueError 回落 7，而 output_seconds_of() 返回的正是 float。
        try:
            fixed = int(round(out_seconds))
        except (TypeError, ValueError):
            fixed = DEFAULT_DURATION
        if not duration_ok(fixed):
            fixed = DEFAULT_DURATION
        seconds = [self._item_seconds(item, fixed, duration_mode) for item in live_items]
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
        note = (f"按（模板时长 + 输出 {out_seconds:g} 秒）× 单价预估，实际以账单为准"
                if duration_mode != DURATION_MODE_ADAPTIVE else
                "按（模板时长 + 自适应输出：≥7s 出 7s、4–7s 跟随模板、<4s 出 4s）× 单价预估，"
                "实际以账单为准")
        return {
            "count": count,
            "seconds": total,
            "unit_price": unit,
            "price_source": source,
            "amount": round(total * unit, 2) if unit else None,
            "resolution": resolution,
            "options": options,
            "currency": "CNY",
            "note": note,
        }

    @staticmethod
    def _item_seconds(item: dict, fixed: int, duration_mode: str) -> float:
        """单条计费秒 = 模板时长 + 这条实际会出的秒数。"""
        template = _as_float(item.get("duration"))
        _request, expected = resolve_output_duration(duration_mode, template, fixed)
        return (template or DEFAULT_TEMPLATE_SECONDS) + (expected if expected else float(fixed))

    @staticmethod
    def output_seconds_of(batch: dict) -> float:
        """批次冻结的输出秒数：duration>0 用它，-1（跟随素材）按 7 秒估算。"""
        duration = _as_int(batch.get("duration"))
        return float(duration) if duration and duration > 0 else OUTPUT_SECONDS

    @staticmethod
    def duration_mode_of(batch: dict) -> str:
        """批次冻结的输出时长策略；老批次没有这个字段 → fixed（历史行为不变）。"""
        mode = str(batch.get("duration_mode") or "").strip().lower()
        return mode if mode in DURATION_MODES else DURATION_MODE_FIXED

    # ------------------------------------------------------------------ 公开字段
    # ------------------------------------------------------------------ 多图槽位
    def _image_slots(self, batch: dict) -> list[dict]:
        """统一读图：images[] 为真源；老批次回退成单元素列表。顺序 = 槽位 = 模型「图N」。"""
        return image_slots(batch, self.settings.data_dir)

    def _sync_image_aliases(self, batch: dict) -> None:
        """images[0] 回写单值别名，让老代码/老前端/历史对比层继续只认主图。

        别名同样按当前数据根重锚，避免把失效的旧绝对路径又写回批次 JSON。
        """
        slots = image_slots(batch, self.settings.data_dir)
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

    def _timeout_jobs(self, batch: dict) -> list[str]:
        """这个批次名下「还有 timeout 记录」的 job id（runs 为主，items 兜底）。

        调用方需持 self.mutex（只读内存，不再取 jobs.changed，避免与调度线程互相等待）。
        """
        seen: set[str] = set()
        out: list[str] = []
        for run in batch.get("runs") or []:
            job_id = str(run.get("job_id") or "")
            if job_id and job_id not in seen:
                seen.add(job_id)
                out.append(job_id)
        for item in batch.get("items") or []:
            job_id = str(item.get("job_id") or "")
            if job_id and job_id not in seen:
                seen.add(job_id)
                out.append(job_id)
        keep: list[str] = []
        for job_id in out:
            job = self.jobs.jobs.get(job_id) if self.jobs is not None else None
            if not isinstance(job, dict):
                continue
            if any(needs_recheck(rec) for rec in job.get("records") or []):
                keep.append(job_id)
        return keep

    def recheck_timeouts(self, batch_id: str, user: dict) -> dict:
        """对这个批次里所有含超时记录的 job 逐个发起「复查」（只查云端，不重新提交）。

        为什么放在批次上：一次大批量跑完，超时条目通常散在同一个 job 的十几条记录里，
        用户真正想点的是「把这批超时的都查一遍」，而不是逐个 job 点。
        """
        with self.mutex:
            batch = self._authorized(batch_id, user)
            jobs = self._timeout_jobs(batch)
        if not jobs:
            raise BatchError("这个批次没有等待超时的条目。", 409)
        started: list[str] = []
        skipped: list[dict] = []
        for job_id in jobs:
            try:
                # 注意：这里**不持 self.mutex** 调用 jobs 侧（jobs.lock 与 batches.mutex
                # 是两把锁，交叉持有会死锁）。
                self.jobs.recheck_timeouts(job_id, auto=True)
                started.append(job_id)
            except ValueError as exc:
                skipped.append({"job_id": job_id, "reason": str(exc)})
        return {"jobs": started, "skipped": skipped, "total_jobs": len(jobs)}

    def public(self, batch: dict, include_selection: bool = False) -> dict:
        with self.mutex:
            data = {key: batch.get(key) for key in (
                "id", "batch_no", "title", "owner", "state", "image_upload_id", "image_name",
                "category",
                "prompt_id", "prompt", "resolution", "duration", "duration_mode",
                "tag_filters", "tag_excludes",
                "style_no", "tags",
                "total_items", "done", "failed", "pending", "producing", "active_job_id",
                "created_at", "updated_at", "locked_at", "closed_at")}
            selection = batch.get("draft_selection") or []
            # 列表页只要数字；完整清单只在详情里下发，避免列表接口背最多 500 条 ID。
            data["selection_count"] = len(selection)
            if include_selection:
                data["draft_selection"] = [
                    {"id": str(entry.get("template_id") or ""),
                     "name": str(entry.get("name") or "")}
                    for entry in selection if isinstance(entry, dict) and entry.get("template_id")
                ]
            # image_url 语义固定为「主图（定妆图）」，老前端/历史对比层靠它，不能改。
            data["image_url"] = (f"/api/uploads/{data['image_upload_id']}"
                                 if data.get("image_upload_id") else "")
            slots = self._image_slots(batch)
            data["image_urls"] = [f"/api/uploads/{s['upload_id']}" for s in slots if s.get("upload_id")]
            data["image_names"] = [s["name"] for s in slots]
            # 定妆图更换记录：只留最近 20 条，历史页/详情页据此展示「谁在什么时候换过」。
            data["image_changes"] = [dict(change) for change in (batch.get("image_changes") or [])
                                     if isinstance(change, dict)][-20:]
            data["runs"] = [dict(run) for run in (batch.get("runs") or [])]
            # 还有超时/下载失败记录的 job：前端据此显示「复查补下载」按钮，
            # 复查成功时批次条目会被 _heal_items 自动改回已完成。
            data["timeout_jobs"] = self._timeout_jobs(batch)
            # 云端已出片、只是下载失败的条目数：重试会跳过它们，前端要把话说清楚。
            data["download_failed_items"] = sum(
                1 for item in (batch.get("items") or []) if item.get("download_failed"))
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
            rec = {}
            if done and job_id:
                rec = self._record_index(cache, job_id).get(template_id) or {}
                warn = str((rec.get("verify") or {}).get("warn") or "")
            row["record_id"] = record_uid(job_id, rec) if rec else ""
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
                             self.output_seconds_of(batch), self.duration_mode_of(batch))

    # ------------------------------------------------------------------ #1 创建
    def create(self, user: dict, data: dict) -> dict:
        batch_no = str(data.get("batch_no") or "").strip()
        title = str(data.get("title") or "").strip()
        prompt_id = str(data.get("prompt_id") or "").strip()
        prompt = str(data.get("prompt") or "").strip()
        resolution = str(data.get("resolution") or "").strip().upper()
        # 任务分类：老调用（含回归测试）不带这个字段时按默认业务线落，避免历史接口直接 400；
        # 传了就必须是白名单里的值，写错要立刻报错，不能静默归到「服装换装」。
        category = str(data.get("category") or "").strip() or DEFAULT_TASK_CATEGORY
        if category not in TASK_CATEGORIES:
            raise BatchError("任务分类只能是：" + " / ".join(TASK_CATEGORIES) + "。")
        # 网页标配「输出固定 7 秒」；-1（跟随素材）与 2–30 的其他秒数仍接受，向后兼容老批次。
        duration = _as_int(data.get("duration"), DEFAULT_DURATION)
        # 输出时长策略（「自适应」选项）：写错立刻报错，不能静默改成固定 7 秒。
        # adaptive 下 duration 没有意义（逐条按模板解析），统一落 7 让老读法不炸。
        duration_mode = str(data.get("duration_mode") or "").strip().lower() or DURATION_MODE_FIXED
        if duration_mode not in DURATION_MODES:
            raise BatchError("输出时长只能是固定秒数或自适应。")
        if duration_mode == DURATION_MODE_ADAPTIVE:
            duration = DEFAULT_DURATION
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
                # 任务分类：批次级冻结（建好后不可改），生产时原样写进 job 供用量统计。
                "category": category,
                # images[] 是唯一真源；三个单值字段是主图别名，供老前端/老代码读取。
                "images": slots,
                "image_upload_id": (slots[0]["upload_id"] if slots else ""),
                "image_path": (slots[0]["path"] if slots else ""),
                "image_name": (slots[0]["name"] if slots else ""),
                "prompt_id": prompt_id, "prompt": prompt,
                "resolution": resolution, "duration": duration, "duration_mode": duration_mode,
                "tag_filters": {}, "tag_excludes": {},
                "items": [], "runs": [],
                # 草稿期的「生产清单」：筛选时只改候选展示，不改这里；锁定后清空。
                "draft_selection": [],
                "active_job_id": "",
                "total_items": 0, "done": 0, "failed": 0, "pending": 0, "producing": 0,
                "created_at": now, "updated_at": now, "locked_at": None, "closed_at": None,
            }
            self.batches[batch_id] = batch
            self.by_no[_norm_no(batch_no)] = batch_id
            self._save(batch)
            self._archive_quiet(batch, "batch_created", user)
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
            self._archive_quiet(batch, "images_set", user)
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
            self._archive_quiet(batch, "image_added", user)
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
            self._archive_quiet(batch, "image_removed", user)
            return {"batch": self.public(batch), "slot_count": len(slots)}

    def replace_primary_image(self, batch_id: str, user: dict, data: dict) -> dict:
        """锁定后更换定妆图（槽位 1）：只影响之后的生产，并留下可追溯的更换记录。

        为什么允许：批次跑了几轮才发现定妆图选错 / 要换角度时，整批重开成本太高；
        每轮生产在 `_start_run` 里已经把当时的 images 快照进 job，换图不会改到已生成的视频。
        为什么留记录：同一批次前后用了不同定妆图，历史里必须能看出是哪一轮换的、谁换的。
        """
        upload_id = str(data.get("upload_id") or "").strip()
        if not re.fullmatch(r"[a-f0-9]{32}", upload_id):
            raise BatchError("新定妆图已失效，请重新上传。", 404)
        with self.mutex:
            batch = self._authorized(batch_id, user)
            self._reconcile(batch)
            if batch.get("state") == "closed":
                raise BatchError("批次已关闭，不能再换定妆图。", 409)
            slots = self._image_slots(batch)
            if not slots:
                raise BatchError("这个批次还没有定妆图，请先追加图片。", 409)
            old = slots[0]
            if str(old.get("upload_id") or "") == upload_id:
                return {"batch": self.public(batch), "changed": False}
            meta = self.resolve_upload(upload_id, str(batch.get("owner") or user["username"]))
            if not meta:
                raise BatchError("新定妆图已失效，请重新上传。", 404)
            slots[0] = {"upload_id": upload_id,
                        "name": str(meta.get("name") or ""),
                        "path": str(meta.get("path") or "")}
            batch["images"] = slots
            self._sync_image_aliases(batch)
            change = {
                "at": time.time(),
                "by": str(user.get("username") or ""),
                "from_upload_id": str(old.get("upload_id") or ""),
                "from_name": str(old.get("name") or ""),
                "to_upload_id": upload_id,
                "to_name": str(meta.get("name") or ""),
                "runs_before": len(batch.get("runs") or []),
            }
            batch.setdefault("image_changes", []).append(change)
            batch["updated_at"] = time.time()
            self._save(batch)
            self._archive_quiet(batch, "primary_image_replaced", user)
            return {"batch": self.public(batch), "changed": True, "change": change}

    # ------------------------------------------------------------------ #3 候选清单
    def candidates(self, batch_id: str, user: dict, data: dict) -> dict:
        tag_filters = data.get("tag_filters") or {}
        if not isinstance(tag_filters, dict):
            raise BatchError("标签筛选条件格式无效。")
        # 排除标签：黑名单，模板命中任一排除标签就不进候选（与包含条件相互独立）。
        tag_excludes = data.get("tag_excludes") or {}
        if not isinstance(tag_excludes, dict):
            raise BatchError("排除标签条件格式无效。")
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
        # 「隐藏已用」：本款号在之前批次里生产过的模板直接不进候选（款号-模板账本，
        # 与候选行的「已用」徽标、锁定时的防重确认同源）。放在评分/排序之前筛，
        # used_skipped 报的是「标签命中里有多少条被这个开关筛掉了」。
        hide_used = data.get("hide_used") is True
        with self.mutex:
            batch = self._authorized(batch_id, user)
            resolution = str(batch.get("resolution") or "720P")
            out_seconds = self.output_seconds_of(batch)
        catalog = self._catalog()
        try:
            matched = catalog.filter_items(tag_filters, tag_excludes)
        except TemplateError as exc:
            raise BatchError(str(exc)) from None
        # 排除标签筛掉多少条：只用于说明「候选为什么变少」，量级是内存里几百条，重复算一次不心疼。
        exclude_skipped = 0
        if any(tag_excludes.values()):
            exclude_skipped = max(0, len(catalog.filter_items(tag_filters)) - len(matched))
        used_skipped = 0
        if hide_used:
            used_ids = set(self.usage.used(self._usage_style(batch)))
            if used_ids:
                before = len(matched)
                matched = [item for item in matched if str(item.get("id")) not in used_ids]
                used_skipped = before - len(matched)
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
            "hide_used": hide_used,
            "used_skipped": used_skipped,
            "exclude_skipped": exclude_skipped,
            "cost_estimate": self._money_view(user, self.estimate(
                matched[:limit], resolution, out_seconds, self.duration_mode_of(batch))),
        }

    # ------------------------------------------------------------------ #3b 草稿生产清单
    def selection(self, batch_id: str, user: dict, data: dict) -> dict:
        """草稿期的生产清单增删（不产生任何任务、不扣费）。

        与 candidates 的分工：candidates 只回答「筛出来哪些候选」，这里只维护
        「用户决定要生产哪些」。筛选条件变化不会动清单，锁定才把清单固化成 items。
        """
        op = str(data.get("op") or "").strip().lower()
        if op not in ("add", "remove", "clear"):
            raise BatchError("op 只能是 add、remove 或 clear。")
        filters = data.get("tag_filters")
        excludes = data.get("tag_excludes")
        with self.mutex:
            batch = self._authorized(batch_id, user)
            if batch.get("state") != "draft":
                raise BatchError("清单已锁定，不能再改。", 409)
            current = self._clean_selection(batch.get("draft_selection"))
            existing = {entry["template_id"] for entry in current}
            if op == "clear":
                current = []
            elif op == "add":
                raw_ids = data.get("template_ids")
                if not isinstance(raw_ids, (list, tuple)) or not raw_ids:
                    raise BatchError("请选择要加入清单的模板。")
                incoming: list[str] = []
                seen: set[str] = set()
                for value in raw_ids:
                    template_id = str(value or "").strip()
                    if not template_id or template_id in seen or template_id in existing:
                        continue
                    seen.add(template_id)
                    incoming.append(template_id)
                if not incoming:
                    return {"batch": self.public(batch, include_selection=True)}
                if len(current) + len(incoming) > self.settings.max_batch_items:
                    raise BatchError(f"清单最多 {self.settings.max_batch_items} 条。", 409)
                catalog = self._catalog()
                try:
                    templates = catalog.pick_by_ids(
                        incoming, max_items=self.settings.max_batch_items)
                except TemplateError as exc:
                    raise BatchError(str(exc)) from None
                names = {str(item.get("id")): str(item.get("name") or "")
                         for item in templates}
                for template_id in incoming:
                    if template_id in names:
                        current.append({"template_id": template_id,
                                        "name": names[template_id]})
            else:  # remove：不存在的 ID 静默忽略，前端重复点 × 不会报错
                raw_ids = data.get("template_ids")
                if not isinstance(raw_ids, (list, tuple)) or not raw_ids:
                    raise BatchError("请选择要移出清单的模板。")
                drop = {str(value or "").strip() for value in raw_ids}
                current = [entry for entry in current
                           if entry["template_id"] not in drop]
            batch["draft_selection"] = current
            # 顺手同步筛选快照：下次「继续筛选」回来时标签条件和清单处于同一状态。
            if isinstance(filters, dict):
                batch["tag_filters"] = dict(filters)
            if isinstance(excludes, dict):
                batch["tag_excludes"] = dict(excludes)
            batch["updated_at"] = time.time()
            self._save(batch)
            return {"batch": self.public(batch, include_selection=True)}

    # ------------------------------------------------------------------ #4 锁定清单
    def lock(self, batch_id: str, user: dict, data: dict) -> dict:
        template_ids = data.get("template_ids")
        # 兼容两条入口：老调用显式传 template_ids；新前端不传，锁草稿里持久化的清单。
        if template_ids is not None:
            if not isinstance(template_ids, (list, tuple)) or not template_ids:
                raise BatchError("锁定清单不能为空。")
        if data.get("confirmed") is not True:
            raise BatchError("请二次确认后再锁定清单。")
        catalog = self._catalog()
        with self.mutex:
            batch = self._authorized(batch_id, user)
            if batch.get("state") != "draft":
                raise BatchError("清单已锁定，不能再改。", 409)
            if template_ids is None:
                template_ids = [entry["template_id"] for entry
                                in self._clean_selection(batch.get("draft_selection"))]
            if not template_ids:
                raise BatchError("清单还是空的，请先在候选卡片加入要生产的模板。")
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
            # 清单已经固化成 items，草稿清单清空，避免锁定后还显示一份不再生效的选择。
            batch["draft_selection"] = []
            batch["tag_filters"] = dict(data.get("tag_filters") or {}) if isinstance(
                data.get("tag_filters"), dict) else {}
            # 排除标签也进快照：锁定后回看批次时要知道这份清单是按什么黑名单筛出来的。
            batch["tag_excludes"] = dict(data.get("tag_excludes") or {}) if isinstance(
                data.get("tag_excludes"), dict) else {}
            batch["state"] = "locked"
            batch["locked_at"] = time.time()
            batch["updated_at"] = batch["locked_at"]
            self._refresh(batch)
            self._save(batch)
            # 锁定 = 清单与款号此刻定型，落一次档案（图片已在加图时复制过，这里补齐清单与版本）。
            self._archive_quiet(batch, "batch_locked", user)
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
        # 「云端已出片、只是下载失败」的条目绝不能重跑：重跑=重新提交=二次计费。
        # 它们只能走批次页的「复查补下载」。这里排掉并计数，前端提示「已跳过 N 条」。
        blocked_download = [item for item in queue if item.get("download_failed")]
        if blocked_download:
            queue = [item for item in queue if not item.get("download_failed")]
        if not queue:
            if blocked_download:
                raise BatchError(
                    f"这 {len(blocked_download)} 条是「云端已出片、只是下载失败」的条目："
                    "重试会重新提交、二次计费。请先点「复查补下载」按原 taskId 把成片补回来。", 409)
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
                item.pop("download_failed", None)
                blocked.append(str(item.get("template_id")))
            else:
                taken.append((item, live))
        if not taken:
            self._refresh(batch)
            batch["updated_at"] = time.time()
            self._save(batch)
            self._archive_quiet(batch, "run_blocked", user)
            raise BatchError("待生产条目的模板都已不可用，请检查模板清单。", 503)
        resolution = str(batch.get("resolution") or "720P")
        # 批次冻结的输出秒数原样下发（7 / -1 / 2–30）；老批次存了非法值也兜回默认，不让 produce 卡死。
        duration = _as_int(batch.get("duration"), DEFAULT_DURATION)
        if not duration_ok(duration):
            duration = DEFAULT_DURATION
        # 「自适应」：整批条目的 duration 由引擎按模板逐条解析（≥7→7、4–7→-1、<4→4）。
        duration_mode = self.duration_mode_of(batch)
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
                category=str(batch.get("category") or ""),
                # 策略随 job 落盘：之后的重试/复查/重启接回都按它逐条解析时长。
                duration_mode=duration_mode,
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
            top = self._usage_style(batch)
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
            # 这一轮用的定妆图快照：之后换图时，历史里还能看出「这一轮是哪张图跑的」。
            "image_upload_id": str(slots[0].get("upload_id") or ""),
            "image_name": str(slots[0].get("name") or ""),
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
        # 档案快照：这一轮用的提示词与图片副本。留档失败只告警，不影响已经建好的任务。
        if self._archive_quiet(batch, "run_started", user):
            self._archive_run_quiet(
                batch, user, job_id, slots,
                [str(item.get("template_id")) for item, _live_item in taken])
        return {
            "job": job,
            "batch": self.public(batch),
            "skipped_download_failed": len(blocked_download),
            "taken": len(taken),
            "blocked": blocked,
            "cost_estimate": self._money_view(user, self.estimate(
                [live for _, live in taken], resolution, duration, duration_mode)),
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
            self._archive_quiet(batch, "batch_closed", user)
            return self.public(batch)

    # ------------------------------------------------------------------ #14 修改提示词（锁定后仍可改）
    def update_prompt(self, batch_id: str, user: dict, data: dict) -> dict:
        """只改批次的提示词正文，其余（清单/定妆图/画质/时长）仍冻结。

        为什么锁定后允许改：`_start_run` 是在**每次生产时**才读 `batch["prompt"]`
        建任务，所以改提示词不影响已生成的视频，只影响之后的分批生产 —— 这正是
        「跑了一批发现提示词要调，不想整批重开」的用法。
        运行中也允许改：`_start_run` 在提交那一刻（同样持 self.mutex）就把 prompt
        快照进了 job，之后改批次上的 prompt 只对**下一轮**生效，不会让同一轮里
        出现两段提示词；前端据此提示「本轮已提交，下一轮生效」。
        只有批次已关闭才拒绝（整个批次不再变化）。
        """
        prompt = str(data.get("prompt") or "").strip()
        if not prompt:
            raise BatchError("提示词不能为空。")
        if len(prompt) > 4000:
            raise BatchError("提示词最多 4000 个字符。")
        with self.mutex:
            batch = self._authorized(batch_id, user)
            self._reconcile(batch)
            if batch.get("state") == "closed":
                raise BatchError("批次已关闭，提示词不可再修改。", 409)
            batch["prompt"] = prompt
            batch["updated_at"] = time.time()
            self._save(batch)
            self._archive_quiet(batch, "prompt_updated", user)
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
            # 先确保永久档案完整落盘，再删批次 JSON：档案写失败就拒绝删除（批次原样保留），
            # 避免「批次删了、图也被 uploads 清理了」的不可恢复丢失。档案本身永远不随删除清理。
            try:
                self.archive.ensure(batch, event="batch_deleted",
                                    user=self._user_name(user))
                self.archive.mark_deleted(batch, user=self._user_name(user))
            except Exception as exc:
                raise BatchError(f"批次档案写入失败，已取消删除：{exc}", 500) from None
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
            data = self.public(batch, include_selection=True)
            # 详情页要能直接显示「再生产这批大概多少钱」，不必先拉候选。
            data["cost_estimate"] = self._money_view(user, self.estimate_for_pending(batch))
            return data, self.public_items(batch, item_state)

    # ------------------------------------------------------------------ #10 整批打包
    def _batch_jobs(self, batch: dict) -> list[dict]:
        """按时间顺序取这个批次名下的全部 job：runs 为主，items / batch_ref 兜底。

        一个批次会分多轮生产（produce / retry_failed 各建一个 job），items 里只留
        最后一轮的 job_id；只扫 items 会漏掉前面几轮已经出片的视频。
        不再按 lane 过滤：批次零头/重试只有 1–3 条时会被判成 lane=small，
        但它们同样是这个批次的产物（见 _history_lane）。
        """
        jobs = self.jobs.jobs if self.jobs else {}
        ordered: list[str] = []
        for run in batch.get("runs") or []:
            jid = str(run.get("job_id") or "")
            if jid and jid not in ordered:
                ordered.append(jid)
        for item in batch.get("items") or []:
            jid = str(item.get("job_id") or "")
            if jid and jid not in ordered:
                ordered.append(jid)
        if not ordered:
            # 老批次没有 runs：按 batch_ref 兜底，按创建时间排，保证后面覆盖前面。
            linked = [job for job in jobs.values()
                      if str(job.get("batch_ref") or "") == str(batch.get("id"))]
            linked.sort(key=lambda job: job.get("created_at") or 0)
            return linked
        return [jobs[jid] for jid in ordered if jid in jobs]

    def batch_videos(self, batch_id: str, user: dict) -> dict:
        """按批次收集全部已出片视频：{"id","batch_no","files":[(zip 名, 本地路径)]}。

        资格：批次存在且属于本人（管理员不限）。小批量试跑不建批次，永远没有 batch_ref；
        批次自己的零头/重试即使被判成 lane=small，也照常收进整批包。
        口径：遍历每一轮 run 的成功记录，按场景 id 去重、保留最新一次成功的视频，
        与「单条下载」同源（job 输出目录里的 <场景id>.mp4）。
        """
        with self.mutex:
            batch = self._authorized(batch_id, user)
            self._reconcile(batch)
            found: dict[str, Path] = {}
            for job in self._batch_jobs(batch):
                jid = str(job.get("id") or "")
                out = self.jobs.output_path(jid) if self.jobs else None
                if not out and job.get("out_dir"):
                    out = relocate(job["out_dir"], self.settings.data_dir)
                if not out:
                    continue
                for rec in job.get("records") or []:
                    if str(rec.get("status") or "") != "success":
                        continue
                    tid = str(rec.get("id") or "")
                    # 场景 id 要进 zip 条目名，先挡掉路径穿越与异常字符。
                    if not tid or not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", tid):
                        continue
                    video = out / f"{tid}.mp4"
                    if video.is_file():
                        found[tid] = video
            return {"id": str(batch.get("id") or ""),
                    "batch_no": str(batch.get("batch_no") or ""),
                    "files": [(f"{tid}.mp4", path) for tid, path in sorted(found.items())]}

    def zip_path(self, batch_id: str, user: dict,
                 files: list[tuple[str, Path]] | None = None) -> Path:
        if files is None:
            files = self.batch_videos(batch_id, user)["files"]
        if not files:
            raise BatchError("暂时没有可下载的视频。", 404)
        # 每次打包一个唯一文件名：固定名会让同一批次的两个并发下载互相 truncate，
        # 客户端拿到半截 zip（调用方负责发完就删）。
        zip_path = (self.settings.data_dir / "tmp"
                    / f"batch-{uuid.uuid4().hex[:10]}.zip")
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

    # ------------------------------------------------------------------ 永久档案出口
    def archive_info(self, batch_id: str, user: dict) -> dict:
        with self.mutex:
            batch = self._authorized(batch_id, user)
            info = self.archive.info(batch)
            # 老批次升级后第一次打开时档案还没生成；此时按批次里已绑定的图报数，
            # 让前端仍能点「下载档案」——真正的复制在 archive_zip() 里完成。
            if not info.get("ready"):
                info["image_count"] = len(self._image_slots(batch))
            return info

    def archive_zip(self, batch_id: str, user: dict, probe: bool = False) -> dict:
        """生成批次永久档案 ZIP；probe=True 只回报数量，不打包。"""
        with self.mutex:
            batch = self._authorized(batch_id, user)
            self._reconcile(batch)
            if probe:
                info = self.archive.info(batch)
                if not info.get("ready"):
                    info["image_count"] = len(self._image_slots(batch))
                return info
            try:
                self.archive.ensure(batch, event="zip_export",
                                    user=self._user_name(user))
            except (BatchArchiveError, OSError) as exc:
                raise BatchError(f"档案写入失败：{exc}", 500) from None
            snapshot = copy.deepcopy(batch)
            batch_no = str(batch.get("batch_no") or "")
        dest = (self.settings.data_dir / "tmp"
                / f"batch-archive-{uuid.uuid4().hex[:10]}.zip")
        try:
            path, count = self.archive.build_zip(snapshot, dest, ensure=False)
        except (BatchArchiveError, OSError) as exc:
            raise BatchError(f"档案打包失败：{exc}", 500) from None
        return {"path": path, "count": count, "batch_no": batch_no}

    # ------------------------------------------------------------------ #11 历史分组
    def history_groups(self, user: dict, all_users: bool = False, owner: str = "",
                       group_by: str = "batch", lane: str = "") -> list[dict]:
        """按来源聚合历史任务：批次、账户、日期三种视角。

        group_by="batch"（默认）：按 batch_ref 聚合 job；分不到现存批次的（试跑 + 老任务
        + 批次已删除）进「未分组」。归属只认批次文件还在不在：job.batch_ref 不回写
        （历史数据只读），批次一旦被删除，它名下的 job 会自动落回未分组。
        group_by="owner"（仅管理员看全部用户时生效）：一个账户的全部任务收进
        `__owner__:<owner>` 一张卡；以前只把未分组按账户拆，同一个账户会被拆成
        「批次卡 + 未分组卡」两张，卡片标题又只有批次号，正是「任务和账号对不上」的来源。
        group_by="day"：忽略批次，全部按提交日期拆成 `__day__:<YYYY-MM-DD>`。
        lane="small" / "big"：只统计该来源的 job（批次零头/重试算 big，见 _history_lane），
        卡片数字、明细与整批下载三处同口径。
        owner 仅对管理员生效：只看某个账户的历史。
        虚拟 key 只存在于接口层，job / batch 的落盘结构一律不动。
        """
        filter_owner = str(owner or "") if all_users else ""
        split_owner = bool(all_users and group_by == "owner")
        by_day = group_by == "day"
        lane = str(lane or "").strip().lower()
        if lane not in ("small", "big"):
            lane = ""
        with self.mutex:
            groups: dict[str, dict] = {}
            for job in self.jobs.jobs.values() if self.jobs else ():
                if not all_users and job.get("owner") != user.get("username"):
                    continue
                if filter_owner and job.get("owner") != filter_owner:
                    continue
                if lane and _history_lane(job) != lane:
                    continue
                ref = str(job.get("batch_ref") or "")
                if by_day:
                    key = f"{DAY_KEY}:{_day_key(job.get('created_at'))}"
                elif split_owner:
                    key = f"{OWNER_KEY}:{str(job.get('owner') or 'unknown')}"
                elif ref and ref in self.batches:
                    key = ref
                else:
                    key = UNGROUPED_KEY
                slot = groups.setdefault(key, {
                    "key": key, "job_count": 0, "total": 0,
                    "success": 0, "failed": 0, "unfinished": 0,
                    # 排队中、还没开跑的条目数（records 未建，按 job.templates 还原）。
                    # 它同时算进 total / unfinished：任务提交时就定了要出这几条，
                    # 不显示的话「4 个任务、0 条记录」会让人以为任务丢了。
                    "waiting": 0,
                    "first_at": None, "last_at": None, "owners": set(), "sig": [],
                    # 按来源计数：历史页要能只显示大批量，也要能把「未分组」里的试跑挑出来。
                    "small_jobs": 0, "big_jobs": 0,
                })
                slot["job_count"] += 1
                if _history_lane(job) == "small":
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
                    # 与 jobs.work_for / 账户用量同口径：非成功终态全算失败侧，
                    # 其余（running/queued/pending/空）算进行中，保证 total 三者之和。
                    # 以前这里写死 4 种失败状态，unknown/timeout/skipped 和进行中
                    # 都会凭空消失，卡片数字与账户用量对不上。
                    # 唯一的补充是下面「等待中」的排队条目：它们提交时已定、必然要产，
                    # 也记进 total / unfinished（口径仍是 total = success + failed + unfinished）。
                    if status == "success":
                        slot["success"] += 1
                    elif status in _FAILED_RECORD_STATES:
                        slot["failed"] += 1
                    else:
                        slot["unfinished"] += 1
                # 排队中的任务没有 records：把提交时定下的模板清单算成「等待中」条目，
                # 与明细行（history_items 的同名占位行）严格一一对应，卡片数字不会变成 0。
                waiting = _waiting_templates(job)
                if waiting:
                    slot["waiting"] += len(waiting)
                    slot["total"] += len(waiting)
                    slot["unfinished"] += len(waiting)
            rows = []
            for key, slot in groups.items():
                is_day_group = key.startswith(DAY_KEY + ":")
                is_owner_group = key.startswith(OWNER_KEY + ":")
                is_ungrouped = key == UNGROUPED_KEY or is_owner_group or is_day_group
                batch = None if is_ungrouped else self.batches.get(key)
                batch_no = str((batch or {}).get("batch_no") or "")
                # 管理员看全部时，分组卡片要能看出这批是谁跑的；多人混在一起就标"多人"。
                owners = {name for name in slot.get("owners", set()) if name}
                owner_label = next(iter(owners)) if len(owners) == 1 else ("多人" if owners else "")
                rows.append({
                    "key": key,
                    "day": key[len(DAY_KEY) + 1:] if is_day_group else "",
                    # 按日期的组没有批次号，留空让明细行不显示「批次 xxx」。
                    # 按账户的组混着批次任务，批次号逐行解析（history_items），卡片不写死。
                    "batch_no": ("" if is_day_group or is_owner_group
                                 else "未分组" if is_ungrouped else batch_no),
                    "group_kind": ("day" if is_day_group
                                   else "owner" if is_owner_group else "batch"),
                    "is_ungrouped": is_ungrouped,
                    "small_jobs": slot["small_jobs"], "big_jobs": slot["big_jobs"],
                    "owner": owner_label,
                    "job_count": slot["job_count"], "total": slot["total"],
                    "success": slot["success"], "failed": slot["failed"],
                    "unfinished": slot["unfinished"], "waiting": slot["waiting"],
                    "state_sig": hashlib.md5("|".join(sorted(slot["sig"])).encode("utf-8")).hexdigest()[:12],
                    "first_at": slot["first_at"], "last_at": slot["last_at"] or 0,
                })
            # 统一按最近更新倒序：以前把「未分组」钉在最后，最新的试跑反而被
            # 压在一堆老批次下面，管理员一进历史页看到的全是旧数据。
            rows.sort(key=lambda row: -(row["last_at"] or 0))
            return rows

    def _run_prompt(self, job_id: str) -> str:
        """该轮任务提交时快照下来的提示词；job 已被删就返回空（不猜、不拿批次当前值冒充）。"""
        if not job_id or self.jobs is None:
            return ""
        job = self.jobs.jobs.get(str(job_id))
        return str((job or {}).get("prompt") or "")

    def run_views(self, batch: dict) -> list[dict]:
        """批次每一轮的生产视图：序号、条数、时间、这一轮实际用的提示词、是否改过。

        `runs` 里只有 job_id 与计数，没有提示词快照（老数据更没有）；提示词从该轮 job 的
        `prompt` 还原 —— 那是提交那一刻的快照，锁定后改提示词只影响之后的轮次。
        `prompt_changed` = 这一轮的提示词与上一轮不同，前端据此标「提示词已改」。
        """
        views: list[dict] = []
        previous = None
        for run in batch.get("runs") or []:
            if not isinstance(run, dict):
                continue
            job_id = str(run.get("job_id") or "")
            prompt = self._run_prompt(job_id)
            views.append({
                "no": len(views) + 1,
                "job_id": job_id,
                "count": _as_int(run.get("count"), 0) or 0,
                "started_at": float(run.get("started_at") or 0) or 0,
                "finished_at": float(run.get("finished_at") or 0) or 0,
                "success": _as_int(run.get("success"), 0) or 0,
                "failed": _as_int(run.get("failed"), 0) or 0,
                "prompt": prompt,
                "prompt_changed": bool(prompt) and previous is not None and prompt != previous,
                # 这一轮用的定妆图（老批次没有快照 → 空，前端回退到批次当前图）。
                "image_upload_id": str(run.get("image_upload_id") or ""),
                "image_name": str(run.get("image_name") or ""),
            })
            if prompt:
                previous = prompt
        return views

    def history_items(self, key: str, user: dict, offset: int = 0, limit: int = 200,
                      owner: str = "", all_users=None, lane: str = "") -> dict:
        """分组明细。all_users 显式传 False 时（管理员「只看我的任务」）连明细也只列自己，
        否则卡片写 9 条、点开却是全账户 20 条，数字对不上。None = 沿用旧行为，兼容离线调用。
        lane 与 history_groups 同口径，避免同一个分组 key 在小批量栏混进大批量的记录。
        """
        offset = max(0, int(offset or 0))
        limit = max(1, min(500, int(limit or 200)))
        owner = str(owner or "") if user.get("role") == "admin" else ""
        if all_users is False:
            owner = str(user.get("username") or "")
        lane = str(lane or "").strip().lower()
        if lane not in ("small", "big"):
            lane = ""
        with self.mutex:
            batch = None
            if key.startswith(DAY_KEY + ":"):
                # 按日期分组：忽略批次归属，取这一天的全部可见 job。
                target_day = key[len(DAY_KEY) + 1:]
                batch_no = ""
                jobs = [job for job in (self.jobs.jobs.values() if self.jobs else ())
                        if _day_key(job.get("created_at")) == target_day]
            elif key.startswith(OWNER_KEY + ":"):
                # 管理员按账户分出来的组：该账户的全部任务（批次任务 + 试跑），
                # 批次号不写死在卡片上，逐行按 job.batch_ref 解析。
                if user.get("role") != "admin":
                    raise BatchError("无权查看该分组。", 403)
                target_owner = key[len(OWNER_KEY) + 1:]
                batch_no = ""
                if owner and owner != target_owner:
                    jobs = []          # 筛选与虚拟组冲突时返回空，不能放宽到别的账户
                else:
                    jobs = [job for job in (self.jobs.jobs.values() if self.jobs else ())
                            if str(job.get("owner") or "") == target_owner]
            elif key.startswith(UNGROUPED_KEY + ":"):
                # 旧版按账户拆「未分组」的 key（页面未刷新时还会请求一次）：
                # 只取该账户、且不属于任何现存批次的任务。新分组不再产出这个 key。
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
            if lane:
                jobs = [job for job in jobs if _history_lane(job) == lane]
            # 「第几批 20 条」：runs 里每轮一条，提示词从该轮 job 的 prompt 还原
            # （runs 本身不存快照，老批次也能还原；job 被删的那轮只剩条数与时间）。
            # 重试/零头会新建一轮，同一条模板可能出现在多轮里，所以按模板收集全部轮次。
            runs = self.run_views(batch) if isinstance(batch, dict) else []
            run_no_by_job = {str(view["job_id"]): view["no"] for view in runs if view.get("job_id")}
            run_changed = {view["no"]: bool(view.get("prompt_changed")) for view in runs}
            run_nos_by_template: dict[str, list[int]] = {}
            for view in runs:
                source = ((self.jobs.jobs.get(str(view.get("job_id") or "")) if self.jobs else None) or {})
                ids = [str(tpl.get("id")) for tpl in (source.get("templates") or [])
                       if isinstance(tpl, dict)]
                if not ids:
                    ids = [str(rec.get("id")) for rec in (source.get("records") or [])
                           if isinstance(rec, dict)]
                for template_id in ids:
                    run_nos_by_template.setdefault(template_id, []).append(view["no"])
            rows = []
            for job in sorted(jobs, key=lambda item: item.get("created_at") or 0):
                job_owner = job.get("owner")
                if user.get("role") != "admin" and job_owner != user.get("username"):
                    continue
                if owner and job_owner != owner:
                    continue
                # 按账户的组里混着批次任务与试跑：批次号逐行解析，指向已删除批次的留空，
                # 不能让整张账户卡都写成「未分组」。
                row_batch_no = batch_no
                if key.startswith(OWNER_KEY + ":"):
                    ref = str(job.get("batch_ref") or "")
                    linked = self.batches.get(ref) if ref else None
                    row_batch_no = str((linked or {}).get("batch_no") or "")
                names = {str(tpl.get("id")): str(tpl.get("name") or "")
                         for tpl in job.get("templates") or [] if isinstance(tpl, dict)}
                # 这条记录对应的定妆图 = job 自己的素材图。批次有批次级定妆图，但试跑/未分组
                # 的历史任务没有批次，只有 job 自己这张才对得上；只有落在 uploads/<xx>/<id>.<ext>
                # 的才走带权限校验的 /api/uploads 接口，其它来源一律不给 URL。
                image_url = ""
                # 优先读多图真源的首张，老 job 由 image_slots() 从 image_path 反解兜底；
                # 历史对比层本次仍只展示主图一张。
                job_slots = image_slots(job, self.settings.data_dir)
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
                        # 每条视频的稳定唯一 ID（新记录落盘，老记录按 job+模板兜底）。
                        "record_id": record_uid(str(job.get("id")), rec),
                        # 这一条属于第几批：run_no 是它最近一次生产所在轮次，
                        # run_nos 列出它出现过的全部轮次（重试会让同一条落在多轮）。
                        "run_no": run_no_by_job.get(str(job.get("id")), 0),
                        "run_nos": sorted(set(run_nos_by_template.get(template_id) or (
                            [run_no_by_job[str(job.get("id"))]]
                            if str(job.get("id")) in run_no_by_job else []))),
                        "run_total": len(runs),
                        "run_prompt_changed": run_changed.get(
                            run_no_by_job.get(str(job.get("id")), 0), False),
                        "image_url": image_url,
                        "template_name": template_name,
                        # 管理员看全部用户时，明细行要能分辨是谁、什么时候、属于哪批。
                        "owner": str(job_owner or ""),
                        "created_at": job.get("created_at") or 0,
                        "batch_no": row_batch_no,
                        "job_status": str(job.get("status") or ""),
                        "status": status,
                        "waiting": False,
                        "seconds": rec.get("seconds"),
                        "download_url": (f"/api/jobs/{job.get('id')}/download/{template_id}"
                                         if status == "success" else ""),
                        "archive_warning": ("视频已生成，但 COS 归档失败，请联系管理员。"
                                            if rec.get("archive_error") else ""),
                        # 与批次条目同口径：done 的质量提示，状态仍是 success。
                        "verify_warn": _redact(str((rec.get("verify") or {}).get("warn") or ""))[:200],
                    })
                # 排队中的任务：records 还没建，但模板清单提交时就定了。不补这几行，
                # 历史明细里这个任务一条都看不见（卡片上的条数却已经算了它们）。
                # waiting=True 让前端把状态写成「等待中」，并且不摆下载/对比按钮。
                for tpl in _waiting_templates(job):
                    rows.append({
                        "job_id": str(job.get("id")),
                        "template_id": tpl["id"],
                        "record_id": "",
                        "run_no": run_no_by_job.get(str(job.get("id")), 0),
                        "run_nos": sorted(set(run_nos_by_template.get(tpl["id"]) or (
                            [run_no_by_job[str(job.get("id"))]]
                            if str(job.get("id")) in run_no_by_job else []))),
                        "run_total": len(runs),
                        "run_prompt_changed": False,
                        "image_url": image_url,
                        "template_name": tpl["name"] or names.get(tpl["id"], ""),
                        "owner": str(job_owner or ""),
                        "created_at": job.get("created_at") or 0,
                        "batch_no": row_batch_no,
                        "job_status": str(job.get("status") or ""),
                        "status": "queued",
                        "waiting": True,
                        "seconds": None,
                        "download_url": "",
                        "archive_warning": "",
                        "verify_warn": "",
                    })
            total = len(rows)
            # 批次组把定妆图与更换记录一起带回：历史页展开时能直接看到「这批复用了哪张图、
            # 中途换过几次」，不用再单独请求批次详情。
            image_changes: list[dict] = []
            image_urls: list[str] = []
            image_names: list[str] = []
            if isinstance(batch, dict):
                image_changes = [dict(change) for change in (batch.get("image_changes") or [])
                                 if isinstance(change, dict)][-20:]
                for slot in self._image_slots(batch):
                    if slot.get("upload_id"):
                        image_urls.append(f"/api/uploads/{slot['upload_id']}")
                        image_names.append(str(slot.get("name") or ""))
            return {"key": key, "batch_no": batch_no,
                    "items": rows[offset:offset + limit],
                    "runs": runs,
                    "image_changes": image_changes,
                    "image_urls": image_urls,
                    "image_names": image_names,
                    "total": total, "offset": offset, "limit": limit}
