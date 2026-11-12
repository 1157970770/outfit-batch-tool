# 给 LLM / Agent：怎么拿到这个项目的生产数据

> 你是被叫来查数据、写报表、做分析的人（模型）。
> **不要**去翻 `web-data/*.json`、**不要**读 `server/config.json`、**不要**扫 12 GB 的 `output/`。
> 直接查一份已经准备好的 SQLite 库就行，一条 `SELECT` 搞定。

---

## 0. 三句话版

```powershell
# 1) 看有哪些表/视图、各多少行
uv run python scripts/query_production.py --schema

# 2) 查（默认连本区，自动按 server/web.json 的 data_root 解析）
uv run python scripts/query_production.py --sql "SELECT * FROM v_usage_daily ORDER BY day DESC LIMIT 20"

# 3) 要喂给自己解析就加 --json（一行一个对象）
uv run python scripts/query_production.py --sql "SELECT * FROM v_failures LIMIT 50" --json
```

库的位置（一般不用管，脚本会自己找）：

```
D:\批量视频任务运行区\web-data\mirror\production.sqlite     # 生产
D:\批量视频任务设计区\web-data-dev\mirror\production.sqlite  # 开发
```

---

## 1. 别做的事（会踩坑或泄密）

| ❌ 别做 | 为什么 |
| --- | --- |
| 读 `server/config.json` / `server/web.json` / `server/imagegen.json` | 明文 API Key、COS 密钥、密码哈希。查数据不需要它们 |
| 用可写连接打开镜像库（默认 `sqlite3.connect(path)`） | 会挡住同步线程，还可能被误改；必须 `mode=ro` |
| 用 `immutable=1` | 库在持续写入，`immutable` 会读到不一致的页 |
| 自己遍历 `web-data/jobs/*.json` 再算金额 | 口径容易漂移，这正是这套镜像存在的原因 |
| 遍历 `web-data/output/`（12 GB）去数视频 | 用 `v_production.file_exists`，需要精确时再 `Test-Path` |
| 把 `file` 列直接拿去用 | 它是**相对** `meta.data_root` 的 POSIX 路径，要拼一下（见 §5） |

---

## 2. 先判断库能不能用（10 秒）

```powershell
uv run python scripts/query_production.py --sql "SELECT key, value FROM meta"
```

看三个键：

| 键 | 期望 | 不对怎么办 |
| --- | --- | --- |
| `built_at_local` | 是**最近几分钟**（服务在跑）；若停在几小时前 = 服务停了或同步线程挂了 | 先看 `last_error`；空的话就是服务没开，库是旧快照，可以照常查但要说明「数据截至 …」 |
| `schema_version` | 与代码一致（2026-11-12 为 `2`） | 不符说明代码升级过，库会自己重建；让服务跑一轮或手动 `--rebuild` |
| `last_error` | 空 | 有内容说明个别 JSON 解析失败，那些任务可能缺行 |

库文件不存在？先生成一次：

```powershell
uv run python -m app.mirror --once                              # 本区
uv run python -m app.mirror --once --data-root "D:\批量视频任务运行区\web-data"   # 生产
```

---

## 3. 数据长什么样

一个生产任务 = `jobs` 里一行；一条视频 = `records` 里一行（同一个 job 下有 N 条）。
日常只用三个视图，别自己写 JOIN。

| 对象 | 一行是什么 | 常用列 |
| --- | --- | --- |
| `v_production` | **一条成片**（records × jobs 扁平） | `record_id` `seq` `job_id` `owner` `day` `category` `template_id` `template_name` `status` `file` `file_exists` `cos_url` `estimated_cost` `cost_source` `error` |
| `v_usage_daily` | **某人某天的用量**（已汇总） | `owner` `day` `jobs` `items` `success` `failed` `unfinished` `timeout` `pending_jobs` `billable_seconds` `estimated_cost` `success_rate` `avg_cost_per_video` |
| `v_failures` | 只看失败侧 | `record_id` `job_id` `owner` `day` `template_id` `status` `error` `estimated_cost` `cost_source` |

