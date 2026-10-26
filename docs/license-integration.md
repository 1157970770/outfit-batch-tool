# 授权服务对接文档

> 面向：要让「换装批量生产器」真正走密码授权的执行者（人或模型）。
> 配套施工书：[deployment-spec.md](deployment-spec.md)（那篇讲"这一次要传什么"，本篇讲"整套怎么咬合、怎么验收、怎么排错"）。
> **本文已按 v0.9 架构更新（2026-10-26 13:50）**：地址改为内置常量，运行配置改由服务端下发；补充 COS 凭证下发（§10）。

---

## 0. 现状诊断（2026-10-26 13:50 实测，v0.9）

| 环节 | 状态 | 证据 |
| --- | --- | --- |
| 云函数 `outfit-license` | ✅ 已上线 | ap-guangzhou，函数 URL `https://1463907213-3ifuebn6ih.ap-guangzhou.tencentscf.com` |
| 密码表 | ✅ 3 条可用 | `server/passwords.json`，真实表只在本地 + 云端各一份 |
| 客户端校验代码 | ✅ 已接 | `app/main.py:887` 起调 `ensure_license()`；`app/startup.py` 弹框；`app/license.py` 激活/缓存 |
| 服务地址 | ✅ **改为内置常量** | `app/config.py:18` `SERVICE_URL` 写死；`配置.ini` 的 `LICENSE_SERVER` 已不再影响授权 |
| 服务端代码 | ✅ 新版 | 含 `CONFIG_PATH`(:38)、`load_runtime_config()`(:133)，`/activate` 返回 `config`(:220) |
| 环境变量 | ✅ 已配 | `LICENSE_SECRET` / `LICENSE_PASSWORDS` / `LICENSE_CONFIG=/var/user/config.json`（已删废弃的 `LICENSE_TTL_DAYS`） |
| `config.json` 打包 | ✅ 已接入（**必需项**） | `scripts/license_deploy.py` 的 `ZIP_FILES` 含它；缺这个文件部署会直接失败并提示怎么建，不会静默漏传 |
| 服务端下发 `config` | ✅ 已通 | 线上 `/activate` 返回 `config`，`manifest_url` 与 `cos` 均已下发 |
| **`config.rh_api_key`** | ❌ **空** | 本地与线上长度都是 0 → 客户端显示「配置：未下发」、跑不了任务 |
| 模板清单 | ✅ 已发布（108 条／目标 500） | `video-input-1463907213.cos.ap-guangzhou.myqcloud.com/manifest.json` → 200，客户端能解析 |
| 分发安装包 | ✅ 已打（v0.9） | `dist\换装批量生产器-v0.9-20261026.zip` |

**结论**：链路本身已通，**唯一缺口是 `server/config.json` 里的 `rh_api_key` 还没填**。填法见第 3 节。

一条命令随时复查：`python3 scripts\license_deploy.py config`

---

## 1. 一次授权到底发生了什么

```
用户启动 exe
   │
   ├─ 地址：app/config.py 内置 SERVICE_URL（写死）
   │
   ├─ 本地有缓存票？ ──否──► 弹框要密码 ──► POST /activate ──► 存票
   │                                  │        └─► 同时下发 config（Key/清单/COS）
   │                          密码不存在/已到期/已停用 → 提示后重输，3 次失败退出
   │
   ├─ 有票：POST /verify ──► 服务端查表（到期/停用/机器）──► 放行 + 再下发 config
   │                     └──► 连不上：上次成功核验 7 天内放行，超过要求联网
   │
   └─ 运行配置（Key/清单/COS）全部来自下发，本地没有可编辑文件
```

票存在 `%APPDATA%\RHOutfitTool\license.json`，里面**没有明文密码**。

---

## 2. 地址与配置从哪来（v0.8 的三个来源）

| 层 | 存在哪 | 内容 | 谁改 | 改完怎么生效 |
| --- | --- | --- | --- | --- |
| 服务地址 | `app/config.py:18` `SERVICE_URL` | 只有 URL | 只有你（改代码） | **重新打包**，客户换 exe |
| 运行配置 | 云函数里的 `config.json` | `rh_api_key` / `manifest_url` / `cos` / `notice` | 只有你 | `deploy` 后客户**下次启动**生效 |
| 本机缓存 | `%APPDATA%\RHOutfitTool\license.json` | 票 + `last_ok`，**DPAPI 加密**（`dpapi:` 开头） | 客户端自己 | 删掉即要求重新激活 |

