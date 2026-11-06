# AGENTS.md · 给下一个接手者（人或模型）的地图

> 先读这一页，再读代码。这里只写“事实与边界”，不重复业务细节；
> 业务与参数口径见 [docs/architecture.md](docs/architecture.md) 与 [docs/project-summary-20261102.md](docs/project-summary-20261102.md)。

## 1. 这是什么

局域网网页版批量视频生产器：1 张定妆图 + 最多 9 张细节图 + N 个视频模板 → N 条 7 秒换装视频，
调用阿里云百炼 `wan3.0-video`，成品落本地并可选归档腾讯云 COS。

| 组成 | 入口 | 说明 |
| --- | --- | --- |
| 网页端 | `run_web.py` → `app/web/` | 标准库 `ThreadingHTTPServer` + 原生 JS |
| 引擎 | `app/core.py` / `app/wan_api.py` | 纯 Python，网页与 CLI 共用 |

## 2. 三分钟跑起来

> **分区提示（2026-11-06 起）**：生产服务固定跑在 `D:\批量视频任务运行区`
> （计划任务 `OutfitWebServer`，端口 8765；重启用运行区 `启动器\重启网页服务-管理员.bat`）。
> 本设计区只做开发：`python run_web.py` 起的是 dev 实例（8766 + `web-data-dev`）。
> 布局、发布与回滚见 [docs/dev-runtime-split.md](docs/dev-runtime-split.md)。

```bash
# 首次：创建管理员（写入 server/web.json）
python run_web.py --init

# 启动（默认 0.0.0.0:8765，启动后约 7 秒才 bind 端口，别急着判失败）
python run_web.py
# 或双击「启动网页服务.vbs」
```

Windows 上解释器通常是 `%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe`；
仓库脚本一律写成 `python3`，本机等价即可。依赖见 `requirements.txt`（Pillow、openpyxl 必需）。

## 3. 代码分层与数据流

```
浏览器（app/web/static/*）
   │  HTTP + Cookie 会话
   ▼
app/web/server.py      路由 / 鉴权 / 静态文件 / CSV·ZIP 下载   ← 唯一 HTTP 入口
   ├─ auth.py          密码哈希、HMAC 会话、登录限流
   ├─ settings.py      读 server/*.json → WebSettings（配额、单价、上限）
   ├─ templates.py     TemplateCatalog（标签真源 / xlsx + manifest 索引）
   ├─ tag_api.py       标签页接口：打标 / 弃用 / 标签库维护（普通用户可写）
   ├─ jobs.py          JobManager（队列、调度、事件、用量统计）
   └─ batches.py       BatchManager（草稿 → 筛选 → 锁定 → 多轮生产 → 关闭）
          │
          ▼
tools/template_store.py  标签唯一真源（JSON）：原子写 + 跨进程锁 + revision 乐观锁
tools/template_catalog.py xlsx 导入导出、扫描、指纹、标签校验（网页与本地打标工具共用）
          │
          ▼
app/core.py            BatchRunner：线程池，逐条 提交/轮询/下载，回调事件
          │
          ▼
app/wan_api.py         WanClient：DashScope(默认)/RunningHub 适配，双令牌桶 + 在飞信号量
app/storage.py         COS / 本地存储适配
```

> 配置一律来自本地 `server/config.json` + `server/web.json`，改完重启生效；
> 没有云端配置下发，也没有授权校验（仓库里不再包含这些模块）。

一次小批量请求经过：`POST /api/uploads` → `POST /api/jobs`（落 `web-data/jobs/<id>.json`）
→ 调度线程 `_dispatch` → `_run_job` → `BatchRunner.run()` → 事件回写 `records/done`
→ 前端每 3.5 秒轮询 `/api/jobs` → 完成后 `GET /api/jobs/<id>/files/...` 下载。

## 4. 七条硬约束（改代码前必须知道）

1. **全站只有一把 API Key**：`server/config.json → wan.api_key`，所有账户共用。
   历史上做过“每账户一把 Key”，已在 v0.4.0 全部移除，别再引入。
