# 换装批量生产器 · 项目交接手册

> **这份文档写给接手人**：从「这台电脑上的代码」到「客户机上能一键出片」，中间每个环节在哪、怎么改、怎么验、哪里会踩坑，都在这里。
> 读完第 1 章能接手判断，读完第 6 章能独立运维，第 13 章是双方逐项签字的交接清单。

| 项 | 内容 |
| --- | --- |
| 文档版本 | **v1.0**（2026-10-28 11:00） |
| 适用代码 | 分支 `feature/aliyun-wan3-official`，HEAD `1d7b9cf`，`APP_VERSION = "0.9"` |
| 适用产品 | 桌面版「换装批量生产器-官方版」+ 命令行工具 + 腾讯云授权服务 |
| 交接范围 | 代码、云资源、授权体系、COS 存储、模板清单、提示词库、发布包、文档 |
| 移交人 / 接手人 | ______________ / ______________ |
| 保密级别 | **内部**。本文**不含任何密钥明文**；密钥走密码管理器或当面移交（见 §2.3） |

---

## 0. 先读这一段

### 0.1 三句话讲清项目

1. **做什么**：1 张无头模特图 + 100 个模板视频 → 100 条 7 秒 720P 竖屏「只换衣服、不换人」的视频。
2. **怎么交付**：Windows 桌面程序（PySide6，onedir 打包），输密码激活后**零配置**跑批；成品落本地并自动归档腾讯云 COS。
3. **谁在跑**：生成用阿里云百炼官方 `wan3.0-video`；授权与配置下发用腾讯云函数 `outfit-license`；素材与成品放腾讯云 COS。

### 0.2 接手时最该先知道的三个事实

| # | 事实 | 影响 | 去哪处理 |
| --- | --- | --- | --- |
| 1 | **代码只在这台机器上**：`feature/aliyun-wan3-official` 比本地 `main` 多 13 个提交；相对 `origin/main` 共 **15 个提交未推送**（`origin` 只有 `main`，且 `main` 自己还压着 2 个） | GitHub Releases 里没有官方版安装包；换机器/硬盘坏了就全丢 | §11 P0-1，§13 Day 1 |
| 2 | **线上是否已下发新 Key 未复核**：本地 `server/config.json` 已在 2026-10-28 09:46 填入百炼 Key，但最近一次部署包 `dist/license-deploy.zip` 停在 10-26 11:31 | 客户端可能仍显示「配置：未下发」 | §6.1，跑一次 `license_deploy.py config` |
| 3 | **模板只有 108 / 500** | 大批量只有第 1 组和「随机 100 条」能跑，第 2–5 组会提示「模板不足 100 个」 | §6.3 |

> 写本文时本机**无法访问外网**（TLS 被环境拦截），所以「线上状态」只能引用最后一次作业记录，不能当作实时结论。
> **接手后第一件事**：在一台能上网的机器上执行 `python3 scripts\license_deploy.py config`，把结果补进 §9.3。

### 0.3 文档约定

- 命令默认在项目根目录 `D:\批量视频任务设计区` 执行，终端用 **PowerShell**。
- 这台机器上 `python3` 是真实解释器；`python` / `py` 不可靠（前者是微软商店存根，后者不存在），**统一写 `python3`**。
- 所有「密钥」在本文只用**名称 + 位置**描述，不出现值。看到 `sk-...`、`AKID...`、`RH-2026-...` 这类字样，一律视为敏感信息，不要贴进聊天、Issue、提交。

---

## 1. 一页速览

### 1.1 产品形态

| 形态 | 入口 | 谁用 |
| --- | --- | --- |
| 桌面版（主交付） | `dist-official\换装批量生产器-官方版\换装批量生产器-官方版.exe` | 运营/操作员，双击即用 |
| 命令行 | `wan3_r2v.py` | 开发、批量补跑、对账 |
| 离线演练 | `demo.py` / `一键演练.bat` | 任何人，不花钱、不联网 |
| 授权与配置服务 | 腾讯云函数 `outfit-license` | 管理员部署，客户端自动访问 |
| 内部运维工具 | `scripts\`（密码管理器、部署、补传） | 管理员，**不分发给客户** |

### 1.2 当前阶段（截至 2026-10-28）

```
协议适配 ✅ → 离线全链路 ✅ → 测试 ✅ → 授权服务 ✅ → 模板清单 ✅(108) → 真实出片 ✅(1 条 480P/5s)
                                                                                    ↓
                                                              桌面端实机 ⬜ → 服务端 deploy 复核 ⬜ → 720P/7s 打样 ⬜ → 补模板到 500 ⬜
```

一句话：**技术链路已经跑通一条真片，剩下的是「把配置正式部署、在真桌面验一遍、把量跑起来」**。

### 1.3 接手第一天做什么（约半天）

```powershell
# 0) 确认仓库与分支（别在 main 上改）
git status
git log --oneline -5
git branch -vv

# 1) 先跑不花钱的离线演练，确认环境没坏
python3 demo.py --count 5

# 2) 跑全部离线测试（7 套，约 1–2 分钟）
python3 tests\test_wan_api.py
python3 tests\test_storage.py
python3 tests\test_core.py
python3 tests\test_remote_config.py
python3 tests\test_prompts.py
python3 tests\test_license.py
$env:QT_QPA_PLATFORM="offscreen"; python3 tests\test_ui.py

# 3) 看服务端配置齐不齐（只打印长度，不打印 Key）
python3 scripts\license_deploy.py config

# 4) 读三篇：本文 §6（运维）、docs\usage-aliyun-wan3.md（使用）、docs\wan-3.0-reference-to-video.md（协议）
```

**第 1 天不要做的事**：不要直接跑 100 条；不要改 `LICENSE_SECRET`；不要 `git push --force`；不要把 `server\config.json` 提交。

### 1.4 业务规模与成本口径（做预算用）

| 项 | 口径 | 说明 |
| --- | --- | --- |
| 目标产能 | 500 条/天 × 26 天/月 ≈ **13,000 条/月** | 每批 100 条，按模板组切 |
| 单条体积 | 7s 720P ≈ 15–20 MB；480P/5s 实测 2.8 MB | 100 条约 2 GB 本地 + COS |
| 计费秒数 | `usage.duration` **含输入参考视频**：7s 模板 + 7s 输出 ≈ **14s/条** | 100 条 ≈ 1400s，不是 700s |
| 单价 | `wan.price_per_second` 当前**未填** | 金额以阿里云账单为准；填了才有「预估费用」 |
| COS 月费 | 模板拉取 + 成品下载各约 190 GB/月 | 约 ¥190–260，占总成本 **<5%** |
| 工期 | 并发 3、单条 1–3 分钟 | 100 条约 **50–90 分钟** |

> 生成费是绝对大头，但**真实单价尚未实测**（见 §9.3）。接手后第一笔真实账单要拿来校准这个模型。

---

## 2. 交接资产清单

### 2.1 代码与仓库

| 项 | 值 | 备注 |
| --- | --- | --- |
| 远端仓库 | `https://github.com/1157970770/outfit-batch-tool.git` | 只有 `main` 在远端 |
| 本机工作区 | `D:\批量视频任务设计区` | 当前分支 `feature/aliyun-wan3-official` |
| 当前 HEAD | `1d7b9cf`（2026-10-28 10:48） | 相对 `main` +13；相对 `origin/main` +15 |
| 旧 RH 版工作树 | `D:\批量视频任务设计区\.worktrees\rh-legacy`（`feature/prompt-library`，`b09cf4e`） | 只读保留，**不要在里面改新功能** |
| 主分支 | `main`（`546bcab`，比 `origin/main` 多 2 个提交） | 也**未推送** |
| 已跟踪文件 | 89 个 | `git ls-files` 可查 |
| 构建产物 | `dist\`（旧 RH 版）、`dist-official\`（新官方版） | 都在 `.gitignore`，不进仓库 |

**分支关系**：

```
origin/main ──●──●  (main, 本地还多 2 个未推送提交)
                    └──●──●──● … 13 个提交 → feature/aliyun-wan3-official（当前，未推送）
                                          └── feature/prompt-library（旧 RH 版，b09cf4e）