`app/main.py:48`：

```python
def resolve_license_server(cfg=None):
    """服务地址只有一处来源：内置常量。改地址必须重新打包。"""
    return (appconfig.SERVICE_URL or "").strip()
```

**注意**：v0.8 起 `配置.ini` 里的 `LICENSE_SERVER` 已经**不再影响授权**，`RH_API_KEY` / `MANIFEST` 也改由服务端下发。
客户端里没有可编辑的配置文件——"改配置"= 改服务端 `config.json` + `deploy`，**不用重新打包、不用让客户换文件**。

---

## 3. 三种场景的操作

### A. 补齐运行配置（当前唯一待办）

> ⚠️ `rh_api_key` 是 32 位密钥，**不要贴进对话、不要提交 Git**。请自己在本地文件里填。

1. 打开 `D:\批量视频任务设计区\server\config.json`
2. 把 32 位 Key 填进引号：
   ```json
   "rh_api_key": "在这里填 32 位 Key",
   ```
3. 保存后执行：
   ```powershell
   python3 scripts\license_deploy.py config     # 看本地是否已填
   python3 scripts\license_deploy.py deploy     # 打包 + 上传（自动附加 config.json）
   python3 scripts\license_deploy.py config     # 看线上是否已下发
   ```
4. 期望：`线上 rh_api_key : 已下发（长度 32）` + `结论: 配置完整，客户端可用`

> `config.json` 里下划线开头的字段（如 `_说明`）是注释，**不会下发给客户端**（服务端已过滤）。

### B. 本机开发（源码运行）

地址是内置的，直接跑就行；用 `server/passwords.json` 里任意一条密码激活。
不想每次输密码、只想改代码：设环境变量 `RH_DEV_NO_LICENSE=1`（**仅源码有效，打包版无效**）。

### C. 换服务地址 / 换机器部署

只有一条路：改 `app/config.py:18` 的 `SERVICE_URL` → `scripts\build.bat` 重新打包 → 客户换 exe。
（云函数地址变了才需要做这件事；日常改 Key、改清单都走 A。）

---

## 4. 代码位置索引

| 文件 | 位置 | 作用 |
| --- | --- | --- |
| `app/config.py` | `:18` | `SERVICE_URL` 内置地址（唯一来源） |
| `app/config.py` | `:95-106` | 把服务端下发的 `config` 映射成本地 cfg |
| `app/main.py` | `:48-50` | `resolve_license_server()`：直接返回内置常量 |
| `app/main.py` | `:887` 起 | 启动门禁：`ensure_license()` |
| `app/startup.py` | `:17-45` | 弹框输密码，3 次失败退出 |
| `app/license.py` | `:188` | `activate()`：换票 + 存缓存 + 记 `last_ok` |
| `app/license.py` | `:212` | `status()`：本地票期 → `/verify` → 7 天救援窗口 |
| `app/secure_store.py` | `:41-102` | DPAPI 加解密，缓存落地即 `dpapi:` 开头 |
| `server/license_api.py` | `:38` | `CONFIG_PATH`（环境变量 `LICENSE_CONFIG` 优先） |
| `server/license_api.py` | `:133` | `load_runtime_config()`，过滤 `_` 开头字段 |
| `server/license_api.py` | `:220` / `:227` | `/activate` `/verify` 附带 `config` 下发 |
| `server/config.json` | — | 运行配置真源（已 gitignore） |
| `server/passwords.json` | — | 密码表（唯一真身，别分发） |
| `scripts/license_deploy.py` | `pack` / `deploy` / `config` | 打包（含 config.json）、部署、配置自检 |
| `scripts/password_manager.py` | — | 图形化管理密码（双击 `scripts\密码管理器.bat`） |

---

## 5. 协议速查

Base = 上面的函数 URL，路径透传，POST + `application/json`。

| 接口 | 入参 | 成功 | 关键失败 |
| --- | --- | --- | --- |
| `POST /activate` | `password`, `machine` | `200 {ok, token, expire_at, days, note, config}` | `401 密码不存在` / `403 授权已到期…` / `403 密码已被停用` |
| `POST /verify` | `token`, `machine` | `200 {ok, expire_at, note, config}`，**续期后可能附带 `renewed_token`** | `403 授权已到期…` / `403 token 与这台机器不匹配` / `403 token 签名不对` |
| `POST /config` | `token`, `machine` | `200 {ok, config}`（不激活只取配置） | 同上 |
| `GET /health` | — | `200 {ok, service, mode}` | — |

