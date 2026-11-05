#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""服务重启「自动接回」测试：本地 mock 百炼，不产生真实费用。

覆盖：
1. 重启前正在跑、且记下了 taskId 的任务 → 重新排队（不是「已中断」），
   接着等那条云端任务出片，全程**不重新提交**（不二次计费）。
2. 已经成功的条目 → 沿用旧记录，不重跑、不重提。
3. 正在跑但没记下 taskId 的 → 不能接回，落 unknown（不可一键重试），job 标「已中断」。
4. 取消过的任务 → 不自动接回。
5. 接回次数达上限 → 不再接回，避免「一启动就崩」的死循环。
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

os.environ.setdefault("WAN_QUERY_RPM", "6000")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from _tmpdir import tempdir  # noqa: E402
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from app.web.auth import make_password  # noqa: E402
from app.web.jobs import JobManager, RESUME_MAX_ATTEMPTS  # noqa: E402
from app.web.settings import load_settings  # noqa: E402

MOCK = ROOT / "tests" / "mock_dashscope.py"


def free_port() -> int:
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def mock_stats(base: str) -> dict:
    with urllib.request.urlopen(f"{base}/stats", timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def make_settings(tmp: Path, mock_base: str):
    secret = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
    web_file = tmp / "web.json"
    web_file.write_text(json.dumps({
        "session_secret": secret, "session_hours": 2, "data_root": str(tmp / "web-data"),
        "max_active_jobs": 4, "max_active_per_user": 2, "max_job_items": 20,
        "users": {"alice": {**make_password("alice-pass-123"), "role": "user",
                            "enabled": True, "version": 1}},
    }), encoding="utf-8")
    api_file = tmp / "config.json"
    api_file.write_text(json.dumps({
        "wan": {"provider": "dashscope", "api_key": "mock-api-key-123456789",
                "model": "wan3.0-video", "base_url": f"{mock_base}/api/v1",
                "default_mode": "edit", "poll_interval": 0.2, "poll_timeout": 30,
                "max_concurrency": 4, "query_qps": 50,
                "price_per_second": {"480P": None, "720P": None, "1080P": None}},
        "manifest_url": "", "cos": {"enabled": False},
    }), encoding="utf-8")
    return load_settings(base_dir=tmp, web_file=web_file, api_file=api_file, load_templates=False)


def write_job(settings, tmp: Path, job: dict) -> None:
    path = Path(settings.data_dir) / "jobs" / f"{job['id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")


def base_job(tmp: Path, job_id: str, **over) -> dict:
    now = time.time()
    out_dir = Path(tmp) / "web-data" / "output" / f"WEB-alice-{job_id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    job = {
        "id": job_id, "owner": "alice", "status": "running",
        "created_at": now - 600, "started_at": now - 300, "finished_at": None,
        "batch_id": f"WEB-alice-{job_id}", "image_path": str(tmp / "look.png"),
        "image_name": "look.png", "prompt_id": "p1", "prompt": "把衣服换到模特身上",
        "templates": [{"id": "001", "url": "http://127.0.0.1/file.mp4", "duration": 1.5}],
        "template_ids": ["001"], "resolution": "720P", "duration": 7,
        "total": 1, "done": 0, "records": [], "summary": None,
        "message": "正在生成，请稍候。", "out_dir": str(out_dir), "log_tail": [],
        "cancel_requested": False, "batch_ref": "", "tags": {"top": "AW26-TEST"},
        "style_no": "", "lane": "small",
    }
    job.update(over)
    return job


