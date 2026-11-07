# 网页版代码架构（v0.6.0）

> 只讲**网页版**这条主线。桌面 exe 版已暂时废弃，其界面代码不在本文范围。
> 快速上手看 [../AGENTS.md](../AGENTS.md)；参数与运维看 [project-summary-20261102.md](project-summary-20261102.md)。

## 1. 进程与线程模型

单进程，没有 Web 框架、没有异步 IO、没有队列中间件——全部用标准库。

```
python run_web.py
  └─ app/web/cli.py::main               解析参数；--init/--add-user 等只改 web.json
      └─ app/web/server.py::serve       加载配置 → 建 WebApplication → 监听
          ├─ ThreadingHTTPServer        每个 HTTP 请求一个线程（daemon）
          ├─ JobManager._dispatch       1 个常驻调度线程：把 queued 变成 running
          └─ 每个运行中的 job            1 个线程 → BatchRunner 内部线程池（max_concurrency）
```

| 组件 | 线程数 | 说明 |
| --- | --- | --- |
| HTTP 请求 | 每请求 1 个 | `daemon_threads = True`，连接断了不影响任务 |
| 调度 | 1 | `_dispatch` + `threading.Condition`，1 秒超时轮询兜底 |
| 单个 job | 1 + N | `_run_job` 自己 1 个，`BatchRunner` 再开 N 个（`wan.max_concurrency`） |
| 云端在飞 | ≤ `max_inflight` | 由 `threading.Semaphore` 控制，与线程数无关 |

**状态存在哪**：内存里是 `JobManager.jobs` / `BatchManager.batches` 字典，每次变更都写 JSON
（`tmp` 文件 + `os.replace` 原子替换）。所以进程被杀只会丢“最后一瞬间”的状态；
重启时 `running` 会被改写成 `interrupted` 并提示核对云端任务。

**启动为什么要 7 秒**：`load_settings()` 要读两个 JSON、拉 `manifest.json`（网络）、
解析标签工作簿（xlsx，openpyxl）。这段时间端口还没 bind，别用“连不上”判定启动失败。

## 2. 模块职责

| 文件 | 行数 | 职责 | 关键入口 |
| --- | --- | --- | --- |
| `app/web/cli.py` | 190 | 命令行：初始化、增删账号、自检、启动 | `main()` |
| `app/web/server.py` | 1696 | 路由、鉴权、静态文件、CSV/ZIP/视频下载 | `WebApplication`、`make_handler`、`serve` |
| `app/web/jobs.py` | 800 | 任务队列、调度、事件回写、用量统计 | `JobManager.create/_dispatch/_run_job` |
| `app/web/batches.py` | 959 | 批次生命周期、候选筛选、锁定、多轮生产、历史分组 | `BatchManager.create/candidates/lock/produce` |
| `app/web/templates.py` | 557 | 模板目录：标签工作簿 + 云端 manifest 交叉索引 | `TemplateCatalog` |
| `app/web/settings.py` | 234 | 配置读取与上限裁剪、单价兜底 | `load_settings`、`WebSettings` |
| `app/web/auth.py` | 95 | 密码哈希、HMAC 会话、登录限流 | `SessionSigner`、`LoginLimiter` |
| `app/web/__init__.py` | 2 | `WEB_VERSION` | — |

下层（两条产品线共用）：

| 文件 | 职责 |
| --- | --- |
| `app/core.py` | `BatchRunner`：线程池、逐条 提交/轮询/下载、`on_event` 回调 |
| `app/wan_api.py` | `WanClient`：DashScope/RunningHub 适配、提交桶/轮询桶、在飞信号量 |
| `app/config.py` | `server/config.json` → 内部扁平 `WAN_*` / `COS_*` |
| `app/storage.py`、`app/media.py` | COS/本地存储、图片与视频预检 |

## 3. 一次请求怎么走

### 3.1 鉴权

