# 授权服务 · 部署交接文档

> **给执行方（另一个模型 / 另一个人）看的完整说明。**
> 目标：把 `license_api.py` 部署成一个公网 HTTPS 接口，并让桌面软件在启动时用「分配的密码」换取授权。
> 本文自带协议规格、文件清单、验收标准，不依赖任何对话上下文。

---

## 0. 执行方须知（先看这一节）

**你要做的只有三件事**：上传两个文件到云函数 → 配 3 个环境变量 → 建 API 网关路由。

**必须遵守的约束**：

| 约束 | 原因 |
| --- | --- |
| 不要修改协议字段名（`password` / `machine` / `token` / `action`） | 桌面客户端已按此实现，改了全部失效 |
| 不要修改 token 的 HMAC 算法与字段名（`p` / `m` / `e`） | 客户端缓存里的旧 token 会验不过 |
| 不要把密码表提交进 Git | 表里是真实发放的密码 |
| 不要把 `LICENSE_SECRET` 写进代码 | 泄露 = 别人可自签 token |
| API 网关**鉴权必须选「免鉴权」** | 否则客户端 401 |

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
- 客户端联网失败时按 token 有效期离线放行（默认 14 天）。

---

## 2. 文件清单

仓库根目录：`D:\批量视频任务设计区`

| 文件 | 大小 | 作用 | 是否上传到服务端 |
| --- | --- | --- | --- |
| `server/license_api.py` | 8.6 KB | 服务端全部逻辑，零第三方依赖 | ✅ 必须 |
| `server/passwords.json` | 0.6 KB | 密码表（真实发放的密码） | ✅ 必须 |
| `server/passwords.example.json` | 0.5 KB | 密码表模板 | ❌ 仅参考 |
| `dist/license-deploy.zip` | 9 KB | 已打好的上传包（含上面两个文件） | ✅ 直接传这个 |
| `app/license.py` | 4.5 KB | 客户端：激活、缓存、机器码 | ❌ 已打包进 exe |
| `app/startup.py` | 1.3 KB | 启动门禁（弹框输密码） | ❌ 同上 |
| `tests/test_license.py` | 5 KB | 11 项自动化验收 | ❌ 本地跑 |

`license-deploy.zip` 的结构（文件在 zip 根目录，**不能套文件夹**）：

```
license-deploy.zip
├── license_api.py
└── passwords.json
```

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
| 403 | 密码已到期 | `expires` 已过 |
| 403 | 密码已被停用 | `revoked: true` |
| 403 | 该密码已在另一台机器上激活 | `machine` 字段已绑定别的机器 |

### 3.2 `POST /verify`

```json
{ "action": "verify", "token": "eyJwIjoi...", "machine": "a1b2c3d4..." }
```

| 结果 | 说明 |
| --- | --- |
| `200 {"ok":true,"expire_at":...,"note":"..."}` | 授权有效 |
| `403 token 已过期` | 超过 `LICENSE_TTL_DAYS` |
| `403 token 与这台机器不匹配` | 缓存被拷到别的电脑 |
| `403 token 签名不对` | 换过 `LICENSE_SECRET`，需重新激活 |
| `403 密码已被停用 / 已被删除` | 密码表里改了 `revoked` 或删了条目 |

### 3.3 `GET /health`（可选，用来确认路由通）

