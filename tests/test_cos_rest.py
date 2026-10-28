#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""标准库 COS 兜底层的离线测试（不联网、不需要 qcloud_cos）。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _tmpdir import tempdir  # noqa: E402

from app.cos_rest import CosRestClient, CosRestStorage  # noqa: E402
from app.storage import StorageConfig  # noqa: E402


def check(condition, message):
    if not condition:
        raise AssertionError(message)
    print(f"  ✓ {message}")


class FakeClient:
    def __init__(self):
        self.puts = []
        self.signed = []
        self.deleted = []

    def put(self, key, data, content_type="application/octet-stream"):
        self.puts.append((key, data, content_type))
        return "etag"

    def put_file(self, key, path, content_type=""):
        self.puts.append((key, Path(path).read_bytes(), content_type))
        return "etag"

    def sign_url(self, key, expires=3600):
        self.signed.append((key, expires))
        return f"https://cdn.example.com/{key}?expires={expires}"

    def delete(self, key):
        self.deleted.append(key)


def main() -> int:
    print("签名参数：")
    client = CosRestClient("demo-1250000000", "ap-guangzhou", "AKIDtest", "secret")
    auth = client._auth("get", "prod/output/source/a.jpg", 600)
    check(auth["q-sign-algorithm"] == "sha1", "q-sign-algorithm")
    check(auth["q-ak"] == "AKIDtest", "q-ak")
    check(auth["q-header-list"] == "host", "q-header-list=host")
    check(len(auth["q-signature"]) == 40, "HMAC-SHA1 签名 40 位")
    check(client._path("a b.jpg") == "/a%20b.jpg", "Key 路径编码")
    url = client.sign_url("prod/output/a.mp4", 60)
    check(url.startswith("https://demo-1250000000.cos.ap-guangzhou.myqcloud.com/"), "签名 URL 域名")

    print("Storage 鸭子类型：")
    cfg = StorageConfig(
        provider="cos", bucket="demo-1250000000", region="ap-guangzhou",
        secret_id="AKIDtest", secret_key="secret", prefix="prod",
    )
    store = CosRestStorage(cfg)
    fake = FakeClient()
    store.client = fake
    key = store.put_bytes(b"hello", "prod/output/a.txt", content_type="text/plain")
    check(key == "prod/output/a.txt", "put_bytes 返回 Key")
    check(fake.puts and fake.puts[0][1] == b"hello", "put_bytes 数据")
    signed = store.sign_url("prod/output/a.txt", 3600)
    check(signed.startswith("https://cdn.example.com/prod/output/a.txt"), "sign_url")

    tmp = tempdir("cosrest-")
    src = tmp / "a.bin"
    src.write_bytes(b"x" * 1024)
    store.put_path(src, "prod/output/b.bin")
    check(fake.puts[-1][0] == "prod/output/b.bin", "put_path Key")
    check(fake.puts[-1][1] == b"x" * 1024, "put_path 数据")

    print("OK COS 兜底层通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
