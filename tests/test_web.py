#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""局域网网页集成检查：本地 mock 百炼，不产生真实费用。"""
from __future__ import annotations

import base64
import http.cookiejar
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from _tmpdir import tempdir  # noqa: E402
sys.path.insert(0, str(ROOT))

from app.web.auth import make_password  # noqa: E402
from app.web.server import WebApplication, make_handler  # noqa: E402
from app.web.settings import load_settings  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402
from PIL import Image  # noqa: E402
import io  # noqa: E402

MOCK = ROOT / "tests" / "mock_dashscope.py"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def call(opener, base, path, method="GET", payload=None, headers=None):
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(base + path, data=raw, method=method, headers=headers or {})
    try:
        with opener.open(req, timeout=15) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def main() -> int:
    tmp = tempdir("webtest-")
    mock_port = free_port()
    env = os.environ.copy()
    env["MOCK_RUNNING_POLLS"] = "0"
    mock = subprocess.Popen([sys.executable, str(MOCK), str(mock_port)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    server = None
    try:
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", mock_port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            raise RuntimeError("本地 mock 百炼服务未启动")

        manifest = tmp / "manifest.json"
        manifest.write_text(json.dumps([{"id": "001", "name": "测试场景", "url": f"http://127.0.0.1:{mock_port}/file.mp4", "duration": 7}]), encoding="utf-8")
        api_config = tmp / "config.json"
        api_config.write_text(json.dumps({
            "wan": {"provider": "dashscope", "api_key": "mock-api-key-123456789", "model": "wan3.0-video",
                    "base_url": f"http://127.0.0.1:{mock_port}/api/v1", "default_mode": "edit",
                    "poll_interval": 1, "poll_timeout": 30, "max_concurrency": 3},
            "manifest_url": str(manifest), "cos": {"enabled": False},
        }), encoding="utf-8")
        web_config = tmp / "web.json"
        secret = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
        web_config.write_text(json.dumps({
            "session_secret": secret, "session_hours": 1, "data_root": str(tmp / "web-data"),
            "max_active_jobs": 2, "max_job_items": 20, "confirm_over_items": 5,
            "users": {
                "alice": {**make_password("alice-pass-123"), "role": "user", "enabled": True, "version": 1},
                "bob": {**make_password("bob-pass-12345"), "role": "user", "enabled": True, "version": 1},
                "admin": {**make_password("admin-pass-123"), "role": "admin", "enabled": True, "version": 1},
            },
        }), encoding="utf-8")
        settings = load_settings(base_dir=tmp, web_file=web_config, api_file=api_config, load_templates=True)
        app = WebApplication(settings)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        alice = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        bob = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        admin = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

        code, _, _ = call(alice, base, "/api/me")
        assert code == 401, code
        code, _, _ = call(alice, base, "/api/login", "POST", {"username": "alice", "password": "alice-pass-123"})
        assert code == 200, code
        code, _, boot = call(alice, base, "/api/bootstrap")
        assert code == 200 and json.loads(boot)["service"]["templates_count"] == 1
        prompt_id = json.loads(boot)["prompts"][0]["id"]

        image = io.BytesIO()
        Image.new("RGB", (256, 320), (230, 220, 210)).save(image, format="PNG")
        code, _, upload = call(alice, base, "/api/uploads", "POST", {
            "filename": "outfit.png", "data": "data:image/png;base64," + base64.b64encode(image.getvalue()).decode("ascii")})
        assert code == 201, (code, upload)
        upload_id = json.loads(upload)["upload_id"]
        code, _, created = call(alice, base, "/api/jobs", "POST", {
            "upload_id": upload_id, "selection": "selected", "template_id": "001", "count": 1,
            "prompt_id": prompt_id, "resolution": "480P", "duration": 5})
        assert code == 201, (code, created)
        job_id = json.loads(created)["job"]["id"]

        deadline = time.time() + 40
        job = None
        while time.time() < deadline:
            code, _, raw = call(alice, base, f"/api/jobs/{job_id}")
            assert code == 200
            job = json.loads(raw)["job"]
            if job["status"] in {"succeeded", "partial", "failed", "canceled"}:
                break
            time.sleep(0.25)
        assert job and job["status"] == "succeeded", job
        assert job["records"][0]["status"] == "success", job
        assert "out_dir" not in (job.get("summary") or {}), job

        code, headers, video = call(alice, base, f"/api/jobs/{job_id}/download/001", headers={"Range": "bytes=0-9"})
        assert code == 206 and len(video) == 10, (code, len(video))
        code, _, _ = call(bob, base, "/api/login", "POST", {"username": "bob", "password": "bob-pass-12345"})
        assert code == 200
        code, _, _ = call(bob, base, f"/api/jobs/{job_id}")
        assert code == 403, code
        code, _, _ = call(admin, base, "/api/login", "POST", {"username": "admin", "password": "admin-pass-123"})
        assert code == 200
        code, _, all_jobs = call(admin, base, "/api/jobs?scope=all")
        assert code == 200 and len(json.loads(all_jobs)["jobs"]) == 1
        code, _, users = call(admin, base, "/api/admin/users")
        assert code == 200 and all("hash" not in u for u in json.loads(users)["users"])
        code, _, added = call(admin, base, "/api/admin/users", "POST", {"username": "newuser", "password": "newuser-pass-123", "role": "user"})
        assert code == 201, (code, added)
        code, _, _ = call(admin, base, "/api/admin/users/newuser/reset", "POST", {"password": "reset-pass-12345"})
        assert code == 200
        code, _, _ = call(admin, base, "/api/admin/users/newuser/disable", "POST", {})
        assert code == 200
        code, _, _ = call(admin, base, "/api/admin/users/newuser/enable", "POST", {})
        assert code == 200
        print("OK 网页上传 → mock 生成 → 进度 → 下载 → 用户隔离 → 管理员账号管理")
        return 0
    finally:
        if server:
            server.shutdown()
            server.server_close()
        if mock.poll() is None:
            mock.terminate()
            try:
                mock.wait(timeout=5)
            except subprocess.TimeoutExpired:
                mock.kill()


if __name__ == "__main__":
    raise SystemExit(main())
