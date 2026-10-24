# 授权（密码）上线手册

> 目标：拿到 exe 的人，**必须输入你分配的密码**才能用。密码可单独发、可单独停用，换电脑不能复用。
>
> 全套 5 个文件，没有数据库、没有第三方依赖，30 分钟内能上线。

## 0. 全景

```
你（控制台）                   你的服务器（腾讯云函数）
passwords.json  ──部署──▶  POST /activate  {password, machine}
                                  │  校验密码表
                                  ▼
                          返回 token（HMAC 签名，绑机器，14 天）
                                  │
                     ◀────────────┘
客户端 exe  ── 首次输密码 ──▶ 存到 %APPDATA%\RHOutfitTool\license.json
以后每次启动：本地验签 + 联网复核（服务端说停用就立刻失效）
```

| 文件 | 作用 |
| --- | --- |
| `server/license_api.py` | 服务端，单文件；既能本地跑也能上云函数 |
| `server/passwords.example.json` | 密码表模板 |
| `app/license.py` | 客户端：激活、缓存、机器码 |
| `app/startup.py` | 启动门禁（弹框输入密码） |
| `tests/test_license.py` | 10 项自动化验证 |

---

## 1. 先在本地跑通（5 分钟，不花一分钱）

上线前先确认这套东西在你机器上是通的：

```bash
# 终端 1：起服务
set LICENSE_SECRET=随便一串很长的随机字符
set LICENSE_PASSWORDS=server\passwords.json
copy server\passwords.example.json server\passwords.json
python3 server\license_api.py --serve 8099
```

```powershell
# 终端 2：造一个密码、验一次（PowerShell 用这个，别用 curl，见下方说明）
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8099/activate" `
  -ContentType "application/json" `
  -Body '{"password":"RH-2026-001","machine":"test-machine"}'
```

返回 `ok=True` 和一个 136 字符的 token 就说明服务端没问题。
返回 `密码不存在`，说明 `passwords.json` 里没有 `RH-2026-001`（模板里默认有，检查路径）。

> ⚠️ **PowerShell 里不要用 `curl -d '{"password":"..."}'`**：PowerShell 5.1 会吃掉 JSON 里的双引号，
> 服务器收到的是 `{password:RH-2026-001,...}`，于是报 `body 不是 JSON`。
> 要么用上面的 `Invoke-RestMethod`，要么把 JSON 写进文件再 `curl --data-binary "@payload.json"`。

> 这一步只验证服务端。客户端联调放第 5 节。

---

## 2. 部署到腾讯云函数（SCF）

### 2.1 打包上传

SCF 里代码目录是 `/var/user`，**只读**。所以两份文件要一起传：

```
license.zip
├── license_api.py
└── passwords.json      ← 由 passwords.example.json 改名而来
```

Windows 下打包：选中这两个文件 → 右键 → 压缩为 zip（**不要**把外层文件夹也压进去）。

### 2.2 创建函数

进 **云函数 SCF 控制台 → 函数管理 → 新建函数**：

| 项 | 填什么 |
| --- | --- |
| 创建方式 | 从头开始 / 模板创建均可 |
| 函数名称 | `outfit-license` |
| 运行环境 | Python 3.9 或更高（选能选到的最新 3.x） |
| 执行方法 | `license_api.main_handler` |
| 提交方法 | 上传 zip 包（不要选"在线编辑"后再传，容易丢 passwords.json） |
| 高级配置 → 内存/超时 | 128 MB / 3 秒足够 |

创建后进函数 → **函数代码**，确认左侧能看到 `license_api.py` 和 `passwords.json` 两个文件。

### 2.3 配环境变量

函数 → **函数配置 → 编辑 → 环境变量**，加三条：

| 变量名 | 值 | 说明 |
| --- | --- | --- |
| `LICENSE_SECRET` | 一串 40 位以上随机字符 | **必改**，泄露 = 别人能自签 token |
| `LICENSE_TTL_DAYS` | `14` | token 有效期；想吊销更快就设 `1` |
| `LICENSE_PASSWORDS` | `/var/user/passwords.json` | 密码表在代码目录里的路径 |

生成随机串（PowerShell）：

```powershell
-join ((48..57)+(65..90)+(97..122) | Get-Random -Count 48 | % {[char]$_})
```

### 2.4 对外暴露：函数 URL（⚠️ 别再照 API 网关配）

> **2026-10 实测**：API 网关已**停止售卖**，新建 service 直接报
> `FailedOperation.LimitingResourceCreated：API网关产品已停止售卖，无法新建资源`。
> 改用 SCF 自带的**函数 URL**——路径透传，**一个地址同时服务 `/activate`、`/verify`、`/health`**，
> 不用像网关那样一条一条建路由。

函数 → **触发管理 → 创建触发器**：

