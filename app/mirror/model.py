#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 web-data 里的 JSON 记录翻译成镜像表的行（纯函数，不做 IO、不建连接）。

这里只负责「字段怎么映射、路径怎么相对化、敏感信息怎么去掉」，
什么时候写库、写哪些行由 `app.mirror.core` 决定。单独拆出来的好处：
映射规则可以直接单测，不用起同步线程。

映射约定：

* 时间：epoch 秒（REAL）为主，另给 `created_local` / `day`（系统本地时区字符串），
  与 `JobManager.usage_for()`、飞书日报的归日方式一致；
* 路径：相对 `data_root`、POSIX 分隔符；不在数据目录下就原样保留并置 `path_external=1`；
* 密钥：`cos_url` 一律去掉 query string（原值带 `q-ak`/`q-signature`，入库即泄密），
  错误信息与消息文本再过一遍正则脱敏。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from app import billing

# 与 app/web/jobs.py 的 _SECRET_PATTERNS 同源：镜像会被别的程序读，不能把 Key 带出去。
_SECRET_PATTERNS = (
    (re.compile(r"sk-[A-Za-z0-9._-]{12,}"), "[已隐藏密钥]"),
    (re.compile(r"(q-signature|q-ak|SecretId|SecretKey)=([^&\s]+)", re.I), r"\1=[已隐藏]"),
)

_TERMINAL_FAILED = billing.FAILED_RECORD_STATES


def redact(text: str) -> str:
    result = str(text or "")
    for pattern, replacement in _SECRET_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def dumps(value) -> str:
    """紧凑 JSON；空值统一存空串，避免报表里出现一堆 'null' / '{}'。"""
    if value in (None, "", [], {}):
        return ""
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return ""


def local_parts(ts) -> tuple[str, str]:
    """epoch 秒 → (本地时间字符串, YYYY-MM-DD)。非法/缺失返回空串。"""
    try:
        value = float(ts)
    except (TypeError, ValueError):
        return "", ""
    if value <= 0:
        return "", ""
    try:
        moment = datetime.fromtimestamp(value)
    except (OverflowError, OSError, ValueError):
        return "", ""
    return moment.strftime("%Y-%m-%d %H:%M:%S"), moment.strftime("%Y-%m-%d")


def rel_path(raw, data_root) -> tuple[str, int]:
    """绝对路径 → 相对 data_root 的 POSIX 路径；不在其下则原样返回并标记 external。

    返回 (路径, 是否外部)。空值返回 ("", 0)。
    注意：这里**不做存在性检查**，也不尝试 relocate()——镜像记录的是"当时写下的路径"，
    文件是否还在由 records.file_exists 单独表达。
    """
    text = str(raw or "").strip()
    if not text:
        return "", 0
    try:
        path = Path(text)
    except (OSError, ValueError):
        return text, 1
    if not path.is_absolute():
        return path.as_posix(), 0
    try:
        root = Path(data_root).resolve()
        resolved = path.resolve()
        if resolved == root:
            return "", 1
        if resolved.is_relative_to(root):
            return resolved.relative_to(root).as_posix(), 0
    except (OSError, ValueError):
        pass
    return str(text), 1


def strip_signed(url: str) -> str:
    """去掉 COS 预签名 URL 的 query/fragment，只留对象地址。"""
    text = str(url or "").strip()
    if not text:
        return ""
    return text.split("?", 1)[0].split("#", 1)[0]


def _template_index(job: dict) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for item in job.get("templates") or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("id") or item.get("template_id") or "")
        if key:
            index[key] = item
    return index


