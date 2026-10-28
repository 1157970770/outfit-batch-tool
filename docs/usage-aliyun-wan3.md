# 万相 3.0 官方 API 分支使用手册

> **分支**：`feature/aliyun-wan3-official`
> **适用对象**：桌面操作员 / 运维管理员 / 命令行开发者
> **当前状态**：真实 1 条已完成（2026-10-28，480P/5s，模板 080，COS 源图/模板拉取成功，成品已归档）；
> 剩下的是**桌面端实机 + 服务端 deploy**。
> **相关文档**：[README.md](../README.md)、[官方接口规格](wan-3.0-reference-to-video.md)、[ADR-002](adr-002-aliyun-wan3-official-api.md)
> **双版本隔离**：[version-isolation.md](version-isolation.md)（旧 RH 版原样保留，新版独立 exe/缓存/安装包）

---

## 0. 30 秒判断

| 你是谁 | 先看 |
| --- | --- |
| 只负责点软件出片 | §4 桌面操作员 |
| 负责服务端配置 / 发 Key / 部署 | §2 管理员配置、§8 上线检查 |
| 用命令行批跑 / 调接口 | §5 命令行、§3 两种模式 |
| 出错了要排查 | §10 故障排查 |
| 想回滚到 RunningHub | §11 回滚 |

**这个分支和主分支的区别**：生成接口从 RunningHub 包装层换成了阿里云百炼官方 `wan3.0-video`；客户端交互、授权、COS 归档基本不变。官方结果链接同样 **24 小时**失效，`task_id` 也只有 24 小时。

---

## 1. 前置条件

| 项 | 要求 | 怎么确认 |
| --- | --- | --- |
| 阿里云账号 | 已开通百炼，账户不欠费 | 百炼控制台能进模型广场 |
| 模型 | 目标地域已开通 `wan3.0-video`（或 `wan3.0-video-prime`） | 模型广场/模型列表能看到 |
| API Key | `sk-` 开头，和模型同地域、同业务空间 | 控制台「API-KEY」页 |
| 预算 | 建议配消费限额/告警 | 百炼「费用与成本」/预算管理 |
| 模板清单 | 108 条已发布，目标 500 | `manifest.json` 能打开 |
| COS | 成品桶可写，模板桶公有读 | `python3 scripts/cos_check.py` |
| Python（命令行/打包） | 3.9+（本机实测 3.12） | `python3 --version` |
| ffmpeg/ffprobe（可选） | 做素材时长预检 | `ffprobe -version` |

> **Key 不能提交 Git、不能贴进对话、不能打进 exe。** 客户端只从服务端下发。

---

## 2. 管理员：服务端配置

### 2.1 改哪里

只改 `server/config.json`（该文件已被 `.gitignore` 排除，不进仓库）。加一个 `wan` 块：

```json
{
  "wan": {
    "provider": "dashscope",
    "api_key": "sk-在这里填",
    "model": "wan3.0-video",
    "base_url": "https://dashscope.aliyuncs.com/api/v1",
    "region": "cn-beijing",
    "default_mode": "edit",
    "default_duration": -1,
    "default_ratio": "adaptive",
    "poll_interval": 15,
    "poll_timeout": 1800,
    "max_concurrency": 3,
    "query_qps": 10,
    "price_per_second": {
      "480P": null,
      "720P": null,
      "1080P": null
    }
  },
  "rh_api_key": "",
  "manifest_url": "https://video-input-1463907213.cos.ap-guangzhou.myqcloud.com/manifest.json",
  "cos": {
    "enabled": true,
    "bucket": "video-input-1463907213",
    "template_bucket": "video-input-1463907213",
    "output_bucket": "video-output-1463907213",
    "region": "ap-guangzhou",
    "prefix": "prod",
    "source_prefix": "",
    "domain": "",
    "secret_id": "子账号 SecretId",
    "secret_key": "子账号 SecretKey"
  }
}
```

