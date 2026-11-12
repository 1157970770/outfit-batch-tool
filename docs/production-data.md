# 生产数据镜像（SQLite）：给外部程序读的一份只读副本

> 面向：想读生产数据的**其他程序**——报表脚本、Excel / Power BI、AI / LLM agent。
> 一句话：**真源仍是 `web-data/*.json`；`web-data/mirror/production.sqlite` 是 5 秒内跟上的派生镜像。
> 只读、可删、可重建，写坏了不影响任何现有功能。**

- 库路径：`<data_root>/mirror/production.sqlite`（连同 `-wal` / `-shm` 两个旁挂文件）
  - 生产：`D:\批量视频任务运行区\web-data\mirror\production.sqlite`
  - 开发：`D:\批量视频任务设计区\web-data-dev\mirror\production.sqlite`
- **让 AI / LLM agent 自己查数据**：把 [llm-data-access.md](llm-data-access.md) 丢给它就行
  （那里只有「怎么查、字段什么口径、常见问题对应的 SQL」，没有运维细节）
- 由网页服务进程内的守护线程同步（`app/mirror/core.py`），服务停机期间不更新
- 结构版本见 `meta.schema_version`（2026-11-12 为 **2**）；版本不符会自动整库重建

## 1. 为什么是「镜像」而不是「搬库」

| 老做法（`scripts/feishu_daily_report.py`） | 现在 |
| --- | --- |
| 外部脚本自己扫 `jobs/*.json`、自己重算金额 | 查库；金额口径由 `app/billing.py` 单一来源给出 |
| 口径复制粘贴，代码改了报表不知道 | `tests/test_data_mirror.py` 拿 `usage_for()` 逐项对平，漂移会被测试抓住 |
| 每加一个报表就多一份解析逻辑 | 一份 schema，`SELECT` 即可；Excel / Power BI 走 ODBC |

**没有引入第二个写入者**：JSON 仍由网页服务原子写（`.tmp` → `os.replace`），镜像线程只读 JSON、写自己的库。
即便镜像线程崩了、库删了，业务照常，下个周期自动回到一致状态。

## 2. 三分钟上手

### 2.1 Python / LLM agent（推荐，最省事）

```powershell
# 人读的表格（自动列对齐，默认最多 1000 行）
uv run python scripts/query_production.py --sql "SELECT * FROM v_usage_daily ORDER BY day DESC, owner"

# 给程序吃：JSON 一行一个对象
uv run python scripts/query_production.py --sql "SELECT * FROM v_failures LIMIT 20" --json

# 不记得有哪些表
uv run python scripts/query_production.py --schema
```

脚本默认连**当前目录所属区**的库（按 `server/web.json` 的 `data_root` 解析），
跨区用 `--data-root "D:\批量视频任务运行区\web-data"`。

自己写代码时**必须只读打开**：

```python
import sqlite3
uri = "file:" + r"D:/批量视频任务运行区/web-data/mirror/production.sqlite" + "?mode=ro"
conn = sqlite3.connect(uri, uri=True, timeout=5.0)
for owner, day, cost in conn.execute(
        "SELECT owner, day, estimated_cost FROM v_usage_daily ORDER BY day DESC LIMIT 10"):
    print(owner, day, cost)
```

> ⚠️ 不要用 `immutable=1`：库在持续写入，`immutable` 会让 SQLite 读到不一致的页。
> 不要用可写连接（`mode=rw`/默认）：会挡住同步线程，还可能被误改。

### 2.2 Excel

**方式 A（推荐，零依赖）：导出 CSV 再打开**

```powershell
uv run python -m app.mirror --csv "D:\报表数据"
```

产出 `jobs.csv` / `records.csv` / `usage_daily.csv`，UTF-8 **带 BOM**（Excel 双击不乱码）。
想让服务每轮变更后自动刷新 CSV，把 `server/web.json` 的 `data_mirror.csv_dir` 设成目标目录。

**方式 B：直接连 SQLite（可刷新数据透视表）**

1. 装 SQLite ODBC 驱动（`sqliteodbc_w64.exe`，Windows x64）
2. Excel → 数据 → 获取数据 → 从其他源 → 从 ODBC → 选 `SQLite3 Datasource`
3. 数据库填 `D:\批量视频任务运行区\web-data\mirror\production.sqlite`，表选 `v_usage_daily`
4. 点「刷新」即可拿到最新数据

### 2.3 Power BI

同上走 **ODBC**（`SQLite3 Datasource`），或先用 `--csv` / `--snapshot` 落地再用「文本/CSV」连接。
Power BI Desktop 对 SQLite 的 WAL 支持不稳，**建议指向快照文件**：

```powershell
uv run python -m app.mirror --snapshot "D:\报表数据\production-$(Get-Date -f yyyyMMdd-HHmm).sqlite"
```

