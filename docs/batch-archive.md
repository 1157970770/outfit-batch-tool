# 批次永久档案（款号 / 提示词 / 参考图）

> 面向：运维、数据备份、后续接手的开发者。代码：`app/web/batch_archive.py` + `BatchManager`；
> 测试：`tests/test_batch_archive.py`；补档：`scripts/backfill_batch_archive.py`。

## 1. 解决什么问题

批次原本只落 `web-data/batches/<id>.json`，参考图只在 `web-data/uploads/`（**没有 GC，但也没有副本**）。
一旦批次被删、uploads 被清理、或换机器只搬了部分目录，**款号、提示词、当时用的是哪几张图**就追溯不到了。

档案把「这一批是什么」固化到独立目录：

- **款号**（`tags.top`，老批次回退 `style_no`）决定归档路径；
- **提示词**每次变化都留一版（`prompt-history.jsonl`），当前版另存 `prompt.txt`；
- **参考图**按 `upload_id` 复制进 `images/`，之后删批次/删 uploads 都不影响；
- 每一轮生产另存 `runs/<job_id>.json`，固定**当轮实际提交的提示词、图片顺序、模板 ID**。

档案与批次生命周期解耦：**删批次只删 `batches/<id>.json`，档案不删**，只写 `deleted.json` 墓碑。

## 2. 目录与文件

```
web-data/archive/<款号>/<批次号>__<batch_id>/
├── meta.json              批次快照；current_images / all_images / 计数 / 时间戳
├── prompt.txt             当前提示词（UTF-8 纯文本）
├── prompt-history.jsonl   每次提示词变化：at / event / by / prompt_id / sha256 / prompt
├── history.jsonl          批次事件流：batch_created / image_added / image_removed /
│                          primary_image_replaced / batch_locked / run_started /
│                          run_blocked / prompt_updated / batch_closed / batch_deleted
├── archive说明.txt        人读说明（含款号、批次号、图片清单、校验方法）
├── checksums.sha256       当前图片的 SHA-256（`<hash>  images/<file>`）
├── images/<upload_id>.<ext>   参考图副本；按 upload_id 去重，删掉的旧图也保留
├── runs/<job_id>.json     每一轮快照：prompt / images[]（slot/upload_id/name/file）/ template_ids / at
└── deleted.json           删除墓碑：deleted_at / deleted_by（仅删除后出现）
```

目录名清洗：替换 `<>:"/\|?*` 与控制字符、去尾部点和空格、避开 Windows 保留名（CON/PRN/…）、
每段最多 60 字符；清洗后为空时回退 `未分类` / `批次` / `id`。

## 3. 什么时候写

| 触发 | event | 说明 |
| --- | --- | --- |
| 建批次 | `batch_created` | 图片在 `POST /api/uploads` 时已存在，这里复制副本 |
| 整组换图 | `images_set` | 草稿态 `POST /api/batches/<id>/image` |
| 追加细节图 | `image_added` | 最多 10 张，锁定后仍可加 |
| 删除细节图 | `image_removed` | 槽位 1 不可删；旧图在档案里标 `removed_at`，文件保留 |
| 换定妆图 | `primary_image_replaced` | 锁定后允许；`image_changes` 一并进 meta |
| 锁定清单 | `batch_locked` | 清单、筛选条件、款号此刻定型 |
| 每轮生产 | `run_started` | 写 `runs/<job_id>.json`；模板全不可用时 `run_blocked` |
| 改提示词 | `prompt_updated` | 只影响之后的生产；历史版进 `prompt-history.jsonl` |
| 关闭 | `batch_closed` | 之后不可再生产，但档案继续可读 |
| 删除 | `batch_deleted` | **先写档案成功才允许删批次**；失败返回 500，批次保留 |

写档案失败（磁盘满、权限、源图丢失）**不阻断**建批次/加图/生产，只在服务日志与内存告警里留 warning；
档案没写成功时 `history.jsonl` 也不会多记一条，避免把失败伪装成成功事件。
唯一硬闸门是删除：避免「批次已经没了，档案却没写全」。

