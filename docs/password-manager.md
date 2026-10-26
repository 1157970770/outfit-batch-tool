# 密码管理器：搭建与使用

> 作用：图形化管理 `server/passwords.json`（谁能用这个软件、到哪天、是否停用），改完一键同步到云函数。
> **内部工具，不随客户端分发**——`build.ps1` 只打包 `run_app.py`，不含 `scripts/`。

---

## 1. 它由什么组成

| 文件 | 作用 |
| --- | --- |
| `scripts/password_manager.py` | 主程序（PySide6 界面，约 390 行） |
| `scripts/密码管理器.bat` | 双击启动；内容全 ASCII，用 `call` 逐个探测 `python3 / py / python`，挑出**真装了 PySide6** 的那个 |
| `scripts/密码管理器.py` | 三行兼容入口（老的快捷方式还能用） |
| `scripts/license_passwords.py` | 命令行后端：读写密码表、生成编号、校验、触发同步 |
| `scripts/license_deploy.py` | 真正干活：打包 + 上传云函数 + 线上验证（`deploy` / `check` 子命令） |
| `server/passwords.json` | **唯一的真身**，不进 Git、不外发 |

界面只是壳，所有逻辑都在 `license_passwords.py`，所以命令行和 GUI 操作的是同一份数据、走同一条同步链路。

---

## 2. 环境要求

```bash
pip install PySide6        # 界面唯一依赖
```

云同步需要本机有可用的腾讯云凭证（`tccli` 或环境变量），因为底层会调 `license_deploy.py deploy`。

> **本机注意**：这台机器上 `python3` 指着一个自带 PySide6 的运行时，`py` 不存在，`python` 是微软商店的存根（返回 9009）。
> 所以命令行一律用 `python3`，双击则用 `.bat`（它会自己挑对解释器）。

---

## 3. 启动

**双击**：`D:\批量视频任务设计区\scripts\密码管理器.bat`
正常会先闪一下黑窗打印 `Using Python: python3`，然后弹界面。

**命令行**：

```bash
python3 scripts/password_manager.py            # 正常启动
python3 scripts/password_manager.py --smoke    # 自检：起窗口 1.5 秒自动退出，用来确认环境没坏
```

---

## 4. 界面能做什么

| 操作 | 说明 |
| --- | --- |
| 新增 | 自动生成下一个编号（`RH-2026-NNN-xxxx`），默认 **31 天**有效，可设天数或勾「永久」 |
| 续期 / 改到期 | 改到期时间与备注；改成过去的时间等于立即失效 |
| 停用 / 恢复 | 切换 `revoked`；停用后客户端 `/verify` 立刻被拒 |
| 删除 | 从表里移除（不可恢复，历史记录没了，一般用停用代替） |
| 复制密码 | 双击表格行同样可复制 |
| 立即同步到云 | 勾选「保存后自动同步」时，每次改动都会自动：存本地 → 打包 → 上传 → 线上验证 |

状态列的颜色含义：绿 = 可用、黄 = 快到期、红 = 已过期或已停用、灰 = 永久。

---

## 5. 数据是怎么走到云端的

```
界面改表 → LP.save(server/passwords.json)     ← 先存本地，网络断了也不丢
        → license_deploy.py deploy            ← 打包（含 passwords.json）上传云函数
        → license_deploy.py check CODE --expect ok|reject   ← 线上实测这条密码是否按预期可用/被拒
```

客户端那边：每次启动走 `/verify`，服务端**每次都回表复查** `expires` 与 `revoked`，所以
**停用或改到期 = 该机器下次启动即被拒**，不需要客户端升级或重装。

---

## 6. 命令行等价操作（不想开界面时）

```bash
python3 scripts/license_passwords.py list                      # 看全表
python3 scripts/license_passwords.py add --note "张三 / 设计部"   # 加一条（默认 31 天）
python3 scripts/license_passwords.py expire CODE 2028-12-31     # 改到期
python3 scripts/license_passwords.py revoke CODE                # 停用
python3 scripts/license_passwords.py unrevoke CODE              # 恢复
python3 scripts/license_passwords.py remove CODE                # 删除
python3 scripts/license_passwords.py deploy                     # 只同步，不改表

python3 scripts/license_deploy.py check CODE                  # 验某条密码线上是否可用
python3 scripts/license_deploy.py check CODE --expect reject   # 验某条是否已被拒
```

`add` / `expire` / `revoke` 这些命令改完会自动触发同步；`deploy` 用来看同步是否成功。

---

## 7. 排错

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 双击后窗口一闪就没 | 批处理挑到了没装 PySide6 的解释器（历史上是落到微软商店的假 `python`） | 现在的 `.bat` 会打印 `Using Python: xxx`；仍失败就看 `%TEMP%\密码管理器_error.log` |
| 提示「这个 Python 里没有 PySide6」 | 双击 `.py` 关联到了别的解释器 | 照提示 `pip install PySide6`，或改用 `.bat` |
| 界面上报「同步没成功」 | 上传失败（凭证/网络），或线上验证没过 | 本地表**已经保存**；网络恢复后点「立即同步到云」，或跑 `license_deploy.py deploy` |
| 同步后客户端仍能用旧密码 | 云函数还在切流量 | 等 10–30 秒再试 `license_deploy.py check CODE --expect reject` |
| 表里改完但线上没变 | 忘了同步（没勾自动同步） | 点「立即同步到云」 |
| 界面能开但点按钮报错 | `license_passwords.py` 被改过，接口不匹配 | 跑 `python3 scripts/password_manager.py --smoke`，再点一次；仍报错贴 `%TEMP%\密码管理器_error.log` |

**崩溃日志**：任何未捕获异常都会写到 `%TEMP%\密码管理器_error.log`（完整 Python 报错栈）。

---

## 8. 红线

1. `server/passwords.json` **不进 Git、不发给客户、不贴进对话**（已在 `.gitignore`）。
2. `scripts/` 整目录是内部工具，任何时候不要塞进分发包。
3. 删密码不可恢复；要"停止授权"用**停用**，不要用删除。
4. 同步会重新部署云函数——如果同时有人在改 `config.json` 或服务端代码，先确认对方是否已部署完，避免互相覆盖。

---

## 9. 当前状态（2026-10-26 核对）

| 项 | 结果 |
| --- | --- |
| 界面-后端接口兼容 | ✅ GUI 用到的 `load/save/next_code/default_expires/_deadline/_sync` 签名都对得上 |
| 启动自检 | ✅ `--smoke` 退出码 0 |
| 密码表 | 3 条：`001`/`002` 待分配（2026-11-24 到期）、`003` 已使用（**永久**） |
| 云同步依赖 | ✅ `license_deploy.py` 的 `pack/deploy/check` 子命令都在 |

> 提醒：`003` 设成了**永久**，等于无法用"到期"自然收口，只能靠手动停用。建议改成明确日期。
