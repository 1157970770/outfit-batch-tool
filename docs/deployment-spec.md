# 部署任务书：让客户端拿到可用配置

> **给执行方（另一个 AI）的施工说明。**
> 目标：把服务端更新到最新版并下发运行配置，使最终用户拿到 zip、输入密码后**直接能用**（不需要任何本地配置文件）。
>
> 仓库：`D:\批量视频任务设计区`　服务地址：`https://1463907213-3ifuebn6ih.ap-guangzhou.tencentscf.com`

---

## 0. 验收标准（做到这几条才算完成）

| # | 检查 | 期望 |
| --- | --- | --- |
| 1 | `GET /health` | `200 {"ok":true,"service":"outfit-license",...}` |
| 2 | `POST /activate` | 返回里**多出 `config` 字段**，且 `config.rh_api_key` 非空 |
| 3 | 客户端首次启动 | 输密码后状态灯显示 `配置：已下发`、`模板清单：N 个` |
| 4 | 跑 1 条小批量 | 真的生成出视频（花 1 条的钱） |
| 5 | 本地不出现明文 | `%APPDATA%\RHOutfitTool\license.json` 以 `dpapi:` 开头，搜不到 Key |

第 2 条是核心：**现在的线上函数是老版本，`/activate` 不返回 `config`**，所以客户端拿不到 Key，界面会显示"配置：未下发"。

---

## 1. 现状盘点

| 项 | 状态 |
| --- | --- |
| 云函数 `outfit-license` | ✅ 已存在并可用（ap-guangzhou） |
| 函数 URL | `https://1463907213-3ifuebn6ih.ap-guangzhou.tencentscf.com` |
| 环境变量 | ✅ `LICENSE_SECRET`、`LICENSE_PASSWORDS` 已配 |
| 密码表 | ✅ 3 条（见 `server/passwords.json`） |
| **函数代码版本** | ⚠️ **旧版**：不含 `config` 下发逻辑 |
| **config.json** | ❌ 不存在，需新建并上传 |
| 客户端 | ✅ v0.8 已打包（`dist\换装批量生产器-v0.8-*.zip`），逻辑已改好 |

也就是说：**只差"把新代码和 config.json 传上去"这一步。**

---

## 2. 需要向用户索取的输入

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `rh_api_key` | ✅ | RunningHub 企业级共享 Key（32 位）。**不要在对话里回显** |
| `manifest_url` | ✅ | 500 个模板的 `manifest.json` 公网地址 |
| `cos.bucket` | 可选 | 已知为 `video-input-1463907213` |
| `cos.region` | 可选 | `ap-guangzhou` |
| `cos.prefix` | 可选 | 如 `prod` |
| `cos.enabled` | 可选 | 是否归档到 COS |

拿不到 `manifest_url` 也能先上线，但客户端会显示"模板清单：未下发"，跑不了任务。

---

## 3. 施工步骤

### 3.1 更新服务端代码

确认 `server/license_api.py` 是最新版（含 `CONFIG_PATH`、`load_runtime_config()`、`/activate` 返回 `config`）。
自检：

```bash
grep -n "load_runtime_config\|LICENSE_CONFIG" server/license_api.py
```

应当能看到函数定义与 `CONFIG_PATH`。

### 3.2 新建 `server/config.json`

在 `server/` 下创建（内容为明文，只存活在服务端）：

```json
{
  "rh_api_key": "填用户的 32 位 Key",
  "manifest_url": "https://cdn.xxx.com/manifest.json",
  "cos": {
    "enabled": true,
    "bucket": "video-input-1463907213",
    "region": "ap-guangzhou",
    "prefix": "prod",
    "domain": ""
  },
  "notice": ""
}
```

- 没有 COS 就把 `enabled` 设 `false`，其余留空。
- `notice` 会显示在客户端顶部横幅，可留空。

> 仓库里已经放了一份 `server/config.json` 骨架（COS 桶、地域已填好），**只需补 `rh_api_key` 和 `manifest_url` 两处**。
> 该文件已被 `.gitignore` 排除，不会被提交。

### 3.3 打包并上传