### 2.2 字段说明

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `wan.provider` | ✅ | `dashscope`（默认）或 `runninghub`（回滚） |
| `wan.api_key` | ✅ | 百炼 Key，`sk-...` |
| `wan.model` | ✅ | `wan3.0-video`（质量优先）/ `wan3.0-video-prime`（速度优先） |
| `wan.base_url` | ✅ | `https://dashscope.aliyuncs.com/api/v1` 或 `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api/v1` |
| `wan.region` | 建议 | 仅展示/排查用；模型、Endpoint、Key 必须同地域 |
| `wan.default_mode` | ✅ | 桌面默认 `edit`；可选 `reference` |
| `wan.default_duration` | 可选 | `-1` = 保持模板时长；`7` = 固定 7s；`null` = 用模式预设 |
| `wan.default_ratio` | 可选 | `adaptive` / `9:16` / `16:9` 等；`null` = 用模式预设 |
| `wan.poll_interval` | ✅ | 轮询间隔，默认 15s；官方查询 RPS 20 |
| `wan.poll_timeout` | ✅ | 单条等待上限，默认 1800s |
| `wan.max_concurrency` | ✅ | 服务端允许的最大并发，默认 3；界面选大了会被钳制 |
| `wan.query_qps` | ✅ | 查询限速，默认 10 QPS |
| `wan.price_per_second` | 可选 | 按分辨率填单价（元/秒）；不填只显示秒数，不显示金额 |
| `cos.source_prefix` | 可选 | 服装图源目录；留空 = `{prefix}/output/source` |

> ⚠️ **模式切换的坑**：`default_duration` / `default_ratio` 是显式覆盖，不随 `default_mode` 自动变。
> 如果把 `default_mode` 改成 `reference`，请同时把 `default_duration` 改成 `7`、`default_ratio` 改成 `9:16`；
> 或者把这两个字段设为 `null`，让模式预设（edit=`-1`/`adaptive`，reference=`7`/`9:16`）生效。

### 2.3 部署与自检

```powershell
# 1) 看本地配置是否填全（只打印长度，不打印 Key）
python3 scripts\license_deploy.py config

# 2) 打包上传云函数，等 Active
python3 scripts\license_deploy.py deploy

# 3) 再查线上是否真的下发
python3 scripts\license_deploy.py config
```

期望看到：

```
[config] 线上 wan.api_key : 已下发（长度 35）
[config] 线上 wan         : model=wan3.0-video base=https://.../api/v1
[config] 线上 manifest_url : https://...
[config] 结论: 配置完整，客户端可用
```

客户端是**下次启动**才拿到新配置；改完让用户关掉重开。

### 2.4 Key 安全

- 用**专用业务空间 + 工作空间级 Key**，不要用主账号高权限 Key；
- 百炼控制台设**消费限额/告警**，超额拒绝优先于扣费；
- 轮换：改 `server/config.json` → `deploy`，客户端下次启动换新；
- 停用：删/禁用 Key；授权侧仍可用密码 `revoke` 立刻拒绝；
- 过渡期保留 `rh_api_key`，旧客户端还能启动；新客户端优先 `wan`。

---

## 3. 两种模式：`edit` / `reference`

| 模式 | 用什么 | 默认参数 | 适合 |
| --- | --- | --- | --- |
| `edit` | 模板视频 + 服装图 | `ratio=adaptive`、`duration=-1` | 换装正式生产：保脸、保动作、保背景，只换衣服 |
| `reference` | 服装图（可选参考视频） | `ratio=9:16`、`duration=7` | 没有模板视频，或想做纯参考生成 |

**桌面版默认 `edit`**：模板清单里的视频就是“待编辑视频”，服装图是参考图。
**命令行默认 `reference`**：`batch/tasks.batch.json` 只有服装图，没有 `videoUrls`。

### 3.1 prompt 怎么写

`edit` 建议明确写编辑意图：