2. **提交与轮询是两个独立额度**：提交 250 RPM（官方 300），轮询 15 RPS（官方 20）。
   429 惩罚只落在对应桶上。轮询间隔 15 秒，与在飞数配对（100 在飞 ≈ 6.7 RPS）。
3. **在飞名额分两个池**：大批量主池 100（跨账户共享）、快速通道池 12（≤3 条的批次专用）。
   线程数（`max_concurrency`）≠ 在飞数，前者只是工作线程。
4. **权限按 owner 隔离**：普通账号只见自己的任务/批次/历史；管理员可 `?scope=all` 或按 `owner` 筛选。
5. **金额只在管理端出现**：试跑页、大批量页、任务操作一律不显示费用；
   计费秒 / 预估金额 / CSV 只在「管理 → 账户用量」。
6. **标签只有一个真源**：`web-data/template_tags.json`。xlsx 只用于导入/导出
   （`run_web.py --import-tags / --export-tags`），不要再把它当真源写。
   「弃用」是软删除：不删本机视频、不删 COS 对象、不重建 manifest，随时可恢复；
   「标签为空」是未打标，两者都不进候选集但语义不同。
7. **运行区只 pull 不改码**：`D:\批量视频任务运行区` 是生产，只通过
   `git pull --ff-only`（或 `python scripts/deploy_runtime.py`）接收已提交的代码；
   一切修改都发生在设计区，见 [docs/dev-runtime-split.md](docs/dev-runtime-split.md)。

## 5. 配置与数据放在哪

> 下表路径按「当前文件夹」解析：运行区与设计区各自持有一套 `server/*.json` 与
> `web-data/`（设计区的生产数据已归档为 `web-data-生产快照-20261106/`，dev 用 `web-data-dev/`）。

| 路径 | 内容 | 是否入库 |
| --- | --- | --- |
| `server/web.json` | 账号、会话密钥、并发上限、快速通道参数 | ❌ 含密码哈希 |
| `server/config.json` | **明文 API Key**、模型、COS、单价、限流参数 | ❌ 绝不提交 |
| `server/*.example.json` | 上述两者的模板 | ✅ |
| `web-data/jobs/<id>.json` | 任务状态与逐条记录 | ❌ |
| `web-data/batches/<id>.json` | 批次与条目 | ❌ |
| `web-data/template_tags.json` | **标签唯一真源**（视频清单 + 标签库 + 弃用状态 + 审计），网页与本地打标工具共用；`.bak` 是上一版 | ❌ |
| `web-data/manifest_overlay.json` | 网页上传的视频索引（云端 manifest 里没有它们）；启动时与 manifest 合并 | ❌ |
| `web-data/tag_versions/` | 真源版本快照（gzip，整库含每条标签与弃用态）；自动留 50 版 + 手动 50 版，回滚前另存备份 | ❌ |
| `web-data/{uploads,output,logs}/` | 定妆图与细节图、成片、任务日志 | ❌ |
| `app/web/static/` | 前端源码（每次请求读盘，改完刷新即可） | ✅ |

**多图（v0.17.0）**：两页都支持 10 个槽位（槽位 1 定妆图必填，2–10 细节图可选），数组顺序即模型 `imageUrls` 顺序与 prompt 的「图N」。job / batch 以 `images[]` 为唯一真源，`image_path/image_name/image_url` 保留为**主图别名**（老数据零迁移）。`/api/jobs`、`/api/batches` 收 `upload_ids[]`（兼容旧 `upload_id`）；锁定后细节图走 `POST /api/batches/<id>/images` 的 `op=add/remove`，主图不可换。删除细节图后序号自动左移；**不改 prompt**，指代由用户自己写「图2…图10」。COS 关闭时 `app/media.py:base64_budget_error` 会按总量 40MB 兜底拒绝。uploads 目录目前**没有 GC**，10 倍图量会持续堆积，后续可加「按 job/batch 引用计数 + 7 天」的清理。

### 换 Key / 换模型 / 换地域，只改一处

全部在 `server/config.json → wan`，网页端**不读环境变量**，改完必须重启服务：

