# 换装批量生产器 · 项目状态报告

**日期**：2026-10-26　**版本**：v0.9　**仓库**：`D:\批量视频任务设计区`
**最近提交**：见 §10 修订记录

> ⚠️ **本文是 11:00 的快照**，当天下午的进展（清单已发布、测试修复、v0.9 打包、文档校正、提交）
> 统一记在 **§10 修订记录**，冲突时以 §10 为准。

---

## 0. 一页速览

**一句话**：授权、配置下发、凭证下发三条链路都已打通并上线，客户端能做到"零配置启动"；
**但 RH Key 还空着、模板清单从未生成**，所以现在还跑不出真实视频。

| 能力 | 状态 | 依据 |
| --- | --- | --- |
| 密码授权（登录门禁） | ✅ 可用 | 云函数 Active，`/activate` `/verify` 已验证 |
| 运行配置下发（Key/清单/COS/公告） | ✅ 通路可用 | `/activate` 返回 `config`，字段齐全 |
| COS 凭证下发（子账号） | ✅ 已上线 | 线上已下发 `secret_id`/`secret_key`，权限边界实测通过 |
| RH Key（跑任务的钱） | ❌ **空** | 客户端显示「配置：未下发」 |
| 模板清单 manifest | ❌ **不存在** | 全仓无 `manifest.json`；`manifest_url` 指向的域名 502 |
| 分发包 | ⚠️ 落后一版 | dist 里最新是 v0.8，代码已是 v0.9 |

---

## 1. 系统架构

```
                    ┌──────────────── 腾讯云函数 outfit-license（ap-guangzhou）────────────────┐
                    │ license_api.py   /activate  /verify  /config  /health                     │
用户双击 exe ───────►│ passwords.json  （谁能用、到哪天、是否停用）                              │
  │                 │ config.json     （RH Key / 清单地址 / COS 参数与凭证 / 公告）             │
  │                 └───────────────────────────────────────────────────────────────────────────┘
  │  ① 地址内置（app/config.py:18 SERVICE_URL）          ② 输密码换票 + 拿到 config
  │
  ├─► 本地缓存 %APPDATA%\RHOutfitTool\license.json（DPAPI 加密：token / expire_at / config / last_ok）
  │
  ├─► 按 manifest_url 拉清单（无缓存，每次启动拉一次）──► COS: manifest.json
  │
  ├─► 提交任务 ──► RunningHub：videoUrls = 清单里的直链 ──► 模型自己去拉模板视频
  │
  └─► 成品 ──► COS：output/prod/（用下发的子账号密钥写入，失败不影响任务）
```

三方职责很干净：**云函数只管"发牌"**，**COS 只管"存素材和成品"**，**RunningHub 干重活**。

---

## 2. 代码地图

| 模块 | 行数 | 职责 | 关键位置 |
| --- | --- | --- | --- |
| `app/main.py` | 796 | 主窗口、启动门禁、状态灯 | `APP_VERSION:41`、`resolve_license_server:48`、`_make_storage:57`、`build_storages:80`、`ensure_license:786` |
| `app/core.py` | 509 | 批量引擎：读清单、分组、提交、下载、归档 | `load_manifest:54`、`plan_batch:86`、`_prepare_templates:331`、`_archive:430` |
| `app/storage.py` | 662 | COS/本地存储抽象（分片、重试、签名） | `StorageConfig:62`、`from_local_profile:108` |
| `app/license.py` | 250 | 激活、票据缓存、7 天断网救援窗口 | `activate:188`、`status:212` |
| `app/config.py` | 115 | 服务端配置 → 本地 cfg 的映射 | `SERVICE_URL:18`、`load:89` |
| `app/secure_store.py` | 115 | DPAPI 加解密 | — |
| `app/transfer.py` | 324 | 素材/结果搬运 | — |
| `app/startup.py` | 56 | 输密码弹框（3 次失败退出） | — |
| `server/license_api.py` | 314 | 零依赖授权服务 | `issue_token:83`、`verify_token:96`、`load_runtime_config:133`、`main_handler:252` |
| `scripts/license_deploy.py` | 332 | 打包/部署/自检 | `ZIP_FILES`、`deploy`、`config`、`check` |
| `scripts/license_passwords.py` | 243 | 密码表增删改（自动同步） | `default_expires` 默认 31 天 |
| `scripts/password_manager.py` | 387 | 图形化密码管理（PySide6） | 双击 `scripts\密码管理器.bat` |
| `tools/build_manifest.py` | 134 | 模板清单生成器 | 支持 `--mode url/key`（本地改动未提交） |