- 登录 `POST /api/login` → 校验 PBKDF2-HMAC-SHA256 哈希（310,000 轮 + 随机盐）→ `SessionSigner.issue()` 签 HMAC Cookie；
- 每个请求 `user_for_token()` 验签 + 查 `web.json` 里的 `version`（改密码后旧会话立即失效）；
- 登录失败限流是双桶：IP 桶 30 次 + 账号桶 8 次 / 10 分钟；未知账号也执行一次等价 PBKDF2，抹平时序差。

### 3.2 小批量试跑

```
POST /api/uploads           每张图各 1 次 → web-data/uploads/<xx>/<32hex>.png + 元数据（≤10 张）
POST /api/jobs              upload_ids[]（兼容旧的单个 upload_id）→ JobManager.create(images=...)：
                              · 校验提示词/条数/画质/时长
                              · lane = small（≤ small_lane_items=3）否则 big
                              · 写 web-data/jobs/<12hex>.json，状态 queued
调度线程 _dispatch          job 槽位与每用户在飞都允许 → 线程跑 _run_job
_run_job                    BatchRunner.run()：提交 → 轮询 → 下载
                            每条事件 _on_event → 回写 records / done / summary
前端                        每 3.5 秒 GET /api/jobs；详情页 GET /api/jobs/<id>
完成后                      GET /api/jobs/<id>/files/<template>.mp4 或 /zip
```

### 3.3 大批量批次

```
POST /api/batches                 建草稿（批次号唯一、绑图、提示词、画质、时长）
POST /api/batches/<id>/image       草稿态整组重设（换定妆图/重新选 1+9 张）
POST /api/batches/<id>/images      op=add 追加细节图 / op=remove 删细节图（锁定后仍可用；主图不可删）
POST /api/batches/<id>/candidates 按标签筛选 → 命中清单（写入 items，状态 pending）
                                   body: tag_filters（包含）/ tag_excludes（排除）/ hide_used / rating_min / sort
POST /api/batches/<id>/lock       锁定这批清单；之后可反复生产
POST /api/batches/<id>/produce    取前 N 条 pending → JobManager.create(batch_ref=...)
                                  一个批次可以跑多轮，直到 pending 耗尽
POST /api/batches/<id>/prompt     改提示词（分批生产阶段就地改）：运行中也能改，
                                  提交那一刻已快照进 job，所以只对下一轮生效；关闭后 409
POST /api/batches/<id>/close      关闭批次（不可再生产）
POST /api/batches/<id>/delete     删除（有运行中任务时拒绝）
```

**COS 归档命名（v0.10.0）**：`{prefix}/output/{YYYY-MM-DD}/{上衣款号}/{批次id}/{编号}-{指纹8}.mp4`。
**6 栏位标签**（`app/core.py` 的 `TAG_SLOTS`）：上衣（必填，进路径）/ 内衬 / 裤子 / 鞋子 / 场景 / 特殊备注。
上衣缺省时该段省略（老任务路径不变）；款号原样保留，空格等字符转短横线。
对象另带自定义元数据 `x-cos-meta-style-no`（ASCII 款号值），以及（需 `cos:PutObjectTagging` 权限的）
中文键值标签：`上衣=xx 内衬=yy … + batch=批次id`；无该权限时跳过（只记一次警告，不影响出片）。

**云端归档检索（管理端）**：`GET /api/admin/archive-search?style=款号` 先查本机任务账本里
记录的 `rec["cos_key"]`（归档时记账，重启不丢），空了再按路径前缀列举兜底（需 ListObjects 权限）；
`POST /api/admin/archive-sign` 给勾选对象批量签 7 天下载链接（COS 永久密钥签名上限）。

**状态回填**：`BatchManager._reconcile()` 用关联 job 的 `records` 反推每个条目的状态
（pending → producing → done/failed），所以进程重启后批次进度能自愈，不需要单独的补偿任务。

## 4. 并发与额度：四个闸门

