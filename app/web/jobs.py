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

from app import billing
from app import config as appconfig
from app.core import TAG_SLOTS, BatchRunner, BatchSpec, status_counts, unique_batch_dir
from app.media import base64_budget_error
from app.runtime import build_storages
from app.wan_api import MAX_IMAGES, global_inflight, global_small_inflight
from app.web.paths import relocate

_SAFE = re.compile(r"[^A-Za-z0-9_-]+")
_STYLE_MAX = 40
# 1 张定妆图 + 最多 9 张细节图：与 wan_api.MAX_IMAGES 对齐（官方 reference_image 上限）。
MAX_IMAGE_SLOTS = MAX_IMAGES
UPLOAD_PATH_RE = re.compile(r"uploads[\\/][0-9a-f]{2}[\\/](?P<id>[0-9a-f]{32})\.[A-Za-z0-9]{1,8}$")


def image_slots(record: dict, data_dir: Path | None = None) -> list[dict]:
    """统一读图：images[] 是唯一真源（下标=槽位=imageUrls 顺序）；老 job / 老 batch
    只有单值 image_path/image_name，回退成 1 个元素，upload_id 从路径反解。

    传入 data_dir 时，把记录里持久化的绝对路径按当前数据根重锚定（见 app.web.paths）；
    不传则完全保持旧行为，让只关心 upload_id / 名称的调用方零改动。
    """
    def fix(raw_path) -> str:
        text = str(raw_path or "")
        if not text or data_dir is None:
            return text
        return str(relocate(text, data_dir))

    raw = record.get("images")
    if isinstance(raw, list) and raw:
        slots = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            slots.append({"upload_id": str(item.get("upload_id") or ""),
                          "name": str(item.get("name") or ""),
                          "path": fix(item.get("path"))})
        if slots:
            return slots
    path = str(record.get("image_path") or "")
    if not path:
        return []
    hit = UPLOAD_PATH_RE.search(path)
    return [{"upload_id": hit.group("id") if hit else "",
             "name": str(record.get("image_name") or ""),
             "path": fix(path)}]


def record_uid(job_id: str, record: dict) -> str:
    """一条生产视频的稳定 ID。

    新记录由 `app.core.new_record_id()` 落盘；老数据没有这个字段，用
    `job_id-template_id` 兜底（同一 job 内模板 ID 唯一，所以不会重复）。
    """
    value = str((record or {}).get("record_id") or "")
    if value:
        return value
    return f"{str(job_id or '')}-{str((record or {}).get('id') or '')}"


def normalize_images(raw, image_path: Path, image_name: str) -> list[dict]:
    """把入参归一成有序槽位数组。raw=None 时用旧的位置参数造单元素列表。"""
    if raw is None:
        raw = [{"upload_id": "", "name": image_name, "path": str(image_path)}]
    if not isinstance(raw, list):
        raise ValueError("图片参数无效，请重新上传。")
    if len(raw) > MAX_IMAGE_SLOTS:
        raise ValueError(f"一次最多 {MAX_IMAGE_SLOTS} 张图片（1 张定妆图 + 9 张细节图）。")
    slots = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("图片参数无效，请重新上传。")
        path = str(item.get("path") or "").strip()
        if not path:
            raise ValueError("图片文件路径为空，请重新上传。")
        hit = UPLOAD_PATH_RE.search(path)
        slots.append({"upload_id": str(item.get("upload_id") or (hit.group("id") if hit else "")),
                      "name": str(item.get("name") or ""), "path": path})
    return slots

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


# 任务分类的兜底名：老任务、试跑任务（没有批次）没有分类字段，统计时归到这里，
# 绝不能把它们的消耗从报表里丢掉。口径定义在 app/billing.py，这里只做别名。
UNCATEGORIZED = billing.UNCATEGORIZED


def clean_category(value) -> str:
    """任务分类是有限枚举，合法性由 BatchManager.create 把关；这里只做去噪与截断。

    老 job 没有这个字段 → 空串，读取端（usage_for / work_for）统一折算成 UNCATEGORIZED。
    """
    text = re.sub(r"[\x00-\x1f\x7f]", "", str(value or "")).strip()
    return text[:20]


# 重试时认的非成功终态：skipped 是「被取消/批量放弃」的条目，重跑一次才有意义；
# running 不算——它表示还在等云端出片（含等待超时），此时重试会和原任务抢同一份费用。
# 可重试的记录状态。unknown/running 不可重试：云端可能已建任务，重试会二次计费。
# timeout（等待超时）可重试：云端大概率已出片，但重试是用户核对后的显式选择。
_RETRYABLE_RECORD_STATES = frozenset({"failed", "invalid", "expired", "canceled", "skipped", "timeout"})


