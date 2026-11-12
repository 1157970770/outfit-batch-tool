# 每日生产数据自动同步飞书

> 状态：**现行** ｜ 时效：2026-11-12 ｜ 入口脚本：`scripts/feishu_daily_report.py`
>
> 一句话：每天 **18:00**，Windows 计划任务读生产任务真源，把「账户 × 日期」的生产条数与消费金额
> 幂等写进飞书多维表格的三张表；人工列永不覆盖，重复跑结果不变。

---

## 0. 一分钟版

| 问题 | 答案 |
| --- | --- |
| 什么时候跑 | 每天 **18:00**（计划任务 `FeishuDailyReport`），开机错过会补跑 |
| 跑什么 | `pythonw scripts\feishu_daily_report.py --quiet`（工作目录 = 运行区） |
| 读什么 | `D:\批量视频任务运行区\web-data\jobs\*.json`（生产任务唯一真源） |
| 写到哪 | 飞书 base `V4C3bbCVGamsD8sBwO2cF2D5nTg` 的三张表，见 §1 |
| 写几天 | 默认 **D-3..今天**；当天写的是 18:00 快照，次日自动修正 |
| 重复跑 | 幂等：只改有差异的行，其余显示「无变化」 |
| 日志 | `D:\批量视频任务运行区\web-data\logs\feishu_report.log` |
| 出问题看 | §8 排障表；手动重跑一句 `Start-ScheduledTask -TaskName FeishuDailyReport` |

---

## 1. 三张表

同一个飞书多维表格 base `V4C3bbCVGamsD8sBwO2cF2D5nTg`，脚本一次跑完三张：

| 表名 | table_id | 谁维护 | 唯一键 | 列 |
| --- | --- | --- | --- | --- |
| **每人每日生产统计**（原名「每日消费表」） | `tblZZPEDvonrvm2P` | 脚本 | 日期 + 账户 | 账户 / 人员 / 日期 / 生产视频数量 / 消费金额 |
| **每日总消耗** | `tblzdoUBJhVXssOy` | 脚本 | 日期 | 日期 / 生产视频数量 / 消费金额 / 任务数 / 生产账户数 |
| **每日视频可用统计**（原名「每日视频统计」） | `tblGkb37vMn8rL4J` | 脚本 + 人工 | 日期 | 日期 / 当日生产的视频总数（自动） / 可用视频数量(待回传数据)（**人工**） |

约定：

- 明细表一行 = **一个账户 + 一天**，当天没生产的 enabled 账户也写 0 行（与管理端「账户用量」含零账户一致）；
- 汇总表一行 = **一天**，是全账户合计；
- 统计表里「可用视频数量(待回传数据)」**永远是人工列**——脚本只发「日期 + 总数」两个字段，
  飞书按字段部分更新，所以人工填的数字任何一次同步都不会被覆盖（已做过破坏性实验验证）。

---

## 2. 数据从哪来、怎么算

### 2.1 真源

`D:\批量视频任务运行区\web-data\jobs\<job_id>.json`，一个任务一个文件。字段：

```text
job:    id / owner / status / created_at(epoch 秒) / started_at / finished_at /
        resolution / total / done / records[] / summary{...} / lane("big"|"small") …
record: id / template / status / billable_seconds / estimated_cost / usage / cos_key …
```

### 2.2 口径（与 `app/web/jobs.py:964 usage_for()` 完全一致）

| 指标 | 算法 |
| --- | --- |
| 归日 | `job.created_at` 按**本机时区**（Asia/Shanghai）取日期，不是 finished_at |
| 生产视频数量 | 当日该账户所有 job 的 `records` 里 `status == "success"` 的条数 |
| 消费金额（元） | 当日该账户**全部** record 的费用之和（失败/超时也计费） |
| 单条费用 | 优先 `record.estimated_cost`（生成时落的账）；没有则 `billable_seconds × 单价` |
| 单价 | `server/config.json → wan.price_per_second[分辨率]`；缺省用公开价 480P 0.3 / 720P 0.6 / 1080P 1.2 元/秒 |
| 任务数 | 当日该账户的 job 个数 |
| 生产账户数 | 当日 `jobs > 0` 的账户个数 |
| 人员 | 账户 → 姓名映射，见 §2.3 |