快照是 `VACUUM INTO` 出来的单文件（无 `-wal` 依赖），拷到别的机器也能直接打开。

### 2.4 sqlite3 命令行 / 其他语言

`sqlite3 "file:D:/.../production.sqlite?mode=ro" -header -column "SELECT 1"`；
Java / Go / .NET 同理，认 SQLite3 文件即可。

## 3. 表与视图

数据量级参考（2026-11-12）：jobs 236 / records 1141 / batches 28 / uploads 648，全库约 2–3 MB。

### 3.1 整表

| 表 | 主键 | 一行是什么 | 关键列 |
| --- | --- | --- | --- |
| `meta` | `key` | 库的元信息 | `schema_version` `built_at` `built_at_local` `data_root` `last_error` `counts_json` |
| `jobs` | `id` | 一次生产任务（N 个模板） | `owner` `status` `batch_id` `category` `created_at` `day` `total` `success_count` `failed_count` `unfinished_count` `billable_seconds` `estimated_cost` `out_dir` |
| `records` | `record_id` | **一个模板的一次生成 = 一条成片** | `job_id` `template_id` `status` `file` `file_exists` `cos_key` `cos_url` `billable_seconds` `estimated_cost` `cost_source` `error` |
| `batches` | `id` | 一个批次（锁定后多轮生产） | `batch_no` `owner` `state` `style_no` `total_items` `done` `failed` `pending` `producing` |
| `batch_items` | `(batch_id, template_id)` | 批次里的一条候选模板 | `state` `job_id` `tags_json` |
| `uploads` | `id` | 一张定妆图/细节图的索引 | `owner` `name` `path` `size_mb` `width` `height` |
| `usage_daily` | `(owner, day)` | **按人按天预聚合**（别自己 GROUP BY 重算口径） | `jobs` `items` `success` `failed` `unfinished` `timeout` `billable_seconds` `estimated_cost` `pending_jobs` |

### 3.2 视图（日常就用这三个）

| 视图 | 用途 | 列 |
| --- | --- | --- |
| `v_production` | 成片清单扁平表（records × jobs） | `record_id job_id owner day created_local category batch_id batch_ref template_id template_name status file file_exists path_external cos_key cos_url billable_seconds estimated_cost cost_source error` |
| `v_usage_daily` | 用量 + 成功率 + 均单价 | `usage_daily` 全部列 + `success_rate` `avg_cost_per_video` |
| `v_failures` | 只看失败侧（`failed/invalid/expired/canceled/unknown/timeout/skipped`） | `record_id job_id owner day template_id template_name status error billable_seconds estimated_cost cost_source` |

### 3.3 常用查询

```sql
-- 今天谁花了多少钱
SELECT owner, jobs, items, success, failed, estimated_cost
FROM v_usage_daily WHERE day = date('now', 'localtime') ORDER BY estimated_cost DESC;

-- 最近 30 天成本趋势
SELECT day, SUM(estimated_cost) AS cost, SUM(success) AS videos
FROM v_usage_daily WHERE day >= date('now', '-30 day') GROUP BY day ORDER BY day;

-- 某个款号的成片都在哪（给剪辑/交付用）
SELECT record_id, template_id, file, cos_url
FROM v_production WHERE category = '换装' ORDER BY day DESC LIMIT 50;

-- 失败原因排行（排障）
SELECT status, error, COUNT(*) AS n FROM v_failures GROUP BY status, error ORDER BY n DESC LIMIT 20;

-- 有记录但磁盘上文件已不在（清理前核对）
SELECT COUNT(*) FROM v_production WHERE status = 'success' AND file_exists = 0;
```

## 4. 字段口径（读之前必须知道的几条）

1. **时间**：`*_at` 是 epoch 秒（REAL，UTC 基准），`created_local` 是 `YYYY-MM-DD HH:MM:SS` 本地时间，
   `day` 是本地日期 `YYYY-MM-DD`。归日与 `usage_for()` / 飞书日报一致（系统本地时区），
   所以 `usage_daily.day` 直接 GROUP BY 即可，别用 `datetime(created_at,'unixepoch')` 自己换算。
2. **路径**：`file` / `out_dir` / `uploads.path` 都是**相对 `meta.data_root` 的 POSIX 路径**，
   拼绝对路径 = `Path(meta.data_root) / row["file"]`。
   不在数据目录下的（历史遗留、外挂盘）原样存绝对路径，并置 `path_external = 1`——这类不要盲目 `Path /` 拼接。
3. **金额**：`estimated_cost` 是「计费秒 × 单价」的**估算**，真实账单以阿里云为准。
   `cost_source` 说明这条金额从哪来：
   - `record`：JSON 里就有 `estimated_cost`（最可信）
   - `derived`：JSON 没写，但知道计费秒 → 按单价回算
   - `missing`：既没金额也没计费秒（通常是还没跑完的任务），金额按 0 计
