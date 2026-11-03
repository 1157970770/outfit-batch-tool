#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""任务状态机回归测试：P0-1（调度线程不能死）+ P0-2（非成功终态不能算「全部完成」）。

不联网、不起 HTTP 服务、不调模型：只驱动 JobManager 的派发/收尾逻辑与
app.core.status_counts 的汇总口径。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _tmpdir  # noqa: E402
from app.core import status_counts  # noqa: E402
import app.web.jobs as jobs_mod  # noqa: E402
from app.web.jobs import JobManager  # noqa: E402


def check(cond: bool, message: str) -> None:
    if not cond:
        raise AssertionError(message)


def make_manager(tmp: Path) -> JobManager:
    """构造一个不跑后台派发线程的 JobManager（派发由测试逐步驱动）。"""
    settings = SimpleNamespace(
        data_dir=tmp,
        max_active_jobs=2,
        max_active_small_jobs=1,
        max_active_per_user=2,
    )
    manager = JobManager(settings, {})
    manager.stop_event.set()
    with manager.changed:
        manager.changed.notify_all()
    manager.dispatcher.join(timeout=3)
    check(not manager.dispatcher.is_alive(), "后台派发线程没停下来，测试无法单步驱动")
    return manager


def queued_job(job_id: str, *, lane: str = "big", created_at: float = 1.0) -> dict:
    return {
        "id": job_id, "status": "queued", "owner": "u1", "lane": lane,
        "created_at": created_at, "batch_id": f"B-{job_id}", "total": 1, "done": 0,
        "records": [], "templates": [], "cancel_requested": False,
    }


def test_load_backfills_missing_id(tmp: Path) -> None:
    jobs_dir = tmp / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    (jobs_dir / "abc.json").write_text(json.dumps(
        {"status": "succeeded", "batch_id": "B1", "total": 1, "done": 1},
        ensure_ascii=False), encoding="utf-8")

    manager = make_manager(tmp)
    check("abc" in manager.jobs, "按文件名补 id 后应能载入")
    check(manager.jobs["abc"].get("id") == "abc", "id 必须被补上，否则派发时 KeyError")
    on_disk = json.loads((jobs_dir / "abc.json").read_text(encoding="utf-8"))
    check(on_disk.get("id") == "abc", "补出来的 id 必须回写，重启后仍然一致")


def test_dispatch_survives_spawn_failure(tmp: Path) -> None:
    manager = make_manager(tmp)
    manager.jobs["j1"] = queued_job("j1")

    def boom(_job_id: str) -> None:
        raise RuntimeError("can't start new thread")

    manager._spawn = boom  # type: ignore[method-assign]
    with manager.changed:
        manager._dispatch_queued(2, 1, 2)

    job = manager.jobs["j1"]
    check(job["status"] == "failed", "起线程失败应把该 job 判失败，而不是留在队列里")
    check("任务无法启动" in job["message"], f"失败原因应可读：{job['message']}")
    check(manager.active == {}, "失败的 job 不能继续占着 active 槽位")

    # 关键回归：一次派发失败后，派发仍要能继续工作（以前这里会带崩整条调度线）。
    manager.jobs["j2"] = queued_job("j2", created_at=2.0)
    spawned: list[str] = []
    manager._spawn = spawned.append  # type: ignore[method-assign]
    with manager.changed:
        manager._dispatch_queued(2, 1, 2)
    check(spawned == ["j2"], f"后续 job 必须照常派发，实际：{spawned}")
    check(manager.active.get("j2", {}).get("lane") == "big", "active 记账不对")


def test_dispatch_skips_job_without_id(tmp: Path) -> None:
    manager = make_manager(tmp)
    manager.jobs["noid"] = {"status": "queued", "owner": "u1", "lane": "big", "created_at": 1}
    manager.jobs["j3"] = queued_job("j3", created_at=2.0)

    spawned: list[str] = []
    manager._spawn = spawned.append  # type: ignore[method-assign]
    with manager.changed:
        manager._dispatch_queued(2, 1, 2)

    check(manager.jobs["noid"]["status"] == "failed", "没有 id 的 job 应就地判失败")
    check(spawned == ["j3"], f"没有 id 的 job 不能挡住后面的 job，实际：{spawned}")


