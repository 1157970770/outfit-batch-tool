#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""计费口径回归测试（第二批 P1）：离线跑，不联网、不花钱。

覆盖：
1. `_RateLimiter.penalize` 必须封顶（连续 429 不能把全站推到很远的未来）。
2. `submit_outcome_unknown`：5xx / 网络中断算「结果未知」，4xx 明确拒绝不算。
3. 下载失败时账不能丢：云端已出片，`billable_seconds` / `estimated_cost` 仍要落库，
   状态标 failed 且报错里写明已按秒计费。
4. 提交结果未知时记录状态为 `unknown`，且汇总里单列，不被当成普通失败静默重试。
5. 批次生产的两阶段标记：`jobs.create` 之前先落 `pending_run`；
   建完 job 后写盘失败，下一次对账能认领回来，不会让用户再点一次生产。
"""
from __future__ import annotations

import sys
import threading
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from _tmpdir import tempdir  # noqa: E402
sys.path.insert(0, str(ROOT))

from app.core import (BatchRunner, BatchSpec, BatchState, SubmitUnknown,  # noqa: E402
                      status_counts, submit_outcome_unknown)
from app.wan_api import (STATUS_SUCCEEDED, SubmitResult, TaskResult, WanError,  # noqa: E402
                         _RateLimiter)
from app.web.batches import BatchError, BatchManager  # noqa: E402


# ---------------------------------------------------------------------------
# 1. 限流惩罚封顶
# ---------------------------------------------------------------------------

def test_penalize_is_capped():
    import time

    limiter = _RateLimiter(qps=1.0)
    limiter._next = time.monotonic()  # 从一个干净的起点开始
    for _ in range(20):
        limiter.penalize(5.0)
        ahead = limiter._next - time.monotonic()
        assert ahead <= _RateLimiter.PENALTY_CAP + 0.5, f"惩罚突破封顶：+{ahead:.1f}s"
    # 封顶后再 penalize 不应继续往后推
    before = limiter._next
    limiter.penalize(5.0)
    assert limiter._next <= before + 0.01


def test_penalize_never_rewinds():
    import time

    limiter = _RateLimiter(qps=1.0)
    now = time.monotonic()
    limiter._next = now + 30.0
    limiter.penalize(1.0)
    assert limiter._next >= now + 29.9, "封顶不能把已有的等待时间抹掉"


# ---------------------------------------------------------------------------
# 2. 提交结果未知的判定
# ---------------------------------------------------------------------------

def test_submit_outcome_unknown_classification():
    assert submit_outcome_unknown(WanError("network", "timed out")) is True
    assert submit_outcome_unknown(WanError("timeout", "read timeout")) is True
    assert submit_outcome_unknown(WanError("server", "bad gateway", http_status=502)) is True
    assert submit_outcome_unknown(WanError("auth", "bad key", http_status=401)) is False
    assert submit_outcome_unknown(WanError("param", "bad body", http_status=400)) is False
    assert submit_outcome_unknown(WanError("throttle", "429", http_status=429)) is False


# ---------------------------------------------------------------------------
# 3 / 4. 引擎：先记账后下载、提交结果未知
# ---------------------------------------------------------------------------

class StubClient:
    """只实现 `_run_one` 需要的最小接口。"""

    def __init__(self, *, submit_exc=None, result=None):
        self.submit_exc = submit_exc
        self.result = result

    def build_body(self, task, defaults=None, *, default_mode=None,
                   probe=None, video_durations=None):
        return {"prompt": task["prompt"], "imageUrls": list(task["imageUrls"]),
                "videoUrls": list(task["videoUrls"])}, [], []

    def wire_body(self, body):
        return body

    def submit(self, wire):
        if self.submit_exc is not None:
            raise self.submit_exc
        return SubmitResult(task_id="task-1", status="PENDING", request_id="req-1")

    def wait_task(self, task_id, *, timeout=None, interval=None, on_status=None,
                  should_stop=None):
        return self.result


def make_runner(tmp: Path, client) -> BatchRunner:
    """绕过 __init__（它会构造真 WanClient），只装 `_run_one` 依赖的属性。"""
    spec = BatchSpec(
        batch_id="billing-1", image_path="", prompt="换装", mode="edit",
        out_dir=tmp / "out", templates=[], api_key="sk-test",
        resolution="480P", duration=7,
        price_per_second={"480P": 0.3},
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


TPL = {"id": "T1", "url": "http://example.com/video.mp4", "duration": 1.5}


def test_download_failure_keeps_billing(monkey="app.core.download_file"):
    import app.core as core

    tmp = Path(tempdir("billing-download-"))
    client = StubClient(result=TaskResult(
        task_id="task-1", status=STATUS_SUCCEEDED,
        video_url="http://example.com/out.mp4",
        usage={"duration": 7}, request_id="req-1",
    ))
    runner = make_runner(tmp, client)

    def boom(url, dest):
        raise OSError("磁盘写满")

    original = core.download_file
    core.download_file = boom
    try:
        rec = runner._run_one(dict(TPL))
    finally:
        core.download_file = original

    assert rec["status"] == "failed", rec
    assert rec["download_failed"] is True
    assert float(rec["billable_seconds"] or 0) > 0, "下载失败也要保留计费秒数"
    assert float(rec["estimated_cost"] or 0) > 0, "下载失败也要保留预估金额"
    assert "计费" in rec["error"], rec["error"]


def test_submit_unknown_is_not_plain_failure():
    tmp = Path(tempdir("billing-unknown-"))
    client = StubClient(submit_exc=WanError("network", "connection reset"))
    runner = make_runner(tmp, client)
    rec = runner._run_one(dict(TPL))
    assert rec["status"] == "unknown", rec
    assert "不要直接重试" in rec["error"], rec["error"]
    assert rec.get("task_id") is None, "提交没拿到 task_id，不能伪造一个"


def test_unknown_counted_separately():
    counts = status_counts([
        {"status": "success"}, {"status": "unknown"}, {"status": "unknown"},
        {"status": "failed"},
    ])
    assert counts["unknown"] == 2
    assert counts["success"] == 1
    assert counts["failed"] == 1
    assert counts["total"] == 4


# ---------------------------------------------------------------------------
# 5. 批次两阶段标记
# ---------------------------------------------------------------------------

class FakeJobs:
    def __init__(self):
        self.lock = threading.RLock()
        self.jobs: dict[str, dict] = {}
        self.created: list[dict] = []

    def create(self, owner, image_path, image_name, templates, prompt_id, resolution,
               duration, prompt, *, batch_ref="", limit_items=None, tags=None, style_no=""):
        job = {
            "id": f"job-{len(self.created) + 1}", "owner": owner, "status": "queued",
            "batch_ref": batch_ref, "records": [],
            "templates": [{"id": str(t["id"])} for t in templates],
        }
        self.created.append(job)
        self.jobs[job["id"]] = job
        return job


class FakeCatalog:
    def __init__(self, items: dict):
        self.items = items

    def preview(self, template_id):
        return self.items.get(str(template_id))


def make_batch_manager(tmp: Path, jobs: FakeJobs) -> BatchManager:
    settings = types.SimpleNamespace(
        data_dir=tmp, max_batch_items=500, max_batch_run_items=100,
    )
    return BatchManager(settings, jobs, lambda: FakeCatalog({}), lambda *a, **k: None)


def make_locked_batch(tmp: Path, count: int = 2) -> dict:
    image = tmp / "image.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    return {
        "id": "batch-1", "batch_no": "2026-11-01", "title": "", "owner": "alice",
        "tags": {"top": "AX111"}, "style_no": "",
        "state": "locked", "image_upload_id": "u1", "image_path": str(image),
        "image_name": "image.png", "prompt_id": "p1", "prompt": "换装",
        "resolution": "720P", "duration": 7, "tag_filters": {},
        "items": [{"template_id": f"T{i}", "name": f"模板{i}", "tags": [],
                   "state": "pending", "job_id": "", "error": ""} for i in range(1, count + 1)],
        "runs": [], "active_job_id": "",
        "total_items": count, "done": 0, "failed": 0, "pending": count, "producing": 0,
        "created_at": 0.0, "updated_at": 0.0, "locked_at": 0.0, "closed_at": None,
    }


LIVE = {f"T{i}": {"id": f"T{i}", "name": f"模板{i}", "url": f"http://example.com/{i}.mp4",
                  "key": "", "duration": 1.5} for i in range(1, 4)}


def make_catalog_manager(tmp: Path, jobs: FakeJobs) -> BatchManager:
    manager = make_batch_manager(tmp, jobs)
    manager.catalog_of = lambda: FakeCatalog(LIVE)
    return manager


def test_start_run_writes_marker_before_create():
    tmp = Path(tempdir("billing-batch-marker-"))
    jobs = FakeJobs()
    manager = make_catalog_manager(tmp, jobs)
    batch = make_locked_batch(tmp)
    real_save = manager._save

    def always_fail(batch_dict):
        raise OSError("磁盘写满")

    manager._save = always_fail
    try:
        manager._start_run(batch, {"username": "alice", "role": "user"},
                           ("pending",), 20, "没有待生产的条目")
        raise AssertionError("写盘失败必须直接报错")
    except BatchError as exc:
        assert "没有创建任务" in str(exc), str(exc)
    finally:
        manager._save = real_save
    assert jobs.created == [], "标记都没写成功，就不能创建任务"
    assert "pending_run" not in batch, "失败后要清掉内存标记"


def test_start_run_recovers_when_save_fails_after_create():
    tmp = Path(tempdir("billing-batch-recover-"))
    jobs = FakeJobs()
    manager = make_catalog_manager(tmp, jobs)
    batch = make_locked_batch(tmp)
    manager.batches[batch["id"]] = batch
    real_save = manager._save
    calls = {"n": 0}

    def flaky_save(batch_dict):
        calls["n"] += 1
        if calls["n"] == 2:      # 第一次写标记成功，建完 job 写回 job_id 时失败
            raise OSError("磁盘写满")
        real_save(batch_dict)

    manager._save = flaky_save
    try:
        manager._start_run(batch, {"username": "alice", "role": "user"},
                           ("pending",), 20, "没有待生产的条目")
        raise AssertionError("写盘失败必须报错，不能让前端以为生产成功")
    except BatchError as exc:
        assert "任务已创建" in str(exc), str(exc)
    finally:
        manager._save = real_save

    assert len(jobs.created) == 1
    job_id = jobs.created[0]["id"]
    assert batch.get("pending_run", {}).get("job_id") == job_id

    # 下一次读批次（持锁对账）会把条目接回 producing，而不是当成还能再点一次生产
    adopted = manager._adopt_pending_run(batch)
    assert adopted == job_id
    assert "pending_run" not in batch
    assert batch["active_job_id"] == job_id
    assert all(item["state"] == "producing" for item in batch["items"])
    assert [run["job_id"] for run in batch["runs"]] == [job_id]


def test_adopt_marks_failed_when_job_missing():
    tmp = Path(tempdir("billing-batch-missing-"))
    jobs = FakeJobs()
    manager = make_catalog_manager(tmp, jobs)
    batch = make_locked_batch(tmp)
    batch["pending_run"] = {"template_ids": ["T1", "T2"], "job_id": "job-gone",
                            "started_at": 0.0}
    assert manager._adopt_pending_run(batch) == ""
    assert "pending_run" not in batch
    assert all(item["state"] == "failed" for item in batch["items"])
    assert all("核对" in item["error"] for item in batch["items"])


def test_adopt_clear_when_create_never_happened():
    tmp = Path(tempdir("billing-batch-nocreate-"))
    jobs = FakeJobs()
    manager = make_catalog_manager(tmp, jobs)
    batch = make_locked_batch(tmp)
    batch["pending_run"] = {"template_ids": ["T1"], "job_id": "", "started_at": 0.0}
    assert manager._adopt_pending_run(batch) == ""
    assert "pending_run" not in batch
    assert all(item["state"] == "pending" for item in batch["items"]), "什么都没建，条目必须还能再排"


if __name__ == "__main__":
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    for name, fn in tests:
        fn()
        print(f"  OK {name}")
    print(f"OK 计费口径 {len(tests)} 组断言通过")
