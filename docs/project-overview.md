# 换装批量生产器 · 项目总览

> 一句话：**1 张无头模特图 + 100 个模板视频 → 100 条 7 秒 720P 竖屏换装视频**，桌面一键跑完，产物自动落本地并归档到腾讯云 COS，分发给别人时用密码授权。
>
> 仓库：`https://github.com/1157970770/outfit-batch-tool`　当前版本：**v0.1 测试版**
> 最近一次全量测试：4 个测试套件全部通过（见 §8）

---

## 1. 业务场景

| 角色 | 动作 |
| --- | --- |
| 操作员 | 选一张无头模特图（只提供服装+上身效果，脸由视频提供）→ 填批次号 → 选模板组 → 点开始 |
| 系统 | 上传图片一次 → 按模板并发提交 100 条 → 轮询 → 下载 → 校验 → 归档 |
| 结果 | `输出根\批次号\001.mp4 ... 100.mp4` + `report.csv` + `batch.json` + `log.txt` |

每天 500 条、26 天/月的量产规模已按成本模型算过（§9）。

---

## 2. 系统全景

```
                    ┌─────────────── 生产链路 ───────────────┐
模板清单(manifest) ──┐                                      │
                    ├─▶ 客户端 ──▶ RunningHub 万相3.0 ──▶ 结果URL(24h)
无头模特图(本地) ─────┘      │        reference-to-video          │
       │                    │                                    │
       └─上传一次─▶ URL ─────┘                                    │
                                                                ▼
                                                          下载 → 校验 → 落盘
                                                                │
                                          ┌─────────────────────┘
                                          ▼
                                    COS 归档(可选) ──▶ 签名链接(7天)
                                                    │
                    ┌─────────────── 授权链路 ──────┴────────┐
                    │ 客户端启动 → 读 配置.ini → LICENSE_SERVER │
                    │   → 本地 token 未过期？→ 联网 /verify      │
                    │   → 否则弹框输密码 → /activate 换 token   │
                    └───────────────────────────────────────────┘
```

---

## 3. 模块地图

### 3.1 桌面程序

| 文件 | 行数量级 | 职责 | 关键点 |
| --- | --- | --- | --- |
| `app/main.py` | 630 | PySide6 界面：两个页面、进度、暂停、结果表 | 配置全来自 `配置.ini`，界面无输入框 |
| `app/core.py` | 480 | 引擎：清单→规划→上传→并发提交→轮询→下载→校验→归档 | `BatchRunner` 可脱离界面单测 |
| `app/storage.py` | 660 | 存储适配层：COS / 本地目录 | 六动作：put/exists/stat/sign/delete/list |
| `app/transfer.py` | 310 | 远端链接→对象存储的**流式**转存（不经本地） | 主流程暂未接入，预留 |
| `app/config.py` | 140 | `配置.ini` 读写、旧 JSON 配置迁移 | 键名全大写 |
| `app/license.py` | 140 | 授权客户端：机器码、激活、token 缓存 | token 绑机器、14 天 |
| `app/startup.py` | 45 | 启动门禁：密码对话框，3 次失败退出 | 未配服务器则直接放行 |
| `run_app.py` | 12 | PyInstaller 入口 | |

### 3.2 命令行工具

| 文件 | 用途 |
| --- | --- |
| `wan3_r2v.py` | 独立批量客户端（校验/上传/提交/轮询/下载/记账/emit 请求原文） |
| `make_batch.py` | 服装清单 → 100 条任务文件（旧链路，与桌面版并存） |
| `tools/build_manifest.py` | 500 个模板视频 → `manifest.json`（读时长、查体积、抽海报） |
| `scripts/build.bat` / `.ps1` | 一键打包：onedir + 自检 + 带日期 zip |
| `scripts/cos_check.py` | COS 七步连通性自检（写/签/读/存在/列举/删） |
| `scripts/archive_to_cos.py` | 历史成片补传 COS，回写 `batch.json` |
| `demo.py` | 离线演练整条链路（假服务，不花钱） |

### 3.3 服务端

| 文件 | 用途 |
| --- | --- |
| `server/license_api.py` | 授权服务，单文件零依赖；SCF 入口 `main_handler`，也可 `--serve` 本地跑 |
| `server/passwords.json` | 密码表（**不进 Git**） |

