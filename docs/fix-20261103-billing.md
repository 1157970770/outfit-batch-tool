# 第二批修复记录：计费口径（2026-11-03）

> 基线 `40330f6`（第一批 P0 之后）。对应评审 [code-review-20261103.md](code-review-20261103.md)
> §2.1 的 P1-1 ~ P1-4，即「钱与账对得上」的四件事。

## 1. 修的四件事

### 1.1 下载失败，账不能凭空消失（P1-1）

原 `app/core.py` 的 `_run_one` 顺序是「下载 → 算 usage → 记账」。云端出片即已按秒计费，
一旦 `download_file` 抛异常（磁盘满、网络断、临时目录不可写），控制流直接落到通用 `except`：
`status=failed`，而 `billable_seconds` / `estimated_cost` / `cost` 全是 `None`——
这笔真实发生的费用在管理端「账户用量」里不存在。

现在改为**先记账、再下载**：

- 拿到终态结果就立刻写 `usage` / `billable_seconds` / `estimated_cost` / `seconds`；
- 下载失败 → `status=failed` + `download_failed=True`，`error` 写明
  「云端已出片并按 N 秒计费，但下载失败：…」；
- 下载成功 → `status=success`，`_verify` 与 `_archive` 的异常不再吞掉整条记录
  （校验失败只写 `verify_error`）。

### 1.2 提交结果未知，既不能当失败也不能当没花钱（P1-2）

`submit()` 抛 `WanError` 时，原代码一律 `failed`。但 5xx / 网络中断意味着请求**可能已送达**，
云端可能已经建了任务；用户看到「失败」再点一次重试，就会生成第二条视频、付两次钱。

- 新增 `SubmitUnknown(WanError)`（`category="unknown"`），记录状态 `unknown`；
- 新增 `submit_outcome_unknown(exc)` 判定：
  - `http_status >= 500` → 未知；
  - `category in ("network", "timeout")` 且没有 `http_status` → 未知；
  - 4xx（参数 / 鉴权 / 限流）是服务端明确拒绝，**一定没建任务**，仍按普通失败处理，可以安全重试。
- `app/web/jobs.py`：`retry_failed` 遇到 `unknown` 直接拒绝并提示「请先到控制台核对，
  确认没有重复后再新建任务」；`usage_for` 把 `unknown` 计入失败侧，避免这笔账
  「既不算成功也不算失败」而静默消失。

### 1.3 限流惩罚不再无上界（P1-3）

`_RateLimiter.penalize()` 原来是 `_next = max(_next, now) + seconds`，连续 429 会把
`_next` 越推越远，而 `_next` 是**全站共享**的：一个坏请求能让提交与轮询一起停摆几十分钟。

现在 `_RateLimiter.PENALTY_CAP = 60.0`，`penalize` 取 `min(target, now + PENALTY_CAP)`：
最多再缓一分钟，且不会把已有的等待时间抹掉（`target` 仍从 `max(_next, now)` 起算）。

### 1.4 批次生产不再可能「同一批做两遍」（P1-4）

原 `_start_run` 的顺序是「`jobs.create` → 改条目 → `_save`」。`jobs.create` 一落盘，
job 就已经在队列里、可能已经开始提交；此时若 `_save` 抛错（磁盘满、序列化失败），
批次文件仍是旧的「待生产」，用户刷新后看到还能点，再点一次 → 同一批模板再建一个 job → 二次计费。

现在改成两阶段落盘：

| 阶段 | 动作 | 失败时 |
| --- | --- | --- |
| 建任务前 | 写 `batch["pending_run"] = {template_ids, job_id: "", started_at}` 并落盘 | 抛 503「批次状态写入失败，本次没有创建任务，请稍后重试」；内存标记清掉 |
| 建任务后 | `marker["job_id"] = job_id` 立刻落盘 | 抛 503「任务已创建（<id>），但批次状态写入失败…请勿重复生产」；标记留在内存 |
| 收尾 | 标记条目 producing、`active_job_id`、`runs`，然后 `pop("pending_run")` 落盘 | 抛 503，并把 `pending_run` 放回内存，刷新即对账 |

对账侧新增 `_adopt_pending_run()`，`_reconcile` 在 `active_job_id` 为空时先调用它：

- 有标记但没 `job_id` → 任务根本没建，条目保持 `pending`，清标记，可以正常再生产；
- 有 `job_id` 但 `jobs` 里查不到 → 云端状态无法确认，**不自动重发**，把对应条目标
  `failed` +「上次生产中断：对应任务已找不到，请核对后重试。」；
- 有 `job_id` 且 job 在 → 接回 `producing`，让正常对账继续收敛。

另外 `usage.record`（款号-模板记账本）改为纯审计：失败只 append 一条 warning，不影响已建好的任务。

## 2. 改动清单

### 2.1 引擎与限流

