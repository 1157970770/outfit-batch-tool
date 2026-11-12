#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生产数据镜像的同步器：把 web-data 下的 JSON 增量搬进一份 SQLite 只读库。

为什么是「进程内线程」而不是独立进程/定时任务：

* 写入方是网页服务自己（`JobManager` / `BatchManager`），JSON 落盘用 `.tmp` + `os.replace`
  原子替换；镜像在同一个进程里读，天然不会读到半个文件；
* 独立进程要么定时全量扫（更贵）、要么在 Windows 上跟服务抢文件句柄，没有收益。

同步策略：每 `poll_seconds` 扫一遍 `jobs/`、`batches/`、`uploads/meta/` 的
`(mtime_ns, size)`，与内存里的快照比对得到「新增/改过/删掉」三组文件：

* 改过的 job：整条覆盖，并**整批重插**它名下的 records（records 是 job 的子集，
  逐条 diff 没有意义，而且 job 一旦变化，记录状态往往会成片变化）；
* 删掉的 job：`DELETE FROM jobs`，records 靠外键级联一起走；
* 有任何一个文件变了，就整表重算一次 `usage_daily`（236 jobs / 1141 records 的规模
  下不到 100ms，比维护增量更省心，也绝不会漂移）。

容错：单个 JSON 解析失败时跳过该文件（保留库里上一次成功同步的行），把错误写进
`meta.last_error` 与 `logs/data_mirror.log`；整轮异常只记日志，绝不让同步线程把服务拖垮。

