#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生产数据镜像（SQLite）回归测试：离线跑，不联网、不花钱、不碰真实 web-data。

覆盖的验收点：

1. 行数与相对路径：jobs/records/batches/batch_items/uploads 都能落到库，路径相对 data_root；
2. 口径对平：`usage_daily` 与 `JobManager.usage_for()` 按人逐项相等（金额精确到分）；
3. 脱敏：`cos_url` 去掉签名 query，错误文本里的 sk- Key 被替换；
4. 增量：改一个 job → 整批重插 records 不留重复；删文件 → 行级联消失；
5. 容错：坏 JSON 跳过且保留上一次的行；schema_version 不符/库损坏 → 自动重建（坏库改名留档）；
6. 并发：同步写入的同时用只读连接查询不报 locked；
7. `--csv` 导出与 `--rebuild` 幂等。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _tmpdir  # noqa: E402
from app.mirror import model, schema  # noqa: E402
from app.mirror.core import Mirror  # noqa: E402
from app.web.jobs import JobManager  # noqa: E402

UNIT_PRICE = 0.3          # 元/秒，720P
BASE_TS = 1794000000.0    # 固定时间戳，避免测试跨天抖动


def check(cond: bool, message) -> None:
    if not cond:
        raise AssertionError(message)


def unit_price(resolution: str) -> tuple[float | None, str]:
    return (UNIT_PRICE, "config") if str(resolution or "").upper() == "720P" else (None, "missing")


class StubSettings:
    """镜像只需要 data_dir + unit_price；报表对平需要 users 与并发上限。"""

    def __init__(self, root: Path):
        self.data_dir = root
        self.web = {}
        self.unit_price = unit_price
        self.users = {"alice": {"role": "user", "enabled": True},
                      "bob": {"role": "user", "enabled": True}}
        self.max_active_jobs = 2
        self.max_active_small_jobs = 1
        self.max_active_per_user = 2


# ---------------------------------------------------------------------------
# 造数据
# ---------------------------------------------------------------------------

def _job(job_id: str, owner: str, status: str, records: list[dict], **extra) -> dict:
    job = {
        "id": job_id, "owner": owner, "status": status,
        "created_at": BASE_TS, "started_at": BASE_TS + 1,
        "finished_at": BASE_TS + 60 if status == "done" else None,
        "prompt_id": "p03", "resolution": "720P", "duration": 7,
        "total": len(records), "done": sum(1 for r in records if r.get("status") == "success"),
        "images": [], "records": records, "message": "",
        "batch_ref": "", "tags": {"top": "LL-001"}, "category": "换装",
        "style_no": "", "lane": "main",
    }
    job.update(extra)
    return job


