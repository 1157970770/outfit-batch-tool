#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""等待超时的「续查」测试（P0）：本地 mock 百炼，不产生真实费用。

背景：这个项目 2026-11-06 有一批 23 条「等待超时」，事后查云端**全部已出片**。
本机 1800s 没等到 ≠ 云端失败；直接点「重试」= 重新提交 = 二次计费。
所以超时条目要做的是「按原 taskId 复查」：只查询、出片就补下载，绝不重新提交。

覆盖：
1. 复查只查询云端，submitted 计数不变（不二次提交）；
2. 云端已出片 → 补下载落盘 + 补记账 + 记录转 success；
3. 云端 FAILED / CANCELED / UNKNOWN → 转 failed / canceled / expired，并写明可安全重试；
4. 没有 taskId 的记录 → 保持 timeout，提示只能重提；
5. 复查期间再次点击被拒（不并发打云端）；
6. 批次条目被复查救活后，_reconcile 自动把 failed 升成 done；批次页能读到 timeout_jobs。
7. 「云端已出片、只是下载失败」（download_failed）：重试必须被拒（二次计费），复查能补回。
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
from app.web.batches import BatchError, BatchManager  # noqa: E402
from app.web.jobs import (  # noqa: E402
    JobManager, needs_recheck, retry_blocked_download_failed,
)
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
                "default_mode": "edit", "poll_interval": 0.1, "poll_timeout": 30,
                "max_concurrency": 4, "query_qps": 50,
                # 测试里关掉「超时后自动复查」，避免后台线程干扰断言顺序。
                "timeout_recheck_delay": 0,
                "price_per_second": {"480P": 0.15, "720P": 0.3, "1080P": 0.6}},
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
    templates = [{"id": tid, "url": "http://127.0.0.1/file.mp4", "duration": 1.5}
                 for tid in ("001", "002", "003", "004", "005")]
    job = {
        "id": job_id, "owner": "alice", "status": "partial",
        "created_at": now - 600, "started_at": now - 300, "finished_at": now - 60,
        "batch_id": f"WEB-alice-{job_id}", "image_path": str(tmp / "look.png"),
        "image_name": "look.png", "prompt_id": "p1", "prompt": "把衣服换到模特身上",
        "templates": templates, "template_ids": [t["id"] for t in templates],
        "resolution": "720P", "duration": 7,
        "total": 5, "done": 1, "records": [], "summary": None,
        "message": "部分完成。", "out_dir": str(out_dir), "log_tail": [],
        "cancel_requested": False, "batch_ref": "", "tags": {"top": "AW26-TEST"},
        "style_no": "", "lane": "big",
    }
    job.update(over)
    return job