| 文件 | 位置 | 改动 |
| --- | --- | --- |
| `app/core.py` | `EngineError` 之后 | 新增 `SubmitUnknown`（`category="unknown"`，文案含「不要直接重试」） |
| `app/core.py` | `status_counts()` | counts 增加 `unknown` 键 |
| `app/core.py` | 模块级 | 新增 `submit_outcome_unknown(exc)` |
| `app/core.py` | `_run_one` 成功分支 | 先写 usage/计费，再 `download_file`；下载失败保账并标 `download_failed`；`_verify` / `_archive` 异常降级为附加字段 |
| `app/core.py` | `_run_one` 异常分支 | 新增 `except SubmitUnknown` → `status="unknown"` |
| `app/core.py` | `_submit_and_wait` | `submit()` 包 `try/except WanError`，结果未知时转 `SubmitUnknown` |
| `app/wan_api.py` | `_RateLimiter` | 新增 `PENALTY_CAP = 60.0`；`penalize` 取 `min(target, now + cap)` |

### 2.2 网页端

| 文件 | 改动 |
| --- | --- |
| `app/web/jobs.py` | `usage_for` 的 `failed_states` 补 `unknown`；`retry_failed` 遇到 `unknown` 拒绝并提示先核对控制台 |
| `app/web/batches.py` | `_reconcile` 先 `_adopt_pending_run`；新增 `_save_quiet` / `_adopt_pending_run`；`_start_run` 两阶段落盘 + `usage.record` 降级为 warning |
| `app/web/static/app.js` | `statusText` 增 `unknown: '结果未知'`；失败计数过滤含 `unknown` |
| `app/web/static/style.css` | 新增 `.status.unknown`（浅橙底深橙字） |

### 2.3 测试

| 文件 | 内容 |
| --- | --- |
| `tests/test_billing.py` | 新增 10 组断言：惩罚封顶 / 不回退、unknown 判定（5xx 与 4xx 分界）、下载失败仍保账、unknown 单列、标记先于建任务、建完写盘失败后能认领、job 丢失标失败、没建任务可再排 |
| `tests/run_all.py` | `REQUIRED` 插入 `test_billing`（放在 `test_jobs_state` 之后） |

## 3. 行为变化对照

| 场景 | 之前 | 现在 |
| --- | --- | --- |
| 云端出片、下载失败 | `failed`，计费字段全空 | `failed` + 保留 `billable_seconds` / `estimated_cost`，报错写明已计费 |
| 提交遇到 502 / 连接重置 | `failed`，可直接重试 | `unknown`，重试被拒并提示先核对控制台 |
| 提交遇到 400 / 401 / 429 | `failed` | `failed`（不变，服务端明确拒绝，可安全重试） |
| 连续 429 十几次 | `_next` 可能推到几十分钟后 | 最多推后 60 秒 |
| `jobs.create` 之后写盘失败 | 批次看起来仍是「待生产」 | 503 + `pending_run` 标记，下次对账认领，不会重复生产 |
| job 建成功但进程被杀 | 条目永远 `pending`，用户再点就重复计费 | 刷新后自动接回 `producing` 继续对账 |
| job 标记存在但 job 文件没了 | 条目永远 `pending` | 标 `failed` +「请核对后重试」，不自动重发 |
| 款号记账本写失败 | 整次生产报错（job 已建） | 只记一条 warning，不影响出片 |
| 管理端用量统计 | `unknown` 既不进成功也不进失败 | 计入失败侧，金额仍可见 |

## 4. 测试与验证

```bash
uv run --no-project python tests/test_billing.py      # 10 组断言
uv run --no-project --with pillow --with openpyxl python tests/run_all.py
```

结果：`test_billing` 10/10；全量 **14/14 通过，0 失败**（含 `test_web`、`test_batches` 端到端）。

## 5. 未纳入本批

- **P1-5（`batch_ref` 丢失 / 重试记到当前登录用户）**：`retry_failed` 的 owner 与 `batch_ref`
  仍沿用旧逻辑，属于「账记给谁」的问题，放到第三批一起做。
- **P1-7（取消打不断轮询）**：`wait_task` 仍不读 `stop_event`；本批只保证「已提交的任务不会
  被当成失败重复提交」，取消的时效性未变。
- **`unknown` 的自动对账**：目前只做「拒绝重试 + 提示核对」，没有主动去云端按 `request_id`
  反查任务是否存在——那需要新的查询接口与运维决策，不适合塞进本批。

## 6. 回滚

```bash
git revert <本批提交>
```

- 无数据结构变更：`pending_run` 只是批次 JSON 里的可选字段，老版本读到会忽略；
  `unknown` 是新的 status 取值，回滚后旧代码会把它当未知状态（`total` 计、其余不计），
  不会崩。
- 已产生的 `web-data/jobs/*.json` 不需要迁移；回滚后 `download_failed` / `verify_error`
  等附加字段会被忽略。