def build_area(root: Path) -> None:
    """在 root 下造一份最小但结构完整的 web-data。"""
    for name in ("jobs", "batches", "uploads/meta", "uploads/ab", "output/WEB-A"):
        (root / name).mkdir(parents=True, exist_ok=True)
    out_file = root / "output" / "WEB-A" / "101.mp4"
    out_file.write_bytes(b"fake-mp4")

    a = _job("jobA", "alice", "done", [
        {"id": "101", "record_id": "r-101", "status": "success",
         "billable_seconds": 7.5, "estimated_cost": 3.6,
         "file": str(out_file), "template": "https://cos.example/101.mp4",
         "cos": {"key": "output/101.mp4",
                 "url": "https://cos.example/101.mp4?q-ak=AKIDxxxx&q-signature=deadbeef"}},
        {"id": "102", "record_id": "r-102", "status": "success",
         "billable_seconds": 7.5, "estimated_cost": 3.6,
         "file": str(root / "output" / "WEB-A" / "102.mp4")},
        {"id": "103", "record_id": "r-103", "status": "failed",
         "billable_seconds": 0, "estimated_cost": 0,
         "error": "鉴权失败 sk-abcdefghijklmnopqrst"},
    ])
    b = _job("jobB", "bob", "done", [
        {"id": "201", "record_id": "r-201", "status": "running"},
        {"id": "202", "record_id": "r-202", "status": "queued"},
    ])
    c = _job("jobC", "alice", "done", [
        {"id": "301", "record_id": "r-301", "status": "timeout",
         "billable_seconds": 7.5, "estimated_cost": None},
    ])
    # 没有记录、只在任务级「未完成」的 job：验证 pending_jobs 与 usage_for 的口径
    d = _job("jobD", "bob", "queued", [])
    for job in (a, b, c, d):
        (root / "jobs" / f"{job['id']}.json").write_text(
            json.dumps(job, ensure_ascii=False), encoding="utf-8")

    batch = {
        "id": "batchX", "batch_no": "WEB-20261112-001", "title": "测试批次",
        "owner": "alice", "state": "locked", "style_no": "LL-001", "category": "换装",
        "prompt_id": "p03", "resolution": "720P", "duration": 7,
        "created_at": BASE_TS, "updated_at": BASE_TS + 10,
        "items": [
            {"template_id": "101", "name": "0101.mp4", "state": "done", "job_id": "jobA"},
            {"template_id": "102", "name": "0102.mp4", "state": "pending"},
            {"template_id": "103", "name": "0103.mp4", "state": "producing"},
        ],
    }
    (root / "batches" / "batchX.json").write_text(
        json.dumps(batch, ensure_ascii=False), encoding="utf-8")

    meta = {
        "id": "upl1", "owner": "alice", "name": "定妆图.png",
        "path": str(root / "uploads" / "ab" / "upl1.png"),
        "created_at": BASE_TS,
        "info": {"size_mb": 0.5, "format": "PNG", "width": 1024, "height": 1024,
                 "has_alpha": False, "path": str(root / "uploads" / "ab" / "upl1.png")},
    }
    (root / "uploads" / "meta" / "upl1.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    (root / "uploads" / "ab" / "upl1.png").write_bytes(b"fake-png")


def make_mirror(root: Path, **kwargs) -> Mirror:
    return Mirror(StubSettings(root), poll_seconds=1, **kwargs)


def rows(conn: sqlite3.Connection, sql: str, args=()) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(sql, args).fetchall()


# ---------------------------------------------------------------------------
# 1. 行数与路径
# ---------------------------------------------------------------------------

def test_counts_and_paths(root: Path) -> None:
    build_area(root)
    mirror = make_mirror(root)
    mirror.sync_full()
    conn = mirror._conn

    counts = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
              for table in ("jobs", "records", "batches", "batch_items", "uploads")}
    check(counts["jobs"] == 4, counts)
    check(counts["records"] == 6, counts)
    check(counts["batches"] == 1, counts)
    check(counts["batch_items"] == 3, counts)
    check(counts["uploads"] == 1, counts)

    record = rows(conn, "SELECT * FROM records WHERE record_id='r-101'")[0]
    check(record["file"] == "output/WEB-A/101.mp4", record["file"])
    check(record["file_exists"] == 1, "刚造出来的成片文件应存在")
    check(record["path_external"] == 0, "路径在 data_root 下，不该标 external")
    check(record["cost_source"] == "record", record["cost_source"])

    missing = rows(conn, "SELECT * FROM records WHERE record_id='r-102'")[0]
    check(missing["file_exists"] == 0, "磁盘上没有的成片要标 0")
    # 派生金额：timeout 那条没有 estimated_cost，按 7.5s × 0.3 回算
    derived = rows(conn, "SELECT * FROM records WHERE record_id='r-301'")[0]
    check(derived["cost_source"] == "derived", derived["cost_source"])
    check(abs(derived["estimated_cost"] - 2.25) < 1e-9, derived["estimated_cost"])

    job = rows(conn, "SELECT * FROM jobs WHERE id='jobA'")[0]
    check(job["success_count"] == 2 and job["failed_count"] == 1, dict(job))
    check(job["day"] == model.local_parts(BASE_TS)[1], job["day"])

    batch = rows(conn, "SELECT * FROM batches WHERE id='batchX'")[0]
    check(batch["total_items"] == 3, batch["total_items"])
    check(batch["done"] == 1 and batch["pending"] == 1 and batch["producing"] == 1, dict(batch))

    upload = rows(conn, "SELECT * FROM uploads WHERE id='upl1'")[0]
    check(upload["path"] == "uploads/ab/upl1.png", upload["path"])
    check(upload["width"] == 1024 and upload["has_alpha"] == 0, dict(upload))

    meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    check(meta["schema_version"] == str(schema.SCHEMA_VERSION), meta)
    check(meta["data_root"] == str(root), meta["data_root"])


# ---------------------------------------------------------------------------
# 2. 口径对平（这一条是整件事的核心：金额不能两个算法）
# ---------------------------------------------------------------------------