```
编辑视频1：保持视频1中人物的面部、发型、肤色、动作、背景与镜头不变，
仅将人物身上服装替换为图1中无头模特展示的这套服装
（版型、颜色、材质、细节一致），画面自然真实，布料质感清晰
```

`reference` 建议：

```
图1中的同一位女性，保持脸型、五官、发型与妆造完全不变，只更换服装：……
```

规则：
- 图/视频/音频**分别计数**：第 1 个 `reference_image` = 图1，第 1 个 `reference_video` = 视频1；
- 批量务必 `prompt_extend=false`，否则 100 条风格会漂；
- 不自动改写用户 prompt；`edit` 缺“编辑/替换/更换”等词时只告警。

### 3.2 A/B 建议

上线前各跑 1 条同模板同服装：

| 看什么 | edit | reference |
| --- | --- | --- |
| 脸/动作/背景 | 应基本保持模板 | 可能重新生成 |
| 服装还原 | 看版型/颜色/材质 | 看参考图 |
| 时长 | 跟随模板（7s） | 默认 7s |
| 计费秒数 | 约 14s（7+7） | 约 14s（如有参考视频） |

按结果把 `wan.default_mode` 定下来，**不用重新打包**。

---

## 4. 桌面操作员

### 4.1 安装与激活

1. 拿整个 zip，解压到可写目录（不要在压缩包里直接双击 exe）；
2. 双击 `换装批量生产器.exe`；
3. 输入管理员分配的密码；
4. 顶部四个状态灯应为：
   - `配置：已下发`
   - `模板清单：108 个`
   - `授权：至 2026-xx-xx`
   - `COS：开`
5. 选一次输出目录（会记住）。

### 4.2 跑一批

1. 「小批量试跑」选一张无头模特图（服装 + 上身效果）；
2. 填批次号（字母/数字，重名自动 `-2`）；
3. 提示词可改，原文会存进 `batch.json`；
4. 点「试跑 3 条」；
5. 看表格状态：运行中 → 完成/失败/已过期；
6. 完成后打开输出目录。

大批量：
- 第 1–5 组（001–500）或「随机 100 条」；
- 并发默认 3；选 10/50/100 会被服务端 `max_concurrency` 钳制；
- 清单不足 200 条时第 2–5 组会提示“模板不足 100 个”。

### 4.3 输出长什么样

```
输出根\批次号\
├── 001.mp4 ... 100.mp4
├── report.csv     ← 状态/模式/耗时/计费秒数/输出时长/分辨率/预估费用/大小/实测时长/错误/对象Key/request_id
├── batch.json     ← 提示词原文 + provider/model/mode + 每条 task_id/usage
└── log.txt
```

开了 COS 归档时，成品还会传到 `video-output-1463907213/prod/output/<日期>/<批次>/`。

### 4.4 状态含义

| 表格状态 | 含义 | 怎么办 |
| --- | --- | --- |
| 运行中 | 已提交，等官方出片 | 等；超过 `poll_timeout` 会变“运行中”保留 task_id |
| 完成 | 下载并校验成功 | 打开 mp4 / 看 report.csv |
| 失败 | 提交/生成/下载失败 | 看备注里的错误和 request_id |
| 已过期 | `task_id` 超过 24h 查不到，或结果链接过期 | 需要重跑（会重新计费） |
| 已取消 | 官方任务被取消 | 重跑 |
| 旧任务 | 旧 RunningHub 的 taskId，官方接口查不了 | 重跑 |
| 校验失败 | 提交前校验没过（缺视频/超时长/字段错） | 按备注改素材/任务 |
| 跳过 | 批次被停止 | 重新跑 |

### 4.5 常见界面提示

- `配置：未下发` → 服务端没有 `wan.api_key`，找管理员；
- `COS：关` → 不归档、服装图走 Base64；能跑，但 100 条请求体更大，建议开；
- `模板清单：未下发/加载失败` → `manifest_url` 空或打不开；
- `并发 X 超过服务端上限 Y` → 已自动降到 Y，日志里能看到。