| 项 | 填什么 |
| --- | --- |
| 触发类型 | **HTTP**（就是函数 URL） |
| 触发器名称 | `licenseUrl` |
| 鉴权 | **免鉴权（NONE）** |
| 公网访问 | 开启 |

保存后得到地址，形如：

```
https://1463907213-<随机8位>.ap-guangzhou.tencentscf.com
```

**客户端要填的 `LICENSE_SERVER` 直接用这个域名**，结尾不要带 `/`：

```ini
LICENSE_SERVER=https://1463907213-<随机8位>.ap-guangzhou.tencentscf.com
```

客户端会自己在后面拼 `/activate` 或 `/verify`，函数按路径最后一段分派。

> 写成 `https://.../license` 也能用（实际请求 `/license/activate`），两种都通。
> 误填成带 `/activate` 的完整地址也不会坏：客户端把动作放在 `body.action` 里，
> 函数**优先看 body.action**，其次才看路径末段。

> ⚠️ 鉴权必须选「免鉴权 / NONE」。开了密钥鉴权客户端会 401，这套方案的安全性
> 靠 `LICENSE_SECRET` 和密码表，不靠触发器鉴权。

> 上线前先用浏览器打开 `https://你的地址/health`，返回 `{"ok":true,...}` 就说明链路通了。


---

## 3. 验证服务端通了

**PowerShell（推荐）**

```powershell
Invoke-RestMethod -Method Post `
  -Uri "https://你的地址/activate" `
  -ContentType "application/json" `
  -Body '{"password":"RH-2026-001-mj45","machine":"test-machine"}'
```

**cmd / bash**

```bash
curl -X POST https://你的地址/activate \
  -H "Content-Type: application/json" \
  -d "{\"password\":\"RH-2026-001-mj45\",\"machine\":\"test-machine\"}"
