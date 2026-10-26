#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""模板上传 + 自动打标签 + 生成清单。

目录名即标签来源：
    ...\近景视频模板\**      → shot=near
    ...\中远视频模板\**      → shot=medium_far

产出：
    1. 把视频以 input/tpl/NNN.mp4 上传到模板桶（幂等：大小一致就跳过）
    2. 每个对象打标签 shot / kind / tpl_id
    3. 生成 manifest（含 shot 字段，客户端不读也不影响）

用法：
    python3 tools/upload_templates.py --dry-run          # 只看编号与标签，不上传
    python3 tools/upload_templates.py --limit 5          # 先传 5 个试试
    python3 tools/upload_templates.py                    # 全量
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

VIDEO_EXT = {".mp4", ".mov", ".m4v"}
MULTIPART_MIN = 8 * 1024 * 1024
BUCKET = "video-input-1463907213"
REGION = "ap-guangzhou"
PREFIX = "input/tpl"
BASE_URL = f"https://{BUCKET}.cos.{REGION}.myqcloud.com"
DEFAULT_ROOT = r"D:\视频模板分类工作区"


def load_admin():
    from qcloud_cos import CosConfig, CosS3Client

    cred = Path.home() / ".cos" / "credentials.json"
    if not cred.exists():
        sys.exit(f"缺少管理员凭证：{cred}")
    data = json.loads(cred.read_text(encoding="utf-8-sig"))
    return CosS3Client(CosConfig(Region=REGION, SecretId=data["secret_id"], SecretKey=data["secret_key"]))


def shot_of(group_dir: str) -> str:
    return "near" if "近景" in Path(group_dir).name else "medium_far"


def collect(root: Path) -> list[dict]:
    """按「目录顺序 + 文件名顺序」稳定编号；os.walk 保证父目录先于子目录。"""
    items: list[dict] = []
    groups = sorted([p for p in root.iterdir() if p.is_dir()], key=lambda p: p.name)
    for group in groups:
        if "模板" not in group.name:
            continue
        for dirpath, dirnames, filenames in os.walk(group):
            dirnames.sort()
            for name in sorted(filenames):
                path = Path(dirpath) / name
                if path.suffix.lower() not in VIDEO_EXT:
                    continue
                items.append({
                    "src": path,
                    "shot": shot_of(group.name),
                    "group": group.name,
                    "sub": Path(dirpath).name,
                })
    for idx, item in enumerate(items, start=1):
        item["id"] = f"{idx:03d}"
    return items


def duration_of(path: Path):
    for exe in ("ffprobe", r"D:\ffmpeg\ffmpeg-9.0.1-essentials_build\bin\ffprobe.exe"):
        try:
            out = subprocess.run(
                [exe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", str(path)],
                capture_output=True, text=True, timeout=30,
            )
            if out.returncode == 0 and out.stdout.strip():
                return round(float(out.stdout.strip()), 2)
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="模板上传 + 打标签 + 生成清单")
    ap.add_argument("--root", default=DEFAULT_ROOT, help="模板总目录（下面有 近景/中远 两个子目录）")
    ap.add_argument("--out", default="build/manifest/manifest.json")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个（调试用）")
    ap.add_argument("--force", action="store_true", help="忽略已存在，强制重传")
    ap.add_argument("--dry-run", action="store_true", help="只打印编号与标签，不上传")
    ap.add_argument("--no-duration", action="store_true", help="跳过 ffprobe 读时长（快）")
    args = ap.parse_args()

    root = Path(args.root)
    if not root.is_dir():
        sys.exit(f"目录不存在：{root}")

    items = collect(root)
    if args.limit:
        items = items[: args.limit]
    if not items:
        sys.exit("没找到视频文件")

    near = sum(1 for i in items if i["shot"] == "near")
    print(f"[扫描] {len(items)} 个视频 | 近景 {near} | 中远景 {len(items) - near}")
    for it in items[:3]:
        print(f"    {it['id']}  {it['group']}/{it['sub']}/{it['src'].name}  shot={it['shot']}")
    if len(items) > 3:
        print(f"    …（共 {len(items)} 条）")

    if args.dry_run:
        print("[dry-run] 不上传，结束")
        return 0

    client = load_admin()
    entries, uploaded, skipped, failed = [], 0, 0, 0
    for n, it in enumerate(items, start=1):
        key = f"{PREFIX}/{it['id']}.mp4"
        size = it["src"].stat().st_size
        need = True
        if not args.force:
            try:
                head = client.head_object(Bucket=BUCKET, Key=key)
                need = int(head.get("Content-Length") or 0) != size
            except Exception:  # noqa: BLE001 - 不存在就走上传
                need = True
        if need:
            try:
                if size >= MULTIPART_MIN:
                    client.upload_file(Bucket=BUCKET, Key=key, LocalFilePath=str(it["src"]),
                                       PartSize=10, MAXThread=5, EnableMD5=False)
                else:
                    client.put_object(Bucket=BUCKET, Key=key, Body=it["src"].read_bytes(),
                                      ContentType="video/mp4")
                uploaded += 1
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"  [{it['id']}] 上传失败：{str(exc)[:100]}")
                continue
        else:
            skipped += 1

        tags = {"shot": it["shot"], "kind": "tpl", "tpl_id": it["id"]}
        try:
            client.put_object_tagging(Bucket=BUCKET, Key=key, Tagging={
                "TagSet": {"Tag": [{"Key": k, "Value": v} for k, v in tags.items()]}})
        except Exception as exc:  # noqa: BLE001
            print(f"  [{it['id']}] 打标签失败：{str(exc)[:100]}")

        # 模型要匿名直链拉取，所以对象必须是公有读（桶策略暂无权限，只能逐对象设）
        try:
            client.put_object_acl(Bucket=BUCKET, Key=key, ACL="public-read")
        except Exception as exc:  # noqa: BLE001
            print(f"  [{it['id']}] 设公有读失败：{str(exc)[:100]}")

        entries.append({
            "id": it["id"],
            "name": it["src"].stem,
            "shot": it["shot"],
            "size_mb": round(size / 1024 / 1024, 2),
            "duration": None if args.no_duration else duration_of(it["src"]),
            "url": f"{BASE_URL}/{key}",
        })
        if n % 10 == 0 or n == len(items):
            print(f"  …进度 {n}/{len(items)}（上传 {uploaded} 跳过 {skipped} 失败 {failed}）")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "count": len(entries), "prefix": PREFIX, "templates": entries}
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[清单] 已写入 {out}（{len(entries)} 条）")
    print(f"[结果] 上传 {uploaded} / 跳过 {skipped} / 失败 {failed}")
    return 0 if failed == 0 else 4


if __name__ == "__main__":
    sys.exit(main())
