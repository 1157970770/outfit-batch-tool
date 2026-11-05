#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""管理员历史记录：状态口径 / 明细字段 / 状态指纹 / 分组排序的离线测试。

不联网、不起 HTTP 服务、不产生任何云端费用。直接构造 job 文件 → JobManager
→ BatchManager，验证「分组卡片 / 分组明细 / 账户用量」三处数字能对上。
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
from _tmpdir import tempdir  # noqa: E402
sys.path.insert(0, str(ROOT))

from app.web.auth import make_password  # noqa: E402
from app.web.batches import BatchError, BatchManager  # noqa: E402
from app.web.jobs import JobManager  # noqa: E402
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


def write_job(settings, job: dict) -> None:
    path = Path(settings.data_dir) / "jobs" / f"{job['id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")


def new_job(job_id: str, owner: str, records: list[dict], *, created_at: float,
            batch_ref: str = "", templates=None, record_template: str = "") -> dict:
    recs = []
    for rec in records:
        item = dict(rec)
        if record_template:
            item["template"] = record_template
        recs.append(item)
    if templates is None:
        templates = [{"id": rec["id"], "name": f"{rec['id']}.mp4"} for rec in records]
    return {
        "id": job_id, "owner": owner, "status": "succeeded",
        "created_at": created_at, "started_at": created_at + 1, "finished_at": created_at + 60,
        "batch_id": f"WEB-{owner}-{job_id}", "image_path": "", "image_name": "",
        "prompt_id": "p1", "prompt": "把衣服换到模特身上",
        "templates": templates, "template_ids": [rec["id"] for rec in records],
        "resolution": "720P", "duration": 7, "total": len(records), "done": len(records),
        "records": recs, "summary": None, "message": "", "out_dir": "", "log_tail": [],
        "cancel_requested": False, "batch_ref": batch_ref, "tags": {}, "style_no": "",
        "lane": "small",
    }