def needs_recheck(rec: dict) -> bool:
    """这条记录该走「复查补下载」，而不是「重试」。

    两种情况，共同点是**云端可能已经出片并按秒计费了**，重试 = 重新提交 = 二次计费：

    1. `status == "timeout"`：本机没在时限内等到结果，云端多半已经出片；
    2. `download_failed`：云端明确已出片（有 usage、已记账），只是本机取文件失败
       ——2026-11-10 job b1cc 就是这种情况（TLS 断流），错的只是本机这一步。

    复查只按原 taskId 查询 + 补下载，补好了记录会自己翻成 success。
    """
    rec = rec or {}
    return str(rec.get("status") or "") == "timeout" or bool(rec.get("download_failed"))


def retry_blocked_download_failed(job: dict) -> list[dict]:
    """重试会把它们重新提交、二次计费的条目（云端已出片，只是下载失败）。

    给自己重试时：这些必须排除，计数回传给前端好提示「已跳过 N 条」。
    """
    return [rec for rec in (job or {}).get("records") or []
            if rec.get("status") in _RETRYABLE_RECORD_STATES and needs_recheck(rec)
            and rec.get("download_failed")]

# 统计口径里的「失败侧」：非成功终态全部计入，保证 items = success + failed + unfinished。
# unknown（提交结果未知/停止等待）云端可能已建任务并计费，必须算失败侧而非静默消失；
# skipped 与 timeout 一样在重试白名单里，同属失败侧。
# 定义在 app/billing.py：数据镜像与外部报表要同一套口径，不能各存一份。
_FAILED_RECORD_STATES = billing.FAILED_RECORD_STATES

# P1-5：金额相关字段只下发给管理员。普通用户可反推 wan.price_per_second，
# 违反硬约束 5（金额统一走 /api/admin/usage 与 /api/admin/accounts/<owner>/work）。
_MONEY_KEYS = frozenset({"billable_seconds", "estimated_cost", "cost",
                         "total_billable_seconds", "total_cost",
                         "unit_price", "price_source", "amount"})

_SECRET_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9._-]{12,}"), "[已隐藏密钥]"),
    (re.compile(r"(q-signature|q-ak|SecretId|SecretKey)=([^&\\s]+)", re.I), r"\1=[已隐藏]"),
]


def _redact(text: str) -> str:
    result = str(text)
    for pattern, replacement in _SECRET_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


# 重启接回：最多自动重排几次。防「一启动就崩、崩了又拉起」的死循环把同一批任务反复折腾。
RESUME_MAX_ATTEMPTS = 3


