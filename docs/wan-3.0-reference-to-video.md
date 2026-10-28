# 万相 3.0 · 官方 API 可执行规格（阿里云百炼）

> 接口：阿里云百炼 `wan3.0-video` / `wan3.0-video-prime`
> 能力：All-in-One，统一支持文生视频、图生视频（首帧/首尾帧）、参考生视频、视频编辑、视频延长。
> 本文件是本仓库 `app/wan_api.py` 与 `wan3_r2v.py` 的实现依据。
>
> 官方文档：
> - API 参考：https://help.aliyun.com/zh/model-studio/wan3-video-generation-api-reference
> - 使用指南：https://help.aliyun.com/zh/model-studio/wan3-video-generation-guide
> - 上传临时文件：https://help.aliyun.com/zh/model-studio/get-temporary-file-url
> - 错误码：https://help.aliyun.com/zh/model-studio/error-code
> - 限流：https://help.aliyun.com/zh/model-studio/rate-limit

---

## 1. 请求链路

```
本地服装图 ──(COS 源目录/Base64)──> URL
模板视频（公网 COS 直链）──────────> URL
      │
      ├─1─ POST {base}/services/aigc/video-generation/video-synthesis
      │        Headers: Authorization: Bearer sk-xxx
      │                 X-DashScope-Async: enable
      │        Body:    {model, input:{prompt, media[]}, parameters}
      │        └─> {output:{task_id, task_status:"PENDING"}, request_id}
      ├─2─ GET  {base}/tasks/{task_id}  ──轮询──> task_status:"SUCCEEDED" + output.video_url
      ├─3─ GET  output.video_url          ──24 小时内下载，过期永久失效
      └─4─ （可选）下载后归档到 COS
```

`base` 可用：
- 旧域名：`https://dashscope.aliyuncs.com/api/v1`
- 业务空间专属域名：`https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api/v1`

模型、Endpoint、API Key 必须同地域；否则 401/404。

---

## 2. 鉴权与请求头

| Header | 值 | 说明 |
| --- | --- | --- |
| `Content-Type` | `application/json` | 提交时必填 |
| `Authorization` | `Bearer sk-xxx` | 百炼 API Key |
| `X-DashScope-Async` | `enable` | **提交必填**；缺了会报 “current user api does not support synchronous calls” |
| `X-DashScope-OssResourceResolve` | `enable` | 仅当素材 URL 是 `oss://` 临时地址时需要 |

---

## 3. 提交请求体

