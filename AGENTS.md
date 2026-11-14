# AGENTS.md · 给下一个接手者（人或模型）的地图

> 先读这一页，再读代码。这里只写“事实与边界”，不重复业务细节；
> 业务与参数口径见 [docs/architecture.md](docs/architecture.md) 与 [docs/project-summary-20261102.md](docs/project-summary-20261102.md)。

## 0. 两区布局（2026-11-06 起，先读这一节）

一个仓库、两个文件夹，代码完全同源；代码里的相对路径按「代码所在文件夹」解析
（`app/config.py` 的 `app_dir()`），所以整目录克隆即自包含、不依赖工作目录。
但**任务 / 批次 / 上传记录里存的是生成那一刻的绝对路径**，换文件夹后会悬空；读数据时
由 `app/web/paths.py::relocate()` 按当前 `data_dir` 重锚（见 [docs/dev-runtime-split.md](docs/dev-runtime-split.md)）。

| | `D:\批量视频任务运行区`（**生产**） | `D:\批量视频任务设计区`（**开发**） |
| --- | --- | --- |
| 谁在用 | 局域网同事，`http://192.168.10.179:8765` | 只有开发者，`http://127.0.0.1:8766` |
| 端口 / 数据 | `server/web.json` → 8765 / `web-data` | 8766 / `web-data-dev` |
| 进程 | 计划任务 `OutfitWebServer`（session 0，开机自启） | 手动：双击 `启动器\启动网页服务.bat` |
| git | **只 `pull --ff-only`，不改码、不提交** | 开发 + commit + push GitHub |
| 打标器 | **只能从这里启动**（真源=运行区 `web-data`） | 别从这里启动（会写进 dev 副本） |

四条铁律：

1. **生产代码只经 git 进入运行区**：`git pull --ff-only` 或 `python scripts/deploy_runtime.py`；
   所有修改都发生在设计区。发布助手只发布**已提交**状态、只拦已跟踪文件的改动
   （运行区的 `运维/`、本机日志是未跟踪文件，不阻塞发布）。
2. **发布流程**：设计区改码 → `python tests/run_all.py` → commit →
   `python scripts/deploy_runtime.py`（查 running/queued → pull → 自检）→
   需要时右键运行区 `启动器\重启网页服务-管理员.bat`（管理员）。只有启动器/文档/脚本类改动可免重启。
3. **回滚**：撤掉上一次发布用 `git -C "D:\批量视频任务运行区" reset --hard ORIG_HEAD`；
   整体退回设计区用 `运行区\运维\回滚到设计区-需管理员.bat`（`运维/` 已被 `.gitignore` 忽略，重新克隆运行区需重建）。
4. **启动器两区通用**：`启动网页服务.vbs` 从本文件夹 `server/web.json` 读 `port` / `data_root`，
   换端口/数据目录只改配置，别改脚本；两个实例同时存在是正常的（8765 生产 + 8766 dev）。
   启动器同时按 `dev_mode` 自动标注【开发版】/【生产版】：控制台标题、横幅、启动失败弹窗都带，
   所以**别把区名写死进这两个脚本**（同一份文件两区共用、会随 git 发布到生产）。
   两份脚本都是 **ANSI(GBK)** 编码且开头 `chcp 936 >nul`，改中文内容必须仍用 GBK 保存。
   设计区另有本机专用的 `启动器\启动网页服务-开发版.bat`（登记在 `.git/info/exclude`，不入库、不发布），
   名字里就带「开发版」，用来区分运行区那份同名启动器。

