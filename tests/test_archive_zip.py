#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""云端归档「打包下载 ZIP」的离线测试。

不联网、不起外部服务、不产生任何云端费用：把 WebApplication 的成品桶换成一个
内存里的假 COS，验证 key 清洗 / 后台打包 / 进度 / 单条失败跳过 / 权限 / HTTP 全链路。
"""
from __future__ import annotations

import base64
import http.cookiejar
import io
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from _tmpdir import tempdir  # noqa: E402
sys.path.insert(0, str(ROOT))

from app.storage import StorageError  # noqa: E402
from app.web.archive_zip import ArchiveZipError, ArchiveZipManager, normalize_keys  # noqa: E402
from app.web.auth import make_password  # noqa: E402
from app.web.server import WebApplication, make_handler  # noqa: E402
from app.web.settings import load_settings  # noqa: E402


def make_settings(tmp: Path):
    secret = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
    web_file = tmp / "web.json"
    web_file.write_text(json.dumps({
        "session_secret": secret, "session_hours": 2,
        "data_root": str(tmp / "web-data"),
        "max_active_jobs": 4, "max_active_per_user": 2, "max_job_items": 20,
        "users": {
            "alice": {**make_password("alice-pass-123"), "role": "user", "enabled": True, "version": 1},
            "admin": {**make_password("admin-pass-123"), "role": "admin", "enabled": True, "version": 1},
        },
    }), encoding="utf-8")
    api_file = tmp / "config.json"
    api_file.write_text(json.dumps({
        "wan": {"provider": "dashscope", "api_key": "mock-api-key-123456789",
                "model": "wan3.0-video", "base_url": "http://127.0.0.1:9/api/v1",
                "default_mode": "edit", "poll_interval": 0.2, "poll_timeout": 30,
                "max_concurrency": 4, "query_qps": 50,
                "price_per_second": {"480P": None, "720P": None, "1080P": None}},
        "manifest_url": "", "cos": {"enabled": False},
    }), encoding="utf-8")
    return load_settings(base_dir=tmp, web_file=web_file, api_file=api_file, load_templates=False)


class FakeStore:
    """内存版成品桶：只有打包要用的 read_stream / sign_url / list_prefix。"""

    def __init__(self, objects: dict[str, bytes], broken: set[str] | None = None):
        self.objects = dict(objects)
        self.broken = set(broken or ())
        self.reads: list[str] = []

    def read_stream(self, key: str):
        self.reads.append(key)
        if key in self.broken:
            raise StorageError(f"模拟取回失败：{key}")
        if key not in self.objects:
            raise StorageError(f"对象不存在：{key}")
        return io.BytesIO(self.objects[key])

    def sign_url(self, key: str, expires: int = 0) -> str:
        return f"https://cos.example.com/{key}?sign=1"

    def list_prefix(self, prefix: str, limit: int = 1000):
        return [{"key": key, "size": len(data)} for key, data in self.objects.items()
                if key.startswith(prefix)]


def wait_ready(manager: ArchiveZipManager, token: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    info = manager.status(token)
    while info and info["state"] == "building" and time.time() < deadline:
        time.sleep(0.05)
        info = manager.status(token)
    assert info, "任务丢了"
    assert info["state"] != "building", f"打包超时：{info}"
    return info


def call(opener, base, path, method="GET", payload=None):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with opener.open(req, timeout=30) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def main() -> int:
    tmp = tempdir("archivezip-")
    settings = make_settings(tmp)

    # ---------- key 清洗（纯函数，先立规矩）
    assert normalize_keys(["a/b/c.mp4", "a/b/c.mp4", "", None, "/d/e.mp4"]) == ["a/b/c.mp4", "d/e.mp4"]
    assert normalize_keys(["../etc/passwd", "a/../b.mp4", "a//b.mp4"]) == []
    assert normalize_keys("not-a-list") == []
    print("  ✓ key 清洗：去重 / 去空 / 挡路径穿越")

    # ---------- 打包本身（直接调 manager，不起 HTTP）
    keys = ["clean/output/AX111/B001/141-aaa.mp4",
            "clean/output/AX111/B001/549-bbb.mp4",
            "clean/output/AX111/B002/141-aaa.mp4"]   # 与第一条同名，必须改名不能覆盖
    objects = {key: f"video:{index}".encode("utf-8") for index, key in enumerate(keys)}
    store = FakeStore(objects)
    manager = ArchiveZipManager(Path(settings.data_dir))

    started = manager.start(store, keys, "AX111")
    assert started["total"] == 3 and started["state"] in ("building", "ready"), started
    info = wait_ready(manager, started["token"])
    assert info["state"] == "ready" and info["done"] == 3, info
    assert info["bytes"] == sum(len(v) for v in objects.values()), info
    assert info["download_url"].endswith(started["token"]), info

    zip_path = manager.path_of(started["token"])
    assert zip_path and zip_path.is_file(), "打包文件没落盘"
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        assert len(names) == 3 and len(set(names)) == 3, names
        assert "141-aaa.mp4" in names, names
        assert "B002/141-aaa.mp4" in names, names
        assert zf.read("141-aaa.mp4") == objects[keys[0]], "同名文件被后一条覆盖了"
        assert zf.read("B002/141-aaa.mp4") == objects[keys[2]], names
    assert manager.zip_name(started["token"]).startswith("AX111-3条-"), manager.zip_name(started["token"])
    print("  ✓ 打包：3 条全进包，同名文件按父目录区分，文件名带款号与条数")

    # 同一批连点：正在跑的任务被复用，不重复拉 COS（用闸门把第一条卡住，制造“打包中”）
    class GatedStore(FakeStore):
        def __init__(self, objects, gate):
            super().__init__(objects)
            self.gate = gate

        def read_stream(self, key):
            self.gate.wait(10)
            return super().read_stream(key)

    gate = threading.Event()
    gated = GatedStore(objects, gate)
    first = manager.start(gated, keys, "AX111")
    second = manager.start(gated, keys, "AX111")
    assert second["token"] == first["token"], "同一批重复点击应复用正在跑的任务"
    gate.set()
    assert wait_ready(manager, first["token"])["state"] == "ready"
    print("  ✓ 幂等：同一批重复点击复用同一个打包任务")

    # ---------- 单条失败：其余照常打包，失败项单独记录
    broken_keys = ["clean/output/AX222/B001/ok-1.mp4", "clean/output/AX222/B001/bad.mp4"]
    broken_store = FakeStore({broken_keys[0]: b"ok-video"}, broken={broken_keys[1]})
    part = manager.start(broken_store, broken_keys, "AX222")
    part_info = wait_ready(manager, part["token"])
    assert part_info["state"] == "ready" and part_info["done"] == 1, part_info
    assert [item["key"] for item in part_info["failed"]] == [broken_keys[1]], part_info
    with zipfile.ZipFile(manager.path_of(part["token"])) as zf:
        assert zf.namelist() == ["ok-1.mp4"], zf.namelist()
    print("  ✓ 容错：坏一条不影响整包，失败项带回前端")

    # ---------- 全失败：不产出空 zip
    dead = manager.start(FakeStore({}, broken={"clean/output/AX333/x.mp4"}),
                         ["clean/output/AX333/x.mp4"], "AX333")
    dead_info = wait_ready(manager, dead["token"])
    assert dead_info["state"] == "failed" and dead_info["error"], dead_info
    assert not manager.path_of(dead["token"]), "全失败不该留下空包"
    print("  ✓ 全失败：状态 failed 且不留空 zip")

    # 空 key / 未启用存储：直接报错，不起线程
    for bad in ([], [""], ["../x"]):
        try:
            manager.start(store, bad)
        except ArchiveZipError as exc:
            assert exc.status == 400, exc
        else:
            raise AssertionError(f"空 key 应当报错：{bad}")
    try:
        manager.start(None, keys)
    except ArchiveZipError as exc:
        assert exc.status == 503, exc
    else:
        raise AssertionError("未启用 COS 应当 503")
    print("  ✓ 校验：空勾选 400 / 云端未启用 503")

    # 过期清理：12 小时前的包会被扫掉
    stale = manager.root / "archive-stale.zip"
    stale.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    old = time.time() - 13 * 3600
    os.utime(stale, (old, old))
    manager.sweep()
    assert not stale.exists(), "过期 zip 没被清理"
    print("  ✓ 清理：超过 12 小时的成品包在下次打包时被扫掉")

    # ---------- HTTP 全链路
    app = WebApplication(settings)
    app._output_store = store           # 用假桶顶掉真实 COS，避免联网
    server = None
    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        admin = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        alice = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        assert call(admin, base, "/api/login", "POST",
                    {"username": "admin", "password": "admin-pass-123"})[0] == 200
        assert call(alice, base, "/api/login", "POST",
                    {"username": "alice", "password": "alice-pass-123"})[0] == 200

        code, _, raw = call(admin, base, "/api/admin/archive-zip", "POST", {"keys": keys, "style": "AX111"})
        assert code == 200, (code, raw[:200])
        token = json.loads(raw.decode("utf-8"))["token"]

        deadline = time.time() + 20
        info = {"state": "building"}
        while info["state"] == "building" and time.time() < deadline:
            time.sleep(0.05)
            code, headers, raw = call(admin, base, f"/api/admin/archive-zip/{token}/status")
            assert code == 200 and "json" in headers.get("Content-Type", ""), (code, headers)
            info = json.loads(raw.decode("utf-8"))
        assert info["state"] == "ready", info

        code, headers, raw = call(admin, base, f"/api/admin/archive-zip/{token}")
        assert code == 200 and raw[:2] == b"PK", (code, raw[:60])
        assert "zip" in headers.get("Content-Type", ""), headers
        assert ".zip" in headers.get("Content-Disposition", ""), headers
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            assert len(zf.namelist()) == 3, zf.namelist()

        assert call(alice, base, "/api/admin/archive-zip", "POST", {"keys": keys})[0] == 403
        assert call(alice, base, f"/api/admin/archive-zip/{token}")[0] == 403
        assert call(admin, base, "/api/admin/archive-zip", "POST", {"keys": []})[0] == 400
        assert call(admin, base, "/api/admin/archive-zip/nope")[0] == 404
        assert call(admin, base, "/api/admin/archive-zip/nope/whatever")[0] == 404
        print("  ✓ HTTP：建任务 → 轮询 → 下载 zip；跨账户 403 / 空勾选 400 / 未知 token 404")
    finally:
        if server:
            server.shutdown()
            server.server_close()
        app.jobs.shutdown()
    print("  OK 云端归档打包下载：清洗 / 打包 / 容错 / 幂等 / 清理 / 权限 / HTTP 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
