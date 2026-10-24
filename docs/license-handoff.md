# 授权服务 · 部署交接文档

> **给执行方（另一个模型 / 另一个人）看的完整说明。**
> 目标：把 `license_api.py` 部署成一个公网 HTTPS 接口，并让桌面软件在启动时用「分配的密码」换取授权。
> 本文自带协议规格、文件清单、验收标准，不依赖任何对话上下文。

---

## 0. 执行方须知（先看这一节）

**你要做的只有三件事**：上传两个文件到云函数 → 配 3 个环境变量 → 建一个 HTTP 触发器（函数 URL）。

**必须遵守的约束**：

| 约束 | 原因 |
| --- | --- |
| 不要修改协议字段名（`password` / `machine` / `token` / `action`） | 桌面客户端已按此实现，改了全部失效 |
| 不要修改 token 的 HMAC 算法与字段名（`p` / `m` / `e`） | 客户端缓存里的旧 token 会验不过 |
| 不要把密码表提交进 Git | 表里是真实发放的密码 |
| 不要把 `LICENSE_SECRET` 写进代码 | 泄露 = 别人可自签 token |
| 对外入口**不要开鉴权**（函数 URL 默认公开，别加签名/密钥） | 否则客户端 401 |

**验收标准**（第 7 节有完整清单）：能用一个密码换到 token；错误密码返回 401；换机器验签失败；停用后立刻拒绝。

---

## 1. 架构

```
使用者                     你部署的授权服务                 客户端 exe
  │  ① 拿到密码                 │                              │
  │  ────────────────────────▶ │                              │
  │                             │  ② POST /activate            │
  │                             │ ◀────────────────────────────
  │                             │     {password, machine}      │
  │                             │  校验密码表（JSON，无数据库） │
  │                             │  签发 token（HMAC，绑机器）  │
  │                             │ ────────────────────────────▶
  │                             │  ③ {token, expire_at}         │
  │                             │                    存 %APPDATA%
  │                             │  ④ 每次启动 POST /verify      │
  │                             │ ◀────────────────────────────
  │                             │     {token, machine}          │
  │                             │  验签 + 查是否停用            │
  │                             │ ────────────────────────────▶ 放行/拒绝
```

- 服务端**完全无状态**：不存 token、不存会话，只读一份 JSON + 算一次 HMAC。适合云函数。
- token 自带过期时间与机器码，服务端不需要数据库。
- 客户端联网失败时按「上次成功核验」放行，窗口 `GRACE_DAYS = 7` 天（`app/license.py`）。
  超过 7 天没连上服务器就要求联网核验一次。

---

## 2. 文件清单

仓库根目录：`D:\批量视频任务设计区`

| 文件 | 大小 | 作用 | 是否上传到服务端 |
| --- | --- | --- | --- |
| `server/license_api.py` | 9.6 KB | 服务端全部逻辑，零第三方依赖 | ✅ 必须 |
| `server/passwords.json` | 0.6 KB | 密码表（真实发放的密码） | ✅ 必须 |
| `server/passwords.example.json` | 0.5 KB | 密码表模板 | ❌ 仅参考 |
| `dist/license-deploy.zip` | 4 KB | 已打好的上传包（含上面两个文件） | ✅ 直接传这个 |
| `app/license.py` | 4.9 KB | 客户端：激活、缓存、机器码 | ❌ 已打包进 exe |
| `app/startup.py` | 1.4 KB | 启动门禁（弹框输密码） | ❌ 同上 |
| `scripts/license_passwords.py` | 6 KB | 密码表增删改，**改完自动同步云函数并验证** | ❌ 本地跑 |
| `scripts/license_deploy.py` | 7 KB | 打包 / 部署 / 查线上状态 / 验收 | ❌ 本地跑 |
| `scripts/密码管理器.py` | 13 KB | **图形化管理界面**（PySide6），日常推荐用这个 | ❌ 内部工具，勿分发 |
| `scripts/密码管理器.bat` | 1 KB | 双击启动上面那个界面 | ❌ 同上 |
| `tests/test_license.py` | 6 KB | 11 项自动化验收 | ❌ 本地跑 |