```
HTTP 请求
   │  ① max_active_jobs（大批量 4） / max_active_small_jobs（小批量 4）
   ▼
job（一个批次的一轮）
   │  ② max_active_per_user（1，仅大批量；小批量额外 +1）
   ▼
工作线程（每 job 最多 max_concurrency=100）
   │  ③ 在飞信号量：主池 100 / 快速池 12
   ▼
云端任务（每条视频）
   │  ④ 提交桶 250 RPM、轮询桶 15 RPS
```

- **① job 槽位**：`_dispatch()` 只统计 `self.active`；大小批量分开计数，小批量优先放行。
- **② 每用户**：防止一个人连提多个批次把机器占满。
- **③ 在飞**：`submit → wait` 全程占名额，任务终态才释放。快速通道用独立信号量，
  所以 3 条小任务不会被大批量的 100 条挡住。
- **④ 令牌桶**：提交与轮询是两个桶，`penalize()` 只推后命中的那个；
  单 job 的线程数再多，也只能按桶的速度发请求。

关键不等式（否则会自我拖累）：

```
轮询 RPS = 在飞数 ÷ poll_interval ≤ query_rps（15）
提交 RPM = 稳态吞吐              ≤ query_rpm（250）
稳态吞吐 ≈ 在飞数 ÷ 单条耗时
```

例：100 在飞、15 秒轮询 → 6.7 RPS；单条 8 分钟 → 12.5 条/分。两者都远低于上限，
所以瓶颈在“在飞 × 生成时长”，不在限流。

**调度伪代码**（`jobs.py::_dispatch`）：

```python
while not stop:
    wait_for(有可派发的 job 或 stop, timeout=1s)
    for job in queued 按 created_at 升序:
        if 槽位满: break
        lane = small / big
        if 该用户在本 lane 已满: continue
        start_thread(_run_job, job)
```

## 5. 数据模型

### 5.1 `web-data/jobs/<id>.json`

| 字段 | 说明 |
| --- | --- |
| `id` / `owner` | 12 位十六进制 / 账号名 |
| `status` | `queued → running → succeeded / partial / failed / canceled / interrupted` |
| `batch_id` | 输出目录名，形如 `WEB-<用户>-<时间>-<后缀>` |
| `lane` | `small`（≤3 条）或 `big`；老数据缺省按 `big` |
| `images[]` | 图片真源（1–10 张，顺序 = 槽位 = `imageUrls`）；`image_path`/`image_name` 是 `images[0]` 别名 |
| `templates` / `template_ids` | 本次要跑的场景（含 COS 地址） |
| `prompt` / `prompt_id` | 实际提交的提示词 |
| `resolution` / `duration` / `total` / `done` | 生成参数与进度 |
| `tags` | 6 栏位标签 `{top/lining/pants/shoes/scene/note}`（v0.10.0 起，top 必填进 COS 路径） |
| `style_no` | 旧款号字段（v0.9.0 老数据展示用，新任务不再写入） |
| `records[]` | 每条：`id / status / seconds / billable_seconds / estimated_cost / cost_source / error / cos_key` |
| `summary` | 引擎汇总（success / failed / 计费秒等） |
| `batch_ref` | 关联批次 id（试跑为空） |
| `log_tail` | 最近几十行日志，前端直接显示 |
| `out_dir` | 成片目录 |

### 5.2 `web-data/batches/<id>.json`