## 4. 读取与下载

```
GET /api/batches/<id>/archive.zip?probe=1
→ {"ok":true,"count":2,"batch_no":"...","ready":true,"size_bytes":123456,
   "download_url":"/api/batches/<id>/archive.zip"}

GET /api/batches/<id>/archive.zip
→ application/zip，文件名 "<批次号>_档案.zip"
```

- 权限与批次详情一致：本人或管理员；跨账户 403，不存在 404。
- `probe` 只读不复制：老批次还没档案时 `ready=false`，`count` 按当前已绑定图报数。
- 真实下载时先 `ensure()` 补档，再在锁外打包；zip 写 `web-data/tmp/batch-archive-<hex>.zip`，响应结束即删。
- zip 内顶层目录为 `档案_<批次号>_<id>/`，图片用 `ZIP_STORED`（不重复压缩），文本用 `DEFLATED`。
- UI：批次详情步骤 4「清单条目」右侧「下载档案（ZIP）」，草稿/锁定/关闭都显示；先 probe 再原生下载。

## 5. 历史补档（44 个现存批次 / 75 个批次生产 job）

```powershell
# 先看会补什么，不写盘
.\.venv\Scripts\python.exe scripts\backfill_batch_archive.py --dry-run

# 全量补（幂等，可重复跑）
.\.venv\Scripts\python.exe scripts\backfill_batch_archive.py

# 只补一个批次 / 限制数量
.\.venv\Scripts\python.exe scripts\backfill_batch_archive.py --batch e0270b4afdd8
.\.venv\Scripts\python.exe scripts\backfill_batch_archive.py --limit 10
```

- 默认 `data_root` 读 `server/web.json`；也可 `--data-dir "D:\...\web-data"`。
- 口径：全库 325 个 job 里 87 个带 `batch_ref`，其中 75 个指向现存 44 个批次、12 个指向已删批次；
  其余 238 个是试跑/无批次归属任务，**不在批次档案范围**，仍留在 `web-data/jobs/`。
- 逐批次 `ensure(event="backfill")`，再按 `runs` 读 `jobs/<job_id>.json` 写 run 快照；
  提示词优先用 job 当轮快照，图片优先用 `job.images`，老 job 回退 `image_path`。
- 单批失败继续跑，最后打印失败明细并以非零退出；修好后重跑即可，已复制的图不会重复复制。
- **生产区跑补档**：在设计区发布并重启后，在 `D:\批量视频任务运行区` 执行同一脚本；
  路径按运行区 `server/web.json` 解析，不需要手工指定。

## 6. 备份与恢复

- 档案是普通目录，直接备份 `web-data/archive/` 即可；按款号分目录，可单款号拷走。
- 恢复：把 `archive/<款号>/...` 放回 `web-data/archive/`，批次 JSON 若已删也不用恢复，
  档案里的 `meta.json` / `prompt.txt` / `images/` 已能独立说明这一批。
- 校验：`checksums.sha256` 覆盖**当前图**；旧图（`removed_at` 非空）不参与校验，但文件仍在。
  用 `sha256sum -c checksums.sha256`（Linux/WSL）或 PowerShell `Get-FileHash` 比对。

## 7. 已知边界

- **不做 GC/去重**：同一张图跨批次会各存一份；磁盘占用约为 `uploads` 中批次参考图的 1 倍。
  需要省空间时按 `archive/<款号>/` 整目录归档到冷存储，不要删单个 `images/` 文件（会破坏 checksums）。
- **同步写、非事务**：单个文件原子写（tmp + `os.replace`），但多文件之间没有事务；
  极端断电可能出现 meta 与图片版本不一致，重跑 `backfill` 或再次 `ensure` 会收敛。
- **删除是软删除**：档案永远保留；要真正清理只能人工处理目录（不建议）。
- **不替代 `uploads`**：运行期仍以 `uploads` 为真源；档案是事后追溯副本。
- 老批次首次 `probe` 不建目录；只有真实下载或 `backfill` 才落盘，避免打开详情就写磁盘。
