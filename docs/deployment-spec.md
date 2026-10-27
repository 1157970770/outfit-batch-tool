# 服务器部署与验收（现行版）

> 复核：**2026-10-26 13:50**，代码版本 **v0.9**。
> 本文原名「部署任务书（给执行方）」，旧内容（"线上还是旧版""`config.json` 不存在"）**已全部过期**，现按实测状态重写。
> 原理与协议看 [license-integration.md](license-integration.md)；本文只讲**怎么查、怎么改、怎么验**。

函数：`outfit-license`（ap-guangzhou）　地址：`https://1463907213-3ifuebn6ih.ap-guangzhou.tencentscf.com`

---

## 0. 现在的真实状态

| 项 | 状态 | 怎么确认 |
| --- | --- | --- |
| 云函数 | ✅ Active，`/health` 200 | `python3 scripts\license_deploy.py status` |
| 线上代码 | ✅ 与仓库 `server/license_api.py` **字节一致** | `dist\license-deploy.zip` 里的文件哈希与 `server\` 对得上 |
| 运行配置下发 | ✅ `/activate` 返回 `config`（百炼 Key、模型、清单地址、COS 双桶、子账号凭证都在） | `python3 scripts\license_deploy.py config` |
| 模板清单 | ✅ 已发布，**108 条**（目标 500） | `python3 tools\publish_manifest.py --verify-only` |
| **`wan.api_key`** | ❌ **空**（`rh_api_key` 也空） | 同上 `config` 命令，会打印「未填」 |
| 分发包 | v0.9（切官方 API 后升 v1.0） |

> 结论：**只差 `wan.api_key` 一处**。填完 `deploy` 一次，客户端「配置：未下发」就变成「已下发」。
> 过渡期同时保留 `rh_api_key`，旧客户端仍能启动；新客户端优先 `wan`。

---

## 1. 环境变量（控制台 → 函数配置）

| 变量 | 值 | 注意 |
| --- | --- | --- |
| `LICENSE_SECRET` | 已存在（48 位） | **绝对不要改**，改=所有客户端要重新激活 |
| `LICENSE_PASSWORDS` | `/var/user/passwords.json` | 密码表 |
| `LICENSE_CONFIG` | `/var/user/config.json` | 运行配置；缺省也能回退到代码同目录 |

> `LICENSE_TTL_DAYS` **已废弃并删除**：有效期只有一个来源 = 密码表里的 `expires`。
> 旧文档里"设成 14"的说法作废。

---

## 2. 改配置 → 部署 → 回验（三步，别跳）

```powershell
# ① 改 server\config.json（唯一真源；已被 .gitignore 排除）
#    wan.api_key / wan.model / wan.base_url / manifest_url / cos.* —— 只改需要改的字段

# ② 查本地填全了没（不打印 Key 值）
python3 scripts\license_deploy.py config

# ③ 打包 + 上传 + 等函数 Active（ZIP 必须含 license_api.py、passwords.json、config.json）
python3 scripts\license_deploy.py deploy

# ④ 再查线上是否真的下发了
python3 scripts\license_deploy.py config
```

期望最后三行：

```
[config] 线上 wan.api_key : 已下发（长度 35）
[config] 线上 wan         : model=wan3.0-video base=https://.../api/v1
[config] 线上 manifest_url : https://...
[config] 结论: 配置完整，客户端可用
```

### 两个必须知道的坑

1. **`tccli` 要写 `C:\Users\<你>\.tccli\log\tccli.log`**。在受限沙箱里跑 `deploy` 会
   `PermissionError`，要在正常终端（或非沙箱会话）执行。
2. **`deploy` 不是即时的**：脚本会等函数回到 `Active` 才算生效；客户端仍是**下次启动**才拿到新配置。

---

## 3. 验收（服务端 5 项，一条命令）

```powershell
python3 scripts\license_deploy.py test      # health / 激活 / 错密码 / 同机 / 换机
python3 scripts\license_deploy.py config    # 配置是否完整下发
```

| # | 检查 | 期望 |
| --- | --- | --- |
| 1 | `/health` | 200 + `{"ok":true,...}` |
| 2 | `/activate` 的 `config.wan.api_key` / `model` / `base_url` | 非空；Key 形如 `sk-...` |
| 3 | `/activate` 的 `config.cos.secret_id` / `secret_key` | 36 / 32 位，均非空 |
| 4 | 清单地址 | 浏览器打开能下载 JSON，条数与 `build\manifest\manifest.json` 一致 |
| 5 | 客户端首次启动 | 状态灯「配置：已下发」「模板清单：108 个」 |

**不做的第 6 项**：真跑一条出视频（要花钱）。没做就明确写"未做真跑验证"，别默认它通。

---

## 4. 故障对照

只列部署阶段会遇到的；客户端侧的现象看 [license-integration.md](license-integration.md) §7。

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `缺少文件：server\config.json` | 文件不在（`copy server\config.example.json server\config.json`） | 建好再 deploy，脚本**不会**静默少传 |
| 线上 `wan.api_key` 仍为空 | 改了本地忘了 deploy，或 deploy 没到 Active | 重跑 `config` 对比；看函数状态 |
| `tccli 调用失败` | 未登录 / 无该函数权限 | `tccli auth login --profile agent1` |
| `deploy` 后客户端没变 | 客户端没重启 | 关掉重开；仍不对就查票是否过期 |
| `/activate` 不返回 `config` | 上传的是旧代码，或 zip 里没 `config.json` | `python3 scripts\license_deploy.py pack` 看打进哪三个文件 |

---

## 5. 红线

1. **不要改 `LICENSE_SECRET`**：一改全员失效。
2. **`config.json` / `passwords.json` 不进 Git、不贴对话、不随软件分发**。
3. 协议字段名（`password` / `machine` / `token` / `action` / `config` / `wan` / `rh_api_key` / `manifest_url` / `cos`）不能改，改了老客户端全废。
4. **百炼 Key / RH Key 都不许写进客户端代码**——它们是服务端下发，写进 exe 等于把所有授权门槛作废。

---

## 6. 回滚

| 想做的事 | 做法 |
| --- | --- |
| 撤掉某人授权 | 密码管理器「停用」→ 自动同步 → 他下次启动被拒 |
| 收回全部授权 | 换 `LICENSE_SECRET` + deploy → 全员需重新激活 |
| 停掉配置下发 | 从 ZIP 里去掉 `config.json` 后 deploy → 客户端显示「配置：未下发」 |
| 回滚到上一版代码 | `git log` 找版本 → `git checkout <sha> -- server/` → deploy |