def test_run_job_setup_failure_releases_slot(tmp: Path) -> None:
    manager = make_manager(tmp)
    job = queued_job("k1")
    job.update({"image_path": str(tmp / "x.png"), "prompt": "p", "resolution": "480P",
                "duration": 5, "template_ids": []})
    manager.jobs["k1"] = job
    manager.active["k1"] = {"owner": "u1", "lane": "big", "runner": None}

    original = jobs_mod.unique_batch_dir

    def broken(*_args, **_kwargs):
        raise OSError("disk full")

    jobs_mod.unique_batch_dir = broken
    try:
        manager._run_job("k1")
    finally:
        jobs_mod.unique_batch_dir = original

    check(manager.jobs["k1"]["status"] == "failed", "建输出目录失败必须落到终态")
    check("disk full" in manager.jobs["k1"]["message"], manager.jobs["k1"]["message"])
    check("k1" not in manager.active, "active 槽位必须释放，否则并发额度永久泄漏")
    persisted = json.loads((tmp / "jobs" / "k1.json").read_text(encoding="utf-8"))
    check(persisted["status"] == "failed", "失败状态要落盘，重启后不能又是 running")


def test_job_outcome_rules(tmp: Path) -> None:
    outcome = JobManager._job_outcome
    check(outcome({"total": 3, "success": 3})[:2] == ("succeeded", "全部完成。"), "全成功")
    status, message, done = outcome({"total": 10, "success": 5, "invalid": 5})
    check(status == "partial", f"5 成功 + 5 无效不能被当成成功：{status}")
    check(done == 10, f"10 条都有终态，进度应满：{done}")
    check("5/10" in message, f"消息要说清比例：{message}")
    check(outcome({"total": 2, "success": 0, "failed": 2})[0] == "failed", "全失败")
    status, _msg, done = outcome({"total": 10, "success": 5, "running": 5})
    check(status == "partial" and done == 5, f"等超时的条目不能把进度拉满：{status} {done}")
    check(outcome({"total": 0})[0] == "failed", "空批次按失败处理")

    # 收尾写盘：partial 状态与 summary 都要落盘
    manager = make_manager(tmp)
    manager.jobs["p1"] = queued_job("p1")
    manager.jobs["p1"]["status"] = "running"
    manager._finish_job("p1", {"total": 4, "success": 3, "invalid": 1})
    job = manager.jobs["p1"]
    check(job["status"] == "partial" and job["done"] == 4, f"收尾状态不对：{job['status']} {job['done']}")
    check(job["summary"]["invalid"] == 1, "summary 要原样保存")

    # 排队期取消的优先口径：已请求取消的任务不该显示成 partial/succeeded
    manager.jobs["p2"] = queued_job("p2")
    manager.jobs["p2"]["cancel_requested"] = True
    manager.jobs["p2"]["status"] = "running"
    manager._finish_job("p2", {"total": 2, "success": 2})
    check(manager.jobs["p2"]["status"] == "canceled", "取消优先于成功口径")


def test_status_counts_covers_non_success_states(tmp: Path) -> None:
    records = [
        {"id": "1", "status": "success"},
        {"id": "2", "status": "success"},
        {"id": "3", "status": "invalid"},
        {"id": "4", "status": "expired"},
        {"id": "5", "status": "canceled"},
        {"id": "6", "status": "skipped"},
        {"id": "7", "status": "failed"},
        {"id": "8", "status": "running"},
        {"id": "9"},
    ]
    counts = status_counts(records)
    check(counts["total"] == 9, counts)
    check(counts["success"] == 2, counts)
    check(counts["invalid"] == 1 and counts["expired"] == 1, counts)
    check(counts["canceled"] == 1 and counts["skipped"] == 1, counts)
    check(counts["failed"] == 1 and counts["running"] == 1, counts)
    # 引擎汇总 + 网页口径组合起来，必须得到 partial 而不是 succeeded
    summary = dict(counts, unsuccessful=counts["total"] - counts["success"])
    check(summary["unsuccessful"] == 7, summary)
    check(JobManager._job_outcome(summary)[0] == "partial", "9 条里 7 条没成功，不能报全部完成")


def main() -> int:
    tmp = _tmpdir.tempdir("jobsstate-")
    for name, func in [
        ("load 补 id", test_load_backfills_missing_id),
        ("派发失败不杀线程", test_dispatch_survives_spawn_failure),
        ("跳过无 id job", test_dispatch_skips_job_without_id),
        ("初始化失败释放槽位", test_run_job_setup_failure_releases_slot),
        ("收尾口径", test_job_outcome_rules),
        ("汇总口径", test_status_counts_covers_non_success_states),
    ]:
        case_tmp = tmp / name.replace(" ", "_")
        case_tmp.mkdir(parents=True, exist_ok=True)
        func(case_tmp)
        print(f"  OK {name}")
    print("OK 任务状态机通过")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print(f"FAIL {exc}")
        sys.exit(1)