**dev 实例边界**：`web-data-dev` 是轻量副本（jobs / batches / template_tags / tag_versions）；
`uploads` 是指向运行区 uploads 的目录联接（见下节），`output` 仍为空，历史视频靠
`设计区\web-data\output` 的临时桥接回运行区。账号与生产相同；
`server/config.json` 是**真实 Key**——dev 提交 = 真实计费，且与生产共用 250 RPM / 100 在飞，别在生产高峰压测。
切换前的完整生产数据归档在 `设计区\web-data-生产快照-20261106\`（只读兜底，运行区稳定一周后可删）。

**批次永久档案（2026-11-14 起）**：每个区的 `web-data/archive/` 各自一份，**不跟 `uploads` 联接走**。
生产档案在 `运行区\web-data\archive\`，dev 档案在 `设计区\web-data-dev\archive\`；档案按
`<款号>/<批次号>__<batch_id>/` 存款号、提示词历史、参考图副本与每轮快照。**删批次不会删档案**，
`uploads` 以后做 GC 也不影响档案；要备份就整目录备 `archive/`。详见 [docs/batch-archive.md](docs/batch-archive.md)。

细节（布局理由、一次性切换/回滚脚本、数据归档）见 [docs/dev-runtime-split.md](docs/dev-runtime-split.md)。

### dev 的 uploads 目录联接（2026-11-06）

`设计区\web-data-dev\uploads` 是指向 `D:\批量视频任务运行区\web-data\uploads` 的 **junction**。
起因：dev 历史记录 / 对比页的定妆图丢了——历史 job 只存 `upload_id`，图片要经
`/api/uploads/<id>` 读 `uploads\meta\*.json` 与图片文件，而 dev 副本里这些是空的；
`app/web/static/batch.js:2700-2702` 在图片 404 时会把「定妆图」面板整块收起，
于是三栏「定妆图 + 生成的视频 + 模板视频」只剩两栏视频。两个视频不依赖本地 uploads：
生成的视频走 `设计区\web-data\output` 的桥，模板视频是 COS 云端预览。

**提示（写入不隔离）**：联接是双向的，dev 之后新上传的图片和 meta 会**写进生产运行区的
uploads**；真源始终在 `运行区\web-data\uploads`。别在 dev 上传敏感 / 大量素材；要彻底隔离
就删掉联接、改成一次性复制快照（约 235 MB）。联接不入库、不随 git 发布，重新克隆或换机器
后需重建：

```powershell
# 先确认 web-data-dev\uploads 是空目录或联接，再执行
Remove-Item 'D:\批量视频任务设计区\web-data-dev\uploads' -Recurse -Force
New-Item -ItemType Junction -Path 'D:\批量视频任务设计区\web-data-dev\uploads' `
  -Target 'D:\批量视频任务运行区\web-data\uploads'
```

**别删 `设计区\web-data\output` 的桥**：dev 旧任务的 `out_dir` 仍指向设计区旧路径，
历史视频靠它接回运行区；生产代码发布重启后生产不再需要，但 dev 仍依赖。
两条桥的本机说明在 `设计区\web-data\README.md`（该目录被 `.gitignore` 忽略，不入库）。

## 1. 这是什么

局域网网页版批量视频生产器：1 张定妆图 + 最多 9 张细节图 + N 个视频模板 → N 条 7 秒换装视频，
调用阿里云百炼 `wan3.0-video`，成品落本地并可选归档腾讯云 COS。
另有一个「生图」tab：提示词 + 参考图直接出图（老张 API 的 GPT Image VIP 与 Nano Banana 2.1，与视频链路完全独立）。
还有一个「自由视频」tab：提示词 + 任意参考图/视频/音频直接生产（文生视频、参考图生视频、视频编辑、配音），
不选模板、不填款号，任务不落 `web-data/jobs`、不进 `BatchRunner`，见 [docs/free-video-tab.md](docs/free-video-tab.md)。

| 组成 | 入口 | 说明 |
| --- | --- | --- |
| 网页端 | `run_web.py` → `app/web/` | 标准库 `ThreadingHTTPServer` + 原生 JS |
| 引擎 | `app/core.py` / `app/wan_api.py` | 纯 Python，网页与 CLI 共用 |
| 生图 | `app/imggen/` | 独立队列 + urllib 客户端，不占视频的在飞名额 |
| 自由视频 | `app/videogen/` | 独立队列 + 独立记录；复用 `WanClient` 但与换装/模板/COS 归档链路无关 |
| 批次档案 | `app/web/batch_archive.py` | 款号 / 提示词 / 参考图永久副本；删批次保留，见 [docs/batch-archive.md](docs/batch-archive.md) |

## 2. 三分钟跑起来

> 分区（2026-11-06 起）：生产固定在**运行区**跑 8765；本设计区只做开发（8766 + `web-data-dev`）。
> 见上节 §0 与 [docs/dev-runtime-split.md](docs/dev-runtime-split.md)。