| 要改什么 | 字段 | 备注 |
| --- | --- | --- |
| 换 API Key | `wan.api_key` | 全站共用这一把；改完重启，网页端会话不受影响 |
| 换模型 | `wan.model` | 例如换版本号；`duration`/`ratio` 约束要跟着核对 |
| 换地域/业务空间 | `wan.base_url`、`wan.region` | 三者必须与 Key 同地域，否则鉴权失败 |
| 换单价 | `wan.price_per_second` | 只影响管理端金额估算 |

改的是**运行区**的 `server/config.json`（生产配置在设计区只是副本）。改完重启生产：

```bash
# 右键 D:\批量视频任务运行区\启动器\重启网页服务-管理员.bat → 以管理员身份运行
# （重启前先确认运行区 web-data/jobs 里没有 running/queued）
```

注意：`wan_configured()` 只校验 Key 与 model **非空**，Key 填错也能启动，
要到第一条任务提交时才报鉴权错误——所以换完 Key 先跑 1 条小批量验证。


## 6. 测试怎么跑

```bash
python tests/run_all.py          # 全部离线套件（不花钱、不联网）
python tests/test_web.py         # 网页端端到端：登录/上传/任务/用量/权限
python tests/test_batches.py     # 批次：草稿→锁定→生产→历史分组 + 额度断言
python tests/test_batch_zip.py   # 整批下载：多轮收集 / 非试跑资格 / probe / HTTP zip
```

`tests/run_all.py` 覆盖引擎、存储、网页接口与批次端到端，全部离线运行。

## 7. 别动 / 小心

- `app/web/__init__.py`：`.gitignore` 里的 `_*.py` 曾误伤所有 `__init__.py`，现已加例外。
  新建包时若发现 `__init__.py` 没进版本库，先查 `.gitignore`。
- `web-data/`、`server/web.json`、`server/config.json`：本机运行数据，不要为了“干净”删掉。
- 服务启动**约 7 秒后**才监听端口；用 `netstat -ano | findstr 8765` 确认只有一个 LISTENING，
  别重复启动（Windows 的 `SO_REUSEADDR` 会让两个进程同时绑上同一端口）。
- 重启会打断 running 任务；重启前先看 `web-data/jobs/*.json` 里有没有 `running/queued`。
- 旧的 `cordis.patch.yml.bak-*`、`probe-registry.mjs` 是历史残留，不要当入口。
- 打标器只能从**运行区**启动（`启动器\启动视频标签整理.bat`）：它按所在文件夹定位
  标签真源（`启动打标器.py` 设 `OUTFIT_TAG_STORE=BASE/web-data/template_tags.json`），
  从设计区启动会写进 dev 副本。

## 8. 已知债（别当成 bug 修）

- `server.py` 1696 行、`wan_api.py` 1120 行：路由与协议都写在大类里，按需再拆，不要顺手重构。
- 测试是独立脚本，没有 pytest 夹具；`tests/run_all.py` 只是顺序执行器。
- `wan.price_per_second` 三档均为 `null`，金额用公开价目表兜底，与真实账单有偏差。
- 本仓库只维护网页端与引擎；不要再引入 GUI 界面或桌面打包相关代码。

## 9. 文档索引

| 文档 | 用途 |
| --- | --- |
| [docs/architecture.md](docs/architecture.md) | **网页版代码架构**：分层、线程、额度、数据模型、接口 |
| [docs/project-summary-20261102.md](docs/project-summary-20261102.md) | v0.6.0 现状、参数表、运维与风险 |
| [docs/account-usage-and-quota.md](docs/account-usage-and-quota.md) | 单 Key 共用与账户用量监控口径 |
| [docs/web-lan.md](docs/web-lan.md) | 局域网部署与访问 |
| [docs/dev-runtime-split.md](docs/dev-runtime-split.md) | 运行区/设计区分区：布局、发布与回滚 |
| [docs/ui-rollback.md](docs/ui-rollback.md) | 界面改版的基线与回滚步骤 |
| [docs/README.md](docs/README.md) | 全部文档索引（现行 / 历史） |
| [README.md](README.md) | 对外说明与快速开始 |