```json
{ "ok": true, "service": "outfit-license", "ttl_days": 14 }
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
| `LICENSE_TTL_DAYS` | `14` | 建议 |
| `LICENSE_PASSWORDS` | `/var/user/passwords.json` | ✅ |

生成随机串（PowerShell）：

```powershell
-join ((48..57)+(65..90)+(97..122) | Get-Random -Count 48 | % {[char]$_})
```

### 4.3 API 网关触发器

函数 → **触发管理 → 创建触发器**：

| 项 | 值 |
| --- | --- |
| 触发方式 | API 网关 |
| API 服务 | 新建 |
| 请求方法 | POST（顺带勾 GET） |
| 发布环境 | `release` |
| 路径 | **`/license/activate`** |
| 鉴权 | **免鉴权** |

**再建第二条**，路径改成 `/license/verify`，其余同上。

> 也可以只建一条 `/license`（单路由），客户端会按 `body.action` 自动适配。
> 但**推荐建两条**，错误码更直观。
>
> ⚠️ 不要只建 `/license` 却让客户端去请求 `/license/activate` —— 那是网关层 404，函数根本不会被调用。
> 客户端已做自动重试，但首次上线仍建议按两条路由配，避免多绕一次。

保存后得到地址：

```
https://service-xxxxxxxx-1234567890.gz.apigw.tencentcs.com/release/license/activate
```

### 4.4 填入客户端的地址

把最后一段 `/activate` **去掉**，得到 base：

```
https://service-xxxxxxxx-1234567890.gz.apigw.tencentcs.com/release/license
```

这个 base 写进客户端的 `配置.ini` 的 `LICENSE_SERVER`。

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
| `expires` | 建议 | `YYYY-MM-DD` 或 `YYYY-MM-DD HH:MM`；不填 = 永久 |
| `revoked` | 否 | `true` = 立即停用 |
| `machine` | 否 | 填了锁死那台机器；留空 = 不锁 |

### 6.2 密码命名规范

`RH-<年份>-<三位序号>-<四位随机>`，例：`RH-2026-007-k3f9`。
好念、可追溯、不易被猜。

### 6.3 运维动作

| 动作 | 改什么 | 生效 |
| --- | --- | --- |
| 新增用户 | 加一条 | 重新部署后 |
| 停用某人 | `revoked: true` | 重新部署后，其下次启动即被拒 |
| 续期 | 改 `expires` | 重新部署后 |
| 换电脑 | 不用改，新机输同一密码 | 立即 |
| 换 `LICENSE_SECRET` | 环境变量 | **所有客户端需重新激活** |

改表流程：控制台 → 函数代码 → 打开 `passwords.json` → 改 → **部署**。

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
| `HTTP 404` | 网关没有该路径 | 建 `/license/activate`、`/license/verify` 两条 |
| `HTTP 401/403` 且非业务错误 | 网关开了鉴权 | 改成「免鉴权」 |
| `服务端没配 LICENSE_SECRET` | 环境变量未生效 | 重新保存，等 1 分钟 |
| `密码不存在` | 密码表没传 / 拼错 | 控制台确认 `passwords.json` 存在 |
| `body 不是 JSON` | 用 curl 在 PowerShell 里测 | 改 `Invoke-RestMethod` |
| 换 `SECRET` 后全员失效 | token 签名变了 | 预期行为，让用户重输密码 |
| 502/504 | 函数超时或路径错 | 超时调到 3s，路径核对 |

---

## 9. 安全红线

1. `LICENSE_SECRET` 只放环境变量，≥48 位随机，**不要**出现在代码/文档/聊天里。
2. `passwords.json` 已在 `.gitignore`（`server/passwords.json`），不要强制提交。
3. API 网关保持「免鉴权」，安全性由 `LICENSE_SECRET` + 密码表保证；不要为了"更安全"去开密钥鉴权，客户端会 401。
4. 密码表泄露 = 所有人可用；泄露后立即换 `LICENSE_SECRET`（强制全员重新激活）+ 换密码。
5. 这套方案的定位是**防止 exe 被随手转发**，不防逆向破解。真实商业授权需要服务端下发业务数据，不是这套。

---

## 10. 附录

### 10.1 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `LICENSE_SECRET` | 空（不配则拒绝服务） | HMAC 密钥 |
| `LICENSE_TTL_DAYS` | `14` | token 有效期（天） |
| `LICENSE_PASSWORDS` | 代码同目录 `passwords.json` | 密码表路径 |

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