`license-deploy.zip` 的结构（文件在 zip 根目录，**不能套文件夹**）：

```
license-deploy.zip
├── license_api.py
└── passwords.json
```

### 2.1 分发边界：哪些文件能到客户手里

客户拿到的只有一个包：`dist/换装批量生产器-v<版本>-<时间>.zip`。解开只有三项：

```
换装批量生产器/
├── _internal/            PyInstaller 运行时 + 依赖
├── 换装批量生产器.exe
└── 配置.ini
```

**`passwords.json` 不在里面，而且客户端代码根本不认识它**——`grep -rn "passwords" app/ run_app.py wan3_r2v.py` 零匹配，`app/license.py` 只负责把密码 POST 到云端 `/activate`。所以这份名单**不会随着软件复制、转发、网盘分享被带出去**。

| 文件 | 里面有什么 | 能给客户吗 |
| --- | --- | --- |
| `dist/换装批量生产器-v*.zip` | exe + 运行时 + `配置.ini` | ✅ 只发这个 |
| `dist/license-deploy.zip` | `license_api.py` + **全量 passwords.json** | ❌ 只用于往云端部署。发给客户等于把密码本交出去 |
| `server/passwords.json` | 真实密码表（唯一真身） | ❌ 本地保留，改也只改这份 |
| `server/passwords.example.json` | 空模板 | 参考用，不含真实密码 |

全世界的密码表只有两份：**本地 `server/passwords.json`（真身，你编辑这份）** 与 **云端函数 `/var/user/passwords.json`（部署时复制过去，运行时读这份）**。二者靠 `scripts/license_passwords.py deploy` 同步。

发版前 30 秒自检：双击打开要发的 zip，根目录只应有 `_internal`、一个 exe、`配置.ini`。**看到 `passwords.json` 或 `server/` 目录，立刻停下，别发。**

---

## 3. 协议规格（可据此独立实现或核对）

Base：部署后得到的地址，例如 `https://service-xxx.gz.apigw.tencentcs.com/release/license`

所有请求：`POST`，`Content-Type: application/json`
所有响应：`application/json`，字段见下。

### 3.1 `POST /activate`

