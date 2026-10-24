#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""存储层 + 转存器 的离线测试：不联网、不花钱、不需要 COS SDK。

    python3 tests/test_storage.py

用一个假的 COS 客户端（模拟 put/head/sign/list/delete 和 5xx 抖动）
验证 Key 规则、幂等、重试、错误映射；再用本机 HTTP 服务验证流式转存。
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app.storage as storage_mod  # noqa: E402
from app.storage import (  # noqa: E402
    CosStorage, LocalStorage, StorageConfig, StorageError, file_fingerprint,
    object_key, open_storage,
)
from app.transfer import (  # noqa: E402
    TransferItem, TransferResult, transfer_many, transfer_url,
)


# ---------------------------------------------------------------------------
# 假 COS 客户端：只实现本项目真正用到的方法
# ---------------------------------------------------------------------------

class FakeServiceError(Exception):
    def __init__(self, status_code: int, error_code: str = "", message: str = ""):
        super().__init__(message or error_code)
        self.status_code = status_code
        self.error_code = error_code
        self.message = message


_UNSET = object()   # 区分「没传 Marker」和「传了 Marker=None」


class FakeCosClient:
    def __init__(self):
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.calls: list[str] = []
        self.fail_next = 0

    def _maybe_fail(self, op: str) -> None:
        self.calls.append(op)
        if self.fail_next > 0:
            self.fail_next -= 1
            raise FakeServiceError(500, "InternalError", "fake 500")

    def put_object(self, Bucket, Key, Body, ContentType="", Metadata=None):  # noqa: N803
        self._maybe_fail("put_object")
        data = Body if isinstance(Body, bytes) else Body.read()
        self.objects[Key] = (data, ContentType)
        return {"ETag": '"fake-etag"'}

    def upload_file(self, Bucket, Key, LocalFilePath, PartSize=1, MAXThread=5,  # noqa: N803
                    EnableMD5=False):
        self._maybe_fail("upload_file")
        self.objects[Key] = (Path(LocalFilePath).read_bytes(), "application/octet-stream")
        return {"ETag": '"fake-etag"'}

    def head_object(self, Bucket, Key):  # noqa: N803
        self._maybe_fail("head_object")
        if Key not in self.objects:
            raise FakeServiceError(404, "NoSuchKey", "The specified key does not exist")
        data, ctype = self.objects[Key]
        return {"Content-Length": str(len(data)), "ETag": '"fake-etag"', "Content-Type": ctype}

    def get_presigned_url(self, Bucket, Key, Expired=3600, Method="GET"):  # noqa: N803
        # 真实 SDK（cos-python-sdk-v5 新版）要求显式传 Method，这里同样强制
        return (f"https://{Bucket}.cos.ap-guangzhou.myqcloud.com/{Key}"
                f"?q-signature=fake&expires={Expired}&method={Method}")

    def delete_object(self, Bucket, Key):  # noqa: N803
        self.objects.pop(Key, None)
        return {}

    def list_objects(self, Bucket, Prefix="", MaxKeys=1000, Marker=_UNSET):  # noqa: N803
        # 真实 SDK 收到 Marker=None 会签出空 query 导致 403，storage 层必须省略该参数
        if Marker is None:
            raise AssertionError("不应把 Marker=None 传给 list_objects")
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        return {"Contents": [{"Key": k} for k in keys[:MaxKeys]], "IsTruncated": "false"}


def cos_config(**overrides) -> StorageConfig:
    base = dict(provider="cos", bucket="demo-1250000000", region="ap-guangzhou",
                secret_id="AKIDfake", secret_key="fakekey")
    base.update(overrides)
    return StorageConfig(**base)


# ---------------------------------------------------------------------------
# HTTP 假服务：正常文件 / 403 过期 / 404 / 200 HTML 错误页
# ---------------------------------------------------------------------------

PAYLOADS: dict[str, bytes] = {}