| 字段 | 说明 |
| --- | --- |
| `id` / `batch_no` / `title` | 内部 id / 批次号（唯一）/ 备注 |
| `state` | `draft → locked → closed`（closed 不可再生产） |
| `images[]` | **图片唯一真源**：`[{upload_id,name,path}]`，顺序 = 槽位 = `imageUrls`；[0]=定妆图 |
| `image_path` / `image_name` / `image_url` | `images[0]` 的**主图别名**（老代码/老前端/历史对比层用），写图时同步回写 |
| `prompt_id` / `prompt` | 提示词（不改 prompt；「图2…图10」由用户自己写）。绑定后仍可在步骤 4「分批生产」里改，只影响之后的生产 |
| `resolution` / `duration` | 本批次参数 |
| `tags` | 6 栏位标签，建批次时填，冻结后不改（旧批次只有 `style_no`） |
| `style_no` | 旧款号字段（v0.9.0），仅老批次存在 |
| `items[]` | 条目：`template_id / name / tags / state(pending/producing/done/failed) / job_id` |
| `runs[]` | 每轮生产记录：`job_id / count / at` |
| `active_job_id` | 当前跑的那一轮（同批次同时只允许一轮） |

## 6. 运行时目录

```
web-data/
├── jobs/<id>.json          任务状态（1 个文件 = 1 轮）
├── batches/<id>.json       批次
├── uploads/<xx>/<32hex>.png 定妆图与细节图（每图一个文件 + meta/<id>.json；目前无 GC）
├── output/<batch_id>/      成片 mp4 + 归档清单
├── logs/<job_id>.log       逐条提交日志
├── style_template_usage.json  款号-模板防重账本（v0.11.0，见 5.3）
└── 访问地址.txt            启动时写入的本机/局域网地址
```

`server/` 下：`web.json`（账号、会话密钥、并发上限）、`config.json`（**明文 Key**、模型、COS、单价、限流）。

### 5.3 `web-data/style_template_usage.json`（款号-模板防重账本）

需求：同一款号跨批次生成时不重复使用视频模板（**软拦截**——前端标记 + 提交确认，可强制）。

- 记账时机：**批次生产提交时**（`BatchManager._start_run`）；失败重试覆盖为最近一次；小批量试跑不计入。
- 记账键：上衣款号（`tags.top`，老批次回退 `style_no`）。
- 结构：`{ "<top>": { "<template_id>": { "batch_no", "batch_id", "at" } } }`。
- 查询：`GET /api/style-usage?top=<款号>`（登录即可），返回 `{ "used": {...} }`。
- 候选筛选：`POST /api/batches/<id>/candidates` 传 `"hide_used": true`（布尔，字面 `true` 才生效）
  即在标签命中里筛掉本款号用过的模板，响应加 `hide_used` / `used_skipped`（本次筛掉条数）；
  缺省 `false`，行为不变。筛选发生在评分过滤与排序**之前**，`filtered_total`/`ready_count`
  都按筛后口径，与徽标、锁定确认同源于同一账本键。
- 前端：批次候选行挂「已用·批次号」徽标，锁定清单时弹确认；第 2 步候选工具栏有
  「隐藏已用」开关（服务端筛，非本地过滤），候选被筛空时提示关掉开关可看全部；
  小批量下拉选项尾部加「【已用】」，提交时弹确认。

### 5.4 标签包含 / 排除条件

候选与锁定都接受两个独立参数，形状相同：`{ "<分类>": ["<标签>", ...] }`。

- `tag_filters`（包含，原有语义）：同一分类内命中任一标签即可（OR），不同分类必须同时满足（AND）。
- `tag_excludes`（排除，黑名单）：模板只要命中**任一**排除标签就剔除，**不再按分类分组**；
  与 `tag_filters` 相互独立，两个都给时先按包含筛、再按排除剔，
  同一标签同时出现在两边时**排除优先**（黑名单永远赢）。
- 校验：未知分类、分类下不存在的标签、值不是列表都返回 400（`TemplateError` 文案透传），
  不会静默失效——写错标签时宁可报错也不要给出一个看似正常的候选集。
- 响应 `exclude_skipped`：被排除条件额外剔掉的条数（仅提示用，`filtered_total` 已是筛后口径）；
  没有排除条件时为 0。
- 锁定（`POST /api/batches/<id>/lock`）会把当时的 `tag_filters` / `tag_excludes` 快照存进批次，
  详情接口原样返回，用于复盘「这批是怎么选出来的」。老批次没有 `tag_excludes` 字段，按空处理。