```

**最保险（任何 shell 都行）**：把 JSON 存成 `payload.json`，再

```bash
curl -X POST https://你的地址/activate -H "Content-Type: application/json" --data-binary "@payload.json"
```

三种返回的含义：

| 返回 | 含义 | 处理 |
| --- | --- | --- |
| `{"ok":true,"token":"..."}` | 成功 | 继续第 5 节 |
| `{"ok":false,"error":"密码不存在"}` | 密码表里没这个密码 | 检查 `passwords.json` 是否真的打进了 zip |
| `{"ok":false,"error":"服务端没配 LICENSE_SECRET"}` | 环境变量没生效 | 重新保存配置，等 1 分钟再试 |
| 404 | 函数 URL 没绑好 | 函数 → 触发管理里确认有一条 HTTP 触发器 |
| 502 / 504 | 函数超时或报错 | 看函数日志；超时调到 5 秒 |


---

## 4. 发密码、停用、续期

### 4.1 密码表长什么样

```json
{
  "passwords": {
    "RH-2026-001": { "note": "张三 / 设计部",   "expires": "2027-12-31", "revoked": false },
    "RH-2026-002": { "note": "李四 / 外包团队", "expires": "2026-12-31", "revoked": false },
    "RH-2026-003": { "note": "王五（已离职）",   "expires": "2027-12-31", "revoked": true  }
  }
}
```

| 字段 | 必填 | 作用 |
| --- | --- | --- |
| `note` | 建议 | 备注，只给你自己看，激活成功时会回显 |
| `expires` | 建议 | 到期日，支持 `2026-12-31` 或 `2026-12-31 23:59`；不填=永久 |
| `revoked` | 否 | `true` = 立即停用 |
| `machine` | 否 | 填了就锁死那台机器；留空=不锁，靠 token 里的机器码绑定 |

### 4.2 密码怎么起名

别用 `123456`。建议 **`RH-年份-序号-4位随机`**：`RH-2026-007-k3f9`。
既好念、又能看出是哪一年发的、还不容易被猜到。

### 4.3 修改后怎么生效

1. 控制台 → 函数代码 → 打开 `passwords.json` → 改 → **保存**
2. 点 **部署**（有的版本叫"部署代码"）
3. 客户端**下次启动**就按新表校验（服务端每次请求都重读文件，不用重启函数）

### 4.4 三种常见操作

| 操作 | 改什么 | 生效时间 |
| --- | --- | --- |
| 停用某人 | `revoked: true` → 部署 | 他下次启动立刻失效（token 缓存也会被服务端否掉） |
| 续期 | 改 `expires` → 部署 | 下次启动 |
| 换电脑 | 不用改；新机器输同一密码即可（token 按机器码重签） | 立即 |

> 想彻底禁止某台机器：把该密码 `revoked` 设 true，再给新机器发一个新密码。

---

## 5. 客户端：打包与分发

### 5.1 打包时把地址写进配置

**默认就是强制授权**：程序启动必须先通过密码校验，没有"留空跳过"这回事。

地址有两个来源，优先级从高到低：

| 顺序 | 来源 | 说明 |
| --- | --- | --- |
| 1 | `配置.ini` 的 `LICENSE_SERVER` | 用户可改，适合换服务器 |
| 2 | 程序内置 `EMBEDDED_LICENSE_SERVER`（`app/main.py` 顶部） | 防止"删掉那行 = 免授权" |

两者都为空时，**程序直接拒绝启动**并提示"未配置授权服务"（失败关闭，不会静默放行）。

正式分发建议两个都填：内置一份兜底，配置文件允许改成新地址。

```ini
# 授权服务地址；留空则使用程序内置地址；两者都空将无法启动
LICENSE_SERVER=https://1463907213-xxxxxxxx.ap-guangzhou.tencentscf.com
```

> 开发调试时如果还没部署服务：源码运行可临时设环境变量 `RH_DEV_NO_LICENSE=1` 跳过校验。
> **打包后的 exe 不认这个变量**，所以不会成为分发版本的绕过口子。

打包：

```bash
scripts\build.bat
```

产物 `dist\换装批量生产器-v0.1-日期.zip` 里就会带上这个 `配置.ini`。
**注意**：`配置.ini` 里有 `RH_API_KEY`，发出去前确认这是你要给人用的那把。

### 5.2 用户首次使用

1. 解压 → 双击 `换装批量生产器.exe`
2. 弹「需要授权」→ 输入你给的密码 → `授权成功`
3. 进主界面，之后 14 天内不再问

密码输错会提示原因（`密码不存在` / `密码已到期` / `密码已被停用` / `该密码已在另一台机器上激活`），连错 3 次程序退出。

### 5.3 缓存与重置

| 项 | 位置 |
| --- | --- |
| token 缓存 | `%APPDATA%\RHOutfitTool\license.json` |
| 删掉它 | 下次启动会重新要求输密码（相当于"退出登录"） |

---

## 6. 排错表

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 弹框一直说"本机还没有授权" | `LICENSE_SERVER` 没填或拼错 | 检查 `配置.ini`，地址结尾不要带 `/` |
| `HTTP 404` | 函数 URL 没绑好，或路径末段不是 `activate`/`verify`/`health` | 函数 → 触发管理确认有 HTTP 触发器 |
| `HTTP 404`，但函数日志里没有这次调用 | 请求根本没进函数 | 触发器被停用，或域名拼错 |
| 返回 `{"ok":false,"error":"未知接口"}` | 请求进到函数了，但路径末段不认识 | 地址末尾多了/少了一段，base 用域名即可 |
| `HTTP 401` / `403` | 触发器开了鉴权 | 改成"免鉴权 / NONE" |
| `服务端没配 LICENSE_SECRET` | 环境变量没保存成功 | 重新保存，等 1 分钟 |
| `密码不存在` | 密码表没传上去 / 拼错 | 控制台看 `passwords.json` 是否存在 |
| `token 签名不对` | 换过 `LICENSE_SECRET` | 用户删掉本地 `license.json` 重新激活 |
| `body 不是 JSON`（用 curl 测时） | PowerShell 吃掉了 JSON 里的双引号 | 改用 `Invoke-RestMethod` 或 `--data-binary "@payload.json"` |
| 换电脑后仍能用 | 正常：token 绑机器，旧机器到期前仍可用 | 需要立刻断：`revoked: true` |

---

## 7. 成本

- 云函数：每月前 40 万 GB·秒 + 100 万次调用免费额度内，**这套 100 人用也基本是 ¥0**
- 函数 URL：不额外计费（API 网关已停售，本来也不用买了）
- 日志服务 CLS：按量计费，函数日志量极小，通常在免费额度内
- 结论：**不花钱**，除非你做到很大规模

---

## 8. 这套方案的边界

| 限制 | 影响 | 缓解 |
| --- | --- | --- |
| 不防逆向 | 有人改 exe 跳过校验 | 只防"随手转发"，防不了专业破解 |
| 吊销有延迟 | 最多一个 token 有效期 | `LICENSE_TTL_DAYS` 设 1~7 |
| 断网可用 | 已激活的机器可离线跑到 token 到期 | 同上，把有效期调短 |
| 密码表明文 | 泄露 = 所有人都能用 | 别提交 Git；只放函数里 |
| 没有用量统计 | 不知道谁跑了多少条 | 需要时在 `/activate` 里加一行写 COS 日志 |

## 9. 和 RH Key 的关系

两层，互不替代：

- **RH Key**（`配置.ini` 的 `RH_API_KEY`）：决定"跑起来花谁的钱"
- **授权密码**：决定"谁能打开这个软件"

分发给别人时两层都开。只开密码不开 Key，用户跑不了任务；只开 Key 不开密码，谁拿到 exe 都能刷你的余额。