def main() -> int:
    tmp = tempdir("histadmin-")
    settings = make_settings(tmp)
    now = time.time()
    # 覆盖全状态谱：成功 1 / 失败侧 5（含以前被漏掉的 unknown、timeout、skipped）
    # / 进行中 3（running、pending、空状态）。
    records = [
        {"id": "001", "status": "success"},
        {"id": "002", "status": "unknown"},
        {"id": "003", "status": "timeout"},
        {"id": "004", "status": "skipped"},
        {"id": "005", "status": "failed"},
        {"id": "006", "status": "canceled"},
        {"id": "007", "status": "running"},
        {"id": "008", "status": "pending"},
        {"id": "009", "status": ""},
    ]
    write_job(settings, new_job("hist00000001", "alice", records, created_at=now - 300))
    # 第二个账户：templates 为空、rec.template 是完整 URL，验证模板名回退取文件名。
    write_job(settings, new_job(
        "hist00000002", "bob", [{"id": "010", "status": "success"}], created_at=now - 100,
        templates=[], record_template="https://cos.example.com/input/tpl/0010.mp4"))

    manager = JobManager(settings, {})
    try:
        batches = BatchManager(settings, manager, lambda: None, lambda u, o: None)
        admin = {"username": "admin", "role": "admin"}
        alice = {"username": "alice", "role": "user"}

        # ---------- 1. 状态口径：三处数字必须能加起来
        rows = batches.history_groups(admin, True, "")
        assert len(rows) == 1 and rows[0]["is_ungrouped"], rows
        row = rows[0]
        assert row["total"] == 10, row
        assert row["success"] == 2 and row["failed"] == 5 and row["unfinished"] == 3, row
        assert row["success"] + row["failed"] + row["unfinished"] == row["total"], row
        print("  ✓ 状态口径：unknown/timeout/skipped 计入失败侧，running 计入进行中，三者合计 = total")

        # ---------- 2. 明细状态指纹：单条状态变化必须让签名改变
        sig_before = row["state_sig"]
        assert sig_before, row
        manager.jobs["hist00000001"]["records"][6]["status"] = "success"
        changed = batches.history_groups(admin, True, "")[0]
        assert changed["state_sig"] != sig_before, (sig_before, changed["state_sig"])
        # 两条记录状态互换（success 数不变）也要能被发现
        manager.jobs["hist00000001"]["records"][0]["status"] = "running"
        swapped = batches.history_groups(admin, True, "")[0]
        assert swapped["state_sig"] != changed["state_sig"], "状态互换不能靠计数发现，必须看指纹"
        print("  ✓ 状态指纹：单条变化与计数互相抵消的状态互换都能触发明细重载")

        # ---------- 3. 明细字段：管理员能分辨归属、时间与批次
        manager.jobs["hist00000001"]["records"][6]["status"] = "running"
        manager.jobs["hist00000001"]["records"][0]["status"] = "success"
        detail = batches.history_items("__ungrouped__", admin, 0, 50, "")
        assert detail["total"] == 10, detail["total"]
        by_tid = {item["template_id"]: item for item in detail["items"]}
        first = by_tid["001"]
        assert first["owner"] == "alice" and first["created_at"] > 0, first
        assert first["batch_no"] == "未分组" and first["job_status"] == "succeeded", first
        assert by_tid["010"]["owner"] == "bob", by_tid["010"]
        assert by_tid["010"]["template_name"] == "0010.mp4", by_tid["010"]
        print("  ✓ 明细字段：owner / created_at / batch_no / job_status 齐备，模板名从 URL 回退成文件名")

        # ---------- 4. 普通用户仍只看得到自己
        mine = batches.history_groups(alice, False, "")
        assert len(mine) == 1 and mine[0]["total"] == 9, mine
        mine_detail = batches.history_items("__ungrouped__", alice, 0, 50, "")
        assert mine_detail["total"] == 9, mine_detail["total"]
        print("  ✓ 权限边界：普通用户的分组与明细只含自己的 9 条")

        # ---------- 5. 按账户筛选（管理员）
        only_bob = batches.history_groups(admin, True, "bob")
        assert len(only_bob) == 1 and only_bob[0]["total"] == 1, only_bob
        only_bob_detail = batches.history_items("__ungrouped__", admin, 0, 50, "bob")
        assert only_bob_detail["total"] == 1, only_bob_detail
        print("  ✓ 账户筛选：分组卡片与明细同口径")

        # ---------- 6. 按账户拆未分组：虚拟 key 只存在于接口层
        split = batches.history_groups(admin, True, "", "owner")
        keys = {row["key"] for row in split}
        assert "__ungrouped__" not in keys, split
        assert keys == {"__ungrouped__:alice", "__ungrouped__:bob"}, split
        alice_group = next(row for row in split if row["key"] == "__ungrouped__:alice")
        bob_group = next(row for row in split if row["key"] == "__ungrouped__:bob")
        assert alice_group["total"] == 9 and alice_group["owner"] == "alice", alice_group
        assert bob_group["total"] == 1 and bob_group["owner"] == "bob", bob_group
        assert all(row["group_kind"] == "owner" and row["is_ungrouped"] for row in split), split
        bob_detail = batches.history_items("__ungrouped__:bob", admin, 0, 50, "")
        assert bob_detail["total"] == 1 and bob_detail["items"][0]["owner"] == "bob", bob_detail
        assert batches.history_items("__ungrouped__:bob", admin, 0, 50, "alice")["total"] == 0, \
            "账户筛选与虚拟组冲突时不能放宽到别的账户"
        try:
            batches.history_items("__ungrouped__:bob", alice, 0, 50, "")
        except BatchError as exc:
            assert exc.status == 403, exc
        else:
            raise AssertionError("普通用户不该能打开按账户拆出来的虚拟组")
        assert len(batches.history_groups(admin, True, "", "batch")) == 1, "按批次仍是一个大组"
        print("  ✓ 按账户拆组：虚拟 key / 明细 / 越权 / 筛选冲突")

        # ---------- 7. 统一按最近更新倒序：新试跑排在老批次前面
        batches.batches["batchOLD"] = {"id": "batchOLD", "batch_no": "OLD-001",
                                        "owner": "alice", "state": "locked"}
        old_job = new_job("hist00000003", "alice", [{"id": "011", "status": "success"}],
                          created_at=now - 9000, batch_ref="batchOLD")
        write_job(settings, old_job)
        manager.jobs[old_job["id"]] = old_job
        ordered = batches.history_groups(admin, True, "", "owner")
        assert ordered[-1]["key"] == "batchOLD", ordered
        assert ordered[0]["key"].startswith("__ungrouped__:"), ordered
        print("  ✓ 排序：最新的未分组试跑排在老批次前面")
    finally:
        manager.shutdown()
    print("  OK 管理员历史记录：状态口径 / 明细字段 / 指纹 / 权限 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