- 前端（`app/web/static/batch.js`）：标签 chip 三态循环 不选 → 包含（✓）→ 排除（✕ 划线）→ 不选；
  改动后自动重新拉候选，候选被排空时优先提示是排除条件筛掉的。

## 7. HTTP 接口

| 方法 | 路径 | 权限 | 作用 |
| --- | --- | --- | --- |
| GET | `/` `/static/*` | 公开 | 页面与静态资源（每次读盘，改完刷新即可） |
| GET | `/api/me` `/api/bootstrap` | 登录 | 当前用户、服务上限、提示词、模板、标签 |
| GET | `/api/templates` `/api/templates/<id>/preview` | 登录 | 模板列表 / 预览视频 |
| GET | `/api/uploads/<id>` | 登录（限本人） | 回显图片（定妆图 / 细节图） |
| POST | `/api/uploads` | 登录 | 单张图片上传（每图一次，≤20MB；多图由前端并发 3 张提交） |
| GET/POST | `/api/jobs` | 登录 | 列表（`?scope=all` 仅 admin；支持 `lane=small\|big`、`offset`、`limit`，返回 `total`）/ 建任务 |
| GET | `/api/jobs/<id>` `/api/jobs/<id>/files/...` | 登录（限本人） | 详情 / 成片、报告 CSV、ZIP |
| POST | `/api/jobs/<id>/cancel` `/retry` | 登录（限本人） | 取消 / 只重试失败项 |
| GET/POST | `/api/batches` | 登录 | 批次列表 / 新建 |
| GET/POST | `/api/batches/<id>[/...]` | 登录（限本人） | 详情、image、images（增删细节图）、candidates、lock、produce、retry-failed、close |
| GET | `/api/batches/<id>/files.zip[?probe=1]` | 登录（限本人） | 整批打包下载，**仅大批量且非试跑的批次**；按 runs 收集每一轮成功视频、按场景去重保留最新。`?probe=1` 只回 `{count,batch_no,download_url}` 不落盘，供前端先确认再触发原生下载 |
| GET | `/api/history/groups[/<key>]` | 登录 | 历史分组 / 组内条目；`group_by=batch\|owner\|day`，`lane=small\|big` 按通道过滤；明细带 `scope` 保证与列表同口径 |
| GET | `/api/style-usage?top=` | 登录 | 款号已用模板账本（防重标记） |
| GET | `/api/admin/status` | admin | 服务状态、队列、额度、账号 |
| GET | `/api/admin/usage` | admin | 按账户汇总 |
| GET | `/api/admin/accounts/<name>/work[.csv]` | admin | 账户逐条明细 / 导出 |
| POST | `/api/admin/users` `/api/admin/users/<name>[/enable]` | admin | 增删启停账号 |
| POST | `/api/admin/reload-templates` | admin | 重载标签清单 |

鉴权失败返回 401，权限不足 403；所有错误体统一 `{ "error": "中文提示" }`。

## 8. 前端结构

无框架、无构建步骤，四个文件：

| 文件 | 职责 |
| --- | --- |
| `index.html` | 六个 view：`create-view`（试跑）、`batch-view`（大批量）、`history-view`、`admin-view`、`tag-view`（视频库）、`material-view`（素材检索，iframe 内嵌 `http://192.168.10.179:8700`） |
| `app.js` | 全局 `state`、`api()`、`toast()`，试跑/任务/历史/管理/素材检索五个模块；末尾 `window.OutfitApp` 暴露 `{ api, toast, state, switchView, openJob, refreshFlatJobs, statusText }` |
| `batch.js` | IIFE，从 `window.OutfitApp` 取依赖；批次列表、新建、候选、生产、条目、历史分组 |
| `tag.js` | IIFE，视频库：打标、批量改标签、弃用/恢复、标签库管理 |