> 失败态口径：`_FAILED_RECORD_STATES = {failed, invalid, expired, canceled, unknown, timeout, skipped}`
> （`app/web/jobs.py:140`），与用量页同一份定义。

### 2.3 人员映射

`scripts/feishu_daily_report.py` 的 `DEFAULT_PERSONS`：

| 账户 | 人员 |
| --- | --- |
| `1157970770` | 郑 |
| `WUZHIHAO` | 吴 |
| `Caiqike` | 蔡 |
| `Liuhanpeng` | 刘 |
| `ZHOUHAO` | （未分配 → 留空，且脚本不写该列，不冲掉人工填的） |

改映射两种方式（都不用改表结构）：

1. 改代码里的 `DEFAULT_PERSONS` 并发布（会同步到两区）；
2. 改各区 `server/feishu.json` 的 `"persons": {"账户": "姓名"}`（只影响本区，无需重启）。

---

## 3. 一次运行做了什么

```text
18:00 计划任务启动（pythonw --quiet，stdout 无控制台 → 全部写日志）
 │
 ├─ 1. 取文件锁 data_root/logs/.feishu_report.lock（>6h 或持锁 pid 已死 → 视为过期锁，自动接管）
 ├─ 2. 读 server/web.json 拿 enabled 账户；读 server/config.json 拿单价
 ├─ 3. 扫 jobs/*.json，按「日期 → 账户」聚合（窗口默认 D-3..今天）
 ├─ 4. 先把聚合结果打进日志（每天每账户一行 + 每天合计一行）
 ├─ 5. 取 tenant_access_token（缓存到过期前 120 秒；接口失败重试 3 次，1s/2.5s 退避）
 ├─ 6. 明细表：ensure_fields（缺列自动补）→ 读全表 → 比对 → 批量新建/更新 → 清空占位行
 ├─ 7. 汇总表：按 table_id 找表（找不到按名字，再找不到就建）→ 按日期 upsert
 ├─ 8. 统计表：同上 → 只写「日期 + 当日生产的视频总数」→ 人工列不动
 └─ 9. 释放锁，退出码 0
```

退出码：`0` 成功 / `1` 网络·接口失败 / `2` 配置或参数问题（计划任务里非 0 就看日志）。

### 3.1 当天快照语义

18:00 跑的时候，当天还没过完，所以当天写的是**截至 18:00 的快照**；
18:00 之后完成的任务，会在**次日 18:00** 的回补（窗口含昨天）里修正进当天行。
真实例子：11-07 当天首写 508 条，次日回补修正为 **583 条**（当晚完成的任务），11-08 为 0。

---

## 4. 幂等与自愈

| 机制 | 行为 |
| --- | --- |
| 唯一键 | 明细 `日期\|账户`；汇总/统计 `日期`。重复运行只更新有差异的行 |
| 占位空行 | 建表自带的整行空记录自动删除；带内容的行一律不动 |
| 重复行 | 同键多行时保留一行，其余自愈删除（仅明细表） |
| 表定位 | `server/feishu.json` 里 `summary_table_id` / `daily_table_id` **优先按 ID 找**（改表名也不丢），其次按名字 |
| 自动建表 | 找不到才建；建表时列名/格式随脚本定义（日期 yyyy/MM/dd、数字 0 / 0.00） |
| 自动补列 | 明细表缺「人员」等列时自动补建，不打断本次写入 |
| 并发保护 | 文件锁，同一数据目录不会两个实例同时跑 |
| 清理 | 统计表只删「整行全空」的占位行，带日期的行永不自愈删除（可能挂人工数据） |

---

## 5. 配置：`server/feishu.json`

**不入库**（`.gitignore` 已忽略），设计区与运行区**各放一份**；脚本总是读「数据目录所在区」的那份。

```json
{
  "app_id": "cli_xxxxxxxxxxxxxxxx",
  "app_secret": "********",
  "app_token": "V4C3bbCVGamsD8sBwO2cF2D5nTg",
  "table_id": "tblZZPEDvonrvm2P",

  "fields": { "account": "账户", "date": "日期",
              "count": "生产视频数量", "cost": "消费金额", "person": "人员" },

  "persons": { "1157970770": "郑", "WUZHIHAO": "吴",
               "Caiqike": "蔡", "Liuhanpeng": "刘" },

  "summary_table_id": "tblzdoUBJhVXssOy",
  "daily_table_id": "tblGkb37vMn8rL4J",
  "daily_table_name": "每日视频可用统计",
  "daily_fields": { "available": "可用视频数量(待回传数据)" }
}
```

