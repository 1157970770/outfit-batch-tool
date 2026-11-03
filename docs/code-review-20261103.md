# 代码评审 · 2026-11-03

> **评审基线**：工作区 `HEAD = 0abf5f5`（*fix(web): 没有上传时间的行退回最近修改时间*），git 工作区干净。
> **范围**：`app/`、`tools/`、`tests/`、`server/*.example.json`、`docs/`（约 13k 行源码，含前端）。
> **方式**：五路并行只读审查（server / jobs+core / batches+tag_api+templates / wan_api+storage / template_store+catalog+前端），
> 交叉复核后由主评审逐条对磁盘实际文件核对行号；`tests/run_all.py` 实测 11/12 通过（唯一失败见 §6）。
> **结论一句话**：分层与安全边界明显强于一般内部工具（静态文件/上传/下载三条路径白名单化、owner 隔离完整、双令牌桶与 `with` 托管信号量正确、前端零 `innerHTML`）；
> 真实风险集中在**失败路径与“控制面”**——调度线程无异常兜底、状态汇总口径、标签真源的写入原子性与乐观锁、以及“钱花了但状态说不清”。

> **修复进度**：§7 第一批（P0-1 + P0-2）已于 2026-11-03 落地，改动未提交 git。
> 涉及 `app/web/jobs.py`（`_load_jobs` 回填 id 并落盘、`_spawn`/`_dispatch_failed`/`_dispatch_queued`、`_run_job` 的状态设置移入 `try` + 排队取消守卫、`_job_outcome`/`_finish_job`、`_on_event` 判空、重试白名单补 `skipped`）、
> `app/core.py`（新增 `status_counts()`，summary 分列 invalid/expired/canceled/skipped/running 并给出 `unsuccessful`）、
> 新增 `tests/test_jobs_state.py` 并登记进 `tests/run_all.py`。全量离线套件 **13/13 通过**（`test_style_usage` 也改用了 `tests/_tmpdir`，不再是环境性失败）。

## 0. 摘要

| 级别 | 数量 | 一句话 |
| --- | --- | --- |
| P0 | 2 | 调度线程异常即全站停摆；一半条目无效被报成「全部完成」且不可重试 |
| P1 | 24 | 计费时点错、重复计费路径、真源可被静默覆盖、协议层无超时/无并发上限、前端可误批量打标 |
| P2 | 30+ | 锁粒度、指标口径、错误处理与前端状态机细节，可一次收掉 |

修复顺序见 §7。前两步合计不到 50 行，能关掉“全站静默卡死”和“假成功”两类故障。

---

## 1. P0（会卡死全站 / 会骗人）

### P0-1 唯一的调度线程没有任何异常兜底 —— 一次异常，全站任务永久排队