```

### 2.2 云资源

| 资源 | 标识 | 配置 | 用途 |
| --- | --- | --- | --- |
| 腾讯云函数 | `outfit-license`（ap-guangzhou） | Python 3.9，128 MB / 5 s，函数 URL 触发器 | 授权、配置下发 |
| 函数 URL | `https://1463907213-3ifuebn6ih.ap-guangzhou.tencentscf.com` | 内置在 `app/config.py:18` | 客户端唯一服务地址 |
| COS 模板桶 | `video-input-1463907213`（ap-guangzhou） | `input/tpl/*` 与根 `manifest.json` **公有读**，其余私有 | 模板视频、清单 |
| COS 成品桶 | `video-output-1463907213`（ap-guangzhou） | `prod/output/*` 允许子账号 Put/Get/分片；禁止列举/删除 | 服装图源、成片归档 |
| 子账号 | `rh-client-uploader`（无控制台登录） | 策略 `rh-client-upload`（PolicyId `291294131`） | 客户端写成品桶 |
| 百炼 | `wan3.0-video`，地域 `cn-beijing` | `base_url = https://dashscope.aliyuncs.com/api/v1` | 视频生成 |
| 模板源素材 | `D:\视频模板分类工作区` | 108 个 mp4（近景 14 / 中远 94） | `upload_templates.py` 的输入 |
| 授权密码表 | `server\passwords.json` + 云函数内 `/var/user/passwords.json` | 3 条（2 条有效至 2026-11-24，1 条永久） | 谁能用、到哪天 |

### 2.3 凭证与密钥（**只列位置，不列值**）

| 凭证 | 用途 | 存放位置 | 是否进 Git | 轮换方式 |
| --- | --- | --- | --- | --- |
| 百炼 API Key | 调用 `wan3.0-video` | `server\config.json` → `wan.api_key`；云函数同文件 | ❌ | 控制台新建 → 改 config → `deploy` |
| COS 子账号密钥 | 客户端写成品桶 | `server\config.json` → `cos.secret_id/secret_key` | ❌ | CAM 新建密钥 → 改 config → `deploy` |
| `LICENSE_SECRET` | 票据 HMAC 签名 | **只在云函数环境变量**里 | ❌ | **改它 = 全员重新激活**，非必要不动 |
| 授权密码表 | 激活凭据 | `server\passwords.json`、云函数 | ❌ | 密码管理器「停用/续期」 |
| 管理员 COS 凭证 | 上传模板、发布清单 | `~\.cos\credentials.json` 或环境变量 | ❌ | 控制台轮换 |
| tccli 凭据 | 部署云函数 | `~\.tccli\`（profile `agent1`） | ❌ | `tccli auth login --profile agent1` |
| 代码签名证书 | 消除 SmartScreen | **目前没有** | — | 采购后配 `CODESIGN_THUMBPRINT` |
| GitHub 账号 | 推送代码、发 Release | 交接时当面授权 | — | — |

> **移交方式**：`server\config.json`、`server\passwords.json`、云函数环境变量、`~\.cos`、`~\.tccli` 这五处**必须当面/密码管理器移交**，不要走聊天工具。
> 拿到后立刻确认：`git check-ignore -v server/config.json server/passwords.json` 两行都有输出（说明确实没入库）。

### 2.4 账号与责任人

| 账号 / 资源 | 当前持有人 | 交接动作 | 交接后责任人 |
| --- | --- | --- | --- |
| 腾讯云主账号（SCF / COS / CAM） | ____________ | 转移管理员或加子账号 | ____________ |
| 阿里云账号（百炼 / 账单 / 预算告警） | ____________ | 转移或加 RAM 子账号 | ____________ |
| GitHub 仓库 `1157970770/outfit-batch-tool` | ____________ | 加 Collaborator / 转移仓库 | ____________ |
| 素材版权方 / 授权文件 | ____________ | 交付授权范围与期限 | ____________ |
| 使用方 / 客户联系人 | ____________ | 交接沟通渠道 | ____________ |

> 上表必须在 §13.4 签字前填完；「有账号但没写责任人」是交接后最常见的断点。

### 2.5 素材与产物

| 资产 | 路径 | 是否在 Git | 说明 |
| --- | --- | --- | --- |
| 提示词库（6 条） | `prompts\prompt-library.json` | ✅ | 打包时复制到 exe 同目录 `prompts\` |
| 提示词原文 | `prompts\raw\P-2026-10-27-0*.md` | ✅ | 6 篇，逐字对齐 |
| 定妆图（真实素材） | `assets\定妆图.jpg`（349 KB） | ❌ **被 gitignore** | **必须单独拷贝移交**，否则新人无法复现 |
| 占位图 | `assets\_placeholder.png` | ✅ | 演练用 |
| 模板清单 | `build\manifest\manifest.json`（108 条） | ❌（`build/` 忽略） | 已发布到 COS 桶根 |
| 真实成片样例 | `out\REAL5S-080-2\080.mp4` + `batch.json` + `report.csv` | ❌（`out/` 忽略） | 只在本机，建议随交接拷贝 |
| 官方版安装包 | `dist-official\换装批量生产器-官方版-v0.9-20261028.zip`（62.6 MB） | ❌ | 可直接分发 |
| 旧 RH 版安装包 | `dist\换装批量生产器-v0.9-20261026.zip`（39.6 MB） | ❌ | **不要覆盖/删除** |

---

## 3. 系统架构

### 3.1 生产链路

```
模板清单 manifest.json（COS 公有读）
        │
        ├─▶ 客户端读清单 → 选组（001–100 / 随机 100）
        │
无头模特图（本地）
        └─▶ 上传一次到 COS prod/output/source/<日期>/ → 签 7 天 URL
                    │
                    ▼
        POST https://dashscope.aliyuncs.com/api/v1/services/aigc/video-generation/video-synthesis
             body: model=wan3.0-video
                   input.media = [reference_video(模板), reference_image(服装图)]
                   parameters  = 720P / adaptive / duration=-1 / prompt_extend=false
                    │
                    ▼  秒回 task_id
        GET /tasks/{task_id} 轮询（15s，抖动，10 QPS）
                    │
                    ▼  SUCCEEDED
        立刻下载（结果 URL 24h 失效）→ 校验（>50KB、时长≈模板）→ 落本地
                    │
                    ├─▶ 归档 COS：prod/output/<日期>/<批次>/<编号>-<指纹8>.mp4
                    └─▶ 写 report.csv / batch.json / log.txt
```

### 3.2 授权与配置链路

```
双击 exe
  │
  ├─ 服务地址：app/config.py:18 的 SERVICE_URL（写死，改地址必须重新打包）
  │
  ├─ 本地有票？ ── 否 ──▶ 弹框输密码 ──▶ POST /activate {password, machine}
  │                                      └─▶ 返回 token + config（Key/清单/COS/公告）
  │
  └─ 有票 ──▶ POST /verify {token, machine}
                 ├─ 服务端回表查 expires / revoked / machine → 放行，并再下发 config
                 └─ 连不上：上次成功核验在 7 天内可离线；超过要求联网