```json
{
  "model": "wan3.0-video",
  "input": {
    "prompt": "视频1中的人物，把服装换成图1这套，面部与动作保持不变",
    "media": [
      {"type": "reference_video", "url": "https://.../001.mp4"},
      {"type": "reference_image", "url": "https://.../outfit.png"}
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

### 3.1 `model`

| 值 | 说明 |
| --- | --- |
| `wan3.0-video` | 标准版，质量优先 |
| `wan3.0-video-prime` | 高速版，能力对齐，端到端更快 |

### 3.2 `input.media[]`

| `type` | 数量/限制 | 用途 |
| --- | --- | --- |
| `reference_image` | ≤10 张；≤20MB；JPEG/JPG/PNG（无透明通道）/BMP/WEBP；单边 240–8000px；宽高比 ≤8:1 | 参考人物/服装/道具/场景 |
| `reference_video` | ≤5 段；单段 1–15s；总时长 ≤15s；≤100MB；MP4/MOV；≥16fps；单边 240–4096px；宽高比 ≤8:1 | 参考动作/运镜/角色/音色 |
| `reference_audio` | ≤5 段；单段 1–15s；总 ≤15s；≤15MB；WAV/MP3 | 参考音色/节奏 |
| `first_frame` | ≤1 张 | 严格首帧（与 reference_* 互斥） |
| `last_frame` | ≤1 张 | 严格尾帧（与 reference_* 互斥） |
| `file` | ≤1 个；≤100MB；≤50 页；docx/doc/xlsx/xls/pptx/ppt/pdf/txt/key/pages/numbers/md | 文档生视频 |
| `link` | ≤1 个；公开网页 | 网页生视频 |

**组合规则**：
- `first_frame`/`last_frame` 与 `reference_*`/`file`/`link` **互斥**，不能混用；
- `file` 与 `link` 二选一，但都可与 `reference_*` 组合；
- `reference_image`/`reference_video`/`reference_audio` 之间可自由组合。

**指代规则**：图片、视频、音频**分别计数**；数组里第 1 个 `reference_image` 就是 prompt 里的“图1”，第 1 个 `reference_video` 是“视频1”，以此类推。

### 3.3 `input.prompt`

- 中英文；≤20000 字符；
- 换装场景建议明确写“编辑视频1 / 替换 / 更换”，否则可能被路由成普通参考生成；
- 台词用中文引号包起来。

### 3.4 `parameters`

| 字段 | 类型 | 默认 | 取值 |
| --- | --- | --- | --- |
| `resolution` | string | `1080P` | `480P` / `720P` / `1080P` |
| `ratio` | string | `adaptive` | `adaptive` / `21:9` / `16:9` / `4:3` / `1:1` / `3:4` / `9:16` |
| `duration` | int | `5` | 无视频输入：2–30；有视频输入：输入+输出 ≤30；`-1` = 智能时长/保持源时长 |
| `audio` | bool | `true` | 输出是否带音轨；开关同价 |
| `seed` | int | 随机 | `-1` 或 `0..2147483647` |
| `prompt_extend` | bool | `true` | 智能改写；有 file/link 时必须 true |
| `watermark` | bool | `false` | 右下角“AI生成”水印 |

> `duration` 是**整数**，不是字符串。旧 RunningHub 版要 `"7"`，这里要 `7`；`app/wan_api.py` 会自动转换并校验。

---

## 4. 响应与状态机

### 4.1 创建任务

```json
{
  "output": {"task_id": "0385dc79-...", "task_status": "PENDING"},
  "request_id": "4909100c-..."
}
```

- 只拿 `output.task_id`；`task_id` 查询有效期 **24 小时**；
- 创建成功 ≠ 任务成功，必须轮询到终态。

### 4.2 状态

`PENDING`（排队）→ `RUNNING`（处理中）→ `SUCCEEDED` / `FAILED` / `CANCELED` / `UNKNOWN`

- `UNKNOWN`：task_id 不存在或已超过 24h；**不要重复提交**，标记过期，显式重跑；
- `FAILED`：读 `output.code` / `output.message`。

### 4.3 查询

```http
GET {base}/tasks/{task_id}
Authorization: Bearer sk-xxx
```

```json
{
  "request_id": "...",
  "output": {
    "task_id": "...",
    "task_status": "SUCCEEDED",
    "submit_time": "2026-08-06 10:01:35.452",
    "end_time": "2026-08-06 10:13:33.838",
    "video_url": "https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/xxx/video.mp4"
  },
  "usage": {
    "video_count": 1,
    "duration": 14.0,
    "input_video_duration": 7.0,
    "output_video_duration": 7.0,
    "fps": 30,
    "SR": 720,
    "ratio": "9:16"
  }
}
```

- `output.video_url` **24 小时**失效，拿到就下载/转存；
- 查询接口默认 RPS 20；批量轮询建议 15s + 抖动，进程内限速 10 QPS；
- `usage.duration` 是**计费时长**：有参考视频时 = 输入 + 输出（7s 模板 + 7s 输出 ≈ 14s/条），不是 7s。

---

## 5. 计费

- 官方 `usage` **没有金额字段**，只给秒数；金额要按百炼模型价格 × `usage.duration` 估算。
- 本仓库 `wan.price_per_second` 由服务端下发（可空）；没有单价时只显示计费秒数，不伪造金额。
- 参考视频输入通常计入计费时长；做预算时按 `usage.duration`，不要按输出秒数。
- 真实扣费以阿里云账单为准。

---

## 6. 素材怎么来

| 方式 | 写法 | 适用 |
| --- | --- | --- |
| 公网 URL | `https://...` | 模板视频（COS 公有读）、已有 CDN |
| COS 签名 URL | `https://...?sign=...` | 服装图（本项目默认，签 7 天） |
| Base64 Data URI | `data:image/png;base64,...` | 图片兜底；**视频/音频不支持** |
| OSS 临时 URL | `oss://dashscope-instant/...` | 官方临时上传（48h、与模型绑定、100QPS，不建议生产） |

**本项目**：
- 模板视频：`manifest.json` 里的公网 COS 直链，原样给 `reference_video`；
- 服装图：`Storage.put_path()` 上传到 `{COS_PREFIX}/output/source/<日期>/<指纹>.png`，`sign_url(7 天)`；无 COS 时转 Base64；
- 生成的成片：`output.video_url` → 下载 → 校验 → 归档 `{COS_PREFIX}/output/<日期>/<批次>/`。