- 任务列表每 **3.5 秒** 轮询一次（页面隐藏时跳过）；批次页单独 3 秒左右轮询详情；
- 管理页只在 admin 登录时显示，`账户用量` 与逐条明细的金额渲染都在 `app.js` 的 `renderUsage` / `renderAccount`；
- 生产页**不显示任何金额**（v0.6.1 起），按钮只标条数。
- `material-view` 是站外工具（另一台局域网服务，账号与数据独立）：iframe 的 `src` 由
  `app.js` 在首次进入该 tab 时从 `data-src` 挂上（登录首屏不拉外部页面）；
  CSP 只放行这一台（`server.py` 的 `_headers` 里 `frame-src http://192.168.10.179:8700`），
  **换地址要同时改 `index.html` 的 `data-src` 和这条 CSP**，别把 `frame-src` 放宽成 `*`。

## 9. 配置映射

| `server/config.json` | 内部键 | 用途 |
| --- | --- | --- |
| `wan.api_key` | `WAN_API_KEY` | 全站唯一 Key |
| `wan.model` / `base_url` / `region` | `WAN_MODEL` / `WAN_BASE_URL` / `WAN_REGION` | 官方接口 |
| `wan.poll_interval` / `poll_timeout` | `WAN_POLL_INTERVAL` / `WAN_POLL_TIMEOUT` | 轮询节奏与超时 |
| `wan.max_concurrency` | `WAN_MAX_CONCURRENCY` | 单 job 线程数（上限 100） |
| `wan.query_rpm` / `query_rps` | `WAN_QUERY_RPM` / `WAN_QUERY_RPS` | 提交桶 / 轮询桶 |
| `wan.max_inflight` | `WAN_MAX_INFLIGHT` | 在飞主池 |
| `wan.price_per_second` | `WAN_PRICE_PER_SECOND` | 单价，null 时按公开价兜底 |
| `wan.query_qps` | `WAN_QUERY_QPS` | **历史字段，不再驱动限流** |

`server/web.json` 里的并发项（`max_active_jobs`、`max_active_per_user`、`small_lane_*`、
`max_job_items`、`max_batch_items`、`max_batch_run_items`）由 `WebSettings` 读取并裁剪上限，
改完**必须重启**。

## 10. 扩展点与已知债

**加功能通常从这三个位置之一入手**：

1. 新接口：`server.py` 的 `do_GET`/`do_POST` 路由 + `WebApplication` 方法；
2. 新调度策略：`jobs.py::_dispatch` 与 `_lane_allowed`；
3. 新批次动作：`batches.py` 加方法 + `server.py` 的 `/api/batches/<id>/...` 分支。

**已知债**（评估过，暂不处理）：

- `server.py` 1161 行、`wan_api.py` 1060 行、`batches.py` 822 行，单文件偏大；
- 无 pytest 夹具，测试是独立脚本，靠 `tests/run_all.py` 顺序跑；
- 前端 3.5 秒轮询而非 SSE/WebSocket，任务多时会有无谓请求；
- 在飞名额全局共享，不按账户平分——两人同时跑 50 条时，先启动的一方会先占满；
- 金额单价三档为 null，只能用公开价，和真实账单有偏差。

## 11. 排障速查

| 现象 | 先看 |
| --- | --- |
| 启动后连不上 | 是否刚起 7 秒内；`netstat -ano \| findstr 8765` 是否只有一个 LISTENING |
| 所有任务卡在排队 | `max_active_jobs` 是否被别的 job 占满；`/api/admin/status` 的 `queued/active` |
| 进度长时间不动 | 名额是否被别人占满；日志里是否出现 `[限流]`；云端是否 `PENDING` |
| 重启后任务变 `interrupted` | 正常：进程被杀时 running 会被改写，需核对云端再重试 |
| 页面打不开某个接口 403 | 普通账号访问了 admin 接口，或 `?scope=all` |
| 改了配置没生效 | `config.json` / `web.json` 需重启；`static/*` 只需刷新 |