- **位置**：[`app/web/jobs.py:232-263`](../app/web/jobs.py#L232-L263)（`_dispatch` 循环体无 `try/except`）、
  [`app/web/jobs.py:139`](../app/web/jobs.py#L139)、[`app/web/jobs.py:255`](../app/web/jobs.py#L255)、
  [`app/web/jobs.py:262`](../app/web/jobs.py#L262)、[`app/web/jobs.py:311-318`](../app/web/jobs.py#L311-L318)。
- **触发**（两条都具体可达）：
  1. `_load_jobs` 用 `self.jobs[job.get("id", path.stem)] = job` 装载，**允许 job 缺 `id` 键**（手改、旧 schema、写盘截断的 `web-data/jobs/*.json`）；
     这类 job 是 `queued`，`_has_dispatchable` 不读 `id` 所以会放行，进入循环后 `self.active[job["id"]]` 直接 `KeyError`。
  2. 线程数耗尽时 `threading.Thread(...).start()` 抛 `RuntimeError: can't start new thread`（每个 job 还会再开 `max_concurrency` 个工作线程，
     20 个 active job 时并不宽裕）。
- **影响**：dispatcher 是 daemon 且无人看护，线程一死 `create()` 照常收单、前端照常显示“已排队”，但**再没有任何 job 会被派发**，
  直到进程重启；唯一证据是 stderr 里一行 traceback。另外 `active` 条目在 `start()` 失败前已写入，该 job 仍是 `queued`，
  下一轮会**重复派发同一 job**（双 runner 写同一 `out_dir`，事件互相覆盖）。
  `_run_job` 的“置 running / 建目录 / `_save`”在 `try`（320 行）**之外**，`finally`（380-383）护不到：`_save` 抛 `OSError`
  （磁盘满、杀软锁文件）或 `unique_batch_dir` 抛 `PermissionError` 时线程直接死，`self.active.pop(job_id)` 永不执行 →
  `max_active_per_user`（默认 1）槽位永久泄漏，job 永卡 `running` 且不可重试，`usage_for` 永久计入 `unfinished`。
- **修法**：循环体包 `try/except` 并写日志（单 job 失败不能拖死调度器）；`_load_jobs` 里 `job.setdefault("id", path.stem)` 并回写；
  `start()` 失败时 `self.active.pop(job_id, None)`；把 311-318 整体挪进 `try`，或改成 `try: <全部> except: <置 failed> finally: <pop active>`；
  把 `dispatcher.is_alive()` 暴露到健康检查，或加“线程不在就重建”的自救。

### P0-2 汇总口径只认 `failed` —— “5 成功 + 5 无效”被报成「全部完成」，且再也重试不了

- **位置**：[`app/core.py:341-358`](../app/core.py#L341-L358)（`summary` 只把 `status == "failed"` 计入 `failed`）、
  [`app/web/jobs.py:360-369`](../app/web/jobs.py#L360-L369)（只读 `success`/`failed`，且 `done = total`）、
  [`app/web/jobs.py:390`](../app/web/jobs.py#L390)（`retry_failed` 的状态白名单）。
- **背景**：`_run_one` 除了 `failed` 还会产出 `invalid`（`build_body` 报错，[`app/core.py:429`](../app/core.py#L429)）、
  `expired`（477）、`canceled`（480）、`skipped`（404、507-509）、`running`（475），这些在 summary 里各有计数，
  但下游判定完全没看它们。
- **触发**：任意“部分失败”批次。最典型：10 条里 5 条 `build_body` 报错（`invalid`）或取消后 5 条 `skipped`。
- **影响**：`success=5, failed=0` → 状态 `succeeded`、文案“全部完成。”、进度条 100%；`partial` 分支（`success>0 and failed>0`）
  对 `invalid/expired/skipped` **永不可达**。用户不看 `records` 就以为交付了；`retry_failed` 因状态是 `succeeded` 直接拒绝
  （“任务尚未结束”），失败项彻底沉底，只能新建任务、重新计费。批次侧 `_reconcile` 会把非 done 记录记成 `failed`，
  所以**批次详情比任务页更接近真相**，两处口径不一致本身就是隐患。
- **修法**：summary 增加 `unsuccessful = total - success`（或分项给 `invalid/skipped/expired` 计数），
  判定改 `success == total → succeeded` / `success > 0 → partial` / 否则 `failed`；`done` 用终态条数；
  `retry_failed` 白名单按记录状态而非 job 状态决定。

---

## 2. P1

### 2.1 计费与账（钱花了，状态说不清）

**P1-1 先下载后记账，下载失败则计费为 0**
[`app/core.py:460-472`](../app/core.py#L460-L472)：`download_file` 在 `usage`/`billable_seconds`/`estimated_cost` 写入**之前**。
成片 URL 过期、网络中断、`out_dir` 无权限、`.part` 写失败都会落到 491 行兜底 `except` → `status="failed"`，
而 `billable_seconds` 仍是 `None`；`_record_cost`（[`app/web/jobs.py:441-461`](../app/web/jobs.py#L441-L461)）回算不到任何依据，
管理端金额系统性偏低。**修法**：计费先落 `rec`，下载单独 `try` 并标 `download_failed`。

**P1-2 提交超时判 `failed`，是当前唯一真实的重复计费路径**
[`app/wan_api.py:974-992`](../app/wan_api.py#L974-L992) 只对 `throttle` 重试，network/超时直接抛；
[`app/core.py:483-487`](../app/core.py#L483-L487) 记 `failed`。此时 `PENDING` 提交可能已在云端建好任务，
用户点“重试失败项”就会**同一条模板二次提交、二次计费**（DashScope 异步提交无幂等键，代码也没有提交前占位）。
**修法**：单列 `submit_unknown` 终态 + 落盘请求指纹；重试前按指纹去重，或先按 `task_id` 查询。

**P1-3 `penalize` 无上界，一次 429 被放大成分钟级停摆**
[`app/wan_api.py:846-850`](../app/wan_api.py#L846-L850)：`_next = max(_next, now) + seconds`，每次 429 无条件叠加且不封顶。
100 个在飞线程同时 429 → `_next` 推后数百秒，全站提交与轮询一起停摆。**修法**：封顶 `now + 30`、窗口内幂等、优先用 `Retry-After`。

**P1-4 批次建 job 与落盘非原子，重复点“生产”会二次计费**
[`app/web/batches.py:547-574`](../app/web/batches.py#L547-L574)：`jobs.create` 内部已 `_save` 并 `notify_all`，
之后才标 `producing` / 写 `active_job_id`；中间的 `usage.record`（[`app/web/style_usage.py:43-47`](../app/web/style_usage.py#L43-L47)，未捕获 `OSError`）
或 `_save` 失败/进程崩溃 → job 在跑但批次仍 `pending`、`active_job_id` 为空、`runs` 少一条。
`_reconcile` 永不认领这些条目，用户再点生产 → 同批模板二次提交。
**修法**：create 成功后立刻写 items/runs/active_job_id 并 `_save`，再 `usage.record`（包 `try`），再 `_save`；
`_load` 时按 `batch_ref` 接管未被 run 认领的 job；失败至少 `jobs.cancel`。

**P1-5 金额/计费秒下发给所有登录用户（违反硬约束 5）**
[`app/web/jobs.py:622-631`](../app/web/jobs.py#L622-L631)：`public()` 的 `summary` 只剔除 `out_dir`，带 `estimated_cost/cost/billable_seconds`；
records 白名单还显式列了 `billable_seconds`/`estimated_cost`。前端每次轮询 `/api/jobs`（3.5s）都会拿到这些字段。
批次侧 [`app/web/batches.py:265-275`](../app/web/batches.py#L265-L275) 的 `unit_price/price_source/amount/options[].amount`
经 470（候选）、580（生产）、703（详情）回到普通用户响应。
前端确实没渲染（`app/web/static/app.js:536` 只在管理页用），但 F12 即可看到单价与花费，可反推 `wan.price_per_second`。
**修法**：按 `role == "admin"` 裁剪；金额统一走 `/api/admin/usage` 与 `/api/admin/accounts/<owner>/work`。

### 2.2 标签真源（`web-data/template_tags.json`）

**P1-6 不传 `revision` 就关闭了乐观锁**
[`app/web/tag_api.py:174`](../app/web/tag_api.py#L174)、185、225-246、418 一律 `revision=data.get("revision")` 下传，
而 [`tools/template_store.py:456-461`](../tools/template_store.py#L456-L461) 只在 `revision is not None` 时比对。
**不传即跳过校验**，在重读后的数据上整行覆盖（`restore` 甚至能不带 revision 整库回滚），与 `tag_api.py` 开头写的
“旧值写入一律 409”矛盾；审计只记后写者。
**修法**：写接口强制要求正整数 `revision`（None/非法 → 400），只有纯追加（`add_video`）豁免；`int(revision)` 的
`ValueError` 要映射成 400 而不是 500。

**P1-7 导入是整份覆盖，且校验只在 dry-run 分支**
[`tools/template_store.py:1154-1156`](../tools/template_store.py#L1154-L1156)（`_check_import` 只在 `dry_run` 里调）、
1178（`self.data["videos"] = videos` 整份替换）、1187（`revision=None`）。
`videos` 来自锁外的 `load_xlsx`（1123），CLI `--import-tags` 期间网页上传的视频或别人的打标会被**静默覆盖**；
任何直接调用非 dry-run 的调用方传空 xlsx 就会清库（`rows <= 0` 检查被跳过）。
**修法**：校验提到分支外；空清单要求显式 `allow_empty=True`；按 id 做差集合并并计数进 report。

**P1-8 原子写没有 fsync，写盘失败也不回滚内存**
[`tools/template_store.py:429-444`](../tools/template_store.py#L429-L444)：`tmp.write_text` → `os.replace`，无 `flush/fsync`，也无目录 fsync；
断电/蓝屏可能得到 0 字节或截断的真源，而 `.bak` 只在“上一版存在且非空”时才写（434）。
更常见的是 [`tools/template_store.py:456-478`](../tools/template_store.py#L456-L478)：`fn()` 已改内存、`_audit_append` 已追加，
`_atomic_save` 抛错后**没有快照可回滚** → 接口返回 503（用户以为没保存），但之后所有 `snapshot`/`usage` 都返回这份未落盘的
“幻影数据”；内存 revision 已 +1 与磁盘分叉，持幻影 revision 的客户端下一次写就莫名 409。
**修法**：`op()` 前 deepcopy，写盘失败恢复 `data`/`revision`/索引后重抛；tmp 写完 `flush()+fsync` 再 replace；
`load()` 失败自动尝试 `.bak` 并标记 `recovered_from_bak`。

**P1-9 xlsx 往返会丢东西**
- 自定义大类被折叠回内置四类：[`tools/template_catalog.py:467-475`](../tools/template_catalog.py#L467-L475) 用 `TAG_CATEGORIES` 当白名单，
  且 `normalize_tag_categories` 没传 `allowed` → 网页端建的自定义大类 export→import 后全部归到第一类，不可自动恢复。
- 公式注入导致丢值：[`tools/template_catalog.py:421-425`](../tools/template_catalog.py#L421-L425) 的 `ws.append(str(...))` 未把单元格标成文本，
  openpyxl 对以 `=` 开头的字符串会写成公式；回读用 `data_only=True`（458）→ 无缓存值得到 `None` → 标签或文件名丢失。
  `validate_tag_name` 只禁 `|;` 和换行，不拦 `=`。
- **修法**：分类接受任意非空字符串并以实际出现的顺序作 `allowed`；写入 `cell.data_type = "s"`，回读对公式单元取原文。

**P1-10 「上传时间」实际全是 0，排序与「新」徽标退化成“今天改过谁”**
- [模板 xlsx 没有时间列](../tools/template_catalog.py#L405-L406)（表头只有 10 列），`load_xlsx` 也不产出这两个键，
  于是 [`tools/template_store.py:223-226`](../tools/template_store.py#L223-L226) 把 `uploaded_at`/`touched_at` 归 0。
- **实测真源**（2026-11-03）：1340 行里 **`uploaded_at` 全部为 0**，其中 **1334 行 `touched_at` 也是 0**（时间戳落在 1970-01-01）；
  只有 6 行是今天被改过标签的（`touched_by` 为真实账号）。
- 于是 [`app/web/static/tag.js:286`](../app/web/static/tag.js#L286) 的排序回退与 [`357-368`](../app/web/static/tag.js#L357-L368) 的「新」徽标
  对这 6 条老视频生效：它们排到“上传时间”第一，并戴上 72h「新」徽标、title 写“上传于 <修改时间>”。
  0abf5f5 让“排序与徽标一致”这个目标成立了，但把一个编辑时间当成了上传时间。
- **修法**（二选一）：xlsx 增加“上传时间/最近修改”两列并在 `load_xlsx` 解析成 epoch；或导入时按 id 从旧真源继承这两个字段。
  另外 `Date.now()/1000 - uploadedAt < 72h` 在客户端时钟超前时恒为真，建议拆成“上传”和“最近变动”两个徽标。

### 2.3 控制面与 HTTP 协议

**P1-11 登录限流按「IP+用户名」分桶，形同虚设且可打满 CPU**
[`app/web/server.py:718`](../app/web/server.py#L718) 的 key = `client_address[0] + "|" + username`：换用户名就是新桶，永远拿不到 429。
[`728-731`](../app/web/server.py#L728-L731) 对未知账号**主动跑一次 310,000 轮 PBKDF2**（约 0.1s CPU/请求，为了抹平时序），
[`app/web/auth.py:79`](../app/web/auth.py#L79) 的 `defaultdict(deque)` 对每个新 key 建空桶且永不清理 → 随机用户名还能刷内存。
局域网内 3-5 个并发即可打满 CPU，正常提交/轮询一起排队。
**修法**：加纯 IP 维度的桶并设全局上限；`failures` 用带上限的 LRU。

**P1-12 鉴权失败不消费请求体，且不处理 `Transfer-Encoding: chunked`**
[`app/web/server.py:589-591`](../app/web/server.py#L589-L591) 未登录直接 `return`，body 一个字节没读；
[`app/web/server.py:297-313`](../app/web/server.py#L297-L313) 只认 `Content-Length`，chunked 时直接当 `{}`。
残留字节会被 `handle_one_request` 当作下一个请求解析（keep-alive 串包）；当前直连只污染攻击者自己的连接，
但**一旦前面挂 nginx / 共享缓存，就是标准请求走私与缓存投毒**。
**修法**：失败分支 `close_connection = True`；显式 411 拒绝 `Transfer-Encoding`；读完校验长度。

**P1-13 没有 socket 超时、没有并发上限**
[`app/web/server.py:1412-1417`](../app/web/server.py#L1412-L1417)：`ThreadingHTTPServer` + `daemon_threads`，每连接一线程，
`BaseHTTPRequestHandler.timeout` 未设。发 `POST /api/uploads` + `Content-Length: 32000000` 然后不发数据，
`self.rfile.read(length)` 永久阻塞，一条连接一个线程；几十条即耗尽，正常用户连不上。
**修法**：类属性 `timeout = 30`（框架自带超时分支），再加 `BoundedSemaphore` 拒绝超额连接。

**P1-14 `persist_web` 用固定 `.tmp`，并发管理操作会写坏 `web.json`**
[`app/web/server.py:142-146`](../app/web/server.py#L142-L146)：所有线程共用一个 `web.json.tmp`。
两个管理员几乎同时改账号 → A 写完 tmp 被 B 以 `"w"` 截断，A 先 `os.replace` 搬走 tmp，B 随后 `os.replace` 抛 `FileNotFoundError`；
更坏是交错内容被发布，下次启动 `load_settings()` 失败 → **全部账号与会话密钥丢失**。
**修法**：`tmp = f"{name}.{uuid4().hex}.tmp"` + 一把 `threading.Lock`。

**P1-15 打包 zip 用固定文件名且边写边发**
[`app/web/server.py:1282`](../app/web/server.py#L1282)、[`app/web/batches.py:724`](../app/web/batches.py#L724)：
同一任务并发下载会在同一路径上 truncate → CRC 错误/半截 zip；`tmp/` 下文件永不清理。
**修法**：请求级 uuid 临时文件 + `os.replace` 成正式名，`finally` 清理；或整段置于同一锁。

**P1-16 `allow_reuse_address` 写在构造之后（死代码）**
[`app/web/server.py:1417-1418`](../app/web/server.py#L1417-L1418)：绑定已在 `__init__` 完成，这行不生效；Windows 的 `SO_REUSEADDR`
语义下两个进程可同时绑 8765（AGENTS.md 第 7 节已观察到该现象，后起的可能抢到带 Cookie 的请求）。
另 [`1443-1444`](../app/web/server.py#L1443-L1444) 的 `访问地址.txt` 写在 `try` 外，写失败会在端口已占用的前提下让进程退出。
**修法**：子类化并设类属性 `allow_reuse_address = False`；地址文件写入包 `try/except OSError` 降级为只打印。

**P1-17 取消打断不了已提交的任务，最长 1800s 仍占在飞名额**
[`app/core.py:436-445`](../app/core.py#L436-L445) 的 `with gate:` 包住整个 submit→wait，
[`app/wan_api.py:997-1032`](../app/wan_api.py#L997-L1032) 的轮询既不看 `stop_event` 也没有回调。
点取消后 `cancel_requested=True`，但已提交的最多 `max_concurrency` 条会继续轮询到各自超时；
该用户大批量槽位（`per_user=1`）一直被占，下一单只能排队；`shutdown()` 同理拉长重启窗口。
**修法**：`wait_task` 增加 `should_stop: Callable[[], bool]`，命中即返回 `stopped` 结果（保留 `task_id`、标 `canceled`），秒级归还信号量。

**P1-18 queued 取消竞态：已取消的排队任务照样跑完整批**
[`app/web/jobs.py:409-413`](../app/web/jobs.py#L409-L413) 的 queued 分支只置 `canceled`，**不设 `cancel_requested`**；
而 `_dispatch` 可能已把该 job 放进 `self.active` 并起了线程，线程随后取得锁 → [`app/web/jobs.py:313`](../app/web/jobs.py#L313)
无条件改回 `running`，351 行的检查也不生效。窗口是毫秒级，但机器繁忙时可达数十毫秒。
**修法**：queued 分支若 `job_id in self.active` 同时置 `cancel_requested`；`_run_job` 开头先判并直接收尾。

**P1-19 `WaitTimeout` 记成 `running`（非终态），该条永久不可重试、永久计入“进行中”**
[`app/core.py:474-476`](../app/core.py#L474-L476) 把状态写成 `"running"`；`retry_failed` 只收
`failed/invalid/expired/canceled`（[`app/web/jobs.py:392-393`](../app/web/jobs.py#L392-L393)），超时项永远无法重试；
`usage_for`/`work_for` 把它永久算 `unfinished`，管理端“进行中”只增不减；云端大概率已生成并计费，却没有任何入口核对。
文案还写死“等待超时(1800s)”，而连续 5 次 network/server/throttle 失败约 90s 就会抛。
**修法**：新增终态 `timeout`（或 `unknown`），保留 `task_id`/`submitted_at`，纳入重试白名单与统计口径。

**P1-20 重试把用量记到“操作人”名下，并丢掉 `batch_ref`**
[`app/web/jobs.py:400-402`](../app/web/jobs.py#L400-L402) 用传入的 `owner` 调 `create`，而 [`app/web/server.py:614`](../app/web/server.py#L614)
传的是当前登录用户。管理员重试他人任务 → 新 job 的 `owner` 变成管理员：用量/金额归属错人，原主看不到结果；
新 job 的 `batch_ref=""`，批次 `history_items` 按 `batch_ref` 回查 `batch_no`（[`app/web/server.py:1129-1131`](../app/web/server.py#L1129-L1131)）断链。
**修法**：用 `source.get("owner")` 建 job 并透传 `batch_ref`，权限判断留在 server 层。

### 2.4 静默产出坏结果 / 前端误操作

**P1-21 EXIF 方向被忽略 → 竖拍定妆图横躺着送进模型**
[`app/media.py:67`](../app/media.py#L67)、[`app/media.py:111`](../app/media.py#L111)：`Image.open` 后直接读 `size`/转 JPEG，
既没 `exif_transpose` 也不保留 EXIF；RGB JPEG 直接透传。手机竖拍的图在模型侧是横的，构图错乱且照常计费。
**修法**：`ImageOps.exif_transpose(img)` 后再处理；顺带加 `Image.MAX_IMAGE_PIXELS` 守卫。

**P1-22 COS REST 兜底丢 STS token → 用临时凭证时归档必然 403**
[`app/cos_rest.py:38-43`](../app/cos_rest.py#L38-L43)、[`217-220`](../app/cos_rest.py#L217-L220)：`CosRestClient` 没有 `token` 参数、
也没有 `x-cos-security-token` 头，`CosRestStorage.__init__` 不传 `cfg.token`；而 SDK 路径是对的（[`app/storage.py:413`](../app/storage.py#L413) `Token=...`）。
`storage.py:641-659` 在无 `qcloud_cos` 时静默降级到 REST；`from_local_profile` 又会把 `~/.tccli` 的临时凭证写进 `cfg.token`
→ 归档全量 `AccessDenied`，报错还不提示“缺安全令牌”。
**修法**：补 `token` 参数与请求头；REST 路径补上与 SDK 一致的 `_with_retry`。

**P1-23 前端 pending 与 selection 不同步 → 单条标签被追加到全库**
[`app/web/static/tag.js:1135-1152`](../app/web/static/tag.js#L1135-L1152) 的四个批量按钮只改 `state.selected`，
不重置 `state.pending`（只有行勾选框 [`335-344`](../app/web/static/tag.js#L335-L344) 会重置）；
[`565`](../app/web/static/tag.js#L565) 在无选中时把 pending 设为预览那条的标签；
[`465`](../app/web/static/tag.js#L465) 又在多选时把 mode 切成 `add`。
路径：点开任意一条 → 「全选全部 N 条」 → 「保存」 → 把这条的标签**追加给 N 条**（N 可达数千），只能回滚版本撤销。
**修法**：抽 `setSelection(nextIds)` 统一重置 pending（0 条取预览行、1 条取该行、≥2 条清空），四个按钮都走它；
mode 只在 `lastMulti` 变化时改。

**P1-24 上传 XHR 没有超时，UI 可永久卡在“上传中…”**
[`app/web/static/tag.js:1020-1057`](../app/web/static/tag.js#L1020-L1057)：只挂 `onprogress/onload/onerror`，没有 `xhr.timeout`/`ontimeout`；
连接建立但挂起时 Promise 永不 resolve，`state.upload.busy` 一直 true → 弹窗关不掉、按钮永久 disabled 且文案“上传中…”，
只能刷新，且不知道哪些文件已成功。
**修法**：`xhr.timeout = 120000` + `ontimeout` 里置失败并 resolve；`finally` 无条件清 busy；允许“重试失败项”。

---

## 3. P2 清单（可一次收掉）

**任务 / 队列 / 统计**
- [`jobs.py:369`](../app/web/jobs.py#L369) `done = total` 与 P0-2 一起改：有 skipped/invalid 时进度条仍 100%。
- [`jobs.py:495-505`](../app/web/jobs.py#L495-L505) `unfinished` 把“job 数”和“条数”混在一个量里，`success_rate` 分母是 items 却拿 success 比。
- [`jobs.py:643-644`](../app/web/jobs.py#L643-L644) `queue_position` 每次 `public()` 重算 O(n log n)，`list_for` 对每 job 调一次 → O(n²) 且全在全局锁内。
- [`jobs.py:197`](../app/web/jobs.py#L197) 队列上限全站 50，无 per-owner；单个用户可占满。
- [`jobs.py:135-141`](../app/web/jobs.py#L135-L141) 的 `interrupted` 不可重试（390 白名单）也不进任何统计口径（495/502）；重启时 queued 任务下次启动会被自动重新派发（`shutdown()` 不处理），与 `running→interrupted` 的语义不一致。
- [`settings.py:78`](../app/web/settings.py#L78) `small_lane_items` 钳位是 `min(20, value)`：配成 10 就把 12 条快速通道发给 20 条的批次，静默突破“≤3 条”契约。建议直接钳死 3。
- 持全局锁做全量 JSON 写盘：[`jobs.py:278-308`](../app/web/jobs.py#L278-L308) 每条日志、每个进度事件都重写整个 job（含全部 records），
  100 条批次约 300-500 次全量写，且 `self.log()` 在 `with gate:` 内调用（[`core.py:519`](../app/core.py#L519)）时还占着在飞名额；
  `/api/jobs` 3.5s 一次轮询会被拖慢。建议锁内只改内存 + 置脏，专职线程合并落盘。
- [`server.py:1027`](../app/web/server.py#L1027) 无锁遍历 `app.jobs.jobs.values()`（调度线程会插入新键）；[`server.py:531-533`](../app/web/server.py#L531-L533)、571-573 同样直接迭代 `settings.users`，而管理端在别的线程增删 → 偶发 `RuntimeError`。

**批次与标签**
- [`batches.py:613-618`](../app/web/batches.py#L613-L618) `close()` 允许把草稿直接关掉（跳过筛选/锁定）；[`batches.py:580`](../app/web/batches.py#L580) 生产估算没传 `output_seconds_of`，与列表口径不一致（duration=2/10 时偏差 30%+）。
- [`templates.py:185`](../app/web/templates.py#L185)、219 `next(sheet.iter_rows(...))` 无默认值 → 空工作表触发未捕获 `StopIteration`，`--import-tags` 崩栈 / 网页 500。
- [`templates.py:340`](../app/web/templates.py#L340) `attach_tag_store` 用 `if tag in categories` 静默丢库外标签（行变 untagged、退出候选集），与 xlsx 路径“直接 raise”的口径相反。
- [`server.py:436-442`](../app/web/server.py#L436-L442) `/api/style-usage?top=` 把全库账本（含**他人**的 `batch_no`/`batch_id`）返回给任何登录用户——硬约束 4 的边界缺口。
- [`batches.py:121-124`](../app/web/batches.py#L121-L124)、669：`_load` 采信 JSON 里的 `id`（可 `../..`），`_save`/`unlink` 会受害；id 与文件名不一致会另建文件、删不掉旧文件。
- [`batches.py:444`](../app/web/batches.py#L444)（`limit or 500` 把显式 0 变 500）、[`359`](../app/web/batches.py#L359)（非法 duration 静默变 7 秒）、
  [`661-677`](../app/web/batches.py#L661-L677)（删除批次不清理 tmp zip 与 StyleUsageLedger 记账）、
  [`722-723`](../app/web/batches.py#L722-L723)（done 条目视频缺失时静默出不完全 zip）。
- [`tag_api.py:140`](../app/web/tag_api.py#L140) catalog rebuild 失败只记 `catalog_error`，而 `snapshot` 以 revision 相同早退 → 之后一直 `unchanged`，候选集 stale（弃用视频仍在候选）。
- [`tag_api.py:341-346`](../app/web/tag_api.py#L341-L346) 上传后缀无白名单、`content_type` 写死 `video/mp4`；[`300-303`](../app/web/tag_api.py#L300-L303) COS tagset 静默截断（Key 16 字、Value 120 字、只留 10 个）；[`164`](../app/web/tag_api.py#L164)/[`397`](../app/web/tag_api.py#L397) 把本机绝对路径回给任意登录用户。
- [`templates.py:418-447`](../app/web/templates.py#L418-L447) `public_tags` 全量无分页；[`template_store.py:378-426`](../tools/template_store.py#L378-L426) `library/usage/stats` 读不加锁；[`995-1013`](../tools/template_store.py#L995-L1013) 孤儿 `.gz/.tmp` 永不清理。

**HTTP 错误处理（一处“请求壳 + 错误壳”可统一收）**
- [`server.py:428-429`](../app/web/server.py#L428-L429) `raw_limit.isdigit()` 对 `"²"` 为真但 `int()` 抛 `ValueError`；[`1338-1343`](../app/web/server.py#L1338-L1343) `resolve()` 在 `try` 外（`a%00b` → `ValueError`）。
- [`server.py:833-855`](../app/web/server.py#L833-L855) `int(data.get("count"))` 只 catch `ValueError`，`{"count":[]}` → `TypeError`；且 `_error(exc)` 会把 `invalid literal for int()` 原样回显。
- [`server.py:1358-1360`](../app/web/server.py#L1358-L1360) 空文件：`end = max(0, size-1) = 0` → `length = 1`，实际发 0 字节（客户端等 1 字节、连接错位）。
- [`server.py:1295-1296`](../app/web/server.py#L1295-L1296) `zip_path.stat().st_size == 0` 永远不成立（空 zip 也有 22 字节 EOCD），分支是死代码；用户拿到空 zip 而不是 404。
- [`server.py:1319-1336`](../app/web/server.py#L1319-L1336) `_preview` 在头已发出后再调 `_error()` → 同一连接两个状态行；且绕过 `_headers()`，缺 CSP/`X-Frame-Options`；上游 chunked 时也不透传长度。
- [`server.py:1294`](../app/web/server.py#L1294)、[`944`](../app/web/server.py#L944) 错误文案回显绝对路径（`_redact` 只遮密钥不遮路径）。
- [`server.py:1147-1161`](../app/web/server.py#L1147-L1161) CSV 公式注入：模板名可来自普通用户上传的文件名（`tag_api.py:351`），以 `= + - @` 开头时应在单元格前置 `'`。
- [`server.py:315-317`](../app/web/server.py#L315-L317) `Host.split(":", 1)[0].strip("[]")` 对 `[::1]:8765` 得到 `"["` → IPv6 面板与 mDNS 名（`xxx.local`）访问全站 421；建议 `urlsplit("//"+host).hostname`。
- [`server.py:349-357`](../app/web/server.py#L349-L357) `_tags` 只 catch `ApiError/ValueError`，`StoreError` 冒泡 → 连接被关，前端只看到“网络错误”。

**引擎 / 存储 / 前端杂项**
- [`core.py:625-626`](../app/core.py#L625-L626) `_emit`/`log` 回调无隔离：回调抛 `OSError` 会被 491 行兜底成“该条 failed”，或在 337 行打断整批。
- [`core.py:126-132`](../app/core.py#L126-L132) `download_file` 失败时 `<id>.mp4.part` 不清理；`_save`/`_save_state` 的 `.tmp` 写入中断也残留。
- [`core.py:633-644`](../app/core.py#L633-L644) `unique_batch_dir` 的“检查在锁内、`mkdir` 在锁外”（`core.py:316`）是 TOCTOU；建议 `mkdir(exist_ok=False)` + `FileExistsError` 重试。
- [`wan_api.py:1002`](../app/wan_api.py#L1002)、1006 用墙钟 `time.time()` 做 deadline（同文件限流器用 `monotonic`）：NTP 前跳立即判超时（走 P1-19 死路），后跳远超 1800s。
- [`wan_api.py:890`](../app/wan_api.py#L890) docstring 仍写“提交与查询同桶”，与 855 行的双桶实现相反；`DEFAULT_MAX_INFLIGHT=50`（809）与 AGENTS 写的 100 不一致（当前 `config.json` 是 100，实际无碍）。
- [`wan_api.py:668`](../app/wan_api.py#L668) `data_obj.get` 未判类型（JSON 数组/null → `AttributeError` 绕过 `classify_error`）。
- [`media.py:201-220`](../app/media.py#L201-L220) 先 probe 后校验扩展名（`.txt` 改 `.mp4` 漏过）；`validate_image` 放行 8000×8000（92），`_flatten_to_jpeg` 峰值可能数百 MB。
- [`batch.js:384-385`](../app/web/static/batch.js#L384-L385) `URL.createObjectURL` 从不 `revoke`；[`508-527`](../app/web/static/batch.js#L508-L527) `fetchCandidates` 无请求代次（切批次/连点刷新时旧响应覆盖新选择）；[`1047-1064`](../app/web/static/batch.js#L1047-L1064) 轮询会在输入中重建提示词 textarea（丢焦点/IME）；[`1239-1240`](../app/web/static/batch.js#L1239-L1240) `renderItems` 的 signature 漏 `name`/`archive_warning`/`download_url`。
- [`tag.js:1118-1122`](../app/web/static/tag.js#L1118-L1122) `loading` 时 `load()` 直接 return 却照样 toast“已刷新”；[`737-740`](../app/web/static/tag.js#L737-L740) 分类 select 失败不回滚；[`1199-1209`](../app/web/static/tag.js#L1199-L1209) `#tag-lib-error` 是死分支；[`553`](../app/web/static/tag.js#L553) 切换视频时旧 `player.onerror` 没清；[`980`](../app/web/static/tag.js#L980) 同名同大小文件静默跳过。

---

## 4. 已核实无问题（不用再查）

- **路径与越权**：静态文件（[`server.py:1338-1349`](../app/web/server.py#L1338-L1349)）、上传落盘（738-800）、下载（1264-1297）三条路径都是白名单 + `resolve/relative_to`
  或 id 反查；`..`、`\`、`%2f`、绝对盘符、UNC、ADS 均落到根外 404；上传落盘名是 uuid。
- **owner 隔离**：逐接口判定完整（`batches._authorized` 覆盖全部 batch 端点），`?scope=all` 需 admin，`owner=` 只在 admin 时生效。
- **额度实现**：提交/轮询双令牌桶确实独立，429 只罚对应桶（未跨桶）；限速基座是 `time.monotonic`，`_next = max(now,_next)+interval` 无漂移、空闲不积 burst；
  在飞信号量用 `with` 托管，异常路径不漏 release；鉴权类错误不重试。
- **前端安全**：全站静态 JS **零 `innerHTML`/`insertAdjacentHTML`/`document.write`**，渲染一律 `createElement + textContent`，XSS 面已消除。
- **响应头**：`nosniff` / `X-Frame-Options: DENY` / CSP `frame-ancestors 'none'` / `no-store` 齐全（`_preview` 除外，见 P2）。
- **密码与会话**：PBKDF2 310k 轮 + 每次登录随机盐，HMAC 会话带版本与过期校验（`auth.py`）。
- **测试**：`tests/run_all.py` 实测 11/12 通过。

## 5. 文档漂移

| 位置 | 写的 | 实际 |
| --- | --- | --- |
| `docs/architecture.md` §3.1 | 密码用 scrypt | PBKDF2-HMAC-SHA256，310,000 轮（`auth.py`） |
| 模块行数表 / `AGENTS.md` | server.py 1161 | 1453 |
| 同上 | jobs.py 601 | 667 |
| 同上 | batches.py 822 | 846 |
| 同上 | tag.js 1160 | 1249 |
| 同上 | batch.js 1771 | 1826 |
| 同上 | template_store.py 1079 | 1211 |
| 同上 | template_catalog.py 537 | 600 |

（`wan_api.py` 记的 1060 与实际一致。）

## 6. 测试缺口与建议补测

现有 12 个离线套件偏向“正常路径端到端”，真正危险的状态机一条断言都没有。建议补：

1. `_dispatch` 遇到缺 `id` 的 job 文件 / `Thread.start()` 抛错时，调度线程仍存活且其它 job 正常派发。
2. queued 状态取消后，job 不得进入 `running`、不得产生任何提交。
3. 一半 `invalid` + 一半 `success` 的批次 → job 状态必须是 `partial`，`done` 不得等于 `total`，且 `retry_failed` 可重试。
4. `retry_failed` 后新 job 的 `owner`/`batch_ref` 与原 job 一致。
5. 不带 `revision` 的 `set_tags` 必须 400；带旧 revision 必须 409。
6. `import_from_xlsx` 在并发新增后不得丢行；空清单非 dry-run 必须拒绝。
7. xlsx export→import 往返后：自定义大类、以 `=` 开头的标签名、`uploaded_at/touched_at` 不丢。
8. ~~`tests/test_style_usage.py` 在本机沙箱下会因 tempfile 清理 `PermissionError` 失败~~——已改用 `tests/_tmpdir.tempdir()`（0o777 + atexit 清理），现可通过。

## 7. 修复顺序建议

| 批次 | 内容 | 规模 | 收益 |
| --- | --- | --- | --- |
| 第一批 | P0-1（`try/except` + `setdefault("id")` + `start()` 失败回滚）、P0-2（汇总口径 + 重试白名单） | < 50 行 | 关掉“全站静默卡死”和“假成功” |
| 第二批 | P1-1/P1-2/P1-3/P1-4（计费时点、`submit_unknown`、`penalize` 封顶、批次原子性） | 中 | 钱与账对得上，消除二次计费 |
| 第三批 | P1-6~P1-10（强制 revision、导入合并、fsync、失败回滚、xlsx 时间列） | 中 | 标签真源“可恢复但不被静默改写” |
| 第四批 | P1-11~P1-18（限流分桶、body 消费、socket 超时、uuid tmp、取消生效） | 中 | 协议面与并发鲁棒性 |
| 第五批 | P1-21~P1-24 + §3 P2 | 大 | 产出正确性与体验 |

### 7.1 第一批实际落地内容（已完成，未提交）

> 完整清单、行为变化与回滚方式见 [fix-20261103-p0.md](fix-20261103-p0.md)。

| 文件 | 改动 | 对应问题 |
| --- | --- | --- |
| `app/web/jobs.py` | `_load_jobs`：补 `id` 并回写、跳过非 dict、`running`→`interrupted` 统一走 `dirty` 落盘 | P0-1 根因 |
| `app/web/jobs.py` | 新增 `_spawn` / `_dispatch_failed` / `_dispatch_queued`；线程起不来时先回滚 `active` 与 lane 计数，再判失败，异常不再逃出调度线程 | P0-1 |
| `app/web/jobs.py` | `_run_job`：置 running / 建目录 / 落盘移入 `try`（`finally` 一定释放槽位）；`status != queued` 或已请求取消时直接返回 | P0-1、P1-18 |
| `app/web/jobs.py` | 新增 `_job_outcome` / `_finish_job`：只有 `success == total` 才 succeeded；否则 partial/failed，`done` 不把 `running` 算完成 | P0-2 |
| `app/web/jobs.py` | `retry_failed`：白名单补 `skipped`；仍有 `running` 时给专门提示 | P0-2 配套 |
| `app/core.py` | 新增 `status_counts()`；summary 分列 `invalid/expired/canceled/skipped/running`，附 `unsuccessful`，`total` 仍取批次条数 | P0-2 |
| `tests/test_jobs_state.py` | 6 组断言：id 回填、起线程失败不杀派发、无 id 不挡后续、建目录失败释放槽位、收尾口径、汇总口径 | 回归防护 |

未纳入本批（仍按 §7 后续批次处理）：重试沿用当前登录用户作 owner / 丢 `batch_ref`（P1-9）、`interrupted` 不可重试（P1-10）、`WaitTimeout` 记 `running` 导致永久不可重试（P1-19）——后者本次只在进度条与重试提示上做了兜底，状态机本身未改。

> 评审阶段为只读；第一批修复已按 §7.1 落地（工作区改动，尚未提交 git）。