> 官方错误码文档说服务端用 `User-Agent: DashScopeUserBot` 拉公网素材，第三方存储理论上可能被拦。
> **本项目已实测（2026-10-28）**：模板 080（COS 公有 URL）+ 服装图（COS 7 天签名 URL）均被官方成功拉取，
> 任务 `SUCCEEDED`，成品归档 COS。**腾讯云 COS 不影响 Wan3 接收参考视频，不需要迁 OSS。**
> 仍保留 `media_fetch` 错误处理：个别模板若不可达，先查 URL/权限/签名，再单独处理。

---

## 7. 常见错误码

| 错误码/文案 | 含义 | 处理 |
| --- | --- | --- |
| `InvalidApiKey` / 401 / 403 | Key 无效、地域/业务空间不匹配 | 检查 `wan.api_key`、`base_url`、地域 |
| `Arrearage` / `QuotaExhausted` / `BudgetLimitExceeded` | 欠费/额度/预算 | 充值或提高预算 |
| `Throttling.*` / 429 / `Request rate increased too quickly` | 限流 | 降并发、拉长轮询、指数退避 |
| `InvalidParameter` / `Model not exist` / `url error` | 参数/模型/URL 不对 | 按 message 修字段 |
| `DataInspectionFailed` / `InternalError.DataInspection` | 内容审核 | 换素材/提示词 |
| `GatewayTimeout.InputDownload` / `Failed to download multimodal content` | 官方拉不到素材 | 检查公网可达；图片改 Base64；视频迁 OSS |
| `UNKNOWN` | task_id 过期/不存在 | 标记过期，显式重跑，不自动重提 |
| `InternalError.Timeout` | 异步任务 3 小时未完成 | 稍后重试/提工单 |

---

## 8. 与旧 RunningHub 版的差异

| 维度 | RunningHub | 官方百炼 |
| --- | --- | --- |
| 提交 | `POST /openapi/v2/alibaba/wan-3.0/reference-to-video` | `POST {base}/services/aigc/video-generation/video-synthesis` |
| 查询 | `POST /openapi/v2/query` | `GET {base}/tasks/{task_id}` |
| 素材 | `imageUrls` / `videoUrls` / `audioUrls` | `input.media[]` |
| 比例 | `aspectRatio` | `parameters.ratio` |
| 时长 | `"7"` 字符串 | `7` 整数 / `-1` |
| 智能改写 | `promptExtend` | `prompt_extend` |
| 状态 | `QUEUED/RUNNING/SUCCESS/FAILED` | `PENDING/RUNNING/SUCCEEDED/FAILED/CANCELED/UNKNOWN` |
| 结果 | `results[0].url` | `output.video_url` |
| 金额 | `usage.consumeMoney` | 无，按秒数 |
| 上传 | `/media/upload/binary` | URL / Base64（仅图）/ OSS 临时 URL |

**任务模式**：
- `mode="edit"`：模板视频 + 服装图，官方按“视频编辑”路由，`ratio=adaptive`、`duration=-1`；
- `mode="reference"`：图片/视频参考生成，`ratio=9:16`、`duration=7`；
- 桌面默认 `edit`，CLI 默认 `reference`，由 `wan.default_mode` / `--mode` 控制。

---

## 9. 本仓库工具用法

```bash
# 1) 校验（不联网）
python3 wan3_r2v.py validate --tasks batch/tasks.batch.json

# 2) 打印官方请求体（不提交）
python3 wan3_r2v.py build --tasks batch/tasks.batch.json --index 0

# 3) 打样 1 条（真实扣费；这份清单只有服装图，所以 reference）
set DASHSCOPE_API_KEY=sk-xxxxxxxx
python3 wan3_r2v.py run --tasks batch/tasks.batch.json --limit 1 --mode reference

# 桌面版有模板视频，默认走 edit；想用 CLI 跑 edit，任务里要带 videoUrls
python3 wan3_r2v.py run --tasks my-edit-tasks.json --limit 1 --mode edit

# 4) 全量（并发 3，轮询 15s）
python3 wan3_r2v.py run --tasks batch/tasks.batch.json --concurrency 3

# 5) 导出 100 个官方请求（可审计）
python3 wan3_r2v.py emit --tasks batch/tasks.batch.json --out batch/requests.jsonl
```

产物：`out/<id>.mp4` + `out/state.json`（含 `task_id`、`request_id`、`usage`、`billable_seconds`）。

- `WAN_BASE_URL` 可指向本地假服务/网关，用于不花钱联调（见 `tests/README.md`）；
- 断点续跑：`state.json` 里 `success` 且本地文件存在默认跳过；`running` 且未过 24h 续查；
- 旧 RunningHub `taskId` 会被标记 `legacy`，不会用官方接口误查。