def job_rows(job: dict, data_root, unit_price) -> tuple[dict, list[dict]]:
    """一个 job → (jobs 行, records 行列表)。

    记录级统计（成功/失败/未完成、计费秒、金额）都在这里算好，SQL 端只做求和，
    这样报表口径与 `usage_for()` 用的是同一份 `app/billing.py`，不会各算各的。
    """
    job_id = str(job.get("id") or "")
    records_json = job.get("records") or []
    templates = _template_index(job)
    created_local, day = local_parts(job.get("created_at"))

    images = []
    for slot in job.get("images") or []:
        if not isinstance(slot, dict):
            continue
        rel, external = rel_path(slot.get("path"), data_root)
        images.append({"upload_id": str(slot.get("upload_id") or ""),
                       "name": str(slot.get("name") or ""),
                       "path": rel,
                       "external": external})
    image_path, _image_external = rel_path(job.get("image_path"), data_root)
    if not image_path and images:
        image_path = images[0]["path"]
    out_dir, _out_external = rel_path(job.get("out_dir"), data_root)

    rows: list[dict] = []
    success = failed = unfinished = 0
    seconds_total = 0.0
    cost_total = 0.0
    done = 0
    for seq, rec in enumerate(records_json):
        if not isinstance(rec, dict):
            continue
        template_id = str(rec.get("id") or rec.get("template_id") or "")
        meta = templates.get(template_id) or {}
        status = str(rec.get("status") or "")
        if status == "success":
            success += 1
            done += 1
        elif status in _TERMINAL_FAILED:
            failed += 1
        elif status == "running":
            unfinished += 1
        try:
            seconds = float(rec.get("billable_seconds") or 0)
        except (TypeError, ValueError):
            seconds = 0.0
        cost, cost_source = billing.record_cost(rec, job, unit_price)
        seconds_total += seconds
        cost_total += cost

        file_rel, file_external = rel_path(rec.get("file"), data_root)
        exists = 0
        if file_rel and not file_external:
            try:
                exists = 1 if (Path(data_root) / file_rel).exists() else 0
            except OSError:
                exists = 0

        cos = rec.get("cos") if isinstance(rec.get("cos"), dict) else {}
        cos_key = str(cos.get("key") or rec.get("cos_key") or "")
        cos_url = strip_signed(cos.get("url") or rec.get("cos_url") or "")

        rows.append({
            "record_id": str(rec.get("record_id") or f"{job_id}:{seq}"),
            "job_id": job_id,
            "seq": seq,
            "template_id": template_id,
            "template_name": str(meta.get("name") or rec.get("template_name") or ""),
            "template_tags_json": dumps(meta.get("tags") or rec.get("template_tags")),
            "template_url": strip_signed(rec.get("template") or meta.get("url") or ""),
            "status": status,
            "provider": str(rec.get("provider") or ""),
            "model": str(rec.get("model") or ""),
            "mode": str(rec.get("mode") or ""),
            "resolution": str(rec.get("resolution") or job.get("resolution") or ""),
            "file": file_rel,
            "file_exists": exists,
            "path_external": file_external,
            "billable_seconds": seconds,
            "estimated_cost": cost,
            "cost_source": cost_source,
            "seconds": rec.get("seconds"),
            "error": redact(rec.get("error") or ""),
            "task_id": str(rec.get("task_id") or ""),
            "request_id": str(rec.get("request_id") or ""),
            "submitted_at": str(rec.get("submitted_at") or ""),
            "submitted_ts": _epoch(rec.get("submitted_ts") or rec.get("submitted_at_ts")),
            "cos_key": cos_key,
            "cos_url": cos_url,
            "usage_json": dumps(rec.get("usage")),
            "verify_json": dumps(rec.get("verify")),
            "cos_tags_json": dumps(cos.get("tags") or rec.get("cos_tags")),
        })

    row = {
        "id": job_id,
        "owner": str(job.get("owner") or ""),
        "status": str(job.get("status") or ""),
        "batch_id": str(job.get("batch_id") or ""),
        "batch_ref": str(job.get("batch_ref") or ""),
        "category": str(job.get("category") or "").strip(),
        "lane": str(job.get("lane") or ""),
        "created_at": _epoch(job.get("created_at")),
        "created_local": created_local,
        "day": day,
        "started_at": _epoch(job.get("started_at")),
        "finished_at": _epoch(job.get("finished_at")),
        "prompt_id": str(job.get("prompt_id") or ""),
        "resolution": str(job.get("resolution") or ""),
        "duration": _int(job.get("duration")),
        "total": _int(job.get("total")) if job.get("total") is not None else len(rows),
        "done": done,
        "success_count": success,
        "failed_count": failed,
        "unfinished_count": unfinished,
        "billable_seconds": round(seconds_total, 4),
        "estimated_cost": round(cost_total, 4),
        "message": redact(job.get("message") or ""),
        "out_dir": out_dir,
        "image_path": image_path,
        "images_json": dumps(images),
        "tags_json": dumps(job.get("tags")),
    }
    return row, rows