合计约 **2800 行客户端 + 314 行服务端 + 1360 行脚本**，零第三方服务端依赖。

---

## 3. 已经打通的能力

| # | 能力 | 实现要点 | 验证证据 |
| --- | --- | --- | --- |
| 1 | 密码授权 | HMAC 票据绑机器码；`/verify` 每次回表查到期与停用；7 天断网救援窗口 | `/health` 200、正确密码 200、错密码 401、换机 403 |
| 2 | 单一到期日 | 全系统只有一个时间 = 密码表 `expires`；票期等于它 | 到期后旧票立即 403（实测）；续期自动换新票 |
| 3 | 配置下发 | 客户端无任何可编辑配置，全靠 `/activate` 下发 | 线上返回 `config.manifest_url` / `cos.*` |
| 4 | 凭证下发 | 子账号 `rh-client-uploader`（仅 `output/*` Put/Get/分片） | 5 项权限探针全过；写/读 `output/` 成功，`input/` 与列举被拒 |
| 5 | 密码运维 | 命令行 + 图形界面，改完自动 deploy 并回验 | `list/add/expire/revoke/remove` + 密码管理器 |
| 6 | 发布打包 | `build.ps1` 自动取版本号、生成干净 `配置.ini`（不含 Key） | v0.8 包已产出（v0.9 待打） |

---

## 4. 数据与配置流向

| 数据 | 谁提供 | 存在哪 | 变了怎么生效 |
| --- | --- | --- | --- |
| 服务地址 | 代码常量 | `app/config.py:18` | 改代码 → 重新打包（没有别的路） |
| RH Key / 清单地址 / COS 参数与凭证 | 服务端 `config.json` | 云函数 + 客户端 `license.json`（DPAPI） | 改文件 → `deploy` → 客户端下次启动 |
| 票据 | 服务端签发 | 同上 | 自动；续期时服务端顺手换新票 |
| 模板清单 | **尚未存在** | 计划放 COS | 覆盖写同名对象，客户端每次启动重拉 |
| 成品视频 | 客户端上传 | COS `output/prod/` | 实时 |

---

## 5. 质量状态

**测试 5 个文件全部通过**（脚本式，直接 `python tests/xxx.py`）：

```
test_license.py        → 全部通过（11 项授权用例）
test_core.py           → OK 核心引擎通过
test_storage.py        → OK 全部通过（未联网、未用真实 COS）
test_remote_config.py  → OK 远程配置链路通过
test_ui.py             → OK 界面集成通过
```

**未提交改动 6 个文件**（都还没进 Git）：

```
M app/config.py               ← 凭证映射（COS_SECRET_ID/KEY、双桶）
M app/main.py                 ← _make_storage 用下发凭证；版本 0.9
M server/license_api.py       ← 单一到期日 + 自动换票 + 时区修正
M scripts/license_deploy.py   ← config.json 进必需打包清单 + config 自检命令
M docs/license-integration.md ← 对接文档更新
M tools/build_manifest.py     ← 清单生成器改造（未完成）
```

**dist 目录有历史残留**：`换装批量生产器-v0.6-20261024/`（目录）、`换装批量生产器/`（旧目录）、`换装批量生产器-v0.8-20261024.zip`。

---

## 6. 阻塞与风险

### P0 · 现在就跑不了

