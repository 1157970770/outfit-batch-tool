# 授权服务对接文档

> 面向：要让「换装批量生产器」真正走密码授权的执行者（人或模型）。
> 现状一句话：**服务端已经跑起来了，客户端代码也已经接好了，唯一缺的是"地址"没填**——所以现在跑起来会弹「未配置授权服务」并退出。

---

## 0. 现状诊断（2026-10-24 实测）

| 环节 | 状态 | 证据 |
| --- | --- | --- |
| 云函数 `outfit-license` | ✅ 已上线 | ap-guangzhou，函数 URL `https://1463907213-3ifuebn6ih.ap-guangzhou.tencentscf.com`，5 项验收通过 |
| 密码表 | ✅ 3 条可用 | `server/passwords.json`，真实表只在本地 + 云端各一份 |
| 客户端校验代码 | ✅ 已接 | `app/main.py:887-900` 调 `ensure_license()`；`app/startup.py` 弹框；`app/license.py` 激活/缓存 |
| 配置字段 | ✅ 已定义 | `app/config.py:28` `LICENSE_SERVER` |
| **授权地址** | ✅ **已填**（v0.6） | `配置.ini` 与 `app/main.py:45` 的 `EMBEDDED_LICENSE_SERVER` 都指向函数 URL |
| 分发安装包 | ✅ 已打（v0.6） | `dist\换装批量生产器-v0.6-20261024.zip`，包内 `配置.ini` 已预填地址、`RH_API_KEY` 为空 |

**结论**：链路已通。本机实测：激活 → `授权有效（到期 2027-12-31）`；换机器 → `token 与这台机器不匹配`；清缓存 → 重新要求输密码。

---

## 1. 一次授权到底发生了什么

```
用户启动 exe
   │
   ├─ 读 配置.ini / 内置地址          ← 第 2 节：地址从哪来
   │
   ├─ 地址为空？ ──是──► 弹「未配置授权服务」→ 退出(2)
   │
   ├─ 本地有缓存票？ ──否──► 弹框要密码 ──► POST /activate ──► 存票
   │                                  │
   │                          密码不存在/已到期/已停用 → 提示后重输，3 次失败退出
   │
   └─ 有票：POST /verify ──► 服务端查表（到期/停用/机器）──► 放行
                       └──► 连不上：上次成功核验 7 天内放行，超过要求联网
```

票存在 `%APPDATA%\RHOutfitTool\license.json`，里面**没有明文密码**。

---

## 2. 地址从哪来（优先级 + 生效条件）

`app/main.py:52`：

```python
def resolve_license_server(cfg):
    return (cfg.get("LICENSE_SERVER") or "").strip() or EMBEDDED_LICENSE_SERVER.strip()
```

| 优先级 | 来源 | 位置 | 谁能改 | 用途 |
| --- | --- | --- | --- | --- |
| 1（高） | `配置.ini` 的 `LICENSE_SERVER` | exe 同目录 | 客户可改 | 临时指向测试服 / 修地址 |
| 2 | `EMBEDDED_LICENSE_SERVER` 常量 | `app/main.py:45` | 只有你（要重新打包） | **正式发布写这里，客户删不掉** |
| 3 | 开发豁免 | 环境变量 `RH_DEV_NO_LICENSE=1` | 你 | 仅源码运行有效，打包版无效 |

两条都空 = **拒绝启动**（`return 2`）。这是故意的：不能靠"删掉那行"绕过授权。

---

## 3. 三种场景的操作

### A. 本机开发（源码运行，最常用）

在 **`D:\批量视频任务设计区\配置.ini`** 里把这行填上（已经有一行空的了，直接补等号后面）：

```ini
LICENSE_SERVER=https://1463907213-3ifuebn6ih.ap-guangzhou.tencentscf.com
```

然后用 `server/passwords.json` 里任意一条密码激活即可（现在有三条待分配）。

不想每次输密码、只想改代码：设环境变量 `RH_DEV_NO_LICENSE=1` 再跑（**仅源码有效**）。

### B. 发布给客户（打包）

```powershell
cd /d D:\批量视频任务设计区
scripts\build.bat -LicenseServer https://1463907213-3ifuebn6ih.ap-guangzhou.tencentscf.com
```

`build.ps1` 会用模板重写包内 `配置.ini` 并预填这一行（顺带保证 `RH_API_KEY` 为空、不夹带你的 Key）。

> 想做到"客户完全删不掉"：同时把 `app/main.py:45` 改成
> `EMBEDDED_LICENSE_SERVER = "https://1463907213-3ifuebn6ih.ap-guangzhou.tencentscf.com"`
> 再打包。这样即使 `配置.ini` 里那行被删，也仍然走授权。

### C. 客户那边已经装了、地址要改

让他打开 exe 同目录的 `配置.ini`，改 `LICENSE_SERVER` 那行，回软件点右上角「重载配置」——不用重装。

---

## 4. 代码位置索引