```

- 票据存 `%APPDATA%\RHOutfitToolWan3\license.json`（DPAPI 加密，`dpapi:` 开头），**里面没有明文密码**。
- 客户端**没有任何可编辑配置文件**：「改配置」永远是改服务端 `config.json` + `deploy` → 客户端下次启动生效。
- 旧 RH 版用 `%APPDATA%\RHOutfitTool\`，两版互不覆盖（见 §6.7）。

### 3.3 三方职责

| 角色 | 负责 | 不负责 |
| --- | --- | --- |
| 腾讯云函数 | 发牌（授权）、下发运行配置 | 不碰生成、不存视频 |
| 腾讯云 COS | 存模板、服装图、成片 | 不做鉴权决策 |
| 阿里云百炼 | 生成视频、返回 24h 结果链接 | 不存长期素材 |
| 客户端 | 组请求、轮询、下载、校验、归档、报表 | 不持有可编辑配置 |

### 3.4 关键决策（读 ADR，不要重新发明）

| 决策 | 结论 | 文档 |
| --- | --- | --- |
| 生成接口 | 2026-10-27 从 RunningHub 切到官方 `wan3.0-video`，保留 `provider=runninghub` 回滚 | `docs\adr-002-aliyun-wan3-official-api.md` |
| 存储 | **不迁 OSS**。模板走 COS 公有直链，服装图走 COS 签名 URL（已实测官方能拉） | ADR-002 §2.3 |
| 客户端持有 COS 子账号密钥 | **已接受**。暴露面压到只能写成品区，不能读模板桶、不能列举、不能删 | `docs\adr-001-cos-client-credential.md` |
| 计费口径 | 官方只给秒数，按 `usage.duration` 记账；填 `price_per_second` 才显示金额 | ADR-002 §2.4 |
| 提交语义 | at-most-once：收到响应后不自动重试，避免重复建任务/重复扣费 | ADR-002 §2.5 |

---

## 4. 代码地图

### 4.1 桌面程序 `app\`

| 文件 | 职责 | 关键点 |
| --- | --- | --- |
| `main.py`（33 KB） | 主窗口、三个页签、状态灯、启动门禁 | `APP_VERSION` 是版本号唯一真源；`resolve_license_server()` 只用内置常量 |
| `runtime.py`（2 KB） | 无界面运行时：配置映射 + 模板/成品双桶构造 | 交付前刚从 `main.py` 抽出（未提交），桌面/网页可复用 |
| `core.py`（23 KB） | 引擎：清单→分组→并发提交→轮询→下载→校验→归档→报表 | `GROUP_SIZE=100`、`TOTAL_TEMPLATES=500`；`BatchRunner` 可脱离界面单测 |
| `wan_api.py`（38 KB） | DashScope / RunningHub 双协议适配 | 请求体映射、约束校验、错误归类、`usage` 计费、`task_id` 状态机 |
| `storage.py`（27 KB） | COS / 本地存储抽象 | put/exists/stat/sign/delete/list；分片、重试、签名 |
| `cos_rest.py`（8 KB） | 缺 `qcloud_cos` 时的标准库 REST 兜底 | 打包时 `--hidden-import app.cos_rest` |
| `media.py`（15 KB） | 图片预检、COS 源图上传、Base64、视频探测 | 图片带 alpha 自动转白底 JPEG |
| `config.py`（6 KB） | 服务端 `config` → 本地扁平 `cfg` 映射 | `SERVICE_URL`、双桶、凭证、价格表 |
| `license.py`（9 KB） | 激活、票据缓存、`/verify`、7 天救援窗口 | token 绑机器 |
| `startup.py`（2 KB） | 启动弹框输密码，3 次失败退出 | **fail-closed**：`server` 为空直接返回 False，不会静默放行 |
| `secure_store.py`（4 KB） | DPAPI 加解密 | 缓存落地即 `dpapi:` |
| `prompts.py` / `prompts_page.py` | 提示词库加载与第三页 UI | 改 JSON 可「重新加载」，不用重打包 |
| `transfer.py`（12 KB） | 远端链接 → 对象存储流式转存 | **主流程未接入**，预留 |

### 4.2 命令行与服务端

| 文件 | 职责 |
| --- | --- |
| `wan3_r2v.py`（25 KB） | 独立批量客户端：validate / build / emit / run / upload / query |
| `make_batch.py` | 服装清单 `outfits.tsv` → 100 条任务 JSON（旧链路，仍可用） |
| `demo.py` | 离线演练：假百炼 + 假图，走完整链路 |
| `server\license_api.py`（13 KB） | 单文件零依赖授权服务；SCF 入口 `main_handler`，也可 `--serve` 本地跑 |
| `server\config.example.json` | 配置模板（**永远为空**，复制成 `config.json` 再填） |
| `tools\upload_templates.py` | 视频目录 → 上传 `input/tpl/` + 打标签 + 生成清单 |
| `tools\publish_manifest.py` | 清单发布到桶根 + 匿名可读自检 |
| `tools\build_manifest.py` | 本地视频目录 → manifest（旧工具，仍可做离线清单） |
| `scripts\license_deploy.py` | pack / deploy / trigger / url / test / status / config / check |
| `scripts\license_passwords.py` | 密码表增删改 + 自动同步 |
| `scripts\password_manager.py` | 密码管理 GUI（`scripts\密码管理器.bat`） |
| `scripts\build-official.ps1` | **新官方版**打包（输出 `dist-official\`） |
| `scripts\build.ps1` | **旧 RH 版**打包（输出 `dist\`，不要拿来打新版） |
| `scripts\cos_check.py` | COS 连通性自检（写/签/读/存在/列举/删） |
| `scripts\archive_to_cos.py` | 历史成片补传 COS，回写 `cos_key` |

### 4.3 测试

| 文件 | 覆盖 | 是否联网 |
| --- | --- | --- |
| `tests\test_wan_api.py` | 请求体映射、媒体顺序、`duration` 整数、官方约束、错误分类、计费、源图上传 | 否 |
| `tests\test_storage.py` | 假 COS 客户端 + 本机 HTTP、分片、重试、转存 | 否 |
| `tests\test_core.py` | 引擎全流程 + 归档幂等 | 否 |
| `tests\test_remote_config.py` | 服务端 config → 激活 → 客户端映射 → 缓存加密 | 否 |
| `tests\test_prompts.py` | 提示词库加载与页面 | 否（需 PySide6） |
| `tests\test_license.py` | 激活/错密码/停用/到期/换机/网关重试 | 否（本地假服务） |
| `tests\test_ui.py` | 界面集成：3 条、报表、重名目录 | 否（离屏） |
| `tests\mock_dashscope.py` | 本地假百炼（提交/查询/下载） | 否 |

---

## 5. 环境搭建

### 5.1 机器要求

| 项 | 要求 | 本项目实测 |
| --- | --- | --- |
| 操作系统 | Windows 10/11 x64 | Windows |
| Python | 3.9+（源码运行/打包） | 3.12 |
| 外网 | 能访问 `dashscope.aliyuncs.com`、`*.myqcloud.com`、`*.tencentscf.com` | — |
| 磁盘 | 源码 + 依赖 + 产物预留 ≥ 20 GB（100 条 720P ≈ 2 GB/批） | — |
| ffmpeg/ffprobe | 可选；用于时长预检 | 建议装 |

### 5.2 安装

```powershell
cd D:\批量视频任务设计区

# 换机器时：分支尚未推送，优先整目录拷贝（含未提交/未推送内容）。
# 推送之后才可以这样拉：
#   git clone https://github.com/1157970770/outfit-batch-tool.git
#   git checkout feature/aliyun-wan3-official

pip install -r requirements.txt
# PySide6>=6.5 / Pillow>=9.0 / cos-python-sdk-v5>=1.9.30（可选，缺包会走 cos_rest 兜底）

# 部署云函数才需要：
pip install tccli
tccli auth login --profile agent1
```

> 项目自带的 `python3` 运行时里通常已装好 PySide6；如果 `python3 tests\test_ui.py` 报缺模块，
> 用 `python3 -m pip install PySide6` 补，不要改用商店版 `python`。

### 5.3 源码运行

```powershell
python3 run_app.py                 # 启动桌面版（会走真实授权）
```

> **没有免授权开关**：现行 `app/startup.py` 在服务地址为空时直接返回 False（fail-closed）。
> 源码运行同样要输密码；用密码表里任意一条激活一次，票在有效期内复用即可。
> 老文档里的 `RH_DEV_NO_LICENSE=1` 在当前代码中**不存在**，别照抄（见 §9.4）。

### 5.4 离线演练（不花钱、不联网）

```powershell
python3 demo.py --count 5
# 或双击 一键演练.bat
```

它会：造占位定妆图 → 起本地假百炼 → 提交/轮询/下载/记账 → 打印结果表。产物在 `out\demo\`。

---

## 6. 运维手册（Runbook）

### 6.1 改服务端配置并下发

**唯一真源**：`server\config.json`（已被 `.gitignore`，不进 Git）。

```powershell
# ① 改 server\config.json（只改要改的字段；不要动字段名）
# ② 本地自检（只打印长度/是否已填，不打印 Key）
python3 scripts\license_deploy.py config
# ③ 打包 + 上传 + 等函数 Active
python3 scripts\license_deploy.py deploy
# ④ 复核线上是否真的下发
python3 scripts\license_deploy.py config
```

期望结尾：

```
[config] 线上 wan.api_key : 已下发（长度 N）
[config] 线上 wan         : model=wan3.0-video base=https://.../api/v1
[config] 结论: 配置完整，客户端可用
```

**字段表**（完整说明见 `docs\usage-aliyun-wan3.md` §2.2）：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `wan.provider` | ✅ | `dashscope`（默认）/ `runninghub`（回滚） |
| `wan.api_key` | ✅ | 百炼 Key |
| `wan.model` | ✅ | `wan3.0-video`（质量）/ `wan3.0-video-prime`（速度） |
| `wan.base_url` | ✅ | 必须带 `/api/v1` |
| `wan.default_mode` | ✅ | 桌面默认 `edit` |
| `wan.default_duration` / `default_ratio` | 建议 | `-1`/`adaptive`（edit）或 `7`/`9:16`（reference）；**切换模式要同时改**，或设 `null` 用模式预设 |
| `wan.max_concurrency` | ✅ | 服务端允许的最大并发，默认 3；界面选大了会被钳制 |
| `wan.price_per_second` | 可选 | 按分辨率填元/秒，不填只显示秒数 |
| `rh_api_key` | 回滚时 | 旧客户端只认它；过渡期可留空 |
| `manifest_url` | ✅ | 模板清单地址 |
| `cos.*` | ✅ | 双桶、地域、前缀、子账号密钥 |
| `notice` | 可选 | 客户端公告；`_` 开头字段不会下发 |

**四个坑**：

1. `tccli` 要写 `C:\Users\<你>\.tccli\log\tccli.log`；在受限沙箱里跑 `deploy` 会 `PermissionError`，要在正常终端执行。
2. `deploy` 不是即时的：脚本会等函数回到 `Active`；客户端**下次启动**才拿到新配置。
3. `deploy` 的 ZIP **必须**含 `license_api.py` + `passwords.json` + `config.json`，缺一个就失败（脚本不会静默少传）。
4. 改 `config.json` 时**不要改字段名**（`wan` / `rh_api_key` / `manifest_url` / `cos` / `api_key` / `secret_id` / `secret_key`），改名会让老客户端全废。

### 6.2 授权与密码管理

**图形界面**：双击 `scripts\密码管理器.bat`（内部工具，不分发）。

| 操作 | 效果 | 生效 |
| --- | --- | --- |
| 新增 | 自动编号 `RH-2026-NNN-xxxx`，默认 31 天 | 同步后立即 |
| 续期 / 改到期 | 改 `expires`；改到过去 = 立即失效 | 下次 `/verify` |
| 停用 / 恢复 | 切 `revoked` | 下次 `/verify` 立刻拒 |
| 删除 | 从表里移除，**不可恢复** | 一般用停用代替 |
| 立即同步到云 | 存本地 → 打包 → 上传 → 线上验证 | 10–30 秒 |

**命令行等价**：

```powershell
python3 scripts\license_passwords.py list
python3 scripts\license_passwords.py add --note "张三 / 设计部"
python3 scripts\license_passwords.py expire <CODE> 2028-12-31
python3 scripts\license_passwords.py revoke <CODE>
python3 scripts\license_passwords.py unrevoke <CODE>
python3 scripts\license_passwords.py deploy                  # 只同步，不改表