```json
{
  "action": "activate",
  "password": "RH-2026-001-mj45",
  "machine": "a1b2c3d4e5f6..."
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `action` | 建议 | 客户端会带上；不带时按路径末段判断 |
| `password` | 是 | 你分配的密码 |
| `machine` | 是 | 客户端算的机器码（32 位十六进制） |

成功（HTTP 200）：

```json
{
  "ok": true,
  "token": "eyJwIjoiUkgtMjAyNi0wMDEiLCJtIjoiLi4uIiwiZSI6MTc5NDAzNjU3NX0.0257b739...",
  "expire_at": 1794036575,
  "days": 14,
  "note": "张三 / 设计部"
}
```

失败：

| HTTP | `error` | 触发条件 |
| --- | --- | --- |
| 400 | 缺少密码 / 缺少 machine | 参数缺失 |
| 401 | 密码不存在 | 密码表里没有 |
| 403 | 授权已到期，请联系管理员续期 | `expires` 已过 |
| 403 | 密码已被停用 | `revoked: true` |
| 403 | 该密码已在另一台机器上激活 | `machine` 字段已绑定别的机器 |

> 成功响应里的 `expire_at` 就是**这个密码的到期日**（`expires` 换算成时间戳），`days` 是距今天数。世上只有一个时间，没有独立的票面 TTL。

### 3.2 `POST /verify`

```json
{ "action": "verify", "token": "eyJwIjoi...", "machine": "a1b2c3d4..." }
```

| 结果 | 说明 |
| --- | --- |
| `200 {"ok":true,"expire_at":...,"note":"..."}` | 授权有效 |
| `403 授权已到期，请联系管理员续期` | **每次都会回表复查 `expires`**，到期就是到期，无宽限 |
| `403 token 与这台机器不匹配` | 缓存被拷到别的电脑 |
| `403 token 签名不对` | 换过 `LICENSE_SECRET`，需重新激活 |
| `403 密码已被停用 / 已被删除` | 密码表里改了 `revoked` 或删了条目 |

### 3.3 `GET /health`（可选，用来确认路由通）

```json
{ "ok": true, "service": "outfit-license", "mode": "password-expires" }
```

### 3.4 token 结构（不要改）

```
base64url({"p":密码,"m":机器码,"e":过期时间戳}) + "." + HMAC-SHA256(前半段, LICENSE_SECRET) 的 hex
```

---

## 4. 部署方式 A：腾讯云函数 SCF（推荐）

### 4.1 上传

1. 打开云函数 SCF 控制台 → **函数管理 → 新建函数**
2. 函数名称 `outfit-license`，运行环境 **Python 3.9 或更高**
3. 提交方法选 **上传 zip 包**，传 `dist/license-deploy.zip`
4. 执行方法填 `license_api.main_handler`
5. 内存 128 MB、超时 3 秒足够

上传后进「函数代码」，左侧应能看到两个文件：`license_api.py`、`passwords.json`。

> ⚠️ SCF 的代码目录 `/var/user` 是**只读**的。想改密码表只能改完重新上传，不能在线编辑保存后不部署。

### 4.2 环境变量

函数 → **函数配置 → 编辑 → 环境变量**：

| 变量 | 值 | 必填 |
| --- | --- | --- |
| `LICENSE_SECRET` | 48 位随机串 | ✅ |
| `LICENSE_PASSWORDS` | `/var/user/passwords.json` | ✅ |

> 旧的 `LICENSE_TTL_DAYS` 已经**废弃**（现在只有一个到期日，就是密码表里的 `expires`）。环境变量里留着也不影响，可以直接删掉。

生成随机串（PowerShell）：

```powershell
-join ((48..57)+(65..90)+(97..122) | Get-Random -Count 48 | % {[char]$_})
```

### 4.3 对外暴露：函数 URL（⚠️ API 网关已停售，别再照着老文档配）

> **2026-10 实测结论**：API 网关已**停止售卖**，新建 service 会直接失败：
> `FailedOperation.LimitingResourceCreated：API网关产品已停止售卖，无法新建资源`。
> 所以不要再去建 API 网关触发器。**改用 SCF 自带的「函数 URL」**——一个地址同时服务
> `/activate`、`/verify`、`/health`，路径是透传的，不需要像网关那样一条一条建路由。

函数 → **触发管理 → 创建触发器**：

| 项 | 值 |
| --- | --- |
| 触发类型 | **HTTP**（就是函数 URL） |
| 触发器名称 | `licenseUrl` |
| 鉴权 | **免鉴权（NONE）** |
| 公网访问 | 开启 |

保存后得到地址，形如：

```
https://1463907213-<随机8位>.ap-guangzhou.tencentscf.com
```

### 4.4 填入客户端的地址

函数 URL **透传路径**，所以 base 直接用域名，不用带 `/license`：

```ini
LICENSE_SERVER=https://1463907213-<随机8位>.ap-guangzhou.tencentscf.com
```

客户端会自动拼 `/activate` 与 `/verify`，服务端按路径最后一段分派。
写成 `https://.../license` 也照样能用（实际请求 `/license/activate`），两种都可以。

#### ⚠️ 客户端是"默认强制授权"（v0.5 起）

这一点直接决定分发包能不能启动，务必先看懂：

| 来源 | 优先级 | 说明 |
| --- | --- | --- |
| `配置.ini` 的 `LICENSE_SERVER` | 1 | 用户可改，换服务器用 |
| 程序内置 `EMBEDDED_LICENSE_SERVER`（`app/main.py` 顶部） | 2 | 兜底，防止用户删掉配置行绕过 |
| 两者都空 | — | **程序拒绝启动**，弹"未配置授权服务" |

所以正式分发前，**必须二选一填上**，否则用户双击 exe 只会看到一行错误提示然后退出。

> 开发调试可临时设 `RH_DEV_NO_LICENSE=1` 跳过校验，但**只对源码运行有效**，打包后的 exe 不认。

