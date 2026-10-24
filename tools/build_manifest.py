#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把一整个文件夹的模板视频整理成 manifest.json（含时长校验 + 海报抽帧）。

    python3 tools/build_manifest.py --src D:\\templates --base-url https://cdn.example.com/tpl \
        --out manifest.json --posters posters

产出：
    manifest.json          [{id, name, url, duration, size_mb, poster}]
    posters/001.jpg        每个模板一张 480px 宽的海报（可选，界面用不到也不影响生产）
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

VIDEO_EXT = {".mp4", ".mov", ".m4v"}
MAX_SECONDS = 15.0
MAX_MB = 100.0


def ffprobe_duration(path: Path) -> float | None:
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    out = subprocess.run(
        [exe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True,
    )
    try:
        return round(float(out.stdout.strip()), 2)
    except (ValueError, AttributeError):
        return None


def make_poster(path: Path, dest: Path, width: int = 480) -> bool:
    exe = shutil.which("ffmpeg")
    if not exe:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [exe, "-y", "-v", "error", "-ss", "1", "-i", str(path),
         "-frames:v", "1", "-vf", f"scale={width}:-1", str(dest)],
        capture_output=True, text=True,
    )
    return result.returncode == 0 and dest.exists()


def main() -> int:
    parser = argparse.ArgumentParser(description="生成模板清单")
    parser.add_argument("--src", required=True, help="模板视频所在文件夹")
    parser.add_argument("--base-url", required=True, help="CDN 上该文件夹对应的公网前缀")
    parser.add_argument("--out", default="manifest.json")
    parser.add_argument("--posters", help="海报输出目录（不给就不抽帧）")
    parser.add_argument("--start", type=int, default=1, help="起始编号，默认 1")
    args = parser.parse_args()

    src = Path(args.src)
    if not src.is_dir():
        print(f"文件夹不存在：{src}", file=sys.stderr)
        return 1

    files = sorted(p for p in src.iterdir() if p.suffix.lower() in VIDEO_EXT and p.is_file())
    if not files:
        print("没有找到视频文件（.mp4/.mov/.m4v）", file=sys.stderr)
        return 1

    base = (args.base_url or "").rstrip("/")
    if args.mode == "key" and not args.prefix:
        args.prefix = base.rsplit("/", 1)[-1] if base else ""
    prefix = (args.prefix or "").strip("/")
    poster_dir = Path(args.posters) if args.posters else None
    entries = []
    warnings = []

    for offset, path in enumerate(files):
        tid = f"{args.start + offset:03d}"
        size_mb = round(path.stat().st_size / (1024 * 1024), 2)
        duration = ffprobe_duration(path)

        if size_mb > MAX_MB:
            warnings.append(f"{path.name}（{tid}）{size_mb} MB，超过 {MAX_MB:g} MB 上限，请压缩")
        if duration is None:
            warnings.append(f"{path.name}（{tid}）读不到时长，跳过时长校验")
        elif duration > MAX_SECONDS:
            warnings.append(f"{path.name}（{tid}）{duration}s，超过 {MAX_SECONDS:g}s 上限，会被接口拒绝")

        poster_url = None
        if poster_dir:
            dest = poster_dir / f"{tid}.jpg"
            if make_poster(path, dest):
                poster_url = (f"{prefix}/{dest.parent.name}/{dest.name}" if args.mode == "key"
                              else f"{base}/{args.posters.split('/')[-1]}/{dest.name}")

        entry = {
            "id": tid,
            "name": path.stem,
            "duration": duration,
            "size_mb": size_mb,
            "poster": poster_url,
        }
        if args.mode == "key":
            entry["key"] = f"{prefix}/{path.name}" if prefix else path.name
        else:
            entry["url"] = f"{base}/{path.name}"
        entries.append(entry)

    out = Path(args.out)
    out.write_text(json.dumps({"version": 1, "templates": entries}, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"已写入 {len(entries)} 个模板 -> {out}")
    print(f"编号范围：{entries[0]['id']} – {entries[-1]['id']}")
    if warnings:
        print(f"\n有 {len(warnings)} 条需要注意：")
        for msg in warnings:
            print("  ⚠ " + msg)
    else:
        print("全部通过：时长与体积都在接口限制内")
    return 0


if __name__ == "__main__":
    sys.exit(main())