| 键 | 必填 | 说明 |
| --- | --- | --- |
| `app_id` / `app_secret` | ✅ | 飞书自建应用（当前 = 新租户 `zcnlg6cqjbf9` 的 `cli_aa67d28b1cf89d07`） |
| `app_token` / `table_id` | ✅ | 目标 base 与明细表 |
| `fields` | 可选 | 明细表列名被人工改过时在此对齐 |
| `persons` | 可选 | 覆盖/追加人员映射 |
| `summary_table_id` / `daily_table_id` | 建议 | **按 ID 找表**，防改名丢表（踩过坑，见 §8） |
| `daily_table_name` | 可选 | 按名字兜底时用 |
| `daily_fields` | 可选 | 统计表列名被人工改过时在此对齐 |

> 权限前提：该应用必须是这个 base 的**协作者**。没加 = 所有接口 `91403 Forbidden`。

---

## 6. 命令行

```bash
python scripts/feishu_daily_report.py                    # 默认：D-3..今天，写三张表
python scripts/feishu_daily_report.py --no-today         # 只写到昨天（纯回补）
python scripts/feishu_daily_report.py --from 2026-10-28 --to 2026-11-11   # 指定区间
python scripts/feishu_daily_report.py --dry-run          # 只打印聚合结果，不联网、不写飞书
python scripts/feishu_daily_report.py --setup-fields     # 只建列/清空行/确认表存在，不写数据
python scripts/feishu_daily_report.py --skip-zero        # 不写 0 行（只保留有生产的账户）
python scripts/feishu_daily_report.py --no-summary       # 不碰「每日总消耗」
python scripts/feishu_daily_report.py --no-daily         # 不碰「每日视频可用统计」
python scripts/feishu_daily_report.py --data-root "D:\批量视频任务运行区\web-data"  # 从设计区读生产数据
python scripts/feishu_daily_report.py --state-dir "D:\批量视频任务设计区\.tmp\feishu-state"  # 锁/日志换目录
```

从**设计区**交互运行时，数据与配置都在运行区，标准姿势：

```powershell
$py = "$env:USERPROFILE\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
& $py scripts/feishu_daily_report.py `
    --data-root "D:\批量视频任务运行区\web-data" `
    --state-dir "D:\批量视频任务设计区\.tmp\feishu-state" `
    --from 2026-10-28 --to 2026-11-11
