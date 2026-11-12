#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生产数据镜像的表结构与连接设置（schema v1）。

设计原则（见 docs/production-data.md）：

* 这张库是**派生视图**，不是真源。真源永远是 `web-data/*.json`；删掉本库可以随时重建。
* 结构一旦发布就要当契约看待：外部脚本、Excel、Power BI 会按列名查询，
  所以加列要升 `SCHEMA_VERSION` 并写迁移（当前策略是版本不符就整库重建）。
* 路径一律相对 `meta.data_root` 保存（POSIX 分隔符），`data_root` 只在 `meta` 里存一次；
  不在数据目录下的路径原样保留并把 `path_external` 置 1，绝不伪造或重锚。
* 金额字段是「计费秒 × 单价」的估算，口径来自 `app/billing.py`；真实账单以阿里云为准。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 2
EXPORTER_VERSION = "1"

# 每次连接都要设的 pragma：WAL 让「只读查询」与「同步线程写入」不互斥；
# foreign_keys 打开后删 job 会级联删它的 records（增量删除靠这个）；
# busy_timeout 给导出线程留出等待窗口，避免 Windows 上偶发 locked。
_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=5000",
    "PRAGMA synchronous=NORMAL",
)

_DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- 一次生产任务（网页端「任务」）：一条 job 对应 N 个模板 → N 条 records。
CREATE TABLE IF NOT EXISTS jobs (
    id                TEXT PRIMARY KEY,
    owner             TEXT,
    status            TEXT,
    batch_id          TEXT,
    batch_ref         TEXT,
    category          TEXT,
    lane              TEXT,
    created_at        REAL,
    created_local     TEXT,
    day               TEXT,
    started_at        REAL,
    finished_at       REAL,
    prompt_id         TEXT,
    resolution        TEXT,
    duration          INTEGER,
    total             INTEGER,
    done              INTEGER,
    success_count     INTEGER,
    failed_count      INTEGER,
    unfinished_count  INTEGER,
    billable_seconds  REAL,
    estimated_cost    REAL,
    message           TEXT,
    out_dir           TEXT,
    image_path        TEXT,
    images_json       TEXT,
    tags_json         TEXT
);

-- 一条记录 = 一个模板的一次生成，是「成片清单」的最细粒度。
CREATE TABLE IF NOT EXISTS records (
    record_id          TEXT PRIMARY KEY,
    job_id             TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    seq                INTEGER,
    template_id        TEXT,
    template_name      TEXT,
    template_tags_json TEXT,
    template_url       TEXT,
    status             TEXT,
    provider           TEXT,
    model              TEXT,
    mode               TEXT,
    resolution         TEXT,
    file               TEXT,
    file_exists        INTEGER,
    path_external      INTEGER,
    billable_seconds   REAL,
    estimated_cost     REAL,
    cost_source        TEXT,
    seconds            REAL,
    error              TEXT,
    task_id            TEXT,
    request_id         TEXT,
    submitted_at       TEXT,
    submitted_ts       REAL,
    cos_key            TEXT,
    cos_url            TEXT,
    usage_json         TEXT,
    verify_json        TEXT,
    cos_tags_json      TEXT
);

CREATE TABLE IF NOT EXISTS batches (
    id                TEXT PRIMARY KEY,
    batch_no          TEXT,
    title             TEXT,
    owner             TEXT,
    state             TEXT,
    style_no          TEXT,
    category          TEXT,
    prompt_id         TEXT,
    resolution        TEXT,
    duration          INTEGER,
    total_items       INTEGER,
    done              INTEGER,
    failed            INTEGER,
    pending           INTEGER,
    producing         INTEGER,
    created_at        REAL,
    created_local     TEXT,
    updated_at        REAL,
    locked_at         REAL,
    closed_at         REAL,
    tags_json         TEXT,
    tag_filters_json  TEXT,
    tag_excludes_json TEXT,
    images_json       TEXT
);

CREATE TABLE IF NOT EXISTS batch_items (
    batch_id    TEXT NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
    template_id TEXT NOT NULL,
    seq         INTEGER,
    name        TEXT,
    state       TEXT,
    job_id      TEXT,
    tags_json   TEXT,
    error       TEXT,
    PRIMARY KEY (batch_id, template_id)
);

-- 定妆图 / 细节图（uploads/meta/<id>.json）。图片文件本身在磁盘上，这里只存索引。
CREATE TABLE IF NOT EXISTS uploads (
    id            TEXT PRIMARY KEY,
    owner         TEXT,
    name          TEXT,
    path          TEXT,
    size_mb       REAL,
    format        TEXT,
    width         INTEGER,
    height        INTEGER,
    has_alpha     INTEGER,
    created_at    REAL,
    created_local TEXT,
    path_external INTEGER
);

