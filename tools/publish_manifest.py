#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把本地清单发布到模板桶根目录（覆盖写同名对象，URL 永远不变）。

用法：
    python3 tools/publish_manifest.py                       # 发布 build/manifest/manifest.json
    python3 tools/publish_manifest.py --file 其他.json
    python3 tools/publish_manifest.py --verify-only         # 只验证线上能否解析
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import urllib.error
import urllib.request

BUCKET = "video-input-1463907213"
REGION = "ap-guangzhou"
KEY = "manifest.json"
URL = f"https://{BUCKET}.cos.{REGION}.myqcloud.com/{KEY}"
DEFAULT_FILE = "build/manifest/manifest.json"


def load_client():
    from qcloud_cos import CosConfig, CosS3Client

    cred = pathlib.Path.home() / ".cos" / "credentials.json"
    if not cred.exists():
        sys.exit(f"缺少管理员凭证：{cred}")
    data = json.loads(cred.read_text(encoding="utf-8-sig"))
    return CosS3Client(CosConfig(Region=REGION, SecretId=data["secret_id"], SecretKey=data["secret_key"]))


def verify() -> int:
    print(f"[验证] 匿名拉取 {URL}")
    try:
        with urllib.request.urlopen(URL, timeout=20) as r:
            raw = r.read()
            print(f"  HTTP {r.status} | {len(raw)} 字节 | {r.headers.get('Content-Type')}")
    except urllib.error.HTTPError as exc:
        print(f"  HTTP {exc.code} —— 清单还没发布，或对象不是公有读")
        return 1
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    from app.core import load_manifest

    items = load_manifest(URL)
    shots = {}
    for it in items:
        shots[it.get("id")] = it.get("shot")
    print(f"  客户端视角解析：{len(items)} 条；首条 {items[0]['id']} / 末条 {items[-1]['id']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="发布模板清单到 COS")
    ap.add_argument("--file", default=DEFAULT_FILE)
    ap.add_argument("--verify-only", action="store_true")
    args = ap.parse_args()

    if args.verify_only:
        return verify()

    path = pathlib.Path(args.file)
    if not path.exists():
        sys.exit(f"清单不存在：{path}")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    items = data.get("templates") if isinstance(data, dict) else data
    if not items:
        sys.exit("清单为空，拒绝发布")
    body = path.read_bytes()

    client = load_client()
    client.put_object(Bucket=BUCKET, Key=KEY, Body=body,
                      ContentType="application/json; charset=utf-8",
                      CacheControl="no-cache, max-age=60")
    client.put_object_acl(Bucket=BUCKET, Key=KEY, ACL="public-read")
    client.put_object_tagging(Bucket=BUCKET, Key=KEY, Tagging={
        "TagSet": {"Tag": [{"Key": "kind", "Value": "manifest"},
                           {"Key": "count", "Value": str(len(items))}]}})
    print(f"[发布] {path} → {URL}（{len(items)} 条，{len(body)} 字节）")
    return verify()


if __name__ == "__main__":
    sys.exit(main())