```

---

## 7. 计划任务与日常运维

| 项 | 值 |
| --- | --- |
| 任务名 | `FeishuDailyReport` |
| 触发 | 每天 **18:00**；`StartWhenAvailable`（错过会补跑） |
| 执行 | `C:\Users\EDY\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\pythonw.exe` |
| 参数 | `"scripts\feishu_daily_report.py" --quiet` |
| 工作目录 | `D:\批量视频任务运行区` |
| 其他 | 单实例（IgnoreNew）、1 小时上限、允许电池启动 |

常用命令（PowerShell）：

```powershell
Get-ScheduledTaskInfo -TaskName FeishuDailyReport          # 看上次结果/下次时间
Start-ScheduledTask   -TaskName FeishuDailyReport          # 立刻跑一次
Disable-ScheduledTask -TaskName FeishuDailyReport          # 临时停用
Get-Content "D:\批量视频任务运行区\web-data\logs\feishu_report.log" -Tail 20 -Encoding UTF8
```

重装/改时间：运行区执行 `scripts\install_feishu_report_task.ps1 [-Time 18:30]`；
卸载加 `-Remove`。设计区的 `scripts/install_feishu_report_task.ps1` 与运行区同源。

---

## 8. 排障

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 日志 `[失败] ... 91403 Forbidden` | 应用不是该 base 的协作者（或用了旧 base 的 token） | 在飞书文档「… → 添加协作者」加 `cli_aa67d28b1cf89d07`；确认 app_token 是 `V4C3bbC...` |
| `[配置] ...` 且退出码 2 | `server/feishu.json` 缺键 / 读不到 | 对照 §5 补齐；两区都要有 |
| `99992402 field validation failed / type is required` | 更新字段接口必须带 `type` | 已在脚本里处理；自写接口时记得带 |
| `PUT .../tables/<id>` 返回 `404 page not found` | 数据表更新接口是 **PATCH** 不是 PUT | 用 `Feishu.rename_table()` |
| 明细/汇总/统计里出现**重复的表** | 人工改了表名，按名字找表失败 → 脚本自愈建了新表 | 在 `server/feishu.json` 配 `summary_table_id` / `daily_table_id`（按 ID 定位），并删掉多余表 |
| 表里某些列对不上 | 列名被人工改过 | `fields` / `daily_fields` 里写当前列名 |
| `PermissionError errno=13 winerror=None`（子进程写日志失败） | DSH 沙箱对 python 子进程的跨区写拦截（合成错误） | 交互跑加 `--state-dir` 指向工作区内目录；计划任务不受影响 |
| token 接口返回里找不到 token | token 在响应**顶层**，不在 `data` | 脚本用 `_request()` 拿完整 JSON、业务接口才用 `_data()` |
| 日志 `[锁] 已有实例在跑` | 上一次还没结束 / 残留锁 | 先看锁文件里的 pid：进程已死时新版本会自动接管；老版本可手动删 `logs/.feishu_report.lock` |
| 当天数字第二天变了 | 快照语义（§3.1） | 正常现象，次日回补自动修正 |

---

## 9. 发布与回滚

```bash
python tests/run_all.py                 # 30 套离线测试
git commit -m "..."                     # 设计区
python scripts/deploy_runtime.py        # 检查 running/queued → pull --ff-only → 自检
```

- 这是**纯脚本**改动，发布后**不需要重启**网页服务；
- 运行区跟的是 **`dev` 分支**：如果改动提交在 `feature/*` 上，要先 cherry-pick 到 `dev` 再发布，
  否则 `deploy_runtime.py` 会报「已是最新」但其实没带上你的提交（2026-11-11 踩过）；
- 回滚：`git -C "D:\批量视频任务运行区" reset --hard ORIG_HEAD`。

---

## 10. 已知限制

- **金额是估算**：优先用记录里的 `estimated_cost`，否则按计费秒 × 单价回算；与阿里云真实账单可能有偏差；
- **18:00 快照**：当天行在次日 18:00 前是未终态数值；
- **「可用视频数量」永远人工**：脚本不知道哪些视频被采用，只能等人工回填；
- **`ZHOUHAO` 没有人员映射**：会留空，需要时在 `persons` 里补；
- **只统计 web 端任务**：自由视频 tab（`app/videogen/`）与生图 tab 不落 `web-data/jobs`，不在本报表口径内；
- **零用量账户默认也写 0 行**：想让表更短用 `--skip-zero`。

---

## 11. 变更记录

| 日期 | 变更 |
| --- | --- |
| 2026-11-07 | 首版：明细表「每日消费表」上线，每天 08:30 回补 D-3..D-1 |
| 2026-11-07 | 改为每天 **18:00**、窗口 **D-3..今天**（含当天快照） |
| 2026-11-07 | 新增「每日总消耗」汇总表（每天一行合计） |
| 2026-11-07 | 新增「每日视频统计」表（总数自动 + 可用数人工） |
| 2026-11-11 | 明细表改名「每人每日生产统计」，新增「人员」列并回填 75 行；表格定位改为 **table_id 优先**（修掉改表名导致的重复建表） |
| 2026-11-12 | 本文档 |

---

## 12. 相关文件

| 文件 | 作用 |
| --- | --- |
| [scripts/feishu_daily_report.py](../scripts/feishu_daily_report.py) | 主脚本（聚合 + 飞书 upsert + 三表维护） |
| [scripts/install_feishu_report_task.ps1](../scripts/install_feishu_report_task.ps1) | 注册/卸载 18:00 计划任务 |
| `server/feishu.json` | 凭证与表 ID（**不入库**，两区各一份） |
| `web-data/logs/feishu_report.log` | 运行日志（运行区） |
| [app/web/jobs.py:964](../app/web/jobs.py) | `usage_for()`：本脚本口径的来源 |
