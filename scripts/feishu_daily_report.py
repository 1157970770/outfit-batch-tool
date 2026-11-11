#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把每日生产数据（账户 × 日期）同步到飞书多维表格「每日消费表」。

为什么需要这个脚本：
    管理端「账户用量」页有完整口径，但每天要人工登录去看；
    本脚本把同样的数据自动落到飞书多维表格，团队在表里直接看每日生产/消费。

口径（与 app/web/jobs.py::usage_for 保持一致）：
    · 一行 = 一个账户 + 一天（本地时区，按 job.created_at 归日）；
    · 「人员」列按账户自动补中文名：1157970770→郑、WUZHIHAO→吴、
      Caiqike→蔡、Liuhanpeng→刘（DEFAULT_PERSONS / 可被 config.persons 覆盖）；
      没有映射的账户留空，不覆盖人工填的名字；
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
    同时维护「每日总消耗」汇总表（每天一行全账户合计：生产视频数量 /
    消费金额 / 任务数 / 生产账户数）和「每日视频统计」表（日期 /
    当日生产的视频总数 / 可用视频数量——最后一列是人工列，脚本只写前两列，
    绝不覆盖），两张表不存在时都会自动创建；
    --no-summary / --no-daily 可单独关掉对应的表。

用法：
    python scripts/feishu_daily_report.py                    # 同步 D-3..今天（默认含当天，18:00 定时任务用）
    python scripts/feishu_daily_report.py --no-today         # 只同步到昨天（纯回补）
    python scripts/feishu_daily_report.py --from 2026-10-28 --to 2026-11-07
    python scripts/feishu_daily_report.py --setup-fields     # 只建字段/清空行，不写数据
    python scripts/feishu_daily_report.py --dry-run          # 只打印聚合结果，不联网
    python scripts/feishu_daily_report.py --quiet            # 计划任务用：只写日志不打印

配置（server/feishu.json，不入库，两区各自放一份）：
    {
      "app_id": "cli_xxx", "app_secret": "xxx",
      "app_token": "bascnxxx", "table_id": "tblxxx",
      "fields": {"account": "账户", "date": "日期",
                 "count": "生产视频数量", "cost": "消费金额", "person": "人员"},
      "persons": {"1157970770": "郑"},              // 可选，覆盖/追加人员映射
      "summary_table_id": "tblxxx", "daily_table_id": "tblxxx",  // 可选，按 ID 找表（改名也不丢）
      "daily_fields": {"available": "可用视频数量(待回传数据)"}   // 可选，列名被改过时对齐
    }

