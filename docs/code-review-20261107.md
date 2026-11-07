# 代码评审 · 2026-11-07

> **评审基线**：工作区 `HEAD = d1c0990`（*筛选模板可「隐藏已用」*），受审文件无未提交改动；工作区只有 `scripts/face_swap_x3.py`、`分镜/` 两个未跟踪项。
> **范围**：`app/web/server.py`、`app/web/jobs.py`、`app/web/batches.py`、`app/web/templates.py`、`app/core.py`、`app/wan_api.py`，以及 `tests/run_all.py` 当前套件。
> **方式**：只读审查；用项目自带 `.dsh` Python 3.12 运行时跑全量离线测试，再对配额与超时复查链路做 3 个不联网、不计费的定向复现。
> **结论一句话**：主链路的用户隔离、状态恢复、重复提交防护总体扎实；真正需要先修的是**配额闸门没有统一入口**、**复查与重试没有互斥**，两者都能导致超出账户上限或二次提交。另有一个测试用例存在随机性，导致全量套件结果不稳定。

## 0. 摘要

| 级别 | 数量 | 一句话 |
| --- | --- | --- |
| P1 | 4 | 重试不查额度；手动挑选可伪造数量；检查与建单非原子；复查期间还能重试 |
| P2 | 2 | 下载无整体超时/大小上限；多图用例随机命中未上传模板导致套件偶发失败 |
| 验证 | — | 首轮全量 24/25；失败用例单独重跑通过，确认是随机性而非稳定缺陷 |

修复顺序建议：先把“条数计算 + 额度检查 + 建单”收进 `JobManager` 同一把锁，再给复查/重建加互斥，最后补下载上限与稳定化测试。

---

## 1. P1：会造成超额或二次计费

### P1-1 失败重试完全绕过账户配额