---

## 5. 部署方式 B：任意一台服务器

```bash
set LICENSE_SECRET=换成一串48位随机字符
set LICENSE_PASSWORDS=D:\批量视频任务设计区\server\passwords.json
python3 server\license_api.py --serve 8099
```

- 内网直接 `http://内网IP:8099`
- 公网必须加 HTTPS 反代（Nginx / Caddy），否则密码明文过网
- 修改密码表后**重启进程**才生效（云函数是重新部署生效）

---

## 6. 密码表运维

### 6.1 格式

```json
{
  "passwords": {
    "RH-2026-001-mj45": {"note": "张三 / 设计部",   "expires": "2027-12-31", "revoked": false},
    "RH-2026-002-4688": {"note": "李四 / 外包",     "expires": "2026-12-31", "revoked": false},
    "RH-2026-003-cegs": {"note": "王五（已离职）",  "expires": "2027-12-31", "revoked": true}
  }
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `note` | 建议 | 只给你看，激活成功会回显 |
| `expires` | 建议 | `YYYY-MM-DD` 或 `YYYY-MM-DD HH:MM`；不填 = 永久。**只写日期时按当天 00:00 失效** |
| `revoked` | 否 | `true` = 立即停用 |
| `machine` | 否 | 填了锁死那台机器；留空 = 不锁 |

### 6.2 密码命名规范

`RH-<年份>-<三位序号>-<四位随机>`，例：`RH-2026-007-k3f9`。
好念、可追溯、不易被猜。

### 6.3 运维动作

| 动作 | 改什么 | 生效 |
| --- | --- | --- |
| 新增用户 | 加一条 | 自动同步后（几秒） |
| 停用某人 | `revoked: true` | 自动同步后，其下次启动即被拒 |
| 续期 | 改 `expires` | 自动同步后 |
| 换电脑 | 不用改，新机输同一密码 | 立即 |
| 换 `LICENSE_SECRET` | 环境变量 | **所有客户端需重新激活** |

### 6.4 改完怎么同步到云端（自动的）

**一条命令，改表 + 上传 + 验证全做完：**

```powershell
python scripts/license_passwords.py add --note "张三 / 设计部"
python scripts/license_passwords.py add --note "李四 / 外包" --expires "2026-12-31 23:59"
```

它会依次做三件事，任何一步失败都会明确告诉你：

1. 改本地 `server/passwords.json`（原子写，写坏不了）
2. 把 `license_api.py` + `passwords.json` 打成 zip 整体上传云函数，并轮询到函数状态回到 `Active`
3. 用这个密码真打一次线上 `/activate`，确认线上**真的认得**（`✓ 线上已生效`）

为什么必须整体重传、不能只改云端那一个文件：SCF 的代码目录 `/var/user` 是**只读**的，改不了单个文件。所以"同步"的本质是重新部署代码包，**耗时 5~15 秒**，不是热更新。

| 命令 | 线上期望结果 |
| --- | --- |
| `add` / `unrevoke` / `expire` | `[PASS] 线上应可用 → 200` |
| `revoke` / `remove` | `[PASS] 线上应被拒 → 403` |

只改本地、先不推云（没网，或还在斟酌）：加 `--no-deploy`。
只推云、不改表：`python scripts/license_passwords.py deploy`。
只想查某个密码线上现在什么状态：`python scripts/license_deploy.py check <密码> [--expect reject]`。

> 同步失败（`✗ 上传没成功`）时云端仍是旧表、**本地已保存**，修好网络或重新登录 tccli 后跑一次 `deploy` 即可，不需要重做改动。

> 手动路线（脚本不可用时）：控制台 → 函数代码 → 打开 `passwords.json` → 改 → 点**部署**。等价，但容易手抖写坏 JSON，不推荐。

### 6.5 到期与续期（全系统只有一个到期日）

**铁律**：时间只有一处 —— 密码表里的 `expires`。票里虽然也带一个到期时间戳，但它的值就等于 `expires`，而且 `/verify` **每次都会回表复查**，所以密码表说了算，不存在"票还没到期所以还能用"的宽限。

**续期一条命令**（改表 + 上传 + 验证 一条龙）：

```powershell
python scripts/license_passwords.py expire RH-2026-001-mj45 2028-12-31
python scripts/license_passwords.py expire RH-2026-001-mj45 "2028-12-31 23:59"
python scripts/license_passwords.py expire RH-2026-001-mj45 never        # 改成永久
```

新增密码默认有效期 **31 天**（今天 +31 天，保留到当天 23:59）。要指定就加 `--expires "2027-06-30 23:59"`。

`list` 会把状态直接标出来：`可用` / `已到期` / `已停用`。

**到期 / 续期后的真实行为：**

| 场景 | 结果 |
| --- | --- |
| 到期后，对方下次启动 | ❌ `403 授权已到期` —— **立刻**，不是拖几天 |
| 到期后，新电脑第一次输这个密码 | ❌ 同样被拒 |
| 续期后，对方已装好的软件 | ✅ **什么都不用做**：下次启动时服务端会顺手换发一张新票，自动续上 |
| 服务端连不上（网络抖动/我们在发布） | ✅ 上次成功核验在 **7 天**内就放行；超过 7 天要求联网核一次 |

**想立刻踢掉某人**：用 `revoke`，不要等到期。`/verify` 每次都读表里的 `revoked`，下次启动即被拒。

> ⏰ 时间精度坑：`expires` 只写日期时按**当天 00:00（北京时间）**失效，`2027-12-31` 的最后有效时刻是 12-30 23:59:59。想"有效到那天结束"，写 `2027-12-31 23:59`。命令行和界面都会提醒，界面默认就填 `23:59`。

### 6.6 图形化管理界面（日常推荐用这个）

双击 **`scripts\密码管理器.bat`**（或 `python scripts\密码管理器.py`）：

| 功能 | 说明 |
| --- | --- |
| 表格 | 密码 / 状态 / 到期时间 / 剩余天数 / 备注，状态分 `可用` `即将到期(≤7天)` `已到期` `已停用` |
| 新增密码 | 自动生成 `RH-年-序号-随机`，**直接复制到剪贴板**，默认 31 天 |
| 续期 / 改到期 | 选「有效天数」或勾「永久」，实时预览到期时间；双击任意行也能打开 |
| 停用 / 恢复 | 一键切换 `revoked` |
| 删除 | 二次确认（提示：只是临时停用请用"停用"） |
| 立即同步到云 | 手动 deploy + 回验线上 |
| 改完自动同步 | 勾上（默认）后每次操作自动 deploy 并验证，结果显示在底部状态栏 |

> 这是**内部管理工具**，不要跟着软件发给客户（`build.ps1` 只打包 `run_app.py`，不含 `scripts/`）。


---

## 7. 部署后验收（逐条打勾）

### 7.1 服务端（不需要客户端）

```powershell
# 1) 健康检查
Invoke-RestMethod "https://你的base/health"

