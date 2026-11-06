#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按批次整批下载：多轮收集 / 非试跑资格 / 权限 / 路径穿越 / probe 的离线测试。

不联网、不起 HTTP 服务、不产生任何云端费用。直接构造 job 与批次文件，
在临时目录里放假 mp4，验证打包清单与 zip 内容。
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

from app.web.auth import make_password  # noqa: E402
from app.web.batches import BatchError  # noqa: E402
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
            "bob": {**make_password("bob-pass-12345"), "role": "user", "enabled": True, "version": 1},
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


def write_job(settings, manager, job_id, owner, records, *, out_root: Path,
              lane="big", batch_ref="", created_at=None):
    """注册一个已完成的 job，并为每条成功记录写一个假 mp4。"""
    out = out_root / job_id
    out.mkdir(parents=True, exist_ok=True)
    for rec in records:
        if rec["status"] == "success":
            target = out / f"{rec['id']}.mp4"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(f"fake-mp4:{job_id}:{rec['id']}".encode("utf-8"))
    job = {
        "id": job_id, "owner": owner, "status": "succeeded",
        "created_at": time.time() if created_at is None else created_at,
        "out_dir": str(out), "batch_ref": batch_ref, "lane": lane,
        "records": [{"id": rec["id"], "status": rec["status"]} for rec in records],
        "template_ids": [rec["id"] for rec in records],
    }
    path = Path(settings.data_dir) / "jobs" / f"{job_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    manager.jobs[job_id] = job
    return job


def make_batch(batches, batch_id, owner, *, runs=(), items=(), batch_no="AW26-001",
               state="locked"):
    batch = {
        "id": batch_id, "batch_no": batch_no, "owner": owner, "state": state,
        "items": list(items), "runs": list(runs), "active_job_id": "",
        "created_at": time.time(), "updated_at": time.time(),
    }
    batches.batches[batch_id] = batch
    return batch


def call(opener, base, path, method="GET", payload=None):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    try:
        with opener.open(req, timeout=20) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def expect_error(fn, status, what):
    try:
        fn()
    except BatchError as exc:
        assert exc.status == status, (what, exc.status, exc)
    else:
        raise AssertionError(f"{what}：应当抛出 {status}")


