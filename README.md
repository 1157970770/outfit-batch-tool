# 换装批量生产器（局域网网页版）

1 张定妆图 + 最多 9 张细节图 + N 个视频模板 → N 条 7 秒换装视频，浏览器操作，主机跑任务。
生成走**阿里云百炼官方** `wan3.0-video`，成品落本地，可选归档腾讯云 COS。

> 接手开发先看 [AGENTS.md](AGENTS.md)（入口地图），再看 [docs/architecture.md](docs/architecture.md)（代码架构）。

| 组成 | 入口 | 说明 |
| --- | --- | --- |
| 网页端 | `run_web.py` → `app/web/` | 局域网多账号，管理员按账户看用量 |
| 引擎 / CLI | `app/core.py`、`app/wan_api.py`、`wan3_r2v.py` | 离线演练、批量脚本 |

---

## 一、快速开始（网页版）

> **本机两区布局（2026-11-06 起）**：生产跑在 `D:\批量视频任务运行区`（8765，同事访问），
> 本仓库目录是开发副本（dev 实例 8766 + `web-data-dev`）。端口与数据目录都由
> `server/web.json` 决定，启动器两区通用。详见 [AGENTS.md §0](AGENTS.md) 与
> [docs/dev-runtime-split.md](docs/dev-runtime-split.md)。下面是全新机器首次部署的通用步骤。

```bash
# 1) 首次：创建管理员，写入 server/web.json（不会覆盖已有配置）
python run_web.py --init

# 2) 准备 Key：server/config.json → wan.api_key（全站共用这一把）

# 3) 启动（端口取自 server/web.json，全新机器是 0.0.0.0:8765）
#    日常：双击「启动网页服务.vbs」——不弹任何命令行窗口，
#          服务已在跑就直接打开浏览器；启动日志写 web-data/logs/launcher.log
#    排错：双击「启动器\启动网页服务.bat」，窗口里能看到实时输出
python run_web.py
```

启动后控制台会打印本机与局域网地址（同时写入 `web-data/访问地址.txt`），同网段设备用浏览器打开即可。

