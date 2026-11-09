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

from app import traffic  # noqa: E402
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
            "manifest_url": str(manifest),
            # 本地 COS 后端：归档真跑一遍（落 tmp/cos-root），管理端云端检索/签名才有数据可查
            # bucket 仅用于走通 build_storages 的分支判断，本地后端不校验桶名
            "cos": {"enabled": True, "provider": "local", "root": str(tmp / "cos-root"),
                    "bucket": "local-000000"},
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
        assert json.loads(boot)["service"]["small_lane_items"] == 3
        prompt_options = json.loads(boot)["prompts"]
        assert not any(p["title"] == "服装定妆" for p in prompt_options), prompt_options
        prompt_option = prompt_options[0]
        prompt_id = prompt_option["id"]
        custom_prompt = prompt_option["body"] + "\n本次微调：只展示服装，不改变人物。"

        image = io.BytesIO()
        Image.new("RGB", (256, 320), (230, 220, 210)).save(image, format="PNG")
        code, _, upload = call(alice, base, "/api/uploads", "POST", {
            "filename": "outfit.png", "data": "data:image/png;base64," + base64.b64encode(image.getvalue()).decode("ascii")})
        assert code == 201, (code, upload)
        upload_id = json.loads(upload)["upload_id"]
        code, _, created = call(alice, base, "/api/jobs", "POST", {
            "upload_id": upload_id, "selection": "selected", "template_id": "001", "count": 1,
            "prompt_id": prompt_id, "prompt": custom_prompt, "resolution": "480P", "duration": 5,
            "tags": {"top": "AW26-143", "lining": "NB-02", "scene": "秋冬"}})
        assert code == 201, (code, created)
        job_id = json.loads(created)["job"]["id"]
        assert json.loads(created)["job"]["tags"] == {"top": "AW26-143", "lining": "NB-02", "scene": "秋冬"}, created
        # 上衣缺了必须 400：云端归档路径按上衣分段
        code, _, no_tag = call(alice, base, "/api/jobs", "POST", {
            "upload_id": upload_id, "selection": "selected", "template_id": "001", "count": 1,
            "prompt_id": prompt_id, "prompt": custom_prompt, "resolution": "480P", "duration": 5})
        assert code == 400 and "上衣" in json.loads(no_tag)["error"], (code, no_tag)

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
        assert job["lane"] == "small", job          # 1 条试跑走快速通道
        assert job["records"][0]["status"] == "success", job
        assert "out_dir" not in (job.get("summary") or {}), job
        with urllib.request.urlopen(f"http://127.0.0.1:{mock_port}/stats", timeout=5) as response:
            mock_state = json.loads(response.read().decode("utf-8"))
        assert mock_state["last_body"]["input"]["prompt"] == custom_prompt

        code, headers, video = call(alice, base, f"/api/jobs/{job_id}/download/001", headers={"Range": "bytes=0-9"})
        assert code == 206 and len(video) == 10, (code, len(video))

        # COS 流量账本（app/traffic.py）：预览/下载是我们自己代理字节，必须按实测记账；
        # 生产时万相来拉模板/图片，只能按对象大小推定，也要各留一笔。
        code, _, _ = call(alice, base, "/api/templates/001/preview", headers={"Range": "bytes=0-1023"})
        assert code in (200, 206), code
        code, _, preview_full = call(alice, base, "/api/templates/001/preview")
        assert code == 200 and len(preview_full) > 1024, (code, len(preview_full))
        code, _, downloaded = call(alice, base, "/api/templates/001/download")
        assert code == 200 and downloaded == preview_full, (code, len(downloaded))
        ledger = traffic.collect(days=1)
        assert ledger["days"] == 1, f"账本没启用：{ledger}"
        assert ledger["totals"]["preview"]["b"] > 1024, ledger["totals"]
        assert ledger["totals"]["download"]["b"] == len(preview_full), ledger["totals"]
        assert ledger["ranges"][ledger["last"]] == {"total": 2, "range": 1}, ledger["ranges"]
        # 生产那条任务会让万相拉 1 个模板 + 1 张定妆图（manifest 无 size_mb，模板字节为 0）
        assert ledger["totals"]["template"]["n"] >= 1, ledger["totals"]
        assert ledger["totals"]["image"]["n"] >= 1, ledger["totals"]
        code, _, _ = call(bob, base, "/api/login", "POST", {"username": "bob", "password": "bob-pass-12345"})
        assert code == 200
        code, _, _ = call(bob, base, f"/api/jobs/{job_id}")
        assert code == 403, code
        code, _, _ = call(admin, base, "/api/login", "POST", {"username": "admin", "password": "admin-pass-123"})
        assert code == 200
        code, _, all_jobs = call(admin, base, "/api/jobs?scope=all")
        assert code == 200 and len(json.loads(all_jobs)["jobs"]) == 1

        # 历史分离：lane 过滤 + 分页字段，试跑页的历史入口靠它只取小批量
        code, _, small_only = call(admin, base, "/api/jobs?scope=all&lane=small&limit=10")
        small_payload = json.loads(small_only)
        assert code == 200 and small_payload["total"] == 1, small_payload
        assert all(job["lane"] == "small" for job in small_payload["jobs"]), small_payload
        code, _, big_only = call(admin, base, "/api/jobs?scope=all&lane=big")
        assert json.loads(big_only)["total"] == 0, big_only

        # 第二个账户用自己的 Key 独立跑一单：验证账户级 Key 端到端，而不是只认管理员
        code, _, bup = call(bob, base, "/api/uploads", "POST", {
            "filename": "bob.png", "data": "data:image/png;base64," + base64.b64encode(image.getvalue()).decode("ascii")})
        assert code == 201, (code, bup)
        code, _, bcreated = call(bob, base, "/api/jobs", "POST", {
            "upload_id": json.loads(bup)["upload_id"], "selection": "selected",
            "template_id": "001", "count": 1, "prompt_id": prompt_id, "prompt": custom_prompt,
            "resolution": "480P", "duration": 5, "tags": {"top": "BOB-001"}})
        assert code == 201, (code, bcreated)
        bjob_id = json.loads(bcreated)["job"]["id"]
        deadline = time.time() + 40
        bjob = None
        while time.time() < deadline:
            code, _, raw = call(bob, base, f"/api/jobs/{bjob_id}")
            bjob = json.loads(raw)["job"]
            if bjob["status"] in {"succeeded", "partial", "failed", "canceled"}:
                break
            time.sleep(0.25)
        assert bjob and bjob["status"] == "succeeded", bjob
        # 管理员按账户筛选：只能看到 bob 的 1 条
        code, _, only_bob = call(admin, base, "/api/jobs?scope=all&owner=bob")
        assert code == 200 and len(json.loads(only_bob)["jobs"]) == 1, only_bob
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
        # 删除账号：非管理员 403、不存在 404、自删 409、正常删 200 且列表消失
        code, _, _ = call(bob, base, "/api/admin/users/newuser", "DELETE")
        assert code == 403, code
        code, _, _ = call(admin, base, "/api/admin/users/ghost", "DELETE")
        assert code == 404, code
        code, _, _ = call(admin, base, "/api/admin/users/admin", "DELETE")
        assert code == 409, code                      # 不能删除当前登录的账号
        code, _, _ = call(admin, base, "/api/admin/users/newuser", "DELETE")
        assert code == 200, code
        code, _, users = call(admin, base, "/api/admin/users")
        assert code == 200 and not any(u["username"] == "newuser" for u in json.loads(users)["users"])
        code, _, _ = call(admin, base, "/api/admin/users/newuser", "DELETE")
        assert code == 404, code                      # 删过的账号再删 404

        # ---- 按账户统计与逐条明细（全站一把 Key，归属靠 owner） ----
        code, _, usage = call(admin, base, "/api/admin/usage")
        assert code == 200, code
        usage_body = json.loads(usage)
        usage_rows = {row["owner"]: row for row in usage_body["accounts"]}
        assert usage_rows["alice"]["success"] == 1, usage_rows["alice"]
        assert usage_rows["bob"]["success"] == 1, usage_rows["bob"]
        assert usage_body["totals"]["success"] == 2, usage_body["totals"]
        assert usage_body["totals"]["items"] == 2, usage_body["totals"]
        # 单价为 null 时必须走兜底价，否则账户花费永远是 0
        assert usage_rows["alice"]["estimated_cost"] > 0, usage_rows["alice"]
        assert usage_body["totals"]["estimated_cost"] > 0, usage_body["totals"]

        # 逐条明细：哪个账户跑了哪个视频、多少计费秒、多少钱
        code, _, work = call(admin, base, "/api/admin/accounts/alice/work")
        assert code == 200, work
        work_data = json.loads(work)
        assert work_data["total"] == 1, work_data
        item = work_data["items"][0]
        assert item["template_id"] == "001" and item["template_name"] == "测试场景", item
        assert item["status"] == "success" and item["estimated_cost"], item
        assert item["cost_source"] == "record", item
        assert work_data["total_cost"] > 0, work_data
        code, _, csv_raw = call(admin, base, "/api/admin/accounts/alice/work.csv")
        assert code == 200, code
        assert "模板ID".encode("utf-8") in csv_raw and b"001" in csv_raw, csv_raw[:200]

        # 历史任务没有落金额时，按当前单价回算（cost_source=derived）
        record = app.jobs.jobs[job_id]["records"][0]
        original_cost = record["estimated_cost"]
        record["estimated_cost"] = None
        code, _, derived = call(admin, base, "/api/admin/accounts/alice/work?status=success")
        derived_item = json.loads(derived)["items"][0]
        assert derived_item["cost_source"] == "derived" and derived_item["estimated_cost"] > 0, derived_item
        record["estimated_cost"] = original_cost

        # ---- 管理端 · 云端归档：账本检索 + 批量签名下载链接 ----
        code, _, raw = call(admin, base, "/api/admin/archive-search?style=AW26-143")
        assert code == 200, code
        archive = json.loads(raw)
        assert archive["source"] == "ledger" and archive["total"] == 1, archive
        entry = archive["entries"][0]
        assert entry["key"].endswith(".mp4") and entry["owner"] == "alice", entry
        assert entry["batch_no"] == "", entry                # 试跑 job 没有批次归属
        code, _, raw = call(admin, base, "/api/admin/archive-sign", "POST",
                            {"keys": [entry["key"]]})
        assert code == 200, raw
        signed = json.loads(raw)
        assert signed["links"] and signed["links"][0]["url"].startswith("file://"), signed
        assert signed["links"][0]["expires_at"] > time.time(), signed
        # 越权检查：普通用户不能检索云端归档
        code, _, _ = call(alice, base, "/api/admin/archive-search?style=AW26-143")
        assert code == 403, code
        code, _, _ = call(alice, base, "/api/admin/archive-sign", "POST", {"keys": [entry["key"]]})
        assert code == 403, code

        # 签名成功后也要落一笔归档账（字节在同事电脑侧，只记条数）
        assert traffic.collect(days=1)["totals"]["archive"]["n"] >= 1, traffic.collect(days=1)["totals"]

        print("OK 网页上传 → mock 生成 → 下载 → 用户隔离 → 账号管理 → 云端归档检索与签名")
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