推荐用仓库里的脚本（需要本机已 `tccli auth login`）：

```bash
python3 scripts/license_deploy.py pack      # 打成 dist/license-deploy.zip
python3 scripts/license_deploy.py deploy    # 部署到云函数
python3 scripts/license_deploy.py url       # 打印客户端要用的地址
```

手动也行：把 `license_api.py`、`passwords.json`、`config.json` **三个文件**放进 zip 根目录，
在云函数控制台「函数代码 → 上传 zip」后点「部署」。

### 3.4 确认环境变量

函数配置 → 环境变量，必须有：

| 变量 | 值 |
| --- | --- |
| `LICENSE_SECRET` | 已存在，**不要改**（改了所有客户端要重新激活） |
| `LICENSE_TTL_DAYS` | `14` |
| `LICENSE_PASSWORDS` | `/var/user/passwords.json` |
| `LICENSE_CONFIG` | `/var/user/config.json` |

`LICENSE_CONFIG` 若缺失，服务端会回退到"与代码同目录的 config.json"，通常也能读到；
显式配置更稳。

### 3.5 验证（按第 0 节逐条打勾）

```powershell
$base = "https://1463907213-3ifuebn6ih.ap-guangzhou.tencentscf.com"

# 1) 健康检查
Invoke-RestMethod "$base/health"

# 2) 激活（用密码表里真实存在的一条）
Invoke-RestMethod -Method Post -Uri "$base/activate" -ContentType "application/json" `
  -Body '{"password":"RH-2026-001-mj45","machine":"deploy-check"}'
```

第 2 条必须能看到 `config` 字段且 `rh_api_key` 有值。看不到就是代码没更新或 config.json 没传上去。

> PowerShell 里**不要**用 `curl -d '{"password":"..."}'`：双引号会被吃掉，服务端报 `body 不是 JSON`。
> 用上面的 `Invoke-RestMethod`，或 `curl --data-binary "@payload.json"`。

---

## 4. 交付给用户

部署完成后，用户需要的只有**一个 zip**：

```
dist\换装批量生产器-v0.8-<日期>.zip
```

说明三句话：

1. 解压到任意可写目录（如 `D:\工具\`），**双击 exe**；
2. 输入分配的密码；
3. 选一次输出目录，即可开始。

不需要改任何文件——配置全部由服务端下发。

---

## 5. 常见故障

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 客户端显示"配置：未下发" | `/activate` 没返回 config | 代码是旧版，或 config.json 没传 |
| "模板清单：未下发" | `manifest_url` 为空或打不开 | 补 config.json，确认 URL 公网可访问 |
| 激活返回 `密码不存在` | 密码表没传 / 拼错 | 控制台确认 `passwords.json` 存在 |
| `服务端没配 LICENSE_SECRET` | 环境变量丢了 | 重新配置，等 1 分钟 |
| 客户端报 `token 签名不对` | `LICENSE_SECRET` 变过 | 让用户重输密码（或删本地 `license.json`） |
| 404 / 502 | 函数 URL 变了 | `scripts/license_deploy.py url` 重新取地址，客户端地址是**内置常量**，变了要重新打包 |

---

## 6. 红线

1. **不要改协议字段名**：`password` / `machine` / `token` / `action` / `config` / `rh_api_key` / `manifest_url` / `cos`。
2. **不要改 `LICENSE_SECRET`**：一改所有用户要重新激活。
3. **不要把 `config.json` / `passwords.json` 提交到 Git 或贴进对话**。
4. **不要动客户端的内置地址** `app/config.py` 的 `SERVICE_URL`，除非函数 URL 真的变了（那要重新打包）。
5. **不要为了"省事"把 RH Key 写进客户端代码**——现在是服务端下发，写进客户端等于前功尽弃。

---

## 7. 完成后回传

一句话即可：

> 已更新到最新版，`/activate` 返回 `config.rh_api_key`（长度 32），`/health` 正常；密码表 3 条可用；客户端 zip 未变。

如果第 3 条验收（真跑 1 条）没做，明确写"未做真跑验证，仅完成服务端部署"。