---

## 5. 命令行

### 5.1 环境变量

```powershell
# Key（二选一，优先 WAN_API_KEY）
$env:WAN_API_KEY="sk-..."
$env:DASHSCOPE_API_KEY="sk-..."     # 兼容官方常用名

# 接口地址（不设则用官方默认）
$env:WAN_BASE_URL="https://dashscope.aliyuncs.com/api/v1"
$env:WAN_MODEL="wan3.0-video"

# COS（本地图片/视频要上传时才需要；成品归档也需要）
$env:COS_ENABLED="true"
$env:COS_BUCKET="video-output-1463907213"
$env:COS_REGION="ap-guangzhou"
$env:COS_PREFIX="prod"
$env:COS_SECRET_ID="AKID..."
$env:COS_SECRET_KEY="..."
# 可选：$env:COS_SOURCE_PREFIX="prod/output"
```

> `set VAR=value`（cmd）与 `$env:VAR="value"`（PowerShell）等价，按你用的终端选。

### 5.2 命令速查

```powershell
# 只看校验，不发请求
python3 wan3_r2v.py validate --tasks batch\tasks.batch.json

# 打印第 1 条官方请求体（不提交）
python3 wan3_r2v.py build --tasks batch\tasks.batch.json --index 0

# 打样 1 条（真实扣费）
python3 wan3_r2v.py run --tasks batch\tasks.batch.json --limit 1 --mode reference

# 全量（并发 3，轮询 15s）
python3 wan3_r2v.py run --tasks batch\tasks.batch.json --concurrency 3

# 导出 100 个官方请求（可审计；--upload 需要 COS_*）
python3 wan3_r2v.py emit --tasks batch\tasks.batch.json --out batch\requests.jsonl

# 上传单个素材，返回 URL
python3 wan3_r2v.py upload assets\定妆图.png

# 复查某个 task_id
python3 wan3_r2v.py query <task_id>
```

### 5.3 `edit` 模式用 CLI 跑

`batch/tasks.batch.json` 没有 `videoUrls`，直接 `--mode edit` 会报“edit 需要至少一段参考视频”。
要跑 edit，自己准备一份带 `videoUrls` 的任务：

```json
{
  "defaults": {
    "mode": "edit",
    "resolution": "720P",
    "duration": -1,
    "aspectRatio": "adaptive",
    "audio": false,
    "promptExtend": false,
    "watermark": false
  },
  "tasks": [
    {
      "id": "e001",
      "prompt": "编辑视频1：保持面部/动作/背景不变，把服装替换为图1这套",
      "videoUrls": ["https://video-input-1463907213.cos.ap-guangzhou.myqcloud.com/input/tpl/001.mp4"],
      "imageUrls": ["assets/定妆图.png"],
      "output": "out/e001.mp4"
    }
  ]
}
```

然后：

```powershell
python3 wan3_r2v.py run --tasks my-edit-tasks.json --limit 1 --mode edit
```

### 5.4 断点续跑

- `out/state.json`（或 `--state-file`）记录每条 `task_id`、`request_id`、`usage`、`billable_seconds`；
- 已成功且本地文件存在 → 默认跳过；`--force` 重跑；
- `running` 且未过 24h → 续查，不重复提交；
- 超过 24h / `UNKNOWN` → 标 `expired`，需显式重跑；
- 旧 RunningHub `taskId` → 标 `legacy`，不会拿官方接口误查；
- `--fresh` 忽略旧状态文件。

### 5.5 计费估算（可选）

```powershell
# 元/秒，按分辨率；没有就不显示金额
$env:WAN_PRICE_PER_SECOND='{"720P":0.5,"1080P":0.8}'
```

`usage.duration` 是**含参考视频输入**的计费秒数：7s 模板 + 7s 输出 ≈ 14s/条。

---

## 6. 存储链路

