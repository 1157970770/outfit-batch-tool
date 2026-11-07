#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把每日生产数据（账户 × 日期）同步到飞书多维表格「每日消费表」。

为什么需要这个脚本：
    管理端「账户用量」页有完整口径，但每天要人工登录去看；
    本脚本把同样的数据自动落到飞书多维表格，团队在表里直接看每日生产/消费。

口径（与 app/web/jobs.py::usage_for 保持一致）：
    · 一行 = 一个账户 + 一天（本地时区，按 job.created_at 归日）；
    · 生产视频数量 = 当日该账户 status=success 的 record 条数；
    · 消费金额（元）= 当日该账户全部 record 的费用之和：
        rec.estimated_cost 有值用记录值（生成时落的账）；
        否则 billable_seconds × 单价(按 job.resolution)，
        单价读 server/config.json 的 wan.price_per_second，缺省用公开价
        480P 0.3 / 720P 0.6 / 1080P 1.2（app/web/settings.py 同款兜底）；
    · 当日没有生产的 enabled 账户也写 0 行（--skip-zero 可去掉），
      与用量页「含零账户」口径一致。

写入（upsert，幂等，跑多少次结果一样）：
    键 = 日期 + 账户；已存在的行比对后有变化才更新，缺的补建；
    建表自带的整行空白占位记录顺手清掉。

用法：
    python scripts/feishu_daily_report.py                    # 同步 D-3..D-1（默认回补三天）
    python scripts/feishu_daily_report.py --from 2026-10-28 --to 2026-11-07
    python scripts/feishu_daily_report.py --include-today    # 连今天也写（当日快照，次日会被修正）
    python scripts/feishu_daily_report.py --setup-fields     # 只建字段/清空行，不写数据
    python scripts/feishu_daily_report.py --dry-run          # 只打印聚合结果，不联网
    python scripts/feishu_daily_report.py --quiet            # 计划任务用：只写日志不打印

配置（server/feishu.json，不入库，两区各自放一份）：
    {
      "app_id": "cli_xxx", "app_secret": "xxx",
      "app_token": "bascnxxx", "table_id": "tblxxx",
      "fields": {"account": "账户", "date": "日期",
                 "count": "生产视频数量", "cost": "消费金额"}   // 可选，字段名可改
    }