```bash
# 首次：创建管理员（写入 server/web.json）
python run_web.py --init

# 启动（设计区读到 8766；启动后约 7 秒才 bind 端口，别急着判失败）
python run_web.py
# 或双击「启动网页服务.vbs」（端口/数据目录从 server/web.json 读，两区通用）
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
   ├─ batches.py       BatchManager（草稿 → 筛选 → 锁定 → 多轮生产 → 关闭）
   └─ batch_archive.py 批次永久档案（款号 / 提示词 / 参考图副本；删批次保留）
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

生图 tab 走另一条完全独立的链路（不进 `jobs`、不占视频的在飞名额）：

```
app/web/static/imagegen.js
   │  POST /api/uploads（参考图，复用 uploads 目录）→ POST /api/imagegen/tasks
   ▼
app/imggen/manager.py   ImageGenManager：任务落盘 + queue.Queue + N 个守护线程（按 provider 分派）
   ├─ models.py         6 个型号（5 个 GPT Image VIP + Nano Banana 2.1）、两套尺寸口径、参数校验
   ├─ processing.py     17 档像素规格、4:5 裁切（只服务 Images 协议；Gemini 直出比例不裁）
   ├─ api.py            stdlib urllib：images/generations、images/edits（字段 image[]）
   ├─ gemini.py         stdlib urllib：{model}:generateContent（Nano Banana，1K/2K/4K）
   ├─ decoder.py        b64_json 或 url 下载（≤64MB）
   └─ errors.py         HTTP 码 → 中文、密钥脱敏