| 文件 | 位置 | 作用 |
| --- | --- | --- |
| `app/main.py` | `:38` | `from app.startup import ensure_license` |
| `app/main.py` | `:45` | `EMBEDDED_LICENSE_SERVER`（内置地址，当前为空） |
| `app/main.py` | `:52-54` | `resolve_license_server()`：配置优先、内置兜底 |
| `app/main.py` | `:887-900` | 启动门禁：无地址 → 拒启；有地址 → `ensure_license()` |
| `app/main.py` | `:749-759` | 界面右上角授权徽标（未配置 / 待激活 / 至 YYYY-MM-DD） |
| `app/startup.py` | `:17-45` | 弹框输密码，3 次失败退出 |
| `app/license.py` | `:28-40` | 机器码 `sha256(MachineGuid+主机名)[:32]` |
| `app/license.py` | `:96-110` | `activate()`：换票 + 写缓存 + 记 `last_ok` |
| `app/license.py` | `:113-160` | `status()`：本地票期 → 远程 `/verify` → 7 天救援窗口 |
| `app/config.py` | `:28` / `:56-59` | `LICENSE_SERVER` 默认值与模板注释 |
| `server/license_api.py` | 全文件 | 服务端：`/activate` `/verify` `/health`，零依赖 |
| `server/passwords.json` | — | 密码表（唯一真身，别分发） |
| `scripts/password_manager.py` | — | 图形化管理密码（双击 `scripts\密码管理器.bat`） |

---

## 5. 协议速查

Base = 上面的函数 URL，路径透传，POST + `application/json`。

| 接口 | 入参 | 成功 | 关键失败 |
| --- | --- | --- | --- |
| `POST /activate` | `password`, `machine` | `200 {ok, token, expire_at, days, note}` | `401 密码不存在` / `403 授权已到期…` / `403 密码已被停用` |
| `POST /verify` | `token`, `machine` | `200 {ok, expire_at, note}`，**续期后可能附带 `renewed_token`** | `403 授权已到期…` / `403 token 与这台机器不匹配` / `403 token 签名不对` |
| `GET /health` | — | `200 {ok, service, mode}` | — |

有效期全系统只有一处：密码表 `expires`（新增默认 31 天，带 `23:59` 表示到当天结束）。`/verify` 每次都回表复查，所以**密码到期 = 下次启动即被拒**，没有宽限。

---

## 6. 对接验收清单

服务端（一条命令跑完）：

```powershell
python3 scripts\license_deploy.py test
```

| 检查 | 期望 |
| --- | --- |
| `/health` | 200 |
| 正确密码 `/activate` | 200，token ≈140 字符 |
| 错误密码 | 401 `密码不存在` |
| 同机 `/verify` | 200 |
| 换机器 `/verify` | 403 `token 与这台机器不匹配` |

客户端（人工 4 步）：

1. 填好 `LICENSE_SERVER` → 启动 → 弹「需要授权」→ 输密码 → 提示「授权成功」
2. 关掉重开 → 不再要求输入（票有效期内）
3. 删 `%APPDATA%\RHOutfitTool\license.json` → 重新要求输密码
4. 在密码管理器里把该密码「停用」→ 重启软件 → 应被拒（`密码已被停用`）

---

## 7. 故障对照表

| 界面现象 | 真实原因 | 处理 |
| --- | --- | --- |
| 「未配置授权服务」+ 直接退出 | 配置和内置换地址都空 | 按第 3 节填地址 |
| 「本机还没有授权，请输入分配的密码」 | 首次运行 / 缓存被清 | 输密码 |
| 「密码不存在」 | 密码打错，或还没同步到云 | 密码管理器里核对，或点「立即同步到云」 |
| 「授权已到期，请联系管理员续期」 | 表里 `expires` 过了 | 密码管理器「续期」 |
| 「密码已被停用」 | `revoked: true` | 需要恢复就「停用/恢复」切回 |
| 「需要联网核验一次（已超过 7 天…）」 | 长时间连不上授权服务 | 检查网络 / 函数是否在跑 |
| 一直提示连接失败 | 地址写错（多了 `/`、少了 `https://`） | 地址只到域名，结尾不要斜杠 |
| 输密码后报「token 签名不对」 | 换过 `LICENSE_SECRET` | 预期行为：让用户重输一次密码 |

---

## 8. 红线

1. **不要动 `LICENSE_SECRET`**：一动全员失效，必须重新输密码。它只在云函数环境变量里，任何时候不要打印。
2. `server/passwords.json` 与 `dist/license-deploy.zip` **都不进任何对外分发物**。
3. `scripts/` 整个目录（含密码管理器）是内部工具，`build.ps1` 只打包 `run_app.py`，本来就不含它——别手动塞进去。
4. 协议字段名（`password` / `machine` / `token` / `action`）与 token 结构（`p` / `m` / `e`）不能改，改了老客户端全废。
5. 打包前确认包内 `配置.ini` 的 `RH_API_KEY` 为空（`build.ps1` 已强制重写成模板，别绕过它手工压 zip）。

---

## 9. 回滚

想让某台机器回到「不需要授权」：

- 单机临时：`配置.ini` 里把 `LICENSE_SERVER` 清空 —— **不行**，会拒绝启动（这是设计如此）。
- 正确做法：直接删掉 `%APPDATA%\RHOutfitTool\license.json` 让它重新激活，或换一条新密码。
- 要真的关掉整个授权功能：把 `app/main.py:887` 起的门禁块注释掉，改 `:45` 为空，重新打包 —— 属于改需求，不是运维动作。