| # | 问题 | 影响 | 解法 |
| --- | --- | --- | --- |
| 1 | `server/config.json` 的 `rh_api_key` 为空 | 客户端「配置：未下发」，提交必然失败 | 你在本地填 32 位 Key → 我 `deploy` |
| 2 | 模板清单从未生成；`manifest_url` 指向 `cdn.ap-shanghai.tencentcos.cn`（实测连接层 502） | 「模板清单：加载失败」，一条都跑不了 | 换桶默认域名 + 生成并发布清单（方案已写，暂缓中） |

### P1 · 会踩但能绕

| # | 问题 | 说明 |
| --- | --- | --- |
| 3 | 双桶已实现但未启用 | 代码支持 `cos.template_bucket` / `cos.output_bucket`，`config.json` 里没配 → 都回退 `COS_BUCKET` |
| 4 | v0.9 未打包 | dist 里是 v0.8；`build.ps1` 会自动删其它版本 zip，注意备份 |
| 5 | `D:\模板` 不存在 | 文档里写的路径是占位；现有候选素材只有 108 个 |

### P2 · 治理类

| # | 问题 | 建议 |
| --- | --- | --- |
| 6 | 密码 003 设为**永久**且已投用 | 永不到期 = 无法用"到期"收口；建议改成明确日期 |
| 7 | `docs/templates-500.md` 推荐"私有桶 + key 模式" | 与现行"公有直链"方案冲突，误导后续运维，需修订 |
| 8 | 子账号 `agent1` 仍是 AdministratorAccess；登录密码曾在对话中出现 | 之前说先 pass，风险仍在 |
| 9 | 6 个文件的改动未提交 Git | 建议尽快提交，否则换机器/回滚都没依据 |

---

## 7. 日常运维入口

| 想做的事 | 命令 |
| --- | --- |
| 加/改/删密码 | `python3 scripts\license_passwords.py add/expire/revoke/remove`（自动同步云） |
| 图形化管理密码 | 双击 `scripts\密码管理器.bat` |
| 部署服务端 | `python3 scripts\license_deploy.py deploy` |
| 看配置齐不齐 | `python3 scripts\license_deploy.py config` |
| 看服务活没活 | `python3 scripts\license_deploy.py status` |
| 线上验收 5 项 | `python3 scripts\license_deploy.py test` |
| 打客户端包 | `scripts\build.bat` |

---

## 8. 文档索引

| 文档 | 定位 | 是否现行 |
| --- | --- | --- |
| `docs/license-integration.md` | **对接总纲**：地址/配置/凭证/协议/验收/排错 | ✅ 最新（含 §10 凭证下发） |
| `docs/deployment-spec.md` | 单次施工任务书（传什么、验什么） | ✅ 现行 |
| `docs/license-handoff.md` | 给执行方的完整交接（协议、密码运维、GUI） | ✅ 现行 |
| `docs/project-overview.md` | 项目总览与待办 | ⚠️ 部分待办已过时 |
| `docs/templates-500.md` | 模板上 CDN 的老流程 | ❌ 与现行方案冲突 |
| `docs/license-setup.md` | 授权方案演进记录 | 🟡 历史参考 |
| `docs/remote-config-design.md` | 配置下发设计 | 🟡 已被实现取代 |
| `docs/cos-setup.md` | COS 桶/CAM/生命周期 | 🟡 注意 `input/` 7 天删除规则 |

---

## 9. 建议的下一步（按顺序）

1. **填 `rh_api_key`**（你操作，1 分钟）→ 我 `deploy` + `config` 自检 → 客户端「配置：已下发」
2. **修 `manifest_url` + 出第一版清单**：换桶默认域名 → 用现有 108 个素材先跑通（数量不足时只能用"随机 100 条"）
3. **打包 v0.9 并在一台干净机器验收**：清 `~/.cos` 与环境变量 → 点开始不弹「COS 配置有误」→ 成品进 `output/prod/`
4. 收尾治理：提交这 6 个文件、修订 `templates-500.md`、清 dist 里的 v0.6 残留、把永久密码改成明确日期

---

## 10. 修订记录（当天下午，以本节为准）

**2026-10-26 13:50–14:15 完成的事**（对照上文的过时项）：