---

## 4. 生产链路（6 步）

```
① 读 manifest（本地/HTTP）→ 得到 [{id, url}]
② 选组：第1–5组（001–500）或随机 100
③ 上传服装图一次 → 100 条共用同一 URL（1 天有效）
④ 并发提交：POST /alibaba/wan-3.0/reference-to-video
        body = {prompt, imageUrls:[服装图], videoUrls:[模板], resolution:"720P",
                duration:"7", aspectRatio:"9:16", audio:false, promptExtend:false}
⑤ 轮询 /query，SUCCESS 即下载（结果链接 24h 失效）
⑥ 校验（>50KB、时长≈7s）→ 落盘 → 可选归档 COS → 写 report.csv / batch.json
```

**断点续跑**：`batch.json` 记录每条 taskId；重启后已成功的跳过，超时的续查而非重提（不重复扣费）。

---

## 5. 存储链路

| 环节 | 位置 | 有效期 |
| --- | --- | --- |
| 模板视频 | 自有 CDN/OSS（manifest 给 URL） | 长期 |
| 服装图 | RunningHub 上传接口 | 1 天 |
| 生成结果 | RunningHub 返回链接 | **24 小时** |
| 本地副本 | `输出根\批次号\` | 永久 |
| COS 副本 | `{前缀}/output/{日期}/{批次}/001-{指纹8}.mp4` | 永久；签名链接 7 天 |

- COS 归档失败**不算任务失败**，只记 `archive_error`。
- 对象名带内容指纹 → 重复上传命中同一 key，不会越存越多。
- 凭证来源：环境变量 → `~/.cos/credentials.json` → `~/.tccli/<profile>.credential`（支持 STS Token）。

---

## 6. 授权链路

```
首次：输密码 → POST /activate {password, machine} → token（HMAC，绑机器，14 天）→ 存 %APPDATA%
之后：本地验签 → 联网 /verify（服务端说停用就立刻失效；断网则按有效期离线放行）
```

- 服务端无状态：只读 JSON + 算 HMAC，天然适合云函数。
- 停用：密码表 `revoked: true` → 重新部署 → 用户下次启动被拒。
- 详见 [license-handoff.md](license-handoff.md)（可直接交给执行方）。

---

## 7. 配置项

### 7.1 客户端 `配置.ini`（与 exe 同目录，程序自动生成）

| 键 | 说明 | 默认 |
| --- | --- | --- |
| `RH_API_KEY` | RunningHub 32 位 Key（企业级共享） | 空 |
| `MANIFEST` | 模板清单地址（https 或本地路径） | 空 |
| `OUTPUT_ROOT` | 输出根目录 | 留空 → exe 同目录 `输出` |
| `COS_ENABLED` | 是否归档到 COS | `false` |
| `COS_BUCKET` / `COS_REGION` / `COS_PREFIX` / `COS_DOMAIN` | COS 参数（**不放密钥**） | — |
| `LICENSE_SERVER` | 授权服务地址；留空=不校验 | 空 |

> 密钥不进 ini：COS 走本机凭证链，RH Key 由 ini 承载。

### 7.2 服务端环境变量

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `LICENSE_SECRET` | ✅ | HMAC 密钥，≥48 位随机 |
| `LICENSE_TTL_DAYS` | — | 默认 14 |
| `LICENSE_PASSWORDS` | ✅（SCF） | `/var/user/passwords.json` |

---

## 8. 状态矩阵（诚实版）

### 已验证 ✅

| 项 | 证据 |
| --- | --- |
| 三个真实端点存在 | 假 Key 探测：`806` vs 乱写路径 `1001` |
| COS 全链路 | 真桶写/读/签名/列举/删，7 步全绿 |
| 归档幂等 | 3 条真传 COS，key 含指纹，验完清理 |
| 100 条调度 | 假服务 17 秒跑完 100 条 |
| 断点续跑 | 超时后重启续查，未重复提交 |
| 界面批量 | 3 条：表格/CSV/日志/重名目录全对 |
| 授权 11 项 | 激活/错误密码/停用/到期/换机器/网关重试 |
| 打包 | onedir + zip 36.7 MB，selftest 通过 |

### 未验证 ⚠️

| 项 | 原因 | 怎么补 |
| --- | --- | --- |
| **真实生成效果** | 无 RH Key | 配 Key 后跑 1 条 720P/7s |
| 真实单价/耗时 | 同上 | 读 `usage.consumeMoney` |
| 50/100 并发下的平台限流 | 无 Key，且不做未授权实测 | 官方文档未写并发上限，需向商务确认或阶梯实测 |
| 500 模板真实 CDN 拉取 | 模板未上传 | 上传后跑 3 条观察 |

---

## 9. 成本模型

**生成费**：单价未知（需实测），100 条/批 × 26 天 × 500 条/天 —— 这是绝对大头。

**COS（按 15 MB/条、13,000 条/月）**：

| 项 | 月流量 | 月费用 |
| --- | --- | --- |
| 模板被模型拉取 | 190 GB | ¥95（CDN 后 ¥38） |
| 成片被下载 1 次/条 | 190 GB | ¥95 |
| 成片上传 | — | **¥0（入站免费）** |
| 存储（3 个月累积 571 GB） | — | 第 3 月约 ¥67，季累计 ¥135 |

结论：**COS 占比 <5%**，优化优先级低。

---

## 10. 发布流程

```bash
scripts\build.bat              # 构建 + 自检 + 生成 dist\换装批量生产器-v0.1-日期.zip
```

- 打包模式：**onedir**（不能用 onefile：会解压到 `%TEMP%`，受限机器会报 `Could not create temporary directory!`）
- 分发：zip 整个文件夹（exe 旁必须有 `_internal\`）
- 大文件不进仓库：`dist/` 已忽略，安装包走 GitHub Releases

---

## 11. 已知限制

| 限制 | 影响 | 缓解 |
| --- | --- | --- |
| 单条 = 单请求 | 100 条 = 100 次 POST | 并发 3–10 |
| 结果链接 24h | 过期不可恢复 | 成功即下载 |
| 上传链接 1 天 | 不能提前一天传 | 当天传当天跑 |
| 并发上限未知 | 拉满可能被限流 | 阶梯测试 / 问商务 |
| Key 内置 | exe 泄露 = 余额可被刷 | 授权密码 + 余额上限 |
| 授权不防逆向 | 改代码可绕过 | 定位是"防随手转发" |
| 单条失败无自动重试 | 需手动补跑 | `report.csv` 列出失败项 |
| 无历史批次界面 | 只能看文件夹 | 后续可加 |

---

## 12. 待办（按优先级）

1. **配 RH Key 跑 1 条真实** → 拿单价、验效果（唯一阻塞项）
2. 500 模板上 CDN → `tools/build_manifest.py` 出 manifest
3. ~~部署授权服务~~ ✅ **已完成 2026-10-24**：腾讯云函数 `outfit-license`（ap-guangzhou，Python3.9，128MB/5s）+ 函数 URL
   `https://1463907213-3ifuebn6ih.ap-guangzhou.tencentscf.com`。
   ⚠️ 原计划的 API 网关已停止售卖，改用 SCF 函数 URL；运维用 `python scripts/license_deploy.py deploy|test|url`。
4. 失败自动重试（1–2 次）+ 一键补跑
5. 历史批次列表（读 `batch.json`）
6. `transfer.py` 接入主流程（边下边传，省本地磁盘）

---

## 13. 文档索引

| 文档 | 内容 |
| --- | --- |
| [README.md](../README.md) | 入口：三条命令上手 |
| [app/README.md](../app/README.md) | 界面使用说明、打包命令 |
| [wan-3.0-reference-to-video.md](wan-3.0-reference-to-video.md) | 接口规格（字段/约束/状态机/计费） |
| [cos-setup.md](cos-setup.md) | COS 接入手册（建桶/密钥/生命周期/CDN） |
| [license-setup.md](license-setup.md) | 授权方案说明与上线手册 |
| [license-handoff.md](license-handoff.md) | **交给执行方的完整交接文档** |
| [batch/README.md](../batch/README.md) | 旧链路：清单→100 条任务 |
| [tests/README.md](../tests/README.md) | 离线联调（假服务） |