`config` 结构：`{rh_api_key, manifest_url, cos:{enabled,bucket,region,prefix,domain,secret_id,secret_key}, notice}`。

有效期全系统只有一处：密码表 `expires`（新增默认 31 天，带 `23:59` 表示到当天结束）。`/verify` 每次都回表复查，所以**密码到期 = 下次启动即被拒**，没有宽限。

---

## 6. 对接验收清单

服务端（一条命令跑完）：

```powershell
python3 scripts\license_deploy.py test      # 5 项：health / 激活 / 错密码 / 同机 / 换机
python3 scripts\license_deploy.py config    # 运行配置是否已下发（含 rh_api_key）
```

| # | 检查 | 期望 |
| --- | --- | --- |
| 1 | `/health` | 200 |
| 2 | `/activate` 里的 `config.rh_api_key` | **非空，长度 32** |
| 3 | 客户端首次启动 | 状态灯「配置：已下发」、「模板清单：N 个」 |
| 4 | 真跑 1 条小批量 | 真的出视频（**要花钱，需确认**） |
| 5 | 本地缓存 | `%APPDATA%\RHOutfitTool\license.json` 以 `dpapi:` 开头，全文搜不到 Key |

客户端（人工 4 步）：

1. 启动 → 弹「需要授权」→ 输密码 → 提示「授权成功」
2. 关掉重开 → 不再要求输入（票有效期内）
3. 删 `%APPDATA%\RHOutfitTool\license.json` → 重新要求输密码
4. 在密码管理器里把该密码「停用」→ 重启软件 → 应被拒（`密码已被停用`）

---

## 7. 故障对照表

| 界面现象 | 真实原因 | 处理 |
| --- | --- | --- |
| 「配置：未下发」 | `/activate` 的 `config.rh_api_key` 为空 | 按第 3 节 A 填 Key 并 deploy |
| 「模板清单：未下发」 | `manifest_url` 空或打不开 | 补 config.json；确认 URL 公网可访问 |
| 「未配置授权服务」+ 直接退出 | 内置 `SERVICE_URL` 被清空 | 恢复 `app/config.py:18` 并重新打包 |
| 「本机还没有授权，请输入分配的密码」 | 首次运行 / 缓存被清 | 输密码 |
| 「密码不存在」 | 密码打错，或还没同步到云 | 密码管理器里核对，或点「立即同步到云」 |
| 「授权已到期，请联系管理员续期」 | 表里 `expires` 过了 | 密码管理器「续期」 |
| 「密码已被停用」 | `revoked: true` | 需要恢复就「停用/恢复」切回 |
| 「需要联网核验一次（已超过 7 天…）」 | 长时间连不上授权服务 | 检查网络 / 函数是否在跑 |
| 一直提示连接失败 | 函数 URL 变了 / 网络不通 | `license_deploy.py url` 核对 |
| 输密码后报「token 签名不对」 | 换过 `LICENSE_SECRET` | 预期行为：让用户重输一次密码 |

---

## 8. 红线

1. **不要动 `LICENSE_SECRET`**：一动全员失效，必须重新输密码。它只在云函数环境变量里，任何时候不要打印。
2. `server/config.json`、`server/passwords.json`、`dist/license-deploy.zip` **都不进 Git、不贴对话、不跟软件一起发**。
3. `scripts/` 整个目录（含密码管理器）是内部工具，`build.ps1` 只打包 `run_app.py`，本来就不含它——别手动塞进去。
4. 协议字段名（`password` / `machine` / `token` / `action` / `config` / `rh_api_key` / `manifest_url` / `cos`）与 token 结构（`p` / `m` / `e`）不能改，改了老客户端全废。
5. **不要把 RH Key 写进客户端代码或打包进 exe**——现在是服务端下发，写进客户端等于前功尽弃。

---

## 9. 回滚

| 想做的事 | 做法 |
| --- | --- |
| 撤掉某个人的授权 | 密码管理器「停用」→ 自动同步 → 他下次启动被拒 |
| 收回全部授权 | 换 `LICENSE_SECRET` + 重新 deploy → 所有客户端需重新激活 |
| 关掉配置下发 | 云函数里删掉 `config.json` 后 deploy → 客户端显示「配置：未下发」 |
| 换服务地址 | 改 `app/config.py:18` → 重新打包 → 客户换 exe（没有别的办法） |
| 让某台机器重新激活 | 删 `%APPDATA%\RHOutfitTool\license.json` |