| 上文结论 | 现在 | 证据 |
| --- | --- | --- |
| P0#2「模板清单从未生成 / manifest_url 502」 | ✅ **已解决** | 生成 108 条 → 发布到 `video-input-1463907213.cos.ap-guangzhou.myqcloud.com/manifest.json`（HTTP 200，模板直链 HEAD 200） |
| P0#2「manifest_url 指向 502 域名」 | ✅ 已换 | `server/config.json` 已改桶默认域名，线上 `/activate` 已下发，与仓库一致 |
| P1#4「v0.9 未打包」 | ✅ 已打 | `dist\换装批量生产器-v0.9-20261026.zip`（41.6 MB）；包内 exe `--selftest` → `v0.9 selftest ok` |
| P2#9「6 个文件未提交」 | ✅ 已提交 | 见本节末尾 |
| 测试「5 套全绿」（靠人工环境） | ✅ 现在**任何受限环境都能跑** | 新增 `tests/_tmpdir.py`；`tempfile.mkdtemp` 的 0o700 显式 DACL 会剥掉继承权限 → 改用 0o777 建目录 |
| 文档索引里标 ❌/⚠️ 的四篇 | ✅ 已校正 | `README.md`、`app/README.md`、`deployment-spec.md`、`templates-500.md`、`project-overview.md` 头部；`license-handoff.md` 加 v0.8 变更横幅 |
| dist v0.6 残留 | ✅ 已删 | `换装批量生产器-v0.6-20261024\`（100 MB / 含废弃 `配置.ini`） |
| v0.8 包 | ✅ 已备份 | `build\release-backup\换装批量生产器-v0.8-20261024.zip`（`build.ps1` 会删 dist 里其它 zip） |

**新发现并处理的安全项**：

- 3 条**真实密码**此前以明文写在 4 篇文档 + `scripts/license_passwords.py` 里，且已进 Git 历史。
  已全部替换为 `RH-2026-00N-xxxx` 占位符（共 16 处），但**Git 历史仍可查到**。
  → **必须轮换**：密码管理器里停用这 3 条并重新分配（旧票会随 `/verify` 回表复查立刻失效）。
- 其中一条 `expires` 为空（= 永久），建议改成明确日期。

**仍然阻塞（唯一）**：`server/config.json` 的 `rh_api_key` 空 → 客户端「配置：未下发」，跑不了真实任务。

**后续建议**：模板补到 500（当前 108，第 2–5 组点不了）；把 `GROUP_SIZE` 分组改成按清单实际条数动态算；
`build_storages` 给模板桶也用了只能写成品桶的子账号（签模板 GET 会 403），走公有直链时无影响但仍是隐患。

**17:55 补充：分发时的 SmartScreen 拦截**

现象：双击 exe 弹「Windows 已保护你的电脑 / 无法识别的应用」，只有「不运行」。
原因不是报毒，是**未签名 + 文件带 MOTW** 的信誉拦截（实测 `Get-AuthenticodeSignature` → `NotSigned`，
且此前 exe **完全没有版本信息**）。

| 已做 | 效果 |
| --- | --- |
| 打包时生成**版本信息资源**（CompanyName / FileDescription / FileVersion，`scripts/make_version_file.py`） | exe 属性面板不再空白，少一类误报理由 |
| 构建加 `--noupx` | 避开 UPX 加壳的误报高发组合 |
| 包内附 `解除锁定并启动.bat` | 收件人双击一次即清 MOTW 再启动 |
| `build.ps1` 新增 `-Sign` / `CODESIGN_THUMBPRINT` | 买到证书后一条命令签名 |

> 注意：这些**不能**让弹窗消失——要"不弹"，只有去 MOTW（内部够用）或**买代码签名证书**（对外必须）。
> 完整对比与操作见 [distribution-signing.md](distribution-signing.md)。
> 另：`scripts/build.ps1` 必须保存为 **UTF-8 with BOM**，否则 Windows PowerShell 5.1 会把中文按 ANSI 解码而报语法错（已写进 `.gitattributes` 注释）。