def batch_rows(batch: dict) -> tuple[dict, list[dict]]:
    """一个批次（草稿/锁定/生产中）→ (batches 行, batch_items 行列表)。"""
    batch_id = str(batch.get("id") or "")
    items = batch.get("items") or []
    created_local, _day = local_parts(batch.get("created_at"))

    counters = {"done": 0, "failed": 0, "pending": 0, "producing": 0}
    rows: list[dict] = []
    for seq, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        state = str(item.get("state") or "")
        if state in ("done", "success", "已生成"):
            counters["done"] += 1
        elif state in ("failed", "error", "失败"):
            counters["failed"] += 1
        elif state in ("producing", "running", "生成中"):
            counters["producing"] += 1
        else:
            counters["pending"] += 1
        rows.append({
            "batch_id": batch_id,
            "template_id": str(item.get("template_id") or item.get("id") or ""),
            "seq": seq,
            "name": str(item.get("name") or item.get("template_name") or ""),
            "state": state,
            "job_id": str(item.get("job_id") or ""),
            "tags_json": dumps(item.get("tags")),
            "error": redact(item.get("error") or ""),
        })

    row = {
        "id": batch_id,
        "batch_no": str(batch.get("batch_no") or batch.get("ref") or ""),
        "title": str(batch.get("title") or ""),
        "owner": str(batch.get("owner") or ""),
        "state": str(batch.get("state") or ""),
        "style_no": str(batch.get("style_no") or ""),
        "category": str(batch.get("category") or "").strip(),
        "prompt_id": str(batch.get("prompt_id") or ""),
        "resolution": str(batch.get("resolution") or ""),
        "duration": _int(batch.get("duration")),
        "total_items": len(rows),
        "done": counters["done"],
        "failed": counters["failed"],
        "pending": counters["pending"],
        "producing": counters["producing"],
        "created_at": _epoch(batch.get("created_at")),
        "created_local": created_local,
        "updated_at": _epoch(batch.get("updated_at")),
        "locked_at": _epoch(batch.get("locked_at")),
        "closed_at": _epoch(batch.get("closed_at")),
        "tags_json": dumps(batch.get("tags")),
        "tag_filters_json": dumps(batch.get("tag_filters")),
        "tag_excludes_json": dumps(batch.get("tag_excludes")),
        "images_json": dumps(_normalized_images(batch.get("images"))),
    }
    return row, rows


def upload_row(meta: dict, data_root) -> dict:
    """一张上传素材（uploads/meta/<id>.json）→ uploads 行。"""
    info = meta.get("info") if isinstance(meta.get("info"), dict) else {}
    raw_path = meta.get("path") or info.get("path")
    path, external = rel_path(raw_path, data_root)
    created_local, _day = local_parts(meta.get("created_at"))
    return {
        "id": str(meta.get("id") or ""),
        "owner": str(meta.get("owner") or ""),
        "name": str(meta.get("name") or ""),
        "path": path,
        "size_mb": _float(info.get("size_mb")),
        "format": str(info.get("format") or ""),
        "width": _int(info.get("width")),
        "height": _int(info.get("height")),
        "has_alpha": 1 if info.get("has_alpha") else 0,
        "created_at": _epoch(meta.get("created_at")),
        "created_local": created_local,
        "path_external": external,
    }


def _normalized_images(value) -> list:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _epoch(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