def test_usage_matches_jobmanager(root: Path) -> None:
    build_area(root)
    mirror = make_mirror(root)
    mirror.sync_full()

    manager = JobManager(StubSettings(root), {})
    manager.stop_event.set()
    with manager.changed:
        manager.changed.notify_all()
    manager.dispatcher.join(timeout=3)
    check(not manager.dispatcher.is_alive(), "派发线程没停下来，无法对平")
    stats = manager.usage_for()

    conn = mirror._conn
    stored = {row["owner"]: dict(row) for row in
              rows(conn, "SELECT * FROM usage_daily")}
    check(set(stored) == {"alice", "bob"}, sorted(stored))
    for account in stats["accounts"]:
        owner = account["owner"]
        if owner not in stored:
            check(account["jobs"] == 0, f"{owner} 没有任务却被记了 {account['jobs']} 个")
            continue
        got = stored[owner]
        for key in ("jobs", "items", "success", "failed", "unfinished", "timeout"):
            check(int(got[key]) == int(account[key]),
                  f"{owner}.{key}: 镜像 {got[key]} != usage_for {account[key]}")
        check(abs(got["billable_seconds"] - account["billable_seconds"]) < 0.011,
              f"{owner} 计费秒不一致：{got['billable_seconds']} vs {account['billable_seconds']}")
        check(abs(got["estimated_cost"] - account["estimated_cost"]) < 0.011,
              f"{owner} 金额不一致：{got['estimated_cost']} vs {account['estimated_cost']}")

    # 视图也要能用
    view = rows(conn, "SELECT * FROM v_usage_daily WHERE owner='alice'")[0]
    check(abs(view["estimated_cost"] - 9.45) < 0.011, view["estimated_cost"])
    check(abs(view["success_rate"] - 0.5) < 1e-9, view["success_rate"])
    flat = rows(conn, "SELECT * FROM v_production WHERE job_id='jobA'")
    check(len(flat) == 3, len(flat))


# ---------------------------------------------------------------------------
# 3. 脱敏
# ---------------------------------------------------------------------------

def test_secrets_are_stripped(root: Path) -> None:
    build_area(root)
    mirror = make_mirror(root)
    mirror.sync_full()
    conn = mirror._conn
    record = rows(conn, "SELECT * FROM records WHERE record_id='r-101'")[0]
    check("?" not in record["cos_url"], record["cos_url"])
    check(record["cos_url"] == "https://cos.example/101.mp4", record["cos_url"])
    failed = rows(conn, "SELECT * FROM records WHERE record_id='r-103'")[0]
    check("sk-abc" not in failed["error"], failed["error"])
    check("已隐藏" in failed["error"], failed["error"])
    dump = "\n".join(str(value) for row in
                     conn.execute("SELECT * FROM records").fetchall() for value in row)
    check("q-signature" not in dump, "签名参数漏进库了")


# ---------------------------------------------------------------------------
# 4. 增量改 / 删
# ---------------------------------------------------------------------------

def test_incremental_update_and_delete(root: Path) -> None:
    build_area(root)
    mirror = make_mirror(root)
    mirror.sync_full()
    conn = mirror._conn
    check(conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 6, "初始记录数不对")

    job_path = root / "jobs" / "jobA.json"
    job = json.loads(job_path.read_text(encoding="utf-8"))
    job["status"] = "done"
    job["records"][2]["status"] = "success"          # 失败 → 成功
    job["records"][2]["billable_seconds"] = 7.5
    job["records"][2]["estimated_cost"] = 3.6
    job["records"].append({"id": "104", "record_id": "r-104", "status": "success",
                           "billable_seconds": 7.5, "estimated_cost": 3.6})
    job_path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
    # 文件大小变了但 mtime 精度可能相同：显式往后拨，模拟真实编辑
    future = time.time() + 5
    os.utime(job_path, (future, future))

    check(mirror.sync_once(), "改了 job 应该有变更")
    check(conn.execute("SELECT COUNT(*) FROM records WHERE job_id='jobA'").fetchone()[0] == 4,
          "整批重插后不应有重复记录")
    check(conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 7, "总记录数不对")
    check(rows(conn, "SELECT * FROM records WHERE record_id='r-103'")[0]["status"] == "success",
          "改过的记录要更新")

    # 无变化时不应该写库
    check(mirror.sync_once() is False, "没有变化却报告了写入")

    # 删 job：记录必须级联消失
    (root / "jobs" / "jobB.json").unlink()
    check(mirror.sync_once(), "删文件应该触发同步")
    check(conn.execute("SELECT COUNT(*) FROM jobs WHERE id='jobB'").fetchone()[0] == 0, "job 没删掉")
    check(conn.execute("SELECT COUNT(*) FROM records WHERE job_id='jobB'").fetchone()[0] == 0,
          "records 没有随 job 级联删除")
    check(conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 5, "级联删多了或少了")


# ---------------------------------------------------------------------------
# 5. 坏 JSON 容错
# ---------------------------------------------------------------------------

def test_bad_json_keeps_previous_rows(root: Path) -> None:
    build_area(root)
    mirror = make_mirror(root)
    mirror.sync_full()
    conn = mirror._conn
    before = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]

    bad = root / "jobs" / "jobA.json"
    bad.write_text('{"id": "jobA", "records": [', encoding="utf-8")
    mirror.sync_once()

    after = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    check(after == before, f"坏文件不该动已有数据：{before} → {after}")
    check(conn.execute("SELECT COUNT(*) FROM jobs WHERE id='jobA'").fetchone()[0] == 1,
          "解析失败时应保留上一次成功同步的行")
    meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    check("jobA.json" in meta.get("last_error", ""), meta.get("last_error"))