class Handler(BaseHTTPRequestHandler):
    get_count = 0

    def do_HEAD(self):  # noqa: N802
        self._respond(body=None)

    def do_GET(self):  # noqa: N802
        Handler.get_count += 1
        self._respond(body=PAYLOADS.get(self.path))

    def _respond(self, body: bytes | None):
        path = self.path
        if path.startswith("/expired"):
            payload = (b"<?xml version='1.0'?><Error><Code>AccessDenied</Code>"
                       b"<Message>Request has expired</Message></Error>")
            return self._send(403, "application/xml", payload)
        if path.startswith("/missing"):
            return self._send(404, "text/plain", b"not found")
        if path.startswith("/html"):
            return self._send(200, "text/html", b"<!DOCTYPE html><html>expired</html>")
        if body is None:
            return self._send(404, "text/plain", b"no body")
        self._send(200, "binary/octet-stream", body)

    def _send(self, code: int, ctype: str, payload: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def log_message(self, *args):  # 静音
        pass


def start_server() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


# ---------------------------------------------------------------------------
# 用例
# ---------------------------------------------------------------------------

def test_keys() -> None:
    key = object_key("output", "第 7 套 服装.MP4", batch="2026 10/24A",
                     prefix="prod", digest="9c1b2d4e5f607182",
                     when=__import__("datetime").datetime(2026, 10, 24))
    assert key.isascii(), key
    assert " " not in key, key
    assert key.startswith("prod/output/2026-10-24/"), key
    assert key.endswith(".mp4"), key
    assert "9c1b2d4e" in key, key

    no_ext = object_key("input", "noext", digest="abcdef1234")
    assert no_ext.endswith("noext-abcdef12"), no_ext
    print("  ✓ Key 规则：", key)


def test_fingerprint(tmp: Path) -> None:
    a = tmp / "a.bin"
    b = tmp / "b.bin"
    a.write_bytes(b"same-content" * 100)
    b.write_bytes(b"same-content" * 100)
    assert file_fingerprint(a) == file_fingerprint(b)
    b.write_bytes(b"different" * 100)
    assert file_fingerprint(a) != file_fingerprint(b)
    print("  ✓ 指纹：", file_fingerprint(a))


def test_local_backend(tmp: Path) -> None:
    store = open_storage(StorageConfig(provider="local", root=str(tmp / "store")))
    assert isinstance(store, LocalStorage)
    src = tmp / "x.mp4"
    src.write_bytes(b"video" * 200)

    key = object_key("output", "x.mp4", digest=file_fingerprint(src))
    store.put_path(src, key)
    assert store.exists(key)
    assert store.stat(key)["size"] == 1000
    assert store.sign_url(key).startswith("file://")
    assert store.list_prefix("output") == [key]
    store.delete(key)
    assert not store.exists(key)

    for bad in ("../escape.txt", "a/../../escape.txt", ""):
        try:
            store._path(bad)
        except StorageError:
            pass
        else:  # pragma: no cover
            raise AssertionError(f"越界 Key 未被拦截：{bad!r}")
    print("  ✓ 本地后端：读写/列举/删除/越界拦截")


def test_cos_small(tmp: Path) -> None:
    client = FakeCosClient()
    store = CosStorage(cos_config(), client=client)
    key = "output/2026-10-24/b1/a-11112222.mp4"
    store.put_bytes(b"hello", key, content_type="video/mp4")
    assert client.objects[key][0] == b"hello"
    assert store.exists(key)
    assert store.stat(key)["size"] == 5
    assert "q-signature" in store.sign_url(key, 600)
    assert store.public_url(key).endswith(key)
    assert store.list_prefix("output/") == [key]
    store.delete(key)
    assert not store.exists(key)
    print("  ✓ COS 小对象：put/head/sign/list/delete")


def test_cos_multipart(tmp: Path) -> None:
    client = FakeCosClient()
    store = CosStorage(cos_config(), client=client)
    src = tmp / "big.mp4"
    src.write_bytes(b"x" * 4096)

    original = storage_mod.MULTIPART_MIN
    storage_mod.MULTIPART_MIN = 1024  # 让 4KB 走分片分支
    try:
        key = store.put_path(src, "output/big-1234abcd.mp4")
    finally:
        storage_mod.MULTIPART_MIN = original

    assert "upload_file" in client.calls, client.calls
    assert client.objects[key][0] == b"x" * 4096
    print("  ✓ COS 大文件走分片：", client.calls)


def test_cos_retry(tmp: Path) -> None:
    client = FakeCosClient()
    client.fail_next = 1
    store = CosStorage(cos_config(), client=client)
    store.put_bytes(b"retry-me", "output/r-0000000.mp4")
    assert client.calls.count("put_object") == 2, client.calls
    assert store.exists("output/r-0000000.mp4")
    print("  ✓ 5xx 自动重试一次后成功")


def test_bad_config() -> None:
    for cfg, keyword in (
        (cos_config(bucket="my-bucket"), "APPID"),
        (cos_config(region="gz"), "地域"),
        (cos_config(secret_key=""), "SecretId"),
    ):
        try:
            CosStorage(cfg)
        except StorageError as exc:
            assert keyword in str(exc), (keyword, str(exc))
        else:  # pragma: no cover
            raise AssertionError(f"配置 {cfg} 应该被拒绝")
    print("  ✓ 配置校验：桶名/地域/密钥")


def test_transfer(tmp: Path, base: str, parts: Path) -> None:
    store = open_storage(StorageConfig(provider="local", root=str(tmp / "cos")))
    payload = PAYLOADS["/video/001.mp4"]

    ok = transfer_url(f"{base}/video/001.mp4", store, "", batch="B1", tmp_dir=parts)
    assert ok.ok and ok.size == len(payload), ok
    assert store.exists(ok.key), ok.key
    assert ok.signed_url.startswith("file://")

    again = transfer_url(f"{base}/video/001.mp4", store, ok.key, batch="B1", tmp_dir=parts)
    assert again.skipped and again.key == ok.key, again
    assert not list(parts.glob("*.part")), "临时文件没有清理干净"

    missing = transfer_url(f"{base}/missing/002.mp4", store, tmp_dir=parts)
    assert not missing.ok and "不存在" in missing.error, missing.error

    expired = transfer_url(f"{base}/expired/003.mp4", store, tmp_dir=parts)
    assert not expired.ok and "失效" in expired.error, expired.error

    html = transfer_url(f"{base}/html/004.mp4", store, tmp_dir=parts)
    assert not html.ok and ("错误页" in html.error or "过期" in html.error), html.error

    results = transfer_many([
        TransferItem(f"{base}/video/001.mp4", batch="B2", prefix="output"),
        TransferItem(f"{base}/video/002.mp4", batch="B2", prefix="output"),
        TransferItem(f"{base}/missing/003.mp4", batch="B2"),
    ], store, workers=3, tmp_dir=parts)
    assert [r.status for r in results] == ["ok", "ok", "failed"], [r.status for r in results]
    for r in results[:2]:
        assert store.exists(r.key), r.key
    print("  ✓ 转存：成功/跳过/404/403过期/HTML错误页/批量")


def make_temp_dir(parent: Path, prefix: str) -> Path:
    """不用 tempfile.mkdtemp：部分受限环境会给它造的目录打上不可写 ACL。"""
    for _ in range(10):
        path = parent / f"{prefix}{uuid.uuid4().hex[:8]}"
        try:
            path.mkdir(parents=True)
            return path
        except FileExistsError:
            continue
    raise RuntimeError("无法创建临时目录")


def main() -> int:
    work = ROOT / ".tmp"
    work.mkdir(exist_ok=True)
    tmp = make_temp_dir(work, "storage-test-")
    PAYLOADS["/video/001.mp4"] = bytes(range(256)) * 600      # 150KB
    PAYLOADS["/video/002.mp4"] = os.urandom(300 * 1024)       # 300KB，跨多个 chunk
    server, base = start_server()
    try:
        print("存储层测试：")
        test_keys()
        test_fingerprint(tmp)
        test_local_backend(tmp)
        test_cos_small(tmp)
        test_cos_multipart(tmp)
        test_cos_retry(tmp)
        test_bad_config()
        print("转存器测试：")
        test_transfer(tmp, base, tmp / "parts")
        print("\nOK 全部通过（未联网、未使用真实 COS）")
        return 0
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