区域解析：数据目录默认 <脚本所在仓库>/web-data；--data-root 指定后，
账号表/单价/飞书凭证一律取该数据目录所在区域的 server/*.json，
从设计区跑生产数据：--data-root "D:\\批量视频任务运行区\\web-data"。

计划任务：
    scripts/install_feishu_report_task.ps1（每天 18:00，pythonw --quiet；
    默认窗口 D-3..今天，当天写快照，次日自动修正）

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
                       "count": "生产视频数量", "cost": "消费金额",
                       "person": "人员"}
# 账户 → 人员中文名：明细表「人员」列每天自动补；没列出的账户（如 ZHOUHAO）留空，
# 也允许 server/feishu.json 里用 "persons": {"账户": "姓名"} 覆盖/追加。
DEFAULT_PERSONS = {
    "1157970770": "郑",
    "WUZHIHAO": "吴",
    "Caiqike": "蔡",
    "Liuhanpeng": "刘",
}
# 汇总表（每天一行：全账户合计），表不存在时自动创建
SUMMARY_TABLE_NAME = "每日总消耗"
SUMMARY_FIELD_NAMES = {"date": "日期", "count": "生产视频数量",
                       "cost": "消费金额", "jobs": "任务数", "accounts": "生产账户数"}
# 质量统计表：脚本只填「当日生产的视频总数」，「可用视频数量」由人工填，回写时不碰
DAILY_TABLE_NAME = "每日视频统计"
DAILY_FIELD_NAMES = {"date": "日期", "count": "当日生产的视频总数",
                     "available": "可用视频数量"}
DEFAULT_DAYS = 4          # 默认窗口 D-3..D0（含今天，当日快照次日修正）
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
                   help=f"不指定 --from/--to 时的窗口天数（默认 {DEFAULT_DAYS} 天，含今天即 D-3..D0）")
    p.add_argument("--no-today", action="store_true", help="只同步到昨天（不含今天，用于纯回补）")
    p.add_argument("--include-today", action="store_true", help="兼容参数：默认已含今天，保留以兼容旧命令行")
    p.add_argument("--skip-zero", action="store_true", help="当日没有任何任务的账户不写 0 行")
    p.add_argument("--setup-fields", action="store_true", help="只建字段/清空行，不写数据")
    p.add_argument("--no-summary", action="store_true",
                   help="不同步「每日总消耗」汇总表（默认每次一起写）")
    p.add_argument("--no-daily", action="store_true",
                   help="不同步「每日视频统计」表（默认每次一起写）")
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

    # ---- 数据表 ----

    def list_tables(self) -> list[dict]:
        items: list[dict] = []
        token = ""
        while True:
            query = {"page_size": 100}
            if token:
                query["page_token"] = token
            data = self._data("GET", f"/open-apis/bitable/v1/apps/{self.app_token}/tables",
                              query=query)
            items.extend(data.get("items") or [])
            if not data.get("has_more"):
                return items
            token = str(data.get("page_token") or "")

    def create_table(self, name: str, fields: list[dict]) -> str:
        """建表（首字段即主字段），返回 table_id。"""
        data = self._data("POST", f"/open-apis/bitable/v1/apps/{self.app_token}/tables",
                          {"table": {"name": name, "default_view_name": "表格", "fields": fields}})
        return str(data.get("table_id") or "")

    def rename_table(self, table_id: str, name: str) -> None:
        # 更新数据表用 PATCH（PUT 会 404）
        self._data("PATCH", f"/open-apis/bitable/v1/apps/{self.app_token}/tables/{table_id}",
                   {"name": name})

    def delete_table(self, table_id: str) -> None:
        self._data("DELETE", f"/open-apis/bitable/v1/apps/{self.app_token}/tables/{table_id}")

    # ---- 字段 ----

    def list_fields(self, table_id: str | None = None) -> list[dict]:
        tid = table_id or self.table_id
        items: list[dict] = []
        token = ""
        while True:
            query = {"page_size": 100}
            if token:
                query["page_token"] = token
            data = self._data("GET", f"/open-apis/bitable/v1/apps/{self.app_token}"
                                      f"/tables/{tid}/fields", query=query)
            items.extend(data.get("items") or [])
            if not data.get("has_more"):
                return items
            token = str(data.get("page_token") or "")

    def create_field(self, name: str, ftype: int, prop: dict | None = None,
                     table_id: str | None = None) -> None:
        tid = table_id or self.table_id
        body: dict = {"field_name": name, "type": ftype}
        if prop:
            body["property"] = prop
        self._data("POST", f"/open-apis/bitable/v1/apps/{self.app_token}"
                           f"/tables/{tid}/fields", body)

    def rename_field(self, field_id: str, name: str, ftype: int) -> None:
        # 该接口要求 type 一并带上（99992402: "type is required"）
        self._data("PUT", f"/open-apis/bitable/v1/apps/{self.app_token}"
                          f"/tables/{self.table_id}/fields/{field_id}",
                   {"field_name": name, "type": ftype})

    # ---- 记录 ----

    def search_records(self, table_id: str | None = None) -> list[dict]:
        tid = table_id or self.table_id
        items: list[dict] = []
        token = ""
        while True:
            query = {"page_size": 500}
            if token:
                query["page_token"] = token
            data = self._data("POST", f"/open-apis/bitable/v1/apps/{self.app_token}"
                                      f"/tables/{tid}/records/search",
                              {}, query=query)
            items.extend(data.get("items") or [])
            if not data.get("has_more"):
                return items
            token = str(data.get("page_token") or "")

    def batch_create(self, records: list[dict], table_id: str | None = None) -> int:
        tid = table_id or self.table_id
        done = 0
        for i in range(0, len(records), BATCH_CHUNK):
            chunk = records[i:i + BATCH_CHUNK]
            self._data("POST", f"/open-apis/bitable/v1/apps/{self.app_token}"
                               f"/tables/{tid}/records/batch_create",
                       {"records": [{"fields": fields} for fields in chunk]})
            done += len(chunk)
        return done

    def batch_update(self, records: list[dict], table_id: str | None = None) -> int:
        tid = table_id or self.table_id
        done = 0
        for i in range(0, len(records), BATCH_CHUNK):
            chunk = records[i:i + BATCH_CHUNK]
            self._data("POST", f"/open-apis/bitable/v1/apps/{self.app_token}"
                               f"/tables/{tid}/records/batch_update",
                       {"records": chunk})
            done += len(chunk)
        return done

    def batch_delete(self, record_ids: list[str], table_id: str | None = None) -> int:
        tid = table_id or self.table_id
        done = 0
        for i in range(0, len(record_ids), BATCH_CHUNK):
            chunk = record_ids[i:i + BATCH_CHUNK]
            self._data("POST", f"/open-apis/bitable/v1/apps/{self.app_token}"
                               f"/tables/{tid}/records/batch_delete",
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
    if names["person"] not in by_name:
        fs.create_field(names["person"], 1)
        actions.append(f"新建字段「{names['person']}」(文本，按账户自动补姓名)")
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


def make_fields(names: dict, iso: str, owner: str, count: int, cost: float,
                person: str = "") -> dict:
    fields = {names["account"]: owner,
              names["date"]: date_ms(iso),
              names["count"]: int(count),
              names["cost"]: round(float(cost), 2)}
    if person:  # 没映射到人的账户不写，避免把人工填的名字冲掉
        fields[names["person"]] = person
    return fields


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
        by_key[key] = {"record_id": record_id, "count": count, "cost": cost,
                       "person": text_value(fields.get(names["person"]))}
    return by_key, empty_ids, dup_ids


def sync_rows(fs: Feishu, names: dict, rows: dict, persons: dict[str, str] | None = None) -> dict:
    persons = persons or {}
    by_key, empty_ids, dup_ids = scan_existing(fs, names)
    creates: list[dict] = []
    updates: list[dict] = []
    skipped = 0
    for iso in sorted(rows):
        for owner in sorted(rows[iso]):
            cell = rows[iso][owner]
            person = str(persons.get(owner) or "")
            desired = make_fields(names, iso, owner, cell["count"], cell["cost"], person)
            key = f"{iso}|{owner}"
            current = by_key.get(key)
            if current is None:
                creates.append(desired)
            elif (current["count"] == cell["count"]
                  and round(current["cost"] or 0.0, 2) == round(cell["cost"], 2)
                  and (not person or (current.get("person") or "") == person)):
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


def summarize(rows: dict[str, dict[str, dict]]) -> dict[str, dict]:
    """{日期iso: {count, cost, jobs, accounts}}：accounts = 当天有任务的账户数。"""
    sums: dict[str, dict] = {}
    for iso in sorted(rows):
        day = rows[iso]
        sums[iso] = {"count": sum(cell["count"] for cell in day.values()),
                     "cost": round(sum(cell["cost"] for cell in day.values()), 2),
                     "jobs": sum(cell["jobs"] for cell in day.values()),
                     "accounts": sum(1 for cell in day.values() if cell["jobs"] > 0)}
    return sums


def _find_table(fs: Feishu, name: str, table_id: str = "") -> str:
    """先按 table_id 找（改名/重命名都稳），找不到再按名字。"""
    tables = fs.list_tables()
    if table_id:
        for table in tables:
            if str(table.get("table_id") or "") == table_id:
                return table_id
    for table in tables:
        if str(table.get("name") or "") == name:
            return str(table.get("table_id") or "")
    return ""


def ensure_summary_table(fs: Feishu, name: str, table_id: str = "") -> tuple[str, str]:
    """按 table_id/名字找汇总表，没有就建（自愈）；返回 (table_id, 说明或空串)。"""
    found = _find_table(fs, name, table_id)
    if found:
        return found, ""
    fields = [
        {"field_name": SUMMARY_FIELD_NAMES["date"], "type": 5,
         "property": {"date_formatter": "yyyy/MM/dd"}},
        {"field_name": SUMMARY_FIELD_NAMES["count"], "type": 2, "property": {"formatter": "0"}},
        {"field_name": SUMMARY_FIELD_NAMES["cost"], "type": 2, "property": {"formatter": "0.00"}},
        {"field_name": SUMMARY_FIELD_NAMES["jobs"], "type": 2, "property": {"formatter": "0"}},
        {"field_name": SUMMARY_FIELD_NAMES["accounts"], "type": 2, "property": {"formatter": "0"}},
    ]
    return fs.create_table(name, fields), f"新建汇总表「{name}」"


def sync_summary(fs: Feishu, table_id: str, sums: dict[str, dict]) -> dict:
    """汇总表 upsert（键 = 日期）：每天一行全账户合计。"""
    names = SUMMARY_FIELD_NAMES
    by_date: dict[str, dict] = {}
    empty_ids: list[str] = []
    dup_ids: list[str] = []
    for item in fs.search_records(table_id):
        record_id = str(item.get("record_id") or "")
        fields = item.get("fields") or {}
        ts = ts_value(fields.get(names["date"]))
        if ts is None:
            if not fields:
                empty_ids.append(record_id)      # 建表自带的空行
            continue
        iso = ms_to_iso(ts)
        if iso in by_date:
            dup_ids.append(record_id)
            continue
        by_date[iso] = {
            "record_id": record_id,
            "count": num_value(fields.get(names["count"])),
            "cost": num_value(fields.get(names["cost"])),
            "jobs": num_value(fields.get(names["jobs"])),
            "accounts": num_value(fields.get(names["accounts"])),
        }

    creates: list[dict] = []
    updates: list[dict] = []
    skipped = 0
    for iso in sorted(sums):
        cell = sums[iso]
        desired = {names["date"]: date_ms(iso),
                   names["count"]: int(cell["count"]),
                   names["cost"]: round(float(cell["cost"]), 2),
                   names["jobs"]: int(cell["jobs"]),
                   names["accounts"]: int(cell["accounts"])}
        current = by_date.get(iso)
        if current is None:
            creates.append(desired)
        elif (current["count"] == cell["count"]
              and round(current["cost"] or 0.0, 2) == round(cell["cost"], 2)
              and current["jobs"] == cell["jobs"]
              and current["accounts"] == cell["accounts"]):
            skipped += 1
        else:
            updates.append({"record_id": current["record_id"], "fields": desired})

    result = {"created": 0, "updated": 0, "skipped": skipped, "deleted": 0}
    if creates:
        result["created"] = fs.batch_create(creates, table_id)
    if updates:
        result["updated"] = fs.batch_update(updates, table_id)
    stale = empty_ids + dup_ids
    if stale:
        result["deleted"] = fs.batch_delete(stale, table_id)
    return result


# ---------------------------------------------------------------------------
# 质量统计表：脚本只填「当日生产的视频总数」，「可用视频数量」留给人工
# ---------------------------------------------------------------------------

def ensure_daily_table(fs: Feishu, name: str, names: dict | None = None,
                       table_id: str = "") -> tuple[str, str]:
    """按 table_id/名字找统计表，没有就建（自愈）；返回 (table_id, 说明或空串)。"""
    names = names or DAILY_FIELD_NAMES
    found = _find_table(fs, name, table_id)
    if found:
        return found, ""
    fields = [
        {"field_name": names["date"], "type": 5,
         "property": {"date_formatter": "yyyy/MM/dd"}},
        {"field_name": names["count"], "type": 2, "property": {"formatter": "0"}},
        {"field_name": names["available"], "type": 2, "property": {"formatter": "0"}},
    ]
    return fs.create_table(name, fields), f"新建统计表「{name}」"


def sync_daily(fs: Feishu, table_id: str, sums: dict[str, dict],
               names: dict | None = None) -> dict:
    """统计表 upsert（键 = 日期）：只写「日期 + 当日生产的视频总数」。

    「可用视频数量」是人工列——更新时只发自己这两个字段（飞书是部分更新），
    绝不覆盖；删除也极其保守：只删整行全空的占位行，带日期的行一律保留，
    重复日期也不删（可能挂着人工数据）。
    """
    names = names or DAILY_FIELD_NAMES
    by_date: dict[str, dict] = {}
    empty_ids: list[str] = []
    for item in fs.search_records(table_id):
        record_id = str(item.get("record_id") or "")
        fields = item.get("fields") or {}
        ts = ts_value(fields.get(names["date"]))
        if ts is None:
            if not fields:                      # 整行全空 = 建表自带占位行
                empty_ids.append(record_id)
            continue                            # 有人工数据但没日期的行：保留
        iso = ms_to_iso(ts)
        if iso in by_date:
            continue                            # 重复日期：保留原行，不再新建
        by_date[iso] = {"record_id": record_id,
                        "count": num_value(fields.get(names["count"]))}

    creates: list[dict] = []
    updates: list[dict] = []
    skipped = 0
    for iso in sorted(sums):
        desired_count = int(sums[iso]["count"])
        desired = {names["date"]: date_ms(iso), names["count"]: desired_count}
        current = by_date.get(iso)
        if current is None:
            creates.append(desired)
        elif current["count"] == desired_count:
            skipped += 1
        else:
            updates.append({"record_id": current["record_id"], "fields": desired})

    result = {"created": 0, "updated": 0, "skipped": skipped, "deleted": 0}
    if creates:
        result["created"] = fs.batch_create(creates, table_id)
    if updates:
        result["updated"] = fs.batch_update(updates, table_id)
    if empty_ids:
        result["deleted"] = fs.batch_delete(empty_ids, table_id)
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
            end = date.fromisoformat(args.date_to) if args.date_to else today
        except ValueError as exc:
            raise RuntimeError(f"日期格式错误（要 YYYY-MM-DD）：{exc}") from None
    else:
        # 默认含今天：每天 18:00 的定时任务要把「当天」写进表；
        # 今天的数据是当日快照，次日 18:00 的回补会用最终值覆盖。
        end = today - timedelta(days=1 if args.no_today else 0)
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
    persons = dict(DEFAULT_PERSONS)
    total_count = sum(cell["count"] for day in rows.values() for cell in day.values())
    total_cost = round(sum(cell["cost"] for day in rows.values() for cell in day.values()), 2)
    emit(args, f"[聚合] {start} ~ {end}（含端点） 账户数={len(users)} "
               f"行数={sum(len(d) for d in rows.values())} 生产={total_count} 条 消费={total_cost} 元")
    for iso in sorted(rows):
        for owner in sorted(rows[iso]):
            cell = rows[iso][owner]
            emit(args, f"  {iso}  {owner:<14} 人员={persons.get(owner) or '-':<2} "
                       f"生产={cell['count']:<4} 消费={cell['cost']:.2f} 元 (任务 {cell['jobs']})")
    sums = summarize(rows)
    for iso in sorted(sums):
        s = sums[iso]
        emit(args, f"  {iso}  合计{'':<12} 生产={s['count']:<4} 消费={s['cost']:.2f} 元 "
                   f"(任务 {s['jobs']} / 账户 {s['accounts']})")
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
    summary_name = str(cfg.get("summary_table_name") or "").strip() or SUMMARY_TABLE_NAME
    daily_name = str(cfg.get("daily_table_name") or "").strip() or DAILY_TABLE_NAME
    daily_fields = dict(DAILY_FIELD_NAMES)
    if isinstance(cfg.get("daily_fields"), dict):   # 表里列名被人工改过时用配置对齐
        for key, value in cfg["daily_fields"].items():
            if key in daily_fields and str(value or "").strip():
                daily_fields[key] = str(value).strip()
    summary_id = str(cfg.get("summary_table_id") or "").strip()
    daily_id = str(cfg.get("daily_table_id") or "").strip()
    if isinstance(cfg.get("persons"), dict):   # 允许本地配置覆盖/追加账户→姓名
        for key, value in cfg["persons"].items():
            persons[str(key)] = str(value or "").strip()

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
                if not args.no_summary:
                    table_id, note = ensure_summary_table(fs, summary_name, summary_id)
                    if note:
                        emit(args, f"[汇总] {note}（{table_id}）")
                if not args.no_daily:
                    table_id, note = ensure_daily_table(fs, daily_name, daily_fields, daily_id)
                    if note:
                        emit(args, f"[统计] {note}（{table_id}）")
                return 0
            result = sync_rows(fs, names, rows, persons)
            emit(args, f"[同步] 新建 {result['created']} / 更新 {result['updated']} / "
                       f"无变化 {result['skipped']} / 清空行 {result['deleted']}")
            sums = summarize(rows)
            if not args.no_summary:
                table_id, note = ensure_summary_table(fs, summary_name, summary_id)
                if not table_id:
                    emit(args, "[汇总] 汇总表创建失败（table_id 为空），本次跳过")
                else:
                    if note:
                        emit(args, f"[汇总] {note}（{table_id}）")
                    sresult = sync_summary(fs, table_id, sums)
                    emit(args, f"[汇总] 新建 {sresult['created']} / 更新 {sresult['updated']} / "
                               f"无变化 {sresult['skipped']} / 清空行 {sresult['deleted']}")
            if not args.no_daily:
                table_id, note = ensure_daily_table(fs, daily_name, daily_fields, daily_id)
                if not table_id:
                    emit(args, "[统计] 统计表创建失败（table_id 为空），本次跳过")
                else:
                    if note:
                        emit(args, f"[统计] {note}（{table_id}）")
                    dresult = sync_daily(fs, table_id, sums, daily_fields)
                    emit(args, f"[统计] 新建 {dresult['created']} / 更新 {dresult['updated']} / "
                               f"无变化 {dresult['skipped']} / 清空行 {dresult['deleted']}")
            return 0
        except FeishuError as exc:
            emit(args, f"[失败] {exc}")
            return 1
    finally:
        release_lock(args)


if __name__ == "__main__":
    sys.exit(main())
