#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给「成品」回填标签：按 key 里的模板编号，去清单里查 shot，再打到对象上。

成品 key 结构（object_key() 生成）：
    prod/output/2026-10-26/20261026A/001-9c1b2d4e.mp4
                                        └┬┘ └──┬───┘
                                        id   内容指纹

打上的标签：shot / tpl_id / kind=out / batch

用法：
    python3 tools/tag_outputs.py --dry-run            # 只看会打什么
    python3 tools/tag_outputs.py                      # 真打
    python3 tools/tag_outputs.py --batch 20261026A    # 只处理某批次
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

BUCKET = "video-output-1463907213"
REGION = "ap-guangzhou"
PREFIX = "prod/output/"
NAME_RE = re.compile(r"^(\d{3})-[0-9a-f]{1,16}\.\w+$", re.I)


def load_client():
    from qcloud_cos import CosConfig, CosS3Client

    cred = Path.home() / ".cos" / "credentials.json"
    if not cred.exists():
        sys.exit(f"缺少管理员凭证：{cred}")
    data = json.loads(cred.read_text(encoding="utf-8-sig"))
    return CosS3Client(CosConfig(Region=REGION, SecretId=data["secret_id"], SecretKey=data["secret_key"]))


def load_shots(path: str) -> dict[str, str]:
    p = Path(path)
    if not p.exists():
        sys.exit(f"清单不存在：{p}（先跑 upload_templates.py）")
    data = json.loads(p.read_text(encoding="utf-8-sig"))
    items = data.get("templates") if isinstance(data, dict) else data
    return {str(t.get("id")): t.get("shot") or "" for t in items or []}


def iter_keys(client, prefix: str):
    marker = ""
    while True:
        resp = client.list_objects(Bucket=BUCKET, Prefix=prefix, Marker=marker, MaxKeys=1000)
        for obj in resp.get("Contents") or []:
            yield obj["Key"]
        if str(resp.get("IsTruncated")).lower() == "true" and resp.get("NextMarker"):
            marker = resp["NextMarker"]
        else:
            return


def parse(key: str):
    """返回 (tpl_id, batch) 或 None。"""
    parts = key.split("/")
    if len(parts) < 3:
        return None
    m = NAME_RE.match(parts[-1])
    if not m:
        return None
    batch = parts[-2] if len(parts) >= 2 else ""
    return m.group(1), batch


def main() -> int:
    ap = argparse.ArgumentParser(description="成品标签回填")
    ap.add_argument("--manifest", default="build/manifest/manifest.json")
    ap.add_argument("--batch", default="", help="只处理某个批次目录")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    shots = load_shots(args.manifest)
    print(f"[清单] {len(shots)} 个模板的 shot 映射")
    client = load_client()

    done = skipped = unknown = 0
    for n, key in enumerate(iter_keys(client, PREFIX), start=1):
        if args.limit and n > args.limit:
            break
        parsed = parse(key)
        if not parsed:
            unknown += 1
            continue
        tpl_id, batch = parsed
        if args.batch and batch != args.batch:
            continue
        shot = shots.get(tpl_id)
        if not shot:
            unknown += 1
            print(f"  [跳过] {key}：清单里没有模板 {tpl_id}")
            continue
        tags = {"shot": shot, "tpl_id": tpl_id, "kind": "out", "batch": batch}
        if args.dry_run:
            print(f"  [dry] {key} -> {tags}")
            done += 1
            continue
        try:
            client.put_object_tagging(Bucket=BUCKET, Key=key, Tagging={
                "TagSet": {"Tag": [{"Key": k, "Value": v} for k, v in tags.items()]}})
            done += 1
        except Exception as exc:  # noqa: BLE001
            skipped += 1
            print(f"  [失败] {key}：{str(exc)[:90]}")

    print(f"[结果] 打标签 {done} / 失败 {skipped} / 无法识别 {unknown}")
    return 0 if skipped == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