底层表（需要明细关系时才用）：`jobs` / `records` / `batches` / `batch_items` / `uploads` / `usage_daily` / `meta`。
`records.job_id` 外键到 `jobs.id`（删 job 会级联删记录）。

**数量级参考（2026-11-12 生产）**：237 个任务、1100+ 条视频、28 个批次、650+ 张图，整库 2–3 MB。
所以随便 `SELECT *` 都能几毫秒返回；但给模型读的时候还是加 `LIMIT`，别把整库灌进上下文。

---

## 4. 自然语言 → SQL 速查

> 全部可直接粘进 `--sql`。`day` 是**本地日期**（Asia/Shanghai），别自己从时间戳换算。

**「今天/这周/这个月花了多少钱，各人多少」**

```sql
SELECT owner, jobs, items, success, failed, estimated_cost
FROM v_usage_daily WHERE day = date('now','localtime')
ORDER BY estimated_cost DESC;
```

```sql
SELECT owner, SUM(estimated_cost) AS cost, SUM(success) AS videos
FROM v_usage_daily WHERE day >= date('now','localtime','-6 day')
GROUP BY owner ORDER BY cost DESC;
```

**「最近 30 天每天的成本和产量」**

```sql
SELECT day, SUM(success) AS videos, ROUND(SUM(estimated_cost),2) AS cost
FROM v_usage_daily WHERE day >= date('now','localtime','-30 day')
GROUP BY day ORDER BY day;
```

**「上周谁产出最多 / 成功率怎么样」**

```sql
SELECT owner, SUM(jobs) AS jobs, SUM(items) AS items, SUM(success) AS ok,
       SUM(failed) AS failed,
       ROUND(CAST(SUM(success) AS REAL) / NULLIF(SUM(items),0), 3) AS success_rate
FROM v_usage_daily
WHERE day BETWEEN date('now','localtime','-13 day') AND date('now','localtime','-7 day')
GROUP BY owner ORDER BY ok DESC;
```

**「某人的任务清单 / 最近 20 个任务」**

```sql
SELECT id, status, created_local, total, success_count, failed_count,
       ROUND(estimated_cost,2) AS cost, category, batch_ref
FROM jobs WHERE owner = 'WUZHIHAO'
ORDER BY created_at DESC LIMIT 20;
```

**「某个款号 / 分类的成片都在哪（要交付/剪辑）」**

> 任务的 `category` 只有两个合法值：`服装换装`、`项链手串`（老任务为空串）。
> 拿不准就先 `SELECT DISTINCT category FROM jobs`。

```sql
SELECT record_id, job_id, owner, day, template_id, file, cos_url
FROM v_production
WHERE category = '项链手串' AND status = 'success'
ORDER BY day DESC LIMIT 100;
```

**「失败的都是什么原因」**

```sql
SELECT status, COALESCE(error,'') AS error, COUNT(*) AS n
FROM v_failures GROUP BY status, error ORDER BY n DESC LIMIT 20;
```

**「某个任务下面的每一条视频」**

```sql
SELECT record_id, seq, template_id, template_name, status, file, file_exists,
       billable_seconds, estimated_cost, error
FROM v_production WHERE job_id = 'xxxxxxxxxxxx' ORDER BY seq;
```

**「有多少成片磁盘上已经没了」（清理前核对）**

```sql
SELECT COUNT(*) AS missing FROM v_production
WHERE status = 'success' AND file_exists = 0;
```

**「批次进度」**

```sql
SELECT b.batch_no, b.title, b.owner, b.state, b.total_items,
       b.done, b.failed, b.pending, b.producing
FROM batches b WHERE b.state != 'closed' ORDER BY b.updated_at DESC;
```

---

## 5. 五个必须知道的字段口径

1. **金额是估算**：`estimated_cost` = 计费秒 × 单价，真实账单以阿里云为准。别把它说成「实际扣费」。
   `cost_source` 告诉你这个数怎么来的：`record`（JSON 里本来就有，最可信）/ `derived`（按计费秒推算）/
   `missing`（既没金额也没计费秒，通常还没跑完，按 0 计）。