def main() -> int:
    tmp = tempdir("batchzip-")
    settings = make_settings(tmp)
    out_root = Path(settings.data_dir) / "output"
    # 用真实的 WebApplication：离线断言和后面的 HTTP 断言共用同一套内存数据。
    app = WebApplication(settings)
    manager, batches = app.jobs, app.batches
    server = None
    try:
        alice = {"username": "alice", "role": "user"}
        bob = {"username": "bob", "role": "user"}
        admin = {"username": "admin", "role": "admin"}

        # 两轮生产：第一轮 001 成功 / 002 失败，第二轮重试 002 并新增 003。
        # items 只留最后一轮 job_id —— 只扫 items 会漏掉第一轮的 001。
        write_job(settings, manager, "job1", "alice",
                  [{"id": "001", "status": "success"}, {"id": "002", "status": "failed"}],
                  out_root=out_root, batch_ref="B1", created_at=time.time() - 200)
        write_job(settings, manager, "job2", "alice",
                  [{"id": "002", "status": "success"}, {"id": "003", "status": "success"}],
                  out_root=out_root, batch_ref="B1", created_at=time.time() - 100)
        # 万一有试跑 job 被挂到同一个 batch_ref，也不能进整批包。
        write_job(settings, manager, "jobSmall", "alice",
                  [{"id": "004", "status": "success"}],
                  out_root=out_root, batch_ref="B1", lane="small")
        # 路径穿越：文件真实存在，但场景 id 含斜杠，必须被挡在 zip 之外。
        write_job(settings, manager, "jobTrap", "alice",
                  [{"id": "sub/evil", "status": "success"}],
                  out_root=out_root, batch_ref="B1")
        make_batch(batches, "B1", "alice",
                   runs=[{"job_id": "job1"}, {"job_id": "job2"},
                         {"job_id": "jobSmall"}, {"job_id": "jobTrap"}],
                   items=[{"template_id": "001", "state": "done", "job_id": "job1"},
                          {"template_id": "002", "state": "done", "job_id": "job2"},
                          {"template_id": "003", "state": "done", "job_id": "job2"}])

        info = batches.batch_videos("B1", alice)
        names = [name for name, _ in info["files"]]
        assert names == ["001.mp4", "002.mp4", "003.mp4"], names
        assert info["batch_no"] == "AW26-001" and info["id"] == "B1", info
        by_name = dict(info["files"])
        assert by_name["001.mp4"].parent.name == "job1", "第一轮成功的视频不能漏"
        assert by_name["002.mp4"].parent.name == "job2", "同场景要保留最新一次成功"
        assert not any("/" in name or ".." in name for name in names), names
        print("  ✓ 完整收集：多轮成功视频都进包，试跑与路径穿越记录被排除")

        # probe 只读不写盘：前端先拿 count，再触发真正的下载。
        tmp_dir = Path(settings.data_dir) / "tmp"
        before = sorted(p.name for p in tmp_dir.glob("*.zip")) if tmp_dir.is_dir() else []
        again = batches.batch_videos("B1", admin)
        assert len(again["files"]) == 3, again
        after = sorted(p.name for p in tmp_dir.glob("*.zip")) if tmp_dir.is_dir() else []
        assert before == after, "probe 不能生成 zip 文件"
        print("  ✓ probe：重复查询返回同样数量且不落盘，管理员可读")

        # 真正打包：zip 内三个文件、内容与各自 job 对应。
        zip_path = batches.zip_path("B1", alice, info["files"])
        try:
            with zipfile.ZipFile(zip_path) as zf:
                assert sorted(zf.namelist()) == names, zf.namelist()
                assert zf.read("001.mp4") == b"fake-mp4:job1:001"
                assert zf.read("002.mp4") == b"fake-mp4:job2:002"
        finally:
            zip_path.unlink(missing_ok=True)
        print("  ✓ 打包：zip 内文件名与内容逐条对得上")

        # 只挂试跑 job 的「批次」：明确拒绝，不产出空包。
        write_job(settings, manager, "jobOnlySmall", "alice",
                  [{"id": "005", "status": "success"}],
                  out_root=out_root, batch_ref="B2", lane="small")
        make_batch(batches, "B2", "alice", runs=[{"job_id": "jobOnlySmall"}],
                   items=[{"template_id": "005", "state": "done", "job_id": "jobOnlySmall"}])
        expect_error(lambda: batches.batch_videos("B2", alice), 403, "纯试跑批次")
        expect_error(lambda: batches.batch_videos("B2", admin), 403, "管理员也不能整批下载试跑")
        print("  ✓ 资格：只挂试跑 job 的批次 403，管理员同样拒绝")

        # 草稿/空批次：没有视频，不生成空 zip。
        make_batch(batches, "B3", "alice", state="draft")
        assert batches.batch_videos("B3", alice)["files"] == []
        expect_error(lambda: batches.zip_path("B3", alice), 404, "空批次打包")
        print("  ✓ 空批次：返回 0 个文件，打包报 404 而非空 zip")

        # 权限：非 owner 不能打包别人的批次；不存在的批次 404。
        expect_error(lambda: batches.batch_videos("B1", bob), 403, "跨账户")
        expect_error(lambda: batches.batch_videos("nope", alice), 404, "不存在的批次")
        print("  ✓ 权限：跨账户 403 / 不存在 404")

        # 老批次没有 runs，只靠 items 里的 job_id 也要能收齐。
        write_job(settings, manager, "jobOld", "alice",
                  [{"id": "006", "status": "success"}],
                  out_root=out_root, batch_ref="B4")
        make_batch(batches, "B4", "alice", runs=[],
                   items=[{"template_id": "006", "state": "done", "job_id": "jobOld"}])
        assert [n for n, _ in batches.batch_videos("B4", alice)["files"]] == ["006.mp4"]
        print("  ✓ 兼容：老批次无 runs 时按 items / batch_ref 兜底")

        # ---------- HTTP：probe / 真实 zip / 权限 / 试跑拒绝
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        alice_op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        bob_op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        assert call(alice_op, base, "/api/login", "POST",
                    {"username": "alice", "password": "alice-pass-123"})[0] == 200
        assert call(bob_op, base, "/api/login", "POST",
                    {"username": "bob", "password": "bob-pass-12345"})[0] == 200

        code, probe_headers, raw = call(alice_op, base, "/api/batches/B1/files.zip?probe=1")
        probe = json.loads(raw.decode("utf-8"))
        assert code == 200 and probe["count"] == 3, (code, probe)
        assert probe["download_url"] == "/api/batches/B1/files.zip", probe
        assert probe["batch_no"] == "AW26-001", probe
        assert "json" in probe_headers.get("Content-Type", ""), probe_headers

        code, zip_headers, raw = call(alice_op, base, "/api/batches/B1/files.zip")
        assert code == 200 and raw[:2] == b"PK", (code, raw[:80])
        assert "zip" in zip_headers.get("Content-Type", ""), zip_headers
        assert ".zip" in zip_headers.get("Content-Disposition", ""), zip_headers
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            assert sorted(zf.namelist()) == ["001.mp4", "002.mp4", "003.mp4"], zf.namelist()

        assert call(bob_op, base, "/api/batches/B1/files.zip?probe=1")[0] == 403, "跨账户"
        assert call(alice_op, base, "/api/batches/B2/files.zip?probe=1")[0] == 403, "纯试跑批次"
        assert call(alice_op, base, "/api/batches/B3/files.zip")[0] == 404, "空批次"
        print("  ✓ HTTP：probe 返回数量 / zip 可下载 / 跨账户 403 / 纯试跑 403 / 空批次 404")
    finally:
        if server:
            server.shutdown()
            server.server_close()
        app.jobs.shutdown()
    print("  OK 按批次整批下载：完整收集 / 非试跑 / 权限 / 路径穿越 / probe / HTTP 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