def wait_recheck(manager: JobManager, job_id: str, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = manager.jobs.get(job_id) or {}
        state = job.get("recheck") or {}
        if state and not state.get("running") and state.get("finished_at"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"等待复查结束超时：{manager.jobs.get(job_id)}")


def main() -> int:
    tmp = tempdir("rechecktest-")
    Image.new("RGB", (480, 720), (200, 180, 160)).save(tmp / "look.png")

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env["MOCK_RUNNING_POLLS"] = "0"        # 查询即出片
    mock = subprocess.Popen([sys.executable, str(MOCK), str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    time.sleep(0.6)
    managers: list[JobManager] = []
    try:
        settings = make_settings(tmp, base)
        out = Path(tmp) / "web-data" / "output" / "WEB-alice-recheck000001"
        out.mkdir(parents=True, exist_ok=True)
        # 已完成的条目：复查不能动它（不能重下载、不能改状态）
        done_file = out / "005.mp4"
        done_file.write_bytes(b"FAKE-MP4-" + b"0" * (120 * 1024))

        write_job(settings, tmp, base_job(tmp, "recheck000001", records=[
            # 1) 云端已出片
            {"id": "001", "status": "timeout", "task_id": "task-ok-1",
             "provider": "dashscope", "resolution": "720P",
             "file": str(out / "001.mp4"),
             "submitted_at": "2026-11-06 17:54:37", "task_expires_at": int(time.time()) + 3600},
            # 2) 云端明确失败
            {"id": "002", "status": "timeout", "task_id": "task-fail",
             "provider": "dashscope", "resolution": "720P",
             "file": str(out / "002.mp4")},
            # 3) 用户取消 / 云端已清理
            {"id": "003", "status": "timeout", "task_id": "task-canceled",
             "provider": "dashscope", "resolution": "720P",
             "file": str(out / "003.mp4")},
            # 4) 当年提交就没拿到 taskId：无法复查
            {"id": "004", "status": "timeout",
             "provider": "dashscope", "resolution": "720P",
             "file": str(out / "004.mp4")},
            # 5) 已经成功：复查必须跳过
            {"id": "005", "status": "success", "file": str(done_file),
             "billable_seconds": 14.0, "provider": "dashscope", "resolution": "720P"},
        ]))
        submitted_before = mock_stats(base)["submitted"]
        manager = JobManager(settings, {})
        managers.append(manager)

        # ---------- 1. 启动复查：立刻返回，running=True
        snapshot = manager.recheck_timeouts("recheck000001")
        state = snapshot.get("recheck") or {}
        assert state.get("running") is True and state.get("total") == 4, state
        assert state.get("message", "").startswith("正在向云端复查"), state

        # 复查中再点一次必须被拒（不能并发打同一个 taskId）
        try:
            manager.recheck_timeouts("recheck000001")
            raise AssertionError("复查进行中应当拒绝重复启动")
        except ValueError as exc:
            assert "还没结束" in str(exc), exc

        job = wait_recheck(manager, "recheck000001")
        records = {str(r["id"]): r for r in job["records"]}
        assert mock_stats(base)["submitted"] == submitted_before, "复查绝不能重新提交任务"
        print(f"  ✓ 只查询不提交（submitted 仍为 {submitted_before}）")

        # ---------- 2. 云端已出片 → 补下载 + 补记账 + success
        rec = records["001"]
        assert rec["status"] == "success", rec
        assert Path(rec["file"]).is_file() and Path(rec["file"]).stat().st_size > 0, rec
        assert str(rec["cloud_status"]).lower() == "succeeded", rec
        assert float(rec["billable_seconds"] or 0) > 0, rec
        assert rec["checked_at"], rec
        print(f"  ✓ 云端已出片：补下载 {Path(rec['file']).name}、记账 {rec['billable_seconds']}s、转 success")

        # ---------- 3. 云端判死 → 转终态并说明「可以安全重试」
        for tid, expect in (("002", "failed"), ("003", "canceled")):
            rec = records[tid]
            assert rec["status"] == expect, rec
            assert "不会重复计费" in (rec.get("error") or ""), rec
        rec = records["003"]
        assert str(rec["cloud_status"]).lower() == "canceled", rec
        print("  ✓ 云端 FAILED/CANCELED → 转 failed/canceled，并写明重试不会二次计费")

        # ---------- 4. 没有 taskId → 保持 timeout，只能重提
        rec = records["004"]
        assert rec["status"] == "timeout", rec
        assert "没有 taskId" in (rec.get("error") or ""), rec
        print("  ✓ 无 taskId：保持 timeout 并提示只能重新提交")

        # ---------- 5. 已成功的条目不许被复查动到
        rec = records["005"]
        assert rec["status"] == "success" and rec.get("checked_at") is None, rec
        print("  ✓ 已成功条目原样保留（不复查、不重下载）")

        # 汇总：1 条救回 + 2 条判死 + 1 条仍超时；1 条历史成功
        state = job["recheck"]
        assert state["checked"] == 4 and state["success"] == 1, state
        assert state["failed"] == 2 and state["errors"] == 1, state
        assert job["status"] == "partial", job["status"]
        assert job["summary"]["timeout"] == 1, job["summary"]
        print(f"  ✓ 汇总：复查 4 条（补回 {state['success']} / 判死 {state['failed']} / 无 taskId {state['errors']}），job 仍 partial")

        # ---------- 6. 批次：条目被救回后自动从 failed 升成 done
        bm = BatchManager(settings, manager, lambda: None, lambda uid, owner: None)
        batch = {
            "id": "batchheal01", "batch_no": "T-HEAL-01", "owner": "alice", "state": "locked",
            "items": [
                {"template_id": "001", "state": "failed", "job_id": "recheck000001",
                 "error": "等待超时（1800s）"},
                {"template_id": "002", "state": "failed", "job_id": "recheck000001",
                 "error": "等待超时（1800s）"},
                {"template_id": "005", "state": "done", "job_id": "recheck000001", "error": ""},
            ],
            "runs": [{"job_id": "recheck000001", "count": 5}],
            "active_job_id": "", "total_items": 3, "done": 1, "failed": 2, "pending": 0,
            "created_at": time.time() - 3600, "updated_at": time.time() - 3600,
        }
        bm.batches[batch["id"]] = batch
        bm._save(batch)
        # 001 已救回、002 云端失败、004 仍超时 → 批次还该显示「可复查」
        assert bm.public(batch)["timeout_jobs"] == ["recheck000001"], bm.public(batch)
        assert bm._reconcile(batch) is True
        states = {item["template_id"]: item["state"] for item in batch["items"]}
        assert states["001"] == "done", states        # 救回 → 自动变已完成
        assert states["002"] == "failed", states      # 真失败 → 保持失败
        assert states["005"] == "done", states
        assert batch["done"] == 2 and batch["failed"] == 1, batch
        print("  ✓ 复查救回的条目：批次自动 failed → done，真失败保持 failed")

        # ---------- 7. 批次级复查入口：一次把该批次所有超时 job 都查一遍
        before = mock_stats(base)["submitted"]
        result = bm.recheck_timeouts("batchheal01", {"username": "alice", "role": "user"})
        assert result["jobs"] == ["recheck000001"], result     # 还剩 004 没 taskId
        job = wait_recheck(manager, "recheck000001")
        assert mock_stats(base)["submitted"] == before, "批次级复查也不能重新提交"
        assert job["recheck"]["checked"] == 1, job["recheck"]  # 只剩 004 这条超时
        print("  ✓ 批次级复查：只查剩余超时条目，依旧不重新提交")

        # ---------- 8. 「云端已出片、只是下载失败」：不许重试，只能复查补下载 ----------
        # 2026-11-10 job b1cc 的真实教训：云端 succeeded 并已计费 15s，本机下载时 TLS 断流，
        # 记录落成 failed+download_failed；用户在页面上点「只重试失败的 1 条」→ 又提交一次
        # → 二次计费。所以这类记录必须被重试排除、被复查纳入。
        dl_out = Path(tmp) / "web-data" / "output" / "WEB-alice-dlfail000001"
        dl_out.mkdir(parents=True, exist_ok=True)
        dl_job = base_job(tmp, "dlfail000001", status="partial", total=1, done=0, summary=None, records=[
            {"id": "001", "status": "failed", "download_failed": True,
             "task_id": "task-ok-dl", "provider": "dashscope", "resolution": "720P",
             "file": str(dl_out / "001.mp4"), "billable_seconds": 15.0, "estimated_cost": 4.5,
             "error": "云端已出片并按 15 秒计费，但下载失败：URLError: <urlopen error "
                      "[SSL: UNEXPECTED_EOF_WHILE_READING]>"},
        ])
        write_job(settings, tmp, dl_job)
        manager.jobs[dl_job["id"]] = dl_job     # 本段只测这两条链路，直接把 job 放进内存
        rec = dl_job["records"][0]
        assert needs_recheck(rec) and needs_recheck({"status": "timeout"}), rec
        assert not needs_recheck({"status": "failed"}), "普通失败不该走复查"
        assert len(retry_blocked_download_failed(dl_job)) == 1, dl_job
        print("  ✓ needs_recheck 认出「下载失败」；普通失败仍算可重试")

        before = mock_stats(base)["submitted"]
        try:
            manager.retry_failed("dlfail000001", "alice")
            raise AssertionError("下载失败的记录绝不能被重试（会二次提交、二次计费）")
        except ValueError as exc:
            assert "二次计费" in str(exc) and "复查" in str(exc), exc
        assert mock_stats(base)["submitted"] == before, "被拒的重试不能真的提交任务"
        print("  ✓ 点重试被拒，并提示改走「复查补下载」（submitted 未变）")

        # 批次侧：条目带上 download_failed 标记，批次页据此提示「会被跳过」
        bm.batches["batchdlfail"] = {
            "id": "batchdlfail", "batch_no": "T-DL-01", "owner": "alice", "state": "locked",
            "images": [{"upload_id": "", "name": "look.png", "path": str(tmp / "look.png")}],
            "image_name": "look.png",
            "items": [{"template_id": "001", "state": "producing",
                       "job_id": "dlfail000001", "error": ""}],
            "runs": [{"job_id": "dlfail000001", "count": 1, "started_at": time.time()}],
            "active_job_id": "dlfail000001", "total_items": 1, "done": 0, "failed": 0,
            "pending": 0, "created_at": time.time() - 600, "updated_at": time.time() - 600,
        }
        spawned = bm.batches["batchdlfail"]
        assert bm._reconcile(spawned) is True
        assert spawned["items"][0]["download_failed"] is True, spawned["items"][0]
        assert bm.public(spawned)["download_failed_items"] == 1, bm.public(spawned)
        assert bm.public(spawned)["timeout_jobs"] == ["dlfail000001"], bm.public(spawned)
        try:
            bm.retry_failed("batchdlfail", {"username": "alice", "role": "user"}, {"count": 10})
            raise AssertionError("批次重试也必须跳过下载失败条目")
        except BatchError as exc:
            assert "二次计费" in str(exc), exc
        assert mock_stats(base)["submitted"] == before, "批次重试被拒时也不能提交"
        print("  ✓ 批次条目带 download_failed 标记，批次重试同样被拒")

        # 复查能把它补回来：按原 taskId 下载 → success，批次条目自动升 done 并清标记
        manager.recheck_timeouts("dlfail000001")
        dl_job = wait_recheck(manager, "dlfail000001")
        rec = dl_job["records"][0]
        assert rec["status"] == "success", rec
        assert Path(rec["file"]).is_file() and Path(rec["file"]).stat().st_size > 0, rec
        assert mock_stats(base)["submitted"] == before, "复查补下载不能重新提交"
        assert bm._heal_items(spawned) is True, spawned["items"]
        assert spawned["items"][0]["state"] == "done", spawned["items"][0]
        assert "download_failed" not in spawned["items"][0], spawned["items"][0]
        print("  ✓ 复查补下载成功，记录/批次条目恢复为完成，全程零新提交")

        print("OK 超时续查：只查不提交 / 补下载记账 / 云端判死转终态 / 无 taskId 保留 / 批次自动救回")
        print("OK 下载失败：不许重试（防二次计费）/ 复查补回 / 批次标记与自动恢复")
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