def _resume_scope(job: dict) -> tuple[int, int]:
    """统计重启后能自动接回的分量：(可接回的云端任务条数, 还没提交过的条目数)。

    只有这两类才值得重排：前者接着等原 taskId（不重提、不二次计费），
    后者本来就没提交过（重排等于继续干）。「正在跑但没有 taskId」不算——
    那类只能落 unknown，让人去控制台核对后再决定。

    running 与 timeout 都算「可接回」：等待超时不代表云端失败，taskId 在 24h 内
    仍然有效；重启后接回只等结果，绝不重新提交（否则同一条模板二次计费）。
    """
    now = time.time()
    records = {}
    for rec in job.get("records") or []:
        if isinstance(rec, dict) and rec.get("id") is not None:
            records[str(rec["id"])] = rec
    reattach = 0
    for rec in records.values():
        if str(rec.get("status") or "") not in ("running", "timeout") or not rec.get("task_id"):
            continue
        try:
            expires = float(rec.get("task_expires_at") or 0)
        except (TypeError, ValueError):
            expires = 0
        if expires and expires < now:
            continue
        reattach += 1
    pending = 0
    for tpl in job.get("templates") or []:
        tid = str((tpl or {}).get("id"))
        rec = records.get(tid)
        if rec is None or str(rec.get("status") or "") in ("", "pending"):
            pending += 1
    return reattach, pending


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

    def _cos_available(self) -> bool:
        """多图预算闸用：启用 COS 时图片走短签名 URL，不受 Base64 总量限制。"""
        # getattr 兜底：单测/诊断脚本里的 settings 是 SimpleNamespace，没有 config 字段。
        cfg = getattr(self.settings, "config", None) or {}
        provider = str(cfg.get("COS_PROVIDER") or "").strip().lower()
        return appconfig.truthy(cfg.get("COS_ENABLED")) and provider != "local"

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
            if job.get("status") in ("running", "interrupted"):
                # 能接回就自动接回（重新排队，_run_job 走 resume 路径），接不回来才留「已中断」。
                # interrupted 也一并处理：上一次重启留下的已中断任务，只要云端那条还在跑
                # （taskId 未过期），这次重启照样能接回来，不必等用户手动去核对。
                reattach, pending = _resume_scope(job)
                attempts = int(job.get("resume_count") or 0)
                if job.get("status") == "running":
                    resumable = bool(reattach or pending)
                else:
                    # 已中断的只认「云端还在跑」这一条：普通条目早就有定论了，
                    # 顺手重排只会把过去的重启又演一遍。
                    resumable = bool(reattach)
                resumable = resumable and attempts < RESUME_MAX_ATTEMPTS \
                    and not job.get("cancel_requested")
                if resumable:
                    parts = []
                    if reattach:
                        parts.append(f"接回 {reattach} 条云端任务")
                    if pending:
                        parts.append(f"续跑 {pending} 条未提交条目")
                    job["status"] = "queued"
                    job["resume"] = True
                    job["resume_count"] = attempts + 1
                    job["started_at"] = None
                    job["finished_at"] = None
                    job["message"] = f"服务重启：自动{'、'.join(parts)}，稍候即继续跟进进度。"
                elif job.get("status") == "running":
                    job["status"] = "interrupted"
                    job["message"] = (
                        "服务重启时任务正在运行，且没有可自动接回的云端任务；"
                        "请核对阿里云任务后再重试。"
                    )
                    job["finished_at"] = time.time()
                for rec in job.get("records") or []:
                    # 没接回来的「正在跑」条目：落 unknown（不可一键重试，
                    # 否则云端那条可能被二次提交、二次计费）。
                    if str(rec.get("status") or "") == "running" and not resumable:
                        rec["status"] = "unknown"
                        rec["error"] = (
                            "服务重启时这条正在运行，但没有可核对的 taskId（或已过期）；"
                            "请到控制台核对后再决定是否重试。"
                        )
                dirty = True
            self.jobs[job_id] = job
            if dirty:
                self._save(job)

    def create(self, owner: str, image_path: Path, image_name: str,
               templates: list[dict], prompt_id: str, resolution: str = "480P",
               duration: int = 5, prompt_body: str | None = None, *,
               batch_ref: str = "", limit_items: int | None = None,
               tags: dict | None = None, style_no: str = "",
               category: str = "",
               admin: bool = False, images: list[dict] | None = None) -> dict:
        """建任务并入队。batch_ref 指向批次（老路径为空）；limit_items 覆盖单次条数上限。

        images：有序槽位数组（1 定妆 + 最多 9 细节），给出时是权威值，覆盖
        image_path/image_name；缺省时用旧的位置参数造单元素列表。
        tags：6 栏位标签（上衣必填）；style_no：v0.9.0 旧字段，读老批次时兜底，
        两者都缺时报错——云端归档路径必须有上衣款号。
        category：任务分类（批次生产时由批次原样带入），管理端按它汇总消耗；
        试跑没有分类，留空。
        """
        slots = normalize_images(images, image_path, image_name)
        if not slots:
            raise ValueError("请先上传服装图片。")
        budget = base64_budget_error([s["path"] for s in slots],
                                     cos_enabled=self._cos_available())
        if budget:
            raise ValueError(budget)
        image_path = Path(slots[0]["path"])
        image_name = slots[0]["name"]
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
            # 多图真源：images[0]=定妆图，images[1..9]=细节图；顺序即传给模型的「图N」。
            "images": slots, "image_upload_id": slots[0]["upload_id"],
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
            # 任务分类：把生成那一刻的分类快照进 job，批次日后被删也不影响分类统计。
            "category": clean_category(category),
            # small = 快速通道（≤small_lane_items 条）；big = 走大批量队列。
            "lane": lane,
        }
        with self.changed:
            if sum(1 for item in self.jobs.values() if item.get("status") == "queued") >= 50:
                raise ValueError("当前排队任务较多，请稍后再提交")
            self.jobs[job_id] = job
            self._save(job)
            self.changed.notify_all()
        return self.public(job, admin)

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
                # 重启接回：复用重启前的输出目录（已下载的成品就在里面），
                # 并把旧记录交给引擎，让它跳过已出片的、接回已提交的，绝不重提。
                resume_mode = bool(job.get("resume"))
                resume_records = {}
                if resume_mode:
                    for rec in job.get("records") or []:
                        if isinstance(rec, dict) and rec.get("id") is not None:
                            resume_records[str(rec["id"])] = dict(rec)
                    job["message"] = "服务重启后已自动接回，正在继续跟进云端进度。"
                    job.pop("resume", None)
                # 代码目录搬家后，旧 out_dir 可能指向已失效的绝对路径：按当前数据根
                # 重锚，避免接回任务时把新视频又写回旧文件夹（must_exist=False 允许
                # 目标目录尚未生成）。
                old_out = str(relocate(job.get("out_dir"), self.root, must_exist=False))
                if resume_mode and old_out and Path(old_out).is_dir():
                    out_dir = Path(old_out)
                else:
                    out_dir = unique_batch_dir(self.output_root, job["batch_id"])
                job["out_dir"] = str(out_dir)
                self._save(job)
            if resume_mode:
                self._append_log(job_id, f"重启接回：沿用 {len(resume_records)} 条旧记录继续")
            spec = self._build_spec(job, Path(job["out_dir"]), resume=resume_records)
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

    def _build_spec(self, job: dict, out_dir: Path, resume: dict | None = None) -> BatchSpec:
        """按 job 记录组装引擎入参。正常跑与「复查超时」共用，避免两处口径漂移。"""
        cfg = self.settings.config
        template_store, output_store = build_storages(cfg)
        # 线程数只是工作线程，不再与 API 并发/速率绑在一起；上限 50 防止误配。
        concurrency = self.settings.max_concurrency
        job_slots = image_slots(job, self.root)
        job_image_paths = [s["path"] for s in job_slots]
        return BatchSpec(
            batch_id=Path(job["out_dir"]).name,
            image_path=job_image_paths[0] if job_image_paths else "",
            image_paths=job_image_paths,
            prompt=job["prompt"], out_dir=Path(job["out_dir"]),
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
            # 只在重启接回时非空：引擎据此跳过已出片、接回已提交的条目。
            resume=resume or {},
        )

    # ---- 复查超时任务（P0） ----
    # 超时只是「本机没等到」，云端很可能已经出片并按秒计费。复查 = 按原 taskId
    # 向云端查一次，出片就补下载/补记账，绝不再提交新任务（避免二次计费）。
    # 一个 HTTP 请求做完会被 socket 超时（30s）掐断，所以放后台线程，前端轮询 job。
    def recheck_timeouts(self, job_id: str, *, auto: bool = False) -> dict:
        """启动一次后台复查；返回 job 快照。重复点击/并发的第二次会直接报错。"""
        with self.changed:
            job = self.jobs.get(job_id)
            if job is None:
                raise ValueError("找不到该任务")
            if str(job.get("status") or "") in ("queued", "running"):
                raise ValueError("任务还在跑，跑完再复查")
            if (job.get("recheck") or {}).get("running"):
                raise ValueError("上一次复查还没结束，请稍等")
            targets = [rec for rec in job.get("records") or [] if needs_recheck(rec)]
            if not targets:
                raise ValueError("没有等待超时或下载失败的条目需要复查")
            job["recheck"] = {
                "running": True,
                "auto": bool(auto),
                "total": len(targets),
                "checked": 0,
                "success": 0,
                "failed": 0,
                "still_running": 0,
                "errors": 0,
                "started_at": time.time(),
                "finished_at": None,
                "message": (f"正在向云端复查 {len(targets)} 条超时/下载失败任务"
                            "（只查询，不重新提交）…"),
            }
            self._save(job)
            self.changed.notify_all()
        thread = threading.Thread(target=self._run_recheck, args=(job_id,),
                                  name=f"recheck-{job_id}", daemon=True)
        thread.start()
        return self.public(job)

    def _run_recheck(self, job_id: str) -> None:
        """后台线程：逐条复查，边查边把结果写回 job（前端轮询就能看到进度）。"""
        job = None
        try:
            with self.lock:
                job = self.jobs.get(job_id)
                if job is None:
                    return
                records = {str(rec.get("id")): dict(rec) for rec in job.get("records") or []}
                targets = [rec for rec in records.values() if needs_recheck(rec)]
                out_dir = relocate(job.get("out_dir"), self.root, must_exist=False)
                spec = self._build_spec(job, Path(out_dir))
            runner = BatchRunner(spec, log=lambda m: self._append_log(job_id, m))
            self._append_log(job_id, f"复查超时/下载失败任务：{len(targets)} 条，按原 taskId 向云端查询")

            def on_update(rec: dict) -> None:
                with self.changed:
                    live = self.jobs.get(job_id)
                    if live is None:
                        return
                    state = live.get("recheck") or {}
                    status = str(rec.get("status") or "")
                    state["checked"] = int(state.get("checked") or 0) + 1
                    if status == "success":
                        state["success"] = int(state.get("success") or 0) + 1
                    elif status == "timeout":
                        # 仍是 timeout 有四种原因：云端还在跑（正常等）、下载失败、
                        # 没有 taskId 无法查、查询本身报错。后三种要算「出错」，用户才知道要再点一次。
                        cloud = str(rec.get("cloud_status") or "")
                        if (rec.get("download_failed") or cloud == "no_task_id"
                                or cloud.startswith("error:")):
                            state["errors"] = int(state.get("errors") or 0) + 1
                        else:
                            state["still_running"] = int(state.get("still_running") or 0) + 1
                    elif rec.get("cloud_status", "").startswith("error:"):
                        state["errors"] = int(state.get("errors") or 0) + 1
                    else:
                        state["failed"] = int(state.get("failed") or 0) + 1
                    state["message"] = (f"已复查 {state['checked']}/{state['total']} 条："
                                        f"补回 {state['success']}、云端已失败 {state['failed']}、"
                                        f"仍在跑 {state['still_running']}、出错 {state['errors']}")
                    live["recheck"] = state
                    live["records"] = [rec if str(r.get("id")) == str(rec.get("id")) else r
                                       for r in (live.get("records") or [])]
                    self._save(live)
                    self.changed.notify_all()

            runner.recheck_timeouts(targets, on_update=on_update)
        except Exception as exc:  # noqa: BLE001 - 复查失败不能把 job 打成 failed
            with self.changed:
                live = self.jobs.get(job_id)
                if live is not None:
                    state = live.get("recheck") or {}
                    state["message"] = f"复查中断：{type(exc).__name__}: {exc}"
                    state["errors"] = int(state.get("errors") or 0) + 1
                    live["recheck"] = state
                    self._save(live)
                self.changed.notify_all()
            self._append_log(job_id, f"复查超时任务失败：{exc}")
        finally:
            with self.changed:
                live = self.jobs.get(job_id)
                if live is not None:
                    self._refresh_summary(live)
                    state = live.get("recheck") or {}
                    state["running"] = False
                    state["finished_at"] = time.time()
                    live["recheck"] = state
                    self._save(live)
                    self.changed.notify_all()
                    if state.get("success"):
                        self._append_log(
                            job_id,
                            f"复查完成：{state['success']} 条云端已出片，已补下载并转为成功",
                        )
                    self._schedule_recheck_after_auto(live, state)

    def _refresh_summary(self, job: dict) -> None:
        """复查改变了记录状态后，重算 job 的汇总与终态（partial → succeeded 等）。"""
        records = job.get("records") or []
        counts = status_counts(records)
        summary = dict(counts)
        summary.update({
            "total": len(records),
            "unsuccessful": max(0, len(records) - counts["success"]),
            "billable_seconds": round(sum(float(r.get("billable_seconds") or 0)
                                          for r in records), 2),
            "estimated_cost": round(sum(float(r.get("estimated_cost") or 0)
                                        for r in records), 2),
            "out_dir": str(job.get("out_dir") or ""),
        })
        summary["cost"] = summary["estimated_cost"]
        status, message, done = self._job_outcome(summary)
        job["summary"] = summary
        job["done"] = done
        if str(job.get("status") or "") not in ("queued", "running"):
            job["status"] = status
            job["message"] = message

    def _schedule_recheck_after_auto(self, job: dict, state: dict) -> None:
        """超时后自动复查一次（P1）：job 刚跑完、还有 timeout 条目时，延迟一段时间再查。"""
        if state.get("auto"):
            return
        delay = float(self.settings.config.get("WAN_TIMEOUT_RECHECK_DELAY") or 0)
        if delay <= 0:
            return
        if (job.get("recheck") or {}).get("auto"):
            return
        if not any(needs_recheck(r) for r in job.get("records") or []):
            return

        def _later() -> None:
            if self.stop_event.wait(delay):
                return
            try:
                self.recheck_timeouts(job["id"], auto=True)
            except Exception:  # noqa: BLE001 - 自动复查只是兜底，失败不影响用户手动点
                pass

        threading.Thread(target=_later, name=f"recheck-later-{job['id']}", daemon=True).start()

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
            # P1：有等待超时的条目就排一次延迟自动复查（只查 taskId，不重新提交）。
            # 放在锁内取快照，线程只拿到 job 引用，不会与结束流程抢写。
            if status in ("partial", "failed") and int(summary.get("timeout") or 0) > 0:
                self._schedule_recheck_after_auto(job, {"auto": False})

    def retry_failed(self, job_id: str, owner: str, admin: bool = False) -> dict:
        with self.lock:
            source = self.jobs.get(job_id)
            if not source:
                raise ValueError("找不到该任务")
            if source.get("status") not in {"partial", "failed", "canceled"}:
                raise ValueError("任务尚未结束，暂时不能重试")
            failed_ids = {str(r.get("id")) for r in source.get("records", [])
                          if r.get("status") in _RETRYABLE_RECORD_STATES
                          and not needs_recheck(r)}
            selected = [dict(t) for t in source.get("templates", []) if str(t.get("id")) in failed_ids]
            if not selected:
                # 云端已出片、只是本机取文件失败的条目（download_failed）：它在重试白名单里，
                # 但重试 = 重新提交 = 二次计费（b1cc 教训）。先复查补下载，补回来自己就成功了。
                blocked = retry_blocked_download_failed(source)
                if blocked:
                    raise ValueError(
                        f"其中 {len(blocked)} 条是「云端已出片、只是下载失败」的条目："
                        "重试会重新提交同一条模板、二次计费。"
                        "请先点「复查补下载」按原 taskId 把成片补回来——补成功后条目会自己变成已完成。"
                    )
                if any(str(r.get("status")) == "running" for r in source.get("records", [])):
                    raise ValueError("还有条目在等云端出片，请稍后再重试")
                unknown = sum(1 for r in source.get("records", []) if str(r.get("status")) == "unknown")
                if unknown:
                    raise ValueError(
                        f"有 {unknown} 条提交结果未知（云端可能已创建任务并计费），"
                        "请先到控制台核对，确认没有重复后再新建任务"
                    )
                raise ValueError("该任务没有可重试的失败项")
            slots = image_slots(source, self.root)
            if not slots:
                raise ValueError("原任务没有可用的图片，请重新上传后新建任务")
            for index, slot in enumerate(slots, 1):
                if not Path(slot["path"]).is_file():
                    raise ValueError(f"原任务第 {index} 张图片已不存在，请重新上传后新建任务")
            # P1-20：用量与结果归属**原 owner**（管理员重试他人任务不记账到自己头上），
            # batch_ref 透传，批次 history 回查不断链。权限判断留在 server 层。
            # 全部图原样带过去：丢一张细节图会让「图N」整体错位，等于偷换了需求。
            return self.create(str(source.get("owner") or owner), Path(slots[0]["path"]), slots[0]["name"], selected,
                               source["prompt_id"], source["resolution"], source["duration"], source.get("prompt"),
                               batch_ref=str(source.get("batch_ref") or ""),
                               tags=dict(source.get("tags") or {}), style_no=str(source.get("style_no") or ""),
                               admin=admin, images=slots)

    def cancel(self, job_id: str) -> bool:
        with self.changed:
            job = self.jobs.get(job_id)
            if not job:
                return False
            if job.get("status") == "queued":
                job["status"] = "canceled"
                # 派发线程可能已经把这个 job 放进 active 并起了线程（毫秒级窗口）。
                # 只改 status 不够保险：一并置取消标记，让工作线程拿锁后自己退出。
                job["cancel_requested"] = True
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

    def get(self, job_id: str, admin: bool = False) -> dict | None:
        with self.lock:
            job = self.jobs.get(job_id)
            return self.public(job, admin) if job else None

    def list_for(self, username: str, admin: bool = False, owner: str = "") -> list[dict]:
        """owner 只对管理员生效：按账户筛选，不传则沿用原口径（mine / all）。"""
        filter_owner = str(owner or "") if admin else ""
        with self.lock:
            items = [j for j in self.jobs.values()
                     if (admin or j.get("owner") == username)
                     and (not filter_owner or j.get("owner") == filter_owner)]
            items.sort(key=lambda j: j.get("created_at", 0), reverse=True)
            return [self.public(j, admin) for j in items]

    def _record_cost(self, rec: dict, job: dict) -> tuple[float, str]:
        """单条记录的费用：优先用生成时落下的金额，否则按当前单价回算。

        实现已下沉到 ``app/billing.py::record_cost``——数据镜像、外部报表与网页端
        必须用同一套口径，这里只是兼容旧调用点的薄委托。
        """
        return billing.record_cost(rec, job, self.settings.unit_price)

    def videos_used(self, owner: str) -> int:
        """账户已占用的生成额度（条），供按账户的生成上限使用。

        口径：
          · success（已生成）与 running（正在生成）计入；
          · queued/running 任务里还没跑到的条目也会生成，一并计入（多算不吃亏）；
          · failed/invalid/expired/canceled/unknown/timeout/skipped 不计——没生成出视频。
        """
        used = 0
        with self.lock:
            for job in self.jobs.values():
                if str(job.get("owner") or "") != owner:
                    continue
                counted = 0
                for rec in job.get("records") or []:
                    if str(rec.get("status")) in ("success", "running"):
                        counted += 1
                if str(job.get("status")) in ("queued", "running"):
                    counted += max(0, int(job.get("total") or 0) - counted)
                used += counted
        return used

    def usage_for(self, start: float | None = None, end: float | None = None,
                  owner: str = "") -> dict:
        """按账户 + 按任务分类汇总工作量：任务数、条数、成功/失败、计费秒、预估金额。

        口径说明：
          · 条数以 job.records 为准（进行中的 job 也有记录）；
          · 金额是「计费秒 × 单价」的估算，真实账单以阿里云为准；
          · 含零使用量的账户，方便管理员看出"谁还没开始用"。
          · categories 与 accounts 同口径、同一批 job，只是分组维度不同：老任务与
            试跑任务没有分类字段，统一归到 UNCATEGORIZED，保证两边总数能对上。
        """
        # unknown = 提交结果未知（云端可能已建任务并计费），必须计入失败侧，
        # 否则账上既不算成功也不算失败，会从统计里静默消失。
        # 口径统一走 _FAILED_RECORD_STATES，与 work_for / 重试白名单对齐。
        failed_states = _FAILED_RECORD_STATES

        def blank(name: str, role: str = "user", enabled: bool = True) -> dict:
            return {"owner": name, "role": role, "enabled": enabled,
                    "jobs": 0, "items": 0, "success": 0, "failed": 0, "unfinished": 0,
                    # timeout 单列：可能云端已出片并计费，直接算「已用」会虚高，
                    # 算「失败」又会让账单少一块；管理员在用量页单独看这一列。
                    "timeout": 0,
                    "billable_seconds": 0.0, "estimated_cost": 0.0, "last_at": 0.0}

        def blank_category(name: str) -> dict:
            return {"category": name, "jobs": 0, "items": 0, "success": 0, "failed": 0,
                    "unfinished": 0, "timeout": 0, "billable_seconds": 0.0,
                    "estimated_cost": 0.0, "last_at": 0.0}

        rows: dict[str, dict] = {}
        cats: dict[str, dict] = {}
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
                cat_name = str(job.get("category") or "").strip() or UNCATEGORIZED
                cat = cats.setdefault(cat_name, blank_category(cat_name))
                cat["jobs"] += 1
                if job.get("status") in {"queued", "running"}:
                    row["unfinished"] += 1
                    cat["unfinished"] += 1
                for rec in job.get("records") or []:
                    status = str(rec.get("status") or "")
                    row["items"] += 1
                    cat["items"] += 1
                    if status == "success":
                        row["success"] += 1
                        cat["success"] += 1
                    elif status == "timeout":
                        # 超时单列：既算进 failed（业务上没拿到片子），也单独计数，
                        # 让管理员知道其中有多少条是「本机没等到、云端可能已计费」。
                        row["failed"] += 1
                        row["timeout"] += 1
                        cat["failed"] += 1
                        cat["timeout"] += 1
                    elif status in failed_states:
                        row["failed"] += 1
                        cat["failed"] += 1
                    elif status == "running":
                        row["unfinished"] += 1
                        cat["unfinished"] += 1
                    try:
                        seconds = float(rec.get("billable_seconds") or 0)
                    except (TypeError, ValueError):
                        seconds = 0.0
                    row["billable_seconds"] += seconds
                    cat["billable_seconds"] += seconds
                    cost_value, _cost_source = self._record_cost(rec, job)
                    row["estimated_cost"] += cost_value
                    cat["estimated_cost"] += cost_value
                row["last_at"] = max(row["last_at"], created,
                                     float(job.get("finished_at") or 0))
                cat["last_at"] = max(cat["last_at"], created,
                                     float(job.get("finished_at") or 0))

        for row in rows.values():
            row["success_rate"] = round(row["success"] / row["items"], 4) if row["items"] else 0.0
            row["billable_seconds"] = round(row["billable_seconds"], 2)
            row["estimated_cost"] = round(row["estimated_cost"], 2)
        for cat in cats.values():
            cat["success_rate"] = round(cat["success"] / cat["items"], 4) if cat["items"] else 0.0
            cat["billable_seconds"] = round(cat["billable_seconds"], 2)
            cat["estimated_cost"] = round(cat["estimated_cost"], 2)

        accounts = sorted(rows.values(),
                          key=lambda r: (-r["estimated_cost"], -r["items"], r["owner"]))
        categories = sorted(cats.values(),
                            key=lambda r: (-r["estimated_cost"], -r["items"], r["category"]))
        totals = {key: 0 for key in ("jobs", "items", "success", "failed", "unfinished",
                                     "timeout")}
        for row in accounts:
            for key in totals:
                totals[key] += int(row[key])
        totals["billable_seconds"] = round(sum(row["billable_seconds"] for row in accounts), 2)
        totals["estimated_cost"] = round(sum(row["estimated_cost"] for row in accounts), 2)
        totals["accounts"] = len(accounts)
        return {"accounts": accounts, "categories": categories, "totals": totals}

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
                        # 任务分类（老任务为空，由 server 侧按批次兜底后再折成「未分类」）
                        "category": str(job.get("category") or ""),
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
            elif status in _FAILED_RECORD_STATES:
                failed += 1
            else:
                # 剩余的只有 running：用 else 兜底而不是白名单，保证
                # items = success + failed + unfinished 恒成立（新状态不会漏计）。
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

    def public(self, job: dict, admin: bool = False) -> dict:
        with self.lock:
            safe = {k: job.get(k) for k in (
                "id", "owner", "status", "created_at", "started_at", "finished_at",
                "batch_id",                 "batch_ref", "image_name", "prompt_id", "template_ids", "resolution",
                "category",
                "duration", "total", "done", "message", "log_tail", "cancel_requested",
                "lane", "style_no", "tags")}
            # 复查超时任务（P0）的进度：前端据此显示「已复查 3/23」并保持轮询。
            state = job.get("recheck")
            safe["recheck"] = dict(state) if isinstance(state, dict) else None
            safe["batch_ref"] = str(safe.get("batch_ref") or "")
            # 只下发可访问的缩略 URL 与文件名；images 里有服务器绝对路径，绝不出公网。
            slots = image_slots(job, self.root)
            safe["image_urls"] = [f"/api/uploads/{s['upload_id']}" for s in slots if s.get("upload_id")]
            safe["image_names"] = [s["name"] for s in slots]
            if safe.get("message"):
                safe["message"] = _redact(safe["message"])
            safe["log_tail"] = [_redact(line) for line in (safe.get("log_tail") or [])]
            summary = job.get("summary")
            if isinstance(summary, dict):
                safe["summary"] = {k: v for k, v in summary.items()
                                   if k != "out_dir" and (admin or k not in _MONEY_KEYS)}
            else:
                safe["summary"] = None
            safe["records"] = []
            record_keys = ("id", "status", "seconds", "error", "task_id", "request_id",
                           "billable_seconds", "estimated_cost", "verify", "cos_key",
                           # 云端已出片、只是下载失败：前端据此把「重试」提示成「复查补下载」。
                           "download_failed")
            if not admin:
                record_keys = tuple(k for k in record_keys if k not in _MONEY_KEYS)
            for rec in job.get("records", []):
                public_record = {k: rec.get(k) for k in record_keys if k in rec}
                # 每条视频的唯一 ID：老记录没有落盘时由 job + 模板 ID 派生，只读时补也不变。
                public_record["record_id"] = record_uid(str(job.get("id")), rec)
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
            if not job or not job.get("out_dir"):
                return None
            # 历史任务存的是生成那一刻的绝对路径；换代码文件夹后按当前数据根重锚。
            return relocate(job["out_dir"], self.root)

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
