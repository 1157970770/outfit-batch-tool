#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第四批回归测试：协议面与并发鲁棒性（P1-11 ~ P1-18）。

离线跑，不联网、不调模型。覆盖：
1. 登录限流：IP 桶与账号桶分开，换用户名绕不过 IP 桶；桶数有上限（随机 key 不会刷爆内存）。
2. `Transfer-Encoding: chunked` 一律 411，而不是当成空 body 猜读。
3. 鉴权失败的请求直接关连接（不把请求体残留留给下一个请求）。
4. `Handler.timeout` / `LocalHTTPServer` 的并发与属性必须写在类上（写在构造之后是死代码）。
5. `persist_web` 并发写不会互相截断，也不留临时文件。
6. 任务 zip / 批次 zip 每次请求文件名唯一（并发下载不再互相 truncate）。
7. `wait_task(should_stop=...)`：取消能在秒级生效，而不是陪跑到 1800s。
8. 停止等待的记录落在 `unknown`（不可重试），不会被「重试失败项」二次计费。
9. 取消排队中的任务会同时置 `cancel_requested`，工作线程不会把它改回 running。
"""
from __future__ import annotations

import base64
import json
import os
import socket
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _tmpdir import tempdir  # noqa: E402

from app.core import BatchRunner, BatchSpec, BatchState  # noqa: E402
from app.wan_api import (STATUS_RUNNING, STATUS_SUCCEEDED, SubmitResult,  # noqa: E402
                         TaskResult, WaitStopped, WanClient, WanConfig)
from app.web.auth import LoginLimiter, make_password  # noqa: E402


def check(cond: bool, message: str) -> None:
    if not cond:
        raise AssertionError(message)


# ---------------------------------------------------------------------------
# 1. 登录限流分桶
# ---------------------------------------------------------------------------

def test_limiter_ip_bucket_blocks_username_rotation():
    limiter = LoginLimiter(limit=3, window=600)
    ip = "ip|10.0.0.9"
    for i in range(3):
        limiter.failed(ip)
        limiter.failed(f"user|attacker{i}")     # 每次都换用户名
    check(limiter.allowed(ip, limit=3) is False, "IP 桶打满后必须挡住，换用户名不能绕过")
    check(limiter.allowed("user|attacker99") is True, "换用户名当然是新账号桶，这不冲突")


def test_limiter_is_bounded():
    limiter = LoginLimiter(limit=3, window=600, max_keys=64)
    for i in range(500):
        limiter.failed(f"u{i}")
    check(len(limiter.failures) <= 64, f"桶数必须有上限，实际 {len(limiter.failures)}")
    check(len(limiter.order) <= 64, "淘汰索引也要跟着收缩")


def test_limiter_drops_empty_bucket():
    limiter = LoginLimiter(limit=3, window=0.05)
    limiter.failed("k1")
    time.sleep(0.12)
    check(limiter.allowed("k1") is True, "窗口过期后应放行")
    check("k1" not in limiter.failures, "过期空桶要删掉，否则随机 key 会长期占内存")


# ---------------------------------------------------------------------------
# 2~5. 真起 HTTP 服务
# ---------------------------------------------------------------------------

def build_app(tmp: Path):
    from app.web.server import WebApplication
    from app.web.settings import load_settings

    secret = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
    web_config = tmp / "web.json"
    web_config.write_text(json.dumps({
        "session_secret": secret, "session_hours": 1, "data_root": str(tmp / "web-data"),
        "max_active_jobs": 1, "max_job_items": 5,
        "users": {
            "alice": {**make_password("alice-pass-123"), "role": "user", "enabled": True, "version": 1},
        },
    }, ensure_ascii=False), encoding="utf-8")
    api_config = tmp / "config.json"
    api_config.write_text(json.dumps({
        "wan": {"provider": "dashscope", "api_key": "mock-api-key-123456789",
                "model": "wan3.0-video", "base_url": "http://127.0.0.1:1/api/v1",
                "default_mode": "edit", "poll_interval": 1, "poll_timeout": 5,
                "max_concurrency": 1},
        "manifest_url": str(tmp / "manifest.json"),
    }, ensure_ascii=False), encoding="utf-8")
    (tmp / "manifest.json").write_text("[]", encoding="utf-8")
    settings = load_settings(base_dir=tmp, web_file=web_config, api_file=api_config,
                             load_templates=True)
    return WebApplication(settings)


def start_server(app):
    from http.server import ThreadingHTTPServer
    from app.web.server import make_handler
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def raw_exchange(port: int, request: bytes, timeout: float = 8.0) -> tuple[bytes, bool]:
    """返回 (响应字节, 服务端是否主动关闭了连接)。"""
    chunks: list[bytes] = []
    closed = False
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.sendall(request)
        sock.settimeout(timeout)
        while True:
            try:
                piece = sock.recv(8192)
            except socket.timeout:
                break
            if not piece:
                closed = True
                break
            chunks.append(piece)
    return b"".join(chunks), closed


def test_chunked_body_rejected_with_411(tmp: Path):
    app = build_app(tmp)
    server = start_server(app)
    port = server.server_address[1]
    try:
        request = (
            "POST /api/login HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            "Content-Type: application/json\r\n"
            "Transfer-Encoding: chunked\r\n"
            "\r\n"
            "5\r\nhello\r\n0\r\n\r\n"
        ).encode("ascii")
        response, _closed = raw_exchange(port, request)
        head = response.split(b"\r\n", 1)[0].decode("latin-1")
        check("411" in head, f"chunked 必须回 411，实际：{head}")
        check(b"chunked" in response, "错误信息里要点明不支持 chunked")
    finally:
        server.shutdown()
        server.server_close()


def test_auth_failure_closes_connection(tmp: Path):
    app = build_app(tmp)
    server = start_server(app)
    port = server.server_address[1]
    try:
        body = json.dumps({"count": 1}).encode("utf-8")
        request = (
            "POST /api/jobs HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            "\r\n"
        ).encode("ascii") + body
        response, closed = raw_exchange(port, request)
        text = response.decode("latin-1")
        check(" 401 " in text.split("\r\n", 1)[0], f"未登录应回 401，实际：{text.splitlines()[0]}")
        check(closed,
              "鉴权失败必须关连接：否则没读的请求体会被当成下一个请求解析（keep-alive 串包）")
        check(text.count("HTTP/1.1 ") == 1, f"只应回一个响应，实际：{text.count('HTTP/1.1 ')} 个")
    finally:
        server.shutdown()
        server.server_close()


def test_ip_bucket_applies_to_known_account(tmp: Path):
    app = build_app(tmp)
    server = start_server(app)
    port = server.server_address[1]
    try:
        from app.web.server import LOGIN_IP_LIMIT
        for _ in range(LOGIN_IP_LIMIT):
            app.limiter.failed("ip|127.0.0.1")
        request = (
            "POST /api/login HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: 48\r\n"
            "\r\n"
            '{"username":"alice","password":"alice-pass-123"}'
        ).encode("ascii")
        response, _closed = raw_exchange(port, request)
        head = response.split(b"\r\n", 1)[0].decode("latin-1")
        check("429" in head, f"IP 桶打满后即使密码正确也应 429，实际：{head}")
    finally:
        server.shutdown()
        server.server_close()


def test_handler_attributes_are_class_level(tmp: Path):
    from app.web.server import LocalHTTPServer, make_handler
    app = build_app(tmp)
    handler = make_handler(app)
    check(handler.timeout == 30, "Handler.timeout 必须是类属性，StreamRequestHandler 才会拿它设 socket 超时")
    check(LocalHTTPServer.daemon_threads is True, "daemon_threads 写在类上才生效")
    check(LocalHTTPServer.allow_reuse_address is True,
          "allow_reuse_address 必须作为类属性声明（构造后再赋值是死代码）")
    check(LocalHTTPServer.request_queue_size == 16, "监听队列长度要在类上")

    srv = LocalHTTPServer(("127.0.0.1", 0), handler, max_connections=2)
    try:
        check(srv._slots._value == 2, "连接上限应通过信号量体现")
    finally:
        srv.server_close()


def test_persist_web_concurrent(tmp: Path):
    app = build_app(tmp)
    path = Path(app.settings.web_file)
    errors: list[BaseException] = []
    stop = threading.Event()

    def worker(idx: int) -> None:
        try:
            for n in range(25):
                # 改内存与落盘在同一把锁里，模拟管理端的真实写入方式
                with app._web_lock:
                    app.settings.web["probe_%d" % idx] = n
                    app.persist_web()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            stop.set()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    check(not errors, f"并发写 web.json 不应抛异常：{errors[:2]!r}")
    text = path.read_text(encoding="utf-8")
    data = json.loads(text)                      # 内容必须是完整 JSON
    check(data.get("users", {}).get("alice"), "账号不能被并发写弄丢")
    leftovers = [p.name for p in path.parent.glob(path.name + ".*.tmp")]
    check(leftovers == [], f"临时文件必须清理干净，残留：{leftovers}")


# ---------------------------------------------------------------------------
# 6. zip 临时文件名唯一
# ---------------------------------------------------------------------------

def test_zip_paths_are_unique(tmp: Path):
    from app.web.server import temp_zip_path
    a = temp_zip_path(tmp / "web-data", "job-abc")
    b = temp_zip_path(tmp / "web-data", "job-abc")
    check(a != b, "同一任务两次打包不能落到同一个路径上（并发会互相 truncate）")
    check(a.parent.is_dir(), "tmp 目录应自动创建")
    check(not a.exists(), "只应给出路径，不应提前建出文件")
    # 批次 zip 用 `batch-<id>-<uuid>.zip`，同一批次两次必须不同
    from app.web import batches as batches_mod
    import inspect
    source = inspect.getsource(batches_mod.BatchManager.zip_path)
    check("uuid.uuid4()" in source, "批次 zip 文件名必须每次请求唯一")


# ---------------------------------------------------------------------------
# 7~8. 取消能打断轮询
# ---------------------------------------------------------------------------

def test_wait_task_stop_is_prompt():
    client = WanClient(WanConfig(provider="dashscope", api_key="sk-test",
                                 base_url="http://127.0.0.1:1/api/v1"))
    calls = {"n": 0}

    def get_task(task_id: str) -> TaskResult:
        calls["n"] += 1
        return TaskResult(task_id=task_id, status=STATUS_RUNNING)

    client.get_task = get_task          # type: ignore[method-assign]
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    started = time.monotonic()
    try:
        client.wait_task("task-1", timeout=120, interval=15, should_stop=stop.is_set)
    except WaitStopped as exc:
        elapsed = time.monotonic() - started
        check("task-1" in str(exc), "停止异常里要带 task_id，便于人工核对")
        check(elapsed < 3.0, f"取消应在秒级生效，实际等了 {elapsed:.1f}s（interval=15）")
    else:
        raise AssertionError("should_stop 命中后应抛 WaitStopped，而不是继续等")


def test_wait_task_terminal_still_returns():
    client = WanClient(WanConfig(provider="dashscope", api_key="sk-test",
                                 base_url="http://127.0.0.1:1/api/v1"))
    client.get_task = lambda tid: TaskResult(  # type: ignore[method-assign]
        task_id=tid, status=STATUS_SUCCEEDED, video_url="http://example.com/a.mp4")
    result = client.wait_task("task-2", timeout=5, interval=0.05,
                              should_stop=lambda: False)
    check(result.status == STATUS_SUCCEEDED, "停止回调为 False 时行为不能变")


class _StopClient:
    """提交成功、但一进入轮询就"被取消"的客户端。"""

    def build_body(self, task, defaults=None, *, default_mode=None,
                   probe=None, video_durations=None):
        return {"prompt": task["prompt"]}, [], []

    def wire_body(self, body):
        return body

    def submit(self, wire):
        return SubmitResult(task_id="task-stop", status="PENDING", request_id="req-stop")

    def wait_task(self, task_id, *, timeout=None, interval=None, on_status=None,
                  should_stop=None):
        raise WaitStopped(task_id)


def make_runner(tmp: Path, client) -> BatchRunner:
    spec = BatchSpec(
        batch_id="stop-1", image_path="", prompt="换装", mode="edit",
        out_dir=tmp / "out", templates=[], api_key="sk-test",
        resolution="480P", duration=7, price_per_second={"480P": 0.3},
    )
    spec.out_dir.mkdir(parents=True, exist_ok=True)
    runner = object.__new__(BatchRunner)
    runner.spec = spec
    runner.on_event = lambda evt: None
    runner.log = lambda msg: None
    runner.pause_event = threading.Event()
    runner.pause_event.set()
    runner.stop_event = threading.Event()
    runner.state = BatchState(spec)
    runner.state.started = ""
    runner._image_url = "http://example.com/image.jpg"
    runner._upload_lock = threading.Lock()
    runner._save_lock = threading.Lock()
    runner._media_cache = {}
    runner._uploader = None
    runner._client = client
    return runner


def test_stopped_record_is_not_retryable(tmp: Path):
    from app.web.jobs import _RETRYABLE_RECORD_STATES
    runner = make_runner(tmp, _StopClient())
    rec = runner._run_one({"id": "T1", "url": "http://example.com/v.mp4", "duration": 1.5})
    check(rec["status"] == "unknown", f"停止等待的记录应是 unknown，实际 {rec['status']}")
    check(rec.get("stopped") is True, "要标出这是我们主动停止的")
    check(rec.get("task_id") == "task-stop", "必须保留 taskId，否则没法去控制台核对")
    check("unknown" not in _RETRYABLE_RECORD_STATES,
          "停止等待不能进可重试集合：云端任务还在跑，重试就是二次计费")


# ---------------------------------------------------------------------------
# 9. queued 取消
# ---------------------------------------------------------------------------

def test_queued_cancel_sets_requested(tmp: Path):
    from app.web.jobs import JobManager
    settings = SimpleNamespace(data_dir=tmp, max_active_jobs=1,
                               max_active_small_jobs=1, max_active_per_user=1)
    manager = JobManager(settings, {})
    manager.stop_event.set()
    with manager.changed:
        manager.changed.notify_all()
    manager.dispatcher.join(timeout=3)
    try:
        manager.jobs["q1"] = {"id": "q1", "status": "queued", "owner": "u1", "lane": "big",
                              "created_at": 1.0, "batch_id": "B-q1", "total": 1, "done": 0,
                              "records": [], "templates": []}
        check(manager.cancel("q1") is True, "排队中的任务应能取消")
        job = manager.jobs["q1"]
        check(job["status"] == "canceled", "状态要置成 canceled")
        check(job.get("cancel_requested") is True,
              "必须同时置 cancel_requested：派发线程可能已经起了线程，只改 status 不够")
    finally:
        manager.shutdown()


def main() -> int:
    tmp = tempdir("hardening-")
    cases = [
        ("登录限流·IP 桶挡换用户名", test_limiter_ip_bucket_blocks_username_rotation, ()),
        ("登录限流·桶数有上限", test_limiter_is_bounded, ()),
        ("登录限流·空桶回收", test_limiter_drops_empty_bucket, ()),
        ("协议·chunked 回 411", test_chunked_body_rejected_with_411, (tmp,)),
        ("协议·鉴权失败关连接", test_auth_failure_closes_connection, (tmp,)),
        ("协议·IP 桶对已知账号也生效", test_ip_bucket_applies_to_known_account, (tmp,)),
        ("协议·类属性与并发上限", test_handler_attributes_are_class_level, (tmp,)),
        ("并发·web.json 并发写", test_persist_web_concurrent, (tmp,)),
        ("并发·zip 临时名唯一", test_zip_paths_are_unique, (tmp,)),
        ("取消·轮询秒级退出", test_wait_task_stop_is_prompt, ()),
        ("取消·终态行为不变", test_wait_task_terminal_still_returns, ()),
        ("取消·停止记录不可重试", test_stopped_record_is_not_retryable, (tmp,)),
        ("取消·排队任务置标记", test_queued_cancel_sets_requested, (tmp,)),
    ]
    failed = 0
    for name, fn, args in cases:
        try:
            fn(*args)
            print(f"  OK {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"协议面与并发鲁棒性 {len(cases) - failed}/{len(cases)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
