#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把本机已经下好的成片补传到腾讯云 COS，并回写 batch.json 里的 cos_key。

用途：先把历史批次补档，再开新批次。脚本可重复执行——同一份内容
（文件名 + 内容指纹）永远落到同一个 Key，不会越传越多。

用法：
    set COS_SECRET_ID=...& set COS_SECRET_KEY=...& set COS_BUCKET=...
    set COS_REGION=ap-guangzhou
    python3 scripts/archive_to_cos.py --root out --batch 20261024A
    python3 scripts/archive_to_cos.py --root out --dry-run        # 只看会传什么
    python3 scripts/archive_to_cos.py --root out --delete-local   # 传完删本地（谨慎）

退出码：0 全部成功 / 1 有失败 / 2 配置或参数有问题。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.storage import (  # noqa: E402
    StorageConfig, StorageError, file_fingerprint, object_key, open_storage,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="本机成片 → 腾讯云 COS 补传")
    parser.add_argument("--root", default="out", help="输出根目录（默认 out）")
    parser.add_argument("--batch", default="", help="只处理某个批次目录")
    parser.add_argument("--prefix", default="output", help="对象前缀（默认 output）")
    parser.add_argument("--workers", type=int, default=4, help="并发上传数（默认 4）")
    parser.add_argument("--expires", type=int, default=7 * 24 * 3600,
                        help="写入索引的签名链接有效期（秒）")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不真传")
    parser.add_argument("--force", action="store_true", help="已存在也重传")
    parser.add_argument("--delete-local", action="store_true",
                        help="上传并校验大小后删除本地文件（不可恢复）")
    parser.add_argument("--no-rewrite", action="store_true", help="不修改 batch.json")
    parser.add_argument("--limit", type=int, default=0, help="最多处理几个文件（调试用）")
    return parser.parse_args(argv)


def collect(root: Path, batch: str) -> list[tuple[Path, str]]:
    """返回 [(本地路径, 批次名)]；批次名取 root 下的第一层目录。"""
    if not root.exists():
        raise SystemExit(f"目录不存在：{root}")
    files: list[tuple[Path, str]] = []
    for path in sorted(root.rglob("*.mp4")):
        rel = path.relative_to(root)
        name = rel.parts[0] if len(rel.parts) > 1 else ""
        if batch and name != batch:
            continue
        files.append((path, name))
    return files


def batch_date(path: Path) -> datetime:
    """用文件修改时间做日期分区：重跑脚本 Key 不会漂移。"""
    try:
        return datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return datetime.now()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = Path(args.root).expanduser()

    try:
        config = StorageConfig.from_env(prefix=args.prefix)
        if not config.enabled:
            raise StorageError("未启用对象存储：请设置 COS_BUCKET / COS_REGION / COS_SECRET_ID / COS_SECRET_KEY")
        storage = open_storage(config)
    except StorageError as exc:
        print(f"✗ 配置有误：{exc}", file=sys.stderr)
        return 2
    assert storage is not None

    files = collect(root, args.batch)
    if args.limit:
        files = files[:args.limit]
    if not files:
        print(f"没有找到 mp4：{root}")
        return 0

    total_bytes = sum(p.stat().st_size for p, _ in files if p.exists())
    print(f"目标：{config.describe()}｜文件 {len(files)} 个｜共 {total_bytes / 1024 / 1024:.1f} MB")
    if args.dry_run:
        for path, batch in files:
            key = object_key(args.prefix, path.name, batch=batch,
                             digest=file_fingerprint(path), when=batch_date(path))
            state = "已存在" if storage.exists(key) else "待上传"
            print(f"  [{state}] {path}  ->  {key}")
        print("（--dry-run，未实际上传）")
        return 0

    started = time.time()
    rows: list[dict] = []
    failures: list[str] = []

    def work(item: tuple[Path, str]) -> None:
        path, batch = item
        key = object_key(args.prefix, path.name, batch=batch,
                         digest=file_fingerprint(path), when=batch_date(path))
        try:
            if not args.force and storage.exists(key):
                signed = storage.sign_url(key, args.expires)
                rows.append({"batch": batch, "file": path.name, "key": key,
                             "size": path.stat().st_size, "url": signed, "status": "skipped"})
                return
            storage.put_path(path, key, content_type="video/mp4")
            stored = storage.stat(key)
            local_size = path.stat().st_size
            if int(stored.get("size", 0)) != local_size:
                raise StorageError(f"大小不一致：本地 {local_size} / 云端 {stored.get('size')}")
            rows.append({"batch": batch, "file": path.name, "key": key,
                         "size": local_size, "url": storage.sign_url(key, args.expires),
                         "status": "uploaded"})
            if args.delete_local:
                path.unlink()
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{path}: {exc}")

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        list(pool.map(work, files))

    rows.sort(key=lambda r: (r["batch"], r["file"]))
    if rows:
        index_path = root / "cos-index.csv"
        with index_path.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=["batch", "file", "key", "size", "status", "url"])
            writer.writeheader()
            writer.writerows(rows)
        print(f"\n索引已写入：{index_path}")

    if not args.no_rewrite:
        rewrite_batches(root, rows)

    uploaded = [r for r in rows if r["status"] == "uploaded"]
    skipped = [r for r in rows if r["status"] == "skipped"]
    moved = sum(r["size"] for r in uploaded)
    print(f"\n完成：上传 {len(uploaded)}｜跳过 {len(skipped)}｜失败 {len(failures)}"
          f"｜耗时 {time.time() - started:.1f}s｜{moved / 1024 / 1024:.1f} MB")
    for line in failures[:10]:
        print(f"  ✗ {line}", file=sys.stderr)
    if failures:
        return 1
    return 0


def rewrite_batches(root: Path, rows: list[dict]) -> None:
    """把 cos_key / cos_url 回写到各批次的 batch.json，失败不影响主流程。"""
    by_batch: dict[str, dict[str, dict]] = {}
    for row in rows:
        by_batch.setdefault(row["batch"], {})[row["file"]] = row

    for batch, mapping in by_batch.items():
        state_path = (root / batch / "batch.json") if batch else (root / "batch.json")
        if not state_path.exists():
            continue
        try:
            # utf-8-sig：兼容记事本/PowerShell 5 写出的带 BOM 文件
            state = json.loads(state_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  跳过 {state_path}：{exc}")
            continue
        changed = 0
        for rec in state.get("records") or []:
            name = Path(str(rec.get("file") or "")).name
            row = mapping.get(name)
            if not row:
                continue
            if rec.get("cos_key") != row["key"]:
                rec["cos_key"] = row["key"]
                rec["cos_url"] = row["url"]
                changed += 1
        if changed:
            tmp = state_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(state_path)
            print(f"  回写 {state_path}（{changed} 条）")


if __name__ == "__main__":
    raise SystemExit(main())
