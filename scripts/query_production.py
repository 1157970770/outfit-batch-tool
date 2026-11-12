#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读查询生产数据镜像（`web-data/mirror/production.sqlite`）。

给「不想写 Python 的脚本」和「LLM agent」用的最小入口：一条 SQL 进去，表格或 JSON 出来。
连接一律 `mode=ro`（只读），并且只放行 SELECT / WITH / PRAGMA / EXPLAIN，
避免手滑把镜像库写坏——真源是 JSON，镜像库坏了虽然能重建，但没必要。

示例：

    # 最近 20 条成片
    python scripts/query_production.py --sql \
        "SELECT owner, day, template_id, status, file FROM v_production ORDER BY day DESC LIMIT 20"

    # 按人按天的金额（直接查预聚合表，别自己写 GROUP BY）
    python scripts/query_production.py --sql \
        "SELECT * FROM v_usage_daily WHERE day >= '2026-11-01' ORDER BY day, owner" --json

    # 看表结构和行数
    python scripts/query_production.py --schema

    # 查生产区的库（跨区）
    python scripts/query_production.py --data-root "D:\\批量视频任务运行区\\web-data" --schema

退出码：0 成功；1 查询失败；2 用法/连接错误。SQL 只读限制在脚本层，不依赖数据库权限。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))   # 直接 `python scripts/xxx.py` 时也能 import app.*

_ALLOWED = ("select", "with", "pragma", "explain", "values")


def resolve_db(data_root: str = "", db_path: str = "") -> Path:
    if db_path:
        return Path(db_path).expanduser().resolve()
    if data_root:
        root = Path(data_root).expanduser().resolve()
    else:
        # 与 `python -m app.mirror` 一致：按本区 server/web.json 的 data_root 解析，
        # 设计区因此指向 web-data-dev，而不是设计区那个只剩桥的 web-data。
        from app.web.settings import load_settings
        root = Path(load_settings(base_dir=ROOT, load_templates=False).data_dir).resolve()
    return (root / "mirror" / "production.sqlite").resolve()


def connect_ro(path: Path) -> sqlite3.Connection:
    """只读打开；库不存在时给一句可操作的话，而不是 sqlite 的英文异常。"""
    if not path.exists():
        raise FileNotFoundError(
            f"镜像库不存在：{path}\n"
            f"先生成一次：python -m app.mirror --once --data-root \"{path.parent.parent}\"")
    uri = "file:" + path.as_posix() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=10.0)
    except sqlite3.OperationalError:
        # 只读打开失败（极少见），退回普通连接但仍然限制语句只读
        conn = sqlite3.connect(str(path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _first_keyword(sql: str) -> str:
    text = sql.strip()
    while text.startswith("--") or text.startswith("/*"):
        if text.startswith("--"):
            cut = text.find("\n")
            text = text[cut + 1:] if cut >= 0 else ""
        else:
            cut = text.find("*/")
            text = text[cut + 2:] if cut >= 0 else ""
        text = text.strip()
    return text.split(None, 1)[0].lower() if text else ""


def run_sql(conn: sqlite3.Connection, sql: str, *, as_json: bool = False,
            max_rows: int = 1000) -> int:
    if _first_keyword(sql) not in _ALLOWED:
        print("只允许 SELECT / WITH / PRAGMA / EXPLAIN / VALUES 语句（镜像库只读）",
              file=sys.stderr)
        return 2
    cursor = conn.execute(sql)
    if cursor.description is None:
        print("语句没有结果集", file=sys.stderr)
        return 2
    columns = [item[0] for item in cursor.description]
    rows = cursor.fetchmany(max_rows)
    if as_json:
        print(json.dumps([dict(zip(columns, row)) for row in rows],
                         ensure_ascii=False, indent=2, default=str))
        return 0
    widths = [len(str(name)) for name in columns]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = min(60, max(widths[index], len(str(value))))
    print("  ".join(str(name).ljust(widths[i]) for i, name in enumerate(columns)))
    print("  ".join("-" * widths[i] for i in range(len(columns))))
    for row in rows:
        print("  ".join(str(value)[:60].ljust(widths[i]) for i, value in enumerate(row)))
    if len(rows) == max_rows:
        print(f"（只显示前 {max_rows} 行，用 --max-rows 调整）")
    return 0


def show_schema(conn: sqlite3.Connection) -> int:
    print("== 表 / 视图 ==")
    for row in conn.execute(
            "SELECT type, name FROM sqlite_master WHERE type IN ('table','view') "
            "AND name NOT LIKE 'sqlite_%' ORDER BY type DESC, name"):
        count = ""
        if row["type"] == "table":
            try:
                count = conn.execute(f"SELECT COUNT(*) FROM \"{row['name']}\"").fetchone()[0]
            except sqlite3.DatabaseError:
                count = "?"
        print(f"  {row['type']:<5} {row['name']:<16} {count}")
    print("== 常用视图的列 ==")
    for view in ("v_production", "v_usage_daily", "v_failures"):
        try:
            columns = [item[1] for item in conn.execute(f"PRAGMA table_info({view})")]
        except sqlite3.DatabaseError:
            continue
        if columns:
            print(f"  {view}: {', '.join(columns)}")
    try:
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        print("== meta ==")
        for key in ("schema_version", "built_at_local", "data_root", "counts_json",
                    "last_error"):
            if key in meta:
                print(f"  {key}: {meta[key]}")
    except sqlite3.DatabaseError:
        pass
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="只读查询生产数据镜像")
    parser.add_argument("--sql", default="", help="要执行的只读 SQL")
    parser.add_argument("--schema", action="store_true", help="列出表/视图、行数与 meta")
    parser.add_argument("--json", action="store_true", help="以 JSON 数组输出")
    parser.add_argument("--max-rows", type=int, default=1000, help="最多返回多少行（默认 1000）")
    parser.add_argument("--db", default="", help="直接指定 production.sqlite 路径")
    parser.add_argument("--data-root", default="",
                        help="数据目录（默认本仓库 web-data；查生产区传运行区 web-data）")
    args = parser.parse_args(argv)

    if not args.sql and not args.schema:
        parser.print_help()
        return 2
    path = resolve_db(args.data_root, args.db)
    try:
        conn = connect_ro(path)
    except (OSError, sqlite3.Error) as exc:
        print(f"无法打开镜像库：{exc}", file=sys.stderr)
        return 2
    try:
        if args.schema:
            return show_schema(conn)
        return run_sql(conn, args.sql, as_json=args.json, max_rows=max(1, args.max_rows))
    except sqlite3.Error as exc:
        print(f"查询失败：{exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