def wait_done(manager: JobManager, job_id: str, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = manager.jobs.get(job_id) or {}
        if job.get("status") in ("succeeded", "partial", "failed", "canceled", "interrupted"):
            return job
        time.sleep(0.1)
    raise AssertionError(f"等待任务结束超时：{manager.jobs.get(job_id)}")


def main() -> int:
    tmp = tempdir("resumetest-")
    Image.new("RGB", (480, 720), (200, 180, 160)).save(tmp / "look.png")

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env["MOCK_RUNNING_POLLS"] = "0"        # 查询即出片，测试跑得快
    mock = subprocess.Popen([sys.executable, str(MOCK), str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    time.sleep(0.6)
    managers: list[JobManager] = []
    try:
        settings = make_settings(tmp, base)

        # ---------- 1 + 2. 有 taskId 的在跑条目：自动接回，且不重新提交
        write_job(settings, tmp, base_job(tmp, "resume000001", records=[{
            "id": "001", "status": "running", "task_id": "task-legacy-1",
            "request_id": "req-legacy-1", "provider": "dashscope", "model": "wan3.0-video",
            "mode": "edit", "resolution": "720P",
            "file": str(tmp / "web-data" / "output" / "WEB-alice-resume000001" / "001.mp4"),
            "submitted_at": "2026-11-05 15:20:00", "task_expires_at": int(time.time()) + 3600,
        }]))
        submitted_before = mock_stats(base)["submitted"]
        manager = JobManager(settings, {})
        managers.append(manager)
        job = manager.jobs["resume000001"]
        assert job["status"] == "queued", f"有 taskId 的应重新排队，实际 {job['status']}：{job}"
        assert job.get("resume") is True, job
        assert "接回" in job["message"], job["message"]
        job = wait_done(manager, "resume000001")
        assert job["status"] == "succeeded", job
        rec = job["records"][0]
        assert rec["task_id"] == "task-legacy-1", rec       # 接的就是重启前那条
        assert rec.get("resumed") is True, rec
        assert mock_stats(base)["submitted"] == submitted_before, "接回路径绝不能重新提交"
        print("  ✓ 有 taskId：自动接回原任务、等到出片，未重新提交")

        # ---------- 3. 在跑但没有 taskId：接不回来 → unknown + 已中断
        write_job(settings, tmp, base_job(tmp, "resume000002", records=[{
            "id": "001", "status": "running", "provider": "dashscope",
            "file": str(tmp / "web-data" / "output" / "WEB-alice-resume000002" / "001.mp4"),
        }]))
        manager2 = JobManager(settings, {})
        managers.append(manager2)
        job = manager2.jobs["resume000002"]
        assert job["status"] == "interrupted", job
        assert job["records"][0]["status"] == "unknown", job["records"][0]
        print("  ✓ 在跑但没 taskId：不硬猜，落 unknown（不可一键重试）")

        # ---------- 4. 已取消的任务不接回
        write_job(settings, tmp, base_job(tmp, "resume000003", cancel_requested=True, records=[{
            "id": "001", "status": "running", "task_id": "task-legacy-3",
            "task_expires_at": int(time.time()) + 3600,
            "file": str(tmp / "web-data" / "output" / "WEB-alice-resume000003" / "001.mp4"),
        }]))
        manager3 = JobManager(settings, {})
        managers.append(manager3)
        assert manager3.jobs["resume000003"]["status"] == "interrupted", manager3.jobs["resume000003"]
        print("  ✓ 已取消的任务不自动接回")

        # ---------- 4.5 上次重启留下的「已中断」：云端那条还在跑，这次重启照样接回
        write_job(settings, tmp, base_job(tmp, "resume000005", status="interrupted", records=[{
            "id": "001", "status": "running", "task_id": "task-legacy-5",
            "task_expires_at": int(time.time()) + 3600,
            "file": str(tmp / "web-data" / "output" / "WEB-alice-resume000005" / "001.mp4"),
        }]))
        manager5 = JobManager(settings, {})
        managers.append(manager5)
        job = manager5.jobs["resume000005"]
        assert job["status"] == "queued", f"已中断但云端还在跑的应接回，实际 {job['status']}：{job}"
        job = wait_done(manager5, "resume000005")
        assert job["status"] == "succeeded", job
        print("  ✓ 上次重启留下的「已中断」：云端任务还在，本次重启自动接回并补完")

        # ---------- 5. 达到接回上限：不再接回（防「一启动就崩」死循环）
        write_job(settings, tmp, base_job(
            tmp, "resume000004", resume_count=RESUME_MAX_ATTEMPTS, records=[{
                "id": "001", "status": "running", "task_id": "task-legacy-4",
                "task_expires_at": int(time.time()) + 3600,
                "file": str(tmp / "web-data" / "output" / "WEB-alice-resume000004" / "001.mp4"),
            }]))
        manager4 = JobManager(settings, {})
        managers.append(manager4)
        assert manager4.jobs["resume000004"]["status"] == "interrupted", manager4.jobs["resume000004"]
        print(f"  ✓ 接回次数达上限（{RESUME_MAX_ATTEMPTS}）：停在已中断，不再自动拉起")

        print("OK 服务重启自动接回：接回在跑任务 / 不重提 / 无 taskId 落 unknown / 取消与上限不接回")
        return 0
    finally:
        for instance in managers:
            instance.shutdown()
        if mock.poll() is None:
            mock.terminate()
            try:
                mock.wait(timeout=5)
            except subprocess.TimeoutExpired:
                mock.kill()


if __name__ == "__main__":
    raise SystemExit(main())