-- 按「账户 × 本地日期」预聚合：Excel / 报表直接查这张表，别自己写 GROUP BY 口径。
CREATE TABLE IF NOT EXISTS usage_daily (
    owner            TEXT NOT NULL,
    day              TEXT NOT NULL,
    jobs             INTEGER,
    items            INTEGER,
    success          INTEGER,
    failed           INTEGER,
    unfinished       INTEGER,
    timeout          INTEGER,
    billable_seconds REAL,
    estimated_cost   REAL,
    pending_jobs     INTEGER,
    PRIMARY KEY (owner, day)
);

CREATE INDEX IF NOT EXISTS idx_jobs_created   ON jobs(created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_owner     ON jobs(owner, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_status    ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_batch     ON jobs(batch_id);
CREATE INDEX IF NOT EXISTS idx_records_job    ON records(job_id);
CREATE INDEX IF NOT EXISTS idx_records_status ON records(status);
CREATE INDEX IF NOT EXISTS idx_records_tpl    ON records(template_id);
CREATE INDEX IF NOT EXISTS idx_records_cos    ON records(cos_key);

-- 扁平视图：一行一条成片，报表/LLM 最常用，省得每次手写 JOIN。
CREATE VIEW IF NOT EXISTS v_production AS
SELECT r.record_id, r.seq, r.job_id, j.owner, j.day, j.created_local, j.category,
       j.batch_id, j.batch_ref, r.template_id, r.template_name, r.status,
       r.file, r.file_exists, r.path_external, r.cos_key, r.cos_url,
       r.billable_seconds, r.estimated_cost, r.cost_source, r.error
FROM records r JOIN jobs j ON j.id = r.job_id;

CREATE VIEW IF NOT EXISTS v_usage_daily AS
SELECT owner, day, jobs, items, success, failed, unfinished, timeout,
       pending_jobs, billable_seconds, estimated_cost,
       CASE WHEN items > 0 THEN ROUND(CAST(success AS REAL) / items, 4) ELSE 0.0 END
         AS success_rate,
       CASE WHEN success > 0 THEN ROUND(estimated_cost / success, 4) ELSE NULL END
         AS avg_cost_per_video
FROM usage_daily;

CREATE VIEW IF NOT EXISTS v_failures AS
SELECT record_id, job_id, owner, day, template_id, template_name, status, error,
       billable_seconds, estimated_cost, cost_source
FROM v_production
WHERE status IN ('failed', 'invalid', 'expired', 'canceled', 'unknown', 'timeout', 'skipped');
"""


def connect(path: Path) -> sqlite3.Connection:
    """打开（或创建）镜像库，套用固定的 pragma。

    `check_same_thread=False`：同步线程与 CLI/导出共用同一条连接，整个库的读写都在
    `Mirror._lock` 里串行，不靠 sqlite 的线程检查兜底。
    """
    conn = sqlite3.connect(str(path), timeout=15.0, check_same_thread=False)
    for pragma in _PRAGMAS:
        try:
            conn.execute(pragma)
        except sqlite3.Error:
            pass
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if not row:
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                     (str(SCHEMA_VERSION),))
    conn.commit()


def inspect(conn: sqlite3.Connection) -> str:
    """返回 ok / missing / stale / corrupt，供同步线程决定是否重建。"""
    try:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    except sqlite3.DatabaseError:
        return "corrupt"
    if not tables:
        return "missing"
    try:
        check = conn.execute("PRAGMA quick_check").fetchone()
    except sqlite3.DatabaseError:
        return "corrupt"
    if not check or str(check[0]).strip().lower() != "ok":
        return "corrupt"
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    except sqlite3.DatabaseError:
        return "stale"
    if not row or str(row[0]) != str(SCHEMA_VERSION):
        return "stale"
    return "ok"


def upsert(conn: sqlite3.Connection, table: str, row: dict) -> None:
    """按主键整行写入/覆盖。列名来自本模块定义的常量，不接受外部输入。

    注意 `INSERT OR REPLACE` 会先删后插，父表（jobs/batches）上会触发 FK 级联；
    调用方负责在覆盖 job 之后重新插入它的 records（core 里就是这个顺序）。
    """
    if not row:
        return
    columns = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(f"INSERT OR REPLACE INTO {table} ({columns}) VALUES ({marks})",
                 tuple(row.values()))