| 环节 | 位置 | 有效期 | 说明 |
| --- | --- | --- | --- |
| 模板视频 | `video-input-.../input/tpl/NNN.mp4` | 长期 | 公有读；直接给 `reference_video` |
| 服装图 | `video-output-.../prod/output/source/<日期>/...` | 签名 **7 天**；对象建议 30 天清理 | 官方模型拉取用；**不是成品** |
| 官方结果 | 百炼返回的 `output.video_url` | **24 小时** | 成功立即下载 |
| 本地成片 | `输出根\批次号\` | 永久 | 断点续跑/校验用 |
| COS 成片 | `video-output-.../prod/output/<日期>/<批次>/` | 长期；签名 7 天 | 归档失败只记 `archive_error` |

**只有服装图上传这一环变了**：RunningHub 的 `/media/upload/binary` 不存在了；现在传 COS 可写前缀，签 7 天。模板视频和成品归档都没改结构。

### 6.1 Day-0 拉取验证（已通过）

官方文档说服务端用 `User-Agent: DashScopeUserBot` 拉公网素材，第三方存储理论上可能被拦。**本项目已实测**：

- 2026-10-28，模板 080（COS 公有 URL）+ 服装图（COS 7 天签名 URL）均被官方成功拉取；
- 任务 `SUCCEEDED`，成品已归档 COS；
- **腾讯云 COS 不影响 Wan3 接收参考视频，不需要迁 OSS。**

保留的兜底：如果以后个别模板出现 `GatewayTimeout.InputDownload` / `Failed to download multimodal content` / 403，
先查该 URL 的公网可达性/权限/签名；必要时单独把该模板放 OSS，或服装图改 Base64（≤20MB）。
不做全量 COS → OSS 迁移。

### 6.2 生命周期

- 成品 `output/`：30 天转低频，90 天转归档，365 天删除；
- 源图 `output/source/`：**30 天删除**（签 7 天足够跑完一批）；
- 未完成分片：3 天清理。

---

## 7. 计费与成本

- 官方 `usage` **没有金额**，只给 `duration` / `input_video_duration` / `output_video_duration` / `SR` / `ratio`；
- `usage.duration` 用于计费：有参考视频时 = 输入 + 输出（7s 模板 + 7s 输出 ≈ **14s/条**）；
- 100 条 ≈ **1400s** 计费时长，不是 700s；
- `wan.price_per_second` 或 `WAN_PRICE_PER_SECOND` 填单价后才显示预估金额；
- 真实扣费以阿里云账单为准。

`report.csv` 的“计费秒数 / 预估费用”就是按这个口径出的；不是旧版的 `consumeMoney`。

---

## 8. Day-0 上线检查清单

- [ ] 百炼已开通，目标地域有 `wan3.0-video`；
- [ ] 专用业务空间 + 工作空间级 Key，配了消费限额/告警；
- [ ] `server/config.json` 的 `wan` 填好，`config` 自检通过，`deploy` 后线上能看到；
- [ ] `wan.default_mode` 与 `default_duration`/`default_ratio` 匹配（edit=-1/adaptive，reference=7/9:16）；
- [ ] 模板清单可打开，模板 URL 公网可下载；
- [ ] COS 子账号能写 `prod/output/*`，`cos_check.py` 全绿；
- [x] 1 条真实任务成功（2026-10-28，480P/5s，模板 080，COS 素材拉取成功）；
- [ ] 记录 `request_id`、`usage.duration`、实际效果；
- [ ] edit vs reference A/B，确定 `default_mode`；
- [ ] 10 条小批，观察 429/审核/失败率；
- [ ] 100 条全量，核对 report.csv 与归档；
- [ ] 验收后再把 `APP_VERSION` 从 `0.9` 升到 `1.0`。

---

## 9. 测试与验收

### 9.1 离线（不花钱）

```powershell
python3 tests\test_wan_api.py
python3 tests\test_storage.py
python3 tests\test_core.py
python3 tests\test_ui.py
python3 tests\test_remote_config.py
python3 tests\test_prompts.py
python3 tests\test_license.py
python3 demo.py --count 5
python3 demo.py --count 100
```

### 9.2 真实

- 1 条：桌面小批量 3 条，或 CLI `--limit 1`；
- A/B：同模板同服装各 1 条；
- 10 条：并发 3、轮询 15s；
- 100 条：断点续跑/归档/报表；
- 打包：`scripts\build.ps1 -OutRoot build\release`。

产物：`build\release\换装批量生产器-v0.9-<日期>.zip`。

---

## 10. 故障排查

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `配置：未下发` / `缺少 API Key：服务端没有下发 wan.api_key` | 服务端没填 `wan.api_key`，或客户端没重启 | 填 `server/config.json` → `deploy` → 客户端重启 |
| `base_url 应指向 .../api/v1` | `wan.base_url` 漏了 `/api/v1` | 改成 `https://dashscope.aliyuncs.com/api/v1` 或业务空间域名 |
| 401/403 `InvalidApiKey` / `AccessDenied` | Key 错、地域/业务空间不匹配、Key 被禁用 | 核对 Key、`base_url`、地域；重新生成 |
| `Arrearage` / `BudgetLimitExceeded` | 欠费/预算用尽 | 充值或提高预算 |
| `触发限流` / 429 / `Request rate increased too quickly` | 提交或查询太快 | 降并发（3 起）、拉长 `poll_interval`、退避 |
| 400 `InvalidParameter` | `duration` 不是整数、`ratio` 不在枚举、素材超限 | 按报错字段改；`duration` 用 `7` 或 `-1` |
| `DataInspectionFailed` | 图片/视频/提示词未过审核 | 换素材/提示词；确认素材授权 |
| `官方拉不到素材` / `GatewayTimeout.InputDownload` / `Failed to download multimodal content` | 个别 URL 不可达/权限/签名问题（COS 整体已验证可用） | 先查该 URL 公网可达/权限/签名；服装图可改 Base64；个别模板再单独迁 OSS |
| `任务已过期（task_id 超过 24h 查不到）` | task_id 过期/UNKNOWN | 显式重跑（会重新计费），不要重复轮询 |
| 下载 403/404 / 结果链接失效 | 结果 URL 过期 | 重跑；成功即下载 |
| `本地视频 ... 需要先上传成公网 URL` | 没配 `COS_*`，官方视频不支持 Base64 | 配 COS 或改用公网 URL |
| `源图上传失败` / COS 403 | 子账号没有 `prod/output/*` 权限，或桶/地域错 | 对照 [cos-setup.md](cos-setup.md) 改 CAM |
| `图片 ... 宽高比 ... 超过 8:1` / `尺寸 ...` | 素材不符合官方限制 | 裁剪/缩放；单边 240–8000px |
| `图片带透明通道` 相关 | PNG 有 alpha | 代码会自动转白底 JPEG；失败就手动另存 JPG |
| `duration 只能是 -1 或 2–30` | 传了 `"auto"` 以外的字符串/超范围 | 用整数；`auto` 会映射 `-1` |
| `未知字段` | 任务 JSON 拼错 | 对照字段表；`webhookUrl` 已不支持 |
| `旧任务` | 旧 RunningHub `taskId` | 重跑；新任务用官方接口 |
| `COS：关` | 服务端 `cos.enabled=false` | 需要归档/源图上传就打开 |
| 预览/下载成品打不开 | 归档失败或对象 Key 不对 | 看 `report.csv` 的对象 Key 和 `archive_error` |

排错时优先提供：**`request_id`**、任务 id、`status`、`error`、`usage`。官方工单基本都问 `request_id`。

---

## 11. 回滚到 RunningHub

服务端改配置即可，不用重打包：

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

`python3 scripts\license_deploy.py deploy` → 客户端重启。

注意：
- 旧客户端只认 `rh_api_key`；过渡期服务端同时保留 `wan` 与 `rh_api_key`；
- 官方 task_id 和 RH task_id 不通用，切换后旧批次不要续跑；
- 也可以 `git revert` 本分支的代码提交。

---

## 12. 常见问题

**Q：为什么 `duration` 不是 `"7"`？**
A：官方要整数 `7`；`"7"` 是旧 RunningHub 的写法。工具会自动转换并校验。

**Q：为什么报表没有“扣费 ¥”？**
A：官方 `usage` 没有金额，只有秒数。填 `price_per_second` 才有“预估费用”，真实金额看阿里云账单。

**Q：为什么服装图跑到 `output/source`？**
A：RunningHub 上传接口没了；官方只收公网 URL/OSS/图片 Base64。放在现有子账号可写的 `prod/output/source/` 不用改 CAM，签 7 天给模型拉。

**Q：为什么 100 条计费 1400s？**
A：`usage.duration` 含参考视频输入：7s 模板 + 7s 输出 = 14s/条。

**Q：为什么任务会“已过期”？**
A：官方 `task_id` 和结果链接都只有 24 小时；超过就查不到，只能重跑。

**Q：为什么 `--mode edit` 报缺视频？**
A：`batch/tasks.batch.json` 只有服装图。edit 必须有 `videoUrls`；桌面版从模板清单自动带。

**Q：桌面版和 CLI 结果为什么不一样？**
A：桌面默认 `edit`（模板当待编辑视频）；CLI 默认 `reference`（只有服装图）。不是同一模式。

**Q：旧客户端还能用吗？**
A：服务端同时下发 `rh_api_key` 时能启动；但旧客户端只会走 RunningHub，不会用 `wan`。

---

## 13. 文件索引

| 文件 | 作用 |
| --- | --- |
| [app/wan_api.py](../app/wan_api.py) | 官方 DashScope / 旧 RunningHub 双协议 |
| [app/media.py](../app/media.py) | 图片预检、COS 源图、Base64、视频探测 |
| [app/core.py](../app/core.py) | 引擎：提交/轮询/下载/校验/归档 |
| [app/main.py](../app/main.py) | 桌面界面 |
| [app/config.py](../app/config.py) | 服务端配置映射 |
| [wan3_r2v.py](../wan3_r2v.py) | 命令行客户端 |
| [tests/mock_dashscope.py](../tests/mock_dashscope.py) | 本地假百炼 |
| [docs/wan-3.0-reference-to-video.md](wan-3.0-reference-to-video.md) | 官方接口规格 |
| [docs/adr-002-aliyun-wan3-official-api.md](adr-002-aliyun-wan3-official-api.md) | 决策/风险/回滚 |
| [docs/cos-setup.md](cos-setup.md) | COS 建桶/CAM/生命周期 |

---

## 14. 附录：官方请求体

### edit（桌面默认）

```json
{
  "model": "wan3.0-video",
  "input": {
    "prompt": "编辑视频1：保持面部/动作/背景不变，把服装替换为图1这套",
    "media": [
      {"type": "reference_video", "url": "https://.../input/tpl/001.mp4"},
      {"type": "reference_image", "url": "https://.../prod/output/source/...png?sign=..."}
    ]
  },
  "parameters": {
    "resolution": "720P",
    "ratio": "adaptive",
    "duration": -1,
    "audio": false,
    "prompt_extend": false,
    "watermark": false
  }
}
```

### reference（CLI 默认）

```json
{
  "model": "wan3.0-video",
  "input": {
    "prompt": "图1中的女性，保持面部不变，只换服装",
    "media": [
      {"type": "reference_image", "url": "https://.../outfit.png"}
    ]
  },
  "parameters": {
    "resolution": "720P",
    "ratio": "9:16",
    "duration": 7,
    "audio": false,
    "prompt_extend": false,
    "watermark": false
  }
}
```
