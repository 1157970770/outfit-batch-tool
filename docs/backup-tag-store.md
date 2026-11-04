# 标签库真源备份（→ 腾讯云 COS）

> 一句话：`web-data/template_tags.json` 是标签库唯一真源，但它**不在 git 里**
> （`.gitignore` 第 62 行排除了整个 `web-data/`）。这份文档管的就是它的外部备份。

---

## 1. 备份什么

| 备份对象 | 本地路径 | 云端位置 |
| --- | --- | --- |
| 真源全量 | `web-data/template_tags.json` | `backup/tags/YYYY-MM-DD/template_tags-r<rev>-<时间>-<指纹8>.json.gz` |
| 最新指针 | 同上的内容 | `backup/tags/latest/template_tags.json.gz` + `latest/meta.json` |
| 版本快照 | `web-data/tag_versions/*.json.gz(.meta.json)` | `backup/tags/versions/<同名>` |

- 桶：`video-output-1463907213`（取自 `server/config.json` 的 `cos.output_bucket`）。
- 压缩：`template_tags.json` 862 KB → gzip 约 **60 KB**，一次上传几秒钟。
- 幂等：文件名尾巴带内容 sha256 前 8 位，**内容没变就不重复传**，跑一百次也只有一份。
- 指纹校验：上传后再 `stat` 比对大小，不一致直接报错，不会留下半份。

---

## 2. 定时执行

已注册 Windows 任务计划：

| 项 | 值 |
| --- | --- |
| 任务名 | `TagStoreBackupDaily` |
| 频率 | 每天 10:30（`StartWhenAvailable`：错过就在下次开机补跑） |
| 命令 | `pythonw.exe scripts/backup_tag_store.py --quiet`（无窗口） |
| 日志 | `web-data/logs/tag_backup.log` |

```powershell
# 立即跑一次
Start-ScheduledTask -TaskName TagStoreBackupDaily

# 看上次结果（0 = 成功）
Get-ScheduledTaskInfo -TaskName TagStoreBackupDaily

# 改时间 / 删任务
powershell -ExecutionPolicy Bypass -File scripts\install_tag_backup_task.ps1 -Time 18:00
powershell -ExecutionPolicy Bypass -File scripts\install_tag_backup_task.ps1 -Remove
```

> 任务以**当前用户**身份运行，只在用户登录时触发。如果你要求"未登录也跑"，
> 需要把任务改成存储凭据的运行方式（`-User` + `-Password`），本机没做。

---

## 3. 手动跑

```bash
PY="C:/Users/EDY/.dsh/dsh-runtimes/dsh-primary-runtime/dependencies/python/python.exe"

"$PY" scripts/backup_tag_store.py                  # 备份一次
"$PY" scripts/backup_tag_store.py --dry-run        # 只看计划，不上传
"$PY" scripts/backup_tag_store.py --no-versions    # 只传真源，不传版本快照
"$PY" scripts/backup_tag_store.py --keep 30        # 云端全量只留最近 30 份
"$PY" scripts/backup_tag_store.py --bucket other-1463907213
```

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--keep` | 60 | 云端保留多少份全量（`0` = 不清理）。只删 `template_tags-r*.json.gz`，不碰 `latest/` 与 `versions/` |
| `--keep-versions` | 0 | 版本快照保留数，默认全留（快照是回滚依据，不主动删） |
| `--force` | 关 | 内容未变也重新上传 |
| `--quiet` | 关 | 全部输出写日志（`pythonw` 下必须带，否则 print 会因 stdout 为 None 报错） |

配置优先级：命令行 → 环境变量 `COS_*` → `server/config.json` 的 `cos` 段 →
`~/.cos/credentials.json` 或 tccli 凭证。

---

## 4. 恢复（真源丢了 / 被改坏）

```bash
# 先干跑，看清会恢复成哪个 revision
"$PY" scripts/backup_tag_store.py --restore --dry-run

# 确认后执行（会先把当前文件存成 template_tags.json.before-restore-<时间>）
"$PY" scripts/backup_tag_store.py --restore --yes

# 想恢复到某个历史点，指定对象 Key
"$PY" scripts/backup_tag_store.py --restore --yes \
  --restore-key "backup/tags/2026-11-04/template_tags-r783-20261104-105629-ad3e7a58.json.gz"
```

恢复流程的四道保险：

1. 下载后与 `latest/meta.json` 的 `sha256` **比对**，不符直接拒绝；
2. 内容必须是合法 JSON 且含 `videos` 列表，否则拒绝；
3. 覆盖前自动 `copy2` 一份当前文件；
4. 写入走 `.restore.tmp` → `replace`，不留半个文件。

**恢复完必须重启网页服务**（8765 的旧进程内存里还挂着旧 catalog），
否则「标签」页读到的仍是旧数据。桌面打标器同理，重开一次。

版本级回滚（只退某一个快照）走网页「标签」页的版本列表，不用这个脚本。

---

## 5. 注意事项

- **不要在 COS 生命周期里给 `backup/` 配删除规则**。现有规则针对 `output/`、`input/`、
  未完成分片，前缀对不上，本来就碰不到；以后加规则时注意别写全桶。
- 并发锁：`web-data/logs/.tag_backup.lock`。计划任务与手动执行撞车时，
  后来者直接退出（退出码 0），不会同时写云端；锁超过 6 小时自动接管。
- 真源在写的时候是原子替换（临时文件 + rename），脚本读前后还会比对 `mtime/size`，
  不会读到写一半的 JSON。
- 备份的是**标签库**，不含视频文件、不含 `web.json`（账号密码）、不含 COS 密钥。
  账号库的备份另议。
- 每次跑都会在 `web-data/logs/tag_backup.log` 留一行：
  `OK/SKIP r<rev> <行数> <大小> sha=... key=... <耗时>`。

---

## 6. 已知边界

| 情况 | 行为 |
| --- | --- |
| 内容没变（两次运行之间没人改标签） | `SKIP`，不上传新对象（除非 `--force`） |
| 真源文件缺失 | 直接失败（退出码 2），不会传空文件 |
| 真源 JSON 损坏 | 直接失败（退出码 2），云端不受影响 |
| 版本快照在同步途中被本地轮转删掉 | 跳过该文件，不中断主备份 |
| 网络中断 / 403 | 主备份失败（退出码 1）并写日志；下次重试即可，已传的对象不重复 |