```

> 两套协议别混：`provider=images` 的尺寸是像素（`2048x2048`），`provider=gemini` 的是
> 「比例@档位」（`4:5@2K`），互不通用；Gemini 的图在 `candidates[].parts[].inlineData`，
> 且 HTTP 200 没图也会计费，所以不自动重试。

> 配置一律来自本地 `server/config.json` + `server/web.json`，改完重启生效；
> 没有云端配置下发，也没有授权校验（仓库里不再包含这些模块）。

一次小批量请求经过：`POST /api/uploads` → `POST /api/jobs`（落 `web-data/jobs/<id>.json`）
→ 调度线程 `_dispatch` → `_run_job` → `BatchRunner.run()` → 事件回写 `records/done`
→ 前端每 3.5 秒轮询 `/api/jobs` → 完成后 `GET /api/jobs/<id>/files/...` 下载。

另有一条**只读旁路**：服务启动时 `app/mirror/` 起一个守护线程，每 5 秒比对 `jobs/`、`batches/`、
`uploads/meta/` 的 `(mtime, size)`，把变化增量写进 `web-data/mirror/production.sqlite`（WAL），
供外部程序查询；金额口径来自 `app/billing.py`（与 `usage_for()` 同源）。
JSON 仍是唯一真源，镜像可删可重建，见 [docs/production-data.md](docs/production-data.md)。

## 4. 八条硬约束（改代码前必须知道）

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
8. **批次档案只追加，不随批次删除**：`web-data/archive/` 是款号、提示词、参考图的永久副本；
   `delete` 只写 `deleted.json` 墓碑，不删档案，`uploads` 以后做 GC 也不影响它。
   删批次前必须先成功写档案，写失败返回 500 并取消删除，不许绕过这条硬闸门；
   要清理只能人工确认后按款号整目录备份/删除，别在代码里做自动回收。

## 5. 配置与数据放在哪

> 下表路径按「当前文件夹」解析：运行区与设计区各自持有一套 `server/*.json` 与
> `web-data/`（设计区的生产数据已归档为 `web-data-生产快照-20261106/`，dev 用 `web-data-dev/`）。

| 路径 | 内容 | 是否入库 |
| --- | --- | --- |
| `server/web.json` | 账号、会话密钥、并发上限、快速通道参数 | ❌ 含密码哈希 |
| `server/config.json` | **明文 API Key**、模型、COS、单价、限流参数 | ❌ 绝不提交 |
| `server/imagegen.json` | 生图 tab 的**明文老张 Key**、base_url / gemini_base_url、默认型号、张数/参考图上限 | ❌ 绝不提交 |
| `server/*.example.json` | 上述三者的模板 | ✅ |
| `web-data/jobs/<id>.json` | 任务状态与逐条记录 | ❌ |
| `web-data/batches/<id>.json` | 批次与条目 | ❌ |
| `web-data/archive/<款号>/<批次号>__<batch_id>/` | **批次永久档案**：款号、提示词与历史、全部参考图副本、每轮 runs 快照、checksums、删除墓碑；删批次与 uploads GC 都不影响，见 [docs/batch-archive.md](docs/batch-archive.md) | ❌ |
| `web-data/template_tags.json` | **标签唯一真源**（视频清单 + 标签库 + 弃用状态 + 审计），网页与本地打标工具共用；`.bak` 是上一版 | ❌ |
| `web-data/manifest_overlay.json` | 网页上传的视频索引（云端 manifest 里没有它们）；启动时与 manifest 合并 | ❌ |
| `web-data/tag_versions/` | 真源版本快照（gzip，整库含每条标签与弃用态）；自动留 50 版 + 手动 50 版，回滚前另存备份 | ❌ |
| `web-data/{uploads,output,logs}/` | 定妆图与细节图、成片、任务日志 | ❌ |
| `web-data/ps/<upload_id>_s<强度>.png` + `.json` | PS 助手去紫结果（PNG + 统计侧车）；路径由原图 id 与强度派生，无登记表，可随时整目录删 | ❌ |
| `web-data/imagegen/tasks/<id>.json` + `files/<id>/` | 生图任务记录、成品 `<n>.png` + `<n>.meta.json` 侧车、参考图副本 `ref<n>.<ext>`（创建任务时拷贝，uploads 删了也能复现） | ❌ |
| `web-data/videogen/{tasks,files,uploads}/` | 自由视频任务记录（`tasks/<id>.json`）、参考素材副本与成品 `shot-<n>.mp4`、参考视频/音频上传元数据；删任务会连 `files/<id>/` 一起删，`uploads/` 不回收 | ❌ |
| `web-data/mirror/production.sqlite` | **生产数据镜像**（只读副本）：jobs/records/batches/uploads/usage_daily + 三个视图，给外部程序（报表、Excel/Power BI、AI agent）读；服务进程内守护线程每 5 秒跟随 JSON 增量同步，**可随时删除自动重建**。见 [docs/production-data.md](docs/production-data.md) | ❌ |
| `app/web/static/` | 前端源码（每次请求读盘，改完刷新即可） | ✅ |

**多图（v0.17.0）**：两页都支持 10 个槽位（槽位 1 定妆图必填，2–10 细节图可选），数组顺序即模型 `imageUrls` 顺序与 prompt 的「图N」。job / batch 以 `images[]` 为唯一真源，`image_path/image_name/image_url` 保留为**主图别名**（老数据零迁移）。`/api/jobs`、`/api/batches` 收 `upload_ids[]`（兼容旧 `upload_id`）；锁定后细节图走 `POST /api/batches/<id>/images` 的 `op=add/remove`，主图不可换。删除细节图后序号自动左移；**不改 prompt**，指代由用户自己写「图2…图10」。COS 关闭时 `app/media.py:base64_budget_error` 会按总量 40MB 兜底拒绝。uploads 目录目前**没有 GC**，10 倍图量会持续堆积，后续可加「按 job/batch 引用计数 + 7 天」的清理。
`archive/` 是独立永久副本：不参与 uploads 清理，也不会被删批次带走；uploads 仍是运行期真源，档案只是快照。

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
python tests/test_batch_archive.py # 批次永久档案：落档 / 图片增删 / 提示词历史 / ZIP / 删除墓碑 / 补档
python tests/test_imagegen.py    # 生图 tab：选项下发 / 参数校验 / 取图下载 / 权限 / 重启恢复
python tests/test_videogen.py    # 自由视频 tab：选项 / 文生·图生·视频编辑 / 校验 / 权限 / 取消 / 删除
python tests/test_data_mirror.py # 生产数据镜像：行数/路径、与 usage_for() 口径对平、脱敏、增量、坏库重建
```

`tests/run_all.py` 覆盖引擎、存储、网页接口、批次、批次永久档案、生图、自由视频与数据镜像端到端（33 套，含 `test_path_relocate`），全部离线运行。

## 7. 别动 / 小心

- `app/web/__init__.py`：`.gitignore` 里的 `_*.py` 曾误伤所有 `__init__.py`，现已加例外。
  新建包时若发现 `__init__.py` 没进版本库，先查 `.gitignore`。
- `web-data/`、`server/web.json`、`server/config.json`：本机运行数据，不要为了“干净”删掉。
- `web-data/archive/`：永久档案，**不要手工删单张图/改文件名**（会破坏 `checksums.sha256`）；
  要清理只能按款号整目录备份后处理，别用脚本扫 `images/` 做去重。
- 服务启动**约 7 秒后**才监听端口；用 `netstat -ano | findstr 8765` 确认只有一个 LISTENING。
  别对**同一个数据目录**起第二个进程（`SO_REUSEADDR` 允许双绑同一端口，实例锁只按 `data_dir` 区分）；
  「8765 生产 + 8766 dev」同时存在是正常状态。设计区 `server/web.json` 不要改回 8765 / `web-data`。
- 重启会打断 running 任务；重启前先看 `web-data/jobs/*.json` 里有没有 `running/queued`。
- 旧的 `cordis.patch.yml.bak-*`、`probe-registry.mjs` 是历史残留，不要当入口。
- 打标器只能从**运行区**启动（`启动器\启动视频标签整理.bat`）：它按所在文件夹定位
  标签真源（`启动打标器.py` 设 `OUTFIT_TAG_STORE=BASE/web-data/template_tags.json`），
  从设计区启动会写进 dev 副本。

## 8. 已知债（别当成 bug 修）

- `server.py` 2000+ 行、`wan_api.py` 1120 行：路由与协议都写在大类里，按需再拆，不要顺手重构。
- 生图任务是**单进程内存队列**（`queue.Queue` + 守护线程），没有跨进程锁；同一 `data_dir` 起两个实例会互相抢任务文件，别这么干。重启不做断点续传，进行中的任务直接落成「生成失败」。
- 自由视频（`app/videogen/`）同样是单进程内存队列 + 守护线程（线程数 `web.json.videogen_max_inflight`，默认 2），重启不续传、`count>1` 串行跑；取消是协作式的，已提交给引擎的任务不撤回、照常计费。
- 批次档案是**同步复制**：建批次/换图/每轮生产都会复制缺失的参考图并重写 checksums，批量换图时会有一次磁盘 IO 尖峰；档案约等于批次参考图的 1 倍磁盘，没有 GC/去重，只能人工按款号清理。ZIP 是现场同步打包（非事务），大档案下载会占请求线程；老批次第一次 probe 不落盘、第一次下载才补齐。
- 测试是独立脚本，没有 pytest 夹具；`tests/run_all.py` 只是顺序执行器。
- `wan.price_per_second` 三档均为 `null`，金额用公开价目表兜底，与真实账单有偏差。
- 本仓库只维护网页端与引擎；不要再引入 GUI 界面或桌面打包相关代码。

## 9. 文档索引

| 文档 | 用途 |
| --- | --- |
| [docs/core-code-and-migration.md](docs/core-code-and-migration.md) | **核心代码导读 + 迁移手册**：搬什么/不搬什么、配置与数据清单、八步迁移、必改的硬编码地址、验收清单 |
| [docs/architecture.md](docs/architecture.md) | **网页版代码架构**：分层、线程、额度、数据模型、接口 |
| [docs/project-summary-20261102.md](docs/project-summary-20261102.md) | v0.6.0 现状、参数表、运维与风险 |
| [docs/account-usage-and-quota.md](docs/account-usage-and-quota.md) | 单 Key 共用与账户用量监控口径 |
| [docs/web-lan.md](docs/web-lan.md) | 局域网部署与访问 |
| [docs/dev-runtime-split.md](docs/dev-runtime-split.md) | 运行区/设计区分区：布局、发布与回滚 |
| [docs/ui-rollback.md](docs/ui-rollback.md) | 界面改版的基线与回滚步骤 |
| [docs/ps-assistant.md](docs/ps-assistant.md) | PS 助手 tab（去紫）：算法口径、接口、像素上限与排障 |
| [docs/free-video-tab.md](docs/free-video-tab.md) | 自由视频 tab（通用 wan3.0）：用法、接口、参数口径、数据落盘、并发计费与限制 |
| [docs/batch-archive.md](docs/batch-archive.md) | **批次永久档案**：目录结构、触发时机、ZIP 接口、历史补档、备份恢复与已知边界 |
| [docs/production-data.md](docs/production-data.md) | **生产数据镜像**：SQLite 只读副本的表/视图字段、金额口径、外部程序（Python/LLM、Excel、Power BI）接法、新鲜度与排障 |
| [docs/llm-data-access.md](docs/llm-data-access.md) | **给 LLM / Agent 的数据获取指南**：怎么连、有哪些表、自然语言→SQL 速查、字段口径与别做的事（要查数据先读这篇） |
| [docs/README.md](docs/README.md) | 全部文档索引（现行 / 历史） |
| [README.md](README.md) | 对外说明与快速开始 |