- **位置**：[`app/web/server.py:727-735`](../app/web/server.py#L727-L735)、[`app/web/jobs.py:845-878`](../app/web/jobs.py#L845-L878)。
- **事实**：`/api/jobs/{id}/retry` 路由只做权限判断，随后直接调用 `jobs.retry_failed()`；该函数在 `self.lock` 内按失败记录重建任务，但没有调用 `quota_check`，也不比较 `len(selected)`。
- **触发**：任意账户达到条数上限后，对一个 `partial/failed/canceled` 任务点“重试失败项”。
- **影响**：新 job 照常入队，派发后产生真实生成费用；账户上限形同虚设。
- **修法**：把额度检查下沉到 `JobManager`，不要依赖路由层。`create()` 与 `retry_failed()` 都应在持锁状态下按**实际条数**检查并完成占位。

### P1-2 手动挑选时校验的 `count` 与实际模板数脱钩

- **位置**：[`app/web/server.py:1022-1035`](../app/web/server.py#L1022-L1035)、[`app/web/templates.py:506-524`](../app/web/templates.py#L506-L524)。
- **事实**：服务端用请求里的 `count` 做配额与“大批量确认”判断；但 `selection="selected"` 分支完全忽略 `count`，按 `template_id` 去重后的数量建单，最多可一次选 3 个。
- **复现**（离线，不提交云端）：

  ```text
  actual_templates=3
  client_count=-100
  quota_error=''
  CLIENT_COUNT_BYPASS_CONFIRMED=True
  ```

- **影响**：账户上限 1 条时，一次选择 3 个模板只按 1 条校验；传负数还能让 `used + count > quota` 永不成立。确认阈值（默认 5）同样可被绕过。
- **修法**：`templates = choose(...)` 之后以 `len(templates)` 作为唯一计费/确认口径；`count` 只允许在 `random` 分支使用，并强制 `1 <= count <= max_items`。

### P1-3 配额检查与建单之间存在并发竞态

- **位置**：[`app/web/server.py:1035-1038`](../app/web/server.py#L1035-L1038)、[`app/web/jobs.py:384-389`](../app/web/jobs.py#L384-L389)。
- **事实**：`quota_check()` 读取 `videos_used()` 时没有持有 `JobManager.lock`；`create()` 直到写 `self.jobs` 才进入 `self.changed`。检查与占位之间没有任何互斥。
- **复现**：账户配额 1，两个请求在 `videos_used()` 返回后同时通过，结果：

  ```text
  created=2 rejected=0
  QUOTA_BUG_CONFIRMED=True
  ```

- **影响**：并发试跑、两个管理员同时重试、或批次生产与试跑交错时，都可能超额建单。
- **修法**：新增 `JobManager.create_within_quota(..., quota_limit)`，在同一 `with self.changed` 内完成：重新计算实际条数 → 检查额度 → 写入 job。路由层保留提前检查仅用于友好提示。

### P1-4 超时复查与失败重试未互斥

- **位置**：[`app/web/jobs.py:646-664`](../app/web/jobs.py#L646-L664)（复查启动并置 `recheck.running=True`）、[`app/web/jobs.py:845-878`](../app/web/jobs.py#L845-L878)（重试未检查该状态）、[`app/web/batches.py:994-1002`](../app/web/batches.py#L994-L1002)（批次重试只查 `active_job_id`，而复查不占该字段）。
- **事实**：复查正在按原 `taskId` 查询云端；同一时间重试会把 `timeout` 记录重新提交。离线复现：

  ```text
  created=ac5aa513a27f while_recheck_running=True
  RECHECK_RETRY_BUG_CONFIRMED=True
  ```

- **影响**：原任务可能已出片并正在补下载，重试又创建一条新任务，造成重复生成与重复计费；批次入口同样受影响。
- **修法**：
  - `retry_failed()` 在锁内遇到 `job.recheck.running` 直接拒绝，文案引导“先复查，确认失败再重试”；
  - 批次重试开始时，对批次内所有关联 job 做同样检查；或给批次增加 `recheck.running` 汇总态；
  - 复查结束时再统一放开重试入口。

---

## 2. P2：可观测性与稳定性

### P2-1 成片下载没有整体截止时间与大小上限

- **位置**：[`app/core.py:148-154`](../app/core.py#L148-L154)。
- **事实**：`urlopen(timeout=600)` 的 `timeout` 作用于单次 socket 操作；`shutil.copyfileobj` 没有总耗时、最大字节数或进度下限判断。
- **触发**：远端持续缓慢返回数据，连接始终不空闲。
- **影响**：单条下载可长期占用工作线程与任务槽，批次一直不收敛；磁盘也可能被异常大文件占满。
- **修法**：以 `time.monotonic()` 设整体 deadline，设置 `MAX_OUTPUT_BYTES`，超限时删除 `.part` 并标记 `download_failed`；测试用本地慢速/超长响应覆盖。

### P2-2 `test_web_multi_images` 依赖随机模板，套件结果不稳定

- **位置**：[`tests/test_web_multi_images.py:50-56`](../tests/test_web_multi_images.py#L50-L56) 使用 `selection="random"`、`count=1`；候选来自 [`tests/test_batches.py:127-129`](../tests/test_batches.py#L127-L129)，其中 `051` 明确是“未上传云端”的模板。
- **现象**：全量首轮 24/25，失败信息为：

  ```text
  AssertionError: (400, ... "所选视频尚未上传到云端模板库（例如 051）...")
  ```

  单独重跑该套件则通过。说明随机抽中 `051` 时，请求在“模板未就绪”分支被拦截，走不到用例预期的 COS 总量闸。
- **影响**：CI/发布前无法用“全绿”作为可信信号；也会掩盖真实回归。
- **修法**：该用例改为 `selection="selected"` 固定使用已就绪模板，或从 `build_rows()` 派生只含 ready 模板的专用 fixture；不要跨套件复用含负例的候选清单。

---

## 3. 已核实无问题 / 值得保留的设计

- **会话与权限**：`auth.py` 的 token 校验同时检查用户 `version`、`enabled`、`expires`，并从账户当前数据取角色；账户改密/角色变更会 bump `version`，旧会话立即失效。未发现角色提升或禁用后仍可访问的路径。
- **用户隔离**：任务、上传图、批次、历史分组、报告下载均按 owner 校验；管理员越权读取属预期能力，普通用户路径未见绕过。
- **重复提交防护**：生成前落 `request_id`、按原 `taskId` 轮询/复查、`unknown` 不自动重提，方向正确；本次问题集中在“重试入口”而非主生成链。
- **状态落盘**：job/batch 使用临时文件 + `os.replace`；路径经 `relocate()` 重锚定；zip 临时名按 job 隔离。
- **限额通道**：大批量与小批量使用独立在飞池，避免小任务被大批量长期压住。

## 4. 验证记录

| 项 | 结果 |
| --- | --- |
| 解释器 | `%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe`（3.12，含 PIL/openpyxl） |
| `tests/run_all.py` | **24/25 通过**；唯一失败 `test_web_multi_images` |
| 单套件重跑 | `test_web_multi_images` **通过**，确认随机性 |
| 配额伪造数量 | 复现通过（3 条实际 vs -100 计费口径） |
| 配额并发 | 复现通过（配额 1 建出 2 个 job） |
| 复查期间重试 | 复现通过（`recheck.running=True` 仍创建新 job） |
| 真实计费/生产 | **未调用**；复现使用临时目录 + `stop_event`，不派发、不联网 |

补充说明：本轮复现只覆盖单进程内并发；跨进程（两台机器共享同一数据目录）没有验证，且当前数据层本身不是跨进程安全的，不应据此推断为“已防护”。

## 5. 建议的回归用例

1. 配额已满时调用 `/retry`，必须 403，且 `videos_used` 不变。
2. `selected` 选 3 个、`count=1/-100`，必须按 3 条校验并触发确认/上限。
3. 两个线程同时建单，配额 1，只允许 1 个成功。
4. `recheck.running=True` 时单任务与批次重试均 409；复查结束后可重试。
5. 慢速下载超过整体 deadline 时，记录进入 `failed/download_failed`，临时文件被清理。
6. 多图用例固定模板集合，连续运行 10 次结果一致。

## 6. 边界

- 未对真实阿里云接口做异常注入，提交后响应丢失导致的重复提交风险仍按既有 `unknown` 策略处理，未在本报告重新评估。
- 未做性能压测；`history_groups`/`work_for` 在数千 job 规模下的耗时未测量。
- 未审查 `scripts/face_swap_x3.py`（未跟踪脚本）与 `分镜/` 素材目录。