---

## 10. 客户端 COS 凭证（服务端下发，v0.9 起）

成品归档需要**往桶里写**，所以客户端要有凭证。做法：**专用子账号密钥随 `config.json` 下发**，客户端零手工配置。

| 项 | 值 |
| --- | --- |
| 子账号 | `rh-client-uploader`（Uin `100054216283`，**无控制台登录**） |
| 策略 | `rh-client-upload`（PolicyId `291294131`） |
| 双桶 | 模板桶 `video-input-1463907213`（只读，客户端不持有写权限）／成品桶 `video-output-1463907213`（可写） |
| 允许 | `PutObject` / `GetObject` / 四个分片动作，**仅** `video-output-1463907213` 的 `prod/output/*` 与 `output/*` |
| 禁止 | 整个 `video-input-1463907213`、`GetBucket`（不可列举）、`DeleteObject` |
| 存放 | `server/config.json` → `cos.secret_id` / `cos.secret_key`（该文件不在 Git） |
| 下发 | 随 `/activate`、`/verify` 回传；客户端 DPAPI 加密存在 `license.json` |

**已在真机验证过的权限边界**：写/读 `video-output/prod/output/…` 成功；写**整个 `video-input` 桶**、写 `prod/…`（无 `output/` 段）、列举桶 全部 `AccessDenied`。

> **两个桶的角色**（`app/main.py:80 build_storages`）：
> `template_bucket` = `video-input-1463907213`（放模板，**客户端无任何权限**，靠公有直链或服务端签名）
> `output_bucket` = `video-output-1463907213`（放成品，客户端用下发的子账号密钥写入）
> 两个字段都在 `server/config.json` 的 `cos` 下；缺省都回退 `cos.bucket`。

> **真实对象路径（踩过的坑）**：成品不是落在 `output/…`，而是 `{cos.prefix}/output/<日期>/<批次>/<文件名>-<指纹8>.mp4`
> （`object_key()` 在 `app/storage.py:199`，前缀取 `cos.prefix`，当前 = `prod`）。
> 所以策略必须写 `prod/output/*`；只写 `output/*` 会让客户端归档**静默失败**——`_archive()` 把异常记进 `archive_error` 就返回，任务本身不受影响，所以日志里才看得到线索。
> 另外 CAM 策略改版后有 **1～2 分钟生效延迟**，改完立刻测会误判。

### 客户端侧的三行代码

| 文件 | 改动 |
| --- | --- |
| `app/config.py` `load()` | 映射 `COS_SECRET_ID` / `COS_SECRET_KEY`（顺带补 `COS_TEMPLATE_BUCKET` / `COS_OUTPUT_BUCKET`，让双桶设计生效） |
| `app/main.py` `_make_storage()` | 有下发就用下发的凭证；没下发则回退本机凭证/环境变量（旧机器不受影响） |

### 轮换密钥（不用重发客户端）

1. 控制台给 `rh-client-uploader` 新建一组密钥 → 禁用旧密钥
2. 新密钥写进 `server/config.json` 的 `cos.secret_id` / `cos.secret_key`
3. `python3 scripts\license_deploy.py deploy`
4. 客户端**下次启动**自动拿到新密钥（票里缓存的旧 config 会被刷新）

> `CreateAccessKey` 的 `SecretKey` **只返回一次**，务必当场落盘；拿不到就只能删了重建。

### 已知风险与接受范围（2026-10-26 决策）

这组密钥会随 `/activate` 下发到客户端，**等于落在客户机磁盘上**。已评估并**接受**该风险，完整记录见
[adr-001-cos-client-credential.md](adr-001-cos-client-credential.md)。

三条必须记住的事实：

1. **反编译 exe 拿不到密钥**，但有"有效密码 + 一台已激活机器"就能解出来——门槛比反编译低。
2. **DPAPI 不是安全边界**：同 Windows 用户下任何进程都能解，它只防"文件被拷到别处"，防不了"本机被读"。
3. **暴露面被压得很小**：密钥只能对 `video-output/prod/output/*` 做 Put/Get，不能列举、不能删除、
   **完全碰不到 `video-input` 模板桶**。最坏情况是成品可被破坏，不是素材外泄。

收口手段只有两个：改密钥 + `deploy`（客户端下次启动自动换，不重打包），或禁用旧密钥（立刻全员失效）。
重新评估的触发条件（对外发客户端、异常写入、对外演示、人员离职）见 ADR 第 6 节。