新鲜度：库最多落后 `poll_seconds` 秒；停机期间库不会更新，消费方应看 `meta.built_at`。
"""
from __future__ import annotations

import csv
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

from app import billing
from app.mirror import model, schema

_TABLES = ("jobs", "records", "batches", "batch_items", "uploads", "usage_daily")


def _empty_bucket() -> dict:
    return {"jobs": 0, "items": 0, "success": 0, "failed": 0, "unfinished": 0,
            "timeout": 0, "billable_seconds": 0.0, "estimated_cost": 0.0,
            "pending_jobs": 0}


class Mirror:
    """把 `settings.data_dir` 下的生产数据同步进 `mirror/production.sqlite`。

    `settings` 只需要两个属性：`data_dir`（Path）与可调用的 `unit_price(resolution)`
    ——这样测试可以传一个极简对象，不必构造完整 WebSettings。
    """

    def __init__(self, settings, *, poll_seconds: float = 5.0, csv_dir: str = ""):
        self.settings = settings
        self.data_root = Path(getattr(settings, "data_dir"))
        self.root = self.data_root / "mirror"
        self.db_path = self.root / "production.sqlite"
        self.poll_seconds = max(1.0, float(poll_seconds or 5.0))
        self.csv_dir = Path(csv_dir) if csv_dir else None
        lookup = getattr(settings, "unit_price", None)
        self.unit_price = lookup if callable(lookup) else (lambda _res: (None, "missing"))
        self._conn: sqlite3.Connection | None = None
        self._state: dict[str, tuple[int, int]] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self.last_error = ""

    # --------------------------------------------------------------- 生命周期
    def start(self) -> "Mirror":
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="data-mirror", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.Error:
                    pass
                self._close()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.sync_once()
            except Exception as exc:            # 镜像失败绝不能影响生产服务
                self.last_error = f"{type(exc).__name__}: {exc}"
                self._log(f"同步失败：{self.last_error}")
            self._stop.wait(self.poll_seconds)

    # ------------------------------------------------------------------ 同步
    def sync_once(self, *, force: bool = False) -> bool:
        """比对并套用增量；返回本轮是否有写入。`force=True` 时无条件重算一遍。"""
        with self._lock:
            conn, rebuilt = self._ensure_ready()
            scan = self._scan()
            changed = sorted(path for path, sig in scan.items()
                             if self._state.get(path) != sig)
            removed = sorted(path for path in self._state if path not in scan)
            if not changed and not removed and not force and not rebuilt:
                return False
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError:
                conn.execute("BEGIN")
            try:
                for path in changed:
                    self._apply(conn, path)
                for path in removed:
                    self._drop(conn, path)
                self._recompute(conn)
                self._write_meta(conn)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            self._state = scan
            if self.csv_dir:
                try:
                    self.export_csv()
                except OSError as exc:
                    self._log(f"导出 CSV 失败：{exc}")
            return True

    def sync_full(self) -> bool:
        """忽略内存快照，整库重扫一遍（重建后调用，或 CLI `--once`）。"""
        self._state = {}
        return self.sync_once(force=True)

    def _scan(self) -> dict[str, tuple[int, int]]:
        found: dict[str, tuple[int, int]] = {}
        for folder in (self.data_root / "jobs", self.data_root / "batches",
                       self.data_root / "uploads" / "meta"):
            try:
                with os.scandir(folder) as entries:
                    for entry in entries:
                        if not entry.name.endswith(".json"):
                            continue
                        try:
                            stat = entry.stat()
                        except OSError:
                            continue
                        if not entry.is_file():
                            continue
                        found[entry.path] = (stat.st_mtime_ns, stat.st_size)
            except FileNotFoundError:
                continue
            except OSError as exc:
                self._log(f"扫描 {folder} 失败：{exc}")
        return found

    def _apply(self, conn: sqlite3.Connection, path: str) -> None:
        target = Path(path)
        kind = _kind_of(target)
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self.last_error = f"{target.name}: {exc}"
            self._log(f"跳过无法解析的文件 {target}：{exc}")
            return
        if not isinstance(data, dict):
            self.last_error = f"{target.name}: 顶层不是对象"
            return
        if kind == "job":
            row, records = model.job_rows(data, self.data_root, self.unit_price)
            if not row["id"]:
                row["id"] = target.stem
            for rec in records:
                rec["job_id"] = row["id"]
            schema.upsert(conn, "jobs", row)          # REPLACE 会级联清掉旧 records
            for rec in records:
                schema.upsert(conn, "records", rec)
        elif kind == "batch":
            row, items = model.batch_rows(data)
            if not row["id"]:
                row["id"] = target.stem
            conn.execute("DELETE FROM batch_items WHERE batch_id=?", (row["id"],))
            schema.upsert(conn, "batches", row)
            for item in items:
                item["batch_id"] = row["id"]
                schema.upsert(conn, "batch_items", item)
        else:
            row = model.upload_row(data, self.data_root)
            if not row["id"]:
                row["id"] = target.stem
            schema.upsert(conn, "uploads", row)

    def _drop(self, conn: sqlite3.Connection, path: str) -> None:
        target = Path(path)
        kind = _kind_of(target)
        if kind == "job":
            conn.execute("DELETE FROM jobs WHERE id=?", (target.stem,))
        elif kind == "batch":
            conn.execute("DELETE FROM batches WHERE id=?", (target.stem,))
        else:
            conn.execute("DELETE FROM uploads WHERE id=?", (target.stem,))

    def _recompute(self, conn: sqlite3.Connection) -> None:
        """整表重算 usage_daily。

        口径与 `JobManager.usage_for()` 逐条对齐（含 timeout 单列、running 记未完成、
        其他非成功终态记失败）；金额直接取 records.estimated_cost，
        而它来自 `app/billing.py::record_cost`，所以两边是同一套算法。
        """
        conn.execute("DELETE FROM usage_daily")
        buckets: dict[tuple[str, str], dict] = {}
        for owner, day, status in conn.execute("SELECT owner, day, status FROM jobs"):
            bucket = buckets.setdefault((owner or "", day or ""), _empty_bucket())
            bucket["jobs"] += 1
            if status in ("queued", "running"):
                bucket["pending_jobs"] += 1
        for owner, day, status, seconds, cost in conn.execute(
                "SELECT j.owner, j.day, r.status, r.billable_seconds, r.estimated_cost "
                "FROM records r JOIN jobs j ON j.id = r.job_id"):
            bucket = buckets.setdefault((owner or "", day or ""), _empty_bucket())
            bucket["items"] += 1
            state = str(status or "")
            if state == "success":
                bucket["success"] += 1
            elif state == "timeout":
                bucket["failed"] += 1
                bucket["timeout"] += 1
            elif state in billing.FAILED_RECORD_STATES:
                bucket["failed"] += 1
            elif state == "running":
                bucket["unfinished"] += 1
            bucket["billable_seconds"] += float(seconds or 0)
            bucket["estimated_cost"] += float(cost or 0)
        for (owner, day), bucket in sorted(buckets.items()):
            conn.execute(
                "INSERT OR REPLACE INTO usage_daily (owner, day, jobs, items, success, "
                "failed, unfinished, timeout, billable_seconds, estimated_cost, pending_jobs) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (owner, day, bucket["jobs"], bucket["items"], bucket["success"],
                 bucket["failed"], bucket["unfinished"], bucket["timeout"],
                 round(bucket["billable_seconds"], 2), round(bucket["estimated_cost"], 2),
                 bucket["pending_jobs"]))

    def _write_meta(self, conn: sqlite3.Connection) -> None:
        counts = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in _TABLES}
        now = time.time()
        values = {
            "schema_version": str(schema.SCHEMA_VERSION),
            "exporter_version": schema.EXPORTER_VERSION,
            "built_at": f"{now:.3f}",
            "built_at_local": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
            "data_root": str(self.data_root),
            "last_error": self.last_error,
            "counts_json": json.dumps(counts, ensure_ascii=False, sort_keys=True),
        }
        for key, value in values.items():
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    # ------------------------------------------------------------------ 维护
    def rebuild(self, *, corrupt: bool = False) -> None:
        """删库重建（corrupt=True 时先把坏库改名留档，便于事后分析）。"""
        with self._lock:
            self._close()
            self.root.mkdir(parents=True, exist_ok=True)
            if corrupt and self.db_path.exists():
                stamp = time.strftime("%Y%m%d-%H%M%S")
                try:
                    self.db_path.replace(self.root / f"production.sqlite.corrupt-{stamp}")
                except OSError:
                    pass
            for suffix in ("", "-wal", "-shm"):
                junk = Path(str(self.db_path) + suffix)
                try:
                    junk.unlink()
                except OSError:
                    pass
            self._state = {}
            self._connect()          # 建空库 + 建表；不要走 _ensure_ready，会递归回 rebuild
            self.sync_full()

    def snapshot(self, out_path) -> Path:
        """导出一份自包含快照（`VACUUM INTO`）：跨机器拷贝/给 BI 工具用时最稳。"""
        with self._lock:
            conn, _ = self._ensure_ready()
            out = Path(out_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            if out.exists():
                out.unlink()
            conn.commit()
            conn.execute("VACUUM INTO ?", (str(out),))
            return out

    def verify(self) -> dict:
        with self._lock:
            conn, _ = self._ensure_ready()
            status = schema.inspect(conn)
            report = {"ok": status == "ok", "status": status, "db": str(self.db_path),
                      "meta": {}, "counts": {}}
            try:
                report["meta"] = dict(conn.execute("SELECT key, value FROM meta").fetchall())
                report["counts"] = {table: conn.execute(
                    f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in _TABLES}
            except sqlite3.DatabaseError as exc:
                report["error"] = str(exc)
            return report

    def export_csv(self, target=None) -> list[Path]:
        """导出 jobs / records / usage_daily 三张表的 CSV（UTF-8 带 BOM，Excel 直接打开）。"""
        folder = Path(target) if target else self.csv_dir
        if not folder:
            return []
        with self._lock:
            conn, _ = self._ensure_ready()
            folder.mkdir(parents=True, exist_ok=True)
            written: list[Path] = []
            for table in ("jobs", "records", "usage_daily"):
                cursor = conn.execute(f"SELECT * FROM {table}")
                columns = [item[0] for item in cursor.description]
                path = folder / f"{table}.csv"
                with path.open("w", encoding="utf-8-sig", newline="") as handle:
                    writer = csv.writer(handle)
                    writer.writerow(columns)
                    writer.writerows(cursor.fetchall())
                written.append(path)
            return written

    # ------------------------------------------------------------------ 内部
    def _ensure_ready(self) -> tuple[sqlite3.Connection, bool]:
        """确保库可用；返回 (连接, 本轮是否重建过)。重建本身就是一次全量同步。"""
        try:
            conn = self._connect()
            status = schema.inspect(conn)
        except sqlite3.DatabaseError as exc:
            # 文件不是数据库 / 头部损坏：连建表都跑不动，按损坏处理
            self._log(f"镜像库打不开（{exc}），按损坏处理并重建")
            self._close()
            self.rebuild(corrupt=True)
            return self._conn, True  # type: ignore[return-value]
        if status == "ok":
            return conn, False
        self._log(f"镜像库状态 {status}，重建：{self.db_path}")
        self.rebuild(corrupt=(status == "corrupt"))
        return self._conn, True  # type: ignore[return-value]

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self.root.mkdir(parents=True, exist_ok=True)
            conn = schema.connect(self.db_path)
            try:
                schema.ensure_schema(conn)
            except sqlite3.DatabaseError:
                # 先关掉再抛：Windows 上占着句柄改名会失败，rebuild 要能挪走坏库
                conn.close()
                raise
            self._conn = conn
        return self._conn

    def _close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None

    def _log(self, message: str) -> None:
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n"
        try:
            path = self.data_root / "logs" / "data_mirror.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError:
            pass


def _kind_of(path: Path) -> str:
    parent = path.parent.name
    if parent == "jobs":
        return "job"
    if parent == "batches":
        return "batch"
    return "upload"


def start_mirror(settings) -> Mirror | None:
    """按 `server/web.json` 的 `data_mirror` 段启动镜像线程；关闭时返回 None。

    配置（全部可选）::

        "data_mirror": {"enabled": true, "poll_seconds": 5, "csv_dir": ""}

    `enabled=false` 是给运维的一键开关：重启后不再写库，库文件留着不影响任何现有功能。
    """
    raw = getattr(settings, "web", None)
    options = raw.get("data_mirror") if isinstance(raw, dict) else None
    if not isinstance(options, dict):
        options = {}
    enabled = options.get("enabled", True)
    if enabled is False or str(enabled).strip().lower() in ("false", "0", "no", "off"):
        return None
    mirror = Mirror(settings,
                    poll_seconds=options.get("poll_seconds", 5),
                    csv_dir=options.get("csv_dir", "") or "")
    mirror.start()
    return mirror