# 2) 正确密码
Invoke-RestMethod -Method Post -Uri "https://你的base/activate" `
  -ContentType "application/json" `
  -Body '{"password":"RH-2026-001-mj45","machine":"test-machine"}'

# 3) 错误密码 → 必须 401 且 error="密码不存在"
Invoke-RestMethod -Method Post -Uri "https://你的base/activate" `
  -ContentType "application/json" `
  -Body '{"password":"WRONG","machine":"test-machine"}'
```

> ⚠️ **PowerShell 里不要用 `curl -d '{"password":...}'`**：PowerShell 5.1 会吃掉 JSON 里的双引号，
> 服务端收到 `{password:...}` 会报 `body 不是 JSON`。用上面的 `Invoke-RestMethod`，
> 或把 JSON 写进文件再 `curl --data-binary "@payload.json"`。

| 检查项 | 期望 |
| --- | --- |
| `/health` | `{"ok":true,...}` |
| 正确密码 | 200 + 129~136 字符 token |
| 错误密码 | 401 + `密码不存在` |
| 换机器验证 | 拿 token + 别的 machine 调 `/verify` → 403 `token 与这台机器不匹配` |
| 停用 | 把该密码 `revoked` 改 true 部署后，`/verify` → 403 |

### 7.2 客户端

1. `配置.ini` 填 `LICENSE_SERVER=<base>`
2. 启动 exe → 弹「需要授权」→ 输密码 → 提示「授权成功」→ 进主界面
3. 关掉重开 → 不再要求（14 天内）
4. 删 `%APPDATA%\RHOutfitTool\license.json` → 重新要求输密码

### 7.3 自动测试（本地、不花钱）

```bash
python3 tests/test_license.py
```

11 项应全绿，覆盖：未激活拒绝、错误密码、停用、到期、激活成功、缓存、换机器、网关重试、停用后清缓存。

---

## 8. 故障排查

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 客户端提示「本机还没有授权」 | `LICENSE_SERVER` 空或拼错 | 检查 `配置.ini`，结尾不要 `/` |
| `HTTP 404` | 函数 URL 没建好，或路径最后一段不是 `activate`/`verify`/`health` | 函数 → 触发管理里确认有一条 HTTP 触发器 |
| `HTTP 401/403` 且非业务错误 | 函数 URL 开了鉴权 | 触发器改成「免鉴权 / NONE」 |
| `服务端没配 LICENSE_SECRET` | 环境变量未生效 | 重新保存，等 1 分钟 |
| `密码不存在` | 密码表没传 / 拼错 | 控制台确认 `passwords.json` 存在 |
| `body 不是 JSON` | 用 curl 在 PowerShell 里测 | 改 `Invoke-RestMethod` |
| 换 `SECRET` 后全员失效 | token 签名变了 | 预期行为，让用户重输密码 |
| 502/504 | 函数超时或路径错 | 超时调到 3s，路径核对 |

---

## 9. 安全红线

1. `LICENSE_SECRET` 只放环境变量，≥48 位随机，**不要**出现在代码/文档/聊天里。
2. `passwords.json` 已在 `.gitignore`（`server/passwords.json`），不要强制提交。
3. 函数 URL 触发器保持「免鉴权 / NONE」，安全性由 `LICENSE_SECRET` + 密码表保证；不要为了"更安全"去开密钥鉴权，客户端会 401。
4. 密码表泄露 = 所有人可用；泄露后立即换 `LICENSE_SECRET`（强制全员重新激活）+ 换密码。
5. 这套方案的定位是**防止 exe 被随手转发**，不防逆向破解。真实商业授权需要服务端下发业务数据，不是这套。
6. `dist/license-deploy.zip` 内含全量密码表，只用于部署。**不放进任何对外分发物**（群文件、网盘、邮件附件），发版时只发 `换装批量生产器-v*.zip`。详见 §2.1。

---

## 10. 附录

### 10.1 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `LICENSE_SECRET` | 空（不配则拒绝服务） | HMAC 密钥 |
| `LICENSE_PASSWORDS` | 代码同目录 `passwords.json` | 密码表路径 |

> `LICENSE_TTL_DAYS` 已废弃。有效期只有一个来源：密码表里的 `expires`。

### 10.2 客户端相关

| 项 | 值 |
| --- | --- |
| 配置项 | `配置.ini` → `LICENSE_SERVER` |
| 缓存文件 | `%APPDATA%\RHOutfitTool\license.json` |
| 机器码 | `sha256(MachineGuid + 主机名)[:32]` |
| 失败策略 | 连续 3 次输错 → 退出 |

### 10.3 与 RH Key 的关系

| 层 | 控制什么 | 配置位置 |
| --- | --- | --- |
| 授权密码 | 谁能打开软件 | `LICENSE_SERVER` + 服务端密码表 |
| RH Key | 跑任务花谁的钱 | `配置.ini` → `RH_API_KEY` |

两层都要开：只开密码，用户跑不了任务；只开 Key，谁拿到 exe 都能刷余额。

### 10.4 成本

云函数：每月 40 万 GB·秒 + 100 万次调用免费；API 网关：100 万次调用免费。
按 100 人、每天各启动 2 次估算，**月成本 ¥0**。