根目录只放三个双击入口：`启动网页服务.vbs`、`启动打标器.vbs`、`重启网页服务.vbs`
（重启只弹一次 UAC）。要看实时日志时，进 `启动器\` 目录双击对应的 `.bat`。

**添加账号**：管理员登录 →「管理 → 添加使用者」；或在主机执行
`python run_web.py --add-user 账号 --role user`（交互输入密码，不在命令行暴露）。

| 命令 | 作用 |
| --- | --- |
| `python run_web.py --init` | 初始化网页服务并建管理员 |
| `python run_web.py --add-user 名字 [--role user\|admin]` | 加账号 |
| `python run_web.py --reset-password 名字` | 重置密码（旧会话失效） |
| `python run_web.py --list-users` | 列账号与角色 |
| `python run_web.py --remove-user 名字` | 停用账号 |
| `python run_web.py --selftest` | 自检：密码、图片依赖、静态文件 |

## 二、网页版能做什么

| 视图 | 能力 |
| --- | --- |
| 小批量试跑 | 三列控制台一屏完成：定妆图+细节图 → 场景与参数 → 提示词，单次上限 3 条 |
| 大批量生产 | 新建批次（批次号/备注/定妆图+细节图）→ 筛选候选 → 锁定清单 → 分多轮「生产 N 条」→ 导出成片 ZIP；锁定后仍可追加/删除细节图 |
| 历史任务 | 按批次号/账户分组查看，重试失败项、下载成片与 CSV 报告 |
| 标签（所有人） | 给视频打标签 / 批量改标签、**上传新视频**（≤50MB，直传 COS 并立即可用）、**弃用与恢复**（软删除）、标签库与**自定义大类**的新建/改名/合并/删除、整库版本历史与回滚；数据落 `web-data/template_tags.json`，网页与本地打标工具共用 |
| 管理（仅 admin） | 服务状态、账号列表、重新加载场景、**账户用量**（条数/成功失败/计费秒/预估金额，点账户看逐条明细并导出 CSV） |

权限按 `owner` 隔离：普通账号只看自己的任务与批次，管理员可按账户筛选。
**快速通道**：≤3 条的批次走独立队列与独立在飞池，不与本人的大批量排队。
**金额只在管理端出现**，生产页不显示任何费用。

## 三、代码架构速览

```
浏览器 app/web/static/*  ──HTTP+Cookie──▶  app/web/server.py（唯一入口）
                                             ├─ auth.py      密码/会话/登录限流
                                             ├─ settings.py  server/*.json → 配额与上限
                                             ├─ templates.py 标签清单 × 云端 manifest
                                             ├─ jobs.py      队列调度 + 事件 + 用量
                                             └─ batches.py   批次生命周期
                                                    │
                                                    ▼
                                          app/core.py  BatchRunner（线程池）
                                                    │
                                                    ▼
                                          app/wan_api.py  双令牌桶 + 在飞信号量
```

完整分层、线程模型、请求生命周期、数据字段与接口表见 **[docs/architecture.md](docs/architecture.md)**。

### 当前关键参数（`server/config.json` / `server/web.json`）

| 参数 | 当前值 | 含义 |
| --- | --- | --- |
| `wan.query_rpm` | 250 | 创建任务上限（官方 300，留余量） |
| `wan.query_rps` | 15 | 轮询上限（官方 20） |
| `wan.poll_interval` | 15 秒 | 轮询间隔 |
| `wan.max_inflight` | 100 | 全站同时在飞任务数 |
| `wan.max_concurrency` | 100 | 单个 job 的工作线程数 |
| `max_active_jobs` | 4 | 同时跑几个大批量 job |
| `small_lane_items` / `small_lane_inflight` | 3 / 12 | 快速通道判定与专用在飞池 |

改 `config.json` / `web.json` **需要重启**；改 `app/web/static/*` 不用（每次请求读盘）。

## 四、目录地图

| 路径 | 说明 |
| --- | --- |
| `run_web.py` | 网页版入口 |
| `app/web/` | 网页服务：`server.py` 路由、`jobs.py`、`batches.py`、`settings.py`、`templates.py`、`auth.py`、`static/` 前端 |
| `app/core.py` | 引擎：线程池、提交/轮询/下载、事件回调 |
| `app/wan_api.py` | 协议适配（DashScope 默认 / RunningHub），限流与在飞 |
| `app/config.py` | 配置映射：`server/config.json` → 内部 `WAN_*` |
| `app/storage.py` / `app/media.py` | COS/本地存储、图片与视频预检 |
| `server/` | 部署配置（`web.json` 账号、`config.json` Key/模型/COS） |
| `web-data/` | 运行数据：任务、批次、上传、成片、日志（不入库） |
| `tests/` | 离线测试；`run_all.py` 一键跑全部 |
| `docs/` | 文档，索引见 [docs/README.md](docs/README.md) |
| `scripts/`、`tools/` | 构建、COS 归档、清单生成等辅助脚本 |

## 五、测试（不花钱、不联网）

```bash
python tests/run_all.py            # 全部离线套件
python tests/test_web.py           # 网页端端到端
python tests/test_batches.py       # 批次全流程 + 额度断言
python demo.py --count 5           # 一键演练整条链路（假百炼）
```

`tests/run_all.py` 覆盖引擎、存储、网页接口与批次端到端，全部离线、不花钱。

## 六、引擎与命令行

```bash
# 离线演练：造占位图 → 本地假百炼 → 提交/轮询/下载/记账
python demo.py --count 5

# 真任务（命令行直接跑，Key 走环境变量，独立于网页版配置）
set DASHSCOPE_API_KEY=sk-你的Key
python wan3_r2v.py run --tasks batch/tasks.batch.json --limit 1 --mode reference
python wan3_r2v.py run --tasks batch/tasks.batch.json --concurrency 3
```

| 命令 | 作用 |
| --- | --- |
| `python wan3_r2v.py validate --tasks ...` | 只校验，不发请求 |
| `python wan3_r2v.py build --tasks ... --index 0` | 打印第 1 条官方请求体 |
| `python wan3_r2v.py emit --tasks ... --out batch/requests.jsonl` | 导出请求原文 |
| `python wan3_r2v.py upload 文件` | 上传素材到 COS，返回 URL |
| `python wan3_r2v.py query <task_id>` | 复查任务 |
| `python make_batch.py` | 服装清单 → 任务文件 |
| `python scripts/cos_check.py` | COS 连通性自检（写/签/读/列/删） |
| `python scripts/archive_to_cos.py --root out` | 历史成片补传 COS，回写 `cos_key` |

## 七、成片归档到 COS

结果链接 **24 小时失效**，本地磁盘迟早会满；接一层 COS 才有长期地址。

- **网页版**：`server/config.json → cos` 配好桶与前缀，任务跑完自动归档；
  归档失败只写 `archive_error`，不影响出片。
- **命令行**：

```bash
set COS_ENABLED=true
set COS_BUCKET=video-output-1463907213
set COS_REGION=ap-guangzhou
set COS_PREFIX=prod
set COS_SECRET_ID=AKIDxxxxxxxx
set COS_SECRET_KEY=xxxxxxxx
python scripts/cos_check.py
```

建桶、CAM 最小权限、报错对照、费用见 [docs/cos-setup.md](docs/cos-setup.md)；
客户端持密钥的风险评估见 [docs/adr-001-cos-client-credential.md](docs/adr-001-cos-client-credential.md)。

## 八、生产链路

```
定妆图 + 细节图（每张各上传 1 次，最多 10 张）
   └─ 逐张签 7 天 URL（或上传 COS），数组顺序 = 「图1…图10」
        └─ N 条请求共用 imageUrls（未启用 COS 时走 Base64，总量上限 40MB）
             ├─ POST /services/aigc/video-generation/video-synthesis ×N → task_id
             ├─ GET  /tasks/{task_id} 轮询 → SUCCEEDED
             ├─ 立即下载 → web-data/output/<批次>/<编号>-<指纹>.mp4
             └─（可选）归档 COS → prod/output/<日期>/<批次>/
```

## 九、核心约定（别改错）

- 官方 `duration` 是**整数**：`7` 或 `-1`；旧 RunningHub 才是字符串 `"7"`；
- 素材统一进 `input.media[]`：`reference_image` / `reference_video` / `reference_audio`；
- 换装不换人靠 prompt：`保持脸型、五官、发型与妆造完全不变，只更换服装`，且要提到「视频1」「图1」；
- 批量务必 `prompt_extend: false`，否则多条风格会漂；
- 结果链接与 `task_id` 都只有 **24 小时**，成功即下载；
- 官方 `usage` 不含金额，按 `duration` 记账，真实账单以阿里云为准。

## 十、文档索引

| 文档 | 用途 |
| --- | --- |
| [AGENTS.md](AGENTS.md) | **给接手模型/人的入口地图**（先读这个） |
| [docs/architecture.md](docs/architecture.md) | **网页版架构**：分层、线程、额度、数据、接口 |
| [docs/project-summary-20261102.md](docs/project-summary-20261102.md) | v0.6.0 现状、参数、运维、风险 |
| [docs/account-usage-and-quota.md](docs/account-usage-and-quota.md) | 单 Key 共用 + 账户用量口径 |
| [docs/web-lan.md](docs/web-lan.md) | 局域网部署与排障 |
| [docs/wan-3.0-reference-to-video.md](docs/wan-3.0-reference-to-video.md) | 官方接口规格 |
| [docs/cos-setup.md](docs/cos-setup.md) | COS 接入手册 |
| [docs/backup-tag-store.md](docs/backup-tag-store.md) | 标签库真源每日备份到 COS、恢复步骤 |
| [docs/README.md](docs/README.md) | 全部文档索引（现行 / 历史） |
