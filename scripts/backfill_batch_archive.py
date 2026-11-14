#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""补齐历史批次的永久档案。

用法（在运行区或设计区根目录执行都可以，路径从本文件推）：

    python scripts/backfill_batch_archive.py --dry-run
    python scripts/backfill_batch_archive.py
    python scripts/backfill_batch_archive.py --batch e0270b4afdd8
    python scripts/backfill_batch_archive.py --data-dir "D:\\...\\web-data"

做三件事：
  1. 逐批次调用 BatchArchive.ensure()，复制参考图、写 meta/prompt/readme/checksums；
  2. 按 runs 逐个读 web-data/jobs/<job_id>.json，写 runs/<job_id>.json 快照
     （提示词优先用 job 里当轮快照，图片优先用 job.images）；
  3. 打印统计；单个批次失败不影响其他批次，最后以非零退出码提示失败数量。

幂等：重复执行只会补缺失文件，不会重复复制已有图片。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.web.batch_archive import BatchArchive, normalize_images  # noqa: E402


def _data_dir(override: str | None) -> Path:
    if override:
        return Path(override).expanduser().resolve()
    web_json = ROOT / "server" / "web.json"
    try:
        raw = json.loads(web_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    value = str(raw.get("data_root") or "web-data")
    path = Path(value)
    return (path if path.is_absolute() else ROOT / path).resolve()


def _job_images(job: dict, data_dir: Path) -> list[dict]:
    """job 自己的 images[] 优先；老 job 回退 image_path。"""
    return normalize_images({
        "images": job.get("images"),
        "image_path": job.get("image_path"),
        "image_upload_id": job.get("image_upload_id"),
        "image_name": job.get("image_name"),
    }, data_dir)


def _template_ids(job: dict) -> list[str]:
    out: list[str] = []
    for item in job.get("templates") or []:
        if isinstance(item, dict) and item.get("id"):
            out.append(str(item["id"]))
        elif isinstance(item, str):
            out.append(item)
    return out


def backfill_one(batch: dict, jobs_dir: Path, archive: BatchArchive,
                 dry_run: bool) -> tuple[int, int]:
    """返回 (复制的图片数, 写的 run 数)；异常向上抛，由调用方计失败。"""
    if dry_run:
        images = normalize_images(batch, archive.data_dir)
        return len(images), len(batch.get("runs") or [])

    meta = archive.ensure(batch, event="backfill", user="backfill")
    copied = len(meta.get("current_images") or [])
    written = 0
    for run in batch.get("runs") or []:
        if not isinstance(run, dict):
            continue
        job_id = str(run.get("job_id") or "").strip()
        if not job_id:
            continue
        job = {}
        job_path = jobs_dir / f"{job_id}.json"
        try:
            loaded = json.loads(job_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                job = loaded
        except (OSError, json.JSONDecodeError):
            job = {}
        prompt = str(job.get("prompt") or batch.get("prompt") or "")
        images = _job_images(job, archive.data_dir) if job else normalize_images(batch, archive.data_dir)
        archive.write_run(
            batch, job_id=job_id, prompt=prompt, images=images,
            template_ids=_template_ids(job),
            at=run.get("started_at") or job.get("created_at"),
        )
        written += 1
    return copied, written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="补齐历史批次的永久档案")
    parser.add_argument("--data-dir", default="", help="web-data 目录；默认读 server/web.json 的 data_root")
    parser.add_argument("--batch", action="append", default=[], help="只补指定 batch id，可重复")
    parser.add_argument("--limit", type=int, default=0, help="最多处理多少个批次（0=全部）")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写文件")
    args = parser.parse_args(argv)

    data_dir = _data_dir(args.data_dir or None)
    batches_dir = data_dir / "batches"
    jobs_dir = data_dir / "jobs"
    if not batches_dir.is_dir():
        print(f"找不到批次目录：{batches_dir}", file=sys.stderr)
        return 2

    wanted = {str(item).strip() for item in args.batch if str(item).strip()}
    archive = BatchArchive(data_dir, create=not args.dry_run)
    paths = sorted(batches_dir.glob("*.json"))
    if wanted:
        paths = [path for path in paths if path.stem in wanted]
    if args.limit and args.limit > 0:
        paths = paths[:args.limit]

    ok = failed = total_images = total_runs = 0
    failures: list[tuple[str, str]] = []
    for path in paths:
        try:
            batch = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            failed += 1
            failures.append((path.stem, f"读取失败：{exc}"))
            continue
        if not isinstance(batch, dict):
            failed += 1
            failures.append((path.stem, "不是 JSON 对象"))
            continue
        batch.setdefault("id", path.stem)
        try:
            copied, written = backfill_one(batch, jobs_dir, archive, args.dry_run)
        except Exception as exc:  # noqa: BLE001 - 单个批次失败继续跑下一个
            failed += 1
            failures.append((path.stem, str(exc)))
            print(f"[失败] {path.stem}：{exc}", file=sys.stderr)
            continue
        ok += 1
        total_images += copied
        total_runs += written
        print(f"[{'检查' if args.dry_run else '补齐'}] {batch.get('batch_no') or '?'} "
              f"{path.stem}：图 {copied} 张，run {written} 个")

    print()
    print(f"完成：成功 {ok} 个批次，失败 {failed} 个；图片 {total_images} 张，run 快照 {total_runs} 个。")
    print(f"档案根：{archive.root}")
    if failures:
        print("失败明细：", file=sys.stderr)
        for batch_id, reason in failures:
            print(f"  - {batch_id}: {reason}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