python3 scripts\license_deploy.py check <CODE>               # 验这条线上可用
python3 scripts\license_deploy.py check <CODE> --expect reject  # 验已被拒
```

**当前密码表**：3 条，2 条有效至 **2026-11-24 23:59**，1 条**永久**（建议改成明确日期，否则无法靠到期自然收口）。

**红线**：

- **不要改 `LICENSE_SECRET`**：一改所有票签名失效，全员必须重新激活。
- 密码表不进 Git、不贴对话、不随软件分发。
- 同步会重新部署云函数；如果同时有人在改 `config.json`，先确认对方已部署完，避免互相覆盖。

### 6.3 模板清单：从视频到「程序能用的清单」

**现行方案**：模板走**公有读直链**，清单放桶根，客户端只读清单、不签名、不碰模板桶。原因见 `docs\templates-500.md` §0（客户端子账号对模板桶零权限，签不了模板链接）。

```powershell
# ① 看编号和标签，不上传
python3 tools\upload_templates.py --dry-run

# ② 先传 5 个试水
python3 tools\upload_templates.py --limit 5

# ③ 全量（幂等：对象大小一致就跳过）
python3 tools\upload_templates.py --no-duration --out build\manifest\manifest.json

# ④ 发布清单到桶根 + 设公有读
python3 tools\publish_manifest.py

# ⑤ 只验证：匿名拉取 + 客户端视角解析
python3 tools\publish_manifest.py --verify-only
```

| 规则 | 说明 |
| --- | --- |
| 编号 | 按「目录顺序 + 文件名顺序」稳定编号 `001…NNN`；目录名含「近景」→ `shot=near`，否则 `medium_far` |
| 分组 | 「第 N 组」= 清单第 `(N-1)*100+1 ~ N*100` 项，**按清单顺序切，不按文件名** |
| 公有读 | 每个模板对象都要 `public-read`，模型才能匿名拉取 |
| 验收 | 浏览器能直接播放任意 `input/tpl/NNN.mp4`（不是 403）；清单能下载且条数一致 |
| 发布地址 | `https://video-input-1463907213.cos.ap-guangzhou.myqcloud.com/manifest.json`（覆盖写，URL 不变） |

**补齐到 500**：素材放进 `D:\视频模板分类工作区`（或 `--root` 指定）→ 全量上传 → 发布 → 客户端重启。
`app/core.py` 的 `TOTAL_TEMPLATES=500` 是写死的分组上限，**补素材即可，不用改代码**。
数量长期不足 500 时，建议把分组改成按实际条数动态算（见 §11 P2）。

### 6.4 打包与发版