区域解析：数据目录默认 <脚本所在仓库>/web-data；--data-root 指定后，
账号表/单价/飞书凭证一律取该数据目录所在区域的 server/*.json，
从设计区跑生产数据：--data-root "D:\\批量视频任务运行区\\web-data"。

计划任务：
    scripts/install_feishu_report_task.ps1（每天 08:30，pythonw --quiet）

退出码：0 成功 / 1 网络·接口失败 / 2 配置或参数问题。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 与 app/web/settings.py::DEFAULT_PRICE_PER_SECOND 相同的公开价兜底（元/计费秒）
DEFAULT_PRICE_PER_SECOND = {"480P": 0.3, "720P": 0.6, "1080P": 1.2}

DEFAULT_FIELD_NAMES = {"account": "账户", "date": "日期",
                       "count": "生产视频数量", "cost": "消费金额"}
DEFAULT_DAYS = 3          # 默认回补 D-3..D-1
LOCK_STALE_SECONDS = 6 * 3600
BATCH_CHUNK = 200         # 批量写每批条数上限（飞书单批上限 1000，留余量）


# ---------------------------------------------------------------------------
# 参数与配置
# ---------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="每日生产数据（账户×日期）→ 飞书多维表格")
    p.add_argument("--data-root", default="", help="数据目录（默认本区 web-data；指到运行区可从设计区跑生产数据）")
    p.add_argument("--state-dir", default="", help="锁/日志目录（默认 <数据目录>/logs；跨区跑数据时可指回本区，避免对数据区写权限依赖）")
    p.add_argument("--feishu-config", default="", help="飞书凭证 JSON（默认 <数据目录所在区>/server/feishu.json）")
    p.add_argument("--web-config", default="", help="账号表 JSON（默认 <数据目录所在区>/server/web.json）")
    p.add_argument("--api-config", default="", help="单价配置 JSON（默认 <数据目录所在区>/server/config.json）")
    p.add_argument("--from", dest="date_from", default="", help="起始日期 YYYY-MM-DD（含）")
    p.add_argument("--to", dest="date_to", default="", help="结束日期 YYYY-MM-DD（含）")
    p.add_argument("--days", type=int, default=DEFAULT_DAYS,
                   help=f"不指定 --from/--to 时回补最近 N 天（默认 {DEFAULT_DAYS}，不含今天）")
    p.add_argument("--include-today", action="store_true", help="把今天也纳入（当日快照，次日修正）")
    p.add_argument("--skip-zero", action="store_true", help="当日没有任何任务的账户不写 0 行")
    p.add_argument("--setup-fields", action="store_true", help="只建字段/清空行，不写数据")
    p.add_argument("--dry-run", action="store_true", help="只打印聚合结果，不联网不写入")
    p.add_argument("--quiet", action="store_true", help="只写日志不打印（计划任务用）")
    return p.parse_args(argv)


def area_root(args: argparse.Namespace) -> Path:
    """数据目录所在区域根（server/*.json 从这里取，保证从哪个区跑就读哪个区的配置）。"""
    data_root = Path(args.data_root).expanduser() if args.data_root else ROOT / "web-data"
    return data_root.parent if data_root.name == "web-data" else data_root.parent


def data_root_of(args: argparse.Namespace) -> Path:
    raw = Path(args.data_root).expanduser() if args.data_root else ROOT / "web-data"
    return raw


def read_json(path: Path) -> dict:
    # utf-8-sig：兼容 PowerShell 5.1 Set-Content -Encoding UTF8 写出的 BOM
    return json.loads(path.read_text(encoding="utf-8-sig"))


def load_users(args: argparse.Namespace) -> list[str]:
    path = Path(args.web_config).expanduser() if args.web_config else area_root(args) / "server" / "web.json"
    try:
        web = read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[警告] 读账号表失败（{path.name}: {exc}），将只写有任务的账户")
        return []
    users = web.get("users") if isinstance(web, dict) else None
    if not isinstance(users, dict):
        return []
    return [str(name) for name, rec in users.items()
            if not isinstance(rec, dict) or bool(rec.get("enabled", True))]


def load_price_map(args: argparse.Namespace) -> dict[str, float]:
    path = Path(args.api_config).expanduser() if args.api_config else area_root(args) / "server" / "config.json"
    prices = dict(DEFAULT_PRICE_PER_SECOND)
    try:
        cfg = read_json(path)
        raw = (cfg.get("wan") or {}).get("price_per_second")
    except (OSError, json.JSONDecodeError):
        raw = None
    if isinstance(raw, dict):
        for key, value in raw.items():
            if value not in (None, ""):
                try:
                    prices[str(key).strip().upper()] = float(value)
                except (TypeError, ValueError):
                    pass
    return prices


def load_feishu_config(args: argparse.Namespace) -> dict:
    path = Path(args.feishu_config).expanduser() if args.feishu_config \
        else area_root(args) / "server" / "feishu.json"
    try:
        cfg = read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"飞书凭证读取失败：{path}（{exc}）") from None
    for key in ("app_id", "app_secret", "app_token", "table_id"):
        if not str(cfg.get(key) or "").strip():
            raise RuntimeError(f"飞书凭证缺字段 {key}：{path}")
    return cfg


# ---------------------------------------------------------------------------
# 聚合：口径与 app/web/jobs.py::usage_for 一致
# ---------------------------------------------------------------------------

def record_cost(rec: dict, job: dict, prices: dict[str, float]) -> float:
    """单条记录费用：优先记录里落的账，否则按单价回算（与 jobs.py::_record_cost 相同）。"""
    raw = rec.get("estimated_cost")
    if raw not in (None, ""):
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    try:
        seconds = float(rec.get("billable_seconds") or 0)
    except (TypeError, ValueError):
        seconds = 0.0
    price = prices.get(str(job.get("resolution") or "").strip().upper())
    if seconds > 0 and price:
        return seconds * price
    return 0.0


def daterange(start: date, end: date):
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


def aggregate(jobs_dir: Path, start: date, end: date,
              users: list[str], prices: dict[str, float]) -> dict[str, dict[str, dict]]:
    """返回 {日期iso: {账户: {jobs, count, cost}}}，含零账户行。"""
    rows: dict[str, dict[str, dict]] = {}
    for day in daterange(start, end):
        rows[day.isoformat()] = {name: {"jobs": 0, "count": 0, "cost": 0.0}
                                 for name in users}
    files = sorted(jobs_dir.glob("*.json")) if jobs_dir.exists() else []
    for path in files:
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(job, dict):
            continue
        try:
            created = float(job.get("created_at") or 0)
        except (TypeError, ValueError):
            continue
        iso = datetime.fromtimestamp(created).date().isoformat()
        if iso not in rows:
            continue
        owner = str(job.get("owner") or "未知")
        row = rows[iso].setdefault(owner, {"jobs": 0, "count": 0, "cost": 0.0})
        row["jobs"] += 1
        for rec in job.get("records") or []:
            if str(rec.get("status") or "") == "success":
                row["count"] += 1
            row["cost"] += record_cost(rec, job, prices)
    for day_rows in rows.values():
        for row in day_rows.values():
            row["cost"] = round(row["cost"], 2)
    return rows


# ---------------------------------------------------------------------------
# 飞书开放平台（仅用到的几个接口，stdlib 实现）
# ---------------------------------------------------------------------------

class FeishuError(RuntimeError):
    pass


class Feishu:
    """tenant_access_token + 多维表格字段/记录接口，带 3 次重试。"""

    def __init__(self, app_id: str, app_secret: str, app_token: str, table_id: str,
                 base_url: str = "https://open.feishu.cn"):
        self.app_id = app_id
        self.app_secret = app_secret
        self.app_token = app_token
        self.table_id = table_id
        self.base = base_url.rstrip("/")
        self._token = ""
        self._token_expire = 0.0

    # ---- 基础请求 ----

    def _fetch_token(self) -> None:
        # 注意：该接口成功时 token 在响应顶层（不在 data 里）
        result = self._request("POST", "/open-apis/auth/v3/tenant_access_token/internal",
                               {"app_id": self.app_id, "app_secret": self.app_secret}, auth=False)
        token = str(result.get("tenant_access_token") or "")
        if not token:
            raise FeishuError("取 tenant_access_token 失败（返回为空）")
        self._token = token
        self._token_expire = time.time() + max(60, int(result.get("expire") or 0) - 120)

    def _request(self, method: str, path: str, body: dict | None = None,
                 query: dict | None = None, auth: bool = True) -> dict:
        """返回完整响应 JSON（code==0 才算成功），失败重试 3 次。"""
        url = self.base + path
        if query:
            url += "?" + "&".join(f"{k}={v}" for k, v in query.items())
        last_error = ""
        for attempt in range(3):
            if auth and (not self._token or time.time() > self._token_expire):
                self._fetch_token()
            headers = {"Content-Type": "application/json; charset=utf-8"}
            if auth:
                headers["Authorization"] = f"Bearer {self._token}"
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
            try:
                req = urllib.request.Request(url, data=payload, headers=headers, method=method)
                with urllib.request.urlopen(req, timeout=30) as resp:
                    raw = resp.read()
                result = json.loads(raw.decode("utf-8"))
                if int(result.get("code") or 0) == 0:
                    return result
                last_error = f"{method} {path} 飞书接口错误 code={result.get('code')} msg={result.get('msg')}"
            except urllib.error.HTTPError as exc:
                # 带 body 的 4xx/5xx：飞书网关层拒绝，body 里往往有原因
                try:
                    detail = exc.read().decode("utf-8", "replace")[:1000]
                except Exception:
                    detail = "<no body>"
                last_error = f"{method} {path} HTTP {exc.code}: {detail}"
            except Exception as exc:  # 网络错误/解析失败都走重试
                last_error = f"{method} {path} 网络错误：{exc}"
            if attempt < 2:
                time.sleep(1.0 + attempt * 1.5)
        raise FeishuError(last_error)

    def _data(self, method: str, path: str, body: dict | None = None,
              query: dict | None = None) -> dict:
        """业务接口统一取 data 段。"""
        return self._request(method, path, body, query).get("data") or {}

    # ---- 字段 ----

    def list_fields(self) -> list[dict]:
        items: list[dict] = []
        token = ""
        while True:
            query = {"page_size": 100}
            if token:
                query["page_token"] = token
            data = self._data("GET", f"/open-apis/bitable/v1/apps/{self.app_token}"
                                      f"/tables/{self.table_id}/fields", query=query)
            items.extend(data.get("items") or [])
            if not data.get("has_more"):
                return items
            token = str(data.get("page_token") or "")

    def create_field(self, name: str, ftype: int, prop: dict | None = None) -> None:
        body: dict = {"field_name": name, "type": ftype}
        if prop:
            body["property"] = prop
        self._data("POST", f"/open-apis/bitable/v1/apps/{self.app_token}"
                           f"/tables/{self.table_id}/fields", body)

    def rename_field(self, field_id: str, name: str, ftype: int) -> None:
        # 该接口要求 type 一并带上（99992402: "type is required"）
        self._data("PUT", f"/open-apis/bitable/v1/apps/{self.app_token}"
                          f"/tables/{self.table_id}/fields/{field_id}",
                   {"field_name": name, "type": ftype})

    # ---- 记录 ----

    def search_records(self) -> list[dict]:
        items: list[dict] = []
        token = ""
        while True:
            query = {"page_size": 500}
            if token:
                query["page_token"] = token
            data = self._data("POST", f"/open-apis/bitable/v1/apps/{self.app_token}"
                                      f"/tables/{self.table_id}/records/search",
                              {}, query=query)
            items.extend(data.get("items") or [])
            if not data.get("has_more"):
                return items
            token = str(data.get("page_token") or "")

    def batch_create(self, records: list[dict]) -> int:
        done = 0
        for i in range(0, len(records), BATCH_CHUNK):
            chunk = records[i:i + BATCH_CHUNK]
            self._data("POST", f"/open-apis/bitable/v1/apps/{self.app_token}"
                               f"/tables/{self.table_id}/records/batch_create",
                       {"records": [{"fields": fields} for fields in chunk]})
            done += len(chunk)
        return done

    def batch_update(self, records: list[dict]) -> int:
        done = 0
        for i in range(0, len(records), BATCH_CHUNK):
            chunk = records[i:i + BATCH_CHUNK]
            self._data("POST", f"/open-apis/bitable/v1/apps/{self.app_token}"
                               f"/tables/{self.table_id}/records/batch_update",
                       {"records": chunk})
            done += len(chunk)
        return done

    def batch_delete(self, record_ids: list[str]) -> int:
        done = 0
        for i in range(0, len(record_ids), BATCH_CHUNK):
            chunk = record_ids[i:i + BATCH_CHUNK]
            self._data("POST", f"/open-apis/bitable/v1/apps/{self.app_token}"
                               f"/tables/{self.table_id}/records/batch_delete",
                       {"records": chunk})
            done += len(chunk)
        return done


# ---------------------------------------------------------------------------
# 表结构：建字段（幂等）+ 顺手清掉建表自带的整行空占位
# ---------------------------------------------------------------------------

def ensure_fields(fs: Feishu, names: dict) -> list[str]:
    actions: list[str] = []
    fields = fs.list_fields()
    by_name = {str(f.get("field_name") or ""): f for f in fields}
    primary = next((f for f in fields if f.get("is_primary")), None)

    if names["account"] not in by_name:
        if primary and str(primary.get("field_name") or "") in ("文本", "Text", ""):
            fs.rename_field(str(primary["field_id"]), names["account"], int(primary.get("type") or 1))
            actions.append(f"主字段改名「{primary.get('field_name')}」→「{names['account']}」")
        else:
            fs.create_field(names["account"], 1)
            actions.append(f"新建字段「{names['account']}」(文本)")
    if names["date"] not in by_name:
        fs.create_field(names["date"], 5, {"date_formatter": "yyyy/MM/dd"})
        actions.append(f"新建字段「{names['date']}」(日期)")
    if names["count"] not in by_name:
        fs.create_field(names["count"], 2, {"formatter": "0"})
        actions.append(f"新建字段「{names['count']}」(数字)")
    if names["cost"] not in by_name:
        fs.create_field(names["cost"], 2, {"formatter": "0.00"})
        actions.append(f"新建字段「{names['cost']}」(数字)")
    return actions


# ---------------------------------------------------------------------------
# 记录值读取/构造（飞书返回的文本可能是 [{text:...}] 分段，日期是毫秒时间戳）
# ---------------------------------------------------------------------------

def text_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = []
        for seg in value:
            if isinstance(seg, str):
                parts.append(seg)
            elif isinstance(seg, dict):
                parts.append(str(seg.get("text") or ""))
        return "".join(parts).strip()
    return str(value).strip()


def num_value(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def ts_value(value) -> int | None:
    num = num_value(value)
    return int(num) if num else None


def date_ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).date().isoformat()


def make_fields(names: dict, iso: str, owner: str, count: int, cost: float) -> dict:
    return {names["account"]: owner,
            names["date"]: date_ms(iso),
            names["count"]: int(count),
            names["cost"]: round(float(cost), 2)}


# ---------------------------------------------------------------------------
# 同步：upsert（键 = 日期|账户），整行空的占位记录清掉
# ---------------------------------------------------------------------------

def scan_existing(fs: Feishu, names: dict) -> tuple[dict, list[str], list[str]]:
    """读全表：按键索引已有行；返回 (by_key, 整行空的占位记录, 同键重复的旧记录)。"""
    by_key: dict[str, dict] = {}
    empty_ids: list[str] = []
    dup_ids: list[str] = []
    for item in fs.search_records():
        record_id = str(item.get("record_id") or "")
        fields = item.get("fields") or {}
        account = text_value(fields.get(names["account"]))
        ts = ts_value(fields.get(names["date"]))
        count = num_value(fields.get(names["count"]))
        cost = num_value(fields.get(names["cost"]))
        if not account and ts is None and count is None and cost is None:
            empty_ids.append(record_id)          # 建表自带的空行，不是数据
            continue
        if not account or ts is None:
            continue                              # 无法识别键的行不动，留给人工处理
        key = f"{ms_to_iso(ts)}|{account}"
        if key in by_key:
            dup_ids.append(record_id)             # 同键重复（极端情况下创建重试落了两次），自愈删旧的
            continue
        by_key[key] = {"record_id": record_id, "count": count, "cost": cost}
    return by_key, empty_ids, dup_ids


def sync_rows(fs: Feishu, names: dict, rows: dict) -> dict:
    by_key, empty_ids, dup_ids = scan_existing(fs, names)
    creates: list[dict] = []
    updates: list[dict] = []
    skipped = 0
    for iso in sorted(rows):
        for owner in sorted(rows[iso]):
            cell = rows[iso][owner]
            desired = make_fields(names, iso, owner, cell["count"], cell["cost"])
            key = f"{iso}|{owner}"
            current = by_key.get(key)
            if current is None:
                creates.append(desired)
            elif (current["count"] == cell["count"]
                  and round(current["cost"] or 0.0, 2) == round(cell["cost"], 2)):
                skipped += 1
            else:
                updates.append({"record_id": current["record_id"], "fields": desired})

    result = {"created": 0, "updated": 0, "skipped": skipped,
              "deleted": 0, "creates": len(creates), "updates": len(updates)}
    if creates:
        result["created"] = fs.batch_create(creates)
    if updates:
        result["updated"] = fs.batch_update(updates)
    stale = empty_ids + dup_ids
    if stale:
        result["deleted"] = fs.batch_delete(stale)
    return result


# ---------------------------------------------------------------------------
# 锁与日志：计划任务与手动执行可能撞车
# ---------------------------------------------------------------------------

def state_dir(args: argparse.Namespace) -> Path:
    if args.state_dir:
        return Path(args.state_dir).expanduser()
    return data_root_of(args) / "logs"


def log_path(args: argparse.Namespace) -> Path:
    return state_dir(args) / "feishu_report.log"


def lock_path(args: argparse.Namespace) -> Path:
    return state_dir(args) / ".feishu_report.lock"


def acquire_lock(args: argparse.Namespace) -> bool:
    path = lock_path(args)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            age = 0
        if age > LOCK_STALE_SECONDS:
            path.unlink(missing_ok=True)
        else:
            return False
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    os.write(fd, str(os.getpid()).encode("ascii"))
    os.close(fd)
    return True


def release_lock(args: argparse.Namespace) -> None:
    lock_path(args).unlink(missing_ok=True)


def write_log(args: argparse.Namespace, line: str) -> None:
    path = log_path(args)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp}  {line}\n")
    except OSError:
        pass  # 日志失败不影响主流程


def emit(args: argparse.Namespace, line: str) -> None:
    if not args.quiet:
        try:
            print(line)
        except (OSError, AttributeError):
            pass
    write_log(args, line)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def resolve_range(args: argparse.Namespace) -> tuple[date, date]:
    today = date.today()
    if args.date_from or args.date_to:
        try:
            start = date.fromisoformat(args.date_from) if args.date_from else today - timedelta(days=args.days)
            end = date.fromisoformat(args.date_to) if args.date_to else today - timedelta(days=1)
        except ValueError as exc:
            raise RuntimeError(f"日期格式错误（要 YYYY-MM-DD）：{exc}") from None
    else:
        end = today - timedelta(days=0 if args.include_today else 1)
        start = end - timedelta(days=args.days - 1)
    if start > end:
        raise RuntimeError(f"日期区间反了：{start} > {end}")
    return start, end


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.quiet:
        # pythonw.exe 下 sys.stdout 是 None，直接 print 会炸；统一收进日志文件
        try:
            sys.stdout = open(os.devnull, "w")  # type: ignore[assignment]
        except OSError:
            pass

    try:
        start, end = resolve_range(args)
    except RuntimeError as exc:
        write_log(args, f"[参数] {exc}")
        if not args.quiet:
            print(f"[参数错误] {exc}")
        return 2

    jobs_dir = data_root_of(args) / "jobs"
    users = load_users(args)
    prices = load_price_map(args)
    rows = aggregate(jobs_dir, start, end, users, prices)

    if args.skip_zero:
        rows = {iso: {owner: cell for owner, cell in day.items() if cell["jobs"] > 0}
                for iso, day in rows.items()}

    # ---- 聚合结果先亮出来（dry-run 到此为止）----
    total_count = sum(cell["count"] for day in rows.values() for cell in day.values())
    total_cost = round(sum(cell["cost"] for day in rows.values() for cell in day.values()), 2)
    emit(args, f"[聚合] {start} ~ {end}（含端点） 账户数={len(users)} "
               f"行数={sum(len(d) for d in rows.values())} 生产={total_count} 条 消费={total_cost} 元")
    for iso in sorted(rows):
        for owner in sorted(rows[iso]):
            cell = rows[iso][owner]
            emit(args, f"  {iso}  {owner:<14} 生产={cell['count']:<4} 消费={cell['cost']:.2f} 元 (任务 {cell['jobs']})")
    if args.dry_run:
        emit(args, "[dry-run] 未联网、未写入")
        return 0

    # ---- 飞书凭证 ----
    try:
        cfg = load_feishu_config(args)
    except RuntimeError as exc:
        emit(args, f"[配置] {exc}")
        return 2
    names = dict(DEFAULT_FIELD_NAMES)
    if isinstance(cfg.get("fields"), dict):
        for key, value in cfg["fields"].items():
            if key in names and str(value or "").strip():
                names[key] = str(value).strip()

    if not acquire_lock(args):
        emit(args, "[锁] 已有实例在跑，本次跳过（默认回补三天，明天会自动补上）")
        return 1

    fs = Feishu(str(cfg["app_id"]), str(cfg["app_secret"]),
                str(cfg["app_token"]), str(cfg["table_id"]))
    try:
        try:
            actions = ensure_fields(fs, names)
            for line in actions:
                emit(args, f"[字段] {line}")
            if args.setup_fields:
                _, empty_ids, dup_ids = scan_existing(fs, names)
                deleted = fs.batch_delete(empty_ids + dup_ids) if (empty_ids or dup_ids) else 0
                emit(args, f"[setup] 建字段完成，清理空占位/重复行 {deleted} 条")
                return 0
            result = sync_rows(fs, names, rows)
            emit(args, f"[同步] 新建 {result['created']} / 更新 {result['updated']} / "
                       f"无变化 {result['skipped']} / 清空行 {result['deleted']}")
            return 0
        except FeishuError as exc:
            emit(args, f"[失败] {exc}")
            return 1
    finally:
        release_lock(args)


if __name__ == "__main__":
    sys.exit(main())
