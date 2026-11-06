# 运行区 / 开发区分区（2026-11-06 起）

> 一个仓库、两个文件夹：**运行区**给同事用（生产），**设计区**只做开发。
> 改代码再也碰不到线上；发布 = git pull + 重启。

## 布局

| | `D:\批量视频任务运行区` | `D:\批量视频任务设计区` |
| --- | --- | --- |
| 角色 | **生产**：局域网同事访问的 8765 | **开发**：改码、测试、提交 |
| git | 只 `pull --ff-only`，**永不改码/不提交** | dev 分支开发、commit、push GitHub |
| 服务 | 计划任务 `OutfitWebServer`（session 0，开机自启） | 手动 `python run_web.py`（端口 **8766**） |
| 数据 | `web-data/`（真实数据：任务、标签真源、上传、成片） | `web-data-dev/`（轻量副本，可随时重建） |
| 配置 | `server/web.json` + `server/config.json`（生产） | 同名文件各一份（真实 Key，见下） |
| 打标器 | **从这里启动**（真源=运行区 `web-data`） | 不要从这里启动（会写进 dev 副本） |
| 前端 | 生效以 git pull 为准 | 静态文件改完即生效（dev 实例） |

两个文件夹的代码**完全同源**：运行时的目录按代码所在根解析（`app/config.py` 的
`app_dir()`），不依赖工作目录；数据根由 `server/web.json` 的 `data_root` 决定。
**但任务 / 批次 / 上传记录里存的是生成那一刻的绝对路径**（`out_dir`、`images[].path`、
`uploads/meta/*.json` 的 `path`），换文件夹后旧路径会悬空。读数据时由
`app/web/paths.py` 的 `relocate()` 按当前 `data_dir` 重锚定，历史 JSON 一个字都不用改；
详见下节「历史任务路径」。

## 日常发布（设计区 → 运行区）

```powershell
# 1. 设计区：改码 → python tests/run_all.py → git commit
# 2. 确认运行区没有 running/queued 任务（管理页队列，或看运行区 web-data\jobs）
# 3. 发布（等于：git -C D:\批量视频任务运行区 pull --ff-only + 自检）
python scripts/deploy_runtime.py
# 4. 右键运行区 启动器\重启网页服务-管理员.bat → 以管理员身份运行
# 5. 浏览器确认（记得前端 ?v= 版本戳要 bump 才会让同事浏览器刷新缓存）
```

**为什么用 git 而不是复制文件**：只发布「已提交」的状态——没写完的实验天然不会漏到线上；
运行区 `git status` 恒干净，能 diff 上次发布，也能 `git reset --hard ORIG_HEAD` 一键回退一次发布。
发布助手只拦**已跟踪文件**的改动；运行区的 `运维/`、本机日志等未跟踪文件已被忽略，不会卡住发布。

## dev 实例（设计区）

- 启动：双击 `启动器\启动网页服务.bat`（或 `启动网页服务.vbs`）——启动器读 `server/web.json`
  决定端口与数据目录，两区同一个脚本；也可以直接 `python run_web.py`。
- `python run_web.py` 读设计区 `server/web.json`：端口 **8766**、`data_root: web-data-dev`、
  独立 `session_secret`（浏览器 cookie 不区分端口，生产/开发会话互不通用）。
- 账号与生产相同；`web-data-dev\uploads` 是指向运行区 uploads 的 junction，历史定妆图靠它显示
  （重建命令与「写入会落到生产」的提示见 [AGENTS.md §0](../AGENTS.md)）；`output` 仍为空，
  历史视频靠 `设计区\web-data\output` 的临时桥，别删那条桥。
- `server/config.json` 是**真实 Key**（用户决定）：dev 提交 = 真实计费，且与生产共用
  250 RPM / 100 在飞额度——跑测试批次时省着点，别在生产高峰期压测。

## 切换 / 回滚（一次性运维，脚本在 `运行区\运维\`）

| 脚本 | 用途 |
| --- | --- |
| `切换服务到本文件夹-需管理员.bat` | 当初一次性迁移：停旧服 → 复制 web-data → 重定向计划任务 → 启动自检（**漏了重写历史 JSON 里的绝对路径**，见「历史任务路径」） |
| `回滚到设计区-需管理员.bat` | 紧急回滚：把计划任务指回设计区并启动；切换后产生的新任务会列出来提示搬回 |

两个脚本都自带安全检查：检测到 running/queued 任务会拒绝执行。
`运维/` 不入库（`.gitignore` 里的本机运维目录）：重新克隆运行区后需要在
`运行区\运维\` 重建这两个脚本，或直接照本文档里的步骤手工切换。

## 打标器归属

`启动打标器.py` 按**所在文件夹**定位真源（`BASE/web-data/template_tags.json`）。
分区后**只能从运行区启动**（`运行区\启动器\启动视频标签整理.bat`）；
从设计区启动会写进 `web-data-dev` 的副本，网页端看不到，等于标签「丢了」。

## 数据归档

- `设计区\web-data-生产快照-20261106\`：切换那一刻的完整生产数据（只读兜底）。
  运行区稳定运行一周后可整体归档/删除。
- 运行区 `web-data` 是唯一真数据；它的备份策略照旧（标签真源每日 COS 导出，
  见 [backup-tag-store.md](backup-tag-store.md)）。

## 历史任务路径（2026-11-06 故障与根治）

现象：切换到运行区当天，旧任务在历史页里显示「成功」，但点下载 / 预览 / 对比全部 404；
同期新提交的任务正常。视频文件其实一直在运行区 `web-data\output`，COS 归档也在。

根因：切换脚本只「复制 web-data」，没有重写历史 JSON 里持久化的旧绝对路径
（指向 `D:\批量视频任务设计区\web-data\...`）；设计区原 `web-data` 归档改名后路径悬空。
当时 `JobManager.output_path()`、`_upload_file()`、`image_slots()` 都直接 `Path(旧绝对路径)`。

- **即时止血（不改数据、不重启）**：在设计区建 `web-data\output`、`web-data\uploads`
  两个目录联接（junction）指向运行区同名目录，让旧路径立即重新可读。该桥是临时的，
  见 `设计区\web-data\README.md`；代码发布重启后即可删除。
- **代码根治**：新增 `app/web/paths.py::relocate()`，读 `out_dir` / `images[].path` /
  上传 meta / 批次条目时按当前 `data_dir` 重锚定；`must_exist=True` 只在目标确实存在时
  替换，绝不误伤外部文件；`must_exist=False` 用于重启接回时准备新输出目录。
  覆盖：任务下载 / zip、整批 zip、图片预览、重跑与重试、批次生产。
- **以后再切换**：要么靠发布后的代码自动重锚（推荐），要么在切换脚本里重写历史 JSON
  的绝对路径；不要只复制目录就以为数据可用。

回归测试：`tests/test_path_relocate.py`（已加入 `tests/run_all.py`）。

## 已知边界

- 设计区 `server/web.json` 若被改回 8765 + `web-data`，手动启动会与生产**同绑 8765**
  （Windows 的 SO_REUSEADDR 允许双绑，实例锁只防同数据目录）——别这么改。
- 设计区搬家/改名：运行区 origin 指向设计区路径，需 `git -C 运行区 remote set-url ...` 同步；
  GitHub 推送习惯不变。
- 运行区升级 Python / 换 vbs 里的解释器路径：与设计区解耦，各自维护。