# ---------------------------------------------------------------------------
# 6. schema 版本不符 / 库损坏 → 自动重建
# ---------------------------------------------------------------------------

def test_schema_mismatch_rebuilds(root: Path) -> None:
    build_area(root)
    mirror = make_mirror(root)
    mirror.sync_full()
    mirror._conn.execute("UPDATE meta SET value='0' WHERE key='schema_version'")
    mirror._conn.commit()
    mirror._conn.close()
    mirror._conn = None
    mirror._state = {}

    check(mirror.sync_once(), "版本不符后应重建并写入")
    conn = mirror._conn
    check(schema.inspect(conn) == "ok", schema.inspect(conn))
    check(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 4, "重建后行数不对")


def test_corrupt_db_is_quarantined(root: Path) -> None:
    build_area(root)
    mirror = make_mirror(root)
    mirror.sync_full()
    mirror._close()
    mirror._state = {}
    mirror.db_path.write_bytes(b"this is definitely not a sqlite database" * 100)

    mirror.sync_once()
    check(mirror._conn is not None, "损坏后没有重建")
    check(mirror._conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 4, "重建后行数不对")
    quarantined = list((root / "mirror").glob("production.sqlite.corrupt-*"))
    check(len(quarantined) == 1, f"坏库应改名留档，实际：{quarantined}")


# ---------------------------------------------------------------------------
# 7. 并发读（WAL：写入不能把只读查询挡住）
# ---------------------------------------------------------------------------

def test_concurrent_reader_during_sync(root: Path) -> None:
    build_area(root)
    mirror = make_mirror(root)
    mirror.sync_full()

    errors: list[str] = []
    stop = threading.Event()

    def reader() -> None:
        uri = "file:" + mirror.db_path.as_posix() + "?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        except sqlite3.Error as exc:
            errors.append(f"只读连接失败：{exc}")
            return
        try:
            while not stop.is_set():
                try:
                    conn.execute("SELECT COUNT(*) FROM v_production").fetchone()
                    conn.execute("SELECT * FROM v_usage_daily").fetchall()
                except sqlite3.Error as exc:
                    errors.append(f"查询失败：{exc}")
                    return
                time.sleep(0.01)
        finally:
            conn.close()

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    try:
        for index in range(12):
            path = root / "jobs" / "jobC.json"
            job = json.loads(path.read_text(encoding="utf-8"))
            job["message"] = f"第 {index} 次改动"
            path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
            future = time.time() + index + 1
            os.utime(path, (future, future))
            mirror.sync_once()
    finally:
        stop.set()
        thread.join(timeout=5)
    check(not errors, errors)


# ---------------------------------------------------------------------------
# 8. rebuild 幂等 + CSV 导出
# ---------------------------------------------------------------------------

def test_rebuild_idempotent_and_csv(root: Path) -> None:
    build_area(root)
    mirror = make_mirror(root)
    mirror.sync_full()
    first = {table: mirror._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
             for table in ("jobs", "records", "batches", "uploads", "usage_daily")}

    mirror.rebuild()
    second = {table: mirror._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
              for table in first}
    check(first == second, f"重建结果不一致：{first} vs {second}")

    folder = root / "csv"
    files = mirror.export_csv(folder)
    check(len(files) == 3, files)
    header = (folder / "usage_daily.csv").read_text(encoding="utf-8-sig").splitlines()[0]
    check("owner" in header and "estimated_cost" in header, header)
    check((folder / "records.csv").read_bytes().startswith(b"\xef\xbb\xbf"),
          "CSV 应为带 BOM 的 UTF-8，Excel 才不乱码")


def main() -> int:
    tmp = _tmpdir.tempdir("mirror-")
    cases = [
        ("行数与路径", test_counts_and_paths),
        ("口径对平", test_usage_matches_jobmanager),
        ("脱敏", test_secrets_are_stripped),
        ("增量改删", test_incremental_update_and_delete),
        ("坏 JSON 容错", test_bad_json_keeps_previous_rows),
        ("版本不符重建", test_schema_mismatch_rebuilds),
        ("坏库留档", test_corrupt_db_is_quarantined),
        ("并发读", test_concurrent_reader_during_sync),
        ("重建幂等与 CSV", test_rebuild_idempotent_and_csv),
    ]
    for name, func in cases:
        case_root = tmp / name
        case_root.mkdir(parents=True, exist_ok=True)
        func(case_root)
        print(f"  OK {name}")
    print("OK 生产数据镜像通过")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print(f"FAIL {exc}")
        sys.exit(1)
