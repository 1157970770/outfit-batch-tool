# 双版本隔离手册（旧 RH 版 / 新官方版）

> **目标**：旧 RunningHub 版**原样保留、优先可用**；新阿里云百炼官方版独立构建、独立安装、独立缓存，互不覆盖。
> 适用分支：`feature/aliyun-wan3-official`（新）/ `feature/prompt-library`（旧）。

---

## 1. 两套版本分别在哪

| | 旧 RH 版（不要动） | 新官方版 |
| --- | --- | --- |
| Git 分支 | `feature/prompt-library`（`b09cf4e`） | `feature/aliyun-wan3-official` |
| 代码位置 | 独立 worktree：`.worktrees/rh-legacy/` | 当前工作区 |
| 打包脚本 | `scripts/build.ps1` | `scripts/build-official.ps1` |
| exe 名 | `换装批量生产器.exe` | `换装批量生产器-官方版.exe` |
| 安装包 | `dist/换装批量生产器-v0.9-20261026.zip`（已存在，41.5 MB） | `dist-official/换装批量生产器-官方版-v0.9-20261028.zip`（51.5 MB） |
| 本机授权缓存 | `%APPDATA%\RHOutfitTool\license.json` | `%APPDATA%\RHOutfitToolWan3\license.json` |
| 服务端 Key | `rh_api_key` | `wan.api_key` |
| 生成接口 | RunningHub `reference-to-video` | 阿里云百炼 `wan3.0-video` |

> `.worktrees/` 和 `dist-official/` 已加入 `.gitignore`，不会混进提交。

---

## 2. 旧版可用性（已做）

### 2.1 旧代码没被改

- 旧版完整保留在 `feature/prompt-library` 分支；
- 新分支从它切出，**没有在旧分支上提交任何东西**；
- 本地用 worktree 把旧版原样检出到 `.worktrees/rh-legacy/`，与新版物理隔离。

### 2.2 旧版测试通过

在 `.worktrees/rh-legacy` 中运行过：

```text
python3 tests/test_core.py   → 3 条成功，归档 3 条，OK
python3 tests/test_ui.py     → 3 条界面跑通，OK
```

### 2.3 旧版安装包还在

```
dist/换装批量生产器-v0.9-20261026.zip   41.5 MB
dist/换装批量生产器/                     旧版文件夹
```

新版的构建输出在 `dist-official/`，**不会删旧 zip**：新版脚本只清理 `换装批量生产器-官方版-v*.zip`。

### 2.4 需要重新检出旧版时

```powershell
# 只读/构建旧版，不影响当前新分支
git worktree add .worktrees\rh-legacy feature\prompt-library

# 进旧版目录
cd .worktrees\rh-legacy
python3 tests\test_core.py
python3 tests\test_ui.py

# 旧版打包（在旧版目录执行；产物是旧名字）
powershell -ExecutionPolicy Bypass -File scripts\build.ps1 -OutRoot build\release-legacy
```

> 旧版 worktree 里的 `scripts/build.ps1` 若因换行/编码被 PowerShell 5.1 误读，最稳妥是
> 用**旧分支的独立 clone/正常检出**打包；不要把新分支的脚本覆盖进去。

---

## 3. 新版构建（独立名字）

```powershell
# 新官方版专用；输出到 dist-official，不会覆盖旧版
powershell -ExecutionPolicy Bypass -File scripts\build-official.ps1
```

产物：

```
dist-official/换装批量生产器-官方版/换装批量生产器-官方版.exe
dist-official/换装批量生产器-官方版-v0.9-<日期>.zip
```

不要用 `scripts/build.ps1` 打新官方版，否则会生成旧名字/旧目录，和旧版混淆。

---

## 4. 运行时隔离

| 隔离点 | 旧版 | 新版 | 效果 |
| --- | --- | --- | --- |
| exe 名 | `换装批量生产器.exe` | `换装批量生产器-官方版.exe` | 可同时装两个文件夹 |
| exe 标题 | `换装批量生产器 · 测试版` | `换装批量生产器 · 官方版` | 一眼区分 |
| 授权缓存 | `%APPDATA%\RHOutfitTool\` | `%APPDATA%\RHOutfitToolWan3\` | 不互相覆盖票/配置 |
| 输出目录 | 各自 exe 旁的 `输出` 或用户选择 | 同左，但安装目录不同 | 不混批次 |
| 安装包 | `dist/` | `dist-official/` | 不删旧包 |

> 不要装到同一个文件夹、不要共用同一个 exe 名；两个版本各自保留自己的 `_internal\`。
>
> **新版第一次启动需要重新输一次密码**：它用独立缓存 `RHOutfitToolWan3`，不会读取旧版的票；
> 旧版的票和配置也不会被新版改写。这是隔离的预期行为，不是授权失效。

---

## 5. 服务端配置：一套服务，两套客户端

服务端 `server/config.json` 过渡期**同时保留** `wan` 与 `rh_api_key`：

```json
{
  "wan": {
    "provider": "dashscope",
    "api_key": "sk-...",
    "model": "wan3.0-video",
    "base_url": "https://dashscope.aliyuncs.com/api/v1",
    "default_mode": "edit",
    "default_duration": -1,
    "default_ratio": "adaptive"
  },
  "rh_api_key": "32 位 RunningHub Key",
  "manifest_url": "...",
  "cos": { "...": "..." }
}
```

- 旧客户端只读 `rh_api_key`，忽略 `wan`；
- 新客户端优先读 `wan`，`rh_api_key` 只在回滚到 `provider="runninghub"` 时用；
- 两边共用同一个授权服务和 COS 桶；
- 不要把 `wan.api_key` 填进旧版代码，也不要把新版逻辑合进旧分支。

---

## 6. 回滚

### 6.1 新版回到 RunningHub（不发版）

```json
{
  "wan": {
    "provider": "runninghub",
    "api_key": "",
    "model": "wan3.0-video",
    "base_url": "https://www.runninghub.cn/openapi/v2"
  },
  "rh_api_key": "32 位 RunningHub Key"
}
```

`license_deploy.py deploy` → 新官方版客户端重启。

### 6.2 直接退回旧版

- 旧版 exe/zip 一直在 `dist/`，直接继续用；
- 旧版缓存 `%APPDATA%\RHOutfitTool\` 从未被新版读写；
- 不需要卸载新版，两者可共存。

### 6.3 代码回滚

- 新分支独立，`git revert` 或删分支都不影响旧分支；
- **不要把新分支 merge 回旧分支**；要合并也只能在确认旧版停用后另开分支。

---

## 7. 红线

1. 旧分支 `feature/prompt-library` / `main` **不改**；
2. 旧版 `dist/换装批量生产器-v0.9-20261026.zip` **不覆盖**；
3. 新版用 `build-official.ps1`，旧版用 `build.ps1`；
4. 两版不共用 `%APPDATA%` 目录；
5. 过渡期服务端同时下发 `wan` + `rh_api_key`；
6. 新版 Key 不进旧版客户端，旧版 Key 不进新版代码。