**新官方版（唯一正确方式）**：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build-official.ps1
# 可选：指定版本 / 换输出目录 / 签名
powershell -ExecutionPolicy Bypass -File scripts\build-official.ps1 -Version 1.0
powershell -ExecutionPolicy Bypass -File scripts\build-official.ps1 -OutRoot build\release
powershell -ExecutionPolicy Bypass -File scripts\build-official.ps1 -Sign -CertThumbprint <指纹>
```

产物：

```
dist-official\换装批量生产器-官方版\换装批量生产器-官方版.exe
dist-official\换装批量生产器-官方版-v<版本>-<日期>.zip
```

脚本做的事：读 `app/main.py` 的 `APP_VERSION` → 关旧实例 → PyInstaller `--onedir --windowed --noupx` → 写版本信息资源 → 复制提示词库 → 放 `解除锁定并启动.bat` → `--selftest` → 可选签名 → 压 zip → 删除**其它版本**的官方版 zip。

**发版规则**：

1. **onedir，不要 onefile**：onefile 每次启动解压到 `%TEMP%`，受限机器会报 `Could not create temporary directory!`。
2. 分发**整个 zip**，exe 旁必须有 `_internal\`；只发 exe 起不来。
3. 安装包走 **GitHub Releases**，不进仓库（`dist*/` 已忽略）。
4. 旧 RH 版用 `scripts\build.ps1` + `dist\`，**不要**用新脚本覆盖。

**SmartScreen**：未签名 exe 从网络下载后会被拦。内部先用「更多信息 → 仍要运行」或包内 `解除锁定并启动.bat`；对外分发必须买代码签名证书。详见 `docs\distribution-signing.md`。

### 6.5 COS 运维

| 项 | 值 |
| --- | --- |
| 模板 | `video-input-1463907213/input/tpl/NNN.mp4`（公有读） |
| 清单 | `video-input-1463907213/manifest.json`（公有读） |
| 服装图源 | `video-output-1463907213/prod/output/source/<日期>/...`（签 7 天；不是成品） |
| 成片 | `video-output-1463907213/prod/output/<日期>/<批次>/<编号>-<指纹8>.mp4` |
| 归档失败 | 只写 `archive_error`，**不算任务失败** |

**CAM 关键**：策略必须写 `prod/output/*`。只写 `output/*` 会让归档 403 **静默失败**（异常被 `_archive()` 吞掉）。CAM 改版有 **1–2 分钟生效延迟**。

**生命周期**（控制台 → 桶 → 基础配置 → 生命周期）：

| 规则 | 前缀 | 动作 |
| --- | --- | --- |
| 成品降冷 | `output/` | 30 天转低频、90 天转归档、365 天删除 |
| 源图清理 | `output/source/` | **30 天删除**（签 7 天足够跑完一批） |
| 分片清理 | — | 未完成分片 3 天清理 |

**自检与补传**：

```powershell
python3 scripts\cos_check.py                          # 写/签/读/存在/列举/删
python3 scripts\archive_to_cos.py --root out --dry-run
python3 scripts\archive_to_cos.py --root out          # 历史成片补传，回写 cos_key
```

**成本**：按 15 MB/条、13,000 条/月估算，COS 月费约 ¥190–260，占总成本 **<5%**，优化优先级低。真实生成单价需实测（见 §9.2）。

### 6.6 回滚

| 想做的事 | 做法 | 影响 |
| --- | --- | --- |
| 新版回到 RunningHub | 改 `server\config.json`：`wan.provider="runninghub"`、填 `rh_api_key` → `deploy` → 客户端重启 | 不用重打包；旧 task_id 不通用，别续跑旧批次 |
| 直接退回旧 RH 版 | 用 `dist\换装批量生产器-v0.9-20261026.zip`；旧缓存 `%APPDATA%\RHOutfitTool\` 未被新版读写 | 两版可共存 |
| 撤销某个授权 | 密码管理器「停用」→ 自动同步 | 该机器下次启动被拒 |
| 收回全部授权 | 换 `LICENSE_SECRET` + `deploy` | **全员重新激活**，慎用 |
| 回滚服务端代码 | `git log` 找版本 → `git checkout <sha> -- server\` → `deploy` | — |
| 回滚客户端代码 | `git revert` 新分支提交；**不要把新分支 merge 回旧分支** | 旧版不受影响 |

### 6.7 双版本隔离（旧 RH 版 / 新官方版）

| | 旧 RH 版 | 新官方版 |
| --- | --- | --- |
| 分支 | `feature/prompt-library`（`b09cf4e`） | `feature/aliyun-wan3-official` |
| 代码位置 | `.worktrees\rh-legacy\` | 当前工作区 |
| 打包脚本 | `scripts\build.ps1` | `scripts\build-official.ps1` |
| exe 名 | `换装批量生产器.exe` | `换装批量生产器-官方版.exe` |
| 安装包 | `dist\换装批量生产器-v0.9-20261026.zip` | `dist-official\换装批量生产器-官方版-v0.9-20261028.zip` |
| 本机缓存 | `%APPDATA%\RHOutfitTool\license.json` | `%APPDATA%\RHOutfitToolWan3\license.json` |
| 服务端 Key | `rh_api_key` | `wan.api_key` |
| 生成接口 | RunningHub `reference-to-video` | 百炼 `wan3.0-video` |

> 新版第一次启动要**重新输一次密码**：它用独立缓存，不读旧版的票。这是隔离的预期行为，不是授权失效。

---

## 7. 日常使用

### 7.1 桌面端（操作员）

1. 拿整个 zip 解压到**可写目录**（不要在压缩包里直接双击）。
2. 双击 `换装批量生产器-官方版.exe`。
3. 输入管理员分配的密码（错 3 次退出）。
4. 顶部四个状态灯应为：

| 灯 | 期望 | 不对时看 |
| --- | --- | --- |
| 配置 | `已下发` | §6.1，服务端 `wan.api_key` |
| 模板清单 | `108 个`（补到 500 后为 500） | `manifest_url` 是否可打开 |
| 授权 | `至 2026-xx-xx` | §6.2，密码是否到期/停用 |
| COS | `开` | §6.5，`cos.enabled` |

5. 选一次输出目录（会记住）。
6. 「小批量试跑」：选无头模特图 → 填批次号 → 点「试跑 3 条」。
7. 「大批量生产」：第 1–5 组（001–500）或「随机 100 条」；并发默认 3。

**产物结构**：

```
输出根\批次号\
├── 001.mp4 … 100.mp4
├── report.csv     ← 状态/模式/耗时/计费秒数/输出时长/分辨率/预估费用/大小/实测时长/错误/对象Key/request_id
├── batch.json     ← 提示词原文 + provider/model/mode + 每条 task_id/usage
└── log.txt
```

**状态含义**：

| 状态 | 含义 | 怎么办 |
| --- | --- | --- |
| 运行中 | 已提交，等出片 | 等；超过 `poll_timeout` 会保留 task_id |
| 完成 | 下载并校验成功 | 看 mp4 / report.csv |
| 失败 | 提交/生成/下载失败 | 看备注里的错误与 `request_id` |
| 已过期 | task_id 超 24h 或结果链接过期 | 重跑（重新计费） |
| 已取消 | 官方任务被取消 | 重跑 |
| 旧任务 | 旧 RH 的 taskId，官方接口查不了 | 重跑 |
| 校验失败 | 提交前校验没过 | 按备注改素材/任务 |
| 跳过 | 批次被停止 | 重新跑 |

**并发**：界面可选 3/10/50/100，但会被服务端 `wan.max_concurrency` 钳制（当前 3）。拉满前先看 429 与失败率。

### 7.2 命令行（开发/补跑/对账）

| 命令 | 作用 |
| --- | --- |
| `python3 wan3_r2v.py validate --tasks batch\tasks.batch.json` | 只校验，不发请求 |
| `python3 wan3_r2v.py build --tasks ... --index 0` | 打印第 1 条真实请求体 |
| `python3 wan3_r2v.py emit --tasks ... --out batch\requests.jsonl` | 导出 100 个请求原文（可审计） |
| `python3 wan3_r2v.py run --tasks ... --limit 1` | 打样 1 条（**真实扣费**） |
| `python3 wan3_r2v.py run --tasks ... --concurrency 3` | 全量 |
| `python3 wan3_r2v.py upload assets\定妆图.png` | 上传素材到 COS，返回 URL |
| `python3 wan3_r2v.py query <task_id>` | 复查某个任务 |
| `python3 scripts\cos_check.py` | COS 连通性自检 |
| `python3 scripts\archive_to_cos.py --root out` | 本机成片补传 COS |

**环境变量**（CLI 用；桌面版不需要）：

```powershell
$env:WAN_API_KEY="sk-..."            # 优先；也认 DASHSCOPE_API_KEY
$env:WAN_BASE_URL="https://dashscope.aliyuncs.com/api/v1"
$env:WAN_MODEL="wan3.0-video"

$env:COS_ENABLED="true"
$env:COS_BUCKET="video-output-1463907213"
$env:COS_REGION="ap-guangzhou"
$env:COS_PREFIX="prod"
$env:COS_SECRET_ID="AKID..."
$env:COS_SECRET_KEY="..."
```

**两种模式（别混）**：

| 模式 | 用什么 | 默认参数 | 适合 |
| --- | --- | --- | --- |
| `edit` | 模板视频 + 服装图 | `ratio=adaptive`、`duration=-1` | 换装正式生产（保脸/动作/背景） |
| `reference` | 服装图（可选参考视频） | `ratio=9:16`、`duration=7` | 没模板，或纯参考生成 |

> 桌面版默认 `edit`；CLI 的 `batch\tasks.batch.json` 只有服装图，默认 `reference`。
> 直接 `--mode edit` 会报「edit 需要至少一段参考视频」——要跑 edit 得自己准备带 `videoUrls` 的任务（见 `docs\usage-aliyun-wan3.md` §5.3）。

**提示词三条铁律**：

1. 换装不换人靠 prompt：`保持脸型、五官、发型与妆造完全不变，只更换服装`，且必须出现「图1」「视频1」。
2. 批量务必 `prompt_extend: false`，否则 100 条风格会漂。
3. `duration` 是**整数**：`7` 或 `-1`；旧 RH 才是字符串 `"7"`。

**断点续跑**：`out\state.json` / `batch.json` 记录每条 `task_id`；已成功且本地文件存在 → 跳过；`running` 未过 24h → 续查不重提；超 24h / `UNKNOWN` → 标 `expired`，需显式重跑。

---

## 8. 测试与验收

### 8.1 离线测试（不花钱）

```powershell
python3 tests\test_wan_api.py
python3 tests\test_storage.py
python3 tests\test_core.py
python3 tests\test_remote_config.py
python3 tests\test_prompts.py
python3 tests\test_license.py
$env:QT_QPA_PLATFORM="offscreen"; python3 tests\test_ui.py
python3 demo.py --count 5
python3 demo.py --count 100
```

**最近一次结果（2026-10-28 11:00，本机离屏环境）**：7 套**全部 exit=0**。

```
test_wan_api        OK 官方适配层通过
test_storage        OK 全部通过（未联网、未使用真实 COS）
test_core           OK 核心引擎通过（3/3 成功，归档 3 条）
test_remote_config  OK 远程配置链路通过（缓存 dpapi: 前缀，含明文 rh-key: False）
test_prompts        OK 提示词库全部通过
test_license        全部通过
test_ui             OK 界面集成通过（3 条 mp4 / csv / json / log）
```

### 8.2 真实打样（花钱，先 1 条）

```powershell
# 桌面版：小批量试跑 3 条
# 或 CLI：
python3 wan3_r2v.py run --tasks my-edit-tasks.json --limit 1 --mode edit
```

**打样必须记录**（填进 §9.3 的真实任务表）：

- `request_id`、`task_id`、`status`、`error`；
- `usage.duration` / `input_video_duration` / `output_video_duration` / `SR` / `ratio`；
- 实际耗时、文件大小、成片观感（脸/动作/背景/服装还原）；
- 是否归档成功、`cos_key`。

**看什么**：

| 项 | edit 期望 | reference 期望 |
| --- | --- | --- |
| 脸/动作/背景 | 基本保持模板 | 可能重新生成 |
| 服装还原 | 版型/颜色/材质一致 | 看参考图 |
| 时长 | 跟随模板 | 默认 7s |
| 计费秒数 | ≈ 输入 + 输出（7+7 ≈ 14s） | 有参考视频才叠加 |

### 8.3 上线验收清单（Day-0）

- [ ] 百炼已开通，目标地域有 `wan3.0-video`；
- [ ] 专用业务空间 + 工作空间级 Key，配了消费限额/告警；
- [ ] `server\config.json` 的 `wan` 填好，`config` 自检通过，`deploy` 后线上能看到；
- [ ] `default_mode` 与 `default_duration`/`default_ratio` 匹配（edit=`-1`/`adaptive`，reference=`7`/`9:16`）；
- [ ] 模板清单可打开，模板 URL 公网可下载且能播放；
- [ ] COS 子账号能写 `prod/output/*`，`cos_check.py` 全绿；
- [ ] 1 条真实任务成功，记录 `request_id`/`usage`；
- [ ] edit vs reference A/B，确定 `default_mode`；
- [ ] 10 条小批，观察 429/审核/失败率；
- [ ] 100 条全量，核对 `report.csv` 与归档；
- [ ] 桌面端在**干净机器**（清 `~/.cos` 与环境变量）激活、出片；
- [ ] 验收后再把 `APP_VERSION` 从 `0.9` 升到 `1.0`。

---

## 9. 现状与证据（截至 2026-10-28 11:00）

### 9.1 已验证

| 项 | 证据 |
| --- | --- |
| 官方协议适配 | `tests\test_wan_api.py` 通过：体映射/约束/错误/计费/源图上传 |
| 离线全链路 | `demo.py --count 5/100`；`test_core` 3 条成功 + 归档 3 条 |
| 单元/集成测试 | 7 套全部 exit=0（见 §8.1） |
| 授权服务 | 云函数 `outfit-license` Active；`/health`、`/activate`、`/verify`、错密码、换机 5 项通过（最后一次线上核验 2026-10-26） |
| 模板清单 | 已发布 **108 条**：`https://video-input-1463907213.cos.ap-guangzhou.myqcloud.com/manifest.json` |
| **真实出片** | **1 条已通**（明细见 §9.2） |
| 官方拉取 COS 素材 | 模板（公有 URL）+ 服装图（签名 URL）均被官方成功拉取，**不需要迁 OSS** |
| COS 归档 | 真实对象已写入 `video-output-1463907213/prod/output/...` |
| 打包 | 官方版 onedir zip 62.6 MB（2026-10-28 10:36） |

### 9.2 真实任务明细（可作为后续对账基线）

| 项 | 值 |
| --- | --- |
| 时间 | 2026-10-28 10:19:53 起，墙钟 215.7 s |
| 模板 | 080（输入视频 8.32 s） |
| 模式 | `edit` |
| 分辨率 / 输出时长 | **480P / 5 s**（不是 720P/7s） |
| `task_id` | `e8b9cec7-0e0c-45ef-b719-4c659ccb3586` |
| `request_id` | `41ed112a-43f2-93a9-9dea-0690f1d72884` |
| 计费秒数 | **13.32**（输入 8.32 + 输出 5） |
| 成品大小 / 实测时长 | 2812 KB / 5.0 s |
| COS Key | `prod/output/2026-10-28/real5s-080/080-0c079bb9.mp4` |
| 本地路径 | `out\REAL5S-080-2\080.mp4`（含 `batch.json` / `report.csv`） |

> 这条证明的是**链路通**（提交→生成→下载→校验→归档），**不代表 720P/7s 的效果与单价已验收**。

### 9.3 未验证 / 待做

| 项 | 状态 | 怎么补 |
| --- | --- | --- |
| 线上 `wan.api_key` 是否已下发 | ❓ **未复核**（本机断网 + 部署包停在 10-26） | 联网跑 `license_deploy.py config`，把结果填这里 |
| 桌面端实机（干净机器） | ⬜ | 解压官方版 zip → 输密码 → 看四灯 → 跑 3 条 |
| 720P / 7s 真实效果与单价 | ⬜ | 桌面小批量 3 条，记 `usage.duration` 与观感 |
| edit vs reference A/B | ⬜ | 同模板同服装各 1 条，定 `default_mode` |
| 50/100 并发限流 | ⬜ | 阶梯实测 3→10→50，观察 429 |
| 500 模板真实拉取 | ⬜ | 补素材后跑 3 条观察 |
| 代码签名 | ⬜ | 买证书，`-Sign` 打包 |
| 自动更新 / 历史批次界面 | ⬜ | 见 §11 P2 |

### 9.4 本次核对发现的不一致（接手后先修文档/状态）

1. **README 状态行过时**：`README.md` 写「只差填 `wan.api_key`」，但本机 `server\config.json` 已在 09:46 填入；README 未更新。以 `config` 命令为准。
2. **部署包陈旧**：`dist\license-deploy.zip` 停在 2026-10-26 11:31，早于 Key 填入时间 → **大概率尚未 `deploy`**。这解释了为什么 README 仍认为「Key 空」。
3. **代码未推送**：相对 `origin/main` 共 **15 个提交未推送**（`feature/aliyun-wan3-official` 比 `main` 多 13 个，`main` 自己比 `origin/main` 多 2 个）。GitHub 上看到的仍是旧代码，Releases 里也没有官方版包。
4. **`docs\project-overview.md` §12 待办**部分已过时（把「配 Key」列为待办），读时以本文 §9/§11 为准。
5. **旧文档里的免授权开关不存在**：`license-integration.md` §3B、`license-handoff.md`、`license-setup.md`、`project-overview.md` 都写「`RH_DEV_NO_LICENSE=1` 可跳过校验 / 未配服务器直接放行」；现行 `app/startup.py` **没有这个分支**，`server` 为空直接 fail-closed。以代码为准，后任不要再照抄。
6. **交付时工作区有未提交改动**：`app/runtime.py` 新增，`app/config.py` / `core.py` / `main.py` 有重构（2026-10-28 11:35–11:36），本文 §4 只反映到模块级。接手后先 `git diff` 看清，再跑 §8.1 测试。

---

## 10. 风险与红线

### 10.1 风险清单

| 级别 | 风险 | 后果 | 缓解 |
| --- | --- | --- | --- |
| **P0** | 代码只在单机、未推送 | 硬盘坏/离职 = 项目丢失 | 立刻 push 分支 + tag + 传 Release |
| **P0** | 线上配置状态不明 | 客户端「配置：未下发」，跑不了 | 联网 `config` 复核，必要时 `deploy` |
| **P0** | 百炼 Key 在客户机可解 | 余额可被刷 | 专用业务空间 + 消费限额；离职/异常时轮换 |
| **P0** | 授权密码曾以明文进 Git 历史 | 旧密码可能仍有效 | **轮换这 3 条密码**，旧票随 `/verify` 失效 |
| P1 | 模板仅 108/500 | 第 2–5 组不可用 | 补素材；或改动态分组 |
| P1 | 结果链接 / `task_id` 仅 24h | 过期不可恢复，重跑要再付费 | 成功即下载；及时归档 |
| P1 | 并发上限未知 | 拉满可能 429 | 3 起，阶梯实测 |
| P1 | 单条失败无自动重试 | 需手动补跑 | `report.csv` 列失败项；后续加重试（§11 P1） |
| P1 | 未签名 exe 被 SmartScreen 拦 | 客户首启受阻 | 去 MOTW / 买签名证书 |
| P2 | DPAPI 不是安全边界 | 同用户进程可解密 | 已接受（ADR-001）；不对外分发 |
| P2 | 无自动更新 | 换程序要重新发 exe | 换 Key/模板不需要发版；程序升级手动 |
| P2 | 素材版权/肖像权 | 合规风险 | 确认模板与服装图授权范围 |

### 10.2 红线（违反会造成不可逆损失）

1. **密钥不进 Git、不进对话、不进 exe、不进日志**。`server\config.json`、`server\passwords.json` 已在 `.gitignore`。
2. **不改 `LICENSE_SECRET`**：一改全员失效。
3. **不改协议字段名**：`password` / `machine` / `token` / `action` / `config` / `wan` / `rh_api_key` / `manifest_url` / `cos`；token 结构 `p`/`m`/`e` 也不能改。
4. **不用 `--onefile`**：受限机器起不来。
5. **不覆盖旧 RH 版的 zip/exe**；不把新分支 merge 回旧分支。
6. **不手改生成的产物**：`batch\tasks.batch.json` 改了要重新生成；`build\manifest\manifest.json` 用工具生成。
7. **提交失败不盲目重试**：at-most-once，重试可能重复建任务/重复扣费。
8. **不先跑 100 条**：必须先 1 条 → 3 条 → 10 条 → 100 条。
9. **不删 `server\passwords.json`**：要停权用「停用」，不要删除（删除不可恢复）。

---

## 11. 待办与建议路线

### P0（1–2 天内，不做会丢东西/跑不起来）

| # | 任务 | 验收标准 |
| --- | --- | --- |
| 1 | 推送代码：`git push -u origin feature/aliyun-wan3-official`，并推 `main`；打 tag `v0.9-wan3` | `git branch -vv` 显示上游；GitHub 能看到 15 个提交 |
| 2 | 联网复核线上配置：`license_deploy.py config`；若 Key 未下发则 `deploy` | 输出「已下发」「配置完整」 |
| 3 | 桌面端实机：干净机器解压官方版 zip → 输密码 → 四灯全绿 → 跑 3 条 | 出 3 条 mp4 + `report.csv`；记录 `request_id` |
| 4 | 轮换 3 条曾泄露的密码（旧票随 `/verify` 失效） | 旧密码 `check --expect reject` 通过；新密码可用 |
| 5 | 移交 `assets\定妆图.jpg`、`out\REAL5S-080-2\`、`server\config.json`、`server\passwords.json` | 接手人本机可复现 1 条 |

### P1（1 周内，决定能不能量产）

| # | 任务 | 验收标准 |
| --- | --- | --- |
| 6 | 720P/7s 打样 + edit/reference A/B | 记录效果、耗时、`usage.duration`；定 `default_mode` |
| 7 | 模板补到 500（上传 + 发布 + 刷新客户端） | 四灯显示 500；第 5 组可跑 |
| 8 | 10 条 / 100 条压测 | 失败率 <5%，无持续 429；报表与归档对得上 |
| 9 | 失败自动重试 1–2 次 + 一键补跑 | 单条网络抖动不再需要人工改 JSON |
| 10 | 填 `wan.price_per_second` | `report.csv` 能出「预估费用」，与阿里云账单口径一致 |
| 11 | 代码签名 / 分发方案 | 外部客户首启不弹 SmartScreen（或明确接受点一次） |

### P2（可选，提升体验）

| # | 任务 | 价值 |
| --- | --- | --- |
| 12 | 分组按清单实际条数动态算（`TOTAL_TEMPLATES` 写死 500） | 模板不足时不再出现点不了的组 |
| 13 | 历史批次列表（读 `batch.json`） | 不用翻文件夹 |
| 14 | `transfer.py` 接入主流程（边下边传） | 省本地磁盘 |
| 15 | 自动更新 / 版本检查 | 换程序不用逐个发 zip |
| 16 | 服务端预签名上传（替代下发 COS 密钥） | 去掉客户端长期密钥（ADR-001 §7） |

---

## 12. 故障速查

### 12.1 授权与配置

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 「配置：未下发」/「缺少 API Key」 | 服务端 `wan.api_key` 空，或客户端没重启 | §6.1 填 Key → `deploy` → 重启客户端 |
| 「模板清单：未下发 / 加载失败」 | `manifest_url` 空 / 打不开 / JSON 错 | 浏览器打开清单；`publish_manifest.py --verify-only` |
| 「未配置授权服务」并退出 | 内置 `SERVICE_URL` 被清空 | 恢复 `app/config.py:18` → 重新打包 |
| 「密码不存在」 | 输错 / 未同步到云 | 密码管理器核对；「立即同步」 |
| 「授权已到期」 | `expires` 过了 | 续期 |
| 「密码已被停用」 | `revoked=true` | 恢复或换新密码 |
| 「需要联网核验一次（已超过 7 天）」 | 长期离线 | 联网；查函数是否在跑 |
| 「token 签名不对」 | 换过 `LICENSE_SECRET` | 预期：重新输密码 |
| 一直连接失败 | 函数 URL 变了 / 网络不通 | `license_deploy.py url` 核对 |

### 12.2 生成与素材

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 401/403 `InvalidApiKey`/`AccessDenied` | Key 错、地域/业务空间不匹配、Key 被禁 | 核对 Key、`base_url`、地域 |
| `Arrearage` / `BudgetLimitExceeded` | 欠费 / 预算用尽 | 充值或提高预算 |
| 429 / `Request rate increased too quickly` | 提交或查询太快 | 降并发（3 起）、拉长 `poll_interval`、退避 |
| 400 `InvalidParameter` | `duration` 非整数、`ratio` 不在枚举、素材超限 | 按报错字段改；`duration` 用 `7`/`-1` |
| `DataInspectionFailed` | 素材/提示词未过审核 | 换素材或提示词；确认授权 |
| `GatewayTimeout.InputDownload` / `Failed to download multimodal content` | 个别 URL 不可达/权限/签名问题 | 先查该 URL；服装图可改 Base64；个别模板再单独迁 OSS |
| `任务已过期` | task_id 超 24h / `UNKNOWN` | 显式重跑（重新计费） |
| 下载 403/404 | 结果链接过期 | 重跑；成功即下载 |
| 图片比例/尺寸报错 | 超官方限制（单边 240–8000 px，比例 ≤8:1） | 裁剪/缩放 |
| `duration 只能是 -1 或 2–30` | 传了字符串/超范围 | 用整数；`auto` 会映射 `-1` |

### 12.3 存储与打包

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 任务成功但桶里没成品 | CAM 少了 `prod/output/*`；或子账号密钥过期 | 看 `report.csv` 的 `archive_error`；改策略后等 1–2 分钟 |
| 模板 403（模型拉不到） | 单条对象漏设 `public-read` | `upload_templates.py --force` 或补 ACL |
| 清单 403 | 对象没设公有读 / 发布失败 | 重跑 `publish_manifest.py` |
| 客户端「COS：关」 | `cos.enabled=false` | 需要归档就打开 |
| 双击 exe 被 SmartScreen 拦 | 未签名 + 带 MOTW | 点「更多信息 → 仍要运行」，或跑 `解除锁定并启动.bat`；根治靠签名 |
| 双击 exe 没反应/闪退 | 只拷了 exe，缺 `_internal\` | 发整个 zip |
| PyInstaller 打包失败 | exe 被占用 / 依赖缺失 | 先关旧实例；看 `dist-official\build.log` |
| `deploy` 报 `tccli 调用失败` | 未登录 / 无权限 / 沙箱写日志失败 | `tccli auth login --profile agent1`；换正常终端 |
| 客户端没拿到新配置 | 没重启 / 没 deploy / 票过期 | 关掉重开；`config` 复核 |

**排错时优先提供**：`request_id`、`task_id`、`status`、`error`、`usage`、客户端 `log.txt`。官方工单基本都问 `request_id`。

---

## 13. 交接执行清单

### 13.1 Day 1（半天）

- [ ] 拿到仓库访问权限；**从本机整目录拷贝**（分支未推送，`git clone` 拿不到），或等 P0-1 推送后再 clone
- [ ] 拿到 `server\config.json`、`server\passwords.json`、`assets\定妆图.jpg`、`out\REAL5S-080-2\`
- [ ] 拿到云资源权限：腾讯云 SCF / COS / CAM、阿里云百炼、GitHub
- [ ] 跑通 `python3 demo.py --count 5`
- [ ] 7 套离线测试全绿
- [ ] 读本文 §0/§3/§6 + `docs\usage-aliyun-wan3.md`
- [ ] 在一台**能联网**的机器上跑 `license_deploy.py config`，把真实结果写进 §9.3

### 13.2 第 2–3 天

- [ ] 推送代码 + tag + 建 Release（P0-1）
- [ ] 复核 / 完成服务端 `deploy`，客户端四灯全绿
- [ ] 桌面端干净机器跑 3 条，记录 `request_id`、`usage`、观感
- [ ] 轮换历史泄露密码（P0-4）
- [ ] 用 `dist-official` 的包在另一台机器复现一次

### 13.3 第 1 周

- [ ] 720P/7s 打样 + edit/reference A/B，定 `default_mode`
- [ ] 模板补到 500 并发布
- [ ] 10 条 / 100 条压测，失败率与归档核对
- [ ] 把 §9.3 的 ❓/⬜ 逐项改成 ✅ 或留下明确阻塞说明
- [ ] 更新 `README.md` 状态表与本文 §9

### 13.4 签字确认

| 交接项 | 移交人 | 接手人 | 日期 |
| --- | --- | --- | --- |
| 代码仓库与分支（含未推送提交） | | | |
| `server\config.json` / `passwords.json` | | | |
| 云函数 `outfit-license` 与 `LICENSE_SECRET` | | | |
| COS 两个桶 + 子账号策略 | | | |
| 百炼业务空间 / API Key / 预算告警 | | | |
| 模板源素材（108 个）与清单 | | | |
| 定妆图、提示词库、真实成片样例 | | | |
| 安装包（新官方版 / 旧 RH 版） | | | |
| 文档与本文 | | | |

> 签字只代表「资产已交付」，不代表「所有待办已完成」。待办以 §11 为准。

---

## 14. 附录

### 14.1 目录树（当前工作区，节选）

```
D:\批量视频任务设计区\
├── app\                    桌面程序（main/core/wan_api/storage/media/license/...）
├── server\                 授权服务（license_api.py + 本地 config/passwords，后者不入库）
├── scripts\                运维脚本（部署/密码/打包/COS 自检/补传）
├── tools\                  模板上传与清单发布
├── tests\                  7 套离线测试 + 假百炼
├── prompts\                提示词库（JSON + raw 原文）
├── docs\                   全部文档（本文在 handover.md）
├── batch\                  旧链路：服装清单 → 100 条任务
├── assets\                 占位图；真实定妆图本地保留、不入库
├── dist\                   旧 RH 版构建产物（v0.9-20261026）
├── dist-official\          新官方版构建产物（v0.9-20261028）
├── build\                  manifest、临时构建、历史 release 备份
├── out\                    本地成片与状态（demo / REAL5S-080*）
├── .worktrees\rh-legacy\   旧 RH 版独立检出（gitignore）
├── wan3_r2v.py             CLI 批量客户端
├── make_batch.py           清单 → 任务
├── demo.py                 离线演练
└── run_app.py              PyInstaller 入口
```

### 14.2 命令总表

| 场景 | 命令 |
| --- | --- |
| 离线演练 | `python3 demo.py --count 5` |
| 全部离线测试 | 见 §8.1 |
| 看配置是否填全/下发 | `python3 scripts\license_deploy.py config` |
| 部署服务端 | `python3 scripts\license_deploy.py deploy` |
| 线上 5 项验收 | `python3 scripts\license_deploy.py test` |
| 服务状态 | `python3 scripts\license_deploy.py status` |
| 函数 URL | `python3 scripts\license_deploy.py url` |
| 加密码 | `python3 scripts\license_passwords.py add --note "..."` |
| 停用密码 | `python3 scripts\license_passwords.py revoke <CODE>` |
| 验某密码 | `python3 scripts\license_deploy.py check <CODE>` |
| 上传模板 | `python3 tools\upload_templates.py` |
| 发布清单 | `python3 tools\publish_manifest.py` |
| 打官方版包 | `powershell -ExecutionPolicy Bypass -File scripts\build-official.ps1` |
| 打旧 RH 版包 | `powershell -ExecutionPolicy Bypass -File scripts\build.ps1` |
| COS 自检 | `python3 scripts\cos_check.py` |
| 成片补传 | `python3 scripts\archive_to_cos.py --root out` |
| CLI 打样 | `python3 wan3_r2v.py run --tasks <file> --limit 1` |
| CLI 全量 | `python3 wan3_r2v.py run --tasks <file> --concurrency 3` |

### 14.3 官方请求体（edit，桌面默认）

```json
{
  "model": "wan3.0-video",
  "input": {
    "prompt": "编辑视频1：保持面部/动作/背景不变，把服装替换为图1这套",
    "media": [
      {"type": "reference_video", "url": "https://.../input/tpl/001.mp4"},
      {"type": "reference_image", "url": "https://.../prod/output/source/...png?sign=..."}
    ]
  },
  "parameters": {
    "resolution": "720P",
    "ratio": "adaptive",
    "duration": -1,
    "audio": false,
    "prompt_extend": false,
    "watermark": false
  }
}
```

约束：单请求最多 10 图 / 5 视频 / 5 音频；单段视频 1–15 s，总时长 ≤15 s；输入+输出 ≤30 s；`duration` 为 `-1` 或 2–30 整数；`prompt` ≤20000 字符。完整字段表见 `docs\wan-3.0-reference-to-video.md`。

### 14.4 术语

| 术语 | 含义 |
| --- | --- |
| 模板 / 参考视频 | 7 秒动作视频，提供人物、动作、背景；换装时作为「待编辑视频」 |
| 定妆图 / 服装图 | 无头模特图，只提供服装与上身效果；脸由模板视频提供 |
| edit 模式 | 给模板视频换衣服（`reference_video` + `reference_image`） |
| reference 模式 | 纯参考生成（主要靠服装图） |
| 清单 / manifest | 模板列表 JSON，客户端每次启动重拉 |
| 票据 / token | 授权服务签发的 HMAC token，绑机器，默认随密码到期 |
| 计费秒数 | `usage.duration`，含输入参考视频；不是输出时长 |
| 归档 | 成品从百炼 24h 链接转存到 COS 长期地址 |

### 14.5 文档索引

| 文档 | 用途 | 状态 |
| --- | --- | --- |
| **本文 `docs\handover.md`** | **交接总入口** | ✅ 最新 |
| `README.md` | 项目门面、三条命令上手 | ⚠️ 状态表待更新（§9.4） |
| `docs\usage-aliyun-wan3.md` | 使用手册：配置/桌面/CLI/存储/排错/回滚 | ✅ 现行 |
| `docs\wan-3.0-reference-to-video.md` | 官方接口规格：字段/约束/状态机/计费 | ✅ 现行 |
| `docs\adr-002-aliyun-wan3-official-api.md` | 切官方 API 的决策、风险、回滚 | ✅ 现行 |
| `docs\adr-001-cos-client-credential.md` | 客户端持有 COS 密钥的风险与边界 | ✅ 现行 |
| `docs\license-integration.md` | 客户端 ↔ 授权服务对接、验收、排错 | 🟡 主体现行，§3B 旧说法见 §9.4-5 |
| `docs\deployment-spec.md` | 服务端单次施工书（改/部署/回验） | ✅ 现行 |
| `docs\license-handoff.md` | 授权体系给执行方的完整交接 | 🟡 主体现行，免授权说法见 §9.4-5 |
| `docs\license-setup.md` | 授权方案演进记录 | 🟡 历史参考 |
| `docs\password-manager.md` | 密码管理器搭建与使用 | ✅ 现行 |
| `docs\prompt-library.md` | 提示词库说明 | ✅ 现行 |
| `docs\remote-config-design.md` | 配置下发设计稿 | 🟡 已被实现取代 |
| `docs\templates-500.md` | 模板上传/清单/发布 | ✅ 现行 |
| `docs\cos-setup.md` | COS 建桶/CAM/生命周期/费用 | ✅ 现行 |
| `docs\version-isolation.md` | 旧 RH 版 / 新官方版隔离 | ✅ 现行 |
| `docs\distribution-signing.md` | SmartScreen 与代码签名 | ✅ 现行 |
| `docs\project-overview.md` | 架构总览 | 🟡 部分待办过时 |
| `docs\project-report-20261026.md` | 历史状态报告（RH 时代） | 🟡 历史参考 |
| `batch\README.md` | 旧链路：清单 → 100 条任务 | 🟡 旧链路 |
| `tests\README.md` | 离线联调与测试套件 | ✅ 现行 |
| `app\README.md` | 客户端使用说明 | ✅ 现行 |

### 14.6 版本记录

| 版本 | 日期 | 作者 | 变更 |
| --- | --- | --- | --- |
| v1.0 | 2026-10-28 | 项目组 | 初版：资产/凭证位置、双链路架构、运维 Runbook、现状证据、风险红线、待办、交接清单 |

> 更新规则：改代码行为、换云资源、动密钥、完成 §11 任一项，都要回来改本文并在上表加一行。

---

**交接完成后，请把本文 §9.3 的 ❓/⬜ 更新为实测结果，并在 §13.4 签字。**
文档随代码走：改行为就改本文，别让下一任再靠猜。