2. **时间**：`created_at`/`finished_at` 是 epoch 秒；`created_local`/`day` 是本地时间，归日口径与网页「账户用量」一致。
   **要用 `day`，不要用 `date(created_at,'unixepoch')`**（那是 UTC，会错 8 小时）。
3. **路径**：`file`、`out_dir`、`uploads.path` 是相对 `meta.data_root` 的路径、正斜杠。
   拼绝对路径 = `Join-Path (SELECT value FROM meta WHERE key='data_root') $file`。
   若 `path_external = 1`，说明这条路径**不在**数据目录下（历史遗留/外挂盘），已经是绝对路径，别再拼。
4. **`file_exists` 是快照**：同步那一刻的结果，不是实时。要断言「现在还在吗」请实际 `Test-Path` 一次。
5. **`status` 只有这几种**：`queued` `running` `success` `failed` `timeout` `invalid` `expired` `canceled` `unknown` `skipped`。
   统计产出用 `success`；失败侧用 `v_failures`；`unfinished` 是在跑/排队的，别混进成功率的分母里随便算。

---

## 6. 直接在 Python 里查（不经过 CLI）

```python
import sqlite3
from pathlib import Path

DB = Path(r"D:\批量视频任务运行区\web-data\mirror\production.sqlite")
conn = sqlite3.connect("file:" + DB.as_posix() + "?mode=ro", uri=True, timeout=5.0)
conn.row_factory = sqlite3.Row

root = conn.execute("SELECT value FROM meta WHERE key='data_root'").fetchone()[0]

for row in conn.execute(
        "SELECT owner, day, items, estimated_cost FROM v_usage_daily "
        "ORDER BY day DESC, estimated_cost DESC LIMIT 10"):
    print(dict(row))

# 需要绝对路径时
for row in conn.execute("SELECT record_id, file, path_external FROM v_production "
                        "WHERE day = date('now','localtime') LIMIT 5"):
    p = row["file"] if row["path_external"] else str(Path(root) / row["file"])
    print(row["record_id"], p)
```

给其他语言/工具：
- Excel：装 SQLite ODBC 驱动 → 数据 → 从 ODBC → `SQLite3 Datasource` → 选库 → 选视图 → 刷新。
- Power BI：同上走 ODBC，或先 `python -m app.mirror --snapshot out.sqlite` 生成单文件快照再连（WAL 多文件对 BI 不友好）。
- 跨机器：始终用 `--snapshot`，别只拷 `production.sqlite`（`-wal` 里有未合并的数据）。

---

## 7. 回答之前，先自检这四件事

1. **数据截止时间**：报一句 `meta.built_at_local`，让读者知道新鲜度。
2. **口径**：金额说「估算」，日期说「本地日期」。
3. **空结果不等于 0**：可能是筛选条件里的账户名/分类写错了。先 `SELECT DISTINCT owner FROM jobs` / `SELECT DISTINCT category FROM jobs` 对一下。
4. **别外泄**：库里没有密钥，但**有账户名、金额、任务量**，属于内部经营数据；输出到对外文档前先脱敏。

---

## 8. 出问题时的最短路径

| 现象 | 下一步 |
| --- | --- |
| 提示「镜像库不存在」 | `uv run python -m app.mirror --once`（跨区加 `--data-root`） |
| `database is locked` | 你是不是用了可写连接？改 `mode=ro`；仍报就看是不是有别的程序长开着 |
| 表/列不存在 | `--schema` 看实际结构；`schema_version` 与代码不符时，库会重建，或手动 `--rebuild` |
| 数字对不上网页「账户用量」 | 差 1 分属正常（取整顺序）；差得多通常是任务还在跑（看 `pending_jobs`）或看的时间范围不一致 |
| 想确认口径没坏 | `uv run python tests/test_data_mirror.py`（离线，1 秒） |

字段全集、运维配置、升级 schema 的注意事项见 [production-data.md](production-data.md)。