4. **`file_exists`** 是**同步那一刻**的快照，不是实时探测。用它做「文件还在不在」的粗筛，
   真要删/搬之前再 `os.path.exists` 复核一次。
5. **脱敏**：`cos_url` 已去掉 `?q-ak=…&q-signature=…` 签名参数（原来那串签名有效期很长，入库等于泄密）；
   错误文本里的 `sk-…` Key 也被替换成 `[已隐藏密钥]`。要带签名的下载链接请走网页端接口，别从库里取。
6. **只读边界**：库里**没有** `server/config.json` 的 API Key、`web.json` 的密码哈希，
   也不含视频/图片二进制——只有索引和统计。

## 5. 新鲜度与可靠性

| 场景 | 行为 |
| --- | --- |
| 服务正常运行 | 每 5 秒扫一次 `jobs/` `batches/` `uploads/meta/`，有变化才写库（无变化不写，WAL 下读不受影响） |
| 判断库有多新 | `SELECT value FROM meta WHERE key='built_at_local'` |
| 服务停了 | 库保持最后一次状态，**不会自动更新**；重启后先全量对账一遍 |
| 单条 JSON 坏了 | 跳过该文件、保留旧行，错误写进 `meta.last_error` 与 `logs/data_mirror.log` |
| 库被删 / 损坏 / 版本不符 | 自动重建（损坏的原库改名 `production.sqlite.corrupt-<时间戳>` 留档） |
| 误连成可写、把库改了 | 删掉库文件即可，下次同步自动重建；真源 JSON 不受影响 |

日志：`<data_root>/logs/data_mirror.log`（只在异常时写，正常不刷屏）。

## 6. 命令行工具

```powershell
uv run python -m app.mirror                        # 等价 --once：同步一轮 + 打印自检 JSON
uv run python -m app.mirror --rebuild              # 强制重建（不保留旧库）
uv run python -m app.mirror --verify               # 只看自检结果（状态/版本/行数/最后错误）
uv run python -m app.mirror --csv "D:\报表数据"     # 导出 jobs/records/usage_daily 三个 CSV
uv run python -m app.mirror --snapshot out.sqlite  # 导出单文件快照（VACUUM INTO）
uv run python -m app.mirror --data-root "D:\批量视频任务运行区\web-data" --once
```

- 不带 `--data-root` 时用当前目录所属区的 `web-data`（读 `server/web.json`）。
- 单价自动取该区 `server/config.json`，所以跨区跑出来的金额与那个区一致。
- 这些命令**不会**抢网页服务：都可以在服务运行时执行（`--rebuild` 除外，它会挪走库文件，
  建议在无人用时跑；生产上更推荐让服务自己重建）。

## 7. 配置

`server/web.json`（可选段，缺省即启用）：

```json
"data_mirror": {
  "enabled": true,
  "poll_seconds": 5,
  "csv_dir": ""
}
```

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 设 `false` 即一键关闭：不建库、不写库、不占线程。已存在的库原样留着，不影响任何功能 |
| `poll_seconds` | `5` | 轮询间隔（秒），最小 1；本地盘 5 秒足够，网络盘可调大 |
| `csv_dir` | `""` | 非空时，每轮有变更就额外导出三个 CSV 到该目录（给 Excel/BI 的免驱动路径） |

改完**重启服务**生效。首次启动会自动建库并全量同步（当前数据量 <1 秒）。

## 8. 排障

| 现象 | 原因 / 处理 |
| --- | --- |
| 库文件不存在 | 服务没起、或 `data_mirror.enabled=false`；`--once` 手动跑一次看报错 |
| `meta.built_at_local` 很旧 | 服务停了，或同步线程异常（看 `logs/data_mirror.log` 与 `meta.last_error`） |
| 查询报 `database is locked` | 用了可写连接或被别的程序长时间占用；改 `mode=ro`，`busy_timeout` 已在库侧设 5000ms |
| Excel 打开 CSV 乱码 | 用了 `--csv` 之外的方式导出；本工具输出 UTF-8 带 BOM，若自己导出请加 `encoding="utf-8-sig"` |
| 金额与网页「账户用量」差几分 | 两边都按分取整，但聚合顺序不同；差 1 分属正常，差得多说明有任务在跑（`pending_jobs`） |
| 库被外部程序改坏了 | 删库重启，或 `--rebuild` 重建 |
| 想确认口径没漂 | `uv run python tests/test_data_mirror.py`（离线，几秒） |

## 9. 升级 schema 注意

`SCHEMA_VERSION` 一变，所有实例下次启动会**整库重建**（数据来自 JSON，无损失）。
重建期间（<1 秒）只读查询可能短暂看到空表，所以：
**改列名/删列属于破坏性变更**，先在这个文档里记一笔并通知下游报表。
